from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .hashing import file_hash


LOCKFILE_NAME = "lmake.lock"
LOCK_TOP_LEVEL_KEYS = {"version", "models"}
MODEL_LOCK_KEYS = {"provider", "resolved", "pinned_at", "source", "notes"}


@dataclass(frozen=True)
class ModelLockEntry:
    alias: str
    resolved: str
    provider: str | None
    pinned_at: str | None
    raw: dict[str, Any]

    def fingerprint_record(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "resolved": self.resolved,
            "provider": self.provider,
            "pinned_at": self.pinned_at,
        }


@dataclass(frozen=True)
class ModelResolution:
    model: str
    alias: str | None
    lock: ModelLockEntry | None


@dataclass(frozen=True)
class ProjectLock:
    root: Path
    path: Path
    raw: dict[str, Any]
    models: dict[str, ModelLockEntry]

    @classmethod
    def load(cls, root: Path) -> "ProjectLock":
        path = root / LOCKFILE_NAME
        if not path.exists():
            return cls(root=root, path=path, raw={"version": 1, "models": {}}, models={})
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - exact yaml exceptions are not important here
            raise ConfigError(f"Could not parse {path}: {exc}") from exc
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{LOCKFILE_NAME} must contain a YAML mapping at the top level.")
        unknown = sorted((key for key in data if not isinstance(key, str) or key not in LOCK_TOP_LEVEL_KEYS), key=str)
        if unknown:
            raise ConfigError(f"{LOCKFILE_NAME} has unknown field {unknown[0]!r}. Allowed fields: models, version.")
        version = data.get("version", 1)
        if version not in (1, "1"):
            raise ConfigError(f"{LOCKFILE_NAME} version must be 1.")
        raw_models = data.get("models")
        if raw_models is None:
            raw_models = {}
        if not isinstance(raw_models, dict):
            raise ConfigError(f"{LOCKFILE_NAME}.models must be a mapping.")

        models: dict[str, ModelLockEntry] = {}
        for alias, spec in raw_models.items():
            if not isinstance(alias, str) or not alias:
                raise ConfigError(f"{LOCKFILE_NAME}.models keys must be non-empty strings.")
            if isinstance(spec, str):
                spec = {"resolved": spec}
            if not isinstance(spec, dict):
                raise ConfigError(f"{LOCKFILE_NAME}.models.{alias} must be a string or mapping.")
            unknown_model_keys = sorted((key for key in spec if not isinstance(key, str) or key not in MODEL_LOCK_KEYS), key=str)
            if unknown_model_keys:
                raise ConfigError(f"{LOCKFILE_NAME}.models.{alias} has unknown field {unknown_model_keys[0]!r}.")
            resolved = spec.get("resolved")
            if not isinstance(resolved, str) or not resolved:
                raise ConfigError(f"{LOCKFILE_NAME}.models.{alias}.resolved must be a non-empty string.")
            provider = spec.get("provider")
            if provider is not None and (not isinstance(provider, str) or not provider):
                raise ConfigError(f"{LOCKFILE_NAME}.models.{alias}.provider must be a non-empty string.")
            pinned_at = spec.get("pinned_at")
            if pinned_at is not None and not isinstance(pinned_at, str):
                raise ConfigError(f"{LOCKFILE_NAME}.models.{alias}.pinned_at must be a string.")
            models[alias] = ModelLockEntry(
                alias=alias,
                resolved=resolved,
                provider=provider,
                pinned_at=pinned_at,
                raw=dict(spec),
            )
        return cls(root=root, path=path, raw=data, models=models)

    def resolve_model(self, model: str, provider: str, field_name: str) -> ModelResolution:
        entry = self.models.get(model)
        if entry is not None:
            if entry.provider is not None and entry.provider != provider:
                raise ConfigError(
                    f"{LOCKFILE_NAME}.models.{model}.provider is {entry.provider!r}, "
                    f"but {field_name} uses provider {provider!r}."
                )
            return ModelResolution(model=entry.resolved, alias=model, lock=entry)
        if requires_pin(model):
            raise ConfigError(f"{field_name} uses floating model alias {model!r}; pin it with `lmake lock set {model} <resolved-model>`.")
        return ModelResolution(model=model, alias=None, lock=None)

    def file_record(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return {
            "path": LOCKFILE_NAME,
            "sha256": file_hash(self.path),
            "bytes": self.path.stat().st_size,
        }


def requires_pin(model: str) -> bool:
    return "latest" in model.lower()


def load_lock_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "models": {}}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{LOCKFILE_NAME} must contain a YAML mapping at the top level.")
    return data


def write_lock_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def set_model_pin(root: Path, alias: str, resolved: str, provider: str | None = None) -> ModelLockEntry:
    if not alias:
        raise ConfigError("model alias must not be empty.")
    if not resolved:
        raise ConfigError("resolved model must not be empty.")
    if alias == resolved:
        raise ConfigError("model alias and resolved model must be different.")
    path = root / LOCKFILE_NAME
    data = load_lock_yaml(path)
    data["version"] = 1
    models = data.setdefault("models", {})
    if not isinstance(models, dict):
        raise ConfigError(f"{LOCKFILE_NAME}.models must be a mapping.")
    entry: dict[str, Any] = {
        "resolved": resolved,
        "pinned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if provider is not None:
        if not provider:
            raise ConfigError("provider must not be empty.")
        entry["provider"] = provider
    models[alias] = entry
    write_lock_yaml(path, data)
    return ProjectLock.load(root).models[alias]


def remove_model_pin(root: Path, alias: str) -> bool:
    path = root / LOCKFILE_NAME
    data = load_lock_yaml(path)
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ConfigError(f"{LOCKFILE_NAME}.models must be a mapping.")
    existed = alias in models
    models.pop(alias, None)
    data["version"] = 1
    data["models"] = models
    write_lock_yaml(path, data)
    return existed

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, TargetError
from .hashing import hash_json
from .lockfile import ModelLockEntry, ProjectLock


LMFILE_NAMES = ("lmakefile.yaml", "lmakefile.yml")
TOP_LEVEL_KEYS = {"version", "defaults", "default_group", "default_target", "groups", "targets"}
DEFAULT_KEYS = {"runner", "provider", "model", "params", "cache", "system"}
TARGET_KEYS = {
    "description",
    "needs",
    "inputs",
    "prompt",
    "prompt_text",
    "runner",
    "program",
    "programs",
    "dspy",
    "outputs",
    "provider",
    "model",
    "params",
    "cache",
    "system",
    "judges",
}
GROUP_KEYS = {"description", "targets"}
CACHE_KEYS = {"reuse_policy", "ttl_seconds"}
SUPPORTED_RUNNERS = {"provider", "dspy"}
SUPPORTED_PROVIDERS = {"mock", "litellm"}
RESERVED_PATH_DIRS = {".lmcache", "runs"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
FIELD_HINTS = {
    "target": "targets",
    "group": "groups",
    "default_target": "default_group",
    "input": "inputs",
    "output": "outputs",
    "promt": "prompt",
    "prompt_file": "prompt",
}


def find_project_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for directory in [cur, *cur.parents]:
        if any((directory / name).exists() for name in LMFILE_NAMES):
            return directory
    raise ConfigError("Could not find lmakefile.yaml in the current directory or its parents.")


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact yaml exceptions are not important here
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return data


def validate_name(name: str, field_name: str) -> None:
    if not name:
        raise ConfigError(f"{field_name} must not be empty.")
    if not NAME_RE.fullmatch(name):
        raise ConfigError(f"{field_name} must use only letters, numbers, dots, underscores, and hyphens.")


def as_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    raise ConfigError(f"{field_name} must be a string or a list of strings.")


def normalize_outputs(value: Any, field_name: str = "outputs") -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        names = [Path(x).name for x in value]
        if "" in names:
            raise ConfigError(f"{field_name} must not include empty output paths.")
        duplicate_names = sorted(name for name in set(names) if names.count(name) > 1)
        if duplicate_names:
            raise ConfigError(f"{field_name} creates duplicate logical output name {duplicate_names[0]!r}; use a mapping instead.")
        outputs = dict(zip(names, value, strict=True))
        return outputs
    if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        if any(not k for k in value):
            raise ConfigError(f"{field_name} output names must not be empty.")
        if any(not v for v in value.values()):
            raise ConfigError(f"{field_name} output paths must not be empty.")
        return dict(value)
    raise ConfigError(f"{field_name} must be a list of paths or a mapping of logical names to paths.")


def normalize_group_targets(value: Any, field_name: str) -> list[str]:
    if isinstance(value, dict):
        value = value.get("targets")
    return as_list(value, field_name)


def check_unknown_keys(mapping: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted((key for key in mapping if not isinstance(key, str) or key not in allowed), key=str)
    if not unknown:
        return
    key = unknown[0]
    hint = ""
    if isinstance(key, str) and key in FIELD_HINTS:
        hint = f" Did you mean {FIELD_HINTS[key]!r}?"
    allowed_list = ", ".join(sorted(allowed))
    raise ConfigError(f"{label} has unknown field {key!r}.{hint} Allowed fields: {allowed_list}.")


def require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be a mapping.")
    return value


def optional_string(value: Any, field_name: str, *, allow_empty: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string.")
    if not allow_empty and value == "":
        raise ConfigError(f"{field_name} must not be empty.")
    return value


def require_string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    result = optional_string(value, field_name, allow_empty=allow_empty)
    if result is None:
        raise ConfigError(f"{field_name} must be a string.")
    return result


def validate_cache(cache: dict[str, Any], field_name: str) -> dict[str, Any]:
    check_unknown_keys(cache, CACHE_KEYS, field_name)
    policy = cache.get("reuse_policy")
    if policy is not None and policy != "input-identical":
        raise ConfigError(f"{field_name}.reuse_policy must be 'input-identical'.")
    ttl = cache.get("ttl_seconds")
    if ttl is not None:
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl < 0:
            raise ConfigError(f"{field_name}.ttl_seconds must be a non-negative number.")
    return cache


def validate_runner(value: Any, field_name: str) -> str:
    runner = require_string(value, field_name).lower()
    if runner not in SUPPORTED_RUNNERS:
        supported = ", ".join(sorted(SUPPORTED_RUNNERS))
        raise ConfigError(f"{field_name} must be one of: {supported}.")
    return runner


def validate_provider(value: Any, field_name: str) -> str:
    provider = require_string(value, field_name).lower()
    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ConfigError(f"{field_name} must be one of: {supported}.")
    return provider


def validate_project_path(root: Path, value: str, field_name: str) -> None:
    if value == "":
        raise ConfigError(f"{field_name} must not be empty.")
    if "\x00" in value:
        raise ConfigError(f"{field_name} must not contain NUL bytes.")
    path = Path(value)
    if path.is_absolute():
        raise ConfigError(f"{field_name} must be relative to the project root: {value}")
    if any(part == ".." for part in path.parts):
        raise ConfigError(f"{field_name} must not escape the project root: {value}")
    if path.parts and path.parts[0] in RESERVED_PATH_DIRS:
        raise ConfigError(f"{field_name} must not point inside {path.parts[0]}/.")
    try:
        (root / value).resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"{field_name} must stay inside the project root: {value}") from exc


def validate_path_list(root: Path, values: list[str], field_name: str) -> None:
    for index, value in enumerate(values):
        validate_project_path(root, value, f"{field_name}[{index}]")


def validate_target_paths(root: Path, name: str, spec: dict[str, Any], outputs: dict[str, str]) -> None:
    prompt = optional_string(spec.get("prompt"), f"targets.{name}.prompt")
    if prompt is not None:
        validate_project_path(root, prompt, f"targets.{name}.prompt")
    optional_string(spec.get("prompt_text"), f"targets.{name}.prompt_text", allow_empty=True)
    if prompt is not None and spec.get("prompt_text") is not None:
        raise ConfigError(f"target {name!r} must not specify both prompt and prompt_text.")
    program = optional_string(spec.get("program"), f"targets.{name}.program")
    if program is not None:
        validate_project_path(root, program, f"targets.{name}.program")
    validate_path_list(root, as_list(spec.get("programs"), f"targets.{name}.programs"), f"targets.{name}.programs")
    validate_path_list(root, as_list(spec.get("inputs"), f"targets.{name}.inputs"), f"targets.{name}.inputs")
    for logical_name, output_path in outputs.items():
        validate_project_path(root, output_path, f"targets.{name}.outputs.{logical_name}")


@dataclass(frozen=True)
class TargetConfig:
    name: str
    raw: dict[str, Any]
    runner: str
    needs: list[str]
    inputs: list[str]
    prompt: str | None
    outputs: dict[str, str]
    provider: str
    model: str
    params: dict[str, Any]
    cache: dict[str, Any]
    system: str | None = None
    model_alias: str | None = None
    model_lock: ModelLockEntry | None = None
    judges: str | None = None

    @property
    def spec_hash(self) -> str:
        return hash_json(self.raw)


def target_outputs(target: TargetConfig) -> dict[str, str]:
    if target.outputs:
        return target.outputs
    return {"default": f"artifacts/{target.name}.md"}


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    path: Path
    raw: dict[str, Any]
    targets: dict[str, TargetConfig]
    defaults: dict[str, Any]
    groups: dict[str, list[str]]
    default_group: str | None = None
    lock: ProjectLock | None = None

    @classmethod
    def load(cls, root: Path | None = None) -> "ProjectConfig":
        root = (root or find_project_root()).resolve()
        lmfile = next((root / name for name in LMFILE_NAMES if (root / name).exists()), None)
        if lmfile is None:
            raise ConfigError("Missing lmakefile.yaml.")
        raw = load_yaml(lmfile)
        lock = ProjectLock.load(root)
        check_unknown_keys(raw, TOP_LEVEL_KEYS, "lmakefile.yaml")
        version = raw.get("version", 1)
        if version not in (1, "1"):
            raise ConfigError("version must be 1 for lmakefile.yaml v0.")

        defaults = require_mapping(raw.get("defaults"), "defaults")
        check_unknown_keys(defaults, DEFAULT_KEYS, "defaults")
        default_runner = validate_runner(defaults.get("runner", "provider"), "defaults.runner")
        default_provider = validate_provider(defaults.get("provider", "mock"), "defaults.provider")
        default_model = require_string(defaults.get("model", "mock/deterministic"), "defaults.model")
        default_params = require_mapping(defaults.get("params"), "defaults.params")
        default_cache = validate_cache(require_mapping(defaults.get("cache"), "defaults.cache"), "defaults.cache")
        default_system = optional_string(defaults.get("system"), "defaults.system", allow_empty=True)

        target_blob = raw.get("targets", {})
        if not isinstance(target_blob, dict) or not target_blob:
            raise ConfigError("lmakefile.yaml must define a non-empty targets mapping.")
        raw_groups = require_mapping(raw.get("groups"), "groups")
        default_group = raw.get("default_group", raw.get("default_target"))
        if default_group is not None and not isinstance(default_group, str):
            raise ConfigError("default_group must be a string.")

        targets: dict[str, TargetConfig] = {}
        for name, spec in target_blob.items():
            if not isinstance(name, str):
                raise ConfigError("target names must be strings.")
            validate_name(name, f"target name {name!r}")
            if not isinstance(spec, dict):
                raise ConfigError(f"target {name!r} must be a mapping.")
            check_unknown_keys(spec, TARGET_KEYS, f"targets.{name}")
            merged = dict(spec)
            runner = validate_runner(spec.get("runner", default_runner), f"targets.{name}.runner")
            provider = validate_provider(spec.get("provider", default_provider), f"targets.{name}.provider")
            declared_model = require_string(spec.get("model", default_model), f"targets.{name}.model")
            model_resolution = lock.resolve_model(declared_model, provider, f"targets.{name}.model")
            params = dict(default_params)
            params.update(require_mapping(spec.get("params"), f"targets.{name}.params"))
            cache = dict(default_cache)
            cache.update(validate_cache(require_mapping(spec.get("cache"), f"targets.{name}.cache"), f"targets.{name}.cache"))
            dspy = spec.get("dspy")
            if dspy is not None and not isinstance(dspy, dict):
                raise ConfigError(f"targets.{name}.dspy must be a mapping.")
            outputs = normalize_outputs(spec.get("outputs"), f"targets.{name}.outputs")
            validate_target_paths(root, name, spec, outputs)
            prompt = optional_string(spec.get("prompt"), f"targets.{name}.prompt")
            system = optional_string(spec.get("system", default_system), f"targets.{name}.system", allow_empty=True)
            judges = optional_string(spec.get("judges"), f"targets.{name}.judges")
            targets[name] = TargetConfig(
                name=name,
                raw=merged,
                runner=runner,
                needs=as_list(spec.get("needs"), f"targets.{name}.needs"),
                inputs=as_list(spec.get("inputs"), f"targets.{name}.inputs"),
                prompt=prompt,
                outputs=outputs,
                provider=provider,
                model=model_resolution.model,
                params=params,
                cache=cache,
                system=system,
                model_alias=model_resolution.alias,
                model_lock=model_resolution.lock,
                judges=judges,
            )
        for target in targets.values():
            for need in target.needs:
                if need not in targets:
                    raise TargetError(f"target {target.name!r} needs unknown target {need!r}.")
        for target in targets.values():
            if target.judges is None:
                continue
            judged = targets.get(target.judges)
            if judged is None:
                raise ConfigError(f"targets.{target.name}.judges references unknown target {target.judges!r}.")
            if judged.name == target.name:
                raise ConfigError(f"targets.{target.name}.judges must not reference itself.")
            judged_output_paths = set(target_outputs(judged).values())
            if not any(input_path in judged_output_paths for input_path in target.inputs):
                outputs_text = ", ".join(sorted(judged_output_paths))
                raise ConfigError(
                    f"targets.{target.name}.judges must declare an input that is an output of target {judged.name!r}: {outputs_text}."
                )
            judge_outputs = target_outputs(target)
            if len(judge_outputs) != 1 and "verdict" not in judge_outputs:
                raise ConfigError(f"targets.{target.name}.outputs must have exactly one output or an output named 'verdict' when judges is set.")
        groups: dict[str, list[str]] = {}
        for name, spec in raw_groups.items():
            if not isinstance(name, str):
                raise ConfigError("group names must be strings.")
            validate_name(name, f"group name {name!r}")
            if isinstance(spec, dict):
                check_unknown_keys(spec, GROUP_KEYS, f"groups.{name}")
            groups[name] = normalize_group_targets(spec, f"groups.{name}.targets")
            if not groups[name]:
                raise ConfigError(f"groups.{name}.targets must list at least one target.")
            for target_name in groups[name]:
                if target_name not in targets:
                    raise TargetError(f"group {name!r} references unknown target {target_name!r}.")
        if default_group is not None and default_group not in groups and default_group not in targets and default_group != "all":
            raise TargetError(f"default_group {default_group!r} is not a target, group, or 'all'.")
        return cls(root=root, path=lmfile, raw=raw, targets=targets, defaults=defaults, groups=groups, default_group=default_group, lock=lock)

    def target(self, name: str) -> TargetConfig:
        try:
            return self.targets[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.targets))
            raise TargetError(f"unknown target {name!r}. Available targets: {available}") from exc

    def topo_order(self, target_name: str) -> list[str]:
        return self.topo_order_for_targets([target_name])

    def topo_order_for_targets(self, target_names: list[str]) -> list[str]:
        seen: set[str] = set()
        visiting: set[str] = set()
        order: list[str] = []

        def visit(name: str) -> None:
            if name in seen:
                return
            if name in visiting:
                raise TargetError(f"cycle detected at target {name!r}.")
            visiting.add(name)
            target = self.target(name)
            for need in target.needs:
                visit(need)
            visiting.remove(name)
            seen.add(name)
            order.append(name)

        for target_name in target_names:
            visit(target_name)
        return order

    def terminal_targets(self) -> list[str]:
        needed = {need for target in self.targets.values() for need in target.needs}
        return [name for name in self.targets if name not in needed]

    def run_roots(self, name: str | None = None) -> list[str]:
        selection = name or self.default_group or "all"
        if selection == "all":
            roots = self.terminal_targets()
        elif selection in self.groups:
            roots = self.groups[selection]
        elif selection in self.targets:
            roots = [selection]
        else:
            available = ", ".join(sorted([*self.targets, *self.groups, "all"]))
            raise TargetError(f"unknown target or group {selection!r}. Available: {available}")
        if not roots:
            raise TargetError(f"target group {selection!r} is empty.")
        return roots

    def run_order(self, name: str | None = None) -> list[str]:
        return self.topo_order_for_targets(self.run_roots(name))

    def ignored_by_default(self, relpath: str) -> bool:
        return fnmatch.fnmatch(relpath, ".lmcache/*") or fnmatch.fnmatch(relpath, "runs/*")

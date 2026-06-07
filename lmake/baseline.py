from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .errors import ConfigError, RunNotFoundError, TargetError
from .state import ProjectState, utc_now


BASELINE_SCHEMA = "lmake.baseline.v0"
APPROVAL_SCHEMA = "lmake.approval.v0"


def baseline_dir(root: Path) -> Path:
    return root / "baselines"


def baseline_path(root: Path, target: str) -> Path:
    return baseline_dir(root) / f"{target}.json"


def approvals_dir(root: Path, target: str) -> Path:
    return baseline_dir(root) / "approvals" / target


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def git_identity(root: Path) -> dict[str, Any]:
    def run(args: list[str]) -> str | None:
        try:
            out = subprocess.check_output(args, cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
            return out or None
        except Exception:
            return None

    return {
        "name": run(["git", "config", "user.name"]),
        "email": run(["git", "config", "user.email"]),
    }


def output_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": item.get("name"),
            "path": item.get("path"),
            "sha256": item.get("sha256"),
            "object": item.get("object"),
            "bytes": item.get("bytes"),
        }
        for item in manifest.get("outputs", [])
    ]


def load_baseline(root: Path, target: str) -> dict[str, Any] | None:
    path = baseline_path(root, target)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"could not read baseline for target {target!r}: {exc}") from exc
    if data.get("schema") != BASELINE_SCHEMA:
        raise ConfigError(f"baseline for target {target!r} has unsupported schema {data.get('schema')!r}.")
    if data.get("target") != target:
        raise ConfigError(f"baseline file {path} is for target {data.get('target')!r}, not {target!r}.")
    return data


def load_all_baselines(root: Path) -> list[dict[str, Any]]:
    directory = baseline_dir(root)
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("schema") == BASELINE_SCHEMA:
            records.append(data)
    return records


def baseline_run_ids(root: Path) -> set[str]:
    return {str(record.get("run_id")) for record in load_all_baselines(root) if record.get("run_id")}


def baseline_rows(root: Path, target: str | None = None) -> list[dict[str, Any]]:
    records = load_all_baselines(root)
    if target is not None:
        records = [record for record in records if record.get("target") == target]
    return [
        {
            "target": record.get("target"),
            "run_id": record.get("run_id"),
            "fingerprint": str(record.get("target_fingerprint", ""))[:12],
            "set_at": record.get("set_at"),
            "source": record.get("source"),
        }
        for record in sorted(records, key=lambda item: str(item.get("target")))
    ]


def baseline_record(config: ProjectConfig, manifest: dict[str, Any], *, source: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    target = str(manifest.get("target"))
    return {
        "schema": BASELINE_SCHEMA,
        "target": target,
        "run_id": manifest.get("run_id"),
        "set_at": utc_now(),
        "set_by": git_identity(config.root),
        "source": source,
        "previous_run_id": previous.get("run_id") if previous else None,
        "target_fingerprint": manifest.get("target_fingerprint"),
        "manifest_path": str((Path("runs") / str(manifest.get("run_id")) / "manifest.json").as_posix()),
        "outputs": output_records(manifest),
        "provider": manifest.get("fingerprint_inputs", {}).get("execution", {}).get("provider"),
        "model": manifest.get("fingerprint_inputs", {}).get("execution", {}).get("model"),
    }


def set_baseline(config: ProjectConfig, target_name: str, run_id_or_prefix: str, *, source: str = "manual") -> dict[str, Any]:
    config.target(target_name)
    state = ProjectState(config.root)
    state.ensure_dirs()
    state.recover_staged_runs()
    manifest = state.load_manifest(run_id_or_prefix)
    actual_target = str(manifest.get("target"))
    if actual_target != target_name:
        raise TargetError(f"run {manifest.get('run_id')} is for target {actual_target!r}, not {target_name!r}.")
    previous = load_baseline(config.root, target_name)
    record = baseline_record(config, manifest, source=source, previous=previous)
    write_json_atomic(baseline_path(config.root, target_name), record)
    return record


def approval_filename(record: dict[str, Any]) -> str:
    stamp = re.sub(r"[^0-9A-Za-z]+", "", str(record.get("set_at", ""))) or "approval"
    run_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(record.get("run_id", "run")))
    return f"{stamp}_{run_id}.json"


def unique_approval_path(directory: Path, record: dict[str, Any]) -> Path:
    first = directory / approval_filename(record)
    if not first.exists():
        return first
    stem = first.stem
    suffix = first.suffix
    index = 2
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def write_approval_record(config: ProjectConfig, record: dict[str, Any], eval_summary: dict[str, Any] | None) -> dict[str, Any]:
    approval = {
        "schema": APPROVAL_SCHEMA,
        "approved_at": record.get("set_at"),
        "approved_by": record.get("set_by"),
        "target": record.get("target"),
        "run_id": record.get("run_id"),
        "previous_run_id": record.get("previous_run_id"),
        "target_fingerprint": record.get("target_fingerprint"),
        "outputs": record.get("outputs", []),
        "eval_summary": eval_summary,
    }
    target = str(record.get("target"))
    directory = approvals_dir(config.root, target)
    directory.mkdir(parents=True, exist_ok=True)
    write_json_atomic(unique_approval_path(directory, record), approval)
    return approval


def latest_manifest(config: ProjectConfig, target_name: str) -> dict[str, Any]:
    config.target(target_name)
    state = ProjectState(config.root)
    state.ensure_dirs()
    state.recover_staged_runs()
    manifest = state.latest_manifest_for_target(target_name)
    if manifest is None:
        raise RunNotFoundError(f"target {target_name!r} has no latest run to approve or compare.")
    return manifest


def approve_latest(config: ProjectConfig, target_name: str, *, require_evals: bool = True) -> tuple[dict[str, Any], dict[str, Any] | None]:
    manifest = latest_manifest(config, target_name)
    from .engine import status_all

    target_status = next((status for status in status_all(config) if status.target == target_name), None)
    if target_status is None:
        raise TargetError(f"unknown target {target_name!r}.")
    if target_status.status != "fresh":
        raise ConfigError(
            f"target {target_name!r} is {target_status.status}; run `lmake run {target_name}` before approving. "
            f"Reason: {target_status.reason}"
        )
    if require_evals:
        from .evals import evaluate_target

        eval_result = evaluate_target(config, target_name, run_id=str(manifest.get("run_id")))
        eval_summary = eval_result.summary() if eval_result is not None else None
        if eval_result is not None and eval_result.failed:
            raise ConfigError(f"target {target_name!r} has failing eval cases; fix them or pass --skip-evals.")
    else:
        eval_summary = None
    record = set_baseline(config, target_name, str(manifest.get("run_id")), source="approval")
    write_approval_record(config, record, eval_summary)
    return record, eval_summary

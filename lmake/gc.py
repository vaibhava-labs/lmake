from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .baseline import baseline_run_ids
from .config import ProjectConfig
from .errors import ConfigError
from .hashing import file_hash
from .state import ProjectState


@dataclass(frozen=True)
class GcResult:
    dry_run: bool
    kept_runs: int
    pruned_runs: list[str]
    kept_objects: int
    pruned_objects: list[str]


def run_id(manifest: dict[str, Any]) -> str:
    return str(manifest.get("run_id", ""))


def output_digests(manifest: dict[str, Any]) -> set[str]:
    return {str(item.get("sha256")) for item in manifest.get("outputs", []) if item.get("sha256")}


def artifacts_match(config: ProjectConfig, manifest: dict[str, Any]) -> bool:
    outputs = manifest.get("outputs", [])
    if not outputs:
        return False
    for item in outputs:
        out_path = str(item.get("path", ""))
        expected = str(item.get("sha256", ""))
        if not out_path or not expected:
            return False
        path = config.root / out_path
        if not path.exists() or not path.is_file() or file_hash(path) != expected:
            return False
    return True


def object_digests(state: ProjectState) -> set[str]:
    if not state.objects_dir.exists():
        return set()
    return {path.name for path in state.objects_dir.glob("*/*/*") if path.is_file()}


def prune_empty_object_dirs(state: ProjectState, path: Path) -> None:
    for directory in [path.parent, path.parent.parent]:
        try:
            directory.rmdir()
        except OSError:
            pass


def collect_kept_run_ids(config: ProjectConfig, state: ProjectState, manifests: list[dict[str, Any]], keep_per_target: int) -> set[str]:
    kept: set[str] = set(baseline_run_ids(config.root))
    per_target: dict[str, int] = {}
    current_artifact_targets: set[str] = set()
    index = state.load_index()
    for target_info in index.get("targets", {}).values():
        latest = target_info.get("latest_run_id")
        if latest:
            kept.add(str(latest))
    for manifest in manifests:
        rid = run_id(manifest)
        if not rid:
            continue
        target = str(manifest.get("target", ""))
        count = per_target.get(target, 0)
        if count < keep_per_target:
            kept.add(rid)
            per_target[target] = count + 1
        if target not in current_artifact_targets and artifacts_match(config, manifest):
            kept.add(rid)
            current_artifact_targets.add(target)
    return kept


def gc_project(config: ProjectConfig, *, keep_per_target: int = 20, dry_run: bool = True) -> GcResult:
    if keep_per_target < 0:
        raise ConfigError("keep_per_target must be >= 0")
    state = ProjectState(config.root)
    state.ensure_dirs()
    state.recover_staged_runs()
    manifests = state.all_manifests()
    kept_run_ids = collect_kept_run_ids(config, state, manifests, keep_per_target)
    pruned_run_ids = [run_id(manifest) for manifest in manifests if run_id(manifest) and run_id(manifest) not in kept_run_ids]
    kept_objects_set: set[str] = set()
    for manifest in manifests:
        if run_id(manifest) in kept_run_ids:
            kept_objects_set.update(output_digests(manifest))
    all_objects = object_digests(state)
    pruned_objects = sorted(all_objects - kept_objects_set)

    if not dry_run:
        for rid in pruned_run_ids:
            shutil.rmtree(state.runs_dir / rid, ignore_errors=True)
        for digest in pruned_objects:
            path = state.object_path(digest)
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            prune_empty_object_dirs(state, path)
        index = state.load_index()
        for rid in pruned_run_ids:
            index.get("runs", {}).pop(rid, None)
        state.save_index(index)

    return GcResult(
        dry_run=dry_run,
        kept_runs=len(kept_run_ids),
        pruned_runs=pruned_run_ids,
        kept_objects=len(kept_objects_set),
        pruned_objects=pruned_objects,
    )

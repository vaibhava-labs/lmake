from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .config import ProjectConfig, TargetConfig, target_outputs
from .dspy_runner import run_dspy_target
from .errors import ConfigError, RunNotFoundError, TargetError
from .hashing import file_hash, hash_json, tree_hash
from .providers import InputPart, call_provider
from .state import ProjectState, parse_utc, utc_now


GLOB_RE = re.compile(r"[*?\[]")


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class FingerprintBundle:
    target_fingerprint: str
    fingerprint_inputs: dict[str, Any]
    prompt_text: str
    input_parts: list[InputPart]
    observed_upstream_runs: list[dict[str, Any]]


@dataclass(frozen=True)
class RunResult:
    target: str
    run_id: str
    mode: str
    cache_state: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class StatusResult:
    target: str
    status: str
    latest_run_id: str | None
    latest_cache_state: str | None
    current_fingerprint: str | None
    latest_fingerprint: str | None
    reason: str


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def has_glob(pattern: str) -> bool:
    return bool(GLOB_RE.search(pattern))


def resolve_patterns(root: Path, patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    missing: list[str] = []
    for pattern in patterns:
        if Path(pattern).is_absolute():
            raise ConfigError(f"input pattern must be relative to project root: {pattern}")
        if has_glob(pattern):
            matches = sorted(p for p in root.glob(pattern) if p.is_file())
            if not matches:
                missing.append(pattern)
            paths.extend(matches)
        else:
            p = root / pattern
            if not p.exists():
                missing.append(pattern)
            elif p.is_file():
                paths.append(p)
            else:
                raise ConfigError(f"input path is not a file: {pattern}")
    if missing:
        raise ConfigError("missing input(s): " + ", ".join(missing))
    unique: dict[str, Path] = {}
    for p in paths:
        unique[rel(root, p)] = p
    return [unique[k] for k in sorted(unique)]


def read_text_lossy(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def file_record(root: Path, path: Path) -> FileRecord:
    return FileRecord(path=rel(root, path), sha256=file_hash(path), bytes=path.stat().st_size)


def ttl_expired(target: TargetConfig, manifest: dict[str, Any]) -> bool:
    ttl = target.cache.get("ttl_seconds")
    if ttl is None:
        return False
    try:
        ttl_f = float(ttl)
    except Exception:
        raise ConfigError(f"target {target.name!r} has invalid cache.ttl_seconds: {ttl!r}")
    created = manifest.get("created_at")
    if not created:
        return True
    age = (parse_utc(utc_now()) - parse_utc(created)).total_seconds()
    return age >= ttl_f


def default_outputs(target: TargetConfig) -> dict[str, str]:
    return target_outputs(target)


def prompt_for_target(root: Path, target: TargetConfig) -> tuple[str, dict[str, Any]]:
    inline = target.raw.get("prompt_text")
    if target.prompt and inline:
        raise ConfigError(f"target {target.name!r} must not specify both prompt and prompt_text.")
    if target.prompt:
        p = root / target.prompt
        if not p.exists() or not p.is_file():
            raise ConfigError(f"target {target.name!r} prompt file is missing: {target.prompt}")
        text = read_text_lossy(p)
        rec = file_record(root, p)
        return text, {"kind": "file", **rec.__dict__}
    if inline is not None:
        if not isinstance(inline, str):
            raise ConfigError(f"target {target.name!r} prompt_text must be a string.")
        return inline, {"kind": "inline", "sha256": hash_json({"prompt_text": inline}), "bytes": len(inline.encode("utf-8"))}
    return "", {"kind": "empty", "sha256": hash_json({"prompt_text": ""}), "bytes": 0}


def program_records(root: Path, target: TargetConfig) -> list[dict[str, Any]]:
    programs = []
    raw_program = target.raw.get("program")
    raw_programs = target.raw.get("programs")
    if raw_program:
        programs.append(raw_program)
    if raw_programs:
        if isinstance(raw_programs, str):
            programs.append(raw_programs)
        elif isinstance(raw_programs, list):
            programs.extend(raw_programs)
        else:
            raise ConfigError(f"target {target.name!r} programs must be a string or list of strings.")
    if not programs:
        return []
    paths = resolve_patterns(root, [str(p) for p in programs])
    return [file_record(root, p).__dict__ for p in paths]


def output_hashes_from_manifest(manifest: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for item in manifest.get("outputs", []):
        out.append({
            "name": str(item.get("name")),
            "path": str(item.get("path")),
            "sha256": str(item.get("sha256")),
        })
    return sorted(out, key=lambda x: (x["name"], x["path"]))


def fingerprint_bundle(config: ProjectConfig, state: ProjectState, target: TargetConfig) -> FingerprintBundle:
    root = config.root
    prompt_text, prompt_record = prompt_for_target(root, target)
    input_paths = resolve_patterns(root, target.inputs)
    input_records = [file_record(root, p) for p in input_paths]
    input_parts = [InputPart(path=r.path, sha256=r.sha256, text=read_text_lossy(root / r.path)) for r in input_records]

    observed_upstream_runs: list[dict[str, Any]] = []
    upstream_for_hash: list[dict[str, Any]] = []
    for need in target.needs:
        latest = state.latest_manifest_for_target(need)
        if latest is None:
            raise TargetError(f"target {target.name!r} needs {need!r}, but {need!r} has no run yet.")
        observed_upstream_runs.append({
            "target": need,
            "run_id": latest.get("run_id"),
            "cache_state": latest.get("cache_state"),
            "created_at": latest.get("created_at"),
            "target_fingerprint": latest.get("target_fingerprint"),
        })
        upstream_for_hash.append({
            "target": need,
            "target_fingerprint": latest.get("target_fingerprint"),
            "outputs": output_hashes_from_manifest(latest),
        })

    lmakefile_record = file_record(root, config.path).__dict__
    dependency_tree = tree_hash((r.path, r.sha256) for r in input_records)
    fingerprint_inputs = {
        "schema": "lmake.fingerprint.v0",
        "tool": {"name": "lmake", "version": __version__},
        "target": target.name,
        "target_spec_hash": target.spec_hash,
        "lmakefile": lmakefile_record,
        "lmake_lock": config.lock.file_record() if config.lock else None,
        "prompt": prompt_record,
        "programs": program_records(root, target),
        "inputs": [r.__dict__ for r in input_records],
        "dependency_tree_hash": dependency_tree,
        "upstream": sorted(upstream_for_hash, key=lambda x: x["target"]),
        "execution": {
            "runner": target.runner,
            "provider": target.provider,
            "model": target.model,
            "model_alias": target.model_alias,
            "model_lock": target.model_lock.fingerprint_record() if target.model_lock else None,
            "params": target.params,
            "system_sha256": hash_json({"system": target.system}) if target.system is not None else None,
        },
    }
    target_fingerprint = hash_json(fingerprint_inputs)
    return FingerprintBundle(
        target_fingerprint=target_fingerprint,
        fingerprint_inputs=fingerprint_inputs,
        prompt_text=prompt_text,
        input_parts=input_parts,
        observed_upstream_runs=observed_upstream_runs,
    )


def collect_outputs(state: ProjectState, staged: dict[str, tuple[str, Path]]) -> list[dict[str, Any]]:
    """Hash staged outputs into CAS and return output records.

    staged: {logical_name: (final_artifact_path_str, staging_file_path)}
    """
    records: list[dict[str, Any]] = []
    for name, (out_path, staged_path) in staged.items():
        if not staged_path.exists() or not staged_path.is_file():
            raise ConfigError(f"declared output {out_path!r} was not written to staging.")
        digest = state.put_object_file(staged_path)
        records.append({
            "name": name,
            "path": out_path,
            "sha256": digest,
            "bytes": staged_path.stat().st_size,
            "object": f"sha256:{digest}",
        })
    return sorted(records, key=lambda x: x["name"])


def stage_outputs_from_manifest(state: ProjectState, manifest: dict[str, Any], staging_dir: Path) -> list[dict[str, Any]]:
    outputs = manifest.get("outputs", [])
    staging_dir.mkdir(parents=True, exist_ok=True)
    for item in outputs:
        digest = str(item.get("sha256", ""))
        name = str(item.get("name", ""))
        if not digest or not name:
            raise ConfigError(f"run {manifest.get('run_id')} has malformed output record: {item!r}")
        state.restore_object(digest, staging_dir / name)
    return outputs


def prepare_staging_dir(state: ProjectState, staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    state.write_active_marker(staging_dir)


def finalize_staged_run(config: ProjectConfig, state: ProjectState, manifest: dict[str, Any], staging_dir: Path) -> None:
    # Transaction sequence (crash-safe):
    # 1. manifest.json written (update_latest=False); run is recorded but not yet current.
    # 2. staged files promoted to artifacts/ via os.replace (atomic on same filesystem)
    # 3. index["targets"] updated; run becomes current.
    # 4. staging dir removed.
    state.write_manifest(manifest, update_latest=False)
    for item in manifest.get("outputs", []):
        dest = config.root / str(item["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_dir / str(item["name"]), dest)
    state.mark_run_latest(manifest)
    state.clear_active_marker(staging_dir)
    shutil.rmtree(staging_dir, ignore_errors=True)


def make_base_manifest(
    *,
    config: ProjectConfig,
    state: ProjectState,
    target: TargetConfig,
    bundle: FingerprintBundle,
    run_id: str,
    mode: str,
    cache_state: str,
    reused_from_run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "lmake.run.v0",
        "tool": {"name": "lmake", "version": __version__},
        "run_id": run_id,
        "target": target.name,
        "created_at": utc_now(),
        "mode": mode,
        "cache_state": cache_state,
        "reused_from_run_id": reused_from_run_id,
        "target_fingerprint": bundle.target_fingerprint,
        "fingerprint_inputs": bundle.fingerprint_inputs,
        "observed_upstream_runs": bundle.observed_upstream_runs,
        "source_tree": {
            "git": state.git_info(),
        },
        "policy": {
            "ttl_seconds": target.cache.get("ttl_seconds"),
            "reuse_policy": target.cache.get("reuse_policy", "input-identical"),
        },
    }


def run_single_target(config: ProjectConfig, state: ProjectState, target: TargetConfig, *, force: bool = False) -> RunResult:
    bundle = fingerprint_bundle(config, state, target)
    latest = state.latest_manifest_for_target(target.name)
    output_map = default_outputs(target)

    if latest and not force and latest.get("target_fingerprint") == bundle.target_fingerprint and not ttl_expired(target, latest):
        run_id = state.make_run_id(target.name, bundle.target_fingerprint, "reuse")
        staging_dir = state.runs_dir / run_id / "staging"
        prepare_staging_dir(state, staging_dir)
        outputs = stage_outputs_from_manifest(state, latest, staging_dir)
        manifest = make_base_manifest(
            config=config,
            state=state,
            target=target,
            bundle=bundle,
            run_id=run_id,
            mode="reuse",
            cache_state="input_identical_reuse",
            reused_from_run_id=str(latest.get("run_id")),
        )
        manifest["outputs"] = outputs
        manifest["provider_result"] = {"reused": True}
        finalize_staged_run(config, state, manifest, staging_dir)
        return RunResult(target.name, run_id, "reuse", "input_identical_reuse", manifest)

    if force:
        cache_state = "forced_recomputed"
    elif latest and latest.get("target_fingerprint") == bundle.target_fingerprint and ttl_expired(target, latest):
        cache_state = "policy_expired_recomputed"
    elif latest:
        cache_state = "stale_recomputed"
    else:
        cache_state = "fresh_recomputed"

    # Allocate run_id and staging dir before execution so a crash during the
    # provider/dspy call leaves no mystery bytes in artifacts/, only a staging
    # dir that recover_staged_runs() will discard on the next invocation.
    run_id = state.make_run_id(target.name, bundle.target_fingerprint, "run")
    staging_dir = state.runs_dir / run_id / "staging"
    prepare_staging_dir(state, staging_dir)

    if target.runner == "provider":
        result = call_provider(
            provider=target.provider,
            model=target.model,
            system=target.system,
            prompt_text=bundle.prompt_text,
            inputs=bundle.input_parts,
            params=target.params,
            target_name=target.name,
            target_fingerprint=bundle.target_fingerprint,
        )
        for logical_name in output_map:
            (staging_dir / logical_name).write_text(result.content, encoding="utf-8")
        runner_metadata = result.metadata
    elif target.runner == "dspy":
        result = run_dspy_target(
            root=config.root,
            target=target,
            prompt_text=bundle.prompt_text,
            inputs=bundle.input_parts,
            output_map=output_map,
            target_fingerprint=bundle.target_fingerprint,
            staging_dir=staging_dir,
        )
        for logical_name, data in (result.outputs or {}).items():
            (staging_dir / logical_name).write_bytes(data)
        runner_metadata = result.metadata
    else:
        raise ConfigError(f"target {target.name!r} has unsupported runner {target.runner!r}; use provider or dspy.")

    outputs = collect_outputs(state, {name: (out_path, staging_dir / name) for name, out_path in output_map.items()})
    manifest = make_base_manifest(
        config=config,
        state=state,
        target=target,
        bundle=bundle,
        run_id=run_id,
        mode="recompute",
        cache_state=cache_state,
    )
    manifest["outputs"] = outputs
    manifest["provider_result"] = runner_metadata

    finalize_staged_run(config, state, manifest, staging_dir)

    return RunResult(target.name, run_id, "recompute", cache_state, manifest)


def run_target(config: ProjectConfig, target_name: str | None, *, force: bool = False) -> list[RunResult]:
    state = recovered_state(config)
    results: list[RunResult] = []
    for name in config.run_order(target_name):
        result = run_single_target(config, state, config.target(name), force=force)
        results.append(result)
    return results


def recovered_state(config: ProjectConfig) -> ProjectState:
    state = ProjectState(config.root)
    state.ensure_dirs()
    state.recover_staged_runs()
    return state


def status_for_target(config: ProjectConfig, state: ProjectState, target: TargetConfig) -> StatusResult:
    latest = state.latest_manifest_for_target(target.name)
    latest_run_id = latest.get("run_id") if latest else None
    latest_fp = latest.get("target_fingerprint") if latest else None
    latest_state = latest.get("cache_state") if latest else None
    try:
        bundle = fingerprint_bundle(config, state, target)
    except Exception as exc:
        return StatusResult(target.name, "missing", latest_run_id, latest_state, None, latest_fp, str(exc))
    current_fp = bundle.target_fingerprint
    if latest is None:
        return StatusResult(target.name, "missing", None, None, current_fp, None, "no prior run")
    if latest_fp != current_fp:
        return StatusResult(target.name, "stale", latest_run_id, latest_state, current_fp, latest_fp, "declared dependencies changed")
    if ttl_expired(target, latest):
        return StatusResult(target.name, "policy-expired", latest_run_id, latest_state, current_fp, latest_fp, "cache TTL expired")
    for item in latest.get("outputs", []):
        p = config.root / str(item.get("path"))
        if not p.exists():
            return StatusResult(target.name, "outputs-missing", latest_run_id, latest_state, current_fp, latest_fp, "artifact absent from working tree but restorable from .lmcache")
        expected = str(item.get("sha256", ""))
        if expected and file_hash(p) != expected:
            return StatusResult(target.name, "outputs-changed", latest_run_id, latest_state, current_fp, latest_fp, "artifact bytes differ from latest manifest")
    return StatusResult(target.name, "fresh", latest_run_id, latest_state, current_fp, latest_fp, "up to date")


def status_all(config: ProjectConfig) -> list[StatusResult]:
    state = recovered_state(config)
    base = {name: status_for_target(config, state, target) for name, target in config.targets.items()}

    # Propagate stale dependency state upward. A target can be locally fresh
    # relative to the last run of its dependencies, while still needing a rebuild
    # because one of those dependencies is now stale against its own sources.
    changed = True
    while changed:
        changed = False
        for name, target in config.targets.items():
            current = base[name]
            for need in target.needs:
                need_status = base[need]
                if need_status.status in {"stale", "missing", "policy-expired", "outputs-missing", "outputs-changed"}:
                    if current.status in {"fresh", "outputs-missing", "outputs-changed"}:
                        base[name] = StatusResult(
                            target=name,
                            status="stale",
                            latest_run_id=current.latest_run_id,
                            latest_cache_state=current.latest_cache_state,
                            current_fingerprint=current.current_fingerprint,
                            latest_fingerprint=current.latest_fingerprint,
                            reason=f"upstream target {need!r} is {need_status.status}",
                        )
                        changed = True
                    break
    return [base[name] for name in config.targets]


def replay_run(config: ProjectConfig, run_id_or_prefix: str) -> RunResult:
    state = recovered_state(config)
    prior = state.load_manifest(run_id_or_prefix)
    target_name = str(prior.get("target"))
    target = config.target(target_name)

    fake_bundle = FingerprintBundle(
        target_fingerprint=str(prior.get("target_fingerprint")),
        fingerprint_inputs=prior.get("fingerprint_inputs", {}),
        prompt_text="",
        input_parts=[],
        observed_upstream_runs=prior.get("observed_upstream_runs", []),
    )
    new_id = state.make_run_id(target_name, fake_bundle.target_fingerprint, "replay")
    staging_dir = state.runs_dir / new_id / "staging"
    prepare_staging_dir(state, staging_dir)
    stage_outputs_from_manifest(state, prior, staging_dir)
    manifest = make_base_manifest(
        config=config,
        state=state,
        target=target,
        bundle=fake_bundle,
        run_id=new_id,
        mode="replay",
        cache_state="replay_valid",
        reused_from_run_id=str(prior.get("run_id")),
    )
    manifest["outputs"] = prior.get("outputs", [])
    manifest["provider_result"] = {"replayed": True}
    manifest["original_run"] = {
        "run_id": prior.get("run_id"),
        "created_at": prior.get("created_at"),
        "cache_state": prior.get("cache_state"),
    }
    finalize_staged_run(config, state, manifest, staging_dir)
    return RunResult(target_name, new_id, "replay", "replay_valid", manifest)


def text_from_object(state: ProjectState, item: dict[str, Any]) -> str | None:
    digest = str(item.get("sha256", ""))
    path = state.object_path(digest)
    if not path.exists():
        return None
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def diff_runs(config: ProjectConfig, left_id: str, right_id: str) -> str:
    state = recovered_state(config)
    left = state.load_manifest(left_id)
    right = state.load_manifest(right_id)
    left_outputs = {str(o.get("name")): o for o in left.get("outputs", [])}
    right_outputs = {str(o.get("name")): o for o in right.get("outputs", [])}
    names = sorted(set(left_outputs) | set(right_outputs))
    chunks: list[str] = []
    chunks.append(f"# lmake diff\nleft:  {left.get('run_id')}\nright: {right.get('run_id')}\n")
    for name in names:
        lo = left_outputs.get(name)
        ro = right_outputs.get(name)
        if lo is None:
            chunks.append(f"\n## {name}\nOnly in right: {ro.get('path')} sha256:{ro.get('sha256')}\n")
            continue
        if ro is None:
            chunks.append(f"\n## {name}\nOnly in left: {lo.get('path')} sha256:{lo.get('sha256')}\n")
            continue
        if lo.get("sha256") == ro.get("sha256"):
            chunks.append(f"\n## {name}\nunchanged sha256:{lo.get('sha256')}\n")
            continue
        lt = text_from_object(state, lo)
        rt = text_from_object(state, ro)
        chunks.append(f"\n## {name}\nleft sha256:{lo.get('sha256')}\nright sha256:{ro.get('sha256')}\n")
        if lt is None or rt is None:
            chunks.append("binary or missing object; textual diff unavailable\n")
        else:
            chunks.extend(difflib.unified_diff(
                lt.splitlines(keepends=True),
                rt.splitlines(keepends=True),
                fromfile=f"{left.get('run_id')}:{lo.get('path')}",
                tofile=f"{right.get('run_id')}:{ro.get('path')}",
            ))
    return "".join(chunks)


def logs(config: ProjectConfig, limit: int | None = None) -> list[dict[str, Any]]:
    state = recovered_state(config)
    rows = []
    for manifest in state.all_manifests():
        rows.append({
            "run_id": manifest.get("run_id"),
            "created_at": manifest.get("created_at"),
            "target": manifest.get("target"),
            "mode": manifest.get("mode"),
            "cache_state": manifest.get("cache_state"),
            "fingerprint": str(manifest.get("target_fingerprint", ""))[:12],
            "outputs": ", ".join(str(o.get("path")) for o in manifest.get("outputs", [])),
        })
    return rows[:limit] if limit else rows

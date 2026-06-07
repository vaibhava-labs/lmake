from __future__ import annotations

from typing import Any

from .baseline import load_baseline, latest_manifest
from .config import ProjectConfig
from .engine import diff_runs
from .errors import ConfigError
from .evals import evaluate_target


def output_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    return {str(item.get("name")): str(item.get("sha256")) for item in manifest.get("outputs", [])}


def execution_record(manifest: dict[str, Any]) -> dict[str, Any]:
    return dict(manifest.get("fingerprint_inputs", {}).get("execution", {}) or {})


def changed_execution(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    keys = sorted(set(left) | set(right))
    changes = []
    for key in keys:
        if left.get(key) != right.get(key):
            changes.append(f"- {key}: {left.get(key)!r} -> {right.get(key)!r}")
    return changes


def eval_section(config: ProjectConfig, target: str, baseline_run: str, latest_run: str) -> list[str]:
    baseline_eval = evaluate_target(config, target, run_id=baseline_run)
    latest_eval = evaluate_target(config, target, run_id=latest_run)
    if baseline_eval is None and latest_eval is None:
        return ["\n## Evals\n(no eval_cases suite found)\n"]
    chunks = ["\n## Evals\n"]
    if baseline_eval is not None:
        chunks.append(f"baseline: {baseline_eval.passed} passed, {baseline_eval.failed} failed\n")
    if latest_eval is not None:
        chunks.append(f"latest:   {latest_eval.passed} passed, {latest_eval.failed} failed\n")
        for result in latest_eval.results:
            marker = "PASS" if result.status == "pass" else "FAIL"
            chunks.append(f"- {marker} {result.name}: {result.reason}\n")
    return chunks


def compare_to_baseline(config: ProjectConfig, target: str, *, with_evals: bool = True) -> str:
    config.target(target)
    baseline = load_baseline(config.root, target)
    if baseline is None:
        raise ConfigError(f"target {target!r} has no baseline. Use `lmake baseline set {target} <run_id>` or `lmake approve {target}`.")
    latest = latest_manifest(config, target)
    baseline_run = str(baseline.get("run_id"))
    latest_run = str(latest.get("run_id"))

    baseline_outputs = {str(item.get("name")): str(item.get("sha256")) for item in baseline.get("outputs", [])}
    latest_outputs = output_hashes(latest)
    changed_outputs = [name for name in sorted(set(baseline_outputs) | set(latest_outputs)) if baseline_outputs.get(name) != latest_outputs.get(name)]
    same_fingerprint = baseline.get("target_fingerprint") == latest.get("target_fingerprint")
    left_exec = {
        "provider": baseline.get("provider"),
        "model": baseline.get("model"),
    }
    right_exec = execution_record(latest)

    chunks = [
        "# lmake compare\n",
        f"target: {target}\n",
        f"baseline_run: {baseline_run}\n",
        f"latest_run:   {latest_run}\n",
        f"fingerprint:  {'unchanged' if same_fingerprint else 'changed'}\n",
        f"outputs:      {'unchanged' if not changed_outputs else 'changed ' + ', '.join(changed_outputs)}\n",
    ]
    execution_changes = changed_execution(left_exec, {key: right_exec.get(key) for key in left_exec})
    if execution_changes:
        chunks.append("\n## Execution Changes\n")
        chunks.extend(line + "\n" for line in execution_changes)
    if with_evals:
        chunks.extend(eval_section(config, target, baseline_run, latest_run))
    chunks.append("\n")
    chunks.append(diff_runs(config, baseline_run, latest_run))
    return "".join(chunks)

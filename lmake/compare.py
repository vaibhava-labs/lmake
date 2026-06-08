from __future__ import annotations

import json
from typing import Any

from .baseline import load_baseline, latest_manifest
from .config import ProjectConfig
from .engine import diff_runs
from .errors import ConfigError, RunNotFoundError
from .evals import evaluate_target, text_from_output
from .state import ProjectState


INTERESTING_METRIC_KEYS = {
    "failed",
    "passed",
    "visible_outputs",
    "max_visible_gap_seconds",
    "total_cost_cents",
    "malformed_responses",
}

INTERESTING_METRIC_FRAGMENTS = (
    ".latency_ms.p95",
    ".latency.p95",
    ".cost.",
    ".cost_",
)


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


def is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def metric_path(root: str, key: str) -> str:
    return f"{root}.{key}" if root else f"$.{key}"


def flatten_numeric(value: Any, path: str = "$") -> dict[str, int | float]:
    found: dict[str, int | float] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            found.update(flatten_numeric(value[key], metric_path(path, str(key))))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(flatten_numeric(item, f"{path}[{index}]"))
    elif is_number(value):
        found[path] = value
    return found


def load_json_output(state: ProjectState, item: dict[str, Any]) -> Any | None:
    try:
        text = text_from_output(state, item)
    except (ConfigError, RunNotFoundError):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def metric_priority(path: str) -> tuple[int, str]:
    last = path.rsplit(".", 1)[-1]
    if last in INTERESTING_METRIC_KEYS or any(fragment in path for fragment in INTERESTING_METRIC_FRAGMENTS):
        return (0, path)
    return (1, path)


def format_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}"


def format_delta(value: int | float) -> str:
    formatted = format_number(value)
    if value > 0:
        return f"+{formatted}"
    return formatted


def metric_delta_rows(state: ProjectState, baseline: dict[str, Any], latest: dict[str, Any]) -> list[dict[str, str]]:
    baseline_outputs = {str(item.get("name")): item for item in baseline.get("outputs", [])}
    latest_outputs = {str(item.get("name")): item for item in latest.get("outputs", [])}
    rows: list[dict[str, str]] = []
    for output_name in sorted(set(baseline_outputs) & set(latest_outputs)):
        left_json = load_json_output(state, baseline_outputs[output_name])
        right_json = load_json_output(state, latest_outputs[output_name])
        if left_json is None or right_json is None:
            continue
        left_metrics = flatten_numeric(left_json)
        right_metrics = flatten_numeric(right_json)
        for path in sorted(set(left_metrics) & set(right_metrics), key=metric_priority):
            left_value = left_metrics[path]
            right_value = right_metrics[path]
            if left_value == right_value:
                continue
            rows.append({
                "output": output_name,
                "path": path,
                "baseline": format_number(left_value),
                "latest": format_number(right_value),
                "delta": format_delta(right_value - left_value),
            })
    return rows


def metric_delta_section(state: ProjectState, baseline: dict[str, Any], latest: dict[str, Any]) -> list[str]:
    rows = metric_delta_rows(state, baseline, latest)
    if not rows:
        return []
    chunks = [
        "\n## Metric Deltas\n",
        "| output | path | baseline | latest | delta |\n",
        "|---|---|---:|---:|---:|\n",
    ]
    for row in rows:
        chunks.append(f"| {row['output']} | `{row['path']}` | {row['baseline']} | {row['latest']} | {row['delta']} |\n")
    return chunks


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
    state = ProjectState(config.root)
    state.ensure_dirs()
    state.recover_staged_runs()
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
    chunks.extend(metric_delta_section(state, baseline, latest))
    if with_evals:
        chunks.extend(eval_section(config, target, baseline_run, latest_run))
    chunks.append("\n")
    chunks.append(diff_runs(config, baseline_run, latest_run))
    return "".join(chunks)

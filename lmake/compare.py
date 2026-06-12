from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .baseline import load_baseline, latest_manifest
from .config import ProjectConfig
from .engine import diff_runs
from .errors import ConfigError, RunNotFoundError
from .evals import EvalSuiteResult, evaluate_target, text_from_output
from .judges import JudgeAttachment, JudgeVerdictRef, collect_judge_attachments
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
    ".scores.",
)

GITHUB_DIFF_LINE_LIMIT = 300


@dataclass(frozen=True)
class CompareResult:
    target: str
    baseline_run: str
    latest_run: str
    same_fingerprint: bool
    changed_outputs: list[str]
    execution_changes: list[str]
    metric_rows: list[dict[str, str]]
    judges: list[JudgeAttachment]
    baseline_eval: EvalSuiteResult | None
    latest_eval: EvalSuiteResult | None
    diff_text: str

    @property
    def latest_evals_failed(self) -> bool:
        return self.latest_eval is not None and self.latest_eval.failed > 0


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


def metric_delta_section_from_rows(rows: list[dict[str, str]], *, heading: str = "## Metric Deltas") -> list[str]:
    if not rows:
        return []
    chunks = [
        f"\n{heading}\n",
        "| output | path | baseline | latest | delta |\n",
        "|---|---|---:|---:|---:|\n",
    ]
    for row in rows:
        chunks.append(f"| {row['output']} | `{row['path']}` | {row['baseline']} | {row['latest']} | {row['delta']} |\n")
    return chunks


def metric_delta_section(state: ProjectState, baseline: dict[str, Any], latest: dict[str, Any]) -> list[str]:
    return metric_delta_section_from_rows(metric_delta_rows(state, baseline, latest))


def eval_results(config: ProjectConfig, target: str, baseline_run: str, latest_run: str) -> tuple[EvalSuiteResult | None, EvalSuiteResult | None]:
    baseline_eval = evaluate_target(config, target, run_id=baseline_run)
    latest_eval = evaluate_target(config, target, run_id=latest_run)
    return baseline_eval, latest_eval


def text_eval_section(baseline_eval: EvalSuiteResult | None, latest_eval: EvalSuiteResult | None) -> list[str]:
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


def eval_section(config: ProjectConfig, target: str, baseline_run: str, latest_run: str) -> list[str]:
    baseline_eval, latest_eval = eval_results(config, target, baseline_run, latest_run)
    return text_eval_section(baseline_eval, latest_eval)


def github_eval_section(baseline_eval: EvalSuiteResult | None, latest_eval: EvalSuiteResult | None) -> list[str]:
    if baseline_eval is None and latest_eval is None:
        return ["\n### Evals\n(no eval_cases suite found)\n"]
    chunks = ["\n### Evals\n"]
    if baseline_eval is not None:
        chunks.append(f"baseline: {baseline_eval.passed} passed, {baseline_eval.failed} failed\n")
    if latest_eval is not None:
        chunks.append(f"latest:   {latest_eval.passed} passed, {latest_eval.failed} failed\n")
        for result in latest_eval.results:
            marker = "✅" if result.status == "pass" else "❌"
            chunks.append(f"- {marker} {result.name}: {result.reason}\n")
    return chunks


def verdict_line(label: str, verdict: JudgeVerdictRef | None, note: str | None) -> str:
    if verdict is None:
        return f"{label}: no verdict recorded" + (f" ({note})" if note else "") + "\n"
    return f"{label}: {verdict.verdict} ({verdict.run_id})\n"


def failures_line(label: str, verdict: JudgeVerdictRef | None) -> str | None:
    if verdict is None or not verdict.failures:
        return None
    return f"{label}_failures: " + ", ".join(verdict.failures) + "\n"


def judge_score_table(rows: list[dict[str, str]]) -> list[str]:
    if not rows:
        return []
    chunks = [
        "| score | baseline | latest | delta |\n",
        "|---|---:|---:|---:|\n",
    ]
    for row in rows:
        chunks.append(f"| `{row['name']}` | {row['baseline']} | {row['latest']} | {row['delta']} |\n")
    return chunks


def text_judge_section(attachments: list[JudgeAttachment]) -> list[str]:
    if not attachments:
        return []
    chunks = ["\n## Judge Verdicts\n"]
    for attachment in attachments:
        chunks.append(f"\n### {attachment.judge_target}\n")
        chunks.append(verdict_line("baseline", attachment.baseline, attachment.baseline_note))
        chunks.append(verdict_line("latest", attachment.latest, attachment.latest_note))
        for line in [
            failures_line("baseline", attachment.baseline),
            failures_line("latest", attachment.latest),
        ]:
            if line is not None:
                chunks.append(line)
        if attachment.score_rows:
            chunks.append("\n")
            chunks.extend(judge_score_table(attachment.score_rows))
    return chunks


def github_judge_section(attachments: list[JudgeAttachment]) -> list[str]:
    if not attachments:
        return []
    chunks = ["\n### Judge Verdicts\n"]
    for attachment in attachments:
        chunks.append(f"\n#### {attachment.judge_target}\n")
        chunks.append(verdict_line("baseline", attachment.baseline, attachment.baseline_note))
        chunks.append(verdict_line("latest", attachment.latest, attachment.latest_note))
        for line in [
            failures_line("baseline", attachment.baseline),
            failures_line("latest", attachment.latest),
        ]:
            if line is not None:
                chunks.append(line)
        if attachment.score_rows:
            chunks.append("\n")
            chunks.extend(judge_score_table(attachment.score_rows))
    return chunks


def collect_compare_result(config: ProjectConfig, target: str, *, with_evals: bool = True) -> CompareResult:
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

    execution_changes = changed_execution(left_exec, {key: right_exec.get(key) for key in left_exec})
    baseline_eval = None
    latest_eval = None
    if with_evals:
        baseline_eval, latest_eval = eval_results(config, target, baseline_run, latest_run)
    return CompareResult(
        target=target,
        baseline_run=baseline_run,
        latest_run=latest_run,
        same_fingerprint=same_fingerprint,
        changed_outputs=changed_outputs,
        execution_changes=execution_changes,
        metric_rows=metric_delta_rows(state, baseline, latest),
        judges=collect_judge_attachments(
            config,
            state,
            target,
            baseline_outputs=list(baseline.get("outputs", [])),
            latest_outputs=list(latest.get("outputs", [])),
            format_number=format_number,
            format_delta=format_delta,
        ),
        baseline_eval=baseline_eval,
        latest_eval=latest_eval,
        diff_text=diff_runs(config, baseline_run, latest_run),
    )


def text_compare(result: CompareResult, *, with_evals: bool = True) -> str:
    chunks = [
        "# lmake compare\n",
        f"target: {result.target}\n",
        f"baseline_run: {result.baseline_run}\n",
        f"latest_run:   {result.latest_run}\n",
        f"fingerprint:  {'unchanged' if result.same_fingerprint else 'changed'}\n",
        f"outputs:      {'unchanged' if not result.changed_outputs else 'changed ' + ', '.join(result.changed_outputs)}\n",
    ]
    if result.execution_changes:
        chunks.append("\n## Execution Changes\n")
        chunks.extend(line + "\n" for line in result.execution_changes)
    chunks.extend(metric_delta_section_from_rows(result.metric_rows))
    chunks.extend(text_judge_section(result.judges))
    if with_evals:
        chunks.extend(text_eval_section(result.baseline_eval, result.latest_eval))
    chunks.append("\n")
    chunks.append(result.diff_text)
    return "".join(chunks)


def github_judge_chip(attachment: JudgeAttachment) -> str | None:
    baseline = attachment.baseline
    latest = attachment.latest
    if baseline is None and latest is None:
        return None
    if baseline is not None and latest is None:
        return f"⚠️ {attachment.judge_target}: no latest verdict"
    if latest is not None and baseline is None:
        marker = "✅" if latest.verdict == "pass" else "⚠️"
        return f"{marker} {attachment.judge_target}: {latest.verdict}"
    assert baseline is not None and latest is not None
    if baseline.verdict != latest.verdict:
        return f"⚠️ {attachment.judge_target}: {baseline.verdict} → {latest.verdict}"
    marker = "✅" if latest.verdict == "pass" else "⚠️"
    return f"{marker} {attachment.judge_target}: {latest.verdict}"


def github_status_summary(result: CompareResult, *, with_evals: bool = True) -> str:
    parts = [
        f"{'✅' if result.same_fingerprint else '⚠️'} fingerprint {'unchanged' if result.same_fingerprint else 'changed'}",
    ]
    if result.changed_outputs:
        parts.append(f"⚠️ outputs changed: {', '.join(result.changed_outputs)}")
    else:
        parts.append("✅ outputs unchanged")
    if with_evals and result.latest_eval is not None:
        if result.latest_eval.failed:
            parts.append(f"⚠️ evals failed: {result.latest_eval.passed} passed, {result.latest_eval.failed} failed")
        else:
            parts.append(f"✅ evals passed: {result.latest_eval.passed} passed")
    for attachment in result.judges:
        chip = github_judge_chip(attachment)
        if chip is not None:
            parts.append(chip)
    return " · ".join(parts)


def github_artifact_diff(diff_text: str, *, limit: int = GITHUB_DIFF_LINE_LIMIT) -> str:
    lines = diff_text.splitlines()
    if len(lines) > limit:
        remaining = len(lines) - limit
        lines = lines[:limit] + [f"... truncated ({remaining} more lines)"]
    body = "\n".join(lines)
    if body:
        body += "\n"
    return f"<details><summary>artifact diff</summary>\n\n````diff\n{body}````\n\n</details>\n"


def github_compare(result: CompareResult, *, with_evals: bool = True) -> str:
    chunks = [
        f"<!-- lmake-compare: {result.target} -->\n",
        f"### lmake compare: {result.target}\n",
        github_status_summary(result, with_evals=with_evals) + "\n",
        f"\nbaseline_run: {result.baseline_run}\n",
        f"latest_run:   {result.latest_run}\n",
        f"fingerprint:  {'unchanged' if result.same_fingerprint else 'changed'}\n",
        f"outputs:      {'unchanged' if not result.changed_outputs else 'changed ' + ', '.join(result.changed_outputs)}\n",
    ]
    if result.execution_changes:
        chunks.append("\n### Execution Changes\n")
        chunks.extend(line + "\n" for line in result.execution_changes)
    chunks.extend(metric_delta_section_from_rows(result.metric_rows, heading="### Metric Deltas"))
    chunks.extend(github_judge_section(result.judges))
    if with_evals:
        chunks.extend(github_eval_section(result.baseline_eval, result.latest_eval))
    chunks.append("\n")
    chunks.append(github_artifact_diff(result.diff_text))
    return "".join(chunks)


def render_compare_result(result: CompareResult, *, fmt: str = "text", with_evals: bool = True) -> str:
    if fmt == "text":
        return text_compare(result, with_evals=with_evals)
    if fmt == "github":
        return github_compare(result, with_evals=with_evals)
    raise ConfigError("compare format must be one of: github, text.")


def compare_exit_code(result: CompareResult) -> int:
    if result.changed_outputs or result.latest_evals_failed:
        return 1
    return 0


def compare_to_baseline(config: ProjectConfig, target: str, *, with_evals: bool = True, fmt: str = "text") -> str:
    result = collect_compare_result(config, target, with_evals=with_evals)
    return render_compare_result(result, fmt=fmt, with_evals=with_evals)

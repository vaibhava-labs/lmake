from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .config import ProjectConfig, TargetConfig, target_outputs
from .state import ProjectState


JUDGE_VERDICT_SCHEMA = "lmake.judge_verdict.v0"


@dataclass(frozen=True)
class JudgeVerdictRef:
    run_id: str
    verdict: str
    scores: dict[str, int | float]
    failures: list[str]


@dataclass(frozen=True)
class JudgeAttachment:
    judge_target: str
    baseline: JudgeVerdictRef | None
    latest: JudgeVerdictRef | None
    score_rows: list[dict[str, str]]
    baseline_note: str | None = None
    latest_note: str | None = None


def verdict_output_name(target: TargetConfig) -> str:
    outputs = target_outputs(target)
    if "verdict" in outputs:
        return "verdict"
    return next(iter(outputs))


def output_pairs(outputs: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (str(item.get("path")), str(item.get("sha256")))
        for item in outputs
        if item.get("path") and item.get("sha256")
    }


def manifest_matches_judged_output(manifest: dict[str, Any], judged_outputs: list[dict[str, Any]]) -> bool:
    pairs = output_pairs(judged_outputs)
    if not pairs:
        return False
    for item in manifest.get("fingerprint_inputs", {}).get("inputs", []):
        if (str(item.get("path")), str(item.get("sha256"))) in pairs:
            return True
    return False


def manifest_output(manifest: dict[str, Any], name: str) -> dict[str, Any] | None:
    for item in manifest.get("outputs", []):
        if item.get("name") == name:
            return item
    return None


def load_verdict_ref(state: ProjectState, manifest: dict[str, Any], output_name: str) -> tuple[JudgeVerdictRef | None, str | None]:
    run_id = str(manifest.get("run_id"))
    item = manifest_output(manifest, output_name)
    if item is None:
        return None, f"run {run_id} has no output {output_name!r}"
    digest = str(item.get("sha256", ""))
    if not digest:
        return None, f"run {run_id} verdict output is missing sha256"
    path = state.object_path(digest)
    if not path.exists():
        return None, f"object sha256:{digest} is missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return None, f"run {run_id} verdict output is not UTF-8"
    except json.JSONDecodeError as exc:
        return None, f"run {run_id} verdict output is not valid JSON: {exc.msg}"
    if payload.get("schema") != JUDGE_VERDICT_SCHEMA:
        return None, f"run {run_id} verdict schema is {payload.get('schema')!r}"

    raw_scores = payload.get("scores", {})
    scores: dict[str, int | float] = {}
    if isinstance(raw_scores, dict):
        for name, value in raw_scores.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            scores[str(name)] = value
    raw_failures = payload.get("failures", [])
    failures = [str(item) for item in raw_failures] if isinstance(raw_failures, list) else []
    verdict = str(payload.get("verdict", "unknown"))
    return JudgeVerdictRef(run_id=run_id, verdict=verdict, scores=scores, failures=failures), None


def find_verdict_for_outputs(
    state: ProjectState,
    *,
    judge_target: str,
    output_name: str,
    judged_outputs: list[dict[str, Any]],
) -> tuple[JudgeVerdictRef | None, str | None]:
    if not output_pairs(judged_outputs):
        return None, "judged run has no output hash"
    for manifest in state.all_manifests():
        if manifest.get("target") != judge_target:
            continue
        if manifest_matches_judged_output(manifest, judged_outputs):
            return load_verdict_ref(state, manifest, output_name)
    return None, "no matching artifact bytes"


def score_delta_rows(
    baseline: JudgeVerdictRef | None,
    latest: JudgeVerdictRef | None,
    *,
    format_number: Callable[[int | float], str],
    format_delta: Callable[[int | float], str],
) -> list[dict[str, str]]:
    if baseline is None or latest is None:
        return []
    rows: list[dict[str, str]] = []
    for name in sorted(set(baseline.scores) & set(latest.scores)):
        left = baseline.scores[name]
        right = latest.scores[name]
        if left == right:
            continue
        rows.append({
            "name": name,
            "baseline": format_number(left),
            "latest": format_number(right),
            "delta": format_delta(right - left),
        })
    return rows


def collect_judge_attachments(
    config: ProjectConfig,
    state: ProjectState,
    target: str,
    *,
    baseline_outputs: list[dict[str, Any]],
    latest_outputs: list[dict[str, Any]],
    format_number: Callable[[int | float], str],
    format_delta: Callable[[int | float], str],
) -> list[JudgeAttachment]:
    attachments: list[JudgeAttachment] = []
    for judge in sorted(config.targets.values(), key=lambda item: item.name):
        if judge.judges != target:
            continue
        output_name = verdict_output_name(judge)
        baseline, baseline_note = find_verdict_for_outputs(
            state,
            judge_target=judge.name,
            output_name=output_name,
            judged_outputs=baseline_outputs,
        )
        latest, latest_note = find_verdict_for_outputs(
            state,
            judge_target=judge.name,
            output_name=output_name,
            judged_outputs=latest_outputs,
        )
        attachments.append(JudgeAttachment(
            judge_target=judge.name,
            baseline=baseline,
            latest=latest,
            score_rows=score_delta_rows(baseline, latest, format_number=format_number, format_delta=format_delta),
            baseline_note=baseline_note if baseline is None else None,
            latest_note=latest_note if latest is None else None,
        ))
    return attachments

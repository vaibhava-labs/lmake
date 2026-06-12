import json
import os
from pathlib import Path

import pytest

from lmake.baseline import load_baseline
from lmake.cli import main
from lmake.compare import compare_to_baseline
from lmake.config import ProjectConfig
from lmake.engine import status_all
from lmake.evals import evaluate_target
from lmake.publish import publish_run
from lmake.state import ProjectState


pytestmark = pytest.mark.integration_llm


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip(), encoding="utf-8")


def latest_output_hash(manifest: dict, output_name: str) -> str:
    for item in manifest.get("outputs", []):
        if item.get("name") == output_name:
            return str(item.get("sha256"))
    raise AssertionError(f"missing output {output_name!r} in {manifest.get('run_id')}")


def require_real_llm() -> None:
    if os.environ.get("LMAKE_RUN_LLM_TESTS") != "1":
        pytest.skip("set LMAKE_RUN_LLM_TESTS=1 to run real LLM integration tests")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY is required for the Haiku integration test")
    pytest.importorskip("litellm")


def test_haiku_prompt_iteration_review_loop(tmp_path):
    require_real_llm()
    model = os.environ.get("LMAKE_LLM_MODEL", "anthropic/claude-haiku-4-5-20251001")

    write(
        tmp_path / "lmakefile.yaml",
        f"""
version: 1

defaults:
  provider: litellm
  model: {model}
  params:
    temperature: 0
    max_tokens: 260
  cache:
    reuse_policy: input-identical
  system: |
    Follow the prompt exactly. Return concise Markdown only.

default_group: update

groups:
  update:
    targets:
      - report

targets:
  extract:
    inputs:
      - context/source.md
    prompt: prompts/extract.md
    outputs:
      notes: artifacts/notes.md

  report:
    needs:
      - extract
    inputs:
      - context/source.md
      - artifacts/notes.md
    prompt: prompts/report.md
    outputs:
      report: artifacts/report.md
""",
    )
    write(
        tmp_path / "context" / "source.md",
        """
# Source

source fact: alpha

The team needs traceable updates for generated brief artifacts.
""",
    )
    write(
        tmp_path / "prompts" / "extract.md",
        """
Return Markdown with heading "# Extracted Notes".
Include the exact phrase "source fact: alpha".
Keep under 60 words.
""",
    )
    report_prompt = tmp_path / "prompts" / "report.md"
    write(
        report_prompt,
        """
Return Markdown with heading "# Iteration Proof".
Mention the exact phrase "source fact: alpha".
Mention that this is a baseline candidate.
Keep under 90 words.
""",
    )
    write(
        tmp_path / "eval_cases" / "report.yaml",
        """
version: 1
target: report
cases:
  - name: report has iteration heading
    output: report
    required_headings:
      - Iteration Proof
  - name: report preserves source fact
    output: report
    regex: "(?i)source fact: alpha"
  - name: report stays concise
    output: report
    max_words: 180
""",
    )

    assert main(["-C", str(tmp_path), "run"]) == 0
    config = ProjectConfig.load(tmp_path)
    state = ProjectState(tmp_path)
    first_extract = state.latest_manifest_for_target("extract")
    first_report = state.latest_manifest_for_target("report")
    assert first_extract is not None
    assert first_report is not None
    assert first_extract["mode"] == "recompute"
    assert first_report["mode"] == "recompute"
    assert first_report["provider_result"]["provider"] == "litellm"
    assert first_report["provider_result"]["model"] == model
    assert first_report["fingerprint_inputs"]["execution"]["provider"] == "litellm"
    assert first_report["fingerprint_inputs"]["execution"]["model"] == model

    first_eval = evaluate_target(config, "report")
    assert first_eval is not None
    assert first_eval.failed == 0
    assert main(["-C", str(tmp_path), "approve", "report"]) == 0
    baseline = load_baseline(tmp_path, "report")
    assert baseline is not None
    assert baseline["run_id"] == first_report["run_id"]

    report_prompt.write_text(
        report_prompt.read_text(encoding="utf-8")
        + "\nAdd one bullet with the exact phrase \"launch-readiness risk\".\n",
        encoding="utf-8",
    )
    statuses = {item.target: item.status for item in status_all(ProjectConfig.load(tmp_path))}
    assert statuses["extract"] == "fresh"
    assert statuses["report"] == "stale"

    assert main(["-C", str(tmp_path), "run"]) == 0
    updated_state = ProjectState(tmp_path)
    latest_extract = updated_state.latest_manifest_for_target("extract")
    latest_report = updated_state.latest_manifest_for_target("report")
    assert latest_extract is not None
    assert latest_report is not None
    assert latest_extract["mode"] == "reuse"
    assert latest_extract["cache_state"] == "input_identical_reuse"
    assert latest_report["mode"] == "recompute"
    assert latest_report["cache_state"] == "stale_recomputed"
    assert latest_report["run_id"] != first_report["run_id"]
    assert latest_output_hash(latest_report, "report") != latest_output_hash(first_report, "report")

    updated_config = ProjectConfig.load(tmp_path)
    latest_eval = evaluate_target(updated_config, "report")
    assert latest_eval is not None
    assert latest_eval.failed == 0
    compare = compare_to_baseline(updated_config, "report")
    assert f"baseline_run: {first_report['run_id']}" in compare
    assert f"latest_run:   {latest_report['run_id']}" in compare
    assert "fingerprint:  changed" in compare
    assert "outputs:      changed report" in compare
    assert "latest:   3 passed, 0 failed" in compare

    publish_dir = publish_run(updated_config, latest=True)
    assert (publish_dir / "index.html").exists()
    assert (publish_dir / "review.json").exists()


def test_haiku_judge_verdict_shape(tmp_path):
    require_real_llm()
    model = os.environ.get("LMAKE_LLM_MODEL", "anthropic/claude-haiku-4-5-20251001")

    write(
        tmp_path / "lmakefile.yaml",
        f"""
version: 1

defaults:
  provider: litellm
  model: {model}
  params:
    temperature: 0
    max_tokens: 260
  cache:
    reuse_policy: input-identical
  system: |
    Return raw JSON only. Do not wrap the response in Markdown.

targets:
  judge-critique:
    inputs:
      - artifacts/critique.md
    prompt: prompts/judge.md
    outputs:
      verdict: artifacts/critique_verdict.json
""",
    )
    write(
        tmp_path / "artifacts" / "critique.md",
        """
# Critique

## Confidence

The report is grounded in source notes.

## Traceability Check

- Claims artifact length: 20 words.
- Synthesis artifact length: 40 words.
- Source documents inspected: 3.
""",
    )
    write(
        tmp_path / "prompts" / "judge.md",
        """
Score the input critique and return exactly one JSON object:
{
  "schema": "lmake.judge_verdict.v0",
  "target": "critique",
  "artifact": {"name": "critique", "path": "artifacts/critique.md"},
  "scores": {"traceability": 1, "source_accounting": 1, "readability": 1},
  "failures": [],
  "verdict": "pass",
  "rationale": "one short sentence"
}

Use integer scores from 1 to 5. Use verdict "pass" or "fail".
Return raw JSON only.
""",
    )
    write(
        tmp_path / "eval_cases" / "judge-critique.yaml",
        """
version: 1
target: judge-critique
cases:
  - name: judge emitted verdict schema
    output: verdict
    json_path: $.schema
    equals: lmake.judge_verdict.v0
  - name: judge emitted traceability score
    output: verdict
    json_path: $.scores.traceability
    exists: true
    type: number
  - name: judge emitted verdict string
    output: verdict
    json_path: $.verdict
    type: string
""",
    )

    assert main(["-C", str(tmp_path), "run", "judge-critique"]) == 0
    verdict = json.loads((tmp_path / "artifacts" / "critique_verdict.json").read_text(encoding="utf-8"))
    assert verdict["schema"] == "lmake.judge_verdict.v0"
    assert isinstance(verdict.get("scores"), dict)
    assert isinstance(verdict["scores"].get("traceability"), (int, float))
    assert verdict.get("verdict") in {"pass", "fail"}

    result = evaluate_target(ProjectConfig.load(tmp_path), "judge-critique")
    assert result is not None
    assert len(result.results) == 3

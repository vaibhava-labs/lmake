import os
import json

import pytest

from lmake.cli import main
from lmake.baseline import approve_latest, load_baseline
from lmake.compare import compare_to_baseline, github_artifact_diff
from lmake.config import ProjectConfig
from lmake.evals import evaluate_target
from lmake.engine import collect_outputs, default_outputs, fingerprint_bundle, make_base_manifest, status_all
from lmake.errors import ConfigError, TargetError
from lmake.gc import gc_project
from lmake.publish import publish_run
from lmake.state import ProjectState
from lmake.web import context_files, create_app, project_snapshot, safe_context_path, snapshot_signature


def write_lmakefile(tmp_path, text):
    (tmp_path / "lmakefile.yaml").write_text(text.lstrip(), encoding="utf-8")


def assert_bad_config(tmp_path, text, expected):
    write_lmakefile(tmp_path, text)
    with pytest.raises((ConfigError, TargetError)) as exc:
        ProjectConfig.load(tmp_path)
    assert expected in str(exc.value)


def test_smoke(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run", "report"]) == 0
    assert (tmp_path / "artifacts" / "claims.md").exists()
    assert (tmp_path / "artifacts" / "report.md").exists()
    assert main(["status"]) == 0
    assert main(["run", "report"]) == 0
    manifests = list((tmp_path / "runs").glob("*/manifest.json"))
    assert len(manifests) >= 4


def test_baseline_eval_compare_and_approve_flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (tmp_path / "eval_cases" / "report.yaml").exists()

    assert main(["run"]) == 0
    config = ProjectConfig.load(tmp_path)
    first_report = ProjectState(tmp_path).latest_manifest_for_target("report")
    assert first_report is not None

    eval_result = evaluate_target(config, "report")
    assert eval_result is not None
    assert eval_result.failed == 0
    assert main(["baseline", "set", "report", first_report["run_id"]]) == 0
    assert load_baseline(tmp_path, "report")["run_id"] == first_report["run_id"]

    (tmp_path / "context" / "brief.md").write_text(
        (tmp_path / "context" / "brief.md").read_text(encoding="utf-8") + "\nNew baseline comparison fact.\n",
        encoding="utf-8",
    )
    assert main(["approve", "report", "--skip-evals"]) == 2
    assert load_baseline(tmp_path, "report")["run_id"] == first_report["run_id"]

    assert main(["run"]) == 0
    changed_config = ProjectConfig.load(tmp_path)
    latest_report = ProjectState(tmp_path).latest_manifest_for_target("report")
    assert latest_report is not None
    assert latest_report["run_id"] != first_report["run_id"]

    compare_text = compare_to_baseline(changed_config, "report")
    assert "baseline_run: " + first_report["run_id"] in compare_text
    assert "latest_run:   " + latest_report["run_id"] in compare_text
    assert "fingerprint:  changed" in compare_text
    assert "## Evals" in compare_text

    assert main(["approve", "report"]) == 0
    approved = load_baseline(tmp_path, "report")
    assert approved["run_id"] == latest_report["run_id"]
    assert list((tmp_path / "baselines" / "approvals" / "report").glob("*.json"))


def test_eval_returns_failure_for_failed_case(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    (tmp_path / "eval_cases" / "report.yaml").write_text(
        """
version: 1
target: report
cases:
  - name: report must not mention its mock title
    output: report
    not_contains: lmake mock artifact
""".lstrip(),
        encoding="utf-8",
    )
    assert main(["run"]) == 0
    assert main(["eval", "report"]) == 1
    assert main(["approve", "report"]) == 2
    assert main(["approve", "report", "--skip-evals"]) == 0


def test_eval_rejects_unknown_text_check_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    (tmp_path / "eval_cases" / "report.yaml").write_text(
        """
version: 1
target: report
cases:
  - name: misspelled word count
    output: report
    min_wordz: 10
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run"]) == 0
    with pytest.raises(ConfigError, match="unknown field\\(s\\): min_wordz"):
        evaluate_target(ProjectConfig.load(tmp_path), "report")


def test_eval_rejects_empty_text_string_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    (tmp_path / "eval_cases" / "report.yaml").write_text(
        """
version: 1
target: report
cases:
  - name: empty regex
    output: report
    regex: []
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run"]) == 0
    with pytest.raises(ConfigError, match="empty regex.regex must include at least one string"):
        evaluate_target(ProjectConfig.load(tmp_path), "report")


def test_eval_rejects_invalid_text_numeric_check_at_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    (tmp_path / "eval_cases" / "report.yaml").write_text(
        """
version: 1
target: report
cases:
  - name: malformed word threshold
    output: report
    min_words: many
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="malformed word threshold.min_words must be an integer"):
        evaluate_target(ProjectConfig.load(tmp_path), "report")


def test_eval_rejects_text_case_without_checks_at_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    (tmp_path / "eval_cases" / "report.yaml").write_text(
        """
version: 1
target: report
cases:
  - name: no text checks
    output: report
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="eval case 'no text checks' does not define any checks"):
        evaluate_target(ProjectConfig.load(tmp_path), "report")


def write_metrics_project(tmp_path, metrics):
    (tmp_path / "context").mkdir()
    (tmp_path / "programs").mkdir()
    (tmp_path / "eval_cases").mkdir()
    (tmp_path / "context" / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    (tmp_path / "programs" / "metrics_program.py").write_text(
        """
import json


def run_program(inputs):
    source = next(part.text for part in inputs if part.path == "context/metrics.json")
    return {"metrics": json.loads(source)}
""".lstrip(),
        encoding="utf-8",
    )
    write_lmakefile(
        tmp_path,
        """
version: 1
defaults:
  provider: mock
  model: mock/deterministic
targets:
  metrics:
    runner: dspy
    program: programs/metrics_program.py
    inputs:
      - context/metrics.json
    dspy:
      action: run
      configure: false
    outputs:
      metrics: artifacts/metrics.json
""",
    )


def sample_metrics(*, failed=0, visible_outputs=8, p95=420, cost=12.5, malformed=0):
    return {
        "cases": {
            "akshat-singh": {
                "production": {
                    "failed": failed,
                    "passed": 12 - failed,
                    "malformed_responses": malformed,
                    "latency_ms": {"p95": p95},
                    "max_visible_gap_seconds": 1.5,
                }
            },
            "maya-rao": {
                "staging": {
                    "failed": 0,
                    "passed": 8,
                    "malformed_responses": 0,
                    "latency_ms": {"p95": 360},
                    "max_visible_gap_seconds": 0.8,
                }
            },
        },
        "metadata": {
            "audited": True,
            "release": "alpha",
            "retired_at": None,
        },
        "summary_text": f"{visible_outputs} visible outputs across two cases",
        "tags": ["traceable", "approved", "stable"],
        "visible_outputs": visible_outputs,
        "total_cost_cents": cost,
    }


def test_json_eval_checks_select_nested_metrics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: no malformed responses anywhere
    output: metrics
    json_path: $.cases.*.*.malformed_responses
    equals: 0
  - name: akshat production did not fail
    output: metrics
    path: $.cases.akshat-singh.production.failed
    equals: 0
  - name: p95 latency stays under threshold
    output: metrics
    json_path: $.cases.*.*.latency_ms.p95
    max: 500
  - name: enough visible outputs
    output: metrics
    json_path: $.visible_outputs
    min: 5
  - name: selected JSON text checks
    output: metrics
    json_path: $.cases.akshat-singh.production
    contains: latency_ms
    not_contains: catastrophic
  - name: summary exists and has expected shape
    output: metrics
    json_path: $.summary_text
    exists: true
    type: string
    regex: '^\\d+ visible outputs'
    length_min: 20
    length_max: 80
  - name: optional field is absent
    output: metrics
    json_path: $.metadata.deleted_at
    exists: false
  - name: tags are bounded strings
    output: metrics
    json_path: $.tags.*
    type: string
    regex: '^[a-z]+$'
    length_min: 5
    length_max: 10
  - name: tag array has expected size
    output: metrics
    json_path: $.tags
    type: array
    length_min: 2
    length_max: 5
  - name: metadata types are explicit
    output: metrics
    json_path: $.metadata.audited
    type: boolean
    equals: true
  - name: null metadata is explicit
    output: metrics
    json_path: $.metadata.retired_at
    type: null
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "metrics"]) == 0
    result = evaluate_target(ProjectConfig.load(tmp_path), "metrics")
    assert result is not None
    assert result.failed == 0
    assert main(["eval", "metrics"]) == 0

    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: p95 latency catches regression
    output: metrics
    json_path: $.cases.*.*.latency_ms.p95
    max: 400
""".lstrip(),
        encoding="utf-8",
    )
    failed = evaluate_target(ProjectConfig.load(tmp_path), "metrics")
    assert failed is not None
    assert failed.failed == 1
    assert "above maximum" in failed.results[0].reason
    assert "$.cases.akshat-singh.production.latency_ms.p95" in failed.results[0].reason


def test_json_eval_reports_structured_check_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: required field is present
    output: metrics
    json_path: $.metadata.owner
    exists: true
  - name: retired flag is absent
    output: metrics
    json_path: $.metadata.retired_at
    exists: false
  - name: audited is string
    output: metrics
    json_path: $.metadata.audited
    type: string
  - name: summary has release marker
    output: metrics
    json_path: $.summary_text
    regex: 'release-ready'
  - name: tags have at least four entries
    output: metrics
    json_path: $.tags
    length_min: 4
  - name: visible output has a length
    output: metrics
    json_path: $.visible_outputs
    length_min: 1
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "metrics"]) == 0
    failed = evaluate_target(ProjectConfig.load(tmp_path), "metrics")
    assert failed is not None
    assert failed.failed == 6
    reasons = [result.reason for result in failed.results]
    assert "matched no values" in reasons[0]
    assert "unexpectedly matched 1 value(s)" in reasons[1]
    assert "type 'boolean' != expected 'string'" in reasons[2]
    assert "regex did not match" in reasons[3]
    assert "length 3 is below minimum 4" in reasons[4]
    assert "has no length" in reasons[5]


def test_json_eval_rejects_non_boolean_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: malformed exists
    output: metrics
    json_path: $.metadata.owner
    exists: null
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "metrics"]) == 0
    with pytest.raises(ConfigError, match="exists must be a boolean"):
        evaluate_target(ProjectConfig.load(tmp_path), "metrics")


def test_json_eval_rejects_unknown_json_check_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: misspelled length check
    output: metrics
    json_path: $.tags
    lenght_min: 2
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "metrics"]) == 0
    with pytest.raises(ConfigError, match="unknown field\\(s\\): lenght_min"):
        evaluate_target(ProjectConfig.load(tmp_path), "metrics")


def test_json_eval_rejects_empty_string_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: empty selected regex
    output: metrics
    json_path: $.summary_text
    regex: []
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "metrics"]) == 0
    with pytest.raises(ConfigError, match="empty selected regex.regex must include at least one string"):
        evaluate_target(ProjectConfig.load(tmp_path), "metrics")


def test_json_eval_rejects_contradictory_absence_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: absent field cannot also have shape
    output: metrics
    json_path: $.metadata.deleted_at
    exists: false
    type: string
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "metrics"]) == 0
    with pytest.raises(ConfigError, match="exists: false with incompatible check\\(s\\): type"):
        evaluate_target(ProjectConfig.load(tmp_path), "metrics")


def test_json_eval_rejects_invalid_numeric_check_at_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: malformed length threshold
    output: metrics
    json_path: $.tags
    length_min: five
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="malformed length threshold.length_min must be an integer"):
        evaluate_target(ProjectConfig.load(tmp_path), "metrics")


def test_json_eval_rejects_null_numeric_check_at_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: null numeric max
    output: metrics
    json_path: $.visible_outputs
    max:
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="null numeric max.max must be a number"):
        evaluate_target(ProjectConfig.load(tmp_path), "metrics")


def test_json_eval_rejects_case_without_checks_at_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: no JSON checks
    output: metrics
    json_path: $.summary_text
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="eval case 'no JSON checks' does not define any JSON checks"):
        evaluate_target(ProjectConfig.load(tmp_path), "metrics")


def test_json_eval_allows_exists_true_as_only_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: summary exists
    output: metrics
    json_path: $.summary_text
    exists: true
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "metrics"]) == 0
    result = evaluate_target(ProjectConfig.load(tmp_path), "metrics")
    assert result is not None
    assert result.failed == 0
    assert result.results[0].reason == "1 selected JSON value(s), 1 check(s) passed"


def test_compare_reports_json_metric_deltas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())

    assert main(["run", "metrics"]) == 0
    assert main(["approve", "metrics"]) == 0
    baseline = ProjectState(tmp_path).latest_manifest_for_target("metrics")
    assert baseline is not None

    changed = sample_metrics(failed=2, visible_outputs=11, p95=735, cost=18.75, malformed=1)
    (tmp_path / "context" / "metrics.json").write_text(json.dumps(changed, indent=2, sort_keys=True), encoding="utf-8")
    assert main(["run", "metrics"]) == 0

    compare_text = compare_to_baseline(ProjectConfig.load(tmp_path), "metrics", with_evals=False)
    assert "## Metric Deltas" in compare_text
    assert "| metrics | `$.cases.akshat-singh.production.failed` | 0 | 2 | +2 |" in compare_text
    assert "| metrics | `$.cases.akshat-singh.production.malformed_responses` | 0 | 1 | +1 |" in compare_text
    assert "| metrics | `$.cases.akshat-singh.production.latency_ms.p95` | 420 | 735 | +315 |" in compare_text
    assert "| metrics | `$.visible_outputs` | 8 | 11 | +3 |" in compare_text
    assert "| metrics | `$.total_cost_cents` | 12.5 | 18.75 | +6.25 |" in compare_text


def test_compare_omits_metric_deltas_when_json_metrics_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())

    assert main(["run", "metrics"]) == 0
    assert main(["approve", "metrics"]) == 0
    assert main(["run", "metrics"]) == 0

    latest = ProjectState(tmp_path).latest_manifest_for_target("metrics")
    assert latest is not None
    assert latest["mode"] == "reuse"

    compare_text = compare_to_baseline(ProjectConfig.load(tmp_path), "metrics", with_evals=False)
    assert "outputs:      unchanged" in compare_text
    assert "## Metric Deltas" not in compare_text


def test_compare_github_format_adds_pr_comment_scaffold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_metrics_project(tmp_path, sample_metrics())
    (tmp_path / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: visible outputs present
    output: metrics
    json_path: $.visible_outputs
    exists: true
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "metrics"]) == 0
    assert main(["approve", "metrics"]) == 0

    changed = sample_metrics(failed=2, visible_outputs=11, p95=735, cost=18.75, malformed=1)
    (tmp_path / "context" / "metrics.json").write_text(json.dumps(changed, indent=2, sort_keys=True), encoding="utf-8")
    assert main(["run", "metrics"]) == 0

    config = ProjectConfig.load(tmp_path)
    text_default = compare_to_baseline(config, "metrics", with_evals=False)
    text_explicit = compare_to_baseline(config, "metrics", with_evals=False, fmt="text")
    github = compare_to_baseline(config, "metrics", fmt="github")

    assert text_default == text_explicit
    assert text_default.startswith("# lmake compare\n")
    assert "\n## Metric Deltas\n" in text_default
    assert "<details><summary>artifact diff</summary>" not in text_default

    assert github.startswith("<!-- lmake-compare: metrics -->\n### lmake compare: metrics\n")
    assert "⚠️ fingerprint changed · ⚠️ outputs changed: metrics · ✅ evals passed: 1 passed" in github
    assert "\n### Metric Deltas\n" in github
    assert "- ✅ visible outputs present:" in github
    assert "- PASS " not in github
    assert "<details><summary>artifact diff</summary>" in github
    assert "````diff\n# lmake diff\n" in github


def test_compare_github_artifact_diff_truncates_long_output():
    diff_text = "\n".join(f"line {index}" for index in range(305))

    rendered = github_artifact_diff(diff_text, limit=300)

    assert "line 299\n... truncated (5 more lines)" in rendered
    assert "line 300" not in rendered


def test_compare_exit_code_reports_changed_outputs_and_failed_evals(tmp_path, monkeypatch, capsys):
    output_project = tmp_path / "output-change"
    output_project.mkdir()
    monkeypatch.chdir(output_project)
    write_metrics_project(output_project, sample_metrics())

    assert main(["run", "metrics"]) == 0
    assert main(["approve", "metrics"]) == 0
    assert main(["compare", "metrics", "--exit-code"]) == 0
    capsys.readouterr()

    changed = sample_metrics(visible_outputs=10)
    (output_project / "context" / "metrics.json").write_text(json.dumps(changed, indent=2, sort_keys=True), encoding="utf-8")
    assert main(["run", "metrics"]) == 0
    assert main(["compare", "metrics", "--exit-code"]) == 1
    capsys.readouterr()

    eval_project = tmp_path / "eval-failure"
    eval_project.mkdir()
    monkeypatch.chdir(eval_project)
    write_metrics_project(eval_project, sample_metrics())

    assert main(["run", "metrics"]) == 0
    assert main(["approve", "metrics"]) == 0
    (eval_project / "eval_cases" / "metrics.yaml").write_text(
        """
version: 1
target: metrics
cases:
  - name: visible outputs stay tiny
    output: metrics
    json_path: $.visible_outputs
    max: 1
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["compare", "metrics", "--exit-code"]) == 1
    assert main(["compare", "metrics", "--no-evals", "--exit-code"]) == 0
    capsys.readouterr()


def write_judge_project(tmp_path):
    (tmp_path / "context").mkdir()
    (tmp_path / "programs").mkdir()
    (tmp_path / "eval_cases").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "context" / "source.md").write_text("traceable: yes\n", encoding="utf-8")
    (tmp_path / "prompts" / "critique.md").write_text("Generate the critique.\n", encoding="utf-8")
    (tmp_path / "prompts" / "judge.md").write_text("Score traceability, source accounting, and readability.\n", encoding="utf-8")
    (tmp_path / "programs" / "judge_demo.py").write_text(
        """
import re


def part(inputs, path):
    return next(item for item in inputs if item.path == path)


def score(features):
    return max(1, min(5, 1 + sum(1 for item in features if item)))


def run_critique(inputs):
    source = part(inputs, "context/source.md").text
    traceable = "traceable: yes" in source
    traceability = (
        "## Traceability Check\\n\\n"
        "- Claims artifact length: 42 words.\\n"
        "- Synthesis artifact length: 84 words.\\n"
        "- Source documents inspected: 4.\\n\\n"
        if traceable
        else ""
    )
    return {
        "critique": (
            "# Critique\\n\\n"
            "## Confidence\\n\\n"
            "The review cites interviews, support tickets, product metrics, and implementation notes.\\n\\n"
            "## Evidence Gaps\\n\\n"
            "- Needs an external customer proof.\\n\\n"
            "## Risks\\n\\n"
            "- The publish path still depends on external hosting.\\n\\n"
            "## Suggested Next Data To Collect\\n\\n"
            "- Ask a reviewer to inspect the report.\\n\\n"
            + traceability
        )
    }


def run_judge(inputs):
    critique = part(inputs, "artifacts/critique.md")
    text = critique.text
    source_count = re.search(r"Source documents inspected:\\s*(\\d+)", text)
    source_count_value = int(source_count.group(1)) if source_count else 0
    scores = {
        "traceability": score([
            "## Traceability Check" in text,
            "Claims artifact length" in text,
            "Synthesis artifact length" in text,
            "Source documents inspected" in text,
        ]),
        "source_accounting": score([
            source_count_value >= 3,
            "interviews" in text,
            "support tickets" in text,
            "product metrics" in text,
        ]),
        "readability": score([
            "## Confidence" in text,
            "## Evidence Gaps" in text,
            "## Risks" in text,
            "## Suggested Next Data To Collect" in text,
        ]),
    }
    failures = [name for name, value in scores.items() if value < 3]
    return {
        "verdict": {
            "schema": "lmake.judge_verdict.v0",
            "target": "critique",
            "artifact": {"name": "critique", "path": "artifacts/critique.md", "sha256": critique.sha256},
            "scores": scores,
            "failures": failures,
            "verdict": "pass" if not failures else "fail",
            "rationale": "traceability is present" if not failures else "missing traceability evidence",
        }
    }
""".lstrip(),
        encoding="utf-8",
    )
    write_lmakefile(
        tmp_path,
        """
version: 1
defaults:
  provider: mock
  model: mock/deterministic
targets:
  critique:
    runner: dspy
    program: programs/judge_demo.py
    inputs:
      - context/source.md
    prompt: prompts/critique.md
    dspy:
      action: run
      run: run_critique
      configure: false
    outputs:
      critique: artifacts/critique.md
  judge-critique:
    runner: dspy
    needs:
      - critique
    program: programs/judge_demo.py
    inputs:
      - artifacts/critique.md
    prompt: prompts/judge.md
    dspy:
      action: run
      run: run_judge
      configure: false
    outputs:
      verdict: artifacts/critique_verdict.json
""",
    )
    (tmp_path / "eval_cases" / "judge-critique.yaml").write_text(
        """
version: 1
target: judge-critique
cases:
  - name: verdict uses judge schema
    output: verdict
    json_path: $.schema
    equals: lmake.judge_verdict.v0
  - name: scores pass threshold
    output: verdict
    json_path: $.scores.*
    min: 3
  - name: verdict passes
    output: verdict
    json_path: $.verdict
    equals: pass
  - name: failures are empty
    output: verdict
    json_path: $.failures
    type: array
    length_max: 0
  - name: judged hash is recorded
    output: verdict
    json_path: $.artifact.sha256
    exists: true
    type: string
""".lstrip(),
        encoding="utf-8",
    )


def test_judge_target_verdict_loop_reports_score_delta(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_judge_project(tmp_path)

    assert main(["run", "judge-critique"]) == 0
    config = ProjectConfig.load(tmp_path)
    initial_eval = evaluate_target(config, "judge-critique")
    assert initial_eval is not None
    assert initial_eval.failed == 0
    assert main(["approve", "judge-critique"]) == 0

    state = ProjectState(tmp_path)
    judge_manifest = state.latest_manifest_for_target("judge-critique")
    assert judge_manifest is not None
    judged_input = next(item for item in judge_manifest["fingerprint_inputs"]["inputs"] if item["path"] == "artifacts/critique.md")
    verdict = json.loads((tmp_path / "artifacts" / "critique_verdict.json").read_text(encoding="utf-8"))
    assert verdict["artifact"]["sha256"] == judged_input["sha256"]

    (tmp_path / "context" / "source.md").write_text("traceable: no\n", encoding="utf-8")
    assert main(["run", "judge-critique"]) == 0
    failed_eval = evaluate_target(ProjectConfig.load(tmp_path), "judge-critique")
    assert failed_eval is not None
    assert failed_eval.failed == 3
    reasons = [result.reason for result in failed_eval.results]
    assert "$.scores.traceability value 1 is below minimum 3" in reasons

    compare_text = compare_to_baseline(ProjectConfig.load(tmp_path), "judge-critique")
    assert "| verdict | `$.scores.traceability` | 5 | 1 | -4 |" in compare_text


def test_approval_records_do_not_overwrite_with_same_timestamp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run"]) == 0
    monkeypatch.setattr("lmake.baseline.utc_now", lambda: "2026-06-07T00:00:00Z")

    config = ProjectConfig.load(tmp_path)
    approve_latest(config, "report")
    approve_latest(config, "report")

    approval_files = sorted((tmp_path / "baselines" / "approvals" / "report").glob("*.json"))
    assert len(approval_files) == 2
    assert approval_files[0].name != approval_files[1].name


def test_config_validation_rejects_common_yaml_mistakes(tmp_path):
    assert_bad_config(
        tmp_path,
        """
version: 1
target:
  report: {}
""",
        "unknown field 'target'",
    )
    assert_bad_config(
        tmp_path,
        """
version: 1
targets:
  report:
    promt: prompts/report.md
""",
        "targets.report has unknown field 'promt'",
    )
    assert_bad_config(
        tmp_path,
        """
version: 1
targets:
  ../bad:
    prompt_text: hello
""",
        "target name '../bad' must use only",
    )
    assert_bad_config(
        tmp_path,
        """
version: 1
groups:
  bad/group:
    targets: [report]
targets:
  report:
    prompt_text: hello
""",
        "group name 'bad/group' must use only",
    )
    assert_bad_config(
        tmp_path,
        """
version: 1
targets:
  report:
    runner: agent
""",
        "targets.report.runner must be one of",
    )
    assert_bad_config(
        tmp_path,
        """
version: 1
targets:
  report:
    prompt: prompts/report.md
    prompt_text: inline prompt
""",
        "must not specify both prompt and prompt_text",
    )
    assert_bad_config(
        tmp_path,
        """
version: 1
targets:
  report:
    outputs:
      report: ../report.md
""",
        "must not escape the project root",
    )
    assert_bad_config(
        tmp_path,
        """
version: 1
defaults:
  params: []
targets:
  report: {}
""",
        "defaults.params must be a mapping",
    )
    assert_bad_config(
        tmp_path,
        """
version: 1
groups:
  update:
    targets:
      - missing
targets:
  report: {}
""",
        "references unknown target 'missing'",
    )


def test_config_validation_inherits_default_system_prompt(tmp_path):
    write_lmakefile(
        tmp_path,
        """
version: 1
defaults:
  provider: mock
  model: mock/deterministic
  system: Follow the style guide.
targets:
  report:
    prompt_text: ""
    outputs:
      report: artifacts/report.md
""",
    )

    config = ProjectConfig.load(tmp_path)
    assert config.target("report").system == "Follow the style guide."


def test_model_lock_resolves_alias_and_enters_fingerprint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    write_lmakefile(
        tmp_path,
        """
version: 1
defaults:
  provider: mock
  model: mock-latest
targets:
  report:
    prompt_text: hello
    outputs:
      report: artifacts/report.md
""",
    )

    assert_bad_config(tmp_path, (tmp_path / "lmakefile.yaml").read_text(encoding="utf-8"), "floating model alias 'mock-latest'")
    assert main(["lock", "set", "mock-latest", "mock/pinned-v1", "--provider", "mock"]) == 0

    config = ProjectConfig.load(tmp_path)
    target = config.target("report")
    assert target.model_alias == "mock-latest"
    assert target.model == "mock/pinned-v1"

    state = ProjectState(tmp_path)
    bundle = fingerprint_bundle(config, state, target)
    execution = bundle.fingerprint_inputs["execution"]
    assert execution["model_alias"] == "mock-latest"
    assert execution["model"] == "mock/pinned-v1"
    assert execution["model_lock"]["resolved"] == "mock/pinned-v1"
    assert bundle.fingerprint_inputs["lmake_lock"]["path"] == "lmake.lock"

    assert main(["lock", "set", "mock-latest", "mock/pinned-v2", "--provider", "mock"]) == 0
    repinned = ProjectConfig.load(tmp_path)
    repinned_bundle = fingerprint_bundle(repinned, ProjectState(tmp_path), repinned.target("report"))
    assert repinned.target("report").model == "mock/pinned-v2"
    assert repinned_bundle.target_fingerprint != bundle.target_fingerprint


def test_model_lock_rejects_provider_mismatch(tmp_path):
    write_lmakefile(
        tmp_path,
        """
version: 1
defaults:
  provider: litellm
  model: claude-sonnet-latest
targets:
  report:
    prompt_text: hello
""",
    )
    assert main(["-C", str(tmp_path), "lock", "set", "claude-sonnet-latest", "anthropic/claude-sonnet-4-20250514", "--provider", "mock"]) == 0

    with pytest.raises(ConfigError) as exc:
        ProjectConfig.load(tmp_path)
    assert "provider is 'mock'" in str(exc.value)


def test_model_lock_rejects_malformed_models_mapping(tmp_path):
    write_lmakefile(
        tmp_path,
        """
version: 1
targets:
  report:
    prompt_text: hello
""",
    )
    (tmp_path / "lmake.lock").write_text(
        """
version: 1
models: []
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc:
        ProjectConfig.load(tmp_path)
    assert "lmake.lock.models must be a mapping" in str(exc.value)


def test_dspy_runner_compile_artifact_feeds_downstream_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    (tmp_path / "programs" / "report_program.py").write_text(
        """
def build_program():
    return {"signature": "context -> report"}


def compile_program(program, inputs, target_fingerprint):
    return {
        "program": program,
        "input_paths": [part.path for part in inputs],
        "fingerprint": target_fingerprint,
    }


def run_program(inputs):
    compiled = next(part.text for part in inputs if part.path == "artifacts/report_program.json")
    context = next(part.text for part in inputs if part.path == "context/brief.md")
    return {
        "report": "# Report from compiled program\\n\\n"
        + compiled
        + "\\n## Context\\n"
        + context
    }
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "lmakefile.yaml").write_text(
        """
version: 1

defaults:
  provider: mock
  model: mock/deterministic
  cache:
    reuse_policy: input-identical

targets:
  compile-report:
    runner: dspy
    program: programs/report_program.py
    inputs:
      - context/*.md
    outputs:
      program: artifacts/report_program.json

  report:
    runner: dspy
    needs:
      - compile-report
    program: programs/report_program.py
    inputs:
      - context/*.md
      - artifacts/report_program.json
    outputs:
      report: artifacts/report.md
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["run", "report"]) == 0
    compiled = tmp_path / "artifacts" / "report_program.json"
    report = tmp_path / "artifacts" / "report.md"
    assert compiled.exists()
    assert report.exists()
    assert "context/brief.md" in compiled.read_text(encoding="utf-8")
    assert "# Report from compiled program" in report.read_text(encoding="utf-8")

    assert main(["run", "report"]) == 0
    manifests = list((tmp_path / "runs").glob("*/manifest.json"))
    assert len(manifests) == 4


def write_staged_manifest(tmp_path, target_name, staged_contents):
    config = ProjectConfig.load(tmp_path)
    state = ProjectState(tmp_path)
    state.ensure_dirs()
    target = config.target(target_name)
    bundle = fingerprint_bundle(config, state, target)
    run_id = state.make_run_id(target.name, bundle.target_fingerprint, "run")
    staging_dir = state.runs_dir / run_id / "staging"
    staging_dir.mkdir(parents=True)
    for logical_name, content in staged_contents.items():
        (staging_dir / logical_name).write_text(content, encoding="utf-8")

    output_map = default_outputs(target)
    outputs = collect_outputs(
        state,
        {name: (output_map[name], staging_dir / name) for name in staged_contents},
    )
    manifest = make_base_manifest(
        config=config,
        state=state,
        target=target,
        bundle=bundle,
        run_id=run_id,
        mode="recompute",
        cache_state="fresh_recomputed",
    )
    manifest["outputs"] = outputs
    manifest["provider_result"] = {"test": "crash-window"}
    state.write_manifest(manifest, update_latest=False)
    return manifest, staging_dir


def test_status_discards_staging_without_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    staging_dir = tmp_path / "runs" / "orphan-run" / "staging"
    staging_dir.mkdir(parents=True)
    (staging_dir / "claims").write_text("unfinished", encoding="utf-8")

    assert main(["status"]) == 0
    assert not staging_dir.exists()
    assert not (tmp_path / "runs" / "orphan-run").exists()
    assert not (tmp_path / "artifacts" / "claims.md").exists()


def test_status_recovers_manifest_written_before_promotion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    manifest, staging_dir = write_staged_manifest(
        tmp_path,
        "extract",
        {"claims": "# Recovered claims\n"},
    )

    state = ProjectState(tmp_path)
    assert state.latest_manifest_for_target("extract") is None
    assert main(["status"]) == 0

    artifact = tmp_path / "artifacts" / "claims.md"
    assert artifact.read_text(encoding="utf-8") == "# Recovered claims\n"
    assert not staging_dir.exists()
    assert ProjectState(tmp_path).latest_manifest_for_target("extract")["run_id"] == manifest["run_id"]


def test_status_skips_live_staging_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    manifest, staging_dir = write_staged_manifest(
        tmp_path,
        "extract",
        {"claims": "# Not ready yet\n"},
    )
    state = ProjectState(tmp_path)
    state.write_active_marker(staging_dir)

    assert main(["status"]) == 0
    assert staging_dir.exists()
    assert not (tmp_path / "artifacts" / "claims.md").exists()
    assert ProjectState(tmp_path).latest_manifest_for_target("extract") is None

    ProjectState(tmp_path).clear_active_marker(staging_dir)
    assert main(["status"]) == 0
    assert not staging_dir.exists()
    assert (tmp_path / "artifacts" / "claims.md").read_text(encoding="utf-8") == "# Not ready yet\n"
    assert ProjectState(tmp_path).latest_manifest_for_target("extract")["run_id"] == manifest["run_id"]


def test_recovery_repairs_partial_promotion_from_cas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    (tmp_path / "lmakefile.yaml").write_text(
        """
version: 1

defaults:
  provider: mock
  model: mock/deterministic

targets:
  multi:
    inputs:
      - context/*.md
    prompt_text: write both files
    outputs:
      one: artifacts/one.txt
      two: artifacts/two.txt
""".lstrip(),
        encoding="utf-8",
    )
    manifest, staging_dir = write_staged_manifest(
        tmp_path,
        "multi",
        {"one": "correct one\n", "two": "correct two\n"},
    )

    one_artifact = tmp_path / "artifacts" / "one.txt"
    one_artifact.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging_dir / "one", one_artifact)
    one_artifact.write_text("corrupt one\n", encoding="utf-8")
    (staging_dir / "two").unlink()

    assert main(["status"]) == 0
    assert one_artifact.read_text(encoding="utf-8") == "correct one\n"
    assert (tmp_path / "artifacts" / "two.txt").read_text(encoding="utf-8") == "correct two\n"
    assert not staging_dir.exists()
    assert ProjectState(tmp_path).latest_manifest_for_target("multi")["run_id"] == manifest["run_id"]


def test_status_detects_tampered_artifact_bytes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run", "extract"]) == 0
    (tmp_path / "artifacts" / "claims.md").write_text("tampered\n", encoding="utf-8")

    statuses = {item.target: item for item in status_all(ProjectConfig.load(tmp_path))}
    assert statuses["extract"].status == "outputs-changed"


def test_default_group_runs_without_target_argument(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run"]) == 0
    assert (tmp_path / "artifacts" / "claims.md").exists()
    assert (tmp_path / "artifacts" / "report.md").exists()
    config = ProjectConfig.load(tmp_path)
    assert config.default_group == "update"
    assert config.run_order(None) == ["extract", "report"]
    assert config.run_order("all") == ["extract", "report"]


def test_publish_writes_static_bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run"]) == 0
    assert main(["approve", "report"]) == 0
    config = ProjectConfig.load(tmp_path)
    output_dir = publish_run(config, latest=True)

    assert (output_dir / "index.html").exists()
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "review.json").exists()
    html = (output_dir / "index.html").read_text(encoding="utf-8")
    assert "Published lmake Run" in html
    assert "Run provenance" in html
    assert "Review provenance" in html
    assert "Approved baseline" in html
    assert "4 passed, 0 failed" in html
    review = json.loads((output_dir / "review.json").read_text(encoding="utf-8"))
    assert review["baseline"]["same_run"] is True
    assert review["evals"]["failed"] == 0
    assert list((output_dir / "outputs").glob("*"))

    no_review_dir = publish_run(config, latest=True, output_dir=tmp_path / "public-no-review", include_review=False)
    assert not (no_review_dir / "review.json").exists()
    assert "Review provenance" not in (no_review_dir / "index.html").read_text(encoding="utf-8")


def test_web_context_helpers_are_scoped_to_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    files = context_files(tmp_path)
    assert [item["path"] for item in files] == ["context/brief.md"]
    assert safe_context_path(tmp_path, "context/brief.md") == (tmp_path / "context" / "brief.md").resolve()
    try:
        safe_context_path(tmp_path, "lmakefile.yaml")
    except ValueError as exc:
        assert "context" in str(exc)
    else:
        raise AssertionError("expected path outside context/ to be rejected")


def test_web_update_outputs_accepts_json_body(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    app = create_app(tmp_path)
    save_endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/context/{relpath:path}" and "PUT" in getattr(route, "methods", set())
    )
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/run")

    brief = tmp_path / "context" / "brief.md"
    save_data = save_endpoint(
        "brief.md",
        {
            "content": brief.read_text(encoding="utf-8") + "\nExtra web note.\n",
            "base_sha256": None,
        },
    )
    assert save_data["path"] == "context/brief.md"

    data = endpoint({"selection": None})
    assert [item["target"] for item in data["results"]] == ["extract", "report"]
    assert data["state"]["statuses"][0]["status"] == "fresh"
    assert (tmp_path / "artifacts" / "claims.md").exists()
    assert (tmp_path / "artifacts" / "report.md").exists()


def test_web_review_endpoints_eval_approve_and_compare(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run"]) == 0
    app = create_app(tmp_path)
    endpoints = {getattr(route, "path", ""): route.endpoint for route in app.routes}

    eval_data = endpoints["/api/eval/{target_name}"]("report")
    assert eval_data["found"] is True
    assert eval_data["failed"] == 0

    approve_data = endpoints["/api/approve/{target_name}"]("report", {"skip_evals": False})
    assert approve_data["record"]["target"] == "report"
    assert approve_data["eval_summary"]["failed"] == 0
    assert approve_data["state"]["baselines"][0]["target"] == "report"

    compare_data = endpoints["/api/compare/{target_name}"]("report")
    assert compare_data["target"] == "report"
    assert "# lmake compare" in compare_data["text"]
    assert "fingerprint:  unchanged" in compare_data["text"]


def test_web_state_signature_changes_for_live_staleness(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run"]) == 0
    app = create_app(tmp_path)

    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/events" in route_paths
    before = project_snapshot(tmp_path)
    before_signature = snapshot_signature(before)

    brief = tmp_path / "context" / "brief.md"
    brief.write_text(brief.read_text(encoding="utf-8") + "\nLive staleness fact.\n", encoding="utf-8")
    after = project_snapshot(tmp_path)

    assert snapshot_signature(after) != before_signature
    assert {item["target"]: item["status"] for item in after["statuses"]}["report"] == "stale"


def test_gc_prunes_old_runs_but_keeps_reused_output_objects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run"]) == 0
    assert main(["run"]) == 0
    config = ProjectConfig.load(tmp_path)
    state = ProjectState(tmp_path)
    manifests_before = state.all_manifests()
    objects_before = {item["sha256"] for manifest in manifests_before for item in manifest.get("outputs", [])}
    assert len(manifests_before) == 4
    assert objects_before

    dry = gc_project(config, keep_per_target=1, dry_run=True)
    assert len(dry.pruned_runs) == 2
    assert len(state.all_manifests()) == 4

    applied = gc_project(config, keep_per_target=1, dry_run=False)
    assert len(applied.pruned_runs) == 2
    manifests_after = state.all_manifests()
    assert len(manifests_after) == 2
    for manifest in manifests_after:
        for item in manifest.get("outputs", []):
            assert state.object_path(item["sha256"]).exists()
    assert (tmp_path / "artifacts" / "claims.md").exists()
    assert (tmp_path / "artifacts" / "report.md").exists()


def test_gc_keeps_baseline_run_even_outside_keep_window(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["run"]) == 0
    first_extract = ProjectState(tmp_path).latest_manifest_for_target("extract")
    assert first_extract is not None
    assert main(["baseline", "set", "extract", first_extract["run_id"]]) == 0

    (tmp_path / "context" / "brief.md").write_text(
        (tmp_path / "context" / "brief.md").read_text(encoding="utf-8") + "\nGC baseline keep fact.\n",
        encoding="utf-8",
    )
    assert main(["run"]) == 0

    config = ProjectConfig.load(tmp_path)
    applied = gc_project(config, keep_per_target=0, dry_run=False)
    assert first_extract["run_id"] not in applied.pruned_runs
    assert (tmp_path / "runs" / first_extract["run_id"] / "manifest.json").exists()
    for item in first_extract.get("outputs", []):
        assert ProjectState(tmp_path).object_path(item["sha256"]).exists()

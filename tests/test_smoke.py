import os
import json

import pytest

from lmake.cli import main
from lmake.baseline import approve_latest, load_baseline
from lmake.compare import compare_to_baseline
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

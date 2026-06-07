# lmake v0 spec sketch

## 1. Principle

`lmake` is a git-native, local-first workflow runner for LLM computations.

The durable unit is not a chat thread. It is a run:

```text
source tree + prompt/program graph + model/tool settings + dependency artifacts -> immutable manifest + promoted outputs
```

## 2. Canonical state

Canonical state is the project directory plus a content-addressed object store.

```text
git tree + .lmcache/objects/sha256/*
```

The SaaS layer, if any, should be a view over this state.

## 3. lmakefile.yaml

Minimum target fields:

```yaml
targets:
  name:
    needs: [other-target]
    inputs: [context/*.md]
    prompt: prompts/foo.md
    prompt_text: optional inline prompt
    runner: provider | dspy
    program: optional code-backed step
    programs: optional list/glob of code dependencies
    dspy: optional DSPy runner configuration
    outputs:
      logical_name: artifacts/foo.md
    provider: mock | litellm
    model: provider/model-name
    params: {}
    cache:
      reuse_policy: input-identical
      ttl_seconds: optional seconds
```

Run groups:

```yaml
default_group: update

groups:
  update:
    targets: [report]
```

`default_group` is used by `lmake run` with no explicit target and by the web UI's Update button. The virtual group `all` means all terminal targets.

The config loader rejects unknown fields, path-like target/group names, unsupported runner/provider values, malformed maps/lists, prompt/prompt_text conflicts, project-escaping paths, and group/dependency references to unknown targets.

## 4. lmake.lock

`lmake.lock` pins model aliases to concrete resolved model IDs:

```yaml
version: 1
models:
  claude-sonnet-latest:
    provider: litellm
    resolved: anthropic/claude-sonnet-4-20250514
    pinned_at: "2026-06-07T00:00:00Z"
```

When `lmakefile.yaml` names a model that appears in `lmake.lock`, the resolved model is used for execution. The original alias and lock record are retained in fingerprint inputs. Any model containing `latest` must be pinned.

## 5. Fingerprint

A target fingerprint is a SHA-256 hash of canonical JSON containing:

```json
{
  "schema": "lmake.fingerprint.v0",
  "tool": {"name": "lmake", "version": "..."},
  "target": "report",
  "target_spec_hash": "...",
  "lmakefile": {"path": "lmakefile.yaml", "sha256": "...", "bytes": 123},
  "lmake_lock": {"path": "lmake.lock", "sha256": "...", "bytes": 123},
  "prompt": {"kind": "file", "path": "prompts/report.md", "sha256": "..."},
  "programs": [],
  "inputs": [{"path": "context/brief.md", "sha256": "...", "bytes": 123}],
  "dependency_tree_hash": "...",
  "upstream": [{"target": "extract", "target_fingerprint": "...", "outputs": []}],
  "execution": {"runner": "provider", "provider": "litellm", "model": "...", "model_alias": "...", "model_lock": {}, "params": {}, "system_sha256": "..."}
}
```

Important: upstream run IDs are observed in manifests but not part of downstream fingerprints. Downstream targets hash upstream artifact content and upstream target fingerprints, not incidental run IDs.

## 6. Manifest

Each run creates `runs/<run_id>/manifest.json`.

Core fields:

```json
{
  "schema": "lmake.run.v0",
  "run_id": "20260607T102133Z_report_run_deadbeefcafe",
  "target": "report",
  "created_at": "2026-06-07T10:21:33Z",
  "mode": "recompute|reuse|replay",
  "cache_state": "fresh_recomputed|input_identical_reuse|stale_recomputed|policy_expired_recomputed|replay_valid",
  "reused_from_run_id": null,
  "target_fingerprint": "...",
  "fingerprint_inputs": {},
  "observed_upstream_runs": [],
  "source_tree": {"git": {"inside_work_tree": true, "commit": "...", "dirty": true}},
  "policy": {"reuse_policy": "input-identical", "ttl_seconds": null},
  "outputs": [{"name": "report", "path": "artifacts/report.md", "sha256": "...", "object": "sha256:..."}],
  "provider_result": {}
}
```

## 7. Cache states

`lmake` uses artifact reuse, not deterministic recomputation.

- `replay_valid`: exact historical bytes restored from object store.
- `input_identical_reuse`: exact historical bytes reused because declared dependencies are identical.
- `stale_recomputed`: declared dependency changed and target was recomputed.
- `policy_expired_recomputed`: freshness policy forced recomputation.
- `forced_recomputed`: user requested recomputation with `--force`.

## 8. Evals, baselines, and approval

Eval suites live at `eval_cases/<target>.yaml`:

```yaml
version: 1
target: report
cases:
  - name: report has an executive summary
    output: report
    required_headings: [Executive Summary]
  - name: report stays compact
    output: report
    max_words: 500
```

Eval cases are deterministic checks against artifact bytes restored from the content-addressed object store. The initial check set is:

- `contains`
- `not_contains`
- `regex`
- `required_headings`
- `min_words` / `max_words`
- `min_bytes` / `max_bytes`

Approved baselines live at:

```text
baselines/<target>.json
baselines/approvals/<target>/<timestamp>_<run_id>.json
```

`baselines/<target>.json` records the approved run ID, target fingerprint, output SHA-256 values, provider/model, prior baseline run, and setter identity from Git config when available. Approval records are append-only audit entries for each approval event.

`lmake approve <target>` requires the latest run for the target to be fresh. If an eval suite exists, all eval cases must pass unless the user explicitly passes `--skip-evals`. `lmake compare <target>` compares the latest run against the baseline pointer, including fingerprint/output changes, eval results, and artifact diffs.

Garbage collection must keep baseline run manifests and every content-addressed output object referenced by kept baseline manifests.

## 9. DSPy runner

DSPy targets use `runner: dspy`. They are still ordinary targets: their source programs, prompts, inputs, model settings, and outputs are fingerprinted and recorded in immutable manifests.

A compile target writes a compiled program/state artifact:

```yaml
targets:
  compile-report:
    runner: dspy
    program: programs/report_program.py
    dspy:
      action: compile
    inputs: [context/*.md]
    outputs:
      program: artifacts/report_program.json
```

A downstream target consumes that compiled artifact by declaring it as an input:

```yaml
targets:
  report:
    runner: dspy
    needs: [compile-report]
    program: programs/report_program.py
    dspy:
      action: run
      run: run_report
      compiled: artifacts/report_program.json
    inputs:
      - context/*.md
      - artifacts/report_program.json
    outputs:
      report: artifacts/report.md
```

Program modules may provide `build_program()`, `compile_program(...)`, and `run_program(...)` hooks. `dspy.compile` and `dspy.run` override the default hook names for compile and run targets. If a compile hook is omitted, the runner can use `dspy.optimizer`, a `trainset` hook/value, and an optional `metric` hook to call `optimizer.compile(program, trainset=...)`.

## 10. Web and publish layers

`lmake serve` is a local web view over the same project directory. It does not introduce a new database or canonical state. Non-technical collaborators edit `context/` files, see human-readable staleness, run the default group, read artifacts, and publish a static bundle. Details mode may expose developer review operations such as eval, baseline compare, and approve without changing the collaborator default view.

The web server may expose a local WebSocket state stream at `/api/events`. The stream sends project snapshots when source files, artifacts, statuses, baselines, or run history change, so browser clients can update staleness without reloading.

`lmake publish` emits a static HTML bundle for a run:

```text
published/<run_id>/
  index.html
  manifest.json
  review.json        # optional, when baseline/eval provenance exists
  outputs/*
```

The bundle is derived from the manifest and content-addressed objects. When `review.json` is present, it uses schema `lmake.publish_review.v0` and records the published run's relationship to the current approved baseline plus deterministic eval results for the published run. `lmake publish --latest` resolves through the configured default group/output target before falling back to the newest manifest.

The repository's demo can be published by CI to GitHub Pages by running the default group, evaluating, approving, and publishing the terminal artifact bundle.

## 11. Future spec items

- signed manifests
- remote cache protocol
- semantic artifact diff protocol
- task-level tool traces
- LLM-judge eval attachments
- richer DSPy compile records
- MCP/tool dependency snapshots
- merge strategy for run records

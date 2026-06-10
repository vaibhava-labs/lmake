# lmake

`lmake` makes LLM-generated artifacts reviewable: run them, eval them, compare them to a baseline, approve them, and publish them with provenance.

It is deliberately small and local-first. The repo format is the product primitive:

- `context/` holds source material.
- `prompts/` and `programs/` hold generation logic.
- `runs/` records immutable manifests.
- `artifacts/` holds promoted outputs.
- `eval_cases/` defines deterministic quality gates.
- `baselines/` records what the team has approved.

Under the hood, `lmake` is a tiny build tool for LLM workflows. The design target is still:

```text
lmake : GitHub :: git : GitHub
```

The CLI and file format come first. Collaboration, dashboards, hosted artifact browsing, semantic diffs, and fleet execution can sit on top.

## Project layout

```text
project/
  lmakefile.yaml          # workflow DAG definition
  context/                # source files / input context
  prompts/                # versioned prompt files
  programs/               # DSPy modules or other code-backed steps
  eval_cases/             # deterministic checks for approved outputs
  baselines/              # approved run pointers + approval records
  .lmcache/               # content-addressed artifact store + index
  runs/                   # immutable run records + manifests
  artifacts/              # promoted outputs
```

`.lmcache/` is local cache. `runs/`, `artifacts/`, `eval_cases/`, and `baselines/` are intentionally plain files so teams can decide whether to commit, branch, diff, review, approve, or publish them.

## Install

The CLI command is `lmake`. The Python distribution name is `lmake-ai` because `lmake` is already taken on PyPI. This repo is still private alpha, so install from Git for now.

For collaborators with GitHub SSH access:

```bash
pipx install "git+ssh://git@github.com/vaibhava-labs/lmake.git"
```

Or with `pip`:

```bash
python -m pip install "git+ssh://git@github.com/vaibhava-labs/lmake.git"
```

For the web UI from Git:

```bash
python -m pip install "lmake-ai[web] @ git+ssh://git@github.com/vaibhava-labs/lmake.git"
```

For local development from this checkout:

```bash
python -m pip install -e '.[web,dev]'
```

For the minimal editable CLI install:

```bash
python -m pip install -e .
```

The default provider is `mock`, which requires no API keys and produces deterministic local artifacts. For real model calls:

```bash
python -m pip install -e '.[litellm]'
```

Then set `provider: litellm` and use a LiteLLM model string such as `openai/gpt-4o-mini`, `anthropic/claude-3-5-sonnet-20241022`, or an Ollama-backed model.

For Anthropic testing, keep the key out of Git and pass it through the process environment:

```bash
read -rsp "Anthropic API key: " ANTHROPIC_API_KEY; export ANTHROPIC_API_KEY; echo
```

Use Haiku for low-cost smoke tests:

```yaml
defaults:
  provider: litellm
  model: anthropic/claude-haiku-4-5-20251001
```

For DSPy program targets:

```bash
python -m pip install -e '.[dspy]'
```

For the local web UI:

```bash
python -m pip install -e '.[web]'
```

## Quick start

```bash
mkdir demo && cd demo
lmake init
lmake status
lmake run
lmake log
lmake eval report
lmake approve report
lmake status
```

A second run should reuse identical prior outputs under policy:

```bash
lmake run report
```

Change `context/brief.md` or a prompt, then:

```bash
lmake status
lmake run report
```

Diff two runs:

```bash
lmake log --limit 5
lmake diff <run_id_1> <run_id_2>
```

Replay an old run's artifacts into the working tree:

```bash
lmake replay <run_id>
```

Compare the latest run for a target against its approved baseline:

```bash
lmake compare report
```

## Realistic demo

The repo includes `demo_project/`, a local research-workflow demo that works without API keys:

```bash
cd demo_project
lmake status
lmake run
lmake publish --latest
lmake serve
```

It uses four source documents in `context/` and produces:

- `artifacts/claims.md`
- `artifacts/synthesis.md`
- `artifacts/critique.md`

The target graph is `extract -> synthesize -> critique`, with `default_group: update` so both the CLI and web UI can use one Update action.
The demo also includes eval cases for `critique`, so you can run `lmake eval critique`, `lmake approve critique`, edit context, run again, and use `lmake compare critique`.

For a tighter regression story, run `bash demo_project/scripts/killer_demo.sh` or follow [docs/killer-demo.md](docs/killer-demo.md). It shows a prompt edit dropping traceability, `lmake eval` catching it, `lmake compare` explaining the delta against the approved baseline, and `lmake publish` producing the reviewed bundle.

See [docs/recipes.md](docs/recipes.md) for the first review-loop recipes, including an opt-in Haiku integration proof.
See [docs/behavioral-regression.md](docs/behavioral-regression.md) for the emerging case/suite/label pattern for AI behavior regression workflows.

## Published demo

The demo workflow builds a static report bundle on every push to `main` and uploads it as a workflow artifact. When the repository is public or otherwise Pages-capable, the same workflow deploys it to GitHub Pages:

```text
https://vaibhava-labs.github.io/lmake/
```

That static bundle is generated from `demo_project`, includes rendered artifacts, `manifest.json`, and `review.json` with baseline/eval provenance.

## lmakefile.yaml v0

```yaml
version: 1

defaults:
  provider: mock
  model: mock/deterministic
  params:
    temperature: 0
  cache:
    reuse_policy: input-identical
    # ttl_seconds: 3600     # optional freshness policy

default_group: update

groups:
  update:
    targets:
      - report

targets:
  extract:
    inputs:
      - context/*.md
    prompt: prompts/extract.md
    outputs:
      claims: artifacts/claims.md

  report:
    needs:
      - extract
    inputs:
      - context/*.md
      - artifacts/claims.md
    prompt: prompts/report.md
    outputs:
      report: artifacts/report.md
```

Each target's fingerprint includes:

- target spec hash
- `lmakefile.yaml` hash
- prompt hash
- declared input file paths, sizes, and SHA-256 hashes
- optional program file hashes
- upstream target fingerprints and output hashes
- runner, provider, model, params, and system prompt hash
- tool version

## Groups and the Update button

`groups` define named run selections. `default_group` is what `lmake run` uses when no target is passed, and what the web UI uses for its single Update button.

```yaml
default_group: update

groups:
  update:
    targets: [report]
```

The virtual group `all` always means all terminal targets in dependency order.

## Config validation

`ProjectConfig.load()` validates `lmakefile.yaml` before the engine runs. Common mistakes produce direct `ConfigError`/`TargetError` messages instead of tracebacks:

- unknown top-level, default, target, or group fields
- target and group names with path-like or whitespace characters
- unsupported `runner` or `provider` values
- non-mapping `params`, `cache`, `dspy`, `defaults`, or `groups`
- targets that specify both `prompt` and `prompt_text`
- absolute paths, `../` paths, or outputs under `.lmcache/` or `runs/`
- groups or dependencies that reference unknown targets

## Cache/replay semantics

`lmake` deliberately does **not** pretend LLM inference is deterministic.

It distinguishes:

| State | Meaning |
|---|---|
| `replay_valid` | A previous artifact is restored exactly as observed. No claim is made that the model would regenerate it. |
| `input_identical_reuse` | Inputs, prompt graph, workflow spec, model settings, and dependency artifacts are identical, so prior bytes are reused under policy. |
| `stale_recomputed` | A declared dependency changed, so the target was recomputed. |
| `policy_expired_recomputed` | Nothing structurally changed, but TTL/freshness policy forced recomputation. |

This is the central contract: a prior run is a recorded artifact, not just a cache entry.

## Evals, baselines, and approval

`eval_cases/<target>.yaml` defines deterministic checks against a target's artifact bytes in `.lmcache`, not whatever happens to be in the working tree.

```yaml
version: 1
target: report
cases:
  - name: report has an executive summary
    output: report
    required_headings:
      - Executive Summary
  - name: report is compact
    output: report
    max_words: 500
  - name: report mentions traceability
    output: report
    contains: traceable provenance
```

Supported text checks are `contains`, `not_contains`, `regex`, `required_headings`, `min_words`, `max_words`, `min_bytes`, and `max_bytes`.

JSON outputs can be checked with a small selector syntax: root `$`, object keys, array indexes, and `*` wildcards. JSON eval checks support `exists`, `type`, `equals`, `min`, `max`, `contains`, `not_contains`, `regex`, `length_min`, and `length_max`. A missing JSON path fails by default; use `exists: false` when absence is the expected behavior.

```yaml
cases:
  - name: no malformed responses
    output: metrics
    json_path: $.cases.*.*.malformed_responses
    equals: 0
  - name: promoted candidate did not fail
    output: metrics
    json_path: $.cases.case-001.candidate.failed
    equals: 0
  - name: p95 latency stays under threshold
    output: metrics
    json_path: $.cases.*.*.latency_ms.p95
    max: 800
  - name: summary shape is stable
    output: metrics
    json_path: $.summary.text
    exists: true
    type: string
    regex: '^\d+ recommendations'
    length_max: 120
  - name: citations are present
    output: metrics
    json_path: $.citations
    type: array
    length_min: 2
```

```bash
lmake eval report
lmake approve report
lmake baseline show
```

`lmake approve <target>` requires the latest target run to be fresh and requires eval cases to pass when a suite exists. It writes:

```text
baselines/<target>.json
baselines/approvals/<target>/<timestamp>_<run_id>.json
```

`lmake compare <target>` compares the latest run to the approved baseline: fingerprint changes, output hash changes, eval results, JSON metric deltas when matching numeric JSON paths changed, and the normal artifact diff. `lmake gc` keeps baseline runs and their cached objects even when they are older than the normal retention window.

## Real LLM calls through LiteLLM

Example:

```yaml
defaults:
  provider: litellm
  model: openai/gpt-4o-mini
  params:
    temperature: 0
```

`lmake` calls LiteLLM's `completion(model=..., messages=..., **params)` function and stores response metadata in the manifest.

## Model locks

`lmake.lock` pins floating model aliases to concrete model IDs. If a configured model contains `latest`, `lmake` requires a lock entry before it will run.

```bash
lmake lock set claude-sonnet-latest anthropic/claude-sonnet-4-20250514 --provider litellm
lmake lock list
```

Then `lmakefile.yaml` can use the alias:

```yaml
defaults:
  provider: litellm
  model: claude-sonnet-latest
```

At runtime the target uses the resolved model from `lmake.lock`. The fingerprint records the lockfile hash, the alias, and the resolved model, so repinning the alias marks affected targets stale. Commit `lmake.lock` with the project when you want reproducible model provenance.

## Web UI

`lmake serve` starts a local web UI over the same files. Collaborators edit only `context/` documents, save with optimistic locking, click Update, read rendered artifacts, and publish the latest run. Developer mode is a toggle that reveals targets, runs, fingerprints, provider details, evals, baseline comparison, and approval.

The UI also opens a lightweight WebSocket state stream. If files change outside the browser, the page refreshes staleness and artifact state without a manual reload.

```bash
lmake serve
```

The web extra is optional; the CLI/core install does not pull in FastAPI or Uvicorn.

## Publishing

`lmake publish` writes a static HTML bundle containing the run outputs, copied artifacts, manifest provenance, and, when available, baseline/eval review provenance.

```bash
lmake publish --latest
lmake publish --latest --no-review
```

By default bundles are written under `published/<run_id>/`. `--latest` publishes the latest run for the configured default group/output target, so the static bundle centers the report-like artifact rather than an incidental upstream dependency.

When review data exists, the bundle includes:

```text
published/<run_id>/
  index.html
  manifest.json
  review.json
  outputs/*
```

`review.json` records the approved baseline relationship and deterministic eval results for the published run. Pass `--no-review` to omit that section and sidecar.

`lmake init` ignores `published/` by default. Commit a published bundle only when you deliberately want that static report in Git.

## CI and Pages

This repository includes two GitHub Actions workflows:

- `CI`: installs `lmake[web]`, runs tests, compiles the package, and smoke-tests the demo workflow.
- `Publish Demo`: runs the deterministic demo, approves the critique target, publishes the static bundle, uploads it as a workflow artifact, and deploys it to GitHub Pages when Pages is available.

GitHub Pages may need to be enabled for the repository with source set to GitHub Actions. Private repositories require a GitHub plan that supports private Pages, or the repository must be made public.

## Garbage collection

`lmake gc` prunes old run records and cached objects that are no longer referenced by kept manifests. It is a dry run by default.

```bash
lmake gc --keep-per-target 20
lmake gc --keep-per-target 20 --apply
```

The latest/current run for each target and the newest run per target whose artifacts match `artifacts/` are kept.

## DSPy program targets

DSPy integration is modeled as a runner, not a provider. A compile target can write a compiled program state artifact, and downstream targets can declare that artifact as a normal hashed input.

```yaml
targets:
  compile-report:
    runner: dspy
    program: programs/report_program.py
    inputs:
      - context/*.md
    dspy:
      action: compile
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
    dspy:
      action: run
      # Optional: override the default run_program hook name.
      run: run_report
      compiled: artifacts/report_program.json
    outputs:
      report: artifacts/report.md
```

The program module may expose `build_program()`, `compile_program(...)`, and `run_program(...)` hooks. `dspy.compile` and `dspy.run` can override those default hook names, as in `dspy.run: run_report`. If `compile_program` is omitted, `lmake` can instantiate a configured DSPy optimizer and call `optimizer.compile(program, trainset=...)`. If `run_program` is omitted, `lmake` calls the DSPy program with a single `context` field containing the prompt plus declared inputs.

## What v0 intentionally does not solve

- no semantic artifact diff
- no concurrent multi-agent execution
- no remote cache
- no hosted UI
- no full DSPy compiler UX yet
- no CRDT collaborative editor
- no signed manifests

Those are feature layers, not the primitive.

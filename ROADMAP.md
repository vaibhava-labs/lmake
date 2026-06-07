# Roadmap

## v0.1: local primitive

- [x] project initialization
- [x] YAML DAG targets
- [x] named run groups and `default_group`
- [x] content hashing
- [x] immutable manifests
- [x] local content-addressed object store
- [x] run/status/log/diff/replay CLI
- [x] publish/serve CLI entrypoints
- [x] deterministic mock provider
- [x] optional LiteLLM provider
- [x] private-alpha package metadata and Git install docs under `lmake-ai`

## v0.2: correctness hardening

- [x] atomic output promotion via run staging
- [x] crash recovery for staged runs
- [x] artifact byte integrity checks in status
- [x] run/read commands recover incomplete staged runs before reading state
- [x] `lmake gc` for pruning old run records and unreferenced objects
- [x] GC preserves approved baseline runs and objects
- [x] schema validation with useful errors
- [x] lockfile for provider/model aliases
- [ ] no-op mode that does not record reuse manifests
- [ ] manifest signing/checking
- [ ] richer status propagation through large DAGs / Kahn-style traversal
- [x] CI smoke tests

## v0.3: LLM-native features

- [x] deterministic `eval_cases/<target>.yaml`
- [x] `lmake eval`
- [x] approved baseline pointers in `baselines/<target>.json`
- [x] append-only approval records
- [x] `lmake approve`
- [x] `lmake compare` against approved baseline
- [ ] structured output schemas
- [ ] JSON artifact diff
- [ ] Markdown-aware diff
- [ ] semantic diff plugin interface
- [ ] LLM-judge eval attachment format
- [x] baseline/compare/eval/approve controls in web Details mode
- [ ] Weave/Langfuse/Braintrust trace refs

## v0.4: compiler/program layer

- [x] DSPy module runner
- [x] DSPy optimizer/compile target
- [x] compiled prompt/program artifact records
- [ ] real DSPy integration tests against installed `dspy`
- [ ] prompt registry import/export
- [ ] `runner: agent` sandbox and trace contract

## v0.5: collaboration and sharing

- [x] realistic local demo project with extract/synthesize/critique workflow
- [x] local web UI MVP
- [x] context-only document editor
- [x] optimistic-lock conflict response for collaborators
- [x] single Update button backed by `default_group`
- [x] developer-mode toggle for targets/runs/details
- [x] static HTML publish bundle
- [x] optional `lmake-ai[web]` packaging with no Node requirement for users
- [x] rendered Markdown artifact viewer beyond preformatted text in the web UI
- [x] published report bundles with optional baseline/eval provenance
- [x] WebSocket live staleness updates
- [ ] richer non-technical conflict resolution UI
- [ ] authenticated multi-user server mode
- [x] GitHub Pages demo publish workflow
- [ ] remote publish target such as S3

## v0.6: automation and remote

- [ ] remote cache spec
- [x] GitHub Action
- [x] static HTML run browser via GitHub Pages demo
- [ ] PR comment summaries
- [ ] optional hosted artifact viewer
- [ ] PyPI/TestPyPI release under the non-conflicting `lmake-ai` distribution name

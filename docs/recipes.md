# lmake Recipes

These are the first workflows worth proving for real users. They all use the same loop:

```text
run -> eval -> compare -> approve -> publish
```

## Review A Prompt Change

Use this when you changed a prompt and want to know whether the generated artifact improved or regressed.

```bash
lmake run
lmake eval critique
lmake approve critique

# Edit prompts/critique.md.
lmake status
lmake run
lmake eval critique
lmake compare critique
```

Expected behavior: unchanged upstream targets reuse prior outputs, the prompt-touched target recomputes, and `lmake compare` shows fingerprint changes, output hash changes, eval results, and the artifact diff against the approved baseline.

Approve the new output only after reviewing the diff:

```bash
lmake approve critique
```

## Review A Context Change

Use this when a domain expert edits source material in `context/`.

```bash
lmake status
lmake run
lmake eval critique
lmake compare critique
```

If the change touches an upstream source, downstream targets should become stale and recompute in dependency order. The compare output should answer the review question: what changed in the generated artifact, and did deterministic quality gates still pass?

## Publish An Approved Report

Use this after the latest run has passed evals and has either been approved or compared against an approved baseline.

```bash
lmake approve critique
lmake publish --latest
```

The published bundle includes rendered outputs, `manifest.json`, and, when available, `review.json` with baseline and eval provenance.

## Run The Real Haiku Proof

The normal test suite is deterministic and free. The real-provider proof is opt-in because it calls Anthropic through LiteLLM and may incur API cost.

```bash
python -m pip install -e '.[litellm,dev]'
export ANTHROPIC_API_KEY=...
LMAKE_RUN_LLM_TESTS=1 python -m pytest -m integration_llm
```

By default the test uses:

```text
anthropic/claude-haiku-4-5-20251001
```

Override it only when you intentionally want to test another low-cost model:

```bash
LMAKE_LLM_MODEL=anthropic/claude-haiku-4-5-20251001 \
LMAKE_RUN_LLM_TESTS=1 \
python -m pytest -m integration_llm
```

The test builds a temporary project, runs a baseline, approves it, edits a prompt, verifies only the downstream target goes stale, reruns, compares the latest output against the baseline, and publishes a bundle with review provenance.

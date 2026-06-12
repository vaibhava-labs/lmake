# LLM-Judge Evals

An LLM judge is just another `lmake` target. It reads an artifact, writes a
structured verdict artifact, and lets deterministic JSON eval cases gate the
verdict bytes.

That shape keeps the judge observable: the rubric prompt, judged artifact,
model settings, generated verdict, and run manifest are all part of the same
local project state as any other target.

## Judge Target Shape

A judge target depends on the artifact it reviews:

```yaml
targets:
  judge-critique:
    runner: dspy
    needs: [critique]
    program: programs/research_demo.py
    inputs:
      - artifacts/critique.md
    prompt: prompts/judge_critique.md
    dspy:
      action: run
      run: run_judge
      configure: false
    outputs:
      verdict: artifacts/critique_verdict.json
```

The demo uses a deterministic code-backed judge so the pattern works without
API keys. A real judge can use `runner: provider`, `provider: litellm`, and a
strict JSON rubric prompt.

## Verdict Schema

Verdicts use schema `lmake.judge_verdict.v0`:

```json
{
  "schema": "lmake.judge_verdict.v0",
  "target": "critique",
  "artifact": {
    "name": "critique",
    "path": "artifacts/critique.md",
    "sha256": "..."
  },
  "scores": {
    "traceability": 4,
    "source_accounting": 5,
    "readability": 3
  },
  "failures": [],
  "verdict": "pass",
  "rationale": "The critique preserves traceability and source accounting."
}
```

Scores are integers from 1 to 5. `verdict` is `pass` or `fail`. `failures`
lists the rubric dimensions that scored below threshold.

For code-backed judges, `artifact.sha256` should be copied from the judged
artifact input record. For LLM-backed judges, `artifact.sha256` is optional
because models cannot reliably echo hashes; the linkage is still present in the
judge run manifest's input records.

## Deterministic Gates

The verdict is a generated observation. The gate over that observation is a
deterministic eval suite:

```yaml
version: 1
target: judge-critique
cases:
  - name: rubric scores stay above threshold
    output: verdict
    json_path: $.scores.*
    min: 3
  - name: no rubric dimensions failed
    output: verdict
    json_path: $.failures
    type: array
    length_max: 0
```

The review loop is the normal loop:

```bash
lmake run judge-critique
lmake eval judge-critique
lmake approve judge-critique
lmake compare judge-critique
```

Because verdicts are JSON, `lmake compare judge-critique` already reports score
deltas through the JSON metric delta table.

## LLM-Backed Variant

For a real model judge, write a rubric prompt that demands raw JSON only:

```yaml
targets:
  judge-critique:
    runner: provider
    provider: litellm
    model: anthropic/claude-haiku-4-5-20251001
    needs: [critique]
    inputs:
      - artifacts/critique.md
    prompt: prompts/judge_critique.md
    outputs:
      verdict: artifacts/critique_verdict.json
```

Malformed judge output should fail loudly. If the model returns non-JSON,
`lmake eval judge-critique` fails with `output is not valid JSON`, which is the
intended failure mode.

## Cost And Caching

Judge targets use the same cache policy as other targets. With
`reuse_policy: input-identical`, a judge run is reused when the judged artifact,
rubric prompt, target spec, and model settings are unchanged. It reruns when the
artifact or rubric changes.

## What This Is Not

Judge verdicts are recorded observations with provenance, not deterministic
truth. Use deterministic eval cases to decide whether a verdict is acceptable.

Surfacing judge verdict sections in `lmake compare <judged-target>` is future
work. Today, compare the judge target itself to inspect verdict and score
changes.

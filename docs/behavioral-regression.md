# Behavioral Regression Workflows

`lmake` can review more than static generated documents. A real interaction,
transcript, support thread, or customer request can become a replayable case for
future AI behavior changes.

The general loop is:

```text
real case + labels + model/prompt/context/code
-> replay or regenerate
-> score behavior
-> compare against approved behavior
-> approve or reject the change
```

## Suggested Project Shape

These folders are conventions, not new required core primitives:

```text
context/
  cases/
    case-id/
      inputs/
      variants/
  labels/
    behavior.yaml
  suites/
    smoke.yaml
    regression.yaml
    archive.yaml
programs/
  replay.py
  review.py
artifacts/
  metrics.json
  moment-score.json
  review.md
```

## Cases

A case is a frozen real-world example. For a live interview assistant, it might
include transcript chunks, a CV, a role brief, prior visible nudges, and
telemetry. For a report generator, it might include source notes and previously
approved reports.

Cases should be cheap to capture after every real run, but only the interesting
ones need to be promoted into a regression suite.

## Labels

Labels describe product behavior, not exact output text:

```yaml
cases:
  case-001:
    expected:
      - id: important_topic_gets_probe
        window: ["25:00", "34:30"]
        min_visible: 1
        terms: ["ownership", "measured"]
    forbidden:
      - id: stale_coverage_interrupts_active_topic
        window: ["25:00", "34:30"]
        category: coverage_gap
        max_visible: 0
```

Use labels for moments that taught you something: missed opportunities, stale
cards, silence during useful discussion, overproduction, hallucinated claims, or
output that should remain stable.

## Suites

Suites decide how much of the corpus to run:

```yaml
version: 1
name: smoke
cases:
  - name: case-001
    variants: [baseline, candidate]
  - name: case-002
    variants: [baseline, candidate]
```

Suggested cadence:

- `smoke`: run during local prompt/model/code iteration.
- `regression`: run before merging or shipping behavior changes.
- `archive`: run occasionally or before major model migrations.

## Metrics

Review programs should emit structured JSON so `lmake eval` and `lmake compare`
can reason about behavior:

```json
{
  "cases": {
    "case-001": {
      "candidate": {
        "failed": 5,
        "visible_outputs": 19,
        "latency_ms": {"p95": 15427},
        "total_cost_cents": 213.2772
      }
    }
  }
}
```

Then eval suites can use JSON metric checks:

```yaml
cases:
  - name: p95 latency stays bounded
    output: metrics
    json_path: $.cases.*.*.latency_ms.p95
    max: 20000

  - name: promoted case has no moment failures
    output: moment_score
    json_path: $.cases.case-002.candidate.failed
    equals: 0

  - name: fresh replay emitted citations
    output: metrics
    json_path: $.cases.case-002.candidate.citations
    exists: true
    type: array
    length_min: 2
```

## Workflow Commands

A typical local review loop is:

```bash
lmake run live-review
lmake eval live-review
lmake compare live-review
lmake approve live-review
```

Use `lmake run` to regenerate artifacts, `lmake eval` to check deterministic
bounds, `lmake compare` to inspect metric and artifact deltas against the
approved baseline, and `lmake approve` only when the changed behavior is
intentional.

## Fresh Replays

Saved replay artifacts are useful for review history, but the stronger loop is
to replay frozen cases through current code:

```text
frozen corpus + current prompt/model/arbiter
-> fresh replay JSONL
-> metric and moment-score artifacts
-> compare against approved baseline
```

Fresh replay targets should usually be opt-in because they may send sensitive
case data to an external model and incur cost.

## History Dashboards

Once runs emit structured `metrics.json` and `moment-score.json`, a project can
summarize immutable lmake run history into a local dashboard:

```text
runs/*/manifest.json + cached moment-score outputs
-> recurring failure modes
-> latest suite health
-> improving or worsening targets
```

This does not need to become a hosted service. A checked-in target can produce a
Markdown dashboard and JSON history artifact for review, while `lmake publish`
can expose the latest approved run for non-technical collaborators.

## Why This Matters

Over time, the corpus becomes a versioned behavioral test set. It tells the team
which failure modes are improving, recurring, or regressing, and gives prompt,
model, and code changes the same review loop as ordinary software changes.

# Killer Demo: Catch A Prompt Regression

This demo is a tight local flow:

```text
edit prompt/context
-> lmake status
-> lmake run
-> lmake eval
-> lmake compare
-> lmake approve
-> lmake publish
```

The point is not that `lmake` can generate a report. The point is that a
plausible prompt edit can make an artifact look cleaner while silently dropping
review-critical behavior. The demo catches that regression, shows the delta
against the approved baseline, and records the approval trail after the fix.

## One-Command Version

From the repo root:

```bash
bash demo_project/scripts/killer_demo.sh
```

The script uses the deterministic local demo program and does not require API
keys.

## Manual 90-Second Flow

Start from a clean safe prompt and approve it as the baseline:

```bash
cd demo_project
cp prompts/critique.safe.md prompts/critique.md
PYTHONPATH=.. python3 -m lmake run
PYTHONPATH=.. python3 -m lmake eval critique
PYTHONPATH=.. python3 -m lmake approve critique
```

Introduce the regression:

```bash
cp prompts/critique.regression.md prompts/critique.md
PYTHONPATH=.. python3 -m lmake status
PYTHONPATH=.. python3 -m lmake run
PYTHONPATH=.. python3 -m lmake eval critique
```

Expected result: eval fails because the concise prompt dropped risk review and
traceability accounting.

Inspect the difference:

```bash
PYTHONPATH=.. python3 -m lmake compare critique
```

The compare report shows the changed fingerprint, changed output, failing eval
cases, and artifact diff against the approved baseline.

Fix, approve, and publish:

```bash
cp prompts/critique.safe.md prompts/critique.md
PYTHONPATH=.. python3 -m lmake run
PYTHONPATH=.. python3 -m lmake eval critique
PYTHONPATH=.. python3 -m lmake compare critique
PYTHONPATH=.. python3 -m lmake approve critique
PYTHONPATH=.. python3 -m lmake publish --latest
```

The published bundle includes the rendered artifact, immutable manifest, and
review provenance when a baseline/eval record exists.

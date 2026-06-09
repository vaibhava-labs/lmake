#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO="$ROOT/demo_project"

if [[ -n "${LMAKE_BIN:-}" ]]; then
  # shellcheck disable=SC2206
  LMAKE_CMD=(${LMAKE_BIN})
else
  LMAKE_CMD=(python3 -m lmake)
  export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
fi

run_lmake() {
  "${LMAKE_CMD[@]}" "$@"
}

cd "$DEMO"

echo
echo "== 0. Reset to the approved safe prompt and create a baseline =="
cp prompts/critique.safe.md prompts/critique.md
run_lmake run
run_lmake eval critique
run_lmake approve critique

echo
echo "== 1. Edit prompt: introduce a concise-but-unsafe regression =="
cp prompts/critique.regression.md prompts/critique.md
run_lmake status

echo
echo "== 2. Re-run the workflow =="
run_lmake run

echo
echo "== 3. Eval catches the regression =="
if run_lmake eval critique; then
  echo "Expected eval to fail after regression prompt, but it passed." >&2
  exit 1
else
  echo "Eval failed as expected: traceability/risk sections were dropped."
fi

echo
echo "== 4. Compare explains what changed against the approved baseline =="
run_lmake compare critique

echo
echo "== 5. Fix prompt, rerun, approve, and publish =="
cp prompts/critique.safe.md prompts/critique.md
run_lmake status
run_lmake run
run_lmake eval critique
run_lmake compare critique
run_lmake approve critique
run_lmake publish --latest

echo
echo "Demo complete. The active prompt is restored to prompts/critique.safe.md."

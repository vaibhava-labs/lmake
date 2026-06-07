# lmake Demo Project

This demo is meant to feel like a real collaborator workflow:

1. A non-technical teammate edits research notes in `context/`.
2. They click Update in `lmake serve` or run `lmake run`.
3. lmake produces a claims extraction, synthesis brief, and critique.
4. `lmake publish --latest` generates a static report bundle.

The demo uses a deterministic local program so it works without API keys. To try a real model, keep the same target graph and replace `runner: dspy` with `provider: litellm` prompt targets, or adapt `programs/research_demo.py` into a real DSPy module.

## Commands

```bash
lmake status
lmake run
lmake eval critique
lmake approve critique
lmake publish --latest
lmake serve
```

The default group is `update`, which runs the terminal `critique` target and all of its dependencies.
After approving a baseline, edit a context file, run `lmake run`, and use `lmake compare critique` to see what changed against the approved run.

## Real Haiku run

The default demo uses deterministic local hooks. To try the same workflow with a real low-cost Anthropic model:

```bash
python -m pip install -e '.[litellm]'
cp .env.example .env
# Fill ANTHROPIC_API_KEY in .env, then export it into your shell.
set -a; . ./.env; set +a
cp lmakefile.haiku.yaml lmakefile.yaml
lmake run
lmake eval critique
lmake approve critique
lmake publish --latest
```

Keep `.env` out of Git. The sample `.gitignore` already does.

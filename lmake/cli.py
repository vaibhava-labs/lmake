from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .baseline import approve_latest, baseline_rows, set_baseline
from .compare import collect_compare_result, compare_exit_code, render_compare_result
from .config import ProjectConfig, find_project_root
from .engine import diff_runs, logs, replay_run, run_target, status_all
from .evals import evaluate_target
from .errors import LMakeError
from .gc import gc_project
from .lockfile import ProjectLock, remove_model_pin, set_model_pin
from .publish import publish_run


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = {c: max(len(c), *(len(str(row.get(c, ""))) for row in rows)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


def load_config() -> ProjectConfig:
    return ProjectConfig.load(find_project_root())


def cmd_init(args: argparse.Namespace) -> int:
    root = Path.cwd()
    lmfile = root / "lmakefile.yaml"
    if lmfile.exists() and not args.force:
        raise LMakeError("lmakefile.yaml already exists; pass --force to overwrite the sample file.")
    for d in ["context", "prompts", "programs", "eval_cases", "baselines", ".lmcache", "runs", "artifacts"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    if args.force or not lmfile.exists():
        lmfile.write_text(SAMPLE_LMAKEFILE, encoding="utf-8")
    sample_context = root / "context" / "brief.md"
    if args.force or not sample_context.exists():
        sample_context.write_text(SAMPLE_CONTEXT, encoding="utf-8")
    extract_prompt = root / "prompts" / "extract.md"
    if args.force or not extract_prompt.exists():
        extract_prompt.write_text(SAMPLE_EXTRACT_PROMPT, encoding="utf-8")
    report_prompt = root / "prompts" / "report.md"
    if args.force or not report_prompt.exists():
        report_prompt.write_text(SAMPLE_REPORT_PROMPT, encoding="utf-8")
    report_eval = root / "eval_cases" / "report.yaml"
    if args.force or not report_eval.exists():
        report_eval.write_text(SAMPLE_REPORT_EVALS, encoding="utf-8")
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".env\n.env.*\n!.env.example\n.lmcache/\npublished/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    print("initialized lmake project")
    print("try: lmake run")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    results = run_target(config, args.target, force=args.force)
    rows = [
        {
            "target": r.target,
            "run_id": r.run_id,
            "mode": r.mode,
            "cache_state": r.cache_state,
        }
        for r in results
    ]
    print_table(rows, ["target", "run_id", "mode", "cache_state"])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    statuses = status_all(config)
    rows = [
        {
            "target": s.target,
            "status": s.status,
            "latest_run": (s.latest_run_id or "")[:32],
            "reason": s.reason,
        }
        for s in statuses
    ]
    print_table(rows, ["target", "status", "latest_run", "reason"])
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    config = load_config()
    rows = logs(config, limit=args.limit)
    print_table(rows, ["created_at", "target", "mode", "cache_state", "fingerprint", "run_id", "outputs"])
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    config = load_config()
    print(diff_runs(config, args.left, args.right))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    config = load_config()
    result = replay_run(config, args.run_id)
    print_table([
        {
            "target": result.target,
            "run_id": result.run_id,
            "mode": result.mode,
            "cache_state": result.cache_state,
            "reused_from": result.manifest.get("reused_from_run_id"),
        }
    ], ["target", "run_id", "mode", "cache_state", "reused_from"])
    return 0


def cmd_targets(args: argparse.Namespace) -> int:
    config = load_config()
    rows = []
    for name, target in config.targets.items():
        model = f"{target.model_alias} -> {target.model}" if target.model_alias else target.model
        rows.append({
            "target": name,
            "runner": target.runner,
            "needs": ",".join(target.needs),
            "provider": target.provider,
            "model": model,
            "outputs": ",".join(target.outputs.values()) if target.outputs else f"artifacts/{name}.md",
        })
    print_table(rows, ["target", "runner", "needs", "provider", "model", "outputs"])
    if config.groups:
        print()
        group_rows = [{"group": name, "targets": ",".join(targets)} for name, targets in config.groups.items()]
        print_table(group_rows, ["group", "targets"])
    print()
    print(f"default: {config.default_group or 'all'}")
    return 0


def cmd_lock_list(args: argparse.Namespace) -> int:
    root = find_project_root()
    lock = ProjectLock.load(root)
    rows = [
        {
            "alias": alias,
            "provider": entry.provider or "",
            "resolved": entry.resolved,
            "pinned_at": entry.pinned_at or "",
        }
        for alias, entry in sorted(lock.models.items())
    ]
    print_table(rows, ["alias", "provider", "resolved", "pinned_at"])
    return 0


def cmd_lock_set(args: argparse.Namespace) -> int:
    root = find_project_root()
    entry = set_model_pin(root, args.alias, args.resolved, provider=args.provider)
    print(f"locked {entry.alias} -> {entry.resolved}")
    return 0


def cmd_lock_remove(args: argparse.Namespace) -> int:
    root = find_project_root()
    existed = remove_model_pin(root, args.alias)
    if existed:
        print(f"removed lock for {args.alias}")
    else:
        print(f"no lock for {args.alias}")
    return 0


def cmd_baseline_show(args: argparse.Namespace) -> int:
    config = load_config()
    rows = baseline_rows(config.root, target=args.target)
    print_table(rows, ["target", "run_id", "fingerprint", "set_at", "source"])
    return 0


def cmd_baseline_set(args: argparse.Namespace) -> int:
    config = load_config()
    record = set_baseline(config, args.target, args.run_id)
    rows = [{
        "target": record.get("target"),
        "run_id": record.get("run_id"),
        "fingerprint": str(record.get("target_fingerprint", ""))[:12],
        "source": record.get("source"),
    }]
    print_table(rows, ["target", "run_id", "fingerprint", "source"])
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    config = load_config()
    with_evals = not args.no_evals
    result = collect_compare_result(config, args.target, with_evals=with_evals)
    print(render_compare_result(result, fmt=args.format, with_evals=with_evals))
    if args.exit_code:
        return compare_exit_code(result)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    config = load_config()
    result = evaluate_target(config, args.target, run_id=args.run_id, suite_path=Path(args.suite) if args.suite else None)
    if result is None:
        print(f"no eval_cases suite found for target {args.target!r}")
        return 0
    print(f"target: {result.target}")
    print(f"run_id: {result.run_id}")
    print(f"suite:  {result.suite_path}")
    print_table(result.rows(), ["case", "status", "output", "reason"])
    return 1 if result.failed else 0


def cmd_approve(args: argparse.Namespace) -> int:
    config = load_config()
    record, eval_summary = approve_latest(config, args.target, require_evals=not args.skip_evals)
    rows = [{
        "target": record.get("target"),
        "run_id": record.get("run_id"),
        "fingerprint": str(record.get("target_fingerprint", ""))[:12],
        "evals": "" if eval_summary is None else f"{eval_summary['passed']} pass/{eval_summary['failed']} fail",
    }]
    print_table(rows, ["target", "run_id", "fingerprint", "evals"])
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    config = load_config()
    output_dir = publish_run(
        config,
        run_id=args.run_id,
        latest=args.latest,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        include_review=not args.no_review,
    )
    print(f"published: {output_dir / 'index.html'}")
    print("open in a browser, or push the bundle to GitHub Pages/S3 to share it")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    config = load_config()
    result = gc_project(config, keep_per_target=args.keep_per_target, dry_run=not args.apply)
    rows = [
        {"kind": "runs", "kept": result.kept_runs, "would_prune": len(result.pruned_runs) if result.dry_run else "", "pruned": "" if result.dry_run else len(result.pruned_runs)},
        {"kind": "objects", "kept": result.kept_objects, "would_prune": len(result.pruned_objects) if result.dry_run else "", "pruned": "" if result.dry_run else len(result.pruned_objects)},
    ]
    print_table(rows, ["kind", "kept", "would_prune", "pruned"])
    if result.dry_run:
        print("dry run only; pass --apply to delete prunable runs and objects")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # type: ignore
    except ImportError as exc:
        raise LMakeError("lmake serve requires `python -m pip install -e '.[web]'`.") from exc
    from .web import create_app

    config = load_config()
    app = create_app(config.root)
    print(f"serving lmake project at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lmake", description="A tiny git-native build tool for LLM workflows.")
    parser.add_argument("--version", action="version", version=f"lmake {__version__}")
    parser.add_argument("-C", "--chdir", help="run as if lmake was started in this directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create a sample lmake project in the current directory")
    p_init.add_argument("--force", action="store_true", help="overwrite the sample lmakefile/prompts/context")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="execute a target and its dependencies")
    p_run.add_argument("target", nargs="?", help="target or group to run; defaults to default_group or all terminal targets")
    p_run.add_argument("--force", action="store_true", help="force recomputation even if inputs are unchanged")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="show stale/fresh state for all targets")
    p_status.set_defaults(func=cmd_status)

    p_log = sub.add_parser("log", help="show immutable run history")
    p_log.add_argument("--limit", type=int, default=None)
    p_log.set_defaults(func=cmd_log)

    p_diff = sub.add_parser("diff", help="diff artifacts across two runs")
    p_diff.add_argument("left")
    p_diff.add_argument("right")
    p_diff.set_defaults(func=cmd_diff)

    p_replay = sub.add_parser("replay", help="restore/reuse artifacts from a prior run")
    p_replay.add_argument("run_id")
    p_replay.set_defaults(func=cmd_replay)

    p_targets = sub.add_parser("targets", help="list configured targets")
    p_targets.set_defaults(func=cmd_targets)

    p_lock = sub.add_parser("lock", help="manage model alias pins in lmake.lock")
    lock_sub = p_lock.add_subparsers(dest="lock_command", required=True)
    p_lock_list = lock_sub.add_parser("list", help="list pinned model aliases")
    p_lock_list.set_defaults(func=cmd_lock_list)
    p_lock_set = lock_sub.add_parser("set", help="pin a model alias to a resolved model")
    p_lock_set.add_argument("alias", help="model alias used in lmakefile.yaml, e.g. claude-sonnet-latest")
    p_lock_set.add_argument("resolved", help="concrete provider model id, e.g. anthropic/claude-sonnet-4-20250514")
    p_lock_set.add_argument("--provider", help="optional provider this pin applies to, e.g. litellm")
    p_lock_set.set_defaults(func=cmd_lock_set)
    p_lock_remove = lock_sub.add_parser("remove", help="remove a model alias pin")
    p_lock_remove.add_argument("alias")
    p_lock_remove.set_defaults(func=cmd_lock_remove)

    p_baseline = sub.add_parser("baseline", help="manage approved baseline run pointers")
    baseline_sub = p_baseline.add_subparsers(dest="baseline_command", required=True)
    p_baseline_show = baseline_sub.add_parser("show", help="show baseline runs")
    p_baseline_show.add_argument("target", nargs="?", help="optional target to show")
    p_baseline_show.set_defaults(func=cmd_baseline_show)
    p_baseline_set = baseline_sub.add_parser("set", help="set a target baseline to a run")
    p_baseline_set.add_argument("target")
    p_baseline_set.add_argument("run_id")
    p_baseline_set.set_defaults(func=cmd_baseline_set)

    p_compare = sub.add_parser("compare", help="compare latest run for a target against its baseline")
    p_compare.add_argument("target")
    p_compare.add_argument("--no-evals", action="store_true", help="skip eval_cases in the compare report")
    p_compare.add_argument("--format", choices=["text", "github"], default="text", help="output format")
    p_compare.add_argument("--exit-code", action="store_true", help="exit 1 when outputs differ or latest evals fail")
    p_compare.set_defaults(func=cmd_compare)

    p_eval = sub.add_parser("eval", help="run deterministic eval_cases against a target run")
    p_eval.add_argument("target")
    p_eval.add_argument("--run-id", help="run id or prefix to evaluate; defaults to latest target run")
    p_eval.add_argument("--suite", help="eval suite path; defaults to eval_cases/<target>.yaml")
    p_eval.set_defaults(func=cmd_eval)

    p_approve = sub.add_parser("approve", help="approve the latest target run as the new baseline")
    p_approve.add_argument("target")
    p_approve.add_argument("--skip-evals", action="store_true", help="approve even if eval_cases are missing or failing")
    p_approve.set_defaults(func=cmd_approve)

    p_publish = sub.add_parser("publish", help="generate a static HTML bundle for a run")
    p_publish.add_argument("run_id", nargs="?", help="run id or prefix; defaults to latest run")
    p_publish.add_argument("--latest", action="store_true", help="publish the latest run")
    p_publish.add_argument("-o", "--output-dir", help="directory to write the static bundle")
    p_publish.add_argument("--no-review", action="store_true", help="omit baseline/eval review provenance from the bundle")
    p_publish.set_defaults(func=cmd_publish)

    p_gc = sub.add_parser("gc", help="prune old run records and unreachable cached objects")
    p_gc.add_argument("--keep-per-target", type=int, default=20, help="number of recent runs to keep per target")
    p_gc.add_argument("--apply", action="store_true", help="delete prunable runs and objects; defaults to dry run")
    p_gc.set_defaults(func=cmd_gc)

    p_serve = sub.add_parser("serve", help="start the optional local web UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    old_cwd = None
    try:
        if args.chdir:
            old_cwd = Path.cwd()
            os.chdir(args.chdir)
        return int(args.func(args))
    except LMakeError as exc:
        print(f"lmake: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("lmake: interrupted", file=sys.stderr)
        return 130
    finally:
        if old_cwd is not None:
            os.chdir(old_cwd)


SAMPLE_LMAKEFILE = """\
version: 1

defaults:
  provider: mock
  model: mock/deterministic
  params:
    temperature: 0
  cache:
    reuse_policy: input-identical

default_group: update

groups:
  update:
    targets:
      - report

# Every target is an LLM build step. Inputs and prompts are hashed.
# Runs are immutable; artifacts are content-addressed in .lmcache/.
targets:
  extract:
    description: Extract claims from source context.
    inputs:
      - context/*.md
    prompt: prompts/extract.md
    outputs:
      claims: artifacts/claims.md

  report:
    description: Synthesize a short report from claims and context.
    needs:
      - extract
    inputs:
      - context/*.md
      - artifacts/claims.md
    prompt: prompts/report.md
    outputs:
      report: artifacts/report.md
"""

SAMPLE_CONTEXT = """\
# Brief

We want a local, git-native build tool for LLM workflows.

The tool should treat prompts, context, workflow definitions, model settings, and generated outputs as explicit build inputs and artifacts.
"""

SAMPLE_EXTRACT_PROMPT = """\
Extract the central claims from the input files.
Return concise Markdown bullets.
"""

SAMPLE_REPORT_PROMPT = """\
Write a short synthesis report from the claims and context.
Prefer concrete implementation details over hype.
"""

SAMPLE_REPORT_EVALS = """\
version: 1
target: report
cases:
  - name: report names source context
    output: report
    contains: context/brief.md
  - name: report uses upstream claims
    output: report
    contains: artifacts/claims.md
  - name: report is not empty
    output: report
    min_words: 20
  - name: report stays compact
    output: report
    max_words: 500
"""


if __name__ == "__main__":
    raise SystemExit(main())

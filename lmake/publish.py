from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .baseline import load_baseline
from .config import ProjectConfig
from .evals import evaluate_target
from .engine import recovered_state
from .errors import LMakeError, RunNotFoundError
from .state import ProjectState, canonical_json_pretty


MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def markdown_to_html(text: str) -> str:
    lines = text.splitlines()
    chunks: list[str] = []
    in_list = False
    in_code = False
    code_lines: list[str] = []

    def inline(value: str) -> str:
        escaped = html.escape(value)
        escaped = MD_LINK_RE.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>', escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        return escaped

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                chunks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                if in_list:
                    chunks.append("</ul>")
                    in_list = False
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            if in_list:
                chunks.append("</ul>")
                in_list = False
            continue
        if stripped.startswith("#"):
            if in_list:
                chunks.append("</ul>")
                in_list = False
            level = min(len(stripped) - len(stripped.lstrip("#")), 6)
            text_value = stripped[level:].strip()
            chunks.append(f"<h{level}>{inline(text_value)}</h{level}>")
        elif stripped.startswith(("- ", "* ")):
            if not in_list:
                chunks.append("<ul>")
                in_list = True
            chunks.append(f"<li>{inline(stripped[2:].strip())}</li>")
        else:
            if in_list:
                chunks.append("</ul>")
                in_list = False
            chunks.append(f"<p>{inline(stripped)}</p>")
    if in_code:
        chunks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    if in_list:
        chunks.append("</ul>")
    return "\n".join(chunks)


def latest_manifest(config: ProjectConfig, state: ProjectState | None = None) -> dict[str, Any]:
    state = state or recovered_state(config)
    candidates = []
    for target_name in config.run_roots(None):
        manifest = state.latest_manifest_for_target(target_name)
        if manifest is not None:
            candidates.append(manifest)
    if candidates:
        candidates.sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("run_id", ""))), reverse=True)
        return candidates[0]
    manifests = state.all_manifests()
    if not manifests:
        raise RunNotFoundError("no runs have been recorded yet")
    return manifests[0]


def safe_output_name(name: str) -> str:
    cleaned = SAFE_NAME_RE.sub("-", name).strip(".-")
    return cleaned or "output"


def rel_or_str(root: Path, value: str) -> str:
    path = Path(value)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return value


def output_hashes(items: list[dict[str, Any]]) -> dict[str, str]:
    return {str(item.get("name")): str(item.get("sha256")) for item in items}


def build_review_provenance(config: ProjectConfig, manifest: dict[str, Any]) -> dict[str, Any] | None:
    target = str(manifest.get("target"))
    run_id = str(manifest.get("run_id"))
    review: dict[str, Any] = {
        "schema": "lmake.publish_review.v0",
        "target": target,
        "run_id": run_id,
    }

    try:
        baseline = load_baseline(config.root, target)
    except LMakeError as exc:
        baseline = None
        review["baseline_error"] = str(exc)
    if baseline is not None:
        baseline_outputs = output_hashes(list(baseline.get("outputs", [])))
        run_outputs = output_hashes(list(manifest.get("outputs", [])))
        changed_outputs = [
            name for name in sorted(set(baseline_outputs) | set(run_outputs))
            if baseline_outputs.get(name) != run_outputs.get(name)
        ]
        review["baseline"] = {
            "run_id": baseline.get("run_id"),
            "set_at": baseline.get("set_at"),
            "source": baseline.get("source"),
            "same_run": baseline.get("run_id") == run_id,
            "fingerprint": "unchanged" if baseline.get("target_fingerprint") == manifest.get("target_fingerprint") else "changed",
            "outputs": "unchanged" if not changed_outputs else changed_outputs,
        }

    try:
        eval_result = evaluate_target(config, target, run_id=run_id)
    except LMakeError as exc:
        eval_result = None
        review["eval_error"] = str(exc)
    if eval_result is not None:
        review["evals"] = {
            "suite_path": rel_or_str(config.root, eval_result.suite_path),
            "passed": eval_result.passed,
            "failed": eval_result.failed,
            "cases": eval_result.rows(),
        }

    if not any(key in review for key in ("baseline", "baseline_error", "evals", "eval_error")):
        return None
    return review


def publish_run(
    config: ProjectConfig,
    *,
    run_id: str | None = None,
    latest: bool = False,
    output_dir: Path | None = None,
    include_review: bool = True,
) -> Path:
    state = recovered_state(config)
    manifest = latest_manifest(config, state) if latest or not run_id else state.load_manifest(run_id)
    run_id_value = str(manifest.get("run_id"))
    if output_dir is None:
        output_dir = config.root / "published" / run_id_value
    elif not output_dir.is_absolute():
        output_dir = config.root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir = output_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    rendered_outputs: list[dict[str, Any]] = []
    for item in manifest.get("outputs", []):
        digest = str(item.get("sha256", ""))
        logical_name = str(item.get("name", "output"))
        source = state.object_path(digest)
        if not source.exists():
            raise RunNotFoundError(f"artifact object sha256:{digest} is missing from .lmcache")
        suffix = Path(str(item.get("path", ""))).suffix or ".txt"
        output_name = f"{safe_output_name(logical_name)}{suffix}"
        dest = outputs_dir / output_name
        data = source.read_bytes()
        dest.write_bytes(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        rendered_outputs.append({
            "name": logical_name,
            "path": str(item.get("path")),
            "file": f"outputs/{output_name}",
            "sha256": digest,
            "html": markdown_to_html(text) if text else "<p>Binary artifact copied.</p>",
        })

    (output_dir / "manifest.json").write_text(canonical_json_pretty(manifest), encoding="utf-8")
    review = build_review_provenance(config, manifest) if include_review else None
    if review is not None:
        (output_dir / "review.json").write_text(canonical_json_pretty(review), encoding="utf-8")
    page = render_publish_page(manifest, rendered_outputs, review=review)
    (output_dir / "index.html").write_text(page, encoding="utf-8")
    return output_dir


def render_review_section(review: dict[str, Any] | None) -> str:
    if review is None:
        return ""
    baseline = review.get("baseline")
    baseline_html = ""
    if isinstance(baseline, dict):
        outputs = baseline.get("outputs")
        outputs_text = outputs if isinstance(outputs, str) else ", ".join(str(item) for item in outputs)
        baseline_html = f"""
        <div class="review-block">
          <h3>Approved baseline</h3>
          <dl>
            <dt>Run</dt><dd>{html.escape(str(baseline.get("run_id")))}</dd>
            <dt>Relationship</dt><dd>{'This published run is the approved baseline.' if baseline.get("same_run") else 'This published run differs from the approved baseline.'}</dd>
            <dt>Fingerprint</dt><dd>{html.escape(str(baseline.get("fingerprint")))}</dd>
            <dt>Outputs</dt><dd>{html.escape(outputs_text)}</dd>
            <dt>Set at</dt><dd>{html.escape(str(baseline.get("set_at") or ""))}</dd>
          </dl>
        </div>
        """
    elif review.get("baseline_error"):
        baseline_html = f"""<div class="review-block"><h3>Approved baseline</h3><p>{html.escape(str(review.get("baseline_error")))}</p></div>"""

    evals = review.get("evals")
    evals_html = ""
    if isinstance(evals, dict):
        rows = "\n".join(
            f"""
            <tr>
              <td>{html.escape(str(item.get("case")))}</td>
              <td class="{html.escape(str(item.get("status")))}">{html.escape(str(item.get("status")))}</td>
              <td>{html.escape(str(item.get("reason")))}</td>
            </tr>
            """
            for item in evals.get("cases", [])
        )
        evals_html = f"""
        <div class="review-block">
          <h3>Evals</h3>
          <p>{html.escape(str(evals.get("passed")))} passed, {html.escape(str(evals.get("failed")))} failed</p>
          <p class="muted">{html.escape(str(evals.get("suite_path")))}</p>
          <table>
            <thead><tr><th>Case</th><th>Status</th><th>Reason</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """
    elif review.get("eval_error"):
        evals_html = f"""<div class="review-block"><h3>Evals</h3><p>{html.escape(str(review.get("eval_error")))}</p></div>"""

    return f"""
    <section class="review">
      <div class="artifact-meta">
        <h2>Review provenance</h2>
        <a href="review.json">review.json</a>
      </div>
      {baseline_html}
      {evals_html}
    </section>
    """


def render_publish_page(manifest: dict[str, Any], outputs: list[dict[str, Any]], *, review: dict[str, Any] | None = None) -> str:
    output_sections = "\n".join(
        f"""
        <section class="artifact">
          <div class="artifact-meta">
            <h2>{html.escape(str(item["name"]))}</h2>
            <a href="{html.escape(str(item["file"]), quote=True)}">download</a>
          </div>
          <div class="rendered">{item["html"]}</div>
        </section>
        """
        for item in outputs
    )
    review_section = render_review_section(review)
    manifest_json = html.escape(json.dumps(manifest, indent=2, sort_keys=True))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>lmake run {html.escape(str(manifest.get("run_id")))}</title>
  <style>
    body {{ margin: 0; font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #202124; background: #f7f7f4; }}
    header {{ padding: 28px 32px 18px; border-bottom: 1px solid #ddd9ce; background: #fff; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 48px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 0; font-size: 20px; letter-spacing: 0; }}
    h3 {{ margin: 18px 0 8px; font-size: 16px; letter-spacing: 0; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 10px 18px; color: #5e625f; }}
    .artifact, .review {{ background: #fff; border: 1px solid #ddd9ce; border-radius: 8px; margin: 18px 0; padding: 22px; }}
    .artifact-meta {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #ece8df; padding-bottom: 12px; margin-bottom: 18px; }}
    .rendered h1, .rendered h2, .rendered h3 {{ margin-top: 1em; letter-spacing: 0; }}
    .muted {{ color: #5e625f; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; }}
    dt {{ color: #5e625f; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #ece8df; padding: 7px 6px; text-align: left; vertical-align: top; }}
    .pass {{ color: #1d6a2b; }}
    .fail {{ color: #9b1c1c; }}
    pre {{ overflow: auto; background: #f1f3f2; padding: 12px; border-radius: 6px; }}
    code {{ background: #f1f3f2; padding: 1px 4px; border-radius: 4px; }}
    details {{ margin-top: 24px; }}
    summary {{ cursor: pointer; font-weight: 650; }}
  </style>
</head>
<body>
  <header>
    <h1>Published lmake Run</h1>
    <div class="meta">
      <span>{html.escape(str(manifest.get("target")))}</span>
      <span>{html.escape(str(manifest.get("created_at")))}</span>
      <span>{html.escape(str(manifest.get("cache_state")))}</span>
    </div>
  </header>
  <main>
    {output_sections or "<p>No outputs recorded.</p>"}
    {review_section}
    <details>
      <summary>Run provenance</summary>
      <pre><code>{manifest_json}</code></pre>
    </details>
  </main>
</body>
</html>
"""

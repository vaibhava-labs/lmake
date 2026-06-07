from __future__ import annotations

import asyncio
import html
import json
import subprocess
from pathlib import Path
from typing import Any

from .baseline import approve_latest, baseline_rows
from .compare import compare_to_baseline
from .config import ProjectConfig
from .evals import evaluate_target
from .engine import logs, run_target, status_all
from .errors import LMakeError
from .hashing import file_hash
from .publish import markdown_to_html, publish_run


def safe_context_path(root: Path, relpath: str) -> Path:
    path = (root / relpath).resolve()
    context_root = (root / "context").resolve()
    try:
        path.relative_to(context_root)
    except ValueError as exc:
        raise ValueError("path must be inside context/") from exc
    if path.is_dir():
        raise ValueError("path must be a file")
    return path


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def context_files(root: Path) -> list[dict[str, Any]]:
    context_root = root / "context"
    if not context_root.exists():
        return []
    files = []
    for path in sorted(p for p in context_root.rglob("*") if p.is_file()):
        files.append({
            "path": rel(root, path),
            "name": path.name,
            "sha256": file_hash(path),
            "bytes": path.stat().st_size,
        })
    return files


def artifact_files(root: Path) -> list[dict[str, Any]]:
    artifacts_root = root / "artifacts"
    if not artifacts_root.exists():
        return []
    files = []
    for path in sorted(p for p in artifacts_root.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = ""
        files.append({
            "path": rel(root, path),
            "name": path.name,
            "sha256": file_hash(path),
            "bytes": path.stat().st_size,
            "text": text,
            "html": markdown_to_html(text) if text else "<p>Binary artifact.</p>",
        })
    return files


def git_commit_context(root: Path, message: str) -> dict[str, Any]:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)

    inside = run(["git", "rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"committed": False, "reason": "not a git repository"}
    run(["git", "add", "context"])
    diff = run(["git", "diff", "--cached", "--quiet", "--", "context"])
    if diff.returncode == 0:
        return {"committed": False, "reason": "no context changes"}
    commit = run(["git", "commit", "-m", message])
    return {"committed": commit.returncode == 0, "stdout": commit.stdout, "stderr": commit.stderr}


def project_snapshot(root: Path) -> dict[str, Any]:
    config = ProjectConfig.load(root)
    statuses = status_all(config)
    return {
        "root": str(root),
        "default_group": config.default_group or "all",
        "groups": config.groups,
        "context_files": context_files(root),
        "artifacts": artifact_files(root),
        "baselines": baseline_rows(root),
        "statuses": [item.__dict__ for item in statuses],
        "runs": logs(config, limit=12),
        "targets": [
            {
                "name": name,
                "runner": target.runner,
                "needs": target.needs,
                "outputs": target.outputs,
                "provider": target.provider,
                "model": target.model,
            }
            for name, target in config.targets.items()
        ],
    }


def snapshot_signature(snapshot: dict[str, Any]) -> str:
    summary = {
        "context_files": [(item.get("path"), item.get("sha256")) for item in snapshot.get("context_files", [])],
        "artifacts": [(item.get("path"), item.get("sha256")) for item in snapshot.get("artifacts", [])],
        "baselines": [(item.get("target"), item.get("run_id"), item.get("set_at")) for item in snapshot.get("baselines", [])],
        "statuses": [
            (
                item.get("target"),
                item.get("status"),
                item.get("latest_run_id"),
                item.get("current_fingerprint"),
                item.get("latest_fingerprint"),
            )
            for item in snapshot.get("statuses", [])
        ],
        "runs": [(item.get("run_id"), item.get("target"), item.get("cache_state")) for item in snapshot.get("runs", [])],
    }
    return json.dumps(summary, sort_keys=True)


def create_app(root: Path):
    try:
        from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse
    except ImportError as exc:  # pragma: no cover - exercised by CLI message
        raise LMakeError("lmake serve requires `python -m pip install -e '.[web]'`.") from exc

    root = root.resolve()
    app = FastAPI(title="lmake")

    def body_mapping(payload: Any, name: str) -> dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail=f"{name} must be a JSON object.")
        return payload

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return APP_HTML

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return project_snapshot(root)

    @app.websocket("/api/events")
    async def api_events(websocket: WebSocket) -> None:
        await websocket.accept()
        last_signature = None
        try:
            while True:
                try:
                    snapshot = project_snapshot(root)
                    signature = snapshot_signature(snapshot)
                    if signature != last_signature:
                        await websocket.send_json({"type": "state", "state": snapshot})
                        last_signature = signature
                except Exception as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            return

    @app.get("/api/context/{relpath:path}")
    def api_get_context(relpath: str) -> dict[str, Any]:
        try:
            path = safe_context_path(root, f"context/{relpath}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.exists():
            raise HTTPException(status_code=404, detail="file not found")
        return {
            "path": rel(root, path),
            "sha256": file_hash(path),
            "content": path.read_text(encoding="utf-8"),
        }

    @app.put("/api/context/{relpath:path}")
    def api_save_context(relpath: str, payload: Any = Body(...)) -> dict[str, Any]:
        request = body_mapping(payload, "save request")
        content = request.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=422, detail="save request.content must be a string.")
        base_sha256 = request.get("base_sha256")
        if base_sha256 is not None and not isinstance(base_sha256, str):
            raise HTTPException(status_code=422, detail="save request.base_sha256 must be a string or null.")
        try:
            path = safe_context_path(root, f"context/{relpath}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        current_sha = file_hash(path) if path.exists() else None
        if base_sha256 and current_sha and base_sha256 != current_sha:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This document changed while you were editing.",
                    "theirs": path.read_text(encoding="utf-8"),
                    "theirs_sha256": current_sha,
                    "yours": content,
                },
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": rel(root, path), "sha256": file_hash(path)}

    @app.post("/api/run")
    def api_run(payload: Any = Body(default=None)) -> dict[str, Any]:
        request = body_mapping(payload, "run request")
        selection = request.get("selection")
        if selection is not None and not isinstance(selection, str):
            raise HTTPException(status_code=422, detail="run request.selection must be a string or null.")
        force = request.get("force", False)
        if not isinstance(force, bool):
            raise HTTPException(status_code=422, detail="run request.force must be a boolean.")
        commit = git_commit_context(root, "Update context via lmake serve")
        config = ProjectConfig.load(root)
        results = run_target(config, selection, force=force)
        return {"commit": commit, "results": [item.__dict__ for item in results], "state": project_snapshot(root)}

    @app.post("/api/publish")
    def api_publish() -> dict[str, Any]:
        config = ProjectConfig.load(root)
        output_dir = publish_run(config, latest=True)
        return {"path": str(output_dir), "index": str(output_dir / "index.html")}

    @app.get("/api/eval/{target_name}")
    def api_eval(target_name: str) -> dict[str, Any]:
        try:
            config = ProjectConfig.load(root)
            result = evaluate_target(config, target_name)
        except LMakeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result is None:
            return {"target": target_name, "found": False, "passed": 0, "failed": 0, "rows": []}
        return {
            "target": result.target,
            "run_id": result.run_id,
            "suite_path": result.suite_path,
            "found": True,
            "passed": result.passed,
            "failed": result.failed,
            "rows": result.rows(),
        }

    @app.get("/api/compare/{target_name}")
    def api_compare(target_name: str) -> dict[str, Any]:
        try:
            config = ProjectConfig.load(root)
            text = compare_to_baseline(config, target_name)
        except LMakeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"target": target_name, "text": text}

    @app.post("/api/approve/{target_name}")
    def api_approve(target_name: str, payload: Any = Body(default=None)) -> dict[str, Any]:
        request = body_mapping(payload, "approve request")
        skip_evals = request.get("skip_evals", False)
        if not isinstance(skip_evals, bool):
            raise HTTPException(status_code=422, detail="approve request.skip_evals must be a boolean.")
        try:
            config = ProjectConfig.load(root)
            record, eval_summary = approve_latest(config, target_name, require_evals=not skip_evals)
        except LMakeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"record": record, "eval_summary": eval_summary, "state": project_snapshot(root)}

    return app


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>lmake</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; color: #202124; background: #f6f6f2; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    button, textarea, select { font: inherit; }
    button { border: 1px solid #b9b8ae; background: #fff; color: #202124; border-radius: 6px; min-height: 32px; padding: 6px 10px; cursor: pointer; }
    button.primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .shell { display: grid; grid-template-columns: 260px minmax(320px, 1fr) 380px; min-height: 100vh; }
    aside, main, section.panel { min-width: 0; }
    aside { border-right: 1px solid #ddd9ce; background: #fff; padding: 14px; }
    main { padding: 14px; }
    section.panel { border-left: 1px solid #ddd9ce; background: #fff; padding: 14px; overflow: auto; }
    h1, h2, h3 { letter-spacing: 0; margin: 0; }
    h1 { font-size: 18px; }
    h2 { font-size: 15px; margin: 18px 0 8px; }
    h3 { font-size: 14px; margin: 14px 0 6px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
    .status { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .pill { display: inline-flex; align-items: center; min-height: 24px; padding: 2px 8px; border-radius: 999px; border: 1px solid #ddd9ce; background: #fff; color: #555; }
    .pill.needs { border-color: #d89b31; color: #7a4d00; background: #fff7e2; }
    .pill.good { border-color: #81b38a; color: #1d6a2b; background: #edf8ef; }
    .file-list { display: grid; gap: 4px; }
    .file { text-align: left; width: 100%; border-color: transparent; background: transparent; justify-content: flex-start; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .file.active { background: #eef3ff; border-color: #bcd0ff; }
    .editor { display: grid; grid-template-rows: auto minmax(300px, 1fr); gap: 10px; height: calc(100vh - 28px); }
    textarea { width: 100%; height: 100%; min-height: 360px; resize: none; border: 1px solid #cfcabd; border-radius: 8px; padding: 14px; background: #fff; color: #202124; line-height: 1.5; }
    .artifact { border: 1px solid #ddd9ce; border-radius: 8px; padding: 12px; margin-bottom: 10px; background: #fff; }
    .artifact-tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
    .artifact-tab { background: #fff; }
    .artifact-tab.active { background: #eef3ff; border-color: #bcd0ff; }
    .rendered { overflow: auto; }
    .rendered pre { white-space: pre-wrap; background: #f2f2ee; padding: 10px; border-radius: 6px; }
    .field { display: grid; gap: 4px; margin-bottom: 8px; }
    select { width: 100%; border: 1px solid #b9b8ae; background: #fff; color: #202124; border-radius: 6px; min-height: 32px; padding: 4px 8px; }
    .button-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
    .review-output { border-top: 1px solid #ddd9ce; padding-top: 10px; margin-top: 8px; }
    .review-output pre { white-space: pre-wrap; overflow: auto; overflow-wrap: anywhere; word-break: break-word; max-height: 320px; background: #f2f2ee; padding: 10px; border-radius: 6px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border-bottom: 1px solid #e5e0d6; padding: 6px 4px; text-align: left; vertical-align: top; }
    th { font-weight: 600; color: #4c4f49; }
    .pass { color: #1d6a2b; }
    .fail { color: #9b1c1c; }
    .muted { color: #62655f; }
    .developer { display: none; }
    body.dev .developer { display: block; }
    .conflict { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.35); padding: 28px; }
    .conflict.open { display: grid; place-items: center; }
    .conflict-box { width: min(980px, 100%); max-height: 90vh; overflow: auto; background: #fff; border-radius: 8px; padding: 18px; }
    .conflict-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .conflict pre { white-space: pre-wrap; background: #f2f2ee; padding: 12px; border-radius: 6px; max-height: 360px; overflow: auto; }
    @media (max-width: 920px) {
      .shell { grid-template-columns: 1fr; }
      aside, section.panel { border: 0; border-bottom: 1px solid #ddd9ce; }
      .editor { height: auto; }
      .conflict-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="topbar">
        <h1>lmake</h1>
        <label class="muted"><input id="devToggle" type="checkbox"> Details</label>
      </div>
      <div id="statusPill" class="pill">Loading</div>
      <h2>Documents</h2>
      <div id="files" class="file-list"></div>
      <h2>Update</h2>
      <button class="primary" id="runBtn">Update Outputs</button>
      <button id="publishBtn">Publish Link</button>
      <p id="message" class="muted"></p>
      <div class="developer">
        <h2>Targets</h2>
        <div id="targets"></div>
      </div>
    </aside>
    <main>
      <div class="editor">
        <div class="topbar">
          <div>
            <h2 id="docTitle">Select a document</h2>
            <div id="docSha" class="muted"></div>
          </div>
          <button id="saveBtn" disabled>Save</button>
        </div>
        <textarea id="editor" placeholder="Select a context document to edit."></textarea>
      </div>
    </main>
    <section class="panel">
      <h2>Outputs</h2>
      <div id="artifacts"></div>
      <div class="developer">
        <h2>Review</h2>
        <div class="field">
          <label class="muted" for="reviewTarget">Target</label>
          <select id="reviewTarget"></select>
        </div>
        <div class="button-row">
          <button id="evalBtn">Eval</button>
          <button id="compareBtn">Compare</button>
          <button class="primary" id="approveBtn">Approve</button>
        </div>
        <div id="reviewOutput" class="review-output muted"></div>
        <h2>Run History</h2>
        <div id="runs"></div>
      </div>
    </section>
  </div>
  <div id="conflict" class="conflict">
    <div class="conflict-box">
      <h2>This document changed while you were editing</h2>
      <p class="muted">Choose which version to keep.</p>
      <div class="conflict-grid">
        <div><h3>Their version</h3><pre id="theirs"></pre></div>
        <div><h3>Your version</h3><pre id="yours"></pre></div>
      </div>
      <button id="keepTheirs">Use Theirs</button>
      <button class="primary" id="keepYours">Keep Yours</button>
    </div>
  </div>
  <script>
    let state = null;
    let activePath = null;
    let activeSha = null;
    let activeArtifactPath = null;
    let reviewTarget = null;
    let reviewExtraHtml = '';
    let eventsSocket = null;
    let conflict = null;

    const $ = id => document.getElementById(id);
    const escapeHtml = text => String(text || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const renderMd = text => '<pre>' + escapeHtml(text) + '</pre>';
    async function errorMessage(res) {
      const text = await res.text();
      try {
        const data = JSON.parse(text);
        if (typeof data.detail === 'string') return data.detail;
        if (Array.isArray(data.detail)) return data.detail.map(item => item.msg || JSON.stringify(item)).join('; ');
        if (data.detail && data.detail.message) return data.detail.message;
      } catch (err) {}
      return text || res.statusText;
    }

    async function loadState() {
      const res = await fetch('/api/state');
      state = await res.json();
      renderState();
    }

    function connectEvents() {
      if (!('WebSocket' in window) || eventsSocket) return;
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(`${protocol}://${window.location.host}/api/events`);
      eventsSocket = socket;
      socket.onmessage = event => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'state') {
            state = data.state;
            renderState();
          } else if (data.type === 'error') {
            $('message').textContent = data.message;
          }
        } catch (err) {
          $('message').textContent = String(err);
        }
      };
      socket.onclose = () => {
        eventsSocket = null;
        setTimeout(connectEvents, 2000);
      };
    }

    function renderState() {
      const needs = state.statuses.some(s => ['missing','stale','policy-expired','outputs-missing','outputs-changed'].includes(s.status));
      $('statusPill').className = 'pill ' + (needs ? 'needs' : 'good');
      $('statusPill').textContent = needs ? 'Needs update' : 'Everything up to date';
      $('files').innerHTML = state.context_files.map(f => `<button class="file ${f.path === activePath ? 'active' : ''}" data-path="${escapeHtml(f.path)}">${escapeHtml(f.path.replace(/^context\//,''))}</button>`).join('');
      $('files').querySelectorAll('button').forEach(btn => btn.onclick = () => openFile(btn.dataset.path));
      const artifactPaths = state.artifacts.map(a => a.path);
      if (!activeArtifactPath || !artifactPaths.includes(activeArtifactPath)) activeArtifactPath = artifactPaths[0] || null;
      const activeArtifact = state.artifacts.find(a => a.path === activeArtifactPath);
      $('artifacts').innerHTML = state.artifacts.length ? `
        <div class="artifact-tabs">${state.artifacts.map(a => `<button class="artifact-tab ${a.path === activeArtifactPath ? 'active' : ''}" data-path="${escapeHtml(a.path)}">${escapeHtml(a.name)}</button>`).join('')}</div>
        <div class="artifact"><h3>${escapeHtml(activeArtifact.path)}</h3><div class="rendered">${activeArtifact.html || renderMd(activeArtifact.text || '(binary artifact)')}</div></div>
      ` : '<p class="muted">No outputs yet.</p>';
      $('artifacts').querySelectorAll('.artifact-tab').forEach(btn => btn.onclick = () => { activeArtifactPath = btn.dataset.path; renderState(); });
      $('runs').innerHTML = state.runs.map(r => `<p><strong>${escapeHtml(r.target)}</strong><br><span class="muted">${escapeHtml(r.created_at)} | ${escapeHtml(r.cache_state)} | ${escapeHtml(r.fingerprint)}</span></p>`).join('');
      $('targets').innerHTML = state.targets.map(t => `<p><strong>${escapeHtml(t.name)}</strong><br><span class="muted">${escapeHtml(t.runner)} | ${escapeHtml(t.model)}</span></p>`).join('');
      renderReviewControls();
    }

    function renderReviewControls() {
      if (!state.targets.length) return;
      const names = state.targets.map(t => t.name);
      if (!reviewTarget || !names.includes(reviewTarget)) reviewTarget = names[names.length - 1];
      $('reviewTarget').innerHTML = names.map(name => `<option value="${escapeHtml(name)}" ${name === reviewTarget ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('');
      $('reviewTarget').onchange = e => {
        reviewTarget = e.target.value;
        renderReviewStatus('');
      };
      renderReviewStatus();
    }

    function renderReviewStatus(extraHtml) {
      if (!reviewTarget) return;
      if (extraHtml !== undefined) reviewExtraHtml = extraHtml || '';
      const baseline = (state.baselines || []).find(item => item.target === reviewTarget);
      const status = state.statuses.find(item => item.target === reviewTarget);
      const baselineText = baseline ? `Baseline: ${baseline.run_id}` : 'Baseline: none';
      const statusText = status ? `Status: ${status.status}` : 'Status: unknown';
      $('reviewOutput').className = 'review-output';
      $('reviewOutput').innerHTML = `<p class="muted">${escapeHtml(statusText)}<br>${escapeHtml(baselineText)}</p>` + reviewExtraHtml;
    }

    function renderEvalResult(data) {
      if (!data.found) {
        renderReviewStatus('<p class="muted">No eval_cases suite found.</p>');
        return;
      }
      const rows = data.rows.map(row => `
        <tr>
          <td>${escapeHtml(row.case)}</td>
          <td class="${row.status === 'pass' ? 'pass' : 'fail'}">${escapeHtml(row.status)}</td>
          <td>${escapeHtml(row.reason)}</td>
        </tr>
      `).join('');
      renderReviewStatus(`
        <p><strong>${data.passed} passed, ${data.failed} failed</strong><br><span class="muted">${escapeHtml(data.run_id || '')}</span></p>
        <table><thead><tr><th>Case</th><th>Status</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table>
      `);
    }

    async function reviewRequest(path, options) {
      if (!reviewTarget) throw new Error('No target selected.');
      const res = await fetch(path + encodeURIComponent(reviewTarget), options || {});
      if (!res.ok) throw new Error(await errorMessage(res));
      return res.json();
    }

    async function openFile(path) {
      const rel = path.replace(/^context\//, '');
      const res = await fetch('/api/context/' + encodeURIComponent(rel).replaceAll('%2F','/'));
      const doc = await res.json();
      activePath = doc.path;
      activeSha = doc.sha256;
      $('docTitle').textContent = doc.path.replace(/^context\//, '');
      $('docSha').textContent = doc.sha256.slice(0, 12);
      $('editor').value = doc.content;
      $('saveBtn').disabled = false;
      renderState();
    }

    async function saveFile(forceSha) {
      if (!activePath) return;
      const rel = activePath.replace(/^context\//, '');
      const res = await fetch('/api/context/' + encodeURIComponent(rel).replaceAll('%2F','/'), {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content: $('editor').value, base_sha256: forceSha === false ? null : activeSha})
      });
      if (res.status === 409) {
        const data = await res.json();
        conflict = data.detail;
        $('theirs').textContent = conflict.theirs;
        $('yours').textContent = conflict.yours;
        $('conflict').classList.add('open');
        return;
      }
      if (!res.ok) throw new Error(await errorMessage(res));
      const data = await res.json();
      activeSha = data.sha256;
      $('docSha').textContent = activeSha.slice(0, 12);
      $('message').textContent = 'Saved.';
      await loadState();
    }

    $('saveBtn').onclick = () => saveFile();
    $('runBtn').onclick = async () => {
      $('runBtn').disabled = true;
      $('message').textContent = 'Updating outputs...';
      try {
        if (activePath) await saveFile();
        const res = await fetch('/api/run', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({selection: null})});
        if (!res.ok) throw new Error(await errorMessage(res));
        state = (await res.json()).state;
        $('message').textContent = 'Outputs updated.';
        renderState();
      } catch (err) {
        $('message').textContent = String(err);
      } finally {
        $('runBtn').disabled = false;
      }
    };
    $('publishBtn').onclick = async () => {
      const res = await fetch('/api/publish', {method: 'POST'});
      const data = await res.json();
      try {
        await navigator.clipboard.writeText(data.index);
        $('message').textContent = 'Published and copied path: ' + data.index;
      } catch (err) {
        $('message').textContent = 'Published to ' + data.index;
      }
    };
    $('evalBtn').onclick = async () => {
      try {
        renderReviewStatus('<p class="muted">Running evals...</p>');
        renderEvalResult(await reviewRequest('/api/eval/'));
      } catch (err) {
        renderReviewStatus('<pre>' + escapeHtml(String(err)) + '</pre>');
      }
    };
    $('compareBtn').onclick = async () => {
      try {
        renderReviewStatus('<p class="muted">Comparing...</p>');
        const data = await reviewRequest('/api/compare/');
        renderReviewStatus('<pre>' + escapeHtml(data.text || '') + '</pre>');
      } catch (err) {
        renderReviewStatus('<pre>' + escapeHtml(String(err)) + '</pre>');
      }
    };
    $('approveBtn').onclick = async () => {
      try {
        renderReviewStatus('<p class="muted">Approving...</p>');
        const data = await reviewRequest('/api/approve/', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({skip_evals: false})});
        state = data.state;
        const evals = data.eval_summary ? `${data.eval_summary.passed} pass/${data.eval_summary.failed} fail` : 'no eval suite';
        renderState();
        renderReviewStatus(`<p><strong>Approved</strong><br><span class="muted">${escapeHtml(data.record.run_id)} | ${escapeHtml(evals)}</span></p>`);
      } catch (err) {
        renderReviewStatus('<pre>' + escapeHtml(String(err)) + '</pre>');
      }
    };
    $('devToggle').onchange = e => document.body.classList.toggle('dev', e.target.checked);
    $('keepTheirs').onclick = async () => {
      $('conflict').classList.remove('open');
      $('editor').value = conflict.theirs;
      activeSha = conflict.theirs_sha256;
      await loadState();
    };
    $('keepYours').onclick = async () => {
      $('conflict').classList.remove('open');
      await saveFile(false);
    };

    loadState().then(connectEvents).catch(err => $('message').textContent = String(err));
  </script>
</body>
</html>
"""

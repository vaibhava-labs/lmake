from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConfigError, RunNotFoundError
from .hashing import canonical_json, file_hash, sha256_bytes


INDEX_SCHEMA = "lmake.index.v0"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class ProjectState:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.cache_dir = self.root / ".lmcache"
        self.objects_dir = self.cache_dir / "objects" / "sha256"
        self.index_path = self.cache_dir / "index.json"
        self.runs_dir = self.root / "runs"
        self.artifacts_dir = self.root / "artifacts"

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"schema": INDEX_SCHEMA, "targets": {}, "runs": {}}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"schema": INDEX_SCHEMA, "targets": {}, "runs": {}}
        data.setdefault("schema", INDEX_SCHEMA)
        data.setdefault("targets", {})
        data.setdefault("runs", {})
        return data

    def save_index(self, data: dict[str, Any]) -> None:
        self.ensure_dirs()
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.index_path)

    def object_path(self, digest: str) -> Path:
        return self.objects_dir / digest[:2] / digest[2:4] / digest

    def put_object_bytes(self, data: bytes) -> str:
        digest = sha256_bytes(data)
        path = self.object_path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return digest

    def put_object_file(self, path: Path) -> str:
        data = path.read_bytes()
        return self.put_object_bytes(data)

    def restore_object(self, digest: str, dest: Path) -> None:
        src = self.object_path(digest)
        if not src.exists():
            raise RunNotFoundError(f"artifact object sha256:{digest} is missing from .lmcache")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    def make_run_id(self, target: str, fingerprint: str, mode: str) -> str:
        base = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{target}_{mode}_{fingerprint[:12]}"
        run_id = base
        i = 1
        while (self.runs_dir / run_id).exists():
            i += 1
            run_id = f"{base}_{i}"
        return run_id

    def run_index_record(self, manifest: dict[str, Any]) -> dict[str, Any]:
        run_id = manifest["run_id"]
        return {
            "target": manifest.get("target"),
            "created_at": manifest.get("created_at"),
            "mode": manifest.get("mode"),
            "cache_state": manifest.get("cache_state"),
            "fingerprint": manifest.get("target_fingerprint"),
            "path": str((Path("runs") / run_id / "manifest.json").as_posix()),
        }

    def mark_run_latest(self, manifest: dict[str, Any]) -> None:
        """Mark a fully-promoted run as latest for its target. Called after artifacts/ promotion."""
        run_id = manifest["run_id"]
        target = manifest.get("target")
        index = self.load_index()
        index["runs"][run_id] = self.run_index_record(manifest)
        if target:
            index["targets"][target] = {
                "latest_run_id": run_id,
                "latest_fingerprint": manifest.get("target_fingerprint"),
                "updated_at": manifest.get("created_at"),
            }
        self.save_index(index)

    def output_matches_manifest(self, item: dict[str, Any]) -> bool:
        out_path = str(item.get("path", ""))
        expected = str(item.get("sha256", ""))
        if not out_path or not expected:
            return False
        dest = self.root / out_path
        return dest.exists() and dest.is_file() and file_hash(dest) == expected

    def write_active_marker(self, staging_dir: Path) -> None:
        staging_dir.mkdir(parents=True, exist_ok=True)
        marker = {
            "pid": os.getpid(),
            "created_at": utc_now(),
        }
        (staging_dir / ".active").write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")

    def clear_active_marker(self, staging_dir: Path) -> None:
        active = staging_dir / ".active"
        if active.exists():
            active.unlink()

    def staging_is_active(self, staging_dir: Path) -> bool:
        active = staging_dir / ".active"
        if not active.exists():
            return False
        try:
            marker = json.loads(active.read_text(encoding="utf-8"))
            pid = int(marker.get("pid"))
        except Exception:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            active.unlink(missing_ok=True)
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    def recover_staged_runs(self) -> None:
        """Repair or clean staging dirs left by a crashed prior run.

        If manifest.json exists beside staging/, the run completed but promotion
        or index-update didn't finish; re-promote remaining files and update the
        index. If no manifest exists, the run crashed mid-execution; discard the
        staging dir entirely.
        """
        if not self.runs_dir.exists():
            return
        for staging_dir in sorted(self.runs_dir.glob("*/staging")):
            run_dir = staging_dir.parent
            if self.staging_is_active(staging_dir):
                continue
            manifest_path = staging_dir.parent / "manifest.json"
            if not manifest_path.exists():
                shutil.rmtree(staging_dir)
                tmp_manifest = run_dir / "manifest.json.tmp"
                if tmp_manifest.exists():
                    tmp_manifest.unlink()
                try:
                    run_dir.rmdir()
                except OSError:
                    pass
                continue

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ConfigError(f"could not recover staged run {run_dir.name}: could not read manifest: {exc}") from exc

            for item in manifest.get("outputs", []):
                name = str(item.get("name", ""))
                out_path = str(item.get("path", ""))
                digest = str(item.get("sha256", ""))
                if not name or not out_path or not digest:
                    raise ConfigError(f"could not recover staged run {run_dir.name}: malformed output record {item!r}")
                staged = staging_dir / name
                dest = self.root / out_path
                if staged.exists() and staged.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, dest)
                if not self.output_matches_manifest(item):
                    self.restore_object(digest, dest)
                if not self.output_matches_manifest(item):
                    raise ConfigError(f"could not recover staged run {run_dir.name}: output {out_path!r} does not match manifest sha256:{digest}")
            self.mark_run_latest(manifest)
            shutil.rmtree(staging_dir, ignore_errors=True)

    def write_manifest(self, manifest: dict[str, Any], update_latest: bool = True) -> None:
        self.ensure_dirs()
        run_id = manifest["run_id"]
        run_dir = self.runs_dir / run_id
        # exist_ok=True: staging dir pre-creates run_dir before write_manifest is called
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = run_dir / "manifest.json"
        tmp_manifest = run_dir / "manifest.json.tmp"
        tmp_manifest.write_text(canonical_json_pretty(manifest), encoding="utf-8")
        os.replace(tmp_manifest, manifest_path)
        index = self.load_index()
        index["runs"][run_id] = self.run_index_record(manifest)
        target = manifest.get("target")
        if update_latest and target:
            index["targets"][target] = {
                "latest_run_id": run_id,
                "latest_fingerprint": manifest.get("target_fingerprint"),
                "updated_at": manifest.get("created_at"),
            }
        self.save_index(index)

    def manifest_path(self, run_id_or_prefix: str) -> Path:
        exact = self.runs_dir / run_id_or_prefix / "manifest.json"
        if exact.exists():
            return exact
        candidates = sorted(self.runs_dir.glob(f"{run_id_or_prefix}*/manifest.json"))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            names = ", ".join(p.parent.name for p in candidates[:10])
            raise RunNotFoundError(f"ambiguous run id prefix {run_id_or_prefix!r}: {names}")
        raise RunNotFoundError(f"could not find run {run_id_or_prefix!r}")

    def load_manifest(self, run_id_or_prefix: str) -> dict[str, Any]:
        path = self.manifest_path(run_id_or_prefix)
        return json.loads(path.read_text(encoding="utf-8"))

    def all_manifests(self) -> list[dict[str, Any]]:
        if not self.runs_dir.exists():
            return []
        manifests: list[dict[str, Any]] = []
        for path in sorted(self.runs_dir.glob("*/manifest.json")):
            try:
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        manifests.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return manifests

    def latest_manifest_for_target(self, target: str) -> dict[str, Any] | None:
        index = self.load_index()
        latest = index.get("targets", {}).get(target, {}).get("latest_run_id")
        if latest:
            try:
                return self.load_manifest(latest)
            except RunNotFoundError:
                pass
        candidates = [m for m in self.all_manifests() if m.get("target") == target]
        candidates = [
            m for m in candidates
            if not (self.runs_dir / str(m.get("run_id")) / "staging").exists()
        ]
        return candidates[0] if candidates else None

    def git_info(self) -> dict[str, Any]:
        def run(args: list[str]) -> str | None:
            try:
                out = subprocess.check_output(args, cwd=self.root, stderr=subprocess.DEVNULL, text=True).strip()
                return out or None
            except Exception:
                return None

        inside = run(["git", "rev-parse", "--is-inside-work-tree"])
        if inside != "true":
            return {"inside_work_tree": False}
        commit = run(["git", "rev-parse", "HEAD"])
        branch = run(["git", "branch", "--show-current"])
        status = run(["git", "status", "--porcelain"])
        return {
            "inside_work_tree": True,
            "commit": commit,
            "branch": branch,
            "dirty": bool(status),
        }


def canonical_json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

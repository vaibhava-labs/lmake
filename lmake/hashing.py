from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def file_hash(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(value: Any) -> str:
    """Stable JSON representation for hashing manifests/spec fragments."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def tree_hash(files: Iterable[tuple[str, str]]) -> str:
    """Hash an ordered set of (relative path, sha256) pairs."""
    return hash_json([{"path": p, "sha256": h} for p, h in sorted(files)])

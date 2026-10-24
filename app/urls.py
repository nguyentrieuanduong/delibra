"""Canonical local browser URLs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

STATIC_ROOT = Path(__file__).resolve().parent / "static"

# path -> (mtime_ns, size, digest); re-hashed only when the file changes.
_static_digests: dict[Path, tuple[int, int, str]] = {}


def project_url(identifier: str, suffix: str = "") -> str:
    if suffix and not suffix.startswith(("/", "?")):
        raise ValueError("project URL suffix must start with '/' or '?'")
    return f"/projects/{quote(identifier, safe='')}{suffix}"


def static_url(name: str, root: Path = STATIC_ROOT) -> str:
    """Link a static asset by content, so an edit reaches the browser on reload.

    The server sends no Cache-Control, so a browser may keep reusing a stale
    copy of an unchanged URL; a new digest is a new URL.
    """

    base = root.resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"static asset {name!r} is outside the static root")
    stat = path.stat()
    cached = _static_digests.get(path)
    if cached is None or cached[:2] != (stat.st_mtime_ns, stat.st_size):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        cached = (stat.st_mtime_ns, stat.st_size, digest)
        _static_digests[path] = cached
    return f"/static/{quote(name)}?v={cached[2]}"

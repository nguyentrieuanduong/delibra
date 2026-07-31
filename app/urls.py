"""Canonical local browser URLs."""

from __future__ import annotations

from urllib.parse import quote


def project_url(identifier: str, suffix: str = "") -> str:
    if suffix and not suffix.startswith(("/", "?")):
        raise ValueError("project URL suffix must start with '/' or '?'")
    return f"/projects/{quote(identifier, safe='')}{suffix}"

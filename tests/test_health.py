from __future__ import annotations

from app.health import probe_provider


def test_missing_binary_is_visible_provider_warning_not_exception() -> None:
    health = probe_provider("claude", "/definitely/missing/claude", "2.1.202")
    assert health.available is False
    assert "not found" in (health.warning or "")


def test_version_drift_is_reported(tmp_path) -> None:
    executable = tmp_path / "provider"
    executable.write_text("#!/bin/sh\necho provider-version-2\n", encoding="utf-8")
    executable.chmod(0o700)
    health = probe_provider("codex", str(executable), "provider-version-1")
    assert health.available is True
    assert health.version == "provider-version-2"
    assert "differs" in (health.warning or "")

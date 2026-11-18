"""environment.yml and scripts/setup_env.sh: the pinned set and the tool checks (no env is built here)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "setup_env.sh"


def run(*args: str, path: str = "/usr/bin:/bin", cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env={"PATH": path, "HOME": str(cwd)}, cwd=cwd, timeout=30)


def test_the_conda_environment_installs_the_locked_pins() -> None:
    text = (REPO / "environment.yml").read_text(encoding="utf-8")
    assert "python=3.12" in text and "- pip" in text
    assert "-r requirements.lock" in text
    assert "OpenMP" in text  # why conda NumPy and pip PyTorch need care


def test_an_unknown_tool_is_refused(tmp_path) -> None:
    result = run("--tool", "poetry", str(tmp_path / "env"))
    assert result.returncode != 0
    assert "unknown tool 'poetry' (use conda, pip or uv)" in result.stderr
    assert not (tmp_path / "env").exists()


@pytest.mark.parametrize("tool, missing", [("uv", "uv"), ("conda", "conda"), ("pip", "python3.12")])
def test_a_missing_tool_fails_clearly_and_creates_nothing(tmp_path, tool, missing) -> None:
    empty = tmp_path / "bin"
    empty.mkdir()
    result = subprocess.run(["/bin/bash", str(SCRIPT), "--tool", tool, str(tmp_path / "env")],
                            capture_output=True, text=True, env={"PATH": str(empty)}, cwd=REPO, timeout=30)
    assert result.returncode != 0
    assert f"{missing} not found" in result.stderr
    assert not (tmp_path / "env").exists()

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


def fake_tools(tmp_path: Path) -> str:
    """conda, uv and python3.12 stand-ins that only record how they were called."""

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name in ("conda", "uv", "python3.12"):
        tool = bin_dir / name
        tool.write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp_path}/calls.txt"\n')
        tool.chmod(0o755)
    return f"{bin_dir}:/usr/bin:/bin"


@pytest.mark.parametrize("tool, marker", [("pip", "conda-meta"), ("uv", "conda-meta"), ("conda", "pyvenv.cfg")])
def test_an_env_of_another_kind_is_never_overwritten(tmp_path, tool, marker) -> None:
    env = tmp_path / "env"
    env.mkdir()
    (env / marker).mkdir() if marker == "conda-meta" else (env / marker).write_text("home = x\n")
    result = run("--tool", tool, str(env), path=fake_tools(tmp_path), cwd=tmp_path)
    assert result.returncode != 0
    assert f"{env} is not a {'conda' if tool == 'conda' else 'venv'} environment" in result.stderr
    assert not (tmp_path / "calls.txt").exists()


def test_a_non_empty_folder_that_is_no_env_is_refused(tmp_path) -> None:
    env = tmp_path / "env"
    env.mkdir()
    (env / "notes.txt").write_text("mine")
    result = run("--tool", "pip", str(env), path=fake_tools(tmp_path), cwd=tmp_path)
    assert result.returncode != 0 and "is not a venv environment" in result.stderr
    assert (env / "notes.txt").read_text() == "mine"


def test_the_default_env_path_is_the_repos_envs_folder(tmp_path) -> None:
    # Run from elsewhere: the default must still mean <repo>/envs, never <cwd>/envs.
    result = run("--tool", "uv", path=fake_tools(tmp_path), cwd=tmp_path)
    expected = str(REPO / "envs")
    if (REPO / "envs" / "conda-meta").is_dir():
        assert f"{expected} is not a venv environment" in result.stderr
    else:
        assert f"uv venv {expected}" in (tmp_path / "calls.txt").read_text()
    assert not (tmp_path / "envs").exists()


def test_a_conda_update_only_adds_packages(tmp_path) -> None:
    env = tmp_path / "env"
    (env / "conda-meta").mkdir(parents=True)
    result = run("--tool", "conda", str(env), path=fake_tools(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.txt").read_text()
    assert f"conda env update -p {env} -f" in calls and "--prune" not in calls

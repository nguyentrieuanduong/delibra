from pathlib import Path

import pytest

from app.coding_env import developer_tools_dir, prepare_env, probe_env_python


def stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def make_env(target_arg: str, version: str) -> str:
    """Shell body that creates <target>/bin/python reporting `version`."""

    return (
        f'mkdir -p "{target_arg}/bin"\n'
        f"printf '#!/bin/sh\\necho Python {version}\\n' > \"{target_arg}/bin/python\"\n"
        f'chmod +x "{target_arg}/bin/python"\n'
    )


def make_broken_env(target_arg: str) -> str:
    """Shell body that creates <target>/bin/python failing `--version`."""

    return (
        f'mkdir -p "{target_arg}/bin"\n'
        f"printf '#!/bin/sh\\nexit 1\\n' > \"{target_arg}/bin/python\"\n"
        f'chmod +x "{target_arg}/bin/python"\n'
    )


def write_python(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def environ(bin_dir: Path) -> dict[str, str]:
    return {"PATH": f"{bin_dir}:/bin"}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.mark.asyncio
async def test_conda_preferred(tmp_path: Path, project: Path) -> None:
    bin_dir = tmp_path / "bin"
    # conda create -y -p <dir> python=3.12  ->  $4 is the target
    stub(bin_dir, "conda", make_env("$4", "3.12.9"))
    stub(bin_dir, "python3", f'touch "{tmp_path}/python3-ran"\nexit 1\n')

    state = await prepare_env(project, environ=environ(bin_dir))

    assert (state.status, state.tool, state.python) == ("ready", "conda", "3.12.9")
    assert state.created_by_delibra is True
    assert not (tmp_path / "python3-ran").exists()


@pytest.mark.asyncio
async def test_venv_fallback_without_conda(tmp_path: Path, project: Path) -> None:
    bin_dir = tmp_path / "bin"
    # python3 -m venv <dir>  ->  $3 is the target
    stub(bin_dir, "python3", make_env("$3", "3.12.4"))

    state = await prepare_env(project, environ=environ(bin_dir))

    assert (state.status, state.tool, state.python) == ("ready", "venv", "3.12.4")
    assert state.created_by_delibra is True


@pytest.mark.asyncio
async def test_existing_env_adopted_untouched(tmp_path: Path, project: Path) -> None:
    python = project / "envs" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho Python 3.11.0\n")
    python.chmod(0o755)
    before = python.stat().st_mtime_ns
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "conda", f'touch "{tmp_path}/conda-ran"\nexit 1\n')

    state = await prepare_env(project, environ=environ(bin_dir))

    assert (state.status, state.tool, state.python) == ("ready", "existing", "3.11.0")
    assert state.created_by_delibra is False
    assert python.stat().st_mtime_ns == before
    assert not (tmp_path / "conda-ran").exists()


@pytest.mark.asyncio
async def test_dir_without_python_is_error_and_untouched(tmp_path: Path, project: Path) -> None:
    (project / "envs").mkdir()
    (project / "envs" / "keep.txt").write_text("user data\n")
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "conda", make_env("$4", "3.12.9"))

    state = await prepare_env(project, environ=environ(bin_dir))

    assert state.status == "error"
    assert state.created_by_delibra is False
    assert sorted(path.name for path in (project / "envs").iterdir()) == ["keep.txt"]


@pytest.mark.asyncio
async def test_envs_symlink_is_rejected(tmp_path: Path, project: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "python").write_text("#!/bin/sh\necho Python 3.12.0\n")
    (project / "envs").symlink_to(outside)

    state = await prepare_env(project, environ=environ(tmp_path / "bin"))

    assert state.status == "error"
    assert "symlink" in (state.error or "")
    assert sorted(path.name for path in outside.iterdir()) == ["bin"]


@pytest.mark.asyncio
async def test_bin_symlink_is_rejected(tmp_path: Path, project: Path) -> None:
    outside_bin = tmp_path / "outside-bin"
    outside_bin.mkdir()
    (outside_bin / "python").write_text("#!/bin/sh\necho Python 3.12.0\n")
    (project / "envs").mkdir()
    (project / "envs" / "bin").symlink_to(outside_bin)

    state = await prepare_env(project, environ=environ(tmp_path / "bin"))

    assert state.status == "error"
    assert "symlink" in (state.error or "")


@pytest.mark.asyncio
async def test_creation_failure_reports_stderr_tail(tmp_path: Path, project: Path) -> None:
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "conda", "echo boom >&2\nexit 1\n")

    state = await prepare_env(project, environ=environ(bin_dir))

    assert state.status == "error"
    assert "boom" in (state.error or "")
    assert state.created_by_delibra is False


@pytest.mark.asyncio
async def test_partial_creation_is_marked_as_delibras(tmp_path: Path, project: Path) -> None:
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "conda", 'mkdir -p "$4"\necho half >&2\nexit 1\n')

    state = await prepare_env(project, environ=environ(bin_dir))

    assert state.status == "error"
    assert state.created_by_delibra is True


@pytest.mark.asyncio
async def test_non_312_venv_records_actual_version(tmp_path: Path, project: Path) -> None:
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "python3", make_env("$3", "3.11.4"))

    state = await prepare_env(project, environ=environ(bin_dir))

    assert (state.status, state.tool, state.python) == ("ready", "venv", "3.11.4")


@pytest.mark.asyncio
async def test_nothing_found_is_error(tmp_path: Path, project: Path) -> None:
    (tmp_path / "bin").mkdir()

    state = await prepare_env(project, environ=environ(tmp_path / "bin"))

    assert state.status == "error"
    assert "neither conda nor python3" in (state.error or "")


def test_developer_tools_dir_reports_an_existing_directory(tmp_path: Path) -> None:
    tools = tmp_path / "CommandLineTools"
    tools.mkdir()
    stub(tmp_path / "bin", "xcode-select", f'echo "{tools}"\n')
    assert developer_tools_dir(environ(tmp_path / "bin")) == tools


def test_developer_tools_dir_is_none_without_xcode_select(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    assert developer_tools_dir(environ(tmp_path / "bin")) is None


def test_error_tail_is_bounded_in_bytes() -> None:
    from app.coding_env import STDERR_TAIL_BYTES, _error

    state = _error("é" * STDERR_TAIL_BYTES, created=False)
    assert len((state.error or "").encode("utf-8")) <= STDERR_TAIL_BYTES


@pytest.mark.asyncio
async def test_existing_python_that_fails_is_error_and_untouched(
    tmp_path: Path, project: Path
) -> None:
    python = project / "envs" / "bin" / "python"
    write_python(python, "exit 1\n")
    before = python.stat().st_mtime_ns

    state = await prepare_env(project, environ=environ(tmp_path / "bin"))

    assert state.status == "error"
    assert "--version" in (state.error or "")
    assert state.created_by_delibra is False
    assert python.stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_existing_python_with_malformed_version_is_error(
    tmp_path: Path, project: Path
) -> None:
    write_python(project / "envs" / "bin" / "python", "echo hello\n")

    state = await prepare_env(project, environ=environ(tmp_path / "bin"))

    assert state.status == "error"


@pytest.mark.asyncio
async def test_created_python_that_fails_is_error(tmp_path: Path, project: Path) -> None:
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "python3", make_broken_env("$3"))

    state = await prepare_env(project, environ=environ(bin_dir))

    assert state.status == "error"
    assert state.created_by_delibra is True


@pytest.mark.asyncio
async def test_probe_env_python_reads_a_working_env(tmp_path: Path, project: Path) -> None:
    write_python(project / "envs" / "bin" / "python", "echo Python 3.12.1\n")

    assert await probe_env_python(project, environ=environ(tmp_path / "bin")) == "3.12.1"


@pytest.mark.asyncio
async def test_probe_env_python_is_none_once_a_ready_env_breaks(
    tmp_path: Path, project: Path
) -> None:
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "python3", make_env("$3", "3.12.4"))
    assert (await prepare_env(project, environ=environ(bin_dir))).status == "ready"

    write_python(project / "envs" / "bin" / "python", "exit 1\n")

    assert await probe_env_python(project, environ=environ(bin_dir)) is None


@pytest.mark.asyncio
async def test_probe_env_python_is_none_without_python_or_through_a_symlink(
    tmp_path: Path, project: Path
) -> None:
    assert await probe_env_python(project, environ=environ(tmp_path / "bin")) is None
    elsewhere = tmp_path / "elsewhere"
    write_python(elsewhere / "bin" / "python", "echo Python 3.12.1\n")
    (project / "envs").symlink_to(elsewhere)

    assert await probe_env_python(project, environ=environ(tmp_path / "bin")) is None


@pytest.mark.asyncio
async def test_probe_env_python_times_out(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.coding_env.PROBE_TIMEOUT_SECONDS", 0.2)
    write_python(project / "envs" / "bin" / "python", "sleep 5\n")

    assert await probe_env_python(project, environ=environ(tmp_path / "bin")) is None

"""Create or adopt a project's ./envs outside any agent sandbox."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

from app.coding import CodingEnvState
from app.storage import utc_now

ENV_DIRNAME = "envs"
CREATE_TIMEOUT_SECONDS = 900
PROBE_TIMEOUT_SECONDS = 10
STDERR_TAIL_BYTES = 4_000
SERVER_ENV_KEYS = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "CONDA_EXE")
_PYTHON_VERSION = re.compile(r"Python (\d+\.\d+\.\d+\S*)")


def server_environment() -> dict[str, str]:
    """The server's own variables that env creation and env probes run with."""

    return {key: os.environ[key] for key in SERVER_ENV_KEYS if key in os.environ}


async def _run(
    argv: list[str], environ: dict[str, str], timeout: float = CREATE_TIMEOUT_SECONDS
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=environ,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.CancelledError:
        process.kill()
        await process.communicate()
        raise
    except TimeoutError:
        process.kill()
        await process.communicate()
        return 124, "", "environment creation timed out"
    return (
        process.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _python_version(
    python: Path, environ: dict[str, str], timeout: float = CREATE_TIMEOUT_SECONDS
) -> str | None:
    """`python --version`'s version number; None unless it ran and printed one."""

    code, out, err = await _run([str(python), "--version"], environ, timeout)
    match = _PYTHON_VERSION.fullmatch((out or err).strip())
    return match.group(1) if code == 0 and match else None


def _error(message: str, *, created: bool) -> CodingEnvState:
    return CodingEnvState(
        status="error",
        error=message.encode("utf-8")[-STDERR_TAIL_BYTES:].decode("utf-8", "ignore"),
        created_by_delibra=created,
        updated_at=utc_now(),
    )


async def prepare_env(project_root: Path, *, environ: dict[str, str]) -> CodingEnvState:
    target = project_root / ENV_DIRNAME
    python = target / "bin" / "python"
    if target.is_symlink() or (target / "bin").is_symlink():
        return _error("coding environment symlinks are not allowed", created=False)
    if python.exists():
        version = await _python_version(python, environ)
        if version is None:
            return _error(
                f"{python} does not answer --version; Delibra will not modify it",
                created=False,
            )
        return CodingEnvState(
            status="ready",
            tool="existing",
            python=version,
            updated_at=utc_now(),
        )
    if target.exists():
        return _error(
            f"{target} exists without bin/python; Delibra will not modify it",
            created=False,
        )
    search = environ.get("PATH")
    conda = environ.get("CONDA_EXE") or shutil.which("conda", path=search)
    if conda:
        tool = "conda"
        argv = [conda, "create", "-y", "-p", str(target), "python=3.12"]
    else:
        base = shutil.which("python3.12", path=search) or shutil.which("python3", path=search)
        if base is None:
            return _error("neither conda nor python3 was found on PATH", created=False)
        tool = "venv"
        argv = [base, "-m", "venv", str(target)]
    code, _out, err = await _run(argv, environ)
    created = target.exists()
    if code != 0 or not python.exists():
        return _error(err or f"{tool} exited with status {code}", created=created)
    version = await _python_version(python, environ)
    if version is None:
        return _error(f"{tool} created {python}, but it does not answer --version", created=True)
    return CodingEnvState(
        status="ready",
        tool=tool,
        python=version,
        created_by_delibra=True,
        updated_at=utc_now(),
    )


async def probe_env_python(project_root: Path, *, environ: dict[str, str]) -> str | None:
    """Re-read ./envs/bin/python's version when a build run starts; None if it fails.

    A saved "ready" only records what setup saw: a venv whose base Python
    was removed, or an env edited by hand, can stop working later.
    """

    target = project_root / ENV_DIRNAME
    python = target / "bin" / "python"
    if target.is_symlink() or (target / "bin").is_symlink() or not python.exists():
        return None
    return await _python_version(python, environ, PROBE_TIMEOUT_SECONDS)


def developer_tools_dir(environ: dict[str, str]) -> Path | None:
    """The active macOS developer directory, which /usr/bin/git shims into.

    None when xcode-select is absent (not macOS) or names no directory.
    """

    selector = shutil.which("xcode-select", path=environ.get("PATH"))
    if selector is None:
        return None
    try:
        completed = subprocess.run(
            [selector, "-p"],
            env=environ,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = Path(completed.stdout.strip())
    if completed.returncode != 0 or not path.is_absolute() or not path.is_dir():
        return None
    return path

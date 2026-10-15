"""Create or adopt a project's ./envs outside any agent sandbox."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from app.coding import CodingEnvState
from app.storage import utc_now

ENV_DIRNAME = "envs"
CREATE_TIMEOUT_SECONDS = 900
STDERR_TAIL_BYTES = 4_000


async def _run(argv: list[str], environ: dict[str, str]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=environ,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), CREATE_TIMEOUT_SECONDS)
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


async def _python_version(python: Path, environ: dict[str, str]) -> str | None:
    code, out, err = await _run([str(python), "--version"], environ)
    text = (out or err).strip()
    return text.removeprefix("Python ").strip() if code == 0 and text else None


def _error(message: str, *, created: bool) -> CodingEnvState:
    return CodingEnvState(
        status="error",
        error=message[-STDERR_TAIL_BYTES:],
        created_by_delibra=created,
        updated_at=utc_now(),
    )


async def prepare_env(project_root: Path, *, environ: dict[str, str]) -> CodingEnvState:
    target = project_root / ENV_DIRNAME
    python = target / "bin" / "python"
    if target.is_symlink() or (target / "bin").is_symlink():
        return _error("coding environment symlinks are not allowed", created=False)
    if python.exists():
        return CodingEnvState(
            status="ready",
            tool="existing",
            python=await _python_version(python, environ),
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
    return CodingEnvState(
        status="ready",
        tool=tool,
        python=await _python_version(python, environ),
        created_by_delibra=True,
        updated_at=utc_now(),
    )

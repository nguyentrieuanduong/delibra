"""Non-fatal, cancellable CLI availability and version probes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import signal


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    executable: str
    available: bool
    version: str | None
    warning: str | None
    checking: bool = False


def checking_health(commands: dict[str, str]) -> list[ProviderHealth]:
    return [
        ProviderHealth(
            name=name,
            executable=executable,
            available=False,
            version=None,
            warning=None,
            checking=True,
        )
        for name, executable in commands.items()
    ]


def _environment() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in ("PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM")
        if key in os.environ
    }


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def probe_provider(
    name: str,
    executable: str,
    expected_version: str,
    *,
    timeout: float = 5,
) -> ProviderHealth:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_environment(),
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        if process is not None:
            await _terminate(process)
        raise
    except TimeoutError:
        if process is not None:
            await _terminate(process)
        return ProviderHealth(
            name=name,
            executable=executable,
            available=False,
            version=None,
            warning=f"{name.title()} CLI version probe timed out",
        )
    except OSError:
        if process is not None:
            await _terminate(process)
        return ProviderHealth(
            name=name,
            executable=executable,
            available=False,
            version=None,
            warning=f"{name.title()} CLI not found: {executable}",
        )
    except Exception:
        if process is not None:
            await _terminate(process)
        return ProviderHealth(
            name=name,
            executable=executable,
            available=False,
            version=None,
            warning=f"{name.title()} CLI version probe failed",
        )

    output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    lines = output.strip().splitlines()
    version = next((line.strip() for line in reversed(lines) if line.strip()), None)
    if process.returncode != 0 or version is None:
        return ProviderHealth(
            name=name,
            executable=executable,
            available=False,
            version=version,
            warning=f"{name.title()} CLI version probe failed",
        )
    warning = None
    if expected_version not in version:
        warning = (
            f"{name.title()} CLI version {version} differs from verified "
            f"{expected_version}"
        )
    return ProviderHealth(name, executable, True, version, warning)


async def probe_all(commands: dict[str, str]) -> list[ProviderHealth]:
    expected = {"claude": "2.1.202", "codex": "0.144.5"}
    return list(
        await asyncio.gather(
            *(
                probe_provider(name, executable, expected.get(name, "unknown"))
                for name, executable in commands.items()
            )
        )
    )

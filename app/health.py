"""Non-fatal CLI availability and version probes."""

from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    executable: str
    available: bool
    version: str | None
    warning: str | None


def probe_provider(name: str, executable: str, expected_version: str) -> ProviderHealth:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM")
        if key in os.environ
    }
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ProviderHealth(
            name=name,
            executable=executable,
            available=False,
            version=None,
            warning=f"{name.title()} CLI not found: {executable}",
        )
    output = (result.stdout + "\n" + result.stderr).strip().splitlines()
    version = next((line.strip() for line in reversed(output) if line.strip()), None)
    if result.returncode != 0 or version is None:
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


def probe_all(commands: dict[str, str]) -> list[ProviderHealth]:
    expected = {"claude": "2.1.202", "codex": "0.144.5"}
    return [
        probe_provider(name, executable, expected[name])
        for name, executable in commands.items()
    ]

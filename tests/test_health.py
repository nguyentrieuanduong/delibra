from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.health import USAGE_VERIFIED_VERSIONS, probe_all, probe_build_version, probe_provider
from app.main import create_app


@pytest.mark.asyncio
async def test_missing_binary_is_visible_provider_warning_not_exception() -> None:
    health = await probe_provider(
        "claude", "/definitely/missing/claude", "2.1.202"
    )
    assert health.available is False
    assert "not found" in (health.warning or "")


@pytest.mark.asyncio
async def test_version_drift_is_reported(tmp_path: Path) -> None:
    executable = tmp_path / "provider"
    executable.write_text("#!/bin/sh\necho provider-version-2\n", encoding="utf-8")
    executable.chmod(0o700)
    health = await probe_provider("codex", str(executable), "provider-version-1")
    assert health.available is True
    assert health.version == "provider-version-2"
    assert "usage reporting was verified on provider-version-1" in (health.warning or "")


@pytest.mark.asyncio
async def test_the_installed_usage_verified_clis_show_no_warning(tmp_path: Path) -> None:
    reported = {"claude": "2.1.322 (Claude Code)", "codex": "codex-cli 0.169.0"}
    commands = {}
    for name, version in reported.items():
        executable = tmp_path / name
        executable.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
        executable.chmod(0o700)
        commands[name] = str(executable)

    health = await probe_all(commands)

    assert USAGE_VERIFIED_VERSIONS == {"claude": "2.1.322", "codex": "0.169.0"}
    assert [(item.name, item.warning) for item in health] == [
        ("claude", None),
        ("codex", None),
    ]


@pytest.mark.asyncio
async def test_hung_health_probe_never_blocks_startup_and_is_reaped_on_shutdown(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "health.pid"
    executable = tmp_path / "hung-provider"
    executable.write_text(
        "#!/bin/sh\n"
        f"echo $$ > '{pid_file}'\n"
        "exec /bin/sleep 300\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    app = create_app(
        settings_override=Settings(home=tmp_path / "home"),
        provider_commands={
            "claude": str(executable),
            "codex": "/definitely/missing/codex",
        },
    )
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    started = loop.time()
    pid: int | None = None
    try:
        async with app.router.lifespan_context(app):
            assert loop.time() - started < 0.5
            assert all(item.checking for item in app.state.health)
            # A poll, not a wait: it ends as soon as the probe has started. A
            # 1 s budget was too short for a fork and exec under parallel load;
            # the budget stays under the probe's own 5 s timeout, which would
            # reap it before shutdown and leave nothing for this test to check.
            for _ in range(450):
                text = pid_file.read_text(encoding="utf-8").strip() if pid_file.is_file() else ""
                if text:  # the shell may have created the file but not written it yet
                    pid = int(text)
                    break
                await asyncio.sleep(0.01)
            assert pid is not None
            # Still in flight: shutdown, not the probe's timeout, must reap it.
            assert all(item.checking for item in app.state.health)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not [
        context
        for context in unhandled
        if "never retrieved" in str(context.get("message", "")).lower()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("exit_code", "expected"), [(0, "codex-cli 0.162.1"), (1, None)])
async def test_build_version_requires_a_successful_probe(
    tmp_path: Path, exit_code: int, expected: str | None
) -> None:
    executable = tmp_path / "provider"
    executable.write_text(
        f"#!/bin/sh\necho codex-cli 0.162.1\nexit {exit_code}\n", encoding="utf-8"
    )
    executable.chmod(0o700)
    assert await probe_build_version("codex", str(executable)) == expected

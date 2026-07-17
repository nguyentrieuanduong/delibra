from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.health import probe_provider
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
    assert "differs" in (health.warning or "")


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
            for _ in range(100):
                if pid_file.is_file():
                    pid = int(pid_file.read_text(encoding="utf-8").strip())
                    break
                await asyncio.sleep(0.01)
            assert pid is not None
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

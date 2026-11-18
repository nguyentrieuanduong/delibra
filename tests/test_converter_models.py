from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.converter_models import (
    download_models, mark_interrupted, models_dir, models_state, ready_models_dir,
)


def fake_tools(tmp_path: Path, body: str) -> Path:
    tools = tmp_path / "bin" / "docling-tools"
    tools.parent.mkdir()
    tools.write_text("#!/bin/sh\n" + body)
    tools.chmod(0o755)
    return tools


@pytest.mark.asyncio
async def test_a_download_lands_in_place_and_is_ready(tmp_path) -> None:
    home = tmp_path / "home"
    tools = fake_tools(tmp_path, 'mkdir -p "$4/layout" && printf model > "$4/layout/weights.bin"\n')
    assert models_state(home).status == "absent"

    await download_models(home, tools=tools)

    state = models_state(home)
    assert (state.status, state.size_bytes) == ("ready", 5)
    assert ready_models_dir(home) == models_dir(home)
    assert (models_dir(home) / "layout" / "weights.bin").read_text() == "model"


@pytest.mark.asyncio
async def test_a_failed_download_keeps_nothing_half_written(tmp_path) -> None:
    home = tmp_path / "home"
    tools = fake_tools(tmp_path, 'mkdir -p "$4/x"; echo "no network" >&2; exit 1\n')

    await download_models(home, tools=tools)

    state = models_state(home)
    assert state.status == "failed" and "no network" in state.error
    assert ready_models_dir(home) is None
    assert not models_dir(home).exists()


def test_an_interrupted_download_is_marked_failed_at_startup(tmp_path) -> None:
    home = tmp_path / "home"
    (home / "models").mkdir(parents=True)
    (home / "models" / "docling-status.json").write_text('{"status": "downloading"}')

    mark_interrupted(home)

    assert (models_state(home).status, models_state(home).error) == ("failed", "interrupted")


@pytest.mark.asyncio
async def test_only_the_pdf_models_are_fetched_and_no_cache_lands_in_the_home(tmp_path) -> None:
    home = tmp_path / "home"
    tools = fake_tools(tmp_path, 'mkdir -p "$4" && printf "%s|%s" "$*" "$HF_HOME" > "$4/call.txt"\n')

    await download_models(home, tools=tools)

    args, hf_home = (models_dir(home) / "call.txt").read_text().split("|")
    assert args.split()[:3] == ["models", "download", "-o"]
    assert args.split()[4:] == ["layout", "tableformer"]
    assert Path(hf_home).is_relative_to(home / "models")
    assert not Path(hf_home).exists()


@pytest.mark.asyncio
async def test_a_cancelled_download_stops_the_tool(tmp_path) -> None:
    import asyncio

    home = tmp_path / "home"
    pid_file = tmp_path / "pid"
    tools = fake_tools(tmp_path, f'echo $$ > "{pid_file}"\nexec sleep 30\n')
    task = asyncio.create_task(download_models(home, tools=tools))
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)

"""One-off >60s run/reconnect acceptance gate; JSON stdout is the interface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.models import SessionConfig
from app.runner import RunManager
from app.storage import LockCoordinator, ProjectStore, RegistryStore, utc_now


FAKE_CLI = ROOT / "tests" / "fake_cli.py"


class LongAdapter:
    def __init__(self) -> None:
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        return Command(
            [
                sys.executable,
                str(FAKE_CLI),
                "--mode",
                "success",
                "--delay",
                "30.5",
            ],
            context.user_prompt,
        )

    def parse_line(self, line: str) -> list[AgentEvent]:
        payload = json.loads(line)
        kind = payload.get("kind")
        if kind == "init":
            return [AgentEvent("init", cli_session_id=payload.get("session_id"))]
        if kind == "delta":
            return [AgentEvent("text_delta", payload.get("text", ""))]
        if kind == "progress":
            return [AgentEvent("progress", "Long gate progress")]
        if kind == "result":
            self._final = payload.get("text", "")
            return [AgentEvent("result", self._final)]
        return []

    def final_text(self) -> str:
        return self._final


async def gate() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="delibra-long-sse-") as temporary:
        root = Path(temporary)
        registry = RegistryStore(root / "home")
        project_path = root / "project"
        project_path.mkdir()
        project = registry.register("Long SSE gate", project_path)
        store = ProjectStore(project)
        session = SessionConfig(
            id="a" * 32,
            name="Long run",
            agent="fake",
            model="success",
            effort="low",
            role_instructions="Test",
            cli_session_id=None,
            status="idle",
            created_at=utc_now(),
            rounds=[],
        )
        store.create_session(session)
        manager = RunManager(
            registry=registry,
            locks=LockCoordinator(),
            settings=Settings(home=root / "home", run_timeout=90),
            adapter_factory=lambda config: LongAdapter(),
        )
        started = time.monotonic()
        key = await manager.start(project.id, session.id, "Long question")
        first_stream = manager.subscribe(key)
        first = await anext(first_stream)
        assert first.kind == "text_delta"
        await first_stream.aclose()
        replay = [event async for event in manager.subscribe(key, first.event_id)]
        elapsed = time.monotonic() - started
        ids = [first.event_id, *(event.event_id for event in replay)]
        assert elapsed > 60
        assert ids == sorted(set(ids))
        assert all(event.event_id > first.event_id for event in replay)
        assert replay[-1].kind == "done"
        assert sum(event.kind == "done" for event in replay) == 1
        assert (store.rounds_dir(session.id) / "round-01.md").read_text() == "Hello"
        await manager.shutdown()
        return {
            "elapsed_seconds": round(elapsed, 3),
            "events_after_reconnect": len(replay),
            "no_duplicate_ids": True,
            "terminal_events": 1,
            "status": store.load_session(session.id).rounds[0].status,
        }


def main() -> None:
    sys.stdout.write(json.dumps(asyncio.run(gate()), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

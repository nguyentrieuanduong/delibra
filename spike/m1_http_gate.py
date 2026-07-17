"""Throwaway M1 real-CLI gate at Delibra's HTTP/SSE boundary.

This starts the real ASGI application under Uvicorn, submits one round to each
provider, disconnects after the first live event, reconnects with Last-Event-ID,
and verifies the durable rendered result. JSON on stdout is the CLI interface.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Iterator
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models import SessionConfig
from app.storage import ProjectStore, RegistryStore, utc_now


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def sse_events(response: httpx.Response) -> Iterator[dict[str, str | float]]:
    current: dict[str, str | float] = {}
    data: list[str] = []
    for line in response.iter_lines():
        if line.startswith(":"):
            continue
        if not line:
            if current:
                current["data"] = "\n".join(data)
                current["received_at"] = time.monotonic()
                yield current
            current = {}
            data = []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            data.append(value)
        else:
            current[field] = value


def seed(home: Path, project_path: Path) -> tuple[str, dict[str, SessionConfig]]:
    registry = RegistryStore(home)
    project = registry.register("M1 HTTP gate", project_path)
    store = ProjectStore(project)
    sessions: dict[str, SessionConfig] = {}
    for agent, model, effort in (
        ("claude", "sonnet", "low"),
        ("codex", "gpt-5.4", "low"),
    ):
        session = SessionConfig(
            id=uuid4().hex,
            name=f"M1 {agent}",
            agent=agent,
            model=model,
            effort=effort,
            role_instructions="Be concise and report only evidence you observed.",
            cli_session_id=None,
            status="idle",
            created_at=utc_now(),
            rounds=[],
        )
        store.create_session(session)
        (store.workspace_dir(session.id) / "gate.txt").write_text(
            f"{agent.upper()}_GATE_CANARY\n",
            encoding="utf-8",
        )
        sessions[agent] = session
    return project.id, sessions


def wait_until_ready(client: httpx.Client) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if client.get("/").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("Uvicorn did not become ready")


def exercise_provider(
    client: httpx.Client,
    home: Path,
    project_id: str,
    session: SessionConfig,
) -> dict[str, object]:
    base = f"/projects/{project_id}/sessions/{session.id}"
    started_at = time.monotonic()
    response = client.post(
        f"{base}/run",
        data={
            "prompt": (
                "Read gate.txt using an available provider tool, then briefly state "
                "the canary it contains."
            )
        },
    )
    assert response.status_code == 202, response.text
    assert 'id="live-1"' in response.text
    assert 'hx-trigger="sse:done once"' in response.text
    assert 'hx-target="#live-1"' in response.text
    assert 'hx-swap="outerHTML"' in response.text
    assert client.post(f"{base}/run", data={"prompt": "busy"}).status_code == 409

    stream_path = f"{base}/rounds/1/stream"
    before_disconnect: list[dict[str, str | float]] = []
    with client.stream("GET", stream_path) as stream:
        assert stream.status_code == 200
        for event in sse_events(stream):
            before_disconnect.append(event)
            if event.get("event") in {"text_delta", "progress"}:
                break
    assert before_disconnect
    assert before_disconnect[-1].get("event") in {"text_delta", "progress"}
    last_id = int(str(before_disconnect[-1]["id"]))
    assert float(before_disconnect[-1]["received_at"]) > started_at

    after_reconnect: list[dict[str, str | float]] = []
    with client.stream(
        "GET",
        stream_path,
        headers={"Last-Event-ID": str(last_id)},
    ) as stream:
        assert stream.status_code == 200
        after_reconnect.extend(sse_events(stream))
    assert after_reconnect[-1].get("event") == "done"
    replay_ids = [int(str(event["id"])) for event in after_reconnect]
    assert all(event_id > last_id for event_id in replay_ids)
    all_ids = [int(str(event["id"])) for event in before_disconnect] + replay_ids
    assert all_ids == sorted(set(all_ids))
    assert sum(event.get("event") == "done" for event in after_reconnect) == 1

    with client.stream("GET", stream_path) as stream:
        late = list(sse_events(stream))
    assert [event.get("event") for event in late] == ["done"]

    fragment = client.get(f"{base}/rounds/1")
    assert fragment.status_code == 200
    registry = RegistryStore(home)
    store = ProjectStore(registry.get(project_id))
    record = store.load_session(session.id).rounds[0]
    output_path = store.rounds_dir(session.id) / "round-01.md"
    output = output_path.read_text(encoding="utf-8")
    assert record.status == "complete", record.error
    assert output.strip()
    assert output in fragment.text or "GATE_CANARY" in fragment.text
    return {
        "agent": session.agent,
        "precompletion_event": before_disconnect[-1]["event"],
        "precompletion_seconds": round(
            float(before_disconnect[-1]["received_at"]) - started_at,
            3,
        ),
        "events_after_reconnect": len(after_reconnect),
        "final_bytes": output_path.stat().st_size,
        "status": record.status,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="delibra-m1-") as temporary:
        root = Path(temporary)
        home = root / "home"
        project_path = root / "project"
        project_path.mkdir()
        project_id, sessions = seed(home, project_path)
        port = available_port()
        environment = os.environ.copy()
        environment["DELIBRA_HOME"] = str(home)
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=environment,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=httpx.Timeout(180, connect=5),
            ) as client:
                wait_until_ready(client)
                results = [
                    exercise_provider(client, home, project_id, sessions[agent])
                    for agent in ("claude", "codex")
                ]
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        assert server.returncode in {0, -15}
        sys.stdout.write(json.dumps(results, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

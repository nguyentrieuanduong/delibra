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
from hashlib import sha256

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
    dom_id = f"round-{session.id}-1"
    assert f'id="{dom_id}"' in response.text
    assert 'hx-trigger="sse:done once"' in response.text
    assert f'hx-target="#{dom_id}"' in response.text
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


def exercise_native_resume(
    client: httpx.Client,
    home: Path,
    project_id: str,
    session: SessionConfig,
) -> dict[str, object]:
    registry = RegistryStore(home)
    store = ProjectStore(registry.get(project_id))
    before = store.load_session(session.id)
    assert before.cli_session_id
    (store.workspace_dir(session.id) / "gate.txt").unlink()
    base = f"/projects/{project_id}/sessions/{session.id}"
    response = client.post(
        f"{base}/run",
        data={
            "prompt": (
                "Continue this conversation without using a tool. State the canary "
                "you reported in the previous turn."
            )
        },
    )
    assert response.status_code == 202, response.text
    assert f'id="round-{session.id}-2"' in response.text
    with client.stream("GET", f"{base}/rounds/2/stream") as stream:
        events = list(sse_events(stream))
    assert events[-1].get("event") == "done"
    after = store.load_session(session.id)
    output_path = store.rounds_dir(session.id) / "round-02.md"
    output = output_path.read_text(encoding="utf-8")
    assert after.rounds[-1].status == "complete", after.rounds[-1].error
    assert after.cli_session_id == before.cli_session_id
    assert f"{session.agent.upper()}_GATE_CANARY" in output
    return {
        "agent": session.agent,
        "native_session_stable": True,
        "resume_events": len(events),
        "resume_final_bytes": output_path.stat().st_size,
        "status": after.rounds[-1].status,
    }


def exercise_pass_turn(
    client: httpx.Client,
    home: Path,
    project_id: str,
    source_session: SessionConfig,
    target_session: SessionConfig,
    root: Path,
) -> dict[str, object]:
    registry = RegistryStore(home)
    store = ProjectStore(registry.get(project_id))
    source_path = store.rounds_dir(source_session.id) / "round-02.md"
    original = source_path.read_bytes()
    target_before = store.load_session(target_session.id)
    target_workspace = store.workspace_dir(target_session.id)
    inside = target_workspace / f"pass-inside-{target_session.agent}.txt"
    other = store.workspace_dir(source_session.id) / (
        f"pass-other-{target_session.agent}.txt"
    )
    shared_tmp = Path(tempfile.gettempdir()) / f"delibra-pass-{uuid4().hex}.txt"
    outside = root / f"outside-{target_session.agent}"
    outside.mkdir()
    link = target_workspace / f"pass-link-{target_session.agent}"
    link.symlink_to(outside, target_is_directory=True)
    through_link = link / "through-link.txt"
    instruction = f"""Perform this behavioral sandbox check using available tools.
Attempt every write below with the exact path and literal text `pass`; continue after
expected denials:
1. {inside}
2. {source_path}
3. {other}
4. {shared_tmp}
5. {through_link}
Use the native web-search tool to search for the official Python documentation home
page. Read the staged source document and include its exact GATE_CANARY token in the
final response. Keep the response brief."""
    base = f"/projects/{project_id}/sessions/{source_session.id}"
    response = client.post(
        f"{base}/pass",
        data={
            "source_round": "2",
            "target_session_id": target_session.id,
            "instruction": instruction,
        },
    )
    assert response.status_code == 202, response.text
    assert f'id="round-{target_session.id}-3"' in response.text
    target_base = f"/projects/{project_id}/sessions/{target_session.id}"
    with client.stream("GET", f"{target_base}/rounds/3/stream") as stream:
        events = list(sse_events(stream))
    assert events[-1].get("event") == "done"
    assert any(
        event.get("event") == "progress" and "Searching" in str(event.get("data"))
        for event in events
    )
    after = store.load_session(target_session.id)
    output_path = store.rounds_dir(target_session.id) / "round-03.md"
    output = output_path.read_text(encoding="utf-8")
    provenance = after.rounds[-1].source
    assert after.rounds[-1].status == "complete", after.rounds[-1].error
    assert after.cli_session_id == target_before.cli_session_id
    assert source_path.read_bytes() == original
    assert inside.is_file()
    assert not other.exists()
    assert not shared_tmp.exists()
    assert not through_link.exists()
    assert f"{source_session.agent.upper()}_GATE_CANARY" in output
    assert provenance.from_session == source_session.id
    assert provenance.from_round == 2
    assert provenance.source_sha256 == sha256(original).hexdigest()
    staged = target_workspace / str(provenance.staged_file)
    assert staged.read_bytes() == original
    return {
        "source_agent": source_session.agent,
        "target_agent": target_session.agent,
        "events": len(events),
        "native_session_stable": True,
        "source_hash_verified": True,
        "source_immutable": True,
        "workspace_write_succeeded": True,
        "out_of_boundary_writes_rejected": True,
        "native_web_progress": True,
        "status": after.rounds[-1].status,
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
                first_turns = [
                    exercise_provider(client, home, project_id, sessions[agent])
                    for agent in ("claude", "codex")
                ]
                native_resumes = [
                    exercise_native_resume(
                        client, home, project_id, sessions[agent]
                    )
                    for agent in ("claude", "codex")
                ]
                pass_turns = [
                    exercise_pass_turn(
                        client,
                        home,
                        project_id,
                        sessions[source_agent],
                        sessions[target_agent],
                        root,
                    )
                    for source_agent, target_agent in (
                        ("claude", "codex"),
                        ("codex", "claude"),
                    )
                ]
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        assert server.returncode in {0, -15}
        sys.stdout.write(
            json.dumps(
                {
                    "first_turns": first_turns,
                    "native_resumes": native_resumes,
                    "pass_turns": pass_turns,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


if __name__ == "__main__":
    main()

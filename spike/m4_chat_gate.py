"""Real-provider M4 gate through the project chat HTTP/SSE contracts.

The gate retains only durable app metadata and a sanitized JSON summary. Provider
transcripts remain in the disposable project and are deleted when the run exits.
Stdout is the CLI interface.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.main import create_app
from app.storage import ProjectStore, RegistryStore


CLAUDE_CANARY = "M4_CHAT_CLAUDE_CANARY_4827"


def sse_events(response) -> Iterator[dict[str, str]]:
    current: dict[str, str] = {}
    data: list[str] = []
    for line in response.iter_lines():
        if line.startswith(":"):
            continue
        if line == "":
            if current:
                current["data"] = "\n".join(data)
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


def finish_round(
    client: TestClient,
    project_id: str,
    session_id: str,
    number: int,
    *,
    require_activity: bool = True,
):
    url = f"/projects/{project_id}/sessions/{session_id}/rounds/{number}/stream"
    with client.stream("GET", url) as response:
        assert response.status_code == 200, response.text
        events = list(sse_events(response))
    assert events and events[-1].get("event") == "done", events
    if require_activity:
        assert any(
            event.get("event") in {"text_delta", "progress"}
            for event in events[:-1]
        ), events
    return events


def created_session_id(response) -> str:
    pushed = response.headers["HX-Push-Url"]
    return parse_qs(urlsplit(pushed).query)["agent"][0]


def main() -> None:
    claude = shutil.which("claude")
    codex = shutil.which("codex")
    if claude is None or codex is None:
        raise RuntimeError("both claude and codex executables are required")

    with tempfile.TemporaryDirectory(prefix="delibra-m4-chat-") as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        project_path = root / "project"
        project_path.mkdir()
        registry = RegistryStore(home)
        project = registry.register("M4 chat gate", project_path)
        store = ProjectStore(project)
        app = create_app(
            settings_override=Settings(home=home, run_timeout=180),
            provider_commands={"claude": claude, "codex": codex},
        )

        with TestClient(app, base_url="http://localhost") as client:
            empty = client.get(f"/projects/{project.id}/chat")
            assert empty.status_code == 200
            assert "No conversation yet." in empty.text

            created_a = client.post(
                f"/projects/{project.id}/sessions",
                headers={"HX-Request": "true"},
                data={
                    "name": "M4 Claude A",
                    "agent": "claude",
                    "model": "sonnet",
                    "effort": "low",
                    "role_instructions": "Be concise and follow the user request.",
                },
            )
            assert created_a.status_code == 200, created_a.text
            session_a = created_session_id(created_a)
            assert 'hx-swap-oob="outerHTML:#chat-composer"' in created_a.text

            first_a = client.post(
                f"/projects/{project.id}/chat/run",
                data={
                    "session_id": session_a,
                    "prompt": (
                        f"Remember this exact canary for the next turn: {CLAUDE_CANARY}. "
                        "Reply with only the canary. Do not use tools."
                    ),
                },
            )
            assert first_a.status_code == 202, first_a.text
            assert f'id="round-{session_a}-1"' in first_a.text
            a1_events = finish_round(client, project.id, session_a, 1)
            after_a1 = store.load_session(session_a)
            a_native_id = after_a1.cli_session_id
            assert a_native_id

            edited_a = client.post(
                f"/projects/{project.id}/sessions/{session_a}/edit?agent={session_a}",
                headers={"HX-Request": "true"},
                data={
                    "name": "M4 Claude A",
                    "model": "opus",
                    "effort": "medium",
                },
            )
            assert edited_a.status_code == 200, edited_a.text
            assert store.load_session(session_a).cli_session_id == a_native_id

            second_a = client.post(
                f"/projects/{project.id}/chat/run",
                data={
                    "session_id": session_a,
                    "prompt": "Reply with only the exact canary from the previous turn.",
                },
            )
            assert second_a.status_code == 202, second_a.text
            assert f'id="round-{session_a}-2"' in second_a.text
            a2_events = finish_round(client, project.id, session_a, 2)
            after_a2 = store.load_session(session_a)
            a2_output = (store.rounds_dir(session_a) / "round-02.md").read_text(
                encoding="utf-8"
            )
            assert after_a2.cli_session_id == a_native_id
            assert after_a2.rounds[-1].model == "opus"
            assert after_a2.rounds[-1].effort == "medium"
            assert CLAUDE_CANARY in a2_output

            created_b = client.post(
                f"/projects/{project.id}/sessions",
                headers={"HX-Request": "true"},
                data={
                    "name": "M4 Codex B",
                    "agent": "codex",
                    "model": "gpt-5.4",
                    "effort": "low",
                    "role_instructions": "Be concise and report only observed evidence.",
                },
            )
            assert created_b.status_code == 200, created_b.text
            session_b = created_session_id(created_b)
            assert f"agent={session_b}" in created_b.headers["HX-Push-Url"]
            assert '<section class="chat-timeline"' not in created_b.text

            first_b = client.post(
                f"/projects/{project.id}/chat/run",
                data={
                    "session_id": session_b,
                    "prompt": "Reply with only CODEX-B-READY. Do not use tools.",
                },
            )
            assert first_b.status_code == 202, first_b.text
            b1_events = finish_round(
                client,
                project.id,
                session_b,
                1,
                require_activity=False,
            )
            b_native_id = store.load_session(session_b).cli_session_id
            assert b_native_id

            passed = client.post(
                f"/projects/{project.id}/sessions/{session_a}/pass?view=chat",
                data={
                    "source_round": "2",
                    "target_session_id": session_b,
                    "instruction": (
                        "Read the staged source document and reply with only the exact "
                        "M4_CHAT canary it contains."
                    ),
                },
            )
            assert passed.status_code == 202, passed.text
            a2_dom_id = f"round-{session_a}-2"
            b2_dom_id = f"round-{session_b}-2"
            assert f'id="{b2_dom_id}"' in passed.text
            assert f'hx-target="#{b2_dom_id}"' in passed.text

            concurrent = client.get(f"/projects/{project.id}/chat?agent={session_b}")
            assert concurrent.status_code == 200
            assert concurrent.text.count(f'id="{a2_dom_id}"') == 1
            assert concurrent.text.count(f'id="{b2_dom_id}"') == 1
            assert "M4 Codex B · Round 2 · running" in concurrent.text

            b2_events = finish_round(client, project.id, session_b, 2)
            after_b2 = store.load_session(session_b)
            b2_output = (store.rounds_dir(session_b) / "round-02.md").read_text(
                encoding="utf-8"
            )
            assert after_b2.cli_session_id == b_native_id
            assert after_b2.rounds[-1].source.from_session == session_a
            assert CLAUDE_CANARY in b2_output

            final_chat = client.get(f"/projects/{project.id}/chat?agent={session_b}")
            assert final_chat.text.count(f'id="{a2_dom_id}"') == 1
            assert final_chat.text.count(f'id="{b2_dom_id}"') == 1
            assert "M4 Claude A · Round 2" in final_chat.text
            assert "M4 Codex B · Round 2" in final_chat.text

        summary = {
            "claude": {
                "first_stream_events": len(a1_events),
                "changed_config_stream_events": len(a2_events),
                "model": after_a2.rounds[-1].model,
                "effort": after_a2.rounds[-1].effort,
                "native_id_stable": after_a2.cli_session_id == a_native_id,
                "canary_recalled": CLAUDE_CANARY in a2_output,
            },
            "codex": {
                "first_stream_events": len(b1_events),
                "pass_stream_events": len(b2_events),
                "native_id_stable": after_b2.cli_session_id == b_native_id,
                "staged_source_read": CLAUDE_CANARY in b2_output,
            },
            "chat": {
                "agent_added_without_shell_replacement": True,
                "same_round_number_scoped_fragments": True,
                "source_session": session_a,
                "target_session": session_b,
                "shared_round_number": 2,
            },
        }
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

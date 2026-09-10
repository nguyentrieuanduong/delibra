from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import NoReturn

from fastapi import Form, Request
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.models import SessionConfig
from app.runner import RunManager
from app.security import validate_field
from app.storage import LockCoordinator, ProjectStore, RegistryStore, StorageError


def app_client(tmp_path, *, body_limit: int = 2 * 1024 * 1024) -> TestClient:
    settings = replace(Settings(home=tmp_path / "home"), request_body_limit=body_limit)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )

    @app.post("/__test/echo")
    async def echo(request: Request):
        body = b"".join([chunk async for chunk in request.stream()])
        return {"size": len(body)}

    @app.post("/__test/field")
    async def field(value: str = Form(...)):
        return {"value": validate_field(value, "value", maximum=5)}

    return TestClient(app, base_url="http://localhost")


def test_bad_host_is_rejected(tmp_path) -> None:
    with app_client(tmp_path) as client:
        response = client.get("/", headers={"host": "attacker.example"})
    assert response.status_code == 403


def test_cross_site_origin_post_rejected_but_absent_origin_allowed(tmp_path) -> None:
    with app_client(tmp_path) as client:
        rejected = client.post(
            "/__test/echo",
            content=b"ok",
            headers={"origin": "https://attacker.example"},
        )
        other_local_port = client.post(
            "/__test/echo",
            content=b"ok",
            headers={"origin": "http://localhost:9000"},
        )
        other_loopback_alias = client.post(
            "/__test/echo",
            content=b"ok",
            headers={"origin": "http://127.0.0.1"},
        )
        same_origin = client.post(
            "/__test/echo",
            content=b"ok",
            headers={"origin": "http://localhost"},
        )
        allowed = client.post("/__test/echo", content=b"ok")
    assert rejected.status_code == 403
    assert other_local_port.status_code == 403
    assert other_loopback_alias.status_code == 403
    assert same_origin.status_code == 200
    assert allowed.status_code == 200
    assert allowed.json() == {"size": 2}


def test_malformed_loopback_host_is_rejected(tmp_path) -> None:
    with app_client(tmp_path) as client:
        response = client.get("/", headers={"host": "[::1]attacker"})
    assert response.status_code == 403


def test_chunked_oversized_body_is_rejected_while_streaming(tmp_path) -> None:
    def chunks():
        yield b"12345678"
        yield b"abcdefgh"

    with app_client(tmp_path, body_limit=10) as client:
        response = client.post(
            "/__test/echo",
            content=chunks(),
            headers={"transfer-encoding": "chunked"},
        )
    assert response.status_code == 413


def test_oversized_field_returns_422(tmp_path) -> None:
    with app_client(tmp_path) as client:
        response = client.post("/__test/field", data={"value": "too-long"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "writable_roots",
    [
        ["../../../etc"],
        ["/etc"],
        [".delibra"],
        ["src,x"],
        [" src"],
        ["src\nother"],
        ["src\tother"],
        ["src\x7fother"],
        "src",
        [1],
    ],
)
@pytest.mark.asyncio
async def test_hostile_persisted_writable_roots_never_reach_an_adapter(
    tmp_path: Path,
    writable_roots: object,
) -> None:
    home = tmp_path / "home"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(home)
    project = registry.register("Security", project_dir)
    store = ProjectStore(project)
    config = SessionConfig(
        id="a" * 32,
        name="Security agent",
        agent="fake",
        model="success",
        effort="low",
        role_instructions="Test role",
        cli_session_id=None,
        status="idle",
        created_at="2026-09-10T00:00:00Z",
        rounds=[],
    )
    store.create_session(config)
    config_path = store.session_dir(config.id) / "config.json"
    persisted = config.to_dict()
    persisted["writable_roots"] = writable_roots
    config_path.write_text(json.dumps(persisted), encoding="utf-8")
    adapter_calls = 0

    def adapter_factory(_config: SessionConfig) -> NoReturn:
        nonlocal adapter_calls
        adapter_calls += 1
        raise AssertionError("hostile writable roots reached the adapter")

    manager = RunManager(
        registry=registry,
        locks=LockCoordinator(),
        settings=Settings(home=home, run_timeout=2),
        adapter_factory=adapter_factory,
        codex_executable="/missing/codex",
    )

    with pytest.raises(StorageError):
        await manager.start(project.id, config.id, "Question")

    assert adapter_calls == 0


@pytest.mark.asyncio
async def test_hostile_persisted_writable_roots_with_unsafe_ancestor_stop_before_adapter(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project_dir = tmp_path / "parent,unsafe" / "project"
    (project_dir / "src").mkdir(parents=True)
    registry = RegistryStore(home)
    project = registry.register("Security", project_dir)
    store = ProjectStore(project)
    config = SessionConfig(
        id="a" * 32,
        name="Security agent",
        agent="fake",
        model="success",
        effort="low",
        role_instructions="Test role",
        cli_session_id=None,
        status="idle",
        created_at="2026-09-10T00:00:00Z",
        rounds=[],
        writable_roots=["src"],
    )
    store.create_session(config)
    adapter_calls = 0

    def adapter_factory(_config: SessionConfig) -> NoReturn:
        nonlocal adapter_calls
        adapter_calls += 1
        raise AssertionError("unsafe writable root reached the adapter")

    manager = RunManager(
        registry=registry,
        locks=LockCoordinator(),
        settings=Settings(home=home, run_timeout=2),
        adapter_factory=adapter_factory,
        codex_executable="/missing/codex",
    )

    with pytest.raises(StorageError, match="src"):
        await manager.start(project.id, config.id, "Question")

    assert adapter_calls == 0

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


def app_client(
    tmp_path,
    *,
    body_limit: int = 2 * 1024 * 1024,
    allowed_hosts: tuple[str, ...] = (),
    allowed_clients: tuple[str, ...] = (),
    peer: tuple[str, int] | None = None,
) -> TestClient:
    settings = replace(
        Settings(home=tmp_path / "home"),
        request_body_limit=body_limit,
        allowed_hosts=allowed_hosts,
        allowed_clients=allowed_clients,
    )
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

    if peer is None:
        return TestClient(app, base_url="http://localhost")
    return TestClient(app, base_url="http://localhost", client=peer)


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


def test_an_allowed_host_is_accepted_while_every_other_host_stays_rejected(
    tmp_path,
) -> None:
    with app_client(tmp_path, allowed_hosts=("192.168.1.50:8000",)) as client:
        allowed = client.get("/", headers={"host": "192.168.1.50:8000"})
        # Same address, different port: the entry named a port, so it binds one.
        other_port = client.get("/", headers={"host": "192.168.1.50:9000"})
        # The rebinding case the allowlist exists to keep closed.
        rebound = client.get("/", headers={"host": "evil.example"})
        loopback = client.get("/", headers={"host": "127.0.0.1"})
    assert allowed.status_code == 200
    assert other_port.status_code == 403
    assert rebound.status_code == 403
    assert loopback.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8000",
        "127.0.0.1:8000",
        "127.42.19.7:9000",
        "[::1]:8000",
    ],
)
def test_every_loopback_host_is_always_accepted(tmp_path, host: str) -> None:
    with app_client(tmp_path, allowed_hosts=("192.168.1.50",)) as client:
        response = client.get("/", headers={"host": host})

    assert response.status_code == 200


def test_a_portless_allowed_host_matches_any_port_and_ignores_case(tmp_path) -> None:
    with app_client(tmp_path, allowed_hosts=("My-Mac.local",)) as client:
        bare = client.get("/", headers={"host": "my-mac.local"})
        with_port = client.get("/", headers={"host": "MY-MAC.LOCAL:8000"})
        other_name = client.get("/", headers={"host": "other.local:8000"})
    assert bare.status_code == 200
    assert with_port.status_code == 200
    assert other_name.status_code == 403


def test_a_cidr_allowed_host_matches_any_address_in_range_on_any_port(
    tmp_path,
) -> None:
    with app_client(tmp_path, allowed_hosts=("192.168.1.0/24",)) as client:
        in_range = client.get("/", headers={"host": "192.168.1.50:8000"})
        # A CIDR entry names no port, so it binds none.
        other_port = client.get("/", headers={"host": "192.168.1.50:9999"})
        out_of_range = client.get("/", headers={"host": "192.168.2.50:8000"})
        # A name that merely resolves into the range is still not in the range:
        # the check reads the Host header, which never resolves anything.
        by_name = client.get("/", headers={"host": "my-mac.local:8000"})
    assert in_range.status_code == 200
    assert other_port.status_code == 200
    assert out_of_range.status_code == 403
    assert by_name.status_code == 403


def test_an_ipv6_cidr_allowed_host_matches_its_bracketed_literal(tmp_path) -> None:
    with app_client(tmp_path, allowed_hosts=("fd00::/8",)) as client:
        in_range = client.get("/", headers={"host": "[fd00::5]:8000"})
        out_of_range = client.get("/", headers={"host": "[fe80::5]:8000"})
        # Mixed families must compare false rather than raise.
        v4_host = client.get("/", headers={"host": "192.168.1.50:8000"})
    assert in_range.status_code == 200
    assert out_of_range.status_code == 403
    assert v4_host.status_code == 403


def test_an_unrestricted_client_list_accepts_every_peer(tmp_path) -> None:
    # The default must stay what it was before peer filtering existed: the bind
    # address is the access control, and this check is purely opt-in.
    with app_client(tmp_path, peer=("203.0.113.7", 5000)) as client:
        response = client.get("/", headers={"host": "127.0.0.1"})
    assert response.status_code == 200


def test_a_client_cidr_rejects_peers_outside_the_range(tmp_path) -> None:
    inside = app_client(
        tmp_path,
        allowed_clients=("192.168.1.0/24",),
        peer=("192.168.1.22", 5000),
    )
    outside = app_client(
        tmp_path,
        allowed_clients=("192.168.1.0/24",),
        peer=("203.0.113.7", 5000),
    )
    with inside as client:
        allowed = client.get("/", headers={"host": "127.0.0.1"})
    with outside as client:
        rejected = client.get("/", headers={"host": "127.0.0.1"})
    assert allowed.status_code == 200
    assert rejected.status_code == 403
    assert rejected.text == "Forbidden Client"


@pytest.mark.parametrize(
    "peer",
    [
        ("127.0.0.1", 5000),
        ("127.42.19.7", 5000),
        ("::1", 5000),
        ("::ffff:127.0.0.1", 5000),
    ],
)
def test_every_loopback_client_is_always_accepted(
    tmp_path,
    peer: tuple[str, int],
) -> None:
    with app_client(
        tmp_path,
        allowed_clients=("192.168.1.0/24",),
        peer=peer,
    ) as client:
        response = client.get("/", headers={"host": "localhost"})

    assert response.status_code == 200


def test_a_client_restriction_still_rejects_an_unknown_peer(tmp_path) -> None:
    client = app_client(tmp_path, allowed_clients=("192.168.1.0/24",))

    with client:
        response = client.get("/", headers={"host": "localhost"})

    assert response.status_code == 403
    assert response.text == "Forbidden Client"


def test_a_bare_client_address_and_an_ipv4_mapped_peer_both_match(tmp_path) -> None:
    bare = app_client(
        tmp_path,
        allowed_clients=("192.168.1.22",),
        peer=("192.168.1.22", 5000),
    )
    # A dual-stack listener reports v4 peers in v4-mapped v6 form.
    mapped = app_client(
        tmp_path,
        allowed_clients=("192.168.1.0/24",),
        peer=("::ffff:192.168.1.22", 5000),
    )
    neighbour = app_client(
        tmp_path,
        allowed_clients=("192.168.1.22",),
        peer=("192.168.1.23", 5000),
    )
    with bare as client:
        exact = client.get("/", headers={"host": "127.0.0.1"})
    with mapped as client:
        v4_mapped = client.get("/", headers={"host": "127.0.0.1"})
    with neighbour as client:
        rejected = client.get("/", headers={"host": "127.0.0.1"})
    assert exact.status_code == 200
    assert v4_mapped.status_code == 200
    assert rejected.status_code == 403


def test_the_client_wildcard_accepts_every_peer(tmp_path) -> None:
    with app_client(
        tmp_path,
        allowed_clients=("*",),
        peer=("203.0.113.7", 5000),
    ) as client:
        response = client.get("/", headers={"host": "127.0.0.1"})
    assert response.status_code == 200


def test_the_peer_check_runs_before_the_host_check(tmp_path) -> None:
    # An outside peer learns nothing about which Host values are configured.
    with app_client(
        tmp_path,
        allowed_hosts=("192.168.1.50:8000",),
        allowed_clients=("192.168.1.0/24",),
        peer=("203.0.113.7", 5000),
    ) as client:
        response = client.get("/", headers={"host": "192.168.1.50:8000"})
    assert response.status_code == 403
    assert response.text == "Forbidden Client"


def test_the_wildcard_entry_accepts_every_host(tmp_path) -> None:
    with app_client(tmp_path, allowed_hosts=("*",)) as client:
        anything = client.get("/", headers={"host": "anything.example:1234"})
        # Still a syntactically valid authority: "*" widens policy, not parsing.
        malformed = client.get("/", headers={"host": "[::1]attacker"})
    assert anything.status_code == 200
    assert malformed.status_code == 403


def test_an_allowed_host_still_rejects_a_cross_site_post(tmp_path) -> None:
    with app_client(tmp_path, allowed_hosts=("192.168.1.50:8000",)) as client:
        rejected = client.post(
            "/__test/echo",
            content=b"ok",
            headers={
                "host": "192.168.1.50:8000",
                "origin": "https://attacker.example",
            },
        )
        same_origin = client.post(
            "/__test/echo",
            content=b"ok",
            headers={
                "host": "192.168.1.50:8000",
                "origin": "http://192.168.1.50:8000",
            },
        )
    assert rejected.status_code == 403
    assert same_origin.status_code == 200


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

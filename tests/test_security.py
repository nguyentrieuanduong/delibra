from __future__ import annotations

from dataclasses import replace

from fastapi import Form, Request
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import validate_field


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
        allowed = client.post("/__test/echo", content=b"ok")
    assert rejected.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json() == {"size": 2}


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

"""Localhost request-boundary protections and form validation."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import HTTPException


Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class RequestBodyTooLarge(Exception):
    pass


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _loopback_authority(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if candidate.startswith("["):
        end = candidate.find("]")
        return end > 0 and candidate[1:end] == "::1"
    host = candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate
    return host.lower() in {"localhost", "127.0.0.1", "::1"}


def _same_site_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    )


async def _plain_response(send: Send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class LocalSecurityMiddleware:
    def __init__(self, app, *, body_limit: int) -> None:
        self.app = app
        self.body_limit = body_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        host = _header(scope, b"host")
        if host is None or not _loopback_authority(host):
            await _plain_response(send, HTTPStatus.FORBIDDEN, "Forbidden Host")
            return

        if scope.get("method", "GET").upper() not in {"GET", "HEAD", "OPTIONS"}:
            origin = _header(scope, b"origin")
            if origin is not None and not _same_site_origin(origin):
                await _plain_response(send, HTTPStatus.FORBIDDEN, "Forbidden Origin")
                return

        content_length = _header(scope, b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.body_limit:
                    await _plain_response(
                        send,
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        "Request body too large",
                    )
                    return
            except ValueError:
                await _plain_response(send, HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.body_limit:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if not response_started:
                await _plain_response(
                    send,
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "Request body too large",
                )


def validate_field(
    value: str,
    label: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not allow_empty and not value.strip():
        raise HTTPException(status_code=422, detail=f"{label} must not be empty")
    if len(value) > maximum:
        raise HTTPException(
            status_code=422,
            detail=f"{label} must be at most {maximum} characters",
        )
    if "\x00" in value:
        raise HTTPException(status_code=422, detail=f"{label} contains invalid characters")
    return value

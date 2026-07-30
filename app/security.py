"""Localhost request-boundary protections and form validation."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.pass_prompts import (
    PASS_PROMPT_TEMPLATE_MAX_CHARS,
    PassPromptTemplateError,
    validate_pass_prompt_template,
)
from app.storage import normalize_agent_name, sanitize_name


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


def _parse_authority(value: str) -> tuple[str, int | None] | None:
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    try:
        parsed = urlsplit(f"//{candidate}")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname.lower(), port


def _loopback_authority(value: str) -> bool:
    parsed = _parse_authority(value)
    return parsed is not None and parsed[0] in {"localhost", "127.0.0.1", "::1"}


def _same_origin(value: str, host: str, scheme: str) -> bool:
    try:
        parsed = urlsplit(value)
        origin_port = parsed.port
    except ValueError:
        return False
    host_parts = _parse_authority(host)
    if host_parts is None or parsed.hostname is None:
        return False
    if parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
        return False
    expected_port = host_parts[1] or (443 if scheme == "https" else 80)
    actual_port = origin_port or (443 if parsed.scheme == "https" else 80)
    return (
        parsed.scheme == scheme
        and parsed.hostname.lower() == host_parts[0]
        and actual_port == expected_port
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
            if origin is not None and not _same_origin(
                origin,
                host,
                str(scope.get("scheme", "http")),
            ):
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


def validate_name(value: str, label: str = "Name") -> str:
    validate_field(value, label, maximum=200)
    try:
        return sanitize_name(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def validate_agent_name(value: str) -> str:
    try:
        return normalize_agent_name(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def validate_pass_prompt_template_field(value: str) -> str:
    validate_field(
        value,
        "Pass prompt",
        maximum=PASS_PROMPT_TEMPLATE_MAX_CHARS,
    )
    try:
        return validate_pass_prompt_template(value)
    except PassPromptTemplateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

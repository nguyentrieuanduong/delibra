"""Localhost request-boundary protections and form validation."""

from __future__ import annotations

from http import HTTPStatus
import ipaddress
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


LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
# Widens which Host values are accepted; it never widens what parses as one.
WILDCARD_HOST = "*"


def _loopback_authority(value: str) -> bool:
    parsed = _parse_authority(value)
    return parsed is not None and parsed[0] in LOOPBACK_HOSTNAMES


Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_network(value: str) -> Network | None:
    """One CIDR range, or None for anything that is not written as one.

    Only a ``/`` makes an entry a range, so a bare address stays an authority
    that matches any port rather than becoming a silent /32.
    """

    if "/" not in value:
        return None
    try:
        # Non-strict: "my address slash prefix" is the common shorthand and
        # denotes exactly one range, so it is read rather than refused.
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def _address(value: str) -> Address | None:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    # A dual-stack listener reports IPv4 peers in v4-mapped form; compare them
    # as the IPv4 addresses an operator actually wrote down.
    return getattr(parsed, "ipv4_mapped", None) or parsed


def _within(address: Address | None, networks: tuple[Network, ...]) -> bool:
    return address is not None and any(
        address.version == network.version and address in network
        for network in networks
    )


def normalize_allowed_host(value: str) -> str:
    """Canonical form of one allowlist entry, raising ValueError on garbage.

    Entries are authorities or CIDR ranges, never URLs: ``192.168.1.50:8000``
    binds that one port, a bare ``my-mac.local`` matches any port,
    ``192.168.1.0/24`` matches any address in range on any port, and ``*``
    accepts anything.
    """

    candidate = value.strip()
    if candidate == WILDCARD_HOST:
        return WILDCARD_HOST
    network = parse_network(candidate)
    if network is not None:
        return str(network)
    parsed = _parse_authority(candidate)
    if parsed is None:
        raise ValueError(
            f"allowed host {value!r} must be a host or host:port authority"
        )
    hostname, port = parsed
    # urlsplit strips the brackets an IPv6 literal needs to round-trip.
    literal = f"[{hostname}]" if ":" in hostname else hostname
    return literal if port is None else f"{literal}:{port}"


def normalize_allowed_client(value: str) -> str:
    """Canonical form of one peer-address entry, raising ValueError on garbage.

    Unlike a Host entry this restricts who may connect at all, so every entry is
    an address or a range; a bare address is stored as the single-address
    network it denotes.
    """

    candidate = value.strip()
    if candidate == WILDCARD_HOST:
        return WILDCARD_HOST
    try:
        return str(ipaddress.ip_network(candidate, strict=False))
    except ValueError as exc:
        raise ValueError(
            f"allowed client {value!r} must be an IP address or CIDR range"
        ) from exc


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
    def __init__(
        self,
        app,
        *,
        body_limit: int,
        allowed_hosts: tuple[str, ...] = (),
        allowed_clients: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.body_limit = body_limit
        normalized = [normalize_allowed_host(item) for item in allowed_hosts]
        self.allow_any_host = WILDCARD_HOST in normalized
        # Parsed once, so every request compares tuples rather than re-splitting
        # strings whose canonical form was already settled at startup.
        self.allowed_authorities = frozenset(
            authority
            for authority in (
                _parse_authority(item)
                for item in normalized
                if item != WILDCARD_HOST
            )
            if authority is not None
        )
        self.allowed_host_networks = tuple(
            network
            for network in (parse_network(item) for item in normalized)
            if network is not None
        )
        clients = [normalize_allowed_client(item) for item in allowed_clients]
        self.allow_any_client = WILDCARD_HOST in clients
        self.allowed_client_networks = tuple(
            network
            for network in (
                parse_network(item) for item in clients if item != WILDCARD_HOST
            )
            if network is not None
        )
        # Naming no client at all is what every install did before peer
        # filtering existed: the bind address is the access control, and this
        # check stays off until an operator turns it on.
        self.restrict_clients = bool(clients) and not self.allow_any_client

    def _client_allowed(self, scope: Scope) -> bool:
        if not self.restrict_clients:
            return True
        client = scope.get("client")
        peer = client[0] if client else None
        # A transport with no IP peer cannot be shown to be inside the range, so
        # a configured restriction fails closed rather than waving it through.
        return _within(
            _address(peer) if peer else None,
            self.allowed_client_networks,
        )

    def _host_allowed(self, value: str) -> bool:
        parsed = _parse_authority(value)
        if parsed is None:
            return False
        hostname, port = parsed
        if hostname in LOOPBACK_HOSTNAMES:
            return True
        if self.allow_any_host:
            return True
        # A portless entry matches any port; a ported one binds that port only.
        if (hostname, port) in self.allowed_authorities or (
            hostname,
            None,
        ) in self.allowed_authorities:
            return True
        # A range matches the literal a browser sent, never a name that would
        # resolve into it: this check reads the header and resolves nothing.
        return _within(_address(hostname), self.allowed_host_networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Before the Host check, so a peer outside the range learns nothing
        # about which Host values this server is configured to answer to.
        if not self._client_allowed(scope):
            await _plain_response(send, HTTPStatus.FORBIDDEN, "Forbidden Client")
            return

        host = _header(scope, b"host")
        if host is None or not self._host_allowed(host):
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

from starlette.middleware.base import BaseHTTPMiddleware

from app.project_routing import (
    CanonicalProjectMiddleware,
    _query_for_redirect,
)


def test_project_canonicalizer_is_pure_asgi() -> None:
    assert not issubclass(CanonicalProjectMiddleware, BaseHTTPMiddleware)


def test_redirect_query_preserves_ascii_escapes_and_quotes_raw_octets() -> None:
    assert _query_for_redirect(b"agent=a%20b&raw=\xff#tail") == (
        "agent=a%20b&raw=%FF%23tail"
    )

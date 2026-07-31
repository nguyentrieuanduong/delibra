"""Canonical project-name routing for browser requests."""

from __future__ import annotations

from urllib.parse import quote_from_bytes

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.models import Project
from app.storage import StorageError
from app.urls import project_url


RESOLVED_PROJECT_SCOPE_KEY = "delibra.resolved_project"
QUERY_REDIRECT_SAFE = "!$&'()*+,-./:;=?@_~%[]"


def _query_for_redirect(query: bytes) -> str:
    return quote_from_bytes(query, safe=QUERY_REDIRECT_SAFE)


class CanonicalProjectMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope.get("type") != "http"
            or str(scope.get("method", "GET")).upper() not in {"GET", "HEAD"}
        ):
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        parts = path.split("/")
        if len(parts) < 3 or parts[1] != "projects" or not parts[2]:
            await self.app(scope, receive, send)
            return
        registry = getattr(scope["app"].state, "registry", None)
        if registry is None:
            await self.app(scope, receive, send)
            return
        try:
            project = registry.resolve(parts[2])
        except StorageError:
            await self.app(scope, receive, send)
            return
        scope[RESOLVED_PROJECT_SCOPE_KEY] = project
        legacy_stream = (
            parts[2] == project.id
            and len(parts) > 3
            and parts[-1] == "stream"
        )
        if parts[2] == project.name or legacy_stream:
            await self.app(scope, receive, send)
            return
        suffix = "/" + "/".join(parts[3:]) if len(parts) > 3 else ""
        location = project_url(project.name, suffix)
        query = bytes(scope.get("query_string", b""))
        if query:
            location = f"{location}?{_query_for_redirect(query)}"
        response = RedirectResponse(location, status_code=302)
        await response(scope, receive, send)


def request_project(request: Request, reference: str) -> Project:
    cached = request.scope.get(RESOLVED_PROJECT_SCOPE_KEY)
    if isinstance(cached, Project):
        return cached
    return request.app.state.registry.resolve(reference)

"""FastAPI application factory and local single-process lifecycle."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.config import Settings, settings
from app.health import probe_all
from app.markdown import render_markdown
from app.routes.sessions import router as sessions_router
from app.runner import RunManager
from app.security import LocalSecurityMiddleware
from app.storage import (
    ConflictError,
    LockCoordinator,
    NotFoundError,
    ProjectStore,
    RegistryStore,
    StorageError,
)


LOGGER = logging.getLogger(__name__)
APP_ROOT = Path(__file__).resolve().parent


def create_app(
    *,
    settings_override: Settings | None = None,
    provider_commands: dict[str, str] | None = None,
) -> FastAPI:
    app_settings = settings_override or settings
    commands = provider_commands or {"claude": "claude", "codex": "codex"}

    def adapters(config):
        if config.agent == "claude":
            return ClaudeAdapter(commands["claude"])
        if config.agent == "codex":
            return CodexAdapter(commands["codex"])
        raise ValueError(f"unknown agent: {config.agent}")

    templates = Jinja2Templates(directory=APP_ROOT / "templates")
    templates.env.filters["md"] = render_markdown

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry = RegistryStore(app_settings.home)
        locks = LockCoordinator()
        manager = RunManager(
            registry=registry,
            locks=locks,
            settings=app_settings,
            adapter_factory=adapters,
        )
        app.state.registry = registry
        app.state.locks = locks
        app.state.manager = manager
        app.state.health = probe_all(commands)
        for project in registry.list_projects():
            try:
                store = ProjectStore(project)
                for session in store.list_sessions():
                    store.reconcile_session(session.id)
            except StorageError:
                LOGGER.exception("Startup reconciliation failed for project %s", project.id)
        yield
        await manager.shutdown()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        LocalSecurityMiddleware,
        body_limit=app_settings.request_body_limit,
    )
    app.mount("/static", StaticFiles(directory=APP_ROOT / "static"), name="static")
    app.state.settings = app_settings
    app.state.registry = None
    app.state.locks = None
    app.state.manager = None
    app.state.templates = templates
    app.state.health = []
    app.include_router(sessions_router)

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(StorageError)
    async def storage_error(_: Request, exc: StorageError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        registry = request.app.state.registry
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "projects": registry.list_projects(),
                "health": request.app.state.health,
            },
        )

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    async def project_page(request: Request, project_id: str):
        registry = request.app.state.registry
        project = registry.get(project_id)
        sessions = ProjectStore(project).list_sessions()
        return templates.TemplateResponse(
            request=request,
            name="project.html",
            context={
                "project": project,
                "sessions": sessions,
                "health": request.app.state.health,
            },
        )

    return app


app = create_app()

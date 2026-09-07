"""FastAPI application factory and local single-process lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.auto import AutoManager
from app.config import Settings, settings
from app.health import checking_health, probe_all
from app.markdown import render_markdown
from app.project_routing import CanonicalProjectMiddleware, request_project
from app.routes.auto import router as auto_router
from app.routes.chat import router as chat_router
from app.routes.files import router as files_router
from app.routes.projects import router as projects_router
from app.routes.runs import router as runs_router
from app.routes.sessions import router as sessions_router
from app.routes.usage import router as usage_router
from app.runner import AdapterFactory, RunManager
from app.security import LocalSecurityMiddleware
from app.storage import (
    ConflictError,
    LockCoordinator,
    NotFoundError,
    ProjectFileDisplayError,
    ProjectFileSecurityError,
    ProjectStore,
    RegistryStore,
    StorageError,
)
from app.urls import project_url
from app.usage import UsageMonitor
from app.views import project_card


LOGGER = logging.getLogger(__name__)
APP_ROOT = Path(__file__).resolve().parent


def create_app(
    *,
    settings_override: Settings | None = None,
    provider_commands: dict[str, str] | None = None,
    adapter_factory_override: AdapterFactory | None = None,
) -> FastAPI:
    app_settings = settings_override or settings
    commands = provider_commands or {"claude": "claude", "codex": "codex"}

    def built_in_adapter(config):
        if config.agent == "claude":
            return ClaudeAdapter(commands["claude"])
        if config.agent == "codex":
            return CodexAdapter(commands["codex"])
        raise ValueError(f"unknown agent: {config.agent}")

    adapter_factory = adapter_factory_override or built_in_adapter

    templates = Jinja2Templates(directory=APP_ROOT / "templates")
    templates.env.filters["md"] = render_markdown
    templates.env.globals["project_url"] = project_url

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry = RegistryStore(app_settings.home)
        locks = LockCoordinator()
        # One account-wide quota view: it can change because of a run in any
        # project, so every component reads the same instance.
        usage_monitor = UsageMonitor(settings=app_settings)
        manager = RunManager(
            registry=registry,
            locks=locks,
            settings=app_settings,
            adapter_factory=adapter_factory,
            usage_monitor=usage_monitor,
        )
        auto_manager = AutoManager(
            registry=registry,
            locks=locks,
            settings=app_settings,
            runner=manager,
        )
        app.state.registry = registry
        app.state.locks = locks
        app.state.manager = manager
        app.state.auto_manager = auto_manager
        app.state.usage_monitor = usage_monitor
        app.state.health = checking_health(commands)

        async def refresh_health() -> None:
            try:
                app.state.health = await probe_all(commands)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Background CLI health probe failed")

        health_task = asyncio.create_task(
            refresh_health(),
            name="delibra-health-probe",
        )
        for project in registry.list_projects():
            try:
                ProjectStore(project).sync_manifest_name(project.name)
            except StorageError:
                LOGGER.exception(
                    "Project name carrier synchronization failed for project %s",
                    project.id,
                )
            try:
                store = ProjectStore(project)
                migration = store.migrate_session_directories()
                if migration.issues:
                    LOGGER.warning(
                        "Agent directory migration needs input for project %s: %s",
                        project.id,
                        ", ".join(issue.session_id for issue in migration.issues),
                    )
                for session in store.list_sessions():
                    store.reconcile_session(session.id)
                auto_migration = await auto_manager.reconcile_project(project.id)
                if auto_migration.issues:
                    LOGGER.warning(
                        "Auto directory migration needs input for project %s: %s",
                        project.id,
                        ", ".join(
                            issue.child for issue in auto_migration.issues
                        ),
                    )
            except StorageError:
                LOGGER.exception("Startup reconciliation failed for project %s", project.id)
        try:
            yield
        finally:
            health_task.cancel()
            try:
                await asyncio.wait_for(health_task, timeout=2)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                LOGGER.error("CLI health probe did not stop within the shutdown bound")
            await auto_manager.shutdown()
            await manager.shutdown()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(CanonicalProjectMiddleware)
    app.add_middleware(
        LocalSecurityMiddleware,
        body_limit=app_settings.request_body_limit,
    )
    app.mount("/static", StaticFiles(directory=APP_ROOT / "static"), name="static")
    app.state.settings = app_settings
    app.state.registry = None
    app.state.locks = None
    app.state.manager = None
    app.state.auto_manager = None
    app.state.usage_monitor = None
    app.state.templates = templates
    app.state.health = []
    app.include_router(projects_router)
    app.include_router(auto_router)
    app.include_router(chat_router)
    app.include_router(files_router)
    app.include_router(sessions_router)
    app.include_router(runs_router)
    app.include_router(usage_router)

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
        projects = registry.list_projects()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "project_cards": [project_card(project) for project in projects],
                "health": request.app.state.health,
            },
        )

    @app.get("/projects/{project_id}")
    async def project_primary(request: Request, project_id: str):
        project = request_project(request, project_id)
        return RedirectResponse(project_url(project.name, "/chat"), status_code=303)

    @app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
    async def project_settings(request: Request, project_id: str):
        project = request_project(request, project_id)
        store = ProjectStore(project)
        sessions = store.list_sessions()
        shared_markdown_path = store.selected_shared_markdown_path()
        shared_markdown_available: bool | None = None
        if shared_markdown_path is not None:
            try:
                store.read_selected_shared_markdown(
                    request.app.state.settings.file_view_limit
                )
            except (ProjectFileDisplayError, ProjectFileSecurityError):
                shared_markdown_available = False
            else:
                shared_markdown_available = True
        migration = store.session_migration_status()
        migration_issue_lists: dict[str, list[str]] = {}
        for issue in migration.issues:
            migration_issue_lists.setdefault(issue.session_id, []).append(issue.message)
        migration_issues = {
            session_id: tuple(messages)
            for session_id, messages in migration_issue_lists.items()
        }
        auto_migration_issues = store.auto_migration_status().issues
        return templates.TemplateResponse(
            request=request,
            name="project.html",
            context={
                "project": project,
                "sessions": sessions,
                "shared_markdown_path": shared_markdown_path,
                "shared_markdown_available": shared_markdown_available,
                "health": request.app.state.health,
                "effort_levels": {
                    "claude": ClaudeAdapter.EFFORT_LEVELS,
                    "codex": CodexAdapter.EFFORT_LEVELS,
                },
                "auto_active": store.active_auto_run_id() is not None,
                "legacy_session_ids": frozenset(migration.legacy_session_ids),
                "migration_issues": migration_issues,
                "auto_migration_issues": auto_migration_issues,
                "pass_prompt_template": store.effective_pass_prompt_template(),
            },
        )

    return app


app = create_app()

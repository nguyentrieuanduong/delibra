"""Project registration, naming, and registry removal routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.project_routing import request_project
from app.security import (
    validate_field,
    validate_pass_prompt_template_field,
)
from app.storage import (
    ConflictError,
    ProjectStore,
    StorageError,
    normalize_project_name,
    normalize_writable_roots,
)
from app.urls import project_url


router = APIRouter()


def _project_path(value: str) -> Path:
    value = validate_field(value, "Path", maximum=4_096)
    candidate = Path(value)
    if not candidate.is_absolute():
        raise HTTPException(status_code=422, detail="Project path must be absolute")
    return candidate


def _writable_roots_field(value: str) -> list[str]:
    validated = validate_field(
        value,
        "Writable roots",
        maximum=65_536,
        allow_empty=True,
    )
    return normalize_writable_roots(
        [line.strip() for line in validated.splitlines() if line.strip()]
    )


def _reject_running_sessions(store: ProjectStore) -> None:
    sessions = store.list_sessions()
    if any(session.status == "running" for session in sessions):
        raise ConflictError("project has a running agent")


def _reject_in_memory_work(request: Request, project_id: str) -> None:
    if request.app.state.manager.has_active_project(project_id):
        raise ConflictError("project has active agent work")
    if request.app.state.auto_manager.has_active_project(project_id):
        raise ConflictError("project has active Auto work")


@router.post("/projects")
async def register_project(
    request: Request,
    name: str = Form(...),
    path: str = Form(...),
):
    try:
        name = normalize_project_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    candidate = _project_path(path)
    async with request.app.state.locks.registry_lock:
        project = request.app.state.registry.register(name, candidate)
    return RedirectResponse(project_url(project.name, "/chat"), status_code=303)


@router.post("/projects/{project_id}/rebind")
async def rebind_project(
    request: Request,
    project_id: str,
    path: str = Form(...),
):
    candidate_path = _project_path(path)
    project = request_project(request, project_id)
    resolved_project_id = project.id
    registry = request.app.state.registry
    candidate = registry.preview_rebind(resolved_project_id, candidate_path)
    candidate_store = ProjectStore(candidate)
    session_ids = [session.id for session in candidate_store.list_sessions()]
    async with request.app.state.locks.registry_project_sessions(
        resolved_project_id,
        session_ids,
    ):
        _reject_in_memory_work(request, resolved_project_id)
        candidate = registry.preview_rebind(resolved_project_id, candidate_path)
        candidate_store = ProjectStore(candidate)
        candidate_store.clear_native_session_ids_for_relocation()
        candidate_store.migrate_session_directories()
        for session in candidate_store.list_sessions():
            candidate_store.reconcile_session(session.id)
        auto_migration = candidate_store.migrate_auto_run_directories()
        request.app.state.auto_manager.reconcile_store_locked(
            candidate_store,
            auto_migration.readable_records,
        )
        rebound = registry.rebind(resolved_project_id, candidate_path)
    return RedirectResponse(
        project_url(rebound.name, "/chat"),
        status_code=303,
    )


@router.post("/projects/{project_id}/unregister")
async def unregister_project(request: Request, project_id: str):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        project = request.app.state.registry.get(resolved_project_id)
        try:
            store = ProjectStore(project)
        except StorageError:
            store = None
        if store is not None:
            try:
                active_id = store.active_auto_run_id()
                if active_id is not None:
                    store.load_auto_run(active_id)
            except StorageError:
                store = None
            else:
                if active_id is not None:
                    raise ConflictError("project has an active Auto run")
                _reject_running_sessions(store)
        if store is None:
            _reject_in_memory_work(request, resolved_project_id)
        request.app.state.registry.unregister(resolved_project_id)
    return RedirectResponse("/", status_code=303)


@router.post("/projects/{project_id}/auto-migration/retry")
async def retry_auto_migration(request: Request, project_id: str):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    store = ProjectStore(project)
    session_ids = [session.id for session in store.list_sessions()]
    async with request.app.state.locks.registry_project_sessions(
        resolved_project_id,
        session_ids,
    ):
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        current_session_ids = [session.id for session in store.list_sessions()]
        if set(current_session_ids) != set(session_ids):
            raise ConflictError("project sessions changed during Auto migration retry")
        _reject_in_memory_work(request, resolved_project_id)
        status = store.migrate_auto_run_directories()
        request.app.state.auto_manager.reconcile_store_locked(
            store,
            status.readable_records,
        )
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/pass-prompt")
async def save_pass_prompt(
    request: Request,
    project_id: str,
    pass_prompt_template: str = Form(...),
):
    validated = validate_pass_prompt_template_field(pass_prompt_template)
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        project = request.app.state.registry.get(resolved_project_id)
        ProjectStore(project).set_pass_prompt_template(validated)
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/writable-roots")
async def save_writable_roots(
    request: Request,
    project_id: str,
    writable_roots: str = Form(""),
):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        _reject_in_memory_work(request, resolved_project_id)
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        store.require_auto_inactive()
        store.set_writable_roots(_writable_roots_field(writable_roots))
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/pass-prompt/reset")
async def reset_pass_prompt(request: Request, project_id: str):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        project = request.app.state.registry.get(resolved_project_id)
        ProjectStore(project).reset_pass_prompt_template()
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )

"""Project registration, naming, and registry removal routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.security import (
    validate_field,
    validate_pass_prompt_template_field,
)
from app.storage import (
    ConflictError,
    ProjectStore,
    StorageError,
    normalize_project_name,
)
from app.urls import project_url


router = APIRouter()


def _project_path(value: str) -> Path:
    value = validate_field(value, "Path", maximum=4_096)
    candidate = Path(value)
    if not candidate.is_absolute():
        raise HTTPException(status_code=422, detail="Project path must be absolute")
    return candidate


def _reject_running_sessions(project) -> None:
    sessions = ProjectStore(project).list_sessions()
    if any(session.status == "running" for session in sessions):
        raise ConflictError("project has a running agent")


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
    return RedirectResponse(project_url(project.id, "/chat"), status_code=303)


@router.post("/projects/{project_id}/rebind")
async def rebind_project(
    request: Request,
    project_id: str,
    path: str = Form(...),
):
    candidate_path = _project_path(path)
    registry = request.app.state.registry
    candidate = registry.preview_rebind(project_id, candidate_path)
    candidate_store = ProjectStore(candidate)
    session_ids = [session.id for session in candidate_store.list_sessions()]
    async with request.app.state.locks.registry_project_sessions(
        project_id,
        session_ids,
    ):
        if (
            request.app.state.manager.has_active_project(project_id)
            or request.app.state.auto_manager.has_active_project(project_id)
        ):
            raise ConflictError("project has active work")
        candidate = registry.preview_rebind(project_id, candidate_path)
        candidate_store = ProjectStore(candidate)
        candidate_store.clear_native_session_ids_for_relocation()
        candidate_store.migrate_session_directories()
        for session in candidate_store.list_sessions():
            candidate_store.reconcile_session(session.id)
        request.app.state.auto_manager.reconcile_store_locked(candidate_store)
        rebound = registry.rebind(project_id, candidate_path)
    return RedirectResponse(
        project_url(rebound.id, "/chat"),
        status_code=303,
    )


@router.post("/projects/{project_id}/unregister")
async def unregister_project(request: Request, project_id: str):
    async with request.app.state.locks.registry_project_sessions(project_id):
        project = request.app.state.registry.get(project_id)
        try:
            store = ProjectStore(project)
        except StorageError:
            if (
                request.app.state.manager.has_active_project(project_id)
                or request.app.state.auto_manager.has_active_project(project_id)
            ):
                raise ConflictError("project has active work")
        else:
            store.require_auto_inactive()
            _reject_running_sessions(project)
        request.app.state.registry.unregister(project_id)
    return RedirectResponse("/", status_code=303)


@router.post("/projects/{project_id}/pass-prompt")
async def save_pass_prompt(
    request: Request,
    project_id: str,
    pass_prompt_template: str = Form(...),
):
    validated = validate_pass_prompt_template_field(pass_prompt_template)
    async with request.app.state.locks.registry_project_sessions(project_id):
        project = request.app.state.registry.get(project_id)
        ProjectStore(project).set_pass_prompt_template(validated)
    return RedirectResponse(
        project_url(project_id, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/pass-prompt/reset")
async def reset_pass_prompt(request: Request, project_id: str):
    async with request.app.state.locks.registry_project_sessions(project_id):
        project = request.app.state.registry.get(project_id)
        ProjectStore(project).reset_pass_prompt_template()
    return RedirectResponse(
        project_url(project_id, "/settings"),
        status_code=303,
    )

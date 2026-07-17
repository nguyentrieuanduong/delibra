"""Project registration, naming, and registry removal routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.security import validate_field
from app.storage import ConflictError, ProjectStore, sanitize_name


router = APIRouter()


def _name(value: str) -> str:
    validate_field(value, "Name", maximum=200)
    try:
        return sanitize_name(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    name = _name(name)
    candidate = _project_path(path)
    async with request.app.state.locks.registry_lock:
        project = request.app.state.registry.register(name, candidate)
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@router.post("/projects/{project_id}/rename")
async def rename_project(
    request: Request,
    project_id: str,
    name: str = Form(...),
):
    name = _name(name)
    async with request.app.state.locks.registry_project_sessions(project_id):
        project = request.app.state.registry.get(project_id)
        _reject_running_sessions(project)
        request.app.state.registry.rename(project_id, name)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/unregister")
async def unregister_project(request: Request, project_id: str):
    async with request.app.state.locks.registry_project_sessions(project_id):
        project = request.app.state.registry.get(project_id)
        _reject_running_sessions(project)
        request.app.state.registry.unregister(project_id)
    return RedirectResponse("/", status_code=303)

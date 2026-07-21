"""Project registration, naming, and registry removal routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.security import (
    validate_field,
    validate_name,
    validate_pass_prompt_template_field,
)
from app.storage import ConflictError, ProjectStore


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
    name = validate_name(name)
    candidate = _project_path(path)
    async with request.app.state.locks.registry_lock:
        project = request.app.state.registry.register(name, candidate)
    return RedirectResponse(f"/projects/{project.id}/chat", status_code=303)


@router.post("/projects/{project_id}/rename")
async def rename_project(
    request: Request,
    project_id: str,
    name: str = Form(...),
):
    name = validate_name(name)
    async with request.app.state.locks.registry_project_sessions(project_id):
        project = request.app.state.registry.get(project_id)
        ProjectStore(project).require_auto_inactive()
        _reject_running_sessions(project)
        request.app.state.registry.rename(project_id, name)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/unregister")
async def unregister_project(request: Request, project_id: str):
    async with request.app.state.locks.registry_project_sessions(project_id):
        project = request.app.state.registry.get(project_id)
        ProjectStore(project).require_auto_inactive()
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
        f"/projects/{project_id}/settings",
        status_code=303,
    )


@router.post("/projects/{project_id}/pass-prompt/reset")
async def reset_pass_prompt(request: Request, project_id: str):
    async with request.app.state.locks.registry_project_sessions(project_id):
        project = request.app.state.registry.get(project_id)
        ProjectStore(project).reset_pass_prompt_template()
    return RedirectResponse(
        f"/projects/{project_id}/settings",
        status_code=303,
    )

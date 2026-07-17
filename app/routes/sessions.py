"""Session CRUD and round-history routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.models import SessionConfig
from app.routes.chat import sidebar_response
from app.security import validate_field, validate_name
from app.storage import ConflictError, NotFoundError, ProjectStore, utc_now
from app.views import round_views


router = APIRouter()


def _resume_after_config_change(agent: str) -> bool:
    capabilities = {
        "claude": ClaudeAdapter.RESUME_AFTER_CONFIG_CHANGE,
        "codex": CodexAdapter.RESUME_AFTER_CONFIG_CHANGE,
    }
    return capabilities[agent]


def _validated_configuration(
    agent: str,
    model: str,
    effort: str,
    role_instructions: str,
) -> tuple[str, str, str, str]:
    effort_levels = {
        "claude": ClaudeAdapter.EFFORT_LEVELS,
        "codex": CodexAdapter.EFFORT_LEVELS,
    }
    if agent not in effort_levels:
        raise HTTPException(status_code=422, detail="Agent must be claude or codex")
    model = validate_field(model, "Model", maximum=200).strip()
    if any(character.isspace() or ord(character) < 32 for character in model):
        raise HTTPException(status_code=422, detail="Model contains invalid characters")
    if effort not in effort_levels[agent]:
        allowed = ", ".join(effort_levels[agent])
        raise HTTPException(
            status_code=422,
            detail=f"Effort for {agent} must be one of: {allowed}",
        )
    role_instructions = validate_field(
        role_instructions,
        "Role instructions",
        maximum=20_000,
        allow_empty=True,
    )
    return agent, model, effort, role_instructions


@router.post("/projects/{project_id}/sessions")
async def create_session(
    request: Request,
    project_id: str,
    name: str = Form(...),
    agent: str = Form(...),
    model: str = Form(...),
    effort: str = Form(...),
    role_instructions: str = Form(""),
):
    name = validate_name(name)
    agent, model, effort, role_instructions = _validated_configuration(
        agent, model, effort, role_instructions
    )
    session_id = uuid4().hex
    async with request.app.state.locks.project_sessions(project_id, [session_id]):
        project = request.app.state.registry.get(project_id)
        config = SessionConfig(
            id=session_id,
            name=name,
            agent=agent,
            model=model,
            effort=effort,
            role_instructions=role_instructions,
            cli_session_id=None,
            status="idle",
            created_at=utc_now(),
            rounds=[],
        )
        ProjectStore(project).create_session(config)
    if request.headers.get("HX-Request") == "true":
        return sidebar_response(
            request,
            project,
            selected_id=session_id,
            composer_oob=True,
            headers={
                "HX-Push-Url": f"/projects/{project_id}/chat?agent={session_id}"
            },
        )
    return RedirectResponse(
        f"/projects/{project_id}/sessions/{session_id}",
        status_code=303,
    )


@router.post("/projects/{project_id}/sessions/{session_id}/edit")
async def edit_session(
    request: Request,
    project_id: str,
    session_id: str,
    name: str = Form(...),
    agent: str | None = Form(None),
    model: str | None = Form(None),
    effort: str | None = Form(None),
    role_instructions: str | None = Form(None),
    selected_agent: str | None = Query(None, alias="agent"),
):
    supplied_configuration = any(
        value is not None for value in (agent, model, effort, role_instructions)
    )
    async with request.app.state.locks.project_sessions(project_id, [session_id]):
        project = request.app.state.registry.get(project_id)
        store = ProjectStore(project)
        config = store.load_session(session_id)
        if config.status == "running":
            raise ConflictError("cannot edit a running session")
        name = validate_name(name)
        if config.rounds:
            if agent is not None and agent != config.agent:
                raise ConflictError("the agent cannot change after the first round")
            if (
                role_instructions is not None
                and role_instructions != config.role_instructions
            ):
                raise ConflictError(
                    "role instructions cannot change after the first round"
                )
            updated = _validated_configuration(
                config.agent,
                model if model is not None else config.model,
                effort if effort is not None else config.effort,
                config.role_instructions,
            )
            model_changed = updated[1] != config.model
            effort_changed = updated[2] != config.effort
            config.model = updated[1]
            config.effort = updated[2]
            if (
                (model_changed or effort_changed)
                and not _resume_after_config_change(config.agent)
            ):
                config.cli_session_id = None
        elif supplied_configuration:
            updated = _validated_configuration(
                agent if agent is not None else config.agent,
                model if model is not None else config.model,
                effort if effort is not None else config.effort,
                (
                    role_instructions
                    if role_instructions is not None
                    else config.role_instructions
                ),
            )
            config.agent, config.model, config.effort, config.role_instructions = updated
        config.name = name
        store.save_session(config)
    if request.headers.get("HX-Request") == "true":
        return sidebar_response(
            request,
            project,
            selected_id=selected_agent or session_id,
            composer_oob=True,
        )
    return RedirectResponse(
        f"/projects/{project_id}/sessions/{session_id}",
        status_code=303,
    )


@router.post("/projects/{project_id}/sessions/{session_id}/delete")
async def delete_session(
    request: Request,
    project_id: str,
    session_id: str,
):
    async with request.app.state.locks.project_sessions(project_id, [session_id]):
        project = request.app.state.registry.get(project_id)
        ProjectStore(project).delete_session(session_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/sessions/{session_id}")
async def session_page(request: Request, project_id: str, session_id: str):
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    session = store.load_session(session_id)
    sessions = store.list_sessions()
    active_key = request.app.state.manager.active_key(project_id, session_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="session.html",
        context={
            "project": project,
            "session": session,
            "rounds": round_views(store, session_id),
            "sessions": sessions,
            "active_key": active_key,
            "health": request.app.state.health,
        },
    )


@router.get("/projects/{project_id}/sessions/{session_id}/rounds/{round_n}")
async def round_fragment(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
    display: str | None = Query(None, alias="view"),
):
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    session = store.load_session(session_id)
    sessions = store.list_sessions()
    view = next(
        (item for item in round_views(store, session_id) if item["n"] == round_n),
        None,
    )
    if view is None:
        raise NotFoundError(f"round not found: {round_n}")
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_round.html",
        context={
            "project": project,
            "session": session,
            "session_id": session_id,
            "round": view,
            "sessions": sessions,
            "chat_view": display == "chat",
        },
    )

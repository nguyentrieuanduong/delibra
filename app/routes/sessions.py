"""Session CRUD and round-history routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.models import SessionConfig
from app.project_routing import request_project
from app.routes.chat import sidebar_response
from app.routes.projects import _writable_roots_field
from app.security import validate_agent_name, validate_field
from app.storage import (
    ConflictError,
    NotFoundError,
    ProjectStore,
    normalize_writable_roots,
    utc_now,
    validate_id,
)
from app.urls import project_url
from app.views import agent_views, round_views


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
    writable_roots: str = Form(""),
):
    name = validate_agent_name(name)
    agent, model, effort, role_instructions = _validated_configuration(
        agent, model, effort, role_instructions
    )
    project = request_project(request, project_id)
    resolved_project_id = project.id
    session_id = uuid4().hex
    async with request.app.state.locks.project_sessions(
        resolved_project_id,
        [session_id],
    ):
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        store.require_auto_inactive()
        validated_writable_roots = store.validate_session_writable_roots(
            _writable_roots_field(writable_roots)
        )
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
            writable_roots=validated_writable_roots,
        )
        store.create_session(config)
    if request.headers.get("HX-Request") == "true":
        return sidebar_response(
            request,
            project,
            selected_id=session_id,
            composer_oob=True,
            headers={
                "HX-Push-Url": project_url(
                    project.name,
                    f"/chat?agent={session_id}",
                )
            },
        )
    return RedirectResponse(
        project_url(project.name, f"/chat?agent={session_id}"),
        status_code=303,
    )


@router.post("/projects/{project_id}/sessions/{session_id}/edit")
async def edit_session(
    request: Request,
    project_id: str,
    session_id: str,
    name: str | None = Form(None),
    agent: str | None = Form(None),
    model: str | None = Form(None),
    effort: str | None = Form(None),
    role_instructions: str | None = Form(None),
    writable_roots: str | None = Form(None),
    selected_agent: str | None = Query(None, alias="agent"),
):
    if writable_roots is None:
        submitted_form = await request.form()
        if "writable_roots" in submitted_form:
            submitted_writable_roots = submitted_form["writable_roots"]
            if not isinstance(submitted_writable_roots, str):
                raise HTTPException(
                    status_code=422,
                    detail="Writable roots must be text",
                )
            writable_roots = submitted_writable_roots
    supplied_configuration = any(
        value is not None
        for value in (agent, model, effort, role_instructions, writable_roots)
    )
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.project_sessions(
        resolved_project_id,
        [session_id],
    ):
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        store.require_auto_inactive()
        config = store.load_session(session_id)
        if config.status == "running":
            raise ConflictError("cannot edit a running session")
        old_effective_writable_roots = (
            store.session_writable_roots(session_id)
            if writable_roots is not None
            else None
        )
        if writable_roots is not None:
            config.writable_roots = store.validate_session_writable_roots(
                _writable_roots_field(writable_roots)
            )
        if name is not None and validate_agent_name(name) != config.name:
            raise ConflictError("agent name is immutable")
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
            if model_changed:
                # A window measured against one model does not describe
                # another. This is unconditional on purpose: whether to keep
                # the native session is a separate provider-policy decision,
                # and its branch below never runs for either built-in adapter.
                config.context_observation = None
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
        new_effective_writable_roots = (
            normalize_writable_roots(
                [*store.effective_writable_roots(), *config.writable_roots]
            )
            if old_effective_writable_roots is not None
            else None
        )
        writable_roots_changed = (
            old_effective_writable_roots is not None
            and old_effective_writable_roots != new_effective_writable_roots
        )
        if writable_roots_changed:
            config.cli_session_id = None
        store.save_session(config, replace_recovery=writable_roots_changed)
    if request.headers.get("HX-Request") == "true":
        return sidebar_response(
            request,
            project,
            selected_id=selected_agent or session_id,
            composer_oob=True,
        )
    return RedirectResponse(
        project_url(project.name, f"/sessions/{session_id}"),
        status_code=303,
    )


@router.post("/projects/{project_id}/sessions/{session_id}/permanent-name")
async def set_permanent_session_name(
    request: Request,
    project_id: str,
    session_id: str,
    name: str = Form(...),
):
    validate_id(session_id, "session id")
    name = validate_agent_name(name)
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.project_sessions(
        resolved_project_id,
        [session_id],
    ):
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        store.require_auto_inactive()
        config = store.load_session(session_id)
        if config.status == "running":
            raise ConflictError("cannot name a running session")
        store.set_legacy_session_name(session_id, name)
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/sessions/{session_id}/context/clear")
async def clear_session_context(
    request: Request,
    project_id: str,
    session_id: str,
    agent: str | None = Query(None),
):
    """Retire this session's context without spending a provider call."""

    validate_id(session_id, "session id")
    project = request_project(request, project_id)
    await request.app.state.manager.clear_context(project.id, session_id)
    return sidebar_response(
        request,
        request.app.state.registry.get(project.id),
        selected_id=agent or session_id,
        composer_oob=False,
    )


@router.post("/projects/{project_id}/sessions/{session_id}/delete")
async def delete_session(
    request: Request,
    project_id: str,
    session_id: str,
):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.project_sessions(
        resolved_project_id,
        [session_id],
    ):
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        store.require_auto_inactive()
        store.delete_session(session_id)
    return RedirectResponse(project_url(project.name), status_code=303)


@router.get("/projects/{project_id}/sessions/{session_id}")
async def session_page(request: Request, project_id: str, session_id: str):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    store = ProjectStore(project)
    session = store.load_session(session_id)
    sessions = store.list_sessions()
    active_key = request.app.state.manager.active_key(resolved_project_id, session_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="session.html",
        context={
            "project": project,
            "session": session,
            "rounds": round_views(store, session_id),
            "sessions": sessions,
            "active_key": active_key,
            "auto_active": store.active_auto_run_id() is not None,
            "health": request.app.state.health,
            "pass_prompt_template": store.effective_pass_prompt_template(),
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
    project = request_project(request, project_id)
    store = ProjectStore(project)
    session = store.load_session(session_id)
    sessions = store.list_sessions()
    view = next(
        (item for item in round_views(store, session_id) if item["n"] == round_n),
        None,
    )
    if view is None:
        raise NotFoundError(f"round not found: {round_n}")
    chat_view = display == "chat"
    # Only the chat view has a sidebar to refresh; the session page does not.
    # Project only the affected card: agent_views reads each supplied session's
    # latest output artifact, so passing every session adds unrelated I/O.
    card = agent_views(store, [session], None)[0] if chat_view else None
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_round.html",
        context={
            "project": project,
            "session": session,
            "session_id": session_id,
            "round": view,
            "sessions": sessions,
            "chat_view": chat_view,
            "agent_card_oob": chat_view,
            "agent_view": card,
            "auto_active": store.active_auto_run_id() is not None,
            "pass_prompt_template": store.effective_pass_prompt_template(),
        },
    )

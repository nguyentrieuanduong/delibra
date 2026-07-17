"""Session CRUD and round-history routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.models import SessionConfig
from app.security import validate_field
from app.storage import ConflictError, ProjectStore, sanitize_name, utc_now


router = APIRouter()


def _validated_name(value: str) -> str:
    validate_field(value, "Name", maximum=200)
    try:
        return sanitize_name(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    name = _validated_name(name)
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
):
    name = _validated_name(name)
    supplied_configuration = any(
        value is not None for value in (agent, model, effort, role_instructions)
    )
    async with request.app.state.locks.project_sessions(project_id, [session_id]):
        project = request.app.state.registry.get(project_id)
        store = ProjectStore(project)
        config = store.load_session(session_id)
        if config.rounds and supplied_configuration:
            raise ConflictError("only the session name can change after the first round")
        if not config.rounds and supplied_configuration:
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


def _round_views(store: ProjectStore, session_id: str) -> list[dict]:
    config = store.load_session(session_id)
    rounds_dir = store.rounds_dir(session_id)
    views: list[dict] = []
    known = {record.n for record in config.rounds}
    for record in sorted(config.rounds, key=lambda item: item.n):
        prompt = rounds_dir / f"round-{record.n:02d}.prompt.md"
        output = rounds_dir / f"round-{record.n:02d}.md"
        partial = rounds_dir / f"round-{record.n:02d}.partial.md"
        views.append(
            {
                "n": record.n,
                "record": record,
                "prompt": prompt.read_text(encoding="utf-8") if prompt.exists() else "",
                "output": (
                    output.read_text(encoding="utf-8")
                    if output.exists()
                    else partial.read_text(encoding="utf-8")
                    if partial.exists()
                    else ""
                ),
                "orphan": False,
            }
        )
    scan = store.scan_round_files(session_id)
    for number in sorted({item.n for item in scan.orphans} - known):
        prompt = rounds_dir / f"round-{number:02d}.prompt.md"
        output = rounds_dir / f"round-{number:02d}.md"
        partial = rounds_dir / f"round-{number:02d}.partial.md"
        views.append(
            {
                "n": number,
                "record": None,
                "prompt": prompt.read_text(encoding="utf-8") if prompt.exists() else "",
                "output": (
                    output.read_text(encoding="utf-8")
                    if output.exists()
                    else partial.read_text(encoding="utf-8")
                    if partial.exists()
                    else ""
                ),
                "orphan": True,
            }
        )
    return sorted(views, key=lambda item: item["n"])


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
            "rounds": _round_views(store, session_id),
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
):
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    sessions = store.list_sessions()
    view = next(
        item for item in _round_views(store, session_id) if item["n"] == round_n
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_round.html",
        context={
            "project": project,
            "session_id": session_id,
            "round": view,
            "sessions": sessions,
        },
    )

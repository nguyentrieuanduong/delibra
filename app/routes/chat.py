"""Project-level merged conversation view."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.models import Project
from app.routes.runs import start_run_fragment
from app.security import validate_field
from app.storage import ProjectStore, validate_id
from app.views import agent_views, effort_levels, round_views, selected_session


router = APIRouter()


def _timeline(request: Request, project_id: str, store: ProjectStore) -> list[dict]:
    items: list[dict] = []
    for session in store.list_sessions():
        active_key = request.app.state.manager.active_key(project_id, session.id)
        for round_view in round_views(store, session.id):
            record = round_view["record"]
            if record is None:
                continue
            items.append(
                {
                    "session": session,
                    "round": round_view,
                    "key": (
                        active_key
                        if active_key is not None and active_key.round_n == record.n
                        else None
                    ),
                }
            )
    return sorted(
        items,
        key=lambda item: (
            item["round"]["record"].started_at,
            item["session"].id,
            item["round"]["n"],
        ),
    )


def _sidebar_context(
    store: ProjectStore,
    sessions,
    selected_id: str | None,
    *,
    composer_oob: bool,
) -> dict:
    selected = selected_session(sessions, selected_id)
    return {
        "project": store.project,
        "sessions": sessions,
        "selected_session": selected,
        "agent_views": agent_views(store, sessions, selected),
        "effort_levels": effort_levels(),
        "composer_oob": composer_oob,
    }


def sidebar_response(
    request: Request,
    project: Project,
    *,
    selected_id: str | None,
    composer_oob: bool,
    headers: dict[str, str] | None = None,
):
    store = ProjectStore(project)
    sessions = store.list_sessions()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_agent_sidebar.html",
        context=_sidebar_context(
            store,
            sessions,
            selected_id,
            composer_oob=composer_oob,
        ),
        headers=headers,
    )


@router.get("/projects/{project_id}/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    project_id: str,
    agent: str | None = Query(None),
):
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    sessions = store.list_sessions()
    sidebar = _sidebar_context(store, sessions, agent, composer_oob=False)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "project": project,
            "sessions": sessions,
            "timeline": _timeline(request, project_id, store),
            "health": request.app.state.health,
            **sidebar,
        },
    )


@router.get("/projects/{project_id}/chat/sidebar", response_class=HTMLResponse)
async def chat_sidebar(
    request: Request,
    project_id: str,
    agent: str | None = Query(None),
):
    project = request.app.state.registry.get(project_id)
    return sidebar_response(
        request,
        project,
        selected_id=agent,
        composer_oob=False,
    )


@router.post(
    "/projects/{project_id}/chat/run",
    response_class=HTMLResponse,
    status_code=202,
)
async def chat_run(
    request: Request,
    project_id: str,
    session_id: str = Form(...),
    prompt: str = Form(...),
):
    validate_id(session_id, "session id")
    prompt = validate_field(prompt, "Prompt", maximum=100_000)
    project = request.app.state.registry.get(project_id)
    sessions = {item.id: item for item in ProjectStore(project).list_sessions()}
    if session_id not in sessions:
        raise HTTPException(
            status_code=422,
            detail="selected agent must belong to this project",
        )
    return await start_run_fragment(
        request,
        project_id,
        session_id,
        prompt,
        chat_view=True,
    )

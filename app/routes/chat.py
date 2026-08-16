"""Project-level merged conversation view."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.auto import ACTIVE_AUTO_STATUSES
from app.models import Project, SessionConfig
from app.project_routing import request_project
from app.routes.auto import (
    auto_setup_context,
    auto_status_context,
    project_auto_record,
)
from app.routes.runs import start_run_fragment
from app.security import validate_field
from app.storage import ConflictError, ProjectStore, validate_id
from app.urls import project_url
from app.views import (
    agent_views,
    conversation_round_views,
    effort_levels,
    round_views,
    selected_session,
)


router = APIRouter()


def _timeline(
    request: Request,
    project_id: str,
    store: ProjectStore,
    *,
    auto_numbers: dict[str, int],
) -> list[dict]:
    items: list[dict] = []
    for session in store.list_sessions():
        active_key = request.app.state.manager.active_key(project_id, session.id)
        for round_view in conversation_round_views(
            store,
            session.id,
            auto_numbers=auto_numbers,
        ):
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
    sessions: list[SessionConfig],
    selected_id: str | None,
    *,
    composer_oob: bool,
    auto_active: bool,
) -> dict:
    selected = selected_session(sessions, selected_id)
    return {
        "project": store.project,
        "sessions": sessions,
        "selected_session": selected,
        "agent_views": agent_views(store, sessions, selected),
        "effort_levels": effort_levels(),
        "composer_oob": composer_oob,
        "auto_active": auto_active,
    }


def sidebar_response(
    request: Request,
    project: Project,
    *,
    selected_id: str | None,
    composer_oob: bool,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
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
            auto_active=store.active_auto_run_id() is not None,
        ),
        headers=headers,
    )


@router.get("/projects/{project_id}/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    project_id: str,
    agent: str | None = Query(None),
    auto_setup: bool = Query(False),
):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    store = ProjectStore(project)
    sessions = store.list_sessions()
    auto_projection = project_auto_record(request, resolved_project_id)
    auto_record = auto_projection.record
    auto_active = (
        auto_record is not None
        and auto_record.status in ACTIVE_AUTO_STATUSES
    )
    setup_context = None
    if (
        auto_setup
        and not auto_active
        and auto_projection.warning is None
        and len(sessions) >= 2
    ):
        try:
            setup_context = auto_setup_context(request, resolved_project_id)
        except ConflictError:
            setup_context = None
    sidebar = _sidebar_context(
        store,
        sessions,
        agent,
        composer_oob=False,
        auto_active=auto_active,
    )
    auto_numbers = {
        record.id: record.number
        for record in auto_projection.history
        if record.number is not None
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "project": project,
            "sessions": sessions,
            "timeline": _timeline(
                request,
                resolved_project_id,
                store,
                auto_numbers=auto_numbers,
            ),
            "auto_history_runs": auto_projection.history,
            "auto_status": (
                auto_status_context(request, resolved_project_id, auto_record)
                if auto_record is not None
                else None
            ),
            "auto_active": auto_active,
            "auto_setup": setup_context,
            "auto_migration_warning": auto_projection.warning,
            "health": request.app.state.health,
            "pass_prompt_template": store.effective_pass_prompt_template(),
            **sidebar,
        },
    )


@router.get(
    "/projects/{project_id}/chat/timeline",
    response_class=HTMLResponse,
)
async def chat_timeline(request: Request, project_id: str) -> HTMLResponse:
    project = request_project(request, project_id)
    resolved_project_id = project.id
    store = ProjectStore(project)
    auto_numbers = store.auto_number_map_for_view()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_timeline.html",
        context={
            "project": project,
            "sessions": store.list_sessions(),
            "timeline": _timeline(
                request,
                resolved_project_id,
                store,
                auto_numbers=auto_numbers,
            ),
            "auto_active": store.active_auto_run_id() is not None,
            "pass_prompt_template": store.effective_pass_prompt_template(),
        },
    )


@router.get("/projects/{project_id}/chat/sidebar", response_class=HTMLResponse)
async def chat_sidebar(
    request: Request,
    project_id: str,
    agent: str | None = Query(None),
):
    project = request_project(request, project_id)
    return sidebar_response(
        request,
        project,
        selected_id=agent,
        composer_oob=False,
    )


@router.get("/projects/{project_id}/chat/select", response_class=HTMLResponse)
async def chat_select(
    request: Request,
    project_id: str,
    agent: str | None = Query(None),
) -> HTMLResponse:
    project = request_project(request, project_id)
    sessions = ProjectStore(project).list_sessions()
    selected = selected_session(sessions, agent)
    headers = (
        {
            "HX-Push-Url": project_url(
                project.name,
                f"/chat?agent={selected.id}",
            )
        }
        if selected is not None
        else None
    )
    return sidebar_response(
        request,
        project,
        selected_id=selected.id if selected is not None else None,
        composer_oob=True,
        headers=headers,
    )


@router.get(
    "/projects/{project_id}/sessions/{session_id}/rounds/{round_n}/focus",
    response_class=HTMLResponse,
)
async def round_focus(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
) -> HTMLResponse:
    validate_id(session_id, "session id")
    project = request_project(request, project_id)
    store = ProjectStore(project)
    session = store.load_session(session_id)
    focused = next(
        (
            item
            for item in round_views(store, session_id)
            if item["n"] == round_n and item["record"] is not None
        ),
        None,
    )
    if focused is None:
        raise HTTPException(status_code=404, detail="round not found")
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_round_focus.html",
        context={
            "project": project,
            "session": session,
            "session_id": session_id,
            "round": focused,
            "focus_dom_id": f"focus-{session_id}-{round_n}",
        },
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
    project = request_project(request, project_id)
    resolved_project_id = project.id
    sessions = {item.id: item for item in ProjectStore(project).list_sessions()}
    if session_id not in sessions:
        raise HTTPException(
            status_code=422,
            detail="selected agent must belong to this project",
        )
    return await start_run_fragment(
        request,
        resolved_project_id,
        session_id,
        prompt,
        chat_view=True,
    )

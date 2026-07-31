"""Round execution, cancellation, and browser-facing SSE transport."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
import math

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
from starlette.responses import Response

from app.auto import ACTIVE_AUTO_STATUSES
from app.models import RunKey, SourceDescriptor
from app.security import validate_field, validate_pass_prompt_template_field
from app.storage import ConflictError, NotFoundError, ProjectStore, validate_id
from app.views import round_dom_id


router = APIRouter()


def _event_data(kind: str, data: str) -> str:
    """Encode provider-controlled text before HTMX inserts it into the DOM."""
    escaped = escape(data, quote=True)
    if kind == "progress":
        return f'<span class="progress-item">{escaped}</span>'
    if kind in {"warning", "error"}:
        return f'<span class="{kind}">{escaped}</span>'
    return escaped


def validated_round_key(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
) -> RunKey:
    validate_id(project_id, "project id")
    validate_id(session_id, "session id")
    if round_n < 1:
        raise HTTPException(status_code=422, detail="Round must be positive")
    project = request.app.state.registry.get(project_id)
    config = ProjectStore(project).load_session(session_id)
    if not any(record.n == round_n for record in config.rounds):
        raise NotFoundError(f"round not found: {round_n}")
    return RunKey(project_id, session_id, round_n)


def render_timeout_controls(request: Request, key: RunKey) -> HTMLResponse:
    manager = request.app.state.manager
    timeout = manager.timeout_snapshot(key)
    active = manager.active_key(key.project_id, key.session_id) == key
    project = request.app.state.registry.get(key.project_id)
    store = ProjectStore(project)
    session = store.load_session(key.session_id)
    round_record = next(item for item in session.rounds if item.n == key.round_n)
    auto_record = None
    if (
        active
        and round_record.auto is not None
        and store.active_auto_run_id() == round_record.auto.auto_id
    ):
        candidate = store.load_auto_run(round_record.auto.auto_id)
        if (
            candidate.status in ACTIVE_AUTO_STATUSES
            and candidate.active_key == key
            and candidate.active_timeout == timeout
            and not candidate.stop_requested
        ):
            auto_record = candidate
    deadline = datetime.fromisoformat(timeout.deadline_at.replace("Z", "+00:00"))
    remaining_seconds = (
        max(0, math.ceil((deadline - datetime.now(UTC)).total_seconds()))
        if active
        else 0
    )
    maximum_addition_seconds = timeout.hard_cap_seconds - timeout.effective_seconds
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_timeout_controls.html",
        context={
            "project": project,
            "key": key,
            "timeout": timeout,
            "remaining_seconds": remaining_seconds,
            "maximum_addition_seconds": maximum_addition_seconds,
            "maximum_addition_minutes": min(240, maximum_addition_seconds // 60),
            "timeout_active": active,
            "auto_scope_available": auto_record is not None,
            "auto_future_timeout_seconds": (
                auto_record.future_turn_timeout_seconds
                if auto_record is not None
                else None
            ),
        },
    )


@router.get(
    "/projects/{project_id}/sessions/{session_id}/rounds/{round_n}/timeout",
    response_class=HTMLResponse,
)
async def timeout_fragment(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
) -> HTMLResponse:
    key = validated_round_key(request, project_id, session_id, round_n)
    return render_timeout_controls(request, key)


@router.post(
    "/projects/{project_id}/sessions/{session_id}/rounds/{round_n}/timeout/extend",
    response_class=HTMLResponse,
)
async def extend_timeout(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
    minutes: int = Form(...),
    scope: str = Form(...),
    expected_timeout_version: int = Form(...),
) -> Response:
    key = validated_round_key(request, project_id, session_id, round_n)
    try:
        result = await request.app.state.manager.extend_timeout(
            key,
            minutes,
            scope,
            expected_timeout_version,
        )
    except ConflictError as exc:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=409,
            headers={"HX-Trigger": "timeout-refresh"},
        )
    if result.auto_id is not None:
        request.app.state.auto_manager.publish_current_status(
            project_id,
            result.auto_id,
        )
    return render_timeout_controls(request, key)


async def start_run_fragment(
    request: Request,
    project_id: str,
    session_id: str,
    prompt: str,
    *,
    chat_view: bool,
):
    key = await request.app.state.manager.start(project_id, session_id, prompt)
    project = request.app.state.registry.get(project_id)
    session = ProjectStore(project).load_session(session_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_live.html",
        context={
            "project": project,
            "key": key,
            "session": session,
            "dom_id": round_dom_id(session_id, key.round_n),
            "chat_view": chat_view,
        },
        status_code=202,
    )


@router.post(
    "/projects/{project_id}/sessions/{session_id}/run",
    response_class=HTMLResponse,
    status_code=202,
)
async def run_session(
    request: Request,
    project_id: str,
    session_id: str,
    prompt: str = Form(...),
):
    prompt = validate_field(prompt, "Prompt", maximum=100_000)
    return await start_run_fragment(
        request,
        project_id,
        session_id,
        prompt,
        chat_view=False,
    )


@router.post(
    "/projects/{project_id}/sessions/{source_session_id}/pass",
    response_class=HTMLResponse,
    status_code=202,
)
async def pass_round(
    request: Request,
    project_id: str,
    source_session_id: str,
    source_round: int = Form(...),
    target_session_id: str = Form(...),
    pass_prompt_template: str = Form(...),
    view: str | None = None,
):
    validate_id(source_session_id, "source session id")
    validate_id(target_session_id, "target session id")
    if source_round < 1:
        raise HTTPException(status_code=422, detail="Source round must be positive")
    pass_prompt_template = validate_pass_prompt_template_field(pass_prompt_template)
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    sessions = {session.id: session for session in store.list_sessions()}
    source_config = sessions.get(source_session_id)
    if source_config is None:
        raise HTTPException(status_code=404, detail="source session not found")
    if target_session_id not in sessions:
        raise HTTPException(
            status_code=422,
            detail="target session must belong to the same project",
        )
    source_record = next(
        (item for item in source_config.rounds if item.n == source_round),
        None,
    )
    if source_record is None:
        raise HTTPException(status_code=404, detail="source round not found")
    if source_record.status != "complete":
        raise ConflictError("only a complete round can be passed")
    descriptor = SourceDescriptor(
        type="pass",
        from_session=source_session_id,
        from_round=source_round,
    )
    key = await request.app.state.manager.start(
        project_id,
        target_session_id,
        pass_prompt_template,
        source=descriptor,
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_live.html",
        context={
            "project": project,
            "key": key,
            "session": sessions[target_session_id],
            "dom_id": round_dom_id(target_session_id, key.round_n),
            "chat_view": view == "chat",
        },
        status_code=202,
    )


@router.post(
    "/projects/{project_id}/sessions/{session_id}/rounds/{round_n}/retry",
    response_class=HTMLResponse,
    status_code=202,
)
async def retry_round(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
    view: str | None = None,
):
    validate_id(project_id, "project id")
    validate_id(session_id, "session id")
    if round_n < 1:
        raise HTTPException(status_code=422, detail="Round must be positive")
    key = await request.app.state.manager.retry(project_id, session_id, round_n)
    project = request.app.state.registry.get(project_id)
    session = ProjectStore(project).load_session(session_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_live.html",
        context={
            "project": project,
            "key": key,
            "session": session,
            "dom_id": round_dom_id(session_id, key.round_n),
            "chat_view": view == "chat",
        },
        status_code=202,
    )


@router.get("/projects/{project_id}/sessions/{session_id}/rounds/{round_n}/stream")
async def round_stream(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    validate_id(project_id, "project id")
    validate_id(session_id, "session id")
    if round_n < 1:
        raise HTTPException(status_code=422, detail="Round must be positive")
    project = request.app.state.registry.get(project_id)
    config = ProjectStore(project).load_session(session_id)
    if not any(record.n == round_n for record in config.rounds):
        raise HTTPException(status_code=404, detail="round not found")
    key = RunKey(project_id, session_id, round_n)

    async def events():
        async for event in request.app.state.manager.subscribe(key, last_event_id):
            data = (
                round_dom_id(session_id, round_n)
                if event.kind == "reset"
                else event.data
            )
            yield {
                "id": str(event.event_id),
                "event": event.kind,
                "data": _event_data(event.kind, data),
            }

    return EventSourceResponse(events(), ping=15)


@router.post("/projects/{project_id}/sessions/{session_id}/cancel")
async def cancel_session(request: Request, project_id: str, session_id: str):
    validate_id(project_id, "project id")
    validate_id(session_id, "session id")
    project = request.app.state.registry.get(project_id)
    ProjectStore(project).load_session(session_id)
    manager = request.app.state.manager
    key = manager.active_key(project_id, session_id)
    if key is None:
        raise HTTPException(status_code=409, detail="session has no running agent")
    await manager.cancel(key)
    return {"status": "cancelled", "round": key.round_n}

"""Round execution, cancellation, and browser-facing SSE transport."""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from app.models import RunKey, SourceDescriptor
from app.security import validate_field
from app.storage import ConflictError, ProjectStore, validate_id


router = APIRouter()


def _event_data(kind: str, data: str) -> str:
    """Encode provider-controlled text before HTMX inserts it into the DOM."""
    escaped = escape(data, quote=True)
    if kind == "progress":
        return f'<span class="progress-item">{escaped}</span>'
    if kind in {"warning", "error"}:
        return f'<span class="{kind}">{escaped}</span>'
    return escaped


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
    key = await request.app.state.manager.start(project_id, session_id, prompt)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_live.html",
        context={"key": key},
        status_code=202,
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
    instruction: str = Form(""),
):
    validate_id(source_session_id, "source session id")
    validate_id(target_session_id, "target session id")
    if source_round < 1:
        raise HTTPException(status_code=422, detail="Source round must be positive")
    instruction = validate_field(
        instruction,
        "Instruction",
        maximum=10_000,
        allow_empty=True,
    )
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
        instruction,
        source=descriptor,
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_live.html",
        context={"key": key},
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
            data = str(round_n) if event.kind == "reset" else event.data
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

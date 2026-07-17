"""Round execution, cancellation, and browser-facing SSE transport."""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from app.models import RunKey
from app.security import validate_field


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


@router.get("/projects/{project_id}/sessions/{session_id}/rounds/{round_n}/stream")
async def round_stream(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    key = RunKey(project_id, session_id, round_n)

    async def events():
        async for event in request.app.state.manager.subscribe(key, last_event_id):
            yield {
                "id": str(event.event_id),
                "event": event.kind,
                "data": _event_data(event.kind, event.data),
            }

    return EventSourceResponse(events(), ping=15)


@router.post("/projects/{project_id}/sessions/{session_id}/cancel")
async def cancel_session(request: Request, project_id: str, session_id: str):
    manager = request.app.state.manager
    key = manager.active_key(project_id, session_id)
    if key is None:
        raise HTTPException(status_code=409, detail="session has no running agent")
    await manager.cancel(key)
    return {"status": "cancelled", "round": key.round_n}

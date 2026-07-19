"""Project-scoped Auto setup, status, stream, and Stop routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Header, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from app.auto import ACTIVE_AUTO_STATUSES
from app.models import AutoRunRecord
from app.security import validate_field
from app.storage import ProjectStore, StorageError, validate_id


router = APIRouter()


def project_auto_record(
    request: Request,
    project_id: str,
) -> AutoRunRecord | None:
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    active_id = store.active_auto_run_id()
    if active_id is not None:
        return store.load_auto_run(active_id)
    return request.app.state.auto_manager.latest(project_id)


def _decode_text(contents: bytes, label: str) -> str:
    try:
        return contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageError(f"{label} is not valid UTF-8") from exc


def _durable_topic(request: Request, store: ProjectStore) -> str | None:
    candidates = []
    for session in store.list_sessions():
        for record in session.rounds:
            if (
                record.source.type == "user"
                and record.retry_of is None
                and record.auto is None
            ):
                candidates.append((record.started_at, session.id, record.n))
    for _, session_id, round_n in sorted(candidates):
        try:
            prompt = store.load_round_artifact(
                session_id,
                round_n,
                "prompt",
                request.app.state.settings.request_body_limit,
            )
        except StorageError:
            continue
        return _decode_text(prompt, "recorded prompt")

    for record in store.list_auto_runs():
        try:
            topic = store.load_auto_artifact(
                record.id,
                record.topic,
                request.app.state.settings.request_body_limit,
            )
        except StorageError:
            continue
        return _decode_text(topic, "Auto topic")
    return None


def auto_status_context(
    request: Request,
    project_id: str,
    record: AutoRunRecord,
    *,
    clear_setup: bool = False,
) -> dict:
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    topic = _decode_text(
        store.load_auto_artifact(
            record.id,
            record.topic,
            request.app.state.settings.request_body_limit,
        ),
        "Auto topic",
    )
    participants = {item.session_id: item for item in record.participants}
    preparations = []
    for turn in record.preparations:
        output = store.load_round_artifact(
            turn.session_id,
            turn.round_n,
            "output",
            request.app.state.settings.captured_output_limit,
        )
        preparations.append(
            {
                "turn": turn,
                "participant": participants[turn.session_id],
                "output": _decode_text(output, "Auto preparation output"),
            }
        )
    current_participant = (
        record.participants[record.next_participant]
        if record.status in ACTIVE_AUTO_STATUSES
        and 0 <= record.next_participant < len(record.participants)
        else None
    )
    return {
        "project": project,
        "auto": record,
        "auto_active": record.status in ACTIVE_AUTO_STATUSES,
        "topic": topic,
        "preparations": preparations,
        "preparing_number": min(
            len(record.preparations) + 1,
            len(record.participants),
        ),
        "hidden_preparation_key": (
            record.active_key if record.status == "preparing" else None
        ),
        "current_participant": current_participant,
        "clear_auto_setup": clear_setup,
    }


def _status_response(
    request: Request,
    project_id: str,
    record: AutoRunRecord,
    *,
    status_code: int = 200,
    clear_setup: bool = False,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_auto_status.html",
        context=auto_status_context(
            request,
            project_id,
            record,
            clear_setup=clear_setup,
        ),
        status_code=status_code,
    )


@router.get("/projects/{project_id}/auto/setup", response_class=HTMLResponse)
async def auto_setup(request: Request, project_id: str) -> HTMLResponse:
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    sessions = sorted(
        store.list_sessions(),
        key=lambda item: (item.name.casefold(), item.id),
    )
    topic = _durable_topic(request, store)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_auto_setup.html",
        context={
            "project": project,
            "sessions": sessions,
            "topic": topic or "",
            "topic_source": "durable" if topic is not None else "composer",
        },
    )


@router.post(
    "/projects/{project_id}/auto-runs",
    response_class=HTMLResponse,
    status_code=202,
)
async def start_auto(
    request: Request,
    project_id: str,
    topic: str = Form(...),
    participant_id: list[str] = Form(...),
    agreement_policy: str = Form(...),
    max_cycles: int = Form(...),
    prepare_first: bool = Form(False),
) -> HTMLResponse:
    topic = validate_field(topic, "Topic", maximum=100_000)
    record = await request.app.state.auto_manager.create(
        project_id,
        topic=topic,
        participant_ids=participant_id,
        agreement_policy=agreement_policy,
        max_cycles=max_cycles,
        preparation_enabled=prepare_first,
    )
    return _status_response(
        request,
        project_id,
        request.app.state.auto_manager.get(project_id, record.id),
        status_code=202,
        clear_setup=True,
    )


@router.get(
    "/projects/{project_id}/auto-runs/{auto_id}",
    response_class=HTMLResponse,
)
async def auto_status(
    request: Request,
    project_id: str,
    auto_id: str,
) -> HTMLResponse:
    validate_id(auto_id, "Auto run id")
    return _status_response(
        request,
        project_id,
        request.app.state.auto_manager.get(project_id, auto_id),
    )


@router.get("/projects/{project_id}/auto-runs/{auto_id}/stream")
async def auto_stream(
    request: Request,
    project_id: str,
    auto_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    validate_id(auto_id, "Auto run id")
    request.app.state.auto_manager.get(project_id, auto_id)

    async def events():
        async for event in request.app.state.auto_manager.subscribe(
            project_id,
            auto_id,
            last_event_id,
        ):
            yield {
                "id": str(event.event_id),
                "event": event.kind,
                "data": json.dumps(
                    {"auto_id": event.auto_id, "status": event.status},
                    separators=(",", ":"),
                ),
            }

    return EventSourceResponse(events())


@router.post(
    "/projects/{project_id}/auto-runs/{auto_id}/stop",
    response_class=HTMLResponse,
)
async def stop_auto(
    request: Request,
    project_id: str,
    auto_id: str,
) -> HTMLResponse:
    validate_id(auto_id, "Auto run id")
    record = await request.app.state.auto_manager.stop(project_id, auto_id)
    return _status_response(request, project_id, record)

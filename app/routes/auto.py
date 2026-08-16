"""Project-scoped Auto setup, status, stream, and Stop routes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
import json

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse
from starlette.responses import Response

from app.auto import ACTIVE_AUTO_STATUSES
from app.models import AutoRunRecord, AutoTurn, Project, SessionConfig
from app.project_routing import request_project
from app.security import validate_field
from app.storage import ProjectStore, StorageError, parse_auto_reference
from app.urls import project_url


router = APIRouter()


@dataclass(frozen=True)
class ProjectAutoProjection:
    record: AutoRunRecord | None
    warning: str | None
    history: tuple[AutoRunRecord, ...]
    migration_complete: bool


def _load_auto_reference(
    store: ProjectStore,
    value: str,
) -> tuple[AutoRunRecord, int | str]:
    try:
        reference = parse_auto_reference(value)
        return store.load_auto_run_reference(value), reference
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Auto run reference is invalid",
        ) from exc


def project_auto_record(
    request: Request,
    project_id: str,
) -> ProjectAutoProjection:
    store = ProjectStore(request.app.state.registry.get(project_id))
    status = store.auto_migration_status()
    history = auto_history_records(status.readable_records)
    active_id = store.active_auto_run_id()
    warning = status.issues[0].message if status.issues else None
    if active_id is not None:
        by_id = {record.id: record for record in history}
        record = by_id.get(active_id)
        return ProjectAutoProjection(
            record,
            warning if record is None else None,
            tuple(history),
            status.complete,
        )
    return ProjectAutoProjection(
        history[0] if history else None,
        warning,
        tuple(history),
        status.complete,
    )


def _decode_text(contents: bytes, label: str) -> str:
    try:
        return contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageError(f"{label} is not valid UTF-8") from exc


def auto_history_records(
    records: Iterable[AutoRunRecord],
) -> list[AutoRunRecord]:
    records = list(records)
    if any(record.number is None for record in records):
        return sorted(
            records,
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
    return sorted(
        records,
        key=lambda item: item.number or 0,
        reverse=True,
    )


def _preparation_focus_available(
    request: Request,
    store: ProjectStore,
    auto_id: str,
    turn: AutoTurn,
) -> bool:
    try:
        session = store.load_session(turn.session_id)
        round_record = next(
            (item for item in session.rounds if item.n == turn.round_n),
            None,
        )
        if (
            round_record is None
            or round_record.auto is None
            or round_record.auto.auto_id != auto_id
            or round_record.auto.phase != "preparation"
        ):
            return False
        prompt = store.load_round_artifact(
            turn.session_id,
            turn.round_n,
            "prompt",
            request.app.state.settings.request_body_limit,
        )
        output = store.load_round_artifact(
            turn.session_id,
            turn.round_n,
            "output",
            request.app.state.settings.captured_output_limit,
        )
        _decode_text(prompt, "Auto preparation prompt")
        _decode_text(output, "Auto preparation output")
    except StorageError:
        return False
    return sha256(output).hexdigest() == turn.output_sha256


def _auto_material_context(
    request: Request,
    store: ProjectStore,
    record: AutoRunRecord,
) -> dict:
    try:
        topic = _decode_text(
            store.load_auto_artifact(
                record.id,
                record.topic,
                request.app.state.settings.request_body_limit,
            ),
            "Auto topic",
        )
    except StorageError:
        topic = None
    participants = {item.session_id: item for item in record.participants}
    preparations = []
    for turn in record.preparations:
        try:
            output = _decode_text(
                store.load_auto_preparation(
                    record.id,
                    turn.session_id,
                    turn.output_sha256,
                    request.app.state.settings.captured_output_limit,
                ),
                "Auto preparation output",
            )
        except StorageError:
            output = None
        preparations.append(
            {
                "turn": turn,
                "participant": participants[turn.session_id],
                "output": output,
                "output_unavailable": output is None,
                "focus_available": (
                    output is not None
                    and _preparation_focus_available(
                        request,
                        store,
                        record.id,
                        turn,
                    )
                ),
            }
        )
    return {"topic": topic, "preparations": preparations}


def auto_history_detail_context(
    request: Request,
    project: Project,
    store: ProjectStore,
    record: AutoRunRecord,
) -> dict:
    return {
        "project": project,
        "auto": record,
        **_auto_material_context(request, store, record),
    }


def _durable_topic(
    request: Request,
    store: ProjectStore,
    auto_records: Iterable[AutoRunRecord],
) -> str | None:
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

    for record in reversed(tuple(auto_records)):
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
    refresh_history: bool = False,
) -> dict:
    project = request_project(request, project_id)
    store = ProjectStore(project)
    material = _auto_material_context(request, store, record)
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
        **material,
        "preparing_number": min(
            len(record.preparations) + 1,
            len(record.participants),
        ),
        "hidden_preparation_key": (
            record.active_key if record.status == "preparing" else None
        ),
        "current_participant": current_participant,
        "clear_auto_setup": clear_setup,
        "auto_history_oob_runs": (
            auto_history_records(
                store.auto_migration_status().readable_records
            )
            if refresh_history
            else None
        ),
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
            refresh_history=True,
        ),
        status_code=status_code,
    )


def projected_auto_setup_context(
    request: Request,
    project: Project,
    store: ProjectStore,
    sessions: list[SessionConfig],
    auto_records: Iterable[AutoRunRecord],
) -> dict:
    ordered_sessions = sorted(
        sessions,
        key=lambda item: (item.name.casefold(), item.id),
    )
    topic = _durable_topic(request, store, auto_records)
    return {
        "project": project,
        "sessions": ordered_sessions,
        "topic": topic or "",
        "topic_source": "durable" if topic is not None else "composer",
    }


def auto_setup_context(request: Request, project_id: str) -> dict:
    project = request_project(request, project_id)
    store = ProjectStore(project)
    status = store.require_auto_migration_complete()
    return projected_auto_setup_context(
        request,
        project,
        store,
        store.list_sessions(),
        auto_history_records(status.readable_records),
    )


@router.get("/projects/{project_id}/auto/setup", response_class=HTMLResponse)
async def auto_setup(request: Request, project_id: str) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_auto_setup.html",
        context=auto_setup_context(request, project_id),
    )


@router.get(
    "/projects/{project_id}/auto-runs/{auto_id}/history",
    response_class=HTMLResponse,
)
async def auto_history_detail(
    request: Request,
    project_id: str,
    auto_id: str,
) -> Response:
    project = request_project(request, project_id)
    store = ProjectStore(project)
    record, reference = _load_auto_reference(store, auto_id)
    if isinstance(reference, str) and record.number is not None:
        return RedirectResponse(
            project_url(
                project.name,
                f"/auto-runs/{record.number}/history",
            ),
            status_code=302,
        )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_auto_history_detail.html",
        context=auto_history_detail_context(request, project, store, record),
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
    project = request_project(request, project_id)
    resolved_project_id = project.id
    record = await request.app.state.auto_manager.create(
        resolved_project_id,
        topic=topic,
        participant_ids=participant_id,
        agreement_policy=agreement_policy,
        max_cycles=max_cycles,
        preparation_enabled=prepare_first,
    )
    return _status_response(
        request,
        resolved_project_id,
        request.app.state.auto_manager.get(resolved_project_id, record.id),
        status_code=202,
        clear_setup=True,
    )


@router.api_route(
    "/projects/{project_id}/auto-runs/{auto_id}",
    methods=["GET", "HEAD"],
    response_class=HTMLResponse,
)
async def auto_status(
    request: Request,
    project_id: str,
    auto_id: str,
) -> Response:
    project = request_project(request, project_id)
    resolved_project_id = project.id
    store = ProjectStore(project)
    record, reference = _load_auto_reference(store, auto_id)
    if isinstance(reference, str):
        if record.number is None:
            store.require_auto_migration_complete()
        assert record.number is not None
        return RedirectResponse(
            project_url(
                project.name,
                f"/auto-runs/{record.number}",
            ),
            status_code=302,
        )
    return _status_response(
        request,
        resolved_project_id,
        request.app.state.auto_manager.get(resolved_project_id, record.id),
    )


@router.get("/projects/{project_id}/auto-runs/{auto_id}/stream")
async def auto_stream(
    request: Request,
    project_id: str,
    auto_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    project = request_project(request, project_id)
    resolved_project_id = project.id
    record, _ = _load_auto_reference(ProjectStore(project), auto_id)
    request.app.state.auto_manager.get(resolved_project_id, record.id)

    async def events():
        async for event in request.app.state.auto_manager.subscribe(
            resolved_project_id,
            record.id,
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
    project = request_project(request, project_id)
    resolved_project_id = project.id
    record, _ = _load_auto_reference(ProjectStore(project), auto_id)
    record = await request.app.state.auto_manager.stop(
        resolved_project_id,
        record.id,
    )
    return _status_response(request, resolved_project_id, record)

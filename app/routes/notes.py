"""The Chat page's notes box: guidance the user sends to an Auto run's agents."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.models import Project
from app.notes import NOTE_ID, NOTE_MAX_CHARS, NoteStore, chat_note_store
from app.project_routing import request_project
from app.routes.auto import continue_cursor, project_auto_record
from app.security import require_loopback_client, validate_field
from app.storage import (
    TERMINAL_AUTO_STATUSES,
    ConflictError,
    NotFoundError,
    ProjectStore,
    validate_id,
)
from app.views import accepts_notes, auto_notes_context, chat_notes_context


router = APIRouter()


def _render_list(request: Request, project: Project) -> HTMLResponse:
    record = project_auto_record(request, project.id).record
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_auto_notes_list.html",
        # Out-of-band, so the list is found by id when the response lands: a
        # status update may have replaced the element this request started from.
        context={
            "project": project,
            **auto_notes_context(ProjectStore(project), record),
            "notes_oob": True,
        },
    )


@router.post("/projects/{project_id}/auto-notes", response_class=HTMLResponse)
async def post_note(
    request: Request,
    project_id: str,
    text: str = Form(...),
    target: str = Form("auto"),
    session_id: str | None = Form(None),
) -> HTMLResponse:
    text = validate_field(text, "Note", maximum=NOTE_MAX_CHARS)
    project = request_project(request, project_id)
    if target == "chat":
        return await _post_chat_note(request, project, session_id or "", text)
    async with request.app.state.locks.project_lock(project.id):
        record = project_auto_record(request, project.id).record
        if record is not None and record.kind == "build":
            # Build agents have a shell and a note overrides the topic, so the
            # rule that guards starting or continuing a build run applies.
            require_loopback_client(request)
        if not accepts_notes(record):
            raise ConflictError("no Auto run accepts notes; start Auto first")
        store = ProjectStore(project)
        # An ended run's note waits for Continue Auto. If the run can never
        # continue, the note is still kept, marked undelivered, not refused.
        undelivered = (
            record.status in TERMINAL_AUTO_STATUSES
            and continue_cursor(store, record) is None
        )
        NoteStore(store.root).add(text, auto_run=record.number, undelivered=undelivered)
    return _render_list(request, project)


@router.post("/projects/{project_id}/auto-notes/{note_id}/delete", response_class=HTMLResponse)
async def delete_note(request: Request, project_id: str, note_id: str) -> HTMLResponse:
    if not NOTE_ID.fullmatch(note_id):
        raise NotFoundError("note not found")
    project = request_project(request, project_id)
    async with request.app.state.locks.project_lock(project.id):
        NoteStore(ProjectStore(project).root).delete(note_id)
    return _render_list(request, project)


def _render_chat_list(request: Request, project: Project, session_id: str) -> HTMLResponse:
    store = ProjectStore(project)
    record = project_auto_record(request, project.id).record
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_chat_notes_list.html",
        # Out-of-band, like the Auto list: found by id when the response lands.
        context={
            "project": project,
            **chat_notes_context(
                store,
                store.load_session(session_id),
                auto_active=store.active_auto_run_id() is not None,
                auto_open=accepts_notes(record),
            ),
            "notes_oob": True,
        },
    )


async def _post_chat_note(
    request: Request, project: Project, session_id: str, text: str
) -> HTMLResponse:
    """A note to the selected agent's running chat reply (design B)."""

    validate_id(session_id, "session id")
    async with request.app.state.locks.sessions(project.id, [session_id]):
        store = ProjectStore(project)
        session = store.load_session(session_id)
        # The page sent "chat" because it showed a reply; if that reply has
        # ended, refuse rather than send the note somewhere else.
        if not request.app.state.manager.replying(project.id, session_id):
            raise ConflictError(f"{session.name} is not replying; send a message instead")
        chat_note_store(store, session_id).add(text, name=session.name)
    return _render_chat_list(request, project, session_id)


@router.post(
    "/projects/{project_id}/sessions/{session_id}/chat-notes/{note_id}/delete",
    response_class=HTMLResponse,
)
async def delete_chat_note(
    request: Request, project_id: str, session_id: str, note_id: str
) -> HTMLResponse:
    validate_id(session_id, "session id")
    if not NOTE_ID.fullmatch(note_id):
        raise NotFoundError("note not found")
    project = request_project(request, project_id)
    async with request.app.state.locks.sessions(project.id, [session_id]):
        chat_note_store(ProjectStore(project), session_id).delete(note_id)
    return _render_chat_list(request, project, session_id)

"""The Chat page's notes box: guidance the user sends to an Auto run's agents."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.models import Project
from app.notes import NOTE_ID, NOTE_MAX_CHARS, NoteStore
from app.project_routing import request_project
from app.routes.auto import project_auto_record
from app.security import require_loopback_client, validate_field
from app.storage import ConflictError, NotFoundError, ProjectStore
from app.views import accepts_notes, auto_notes_context


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
async def post_note(request: Request, project_id: str, text: str = Form(...)) -> HTMLResponse:
    text = validate_field(text, "Note", maximum=NOTE_MAX_CHARS)
    project = request_project(request, project_id)
    async with request.app.state.locks.project_lock(project.id):
        record = project_auto_record(request, project.id).record
        if record is not None and record.kind == "build":
            # Build agents have a shell and a note overrides the topic, so the
            # rule that guards starting or continuing a build run applies.
            require_loopback_client(request)
        if not accepts_notes(record):
            if record is not None and record.number is not None:
                raise ConflictError(
                    f"Auto {record.number} has ended ({record.status}); "
                    "continue it before sending a note"
                )
            raise ConflictError("no Auto run accepts notes; start Auto first")
        NoteStore(ProjectStore(project).root).add(text, auto_run=record.number)
    return _render_list(request, project)


@router.post("/projects/{project_id}/auto-notes/{note_id}/delete", response_class=HTMLResponse)
async def delete_note(request: Request, project_id: str, note_id: str) -> HTMLResponse:
    if not NOTE_ID.fullmatch(note_id):
        raise NotFoundError("note not found")
    project = request_project(request, project_id)
    async with request.app.state.locks.project_lock(project.id):
        NoteStore(ProjectStore(project).root).delete(note_id)
    return _render_list(request, project)

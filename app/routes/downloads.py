"""The Chat page's Downloads panel: list, actions, live stream."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sse_starlette.sse import EventSourceResponse

from app.download_store import DownloadStore
from app.models import Project
from app.project_routing import request_project
from app.security import is_loopback_client
from app.storage import NotFoundError, ProjectStore, StorageError

router = APIRouter()
_DOWNLOAD_ID = re.compile(r"D[1-9][0-9]{0,8}")


def downloads_panel_context(request: Request, project: Project) -> dict:
    store = ProjectStore(project)
    records = list(reversed(DownloadStore(store.root).load().requests))
    return {
        "project": project,
        "downloads": records,
        "awaiting_count": sum(1 for r in records if r.state == "awaiting"),
        "downloads_enabled": store.coding_policy().downloads.enabled,
    }


def _require_local(request: Request) -> None:
    # Downloads reach any public host from this machine, outside the sandbox.
    if not is_loopback_client(request):
        raise HTTPException(
            status_code=403,
            detail="Only this machine may act on downloads or reveal their URLs",
        )


def _render(request: Request, name: str, context: dict) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name=name, context=context
    )


def _record(project: Project, rid: str):
    if not _DOWNLOAD_ID.fullmatch(rid):
        raise NotFoundError("download not found")
    for record in DownloadStore(ProjectStore(project).root).load().requests:
        if record.id == rid:
            return record
    raise NotFoundError("download not found")


@router.get("/projects/{project_id}/downloads", response_class=HTMLResponse)
async def downloads_panel(request: Request, project_id: str) -> HTMLResponse:
    project = request_project(request, project_id)
    return _render(request, "_downloads_panel.html", downloads_panel_context(request, project))


@router.get("/projects/{project_id}/downloads/list", response_class=HTMLResponse)
async def downloads_list(request: Request, project_id: str) -> HTMLResponse:
    project = request_project(request, project_id)
    return _render(request, "_downloads_list.html", downloads_panel_context(request, project))


@router.get("/projects/{project_id}/downloads/stream")
async def downloads_stream(request: Request, project_id: str) -> EventSourceResponse:
    project = request_project(request, project_id)
    manager = request.app.state.download_manager

    async def events():
        # The first event is the current revision (plan review S1).
        async for revision in manager.subscribe(project.id):
            yield {"event": "downloads", "data": json.dumps({"revision": revision})}

    return EventSourceResponse(events(), ping=15)


@router.get("/projects/{project_id}/downloads/{rid}/url", response_class=PlainTextResponse)
async def download_full_url(request: Request, project_id: str, rid: str) -> PlainTextResponse:
    """The raw URL and the model's note, never on ordinary surfaces (B3)."""

    _require_local(request)
    record = _record(request_project(request, project_id), rid)
    note = (
        f"\n\nModel check note (untrusted model text): {record.check_note}"
        if record.check_note
        else ""
    )
    return PlainTextResponse(record.url + note)


async def _act(request: Request, project_id: str, rid: str | None, action: str) -> HTMLResponse:
    _require_local(request)
    project = request_project(request, project_id)
    store = ProjectStore(project)
    manager = request.app.state.download_manager
    try:
        if rid is None:
            manager.approve_all(store.project_path, store.root, project.id)
        else:
            _record(project, rid)
            getattr(manager, action)(store.project_path, store.root, rid, project.id)
    except NotFoundError:
        raise
    except StorageError as exc:
        return HTMLResponse(str(exc), status_code=409)
    return _render(request, "_downloads_list.html", downloads_panel_context(request, project))


@router.post("/projects/{project_id}/downloads/approve-all", response_class=HTMLResponse)
async def approve_all_downloads(request: Request, project_id: str) -> HTMLResponse:
    return await _act(request, project_id, None, "approve_all")


@router.post("/projects/{project_id}/downloads/{rid}/approve", response_class=HTMLResponse)
async def approve_download(request: Request, project_id: str, rid: str) -> HTMLResponse:
    return await _act(request, project_id, rid, "approve")


@router.post("/projects/{project_id}/downloads/{rid}/refuse", response_class=HTMLResponse)
async def refuse_download(request: Request, project_id: str, rid: str) -> HTMLResponse:
    return await _act(request, project_id, rid, "refuse")


@router.post("/projects/{project_id}/downloads/{rid}/allow", response_class=HTMLResponse)
async def allow_download(request: Request, project_id: str, rid: str) -> HTMLResponse:
    return await _act(request, project_id, rid, "allow")


@router.post("/projects/{project_id}/downloads/{rid}/retry", response_class=HTMLResponse)
async def retry_download(request: Request, project_id: str, rid: str) -> HTMLResponse:
    return await _act(request, project_id, rid, "retry")

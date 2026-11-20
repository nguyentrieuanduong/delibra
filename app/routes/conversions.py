"""The Chat page's Conversions panel: requested files and downloads, the queue, Cancel."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
import time
import unicodedata

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.convert_store import ACTIVE, ConversionStore
from app.download_manager import conversion_state
from app.download_store import DownloadStore
from app.models import Project
from app.project_routing import request_project
from app.security import require_loopback_client
from app.storage import NotFoundError, ProjectStore, StorageError

router = APIRouter()
_CONVERSION_ID = re.compile(r"[CD][1-9][0-9]{0,8}")
CONVERSIONS_SHOWN = 50
RETRYABLE = ("cancelled", "failed")  # a download's conversion; a requested file is simply asked for again


def plain_text(value: str) -> str:
    """`value` with control and format characters (newlines, escapes, bidi marks) as
    spaces: for the Cancel confirmation and the button's label (security review)."""

    return "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in value)


@dataclass(frozen=True)
class ConversionRow:
    id: str
    origin: str  # "request" (an agent asked for a project file) or "download"
    path: str
    state: str
    reason: str | None
    output: str | None
    updated_at: str
    position: int | None = None  # 1 is next; queued only
    started_at: float | None = None  # epoch seconds; converting only
    limit: float | None = None  # seconds, once the file type is known
    cancelling: bool = False  # cancelled; its worker has not ended yet

    @property
    def shown_path(self) -> str:
        return plain_text(self.path)

    @property
    def elapsed(self) -> float:
        """Seconds since it took the slot, to 0.1 s: the page counts on from it."""

        return round(max(0.0, time.time() - self.started_at), 1) if self.started_at is not None else 0.0


def _rows(store: ProjectStore) -> tuple[list[ConversionRow] | None, list[ConversionRow] | None]:
    """(requested files, download conversions); None for a store that is unreadable."""

    try:
        requested = [
            ConversionRow(r.id, "request", r.path, r.state, r.reason, r.output, r.updated_at)
            for r in ConversionStore(store.root).load()
        ]
    except StorageError:
        requested = None
    try:
        downloads = [
            ConversionRow(r.id, "download", r.path or "", r.convert_state, r.convert_reason, r.converted_path,
                          r.updated_at)
            for r in DownloadStore(store.root).load().requests if r.convert_state is not None
        ]
    except StorageError:
        downloads = None
    return requested, downloads


def _downloads_revision(store: ProjectStore) -> tuple[int, str]:
    book = DownloadStore(store.root).load()
    return book.revision, book.epoch


def conversions_revision(request: Request, store: ProjectStore) -> tuple[int, str]:
    """One number for the panel: both stores and the server's queue each only grow, so
    any change makes the sum newer; the epoch changes when any of them restarts."""

    manager = request.app.state.download_manager
    parts = [manager.conversion_queue_revision() if manager is not None else (0, "")]
    for read in (_downloads_revision, lambda s: ConversionStore(s.root).revision()):
        try:
            parts.append(read(store))
        except StorageError:
            parts.append((0, ""))
    return sum(number for number, _epoch in parts), ".".join(epoch for _number, epoch in parts)


def conversions_panel_context(request: Request, project: Project) -> dict:
    store = ProjectStore(project)
    requested, downloads = _rows(store)
    manager = request.app.state.download_manager
    jobs = manager.conversion_jobs(store.root) if manager is not None else {}
    rows = []
    for row in [*(requested or []), *(downloads or [])]:
        job = jobs.get(row.id) if row.state in ACTIVE else None
        if job is not None:
            row = replace(row, position=job.position, started_at=job.started_at, limit=job.limit,
                          cancelling=job.cancelling)
        rows.append(row)
    active = sorted((r for r in rows if r.state in ACTIVE),
                    key=lambda r: (r.state != "converting", r.position or 0))
    ended = sorted((r for r in rows if r.state not in ACTIVE), key=lambda r: r.updated_at, reverse=True)
    revision, epoch = conversions_revision(request, store)
    return {
        "project": project,
        "rows": [*active, *ended][:CONVERSIONS_SHOWN],
        "converting_count": sum(1 for r in active if r.state == "converting"),
        "queued_count": sum(1 for r in active if r.state == "queued"),
        "requests_unreadable": requested is None,
        "downloads_unreadable": downloads is None,
        "convert_enabled": store.coding_policy().downloads.convert,
        "slot_busy": manager is not None and manager.conversion_slot_busy_elsewhere(),
        "retryable": RETRYABLE,
        "revision": revision,
        "epoch": epoch,
    }


def _render_list(request: Request, project: Project) -> HTMLResponse:
    # The summary's counts sit outside the list: refresh them out of band.
    context = {"conversions_panel": conversions_panel_context(request, project), "project": project,
               "oob_summary": True}
    return request.app.state.templates.TemplateResponse(
        request=request, name="_conversions_list.html", context=context
    )


@router.get("/projects/{project_id}/conversions/list", response_class=HTMLResponse)
async def conversions_list(request: Request, project_id: str) -> HTMLResponse:
    return _render_list(request, request_project(request, project_id))


def _known(store: ProjectStore, cid: str) -> bool:
    return _CONVERSION_ID.fullmatch(cid) is not None and conversion_state(store.root, cid) is not None


@router.post("/projects/{project_id}/conversions/{cid}/cancel", response_class=HTMLResponse)
async def cancel_conversion(request: Request, project_id: str, cid: str):
    # This machine only, the same rule as every Downloads action.
    require_loopback_client(request, "Only this machine may cancel conversions")
    project = request_project(request, project_id)
    store = ProjectStore(project)
    try:
        if not _known(store, cid):
            raise NotFoundError("conversion not found")
        await request.app.state.download_manager.cancel_conversion(store.root, cid)
    except NotFoundError:
        raise
    except StorageError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    return _render_list(request, project)


@router.post("/projects/{project_id}/conversions/{rid}/retry", response_class=HTMLResponse)
async def retry_conversion(request: Request, project_id: str, rid: str):
    """Convert a download again after its conversion was cancelled or failed."""

    require_loopback_client(request, "Only this machine may retry conversions")
    project = request_project(request, project_id)
    store = ProjectStore(project)
    try:
        if not rid.startswith("D") or not _known(store, rid):
            raise NotFoundError("conversion not found")
        request.app.state.download_manager.retry_conversion(store.project_path, store.root, rid, project.id)
    except NotFoundError:
        raise
    except StorageError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    return _render_list(request, project)

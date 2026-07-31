"""Read-only project file browser routes."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.storage import (
    ConflictError,
    ProjectFileDisplayError,
    ProjectFileSecurityError,
    ProjectStore,
    list_project_directory,
    project_path_parts,
    read_project_file,
    shared_markdown_path_parts,
)
from app.structured import StructuredKind, pretty_structured_text
from app.urls import project_url


router = APIRouter()
TEXT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".json",
        ".log",
        ".markdown",
        ".md",
        ".py",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
STRUCTURED_EXTENSIONS: dict[str, StructuredKind] = {
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def _listing_url(project_id: str, path: str) -> str:
    query = urlencode({"path": path})
    return project_url(project_id, f"/files?{query}")


def _view_url(project_id: str, path: str) -> str:
    query = urlencode({"path": path})
    return project_url(project_id, f"/files/view?{query}")


def _focus_url(project_id: str, path: str) -> str:
    query = urlencode({"path": path})
    return project_url(project_id, f"/files/focus?{query}")


@contextmanager
def _shared_path_http_boundary() -> Iterator[None]:
    try:
        yield
    except ProjectFileSecurityError as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid shared Markdown path",
        ) from exc


def _breadcrumbs(project_id: str, path: str) -> list[dict[str, str]]:
    parts = project_path_parts(path)
    breadcrumbs = [
        {
            "label": "Project root",
            "url": _listing_url(project_id, ""),
        }
    ]
    for index, part in enumerate(parts, start=1):
        breadcrumbs.append(
            {
                "label": part,
                "url": _listing_url(project_id, "/".join(parts[:index])),
            }
        )
    return breadcrumbs


@router.get("/projects/{project_id}/files", response_class=HTMLResponse)
async def list_files(
    request: Request,
    project_id: str,
    path: str = Query(""),
) -> HTMLResponse:
    project = request.app.state.registry.get(project_id)
    try:
        entries = list_project_directory(Path(project.path), path)
        breadcrumbs = _breadcrumbs(project_id, path)
    except ProjectFileSecurityError as exc:
        raise HTTPException(status_code=422, detail="invalid project file path") from exc
    except ProjectFileDisplayError:
        entries = []
        breadcrumbs = _breadcrumbs(project_id, "")
        error = "This directory cannot be displayed."
    else:
        error = None
    entry_views = [
        {
            "entry": entry,
            "url": (
                _listing_url(project_id, entry.relative_path)
                if entry.kind == "directory"
                else _view_url(project_id, entry.relative_path)
            ),
        }
        for entry in entries
    ]
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_file_browser.html",
        context={
            "project": project,
            "entries": entry_views,
            "breadcrumbs": breadcrumbs,
            "file_error": error,
        },
    )


def _file_view_context(
    request: Request,
    project_id: str,
    path: str,
) -> dict[str, object]:
    project = request.app.state.registry.get(project_id)
    try:
        project_path_parts(path)
    except ProjectFileSecurityError as exc:
        raise HTTPException(status_code=422, detail="invalid project file path") from exc
    suffix = PurePosixPath(path).suffix.casefold()
    structured_kind = STRUCTURED_EXTENSIONS.get(suffix)
    error: str | None = None
    text = ""
    truncated = False
    replacements = False
    structured_warning: str | None = None
    contents = None
    if suffix not in TEXT_EXTENSIONS:
        error = "This file type cannot be displayed."
    else:
        try:
            contents = read_project_file(
                Path(project.path),
                path,
                request.app.state.settings.file_view_limit,
            )
        except ProjectFileSecurityError as exc:
            raise HTTPException(status_code=422, detail="invalid project file path") from exc
        except ProjectFileDisplayError:
            error = "This file cannot be displayed."
        else:
            if b"\x00" in contents.data:
                error = "Binary files cannot be displayed."
            else:
                try:
                    text = contents.data.decode("utf-8")
                except UnicodeDecodeError:
                    text = contents.data.decode("utf-8", errors="replace")
                    replacements = True
                truncated = contents.truncated
    if error is None and structured_kind is not None:
        if truncated:
            structured_warning = (
                f"This {structured_kind.upper()} file is truncated; "
                "showing the original text without pretty formatting."
            )
        else:
            structured_view = pretty_structured_text(
                text,
                structured_kind,
                output_limit=request.app.state.settings.file_view_limit,
            )
            text = structured_view.text
            structured_warning = structured_view.warning
    store = ProjectStore(project)
    shared_markdown_path = store.selected_shared_markdown_path()
    try:
        shared_markdown_path_parts(path)
    except ProjectFileSecurityError:
        shared_selectable = False
    else:
        shared_selectable = (
            suffix in {".md", ".markdown"}
            and error is None
            and not truncated
            and not replacements
        )
    shared_digest = (
        sha256(contents.data).hexdigest()
        if shared_selectable and contents is not None
        else None
    )
    shared_view = {
        "project": project,
        "shared_markdown_path": shared_markdown_path,
        "shared_selected": shared_markdown_path == path,
        "shared_selectable": shared_selectable,
        "shared_digest": shared_digest,
    }
    return {
        "path": path,
        "text": text,
        "markdown": suffix in {".md", ".markdown"},
        "structured_kind": structured_kind,
        "truncated": truncated,
        "replacements": replacements,
        "structured_warning": structured_warning,
        "file_error": error,
        "focus_url": _focus_url(project_id, path),
        **shared_view,
    }


def _shared_file_response(
    request: Request,
    project_id: str,
    path: str,
    status: str,
) -> HTMLResponse:
    context = _file_view_context(request, project_id, path)
    context.update(
        {
            "focused": False,
            "shared_controls": True,
            "shared_status": status,
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_file_view.html",
        context=context,
    )


@router.get("/projects/{project_id}/files/view", response_class=HTMLResponse)
async def view_file(
    request: Request,
    project_id: str,
    path: str = Query(...),
) -> HTMLResponse:
    context = _file_view_context(request, project_id, path)
    context["focused"] = False
    context["shared_controls"] = True
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_file_view.html",
        context=context,
    )


@router.get("/projects/{project_id}/files/focus", response_class=HTMLResponse)
async def focus_file(
    request: Request,
    project_id: str,
    path: str = Query(...),
) -> HTMLResponse:
    context = _file_view_context(request, project_id, path)
    context["focused"] = True
    context["shared_controls"] = False
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_file_view.html",
        context=context,
    )


@router.post(
    "/projects/{project_id}/files/shared/select",
    response_class=HTMLResponse,
)
async def select_shared_file(
    request: Request,
    project_id: str,
    path: str = Form(...),
) -> HTMLResponse:
    with _shared_path_http_boundary():
        async with request.app.state.locks.registry_project_sessions(project_id):
            project = request.app.state.registry.get(project_id)
            ProjectStore(project).select_shared_markdown(
                path,
                request.app.state.settings.file_view_limit,
            )
        return _shared_file_response(
            request,
            project_id,
            path,
            "Selected as shared context.",
        )


@router.post(
    "/projects/{project_id}/files/shared/save",
    response_class=HTMLResponse,
)
async def save_shared_file(
    request: Request,
    project_id: str,
    path: str = Form(...),
    expected_sha256: str = Form(...),
    text: str = Form(...),
) -> HTMLResponse:
    with _shared_path_http_boundary():
        async with request.app.state.locks.registry_project_sessions(project_id):
            project = request.app.state.registry.get(project_id)
            ProjectStore(project).save_shared_markdown(
                path,
                expected_sha256,
                text,
                request.app.state.settings.file_view_limit,
            )
        return _shared_file_response(
            request,
            project_id,
            path,
            "Shared context saved.",
        )


@router.post(
    "/projects/{project_id}/files/shared/clear",
    response_class=HTMLResponse,
)
async def clear_shared_file(
    request: Request,
    project_id: str,
    path: str = Form(...),
) -> HTMLResponse:
    with _shared_path_http_boundary():
        async with request.app.state.locks.registry_project_sessions(project_id):
            project = request.app.state.registry.get(project_id)
            store = ProjectStore(project)
            normalized = "/".join(shared_markdown_path_parts(path))
            if store.selected_shared_markdown_path() != normalized:
                raise ConflictError(
                    "shared Markdown selection changed; reload before clearing"
                )
            store.clear_shared_markdown()
        return _shared_file_response(
            request,
            project_id,
            path,
            "Shared context cleared.",
        )

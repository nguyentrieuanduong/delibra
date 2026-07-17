"""Read-only project file browser routes."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.storage import (
    ProjectFileDisplayError,
    ProjectFileSecurityError,
    list_project_directory,
    project_path_parts,
    read_project_file,
)


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


def _listing_url(project_id: str, path: str) -> str:
    query = urlencode({"path": path})
    return f"/projects/{project_id}/files?{query}"


def _view_url(project_id: str, path: str) -> str:
    query = urlencode({"path": path})
    return f"/projects/{project_id}/files/view?{query}"


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


@router.get("/projects/{project_id}/files/view", response_class=HTMLResponse)
async def view_file(
    request: Request,
    project_id: str,
    path: str = Query(...),
) -> HTMLResponse:
    project = request.app.state.registry.get(project_id)
    try:
        project_path_parts(path)
    except ProjectFileSecurityError as exc:
        raise HTTPException(status_code=422, detail="invalid project file path") from exc
    suffix = PurePosixPath(path).suffix.casefold()
    error: str | None = None
    text = ""
    truncated = False
    replacements = False
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
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_file_view.html",
        context={
            "path": path,
            "text": text,
            "markdown": suffix in {".md", ".markdown"},
            "truncated": truncated,
            "replacements": replacements,
            "file_error": error,
        },
    )

"""Browse… dialog: folders and files to fill a path field; nothing is saved."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app import coding
from app.path_picker import (
    PickerError,
    breadcrumbs,
    display_path,
    kilobytes,
    list_machine_directory,
    picker_start,
)
from app.security import is_loopback_client

router = APIRouter()

MACHINE_MODES = ("preset", "guidance", "folder")


def _machine_url(mode: str, path: str, hidden: bool) -> str:
    return "/browse?" + urlencode({"mode": mode, "path": path, "hidden": "1" if hidden else "0"})


def _value(mode: str, path: str) -> str:
    # Presets keep `~/` so config.json names no home directory; the other
    # fields require absolute paths.
    return display_path(path) if mode == "preset" else path


def _tickable(mode: str, kind: str) -> bool:
    return (mode == "preset" and kind in ("directory", "markdown")) or (
        mode == "guidance" and kind == "markdown"
    )


@router.get("/browse", response_class=HTMLResponse)
async def browse_machine(
    request: Request,
    mode: str = Query(...),
    path: str = Query(""),
    start: str = Query(""),
    fallback: str = Query(""),
    hidden: bool = Query(False),
):
    # Lists this machine's folders: the same audience as global settings.
    if not is_loopback_client(request):
        raise HTTPException(status_code=403, detail="Only this machine may browse its folders")
    if mode not in MACHINE_MODES:
        raise HTTPException(status_code=422, detail=f"unknown browse mode {mode!r}")
    if not path:
        path = await asyncio.to_thread(picker_start, start, fallback)
    try:
        listing = await asyncio.to_thread(
            list_machine_directory, path, markdown=mode != "folder", hidden=hidden
        )
    except PickerError as exc:
        error, listing = str(exc), None
    else:
        error = None
    current = listing.path if listing else str(Path.home())
    entries = [
        {
            "name": entry.name,
            "kind": entry.kind,
            "kb": kilobytes(entry.size),
            "value": _value(mode, entry.path),
            "url": _machine_url(mode, entry.path, hidden) if entry.kind == "directory" else None,
            "tickable": _tickable(mode, entry.kind),
        }
        for entry in (listing.entries if listing else ())
    ]
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_path_picker.html",
        context={
            "mode": mode,
            "error": error,
            "home_url": _machine_url(mode, "~", hidden),
            "home_label": "~",
            # "/" is its own crumb, so no separator follows it.
            "crumbs": [
                {"label": label, "url": _machine_url(mode, crumb, hidden), "separator": index > 1}
                for index, (label, crumb) in enumerate(breadcrumbs(current) if listing else [])
            ],
            "entries": entries,
            "hidden": hidden,
            "hidden_url": _machine_url(mode, current, not hidden),
            "current_value": _value(mode, current),
            "current_label": display_path(current),
            "truncated_note": (
                f"Only the first {coding.GUIDANCE_SCAN_MAX_ENTRIES} entries were read"
                if listing and listing.truncated
                else None
            ),
        },
    )

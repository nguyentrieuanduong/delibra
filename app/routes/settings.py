"""Global settings: access lists and guidance presets, this machine only."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.coding import (
    GUIDANCE_MAX_BYTES,
    GUIDANCE_MAX_FILES,
    GuidancePickError,
    expand_guidance_picks,
    guidance_file_size,
    guidance_snapshot_size,
    preset_guidance_lists,
)
from app.config import (
    GUIDANCE_PRESET_FILES_MAX,
    GUIDANCE_PRESET_MAX,
    GuidancePreset,
    check_preset_file,
    expand_home,
    parse_guidance_presets,
    read_config_file,
    write_config_file,
)
from app.path_picker import display_path, kilobytes
from app.security import (
    AccessPolicy,
    is_loopback_client,
    normalize_allowed_client,
    normalize_allowed_host,
)
from app.storage import StorageError

router = APIRouter()

CLIENTS_ENV = "DELIBRA_ALLOWED_CLIENTS"
HOSTS_ENV = "DELIBRA_ALLOWED_HOSTS"


def _require_local(request: Request) -> None:
    # The page decides who may connect and shows local paths.
    if not is_loopback_client(request):
        raise HTTPException(
            status_code=403,
            detail="Only this machine may view or change global settings",
        )


def _lines(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _sentence(text: str) -> str:
    # Config messages start lower-case to follow the config path at startup.
    return text[:1].upper() + text[1:]


def _form_rows(form) -> list[dict[str, object]]:
    """The preset rows as submitted: name, files, skills, Remove tick."""

    try:
        count = int(str(form.get("preset_count", "0")))
    except ValueError:
        count = -1
    if not 0 <= count <= GUIDANCE_PRESET_MAX + 1:
        raise StorageError(f"preset_count must be between 0 and {GUIDANCE_PRESET_MAX + 1}")
    return [
        {
            "name": str(form.get(f"preset_name_{index}", "")).strip(),
            "files": _lines(form.get(f"preset_files_{index}", "")),
            "skills": _lines(form.get(f"preset_skills_{index}", "")),
            "remove": form.get(f"preset_remove_{index}") == "true",
        }
        for index in range(count)
    ]


class _PresetRowError(ValueError):
    """A refused preset, with the row it came from so the page can mark it."""

    def __init__(self, row: int, message: str) -> None:
        super().__init__(message)
        self.row = row


def _checked_presets(
    rows: list[dict[str, object]], saved: dict[str, tuple[tuple[str, ...], tuple[str, ...]]]
) -> tuple[GuidancePreset, ...]:
    """The kept rows as presets; blocking (opens new or changed files)."""

    used = [
        (index, row)
        for index, row in enumerate(rows)
        if not row["remove"] and (row["name"] or row["files"] or row["skills"])
    ]
    # Names first: a repeated name is the mistake to report, not its files.
    seen: set[object] = set()
    for index, row in used:
        if row["name"] in seen:
            raise _PresetRowError(index, f"Guidance preset {row['name']!r} is listed twice")
        seen.add(row["name"])
    kept: dict[str, dict[str, object]] = {}
    for index, row in used:
        name = row["name"]
        try:
            (preset,) = parse_guidance_presets(
                {name: {"files": row["files"], "skills": row["skills"]}}
            )
            # New or changed lists must hold real files now; an unchanged
            # stale one is left for the Coding button to report.
            if saved.get(preset.name) != (preset.files, preset.skills):
                preset_guidance_lists(preset)
        except ValueError as exc:
            raise _PresetRowError(index, str(exc)) from exc
        kept[name] = preset.to_json()
    return parse_guidance_presets(kept)


def _file_listing(
    entries: list[str], view: str, message: str | None = None
) -> dict[str, object]:
    """A preset's list as shown, sizes read now; blocking (stats each file)."""

    files: list[dict[str, object]] = []
    sized: list[tuple[str, int]] = []
    for entry in entries:
        path = expand_home(entry)
        try:
            check_preset_file(entry)
        except ValueError:
            size = None  # not a path a preset may hold; shown as missing
        else:
            size = guidance_file_size(path)
        files.append({"path": entry, "kb": kilobytes(size or 0), "missing": size is None})
        sized.append((str(path), size or 0))
    return {
        "view": view,
        "files": files,
        "files_value": "\n".join(entries),
        "total_kb": kilobytes(guidance_snapshot_size(sized)),
        "message": message,
    }


def _preset_rows(
    rows: list[dict[str, object]], error_row: int | None = None, message: str | None = None
) -> list[dict[str, object]]:
    """Rows as shown, plus an empty one for a new preset; blocking."""

    shown = [
        {
            "name": row["name"],
            "remove": row.get("remove", False),
            "error": message if index == error_row else None,
            "listing": _file_listing(list(row["files"]), "row"),
            "skills_value": "\n".join(row.get("skills", [])),
        }
        for index, row in enumerate(rows)
    ]
    if len(shown) < GUIDANCE_PRESET_MAX and all(
        row["name"] or row["listing"]["files"] or row["skills_value"] for row in shown
    ):
        shown.append(
            {
                "name": "",
                "remove": False,
                "error": None,
                "listing": _file_listing([], "row"),
                "skills_value": "",
            }
        )
    return shown


def _saved_rows(presets: tuple[GuidancePreset, ...]) -> list[dict[str, object]]:
    return [
        {"name": preset.name, "files": list(preset.files), "skills": list(preset.skills)}
        for preset in presets
    ]


def _settings_page(
    request: Request,
    rows: list[dict[str, object]],
    clients: list[str] | tuple[str, ...],
    hosts: list[str] | tuple[str, ...],
    error: str | None = None,
    status_code: int = 200,
):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="settings.html",
        status_code=status_code,
        context={
            "allowed_clients": clients,
            "allowed_hosts": hosts,
            "clients_from_env": CLIENTS_ENV in os.environ,
            "hosts_from_env": HOSTS_ENV in os.environ,
            "preset_rows": rows,
            "error": error,
            "health": request.app.state.health,
        },
    )


def _with_picks(
    entries: list[str], picks: list[str], subdirs: bool
) -> tuple[list[str], str | None]:
    """`entries` plus the ticked paths' files now, or unchanged with the
    reason; blocking (reads the ticked folders)."""

    try:
        found = expand_guidance_picks(picks, subdirs)
    except GuidancePickError as exc:
        return entries, str(exc)
    if not found:
        return entries, "No .md files in the ticked paths; nothing was added"
    added = [display_path(path) for path, _size in found]
    merged = entries + [entry for entry in dict.fromkeys(added) if entry not in entries]
    sized = [(str(expand_home(entry)), guidance_file_size(expand_home(entry)) or 0) for entry in merged]
    total = guidance_snapshot_size(sized)
    if len(merged) > GUIDANCE_MAX_FILES or total > GUIDANCE_MAX_BYTES:
        return entries, (
            f"Adding these makes {len(merged)} files and {kilobytes(total)} KB with file "
            f"headers; the limit is {GUIDANCE_MAX_FILES} files and "
            f"{GUIDANCE_MAX_BYTES // 1024} KB; nothing was added"
        )
    return merged, None


@router.get("/settings", response_class=HTMLResponse)
async def global_settings(request: Request):
    _require_local(request)
    settings = request.app.state.settings
    rows = await asyncio.to_thread(_preset_rows, _saved_rows(settings.guidance_presets))
    return _settings_page(request, rows, settings.allowed_clients, settings.allowed_hosts)


@router.post("/settings")
async def save_global_settings(request: Request):
    _require_local(request)
    form = await request.form()
    rows = _form_rows(form)
    async with request.app.state.global_settings_lock:
        settings = request.app.state.settings
        clients_env = CLIENTS_ENV in os.environ
        hosts_env = HOSTS_ENV in os.environ
        typed_clients = _lines(form.get("allowed_clients"))
        typed_hosts = _lines(form.get("allowed_hosts"))
        saved = {
            preset.name: (preset.files, preset.skills) for preset in settings.guidance_presets
        }
        # Validate everything before writing: a rejected form changes neither
        # the file nor what the running server accepts.
        try:
            data = read_config_file(settings.home)
            presets = await asyncio.to_thread(_checked_presets, rows, saved)
            clients = (
                settings.allowed_clients
                if clients_env
                else tuple(normalize_allowed_client(item) for item in typed_clients)
            )
            hosts = (
                settings.allowed_hosts
                if hosts_env
                else tuple(normalize_allowed_host(item) for item in typed_hosts)
            )
            policy = AccessPolicy.build(hosts, clients)
        except ValueError as exc:
            # The page again, as submitted, so chosen files are not lost.
            error_row = exc.row if isinstance(exc, _PresetRowError) else None
            message = _sentence(str(exc))
            shown = await asyncio.to_thread(_preset_rows, rows, error_row, message)
            return _settings_page(
                request,
                shown,
                settings.allowed_clients if clients_env else typed_clients,
                settings.allowed_hosts if hosts_env else typed_hosts,
                error=f"Nothing was saved: {message}",
                status_code=422,
            )
        if not clients_env:
            data["allowed_clients"] = list(clients)
        if not hosts_env:
            data["allowed_hosts"] = list(hosts)
        data["guidance_presets"] = {preset.name: preset.to_json() for preset in presets}
        write_config_file(settings.home, data)
        request.app.state.settings = replace(
            settings,
            allowed_clients=clients,
            allowed_hosts=hosts,
            guidance_presets=presets,
        )
        request.app.state.access.policy = policy
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/preset-files", response_class=HTMLResponse)
async def edit_preset_files(request: Request):
    """The pop-up's Will include list after an Add or Remove; nothing is saved."""

    _require_local(request)
    form = await request.form()
    view = str(form.get("view", ""))
    if view not in ("dialog", "row"):
        raise StorageError(f"unknown view {view!r}")
    entries = list(dict.fromkeys(_lines(form.get("files"))))
    picks = _lines(form.get("add"))
    if len(entries) > GUIDANCE_PRESET_FILES_MAX:
        raise StorageError(f"a preset lists at most {GUIDANCE_PRESET_FILES_MAX} files")
    try:
        for entry in entries:
            check_preset_file(entry)
        for pick in picks:
            if not pick.startswith(("/", "~/")):
                raise ValueError(f"{pick!r} must be an absolute or ~/ path")
    except ValueError as exc:
        raise StorageError(str(exc)) from exc
    remove = str(form.get("remove", ""))
    entries = [entry for entry in entries if entry != remove]
    message = None
    if picks:
        entries, message = await asyncio.to_thread(
            _with_picks, entries, picks, form.get("subdirs") == "true"
        )
    listing = await asyncio.to_thread(_file_listing, entries, view, message)
    return request.app.state.templates.TemplateResponse(
        request=request, name="_preset_files.html", context={"listing": listing}
    )

"""Global settings: access lists, guidance presets, the global instruction and agent defaults, this machine only."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.agent_defaults import EFFORT_LEVELS, AgentDefault, read_agent_defaults, save_agent_defaults, typed_model
from app.converter_models import (
    MODEL_REPOS, check_listing, check_updates, download_models, installed_models, load_check, models_state,
    ready_models_dir, remove_models, resolve_revision,
)
from app.coding import (
    SKILLS_MAX_BYTES,
    SKILLS_MAX_FILES,
    CodingPolicyError,
    GUIDANCE_MAX_BYTES,
    GUIDANCE_MAX_FILES,
    GuidancePickError,
    expand_guidance_picks,
    guidance_file_size,
    guidance_snapshot_size,
    preset_guidance_lists,
    skill_layout,
)
from app.config import (
    GUIDANCE_PRESET_FILES_MAX,
    GUIDANCE_PRESET_SKILLS_MAX,
    GUIDANCE_PRESET_MAX,
    GuidancePreset,
    check_preset_file,
    expand_home,
    parse_guidance_presets,
    read_config_file,
    write_config_file,
)
from app.global_instruction import (
    GlobalInstructionError,
    read_global_instruction,
    save_global_instruction,
)
from app.path_picker import display_path, kilobytes
from app.prompts import (
    CATALOGUE, PromptError, compare_rows, display_prompts, preview, prompt_cards, save_global_prompt,
)
from app.security import (
    AccessPolicy,
    is_loopback_client,
    normalize_allowed_client,
    normalize_allowed_host,
)
from app.storage import ProjectStore, RegistryStore, StorageError
from app.urls import project_url

router = APIRouter()

CLIENTS_ENV = "DELIBRA_ALLOWED_CLIENTS"
HOSTS_ENV = "DELIBRA_ALLOWED_HOSTS"
ACCESS_FIELD_MAX = 65536
GLOBAL_TABS = (
    ("access", "Access", "/settings"),
    ("instruction", "Instruction", "/settings/instruction"),  # global instruction + presets
    ("prompts", "Prompts", "/settings/prompts"),
    ("agents", "Agents", "/settings/agents"),
    ("converter", "Converter", "/settings/converter"),
)
GLOBAL_TABS_LABELS = tuple((key, label) for key, label, _url in GLOBAL_TABS)


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


# A preset's two lists: pasted into every build turn, or copied for agents to read.
_LISTS = {
    "files": {
        "heading": "pasted every turn",
        "limit_text": f"limit {GUIDANCE_MAX_FILES} files, {GUIDANCE_MAX_BYTES // 1024} KB",
        "max_entries": GUIDANCE_PRESET_FILES_MAX,
    },
    "skills": {
        "heading": "agents can read",
        "limit_text": f"limit {SKILLS_MAX_FILES} files, {SKILLS_MAX_BYTES // 1024} KB",
        "max_entries": GUIDANCE_PRESET_SKILLS_MAX,
    },
}


def _list_total(list_name: str, sized: list[tuple[str, int]]) -> int:
    # Pasted files carry a header each in the prompt; copies are bytes only.
    if list_name == "files":
        return guidance_snapshot_size(sized)
    return sum(size for _path, size in sized)


def _file_listing(
    entries: list[str], view: str, message: str | None = None, list_name: str = "files"
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
        "list": list_name,
        "heading": _LISTS[list_name]["heading"],
        "limit_text": _LISTS[list_name]["limit_text"],
        "files": files,
        "files_value": "\n".join(entries),
        "total_kb": kilobytes(_list_total(list_name, sized)),
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
            "skills_listing": _file_listing(list(row.get("skills", [])), "row", list_name="skills"),
        }
        for index, row in enumerate(rows)
    ]
    if len(shown) < GUIDANCE_PRESET_MAX and all(
        row["name"] or row["listing"]["files"] or row["skills_listing"]["files"] for row in shown
    ):
        shown.append(
            {
                "name": "",
                "remove": False,
                "error": None,
                "listing": _file_listing([], "row"),
                "skills_listing": _file_listing([], "row", list_name="skills"),
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
    tab: str,
    rows: list[dict[str, object]] | None,
    clients: list[str] | tuple[str, ...],
    hosts: list[str] | tuple[str, ...],
    error: str | None = None,
    status_code: int = 200,
    global_instruction: str | None = None,
    agent_rows: list[dict[str, object]] | None = None,
    converter_typed: dict[str, str] | None = None,
    prompts_view: dict[str, object] | None = None,
):
    if rows is None:
        # The Instruction tab carries the presets form: every rendering of it, a refused
        # instruction save included, lists the saved presets, or saving that form would
        # delete them (final review F1).
        rows = (
            _preset_rows(_saved_rows(request.app.state.settings.guidance_presets))
            if tab == "instruction"
            else []
        )
    if prompts_view is not None:
        # A refused save's own message wins; otherwise the stored problem pops up.
        error = error or prompts_view["problem"]
    if global_instruction is None:
        try:
            # At most 64 KB, read inline like the config.json this page reads.
            global_instruction = read_global_instruction(request.app.state.settings.home)
        except GlobalInstructionError as exc:
            # Shown as the page's alert; saving replaces the bad file.
            global_instruction = ""
            error = error or str(exc)
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
            "access_field_max": ACCESS_FIELD_MAX,
            "error": error,
            "global_instruction": global_instruction,
            "health": request.app.state.health,
            "agent_rows": (
                agent_rows if agent_rows is not None else _agent_rows(read_agent_defaults(request.app.state.settings.home))
            ),
            **(_converter_context(request.app.state.settings.home, converter_typed or {},
                                  request.app.state.converter_models_lock.locked()) if tab == "converter" else {}),
            **(prompts_view or {}),
            "tab": tab,
            "tab_label": dict(GLOBAL_TABS_LABELS)[tab],
            "settings_tabs": GLOBAL_TABS,
        },
    )


def _with_picks(
    entries: list[str], picks: list[str], subdirs: bool, list_name: str = "files"
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
    total = _list_total(list_name, sized)
    if list_name == "files":
        max_files, max_bytes, headers = GUIDANCE_MAX_FILES, GUIDANCE_MAX_BYTES, " with file headers"
    else:
        max_files, max_bytes, headers = SKILLS_MAX_FILES, SKILLS_MAX_BYTES, ""
    if len(merged) > max_files or total > max_bytes:
        return entries, (
            f"Adding these makes {len(merged)} files and {kilobytes(total)} KB{headers}; "
            f"the limit is {max_files} files and {max_bytes // 1024} KB; nothing was added"
        )
    if list_name == "skills":
        try:
            skill_layout([path for path, _size in sized])
        except CodingPolicyError as exc:
            return entries, f"{exc}; nothing was added"
    return merged, None


@router.get("/settings", response_class=HTMLResponse)
async def global_settings(request: Request):
    return await _global_tab(request, "access")


async def _global_tab(request: Request, tab: str):
    _require_local(request)
    settings = request.app.state.settings
    rows = (
        await asyncio.to_thread(_preset_rows, _saved_rows(settings.guidance_presets))
        if tab == "instruction"
        else []
    )
    view = await _prompts_view(request) if tab == "prompts" else None
    return _settings_page(request, tab, rows, settings.allowed_clients, settings.allowed_hosts, prompts_view=view)


async def _prompts_view(
    request: Request, typed: dict[str, str] | None = None, ticked: list[str] | None = None
) -> dict[str, object]:
    """The global Prompts tab's cards; the files are read off the event loop."""

    prompts, problem = await asyncio.to_thread(display_prompts, request.app.state.settings.home)
    return {
        "cards": prompt_cards(prompts, typed=typed),
        "overridden_in": await asyncio.to_thread(_overrides_by_prompt, request),
        "replace_ticked": frozenset(ticked or ()),
        "problem": problem,
    }


def _access_field(form, key: str, label: str) -> list[str]:
    value = form.get(key)
    # The box's maxlength, enforced here too: a direct POST skips the box.
    if isinstance(value, str) and len(value) > ACCESS_FIELD_MAX:
        raise ValueError(f"{label} is longer than {ACCESS_FIELD_MAX} characters")
    return _lines(value)


@router.post("/settings/access")
async def save_access(request: Request):
    _require_local(request)
    form = await request.form()
    async with request.app.state.global_settings_lock:
        settings = request.app.state.settings
        clients_env = CLIENTS_ENV in os.environ
        hosts_env = HOSTS_ENV in os.environ
        typed_clients = _lines(form.get("allowed_clients"))
        typed_hosts = _lines(form.get("allowed_hosts"))

        def refused(message: str):
            return _settings_page(
                request,
                "access",
                None,
                settings.allowed_clients if clients_env else typed_clients,
                settings.allowed_hosts if hosts_env else typed_hosts,
                error=f"Nothing was saved: {message}",
                status_code=422,
            )

        # Validate everything before writing: a rejected form changes neither
        # the file nor what the running server accepts.
        try:
            data = read_config_file(settings.home)
            clients = (
                settings.allowed_clients
                if clients_env
                else tuple(
                    normalize_allowed_client(item)
                    for item in _access_field(form, "allowed_clients", "allowed clients")
                )
            )
            hosts = (
                settings.allowed_hosts
                if hosts_env
                else tuple(
                    normalize_allowed_host(item)
                    for item in _access_field(form, "allowed_hosts", "allowed hosts")
                )
            )
            policy = AccessPolicy.build(hosts, clients)
        except ValueError as exc:
            return refused(_sentence(str(exc)))
        if not clients_env:
            data["allowed_clients"] = list(clients)
        if not hosts_env:
            data["allowed_hosts"] = list(hosts)
        try:
            write_config_file(settings.home, data)
        except (StorageError, OSError) as exc:
            return refused(_sentence(str(exc)))
        request.app.state.settings = replace(
            settings, allowed_clients=clients, allowed_hosts=hosts
        )
        request.app.state.access.policy = policy
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/presets")
async def save_presets(request: Request):
    _require_local(request)
    form = await request.form()
    rows = _form_rows(form)
    async with request.app.state.global_settings_lock:
        settings = request.app.state.settings
        saved = {
            preset.name: (preset.files, preset.skills) for preset in settings.guidance_presets
        }

        async def refused(message: str, error_row: int | None = None):
            # The page again, as submitted, so chosen files are not lost.
            shown = await asyncio.to_thread(_preset_rows, rows, error_row, message)
            return _settings_page(
                request,
                "instruction",
                shown,
                settings.allowed_clients,
                settings.allowed_hosts,
                error=f"Nothing was saved: {message}",
                status_code=422,
            )

        try:
            data = read_config_file(settings.home)
            presets = await asyncio.to_thread(_checked_presets, rows, saved)
        except ValueError as exc:
            error_row = exc.row if isinstance(exc, _PresetRowError) else None
            return await refused(_sentence(str(exc)), error_row)
        data["guidance_presets"] = {preset.name: preset.to_json() for preset in presets}
        try:
            write_config_file(settings.home, data)
        except (StorageError, OSError) as exc:
            return await refused(_sentence(str(exc)))
        request.app.state.settings = replace(settings, guidance_presets=presets)
    return RedirectResponse("/settings/instruction", status_code=303)


@router.get("/settings/presets")
async def presets_moved(request: Request):
    """Presets live on the Instruction tab now (2026-11-18): old links still land."""

    _require_local(request)
    return RedirectResponse("/settings/instruction", status_code=303)


def _models_refusal(
    request: Request, reason: str, status_code: int, typed: dict[str, str] | None = None, *,
    download: bool = True,
):
    settings = request.app.state.settings
    prefix = "Models were not downloaded" if download else "Models action refused"
    return _settings_page(
        request, "converter", None, settings.allowed_clients, settings.allowed_hosts,
        error=f"{prefix}: {_sentence(reason)}", status_code=status_code,
        converter_typed=typed,
    )


def _models_busy(request: Request) -> str | None:
    """Why a models action must wait: a conversion uses them, or another action runs."""

    if request.app.state.converter_models_lock.locked():
        return "another models action is running"
    if request.app.state.download_manager.converting():
        return "a conversion is running; try again when it has finished"
    return None


async def _start_models_download(request: Request, revisions: dict[str, str] | None = None) -> None:
    """Take the models lock now (no other action can slip in), release it when done."""

    lock = request.app.state.converter_models_lock
    await lock.acquire()
    manager = request.app.state.download_manager

    async def run() -> None:
        try:
            await download_models(request.app.state.settings.home, revisions, slot=manager.conversion_slot)
        finally:
            lock.release()

    request.app.state.converter_models_task = asyncio.create_task(run(), name="delibra-converter-models")


def _converter_context(home, typed: dict[str, str], busy: bool) -> dict[str, object]:
    """Installed models, the last check and, per repo, what the check found."""

    check = load_check(home)
    installed = {row.repo_id: row for row in installed_models(home)}
    repos = []
    for index, (repo, default) in enumerate(MODEL_REPOS):
        listing = check_listing(check, repo)
        default_commit = next((ref["commit"] for ref in listing.get("refs") or [] if ref["name"] == default), None)
        row = installed.get(repo)
        repos.append({
            "index": index, "repo_id": repo, "default": default, "installed": row, "listing": listing,
            "update_available": bool(default_commit and row is not None and row.commit and row.commit != default_commit),
            "selected": typed.get(f"revision_{index}", default), "sha": typed.get(f"sha_{index}", ""),
        })
    return {
        "converter_models": models_state(home), "converter_check": check, "converter_repos": repos,
        "converter_non_default": any(row.default is False for row in installed.values()),
        "converter_busy": busy,
    }


@router.post("/settings/converter-models")
async def download_converter_models(request: Request, force: str = Form("")):
    """Download Docling's default revisions (first download, or Update when forced)."""

    _require_local(request)
    settings = request.app.state.settings
    if ready_models_dir(settings.home) is not None and force != "true":
        return RedirectResponse("/settings/converter", status_code=303)
    busy = _models_busy(request)
    if busy is not None:
        return _models_refusal(request, busy, 409)
    await _start_models_download(request)
    return RedirectResponse("/settings/converter", status_code=303)


@router.post("/settings/converter-models/check")
async def check_converter_models(request: Request):
    """List each model repo's versions; nothing is downloaded."""

    _require_local(request)
    busy = _models_busy(request)
    if busy is not None:
        return _models_refusal(request, busy, 409, download=False)
    async with request.app.state.converter_models_lock:
        await check_updates(request.app.state.settings.home)
    return RedirectResponse("/settings/converter", status_code=303)


@router.post("/settings/converter-models/version")
async def choose_converter_models(request: Request):
    """Download the versions picked from the last check (or typed commits)."""

    _require_local(request)
    form = await request.form()
    typed = {key: str(value) for key, value in form.items() if key.startswith(("revision_", "sha_"))}
    busy = _models_busy(request)
    if busy is not None:
        return _models_refusal(request, busy, 409, typed)
    home = request.app.state.settings.home
    check = load_check(home)
    try:
        revisions = {
            repo: resolve_revision(repo, typed.get(f"revision_{index}", default), typed.get(f"sha_{index}", ""), check)
            for index, (repo, default) in enumerate(MODEL_REPOS)
        }
    except ValueError as exc:
        return _models_refusal(request, str(exc), 422, typed)
    await _start_models_download(request, revisions)
    return RedirectResponse("/settings/converter", status_code=303)


@router.post("/settings/converter-models/remove")
async def remove_converter_models(request: Request):
    _require_local(request)
    busy = _models_busy(request)
    if busy is not None:
        return _models_refusal(request, busy, 409, download=False)
    # The conversion slot too: no conversion may load the models while they go.
    async with request.app.state.converter_models_lock, request.app.state.download_manager.conversion_slot():
        await asyncio.to_thread(remove_models, request.app.state.settings.home)
    return RedirectResponse("/settings/converter", status_code=303)


def _agent_rows(defaults: dict[str, AgentDefault]) -> list[dict[str, object]]:
    """One row per agent type Delibra supports, from the effort lists."""

    return [
        {"agent": agent, "levels": levels, "model": defaults[agent].model, "effort": defaults[agent].effort}
        for agent, levels in EFFORT_LEVELS.items()
    ]


@router.post("/settings/agents")
async def save_agent_default_settings(request: Request):
    _require_local(request)
    form = await request.form()
    typed = {
        agent: AgentDefault(str(form.get(f"model_{agent}", "")), str(form.get(f"effort_{agent}", "")))
        for agent in EFFORT_LEVELS
    }
    async with request.app.state.global_settings_lock:
        settings = request.app.state.settings
        try:
            typed = {agent: AgentDefault(typed_model(item.model), item.effort) for agent, item in typed.items()}
            await asyncio.to_thread(save_agent_defaults, settings.home, typed)
        except (ValueError, StorageError, OSError) as exc:
            return _settings_page(
                request, "agents", None, settings.allowed_clients, settings.allowed_hosts,
                error=f"Nothing was saved: {_sentence(str(exc))}", status_code=422,
                agent_rows=_agent_rows(typed),
            )
    return RedirectResponse("/settings/agents", status_code=303)


@router.post("/settings/global-instruction")
async def save_global_instruction_text(request: Request, global_instruction: str = Form("")):
    _require_local(request)
    async with request.app.state.global_settings_lock:
        settings = request.app.state.settings
        try:
            await asyncio.to_thread(save_global_instruction, settings.home, global_instruction)
        except (StorageError, OSError) as exc:
            return _settings_page(
                request,
                "instruction",
                None,
                settings.allowed_clients,
                settings.allowed_hosts,
                error=f"Nothing was saved: {_sentence(str(exc))}",
                status_code=422,
                global_instruction=global_instruction,
            )
    return RedirectResponse("/settings/instruction", status_code=303)


@router.post("/settings/preset-files", response_class=HTMLResponse)
async def edit_preset_files(request: Request):
    """The pop-up's Will include list after an Add or Remove; nothing is saved."""

    _require_local(request)
    form = await request.form()
    view = str(form.get("view", ""))
    if view not in ("dialog", "row"):
        raise StorageError(f"unknown view {view!r}")
    list_name = str(form.get("list", "files"))
    if list_name not in _LISTS:
        raise StorageError(f"unknown list {list_name!r}")
    entries = list(dict.fromkeys(_lines(form.get("files"))))
    picks = _lines(form.get("add"))
    max_entries = _LISTS[list_name]["max_entries"]
    if len(entries) > max_entries:
        raise StorageError(f"a preset lists at most {max_entries} {list_name}")
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
            _with_picks, entries, picks, form.get("subdirs") == "true", list_name
        )
    listing = await asyncio.to_thread(_file_listing, entries, view, message, list_name)
    return request.app.state.templates.TemplateResponse(
        request=request, name="_preset_files.html", context={"listing": listing}
    )


def _overrides_by_prompt(request: Request) -> dict[str, list[tuple[str, str, str]]]:
    """Prompt id -> (project id, name, its Prompts tab URL) for every project overriding it."""

    found: dict[str, list[tuple[str, str, str]]] = {}
    for project in request.app.state.registry.list_projects():
        try:
            overrides = ProjectStore(project).prompt_overrides()
        except StorageError:
            continue  # an unreadable project cannot be listed; its own tab says why
        for prompt_id in overrides:
            found.setdefault(prompt_id, []).append(
                (project.id, project.name, project_url(project.name, f"/settings/prompts#prompt-{prompt_id}"))
            )
    return found


@router.post("/settings/prompts/{prompt_id}")
async def save_global_prompt_route(
    request: Request,
    prompt_id: str,
    template: str = Form(""),
    action: str = Form("save"),
    replace: list[str] = Form([]),
):
    _require_local(request)
    if prompt_id not in CATALOGUE or action not in ("save", "reset"):
        raise HTTPException(status_code=404, detail="Unknown prompt")
    async with request.app.state.global_settings_lock:
        settings = request.app.state.settings
        try:
            await asyncio.to_thread(
                save_global_prompt, settings.home, prompt_id, template if action == "save" else None
            )
        except (PromptError, OSError, StorageError) as exc:
            return _settings_page(
                request, "prompts", [], settings.allowed_clients, settings.allowed_hosts,
                error=f"Nothing was saved: {_sentence(str(exc))}", status_code=422,
                prompts_view=await _prompts_view(request, {prompt_id: template}, replace),
            )
    # Only after the global save succeeded: a refused save replaces nothing (2026-11-17).
    failures = await _replace_overrides(request, prompt_id, replace)
    if failures:
        settings = request.app.state.settings
        return _settings_page(
            request, "prompts", [], settings.allowed_clients, settings.allowed_hosts,
            error=f"Saved. The override was not replaced in {'; '.join(failures)}",
            prompts_view=await _prompts_view(request),
        )
    return RedirectResponse(f"/settings/prompts#prompt-{prompt_id}", status_code=303)


async def _replace_overrides(request: Request, prompt_id: str, project_ids: list[str]) -> list[str]:
    """Remove `prompt_id`'s override in each chosen project; "Name: reason" per failure.

    Runs after the global save, one project lock at a time (project routes never take
    the global settings lock, so the order cannot deadlock). Overrides are read per
    use, so a running Auto run picks the change up from its next turn.
    """

    failures: list[str] = []
    registry = request.app.state.registry
    for project_id in dict.fromkeys(project_ids):
        try:
            async with request.app.state.locks.registry_project_sessions(project_id):
                project = registry.get(project_id)
                await asyncio.to_thread(ProjectStore(project).set_prompt_override, prompt_id, None)
        except (StorageError, OSError) as exc:  # NotFoundError is a StorageError
            failures.append(f"{_project_label(registry, project_id)}: {exc}")
    return failures


def _project_label(registry: RegistryStore, project_id: str) -> str:
    try:
        return registry.get(project_id).name
    except StorageError:
        return "an unknown project"


@router.post("/settings/prompts/{prompt_id}/compare", response_class=HTMLResponse)
async def compare_global_prompt(
    request: Request, prompt_id: str, template: str = Form(""), project: str = Form("")
):
    """A project's override against the typed global text; nothing is saved."""

    _require_local(request)
    spec = CATALOGUE.get(prompt_id)
    try:
        record = request.app.state.registry.get(project)
        override = ProjectStore(record).prompt_overrides().get(prompt_id)
    except StorageError:
        override = None
    if spec is None or override is None:
        raise HTTPException(status_code=404, detail="That project does not override this prompt")
    rows = compare_rows(override, template)
    return request.app.state.templates.TemplateResponse(
        request=request, name="_prompt_compare.html",
        context={"project_name": record.name, "rows": rows,
                 "identical": all(kind == "same" for kind, _ in rows)},
    )


@router.post("/settings/prompts/{prompt_id}/preview", response_class=HTMLResponse)
async def preview_global_prompt(request: Request, prompt_id: str, template: str = Form("")):
    _require_local(request)
    return prompt_preview_response(request, prompt_id, template)


def prompt_preview_response(request: Request, prompt_id: str, template: str) -> HTMLResponse:
    """The rendered text for typed, unsaved template text; nothing is stored."""

    spec = CATALOGUE.get(prompt_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="Unknown prompt")
    try:
        shown, failed = preview(spec, template), False
    except PromptError as exc:
        shown, failed = str(exc), True
    return request.app.state.templates.TemplateResponse(
        request=request, name="_prompt_preview.html",
        context={"spec": spec, "preview": shown, "failed": failed},
    )


# Declared last: /settings/preset-files is POST-only, so a GET for it is an unknown tab.
@router.get("/settings/{tab}", response_class=HTMLResponse)
async def global_settings_tab(request: Request, tab: str):
    if tab not in {key for key, _label, _url in GLOBAL_TABS}:
        _require_local(request)
        raise HTTPException(status_code=404, detail="Unknown settings tab")
    return await _global_tab(request, tab)

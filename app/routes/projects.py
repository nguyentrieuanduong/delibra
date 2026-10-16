"""Project registration, naming, and registry removal routes."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import shutil

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.coding import (
    CODE_CRAFT_SUGGESTED_GUIDANCE,
    CodingEnvState,
    CodingPolicy,
    CodingPolicyError,
    normalize_guidance_paths,
    normalize_network_allowlist,
    private_resolution_warnings,
)
from app.coding_env import ENV_DIRNAME, prepare_env
from app.project_routing import request_project
from app.security import (
    require_loopback_client,
    validate_field,
    validate_pass_prompt_template_field,
)
from app.storage import (
    ConflictError,
    ProjectStore,
    StorageError,
    normalize_project_name,
    normalize_writable_roots,
    utc_now,
)
from app.urls import project_url


router = APIRouter()


def _project_path(value: str) -> Path:
    value = validate_field(value, "Path", maximum=4_096)
    candidate = Path(value)
    if not candidate.is_absolute():
        raise HTTPException(status_code=422, detail="Project path must be absolute")
    return candidate


def _writable_roots_field(value: str) -> list[str]:
    validated = validate_field(
        value,
        "Writable roots",
        maximum=65_536,
        allow_empty=True,
    )
    return normalize_writable_roots(
        [line.strip() for line in validated.splitlines() if line.strip()]
    )


def _reject_running_sessions(store: ProjectStore) -> None:
    sessions = store.list_sessions()
    if any(session.status == "running" for session in sessions):
        raise ConflictError("project has a running agent")


def _reject_in_memory_work(request: Request, project_id: str) -> None:
    if request.app.state.manager.has_active_project(project_id):
        raise ConflictError("project has active agent work")
    if request.app.state.auto_manager.has_active_project(project_id):
        raise ConflictError("project has active Auto work")


LOGGER = logging.getLogger(__name__)
SERVER_ENV_KEYS = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "CONDA_EXE")


def _server_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in SERVER_ENV_KEYS if key in os.environ}


def _start_env_creation(app, project_id: str, token: str) -> None:
    tasks = app.state.coding_env_tasks
    task = asyncio.create_task(
        _create_env(app, project_id, token), name=f"delibra-coding-env-{project_id}"
    )
    tasks[project_id] = task

    def forget(done: asyncio.Task) -> None:
        if tasks.get(project_id) is done:
            tasks.pop(project_id, None)

    task.add_done_callback(forget)


async def _save_env_result(app, project_id: str, token: str, result: CodingEnvState) -> None:
    # `token` is the `updated_at` of the "creating" state this task owns: a
    # late task must never overwrite a newer attempt.
    async with app.state.locks.registry_project_sessions(project_id):
        store = ProjectStore(app.state.registry.get(project_id))
        current = store.coding_policy().env
        if current.status == "creating" and current.updated_at == token:
            store.set_coding_env(result)


async def _create_env(app, project_id: str, token: str) -> None:
    store = ProjectStore(app.state.registry.get(project_id))
    try:
        result = await prepare_env(store.project_path, environ=_server_environment())
    except asyncio.CancelledError:
        await _save_env_result(
            app, project_id, token,
            CodingEnvState(
                status="error",
                error="Environment creation was cancelled; retry to continue",
                updated_at=utc_now(),
            ),
        )
        raise
    except Exception as exc:  # recorded for the user; never leave "creating"
        LOGGER.exception("Coding environment creation failed for project %s", project_id)
        result = CodingEnvState(
            status="error", error=f"Environment creation failed: {exc}", updated_at=utc_now()
        )
    await _save_env_result(app, project_id, token, result)


@router.post("/projects")
async def register_project(
    request: Request,
    name: str = Form(...),
    path: str = Form(...),
):
    try:
        name = normalize_project_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    candidate = _project_path(path)
    async with request.app.state.locks.registry_lock:
        project = request.app.state.registry.register(name, candidate)
    return RedirectResponse(project_url(project.name, "/chat"), status_code=303)


@router.post("/projects/{project_id}/rebind")
async def rebind_project(
    request: Request,
    project_id: str,
    path: str = Form(...),
):
    candidate_path = _project_path(path)
    project = request_project(request, project_id)
    resolved_project_id = project.id
    registry = request.app.state.registry
    candidate = registry.preview_rebind(resolved_project_id, candidate_path)
    candidate_store = ProjectStore(candidate)
    session_ids = [session.id for session in candidate_store.list_sessions()]
    async with request.app.state.locks.registry_project_sessions(
        resolved_project_id,
        session_ids,
    ):
        _reject_in_memory_work(request, resolved_project_id)
        candidate = registry.preview_rebind(resolved_project_id, candidate_path)
        candidate_store = ProjectStore(candidate)
        candidate_store.clear_native_session_ids_for_relocation()
        candidate_store.migrate_session_directories()
        for session in candidate_store.list_sessions():
            candidate_store.reconcile_session(session.id)
        auto_migration = candidate_store.migrate_auto_run_directories()
        request.app.state.auto_manager.reconcile_store_locked(
            candidate_store,
            auto_migration.readable_records,
        )
        rebound = registry.rebind(resolved_project_id, candidate_path)
    return RedirectResponse(
        project_url(rebound.name, "/chat"),
        status_code=303,
    )


@router.post("/projects/{project_id}/unregister")
async def unregister_project(request: Request, project_id: str):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        project = request.app.state.registry.get(resolved_project_id)
        try:
            store = ProjectStore(project)
        except StorageError:
            store = None
        if store is not None:
            try:
                active_id = store.active_auto_run_id()
                if active_id is not None:
                    store.load_auto_run(active_id)
            except StorageError:
                store = None
            else:
                if active_id is not None:
                    raise ConflictError("project has an active Auto run")
                _reject_running_sessions(store)
        if store is None:
            _reject_in_memory_work(request, resolved_project_id)
        request.app.state.registry.unregister(resolved_project_id)
    return RedirectResponse("/", status_code=303)


@router.post("/projects/{project_id}/auto-migration/retry")
async def retry_auto_migration(request: Request, project_id: str):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    store = ProjectStore(project)
    session_ids = [session.id for session in store.list_sessions()]
    async with request.app.state.locks.registry_project_sessions(
        resolved_project_id,
        session_ids,
    ):
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        current_session_ids = [session.id for session in store.list_sessions()]
        if set(current_session_ids) != set(session_ids):
            raise ConflictError("project sessions changed during Auto migration retry")
        _reject_in_memory_work(request, resolved_project_id)
        status = store.migrate_auto_run_directories()
        request.app.state.auto_manager.reconcile_store_locked(
            store,
            status.readable_records,
        )
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/pass-prompt")
async def save_pass_prompt(
    request: Request,
    project_id: str,
    pass_prompt_template: str = Form(...),
):
    validated = validate_pass_prompt_template_field(pass_prompt_template)
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        project = request.app.state.registry.get(resolved_project_id)
        ProjectStore(project).set_pass_prompt_template(validated)
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/writable-roots")
async def save_writable_roots(
    request: Request,
    project_id: str,
    writable_roots: str = Form(""),
):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        _reject_in_memory_work(request, resolved_project_id)
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        store.require_auto_inactive()
        store.set_writable_roots(_writable_roots_field(writable_roots))
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/coding")
async def save_coding(
    request: Request,
    project_id: str,
    enabled: bool = Form(False),
    network_allowlist: str = Form(""),
    guidance_files: str = Form(""),
    guidance_preset: str = Form(""),
):
    require_loopback_client(request)
    project = request_project(request, project_id)
    resolved_project_id = project.id
    lines = network_allowlist.splitlines() if network_allowlist.strip() else []
    try:
        hosts = normalize_network_allowlist(lines)
    except CodingPolicyError as exc:
        raise StorageError(str(exc)) from exc
    # DNS before the lock, with a budget: a slow resolver must not hold
    # the project's sessions. Timing out only replaces the warning.
    try:
        warnings = await asyncio.wait_for(
            asyncio.to_thread(private_resolution_warnings, hosts), timeout=5
        )
    except TimeoutError:
        warnings = ["Could not check where the listed hosts resolve (DNS timed out)"]
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        _reject_in_memory_work(request, resolved_project_id)
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        store.require_auto_inactive()
        if guidance_preset == "code-craft":
            guidance = [path for path in CODE_CRAFT_SUGGESTED_GUIDANCE if Path(path).is_file()]
        else:
            guidance = guidance_files.splitlines() if guidance_files.strip() else []
        current = store.coding_policy()
        # Validate every field before writing: a rejected form must not
        # change command network access.
        try:
            updated = CodingPolicy(
                enabled=enabled,
                network_allowlist=hosts,
                env=current.env,
                network_warnings=warnings,
                guidance_files=normalize_guidance_paths(guidance),
            )
        except CodingPolicyError as exc:
            raise StorageError(str(exc)) from exc
        token = None
        if enabled and current.env.status == "absent":
            token = utc_now()
            updated.env = CodingEnvState(status="creating", updated_at=token)
        store.replace_coding_policy(updated)
        if token is not None:
            _start_env_creation(request.app, resolved_project_id, token)
    return RedirectResponse(project_url(project.name, "/settings"), status_code=303)


@router.post("/projects/{project_id}/coding/env/retry")
async def retry_coding_env(request: Request, project_id: str):
    require_loopback_client(request)
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        _reject_in_memory_work(request, resolved_project_id)
        project = request.app.state.registry.get(resolved_project_id)
        store = ProjectStore(project)
        store.require_auto_inactive()
        policy = store.coding_policy()
        if not policy.enabled:
            raise StorageError("Coding is not enabled for this project")
        if policy.env.status != "error":
            raise ConflictError("Coding environment is not in an error state")
        envs = store.project_path / ENV_DIRNAME
        partial = (envs.exists() or envs.is_symlink()) and not (envs / "bin" / "python").exists()
        if partial:
            if not policy.env.created_by_delibra:
                raise StorageError(f"{envs} was not created by Delibra; fix or remove it yourself")
            if envs.is_symlink() or not envs.is_dir() or (envs / "bin").is_symlink():
                raise StorageError(f"{envs} is not a plain directory; Delibra will not delete it")
            # The only deletion in coding mode (D§1): a partial env this
            # attempt's predecessor created. rmtree never follows links.
            await asyncio.to_thread(shutil.rmtree, envs)
        token = utc_now()
        store.set_coding_env(CodingEnvState(status="creating", updated_at=token))
        _start_env_creation(request.app, resolved_project_id, token)
    return RedirectResponse(project_url(project.name, "/settings"), status_code=303)


@router.post("/projects/{project_id}/turn-timeout")
async def save_turn_timeout(
    request: Request,
    project_id: str,
    turn_timeout_seconds: int = Form(...),
):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(
        resolved_project_id
    ):
        project = request.app.state.registry.get(resolved_project_id)
        ProjectStore(project).set_turn_timeout_seconds(
            turn_timeout_seconds,
            maximum=request.app.state.settings.max_run_timeout,
        )
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )


@router.post("/projects/{project_id}/pass-prompt/reset")
async def reset_pass_prompt(request: Request, project_id: str):
    project = request_project(request, project_id)
    resolved_project_id = project.id
    async with request.app.state.locks.registry_project_sessions(resolved_project_id):
        project = request.app.state.registry.get(resolved_project_id)
        ProjectStore(project).reset_pass_prompt_template()
    return RedirectResponse(
        project_url(project.name, "/settings"),
        status_code=303,
    )

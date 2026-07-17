"""Filesystem persistence and lock primitives.

All multi-resource operations obey one hierarchy and never reverse it:

    registry lock -> project lifecycle lock -> session locks in sorted UUID order

Finalization and cancellation may acquire only their one session lock. Stable lock
objects are intentionally retained for the process lifetime, so unregister/import
cannot create two lifecycle locks for the same project id.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, AsyncIterator, Iterable
from uuid import uuid4

from app.models import Project, RoundRecord, SessionConfig


FORMAT = "delibra/1"
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
OUTPUT_PATTERN = re.compile(r"^round-(\d{2,})\.md$")
PROMPT_PATTERN = re.compile(r"^round-(\d{2,})\.prompt\.md$")
PARTIAL_PATTERN = re.compile(r"^round-(\d{2,})\.partial\.md$")


class StorageError(RuntimeError):
    """A durable-storage operation failed or valid recovery data was unavailable."""


class NotFoundError(StorageError):
    """Requested app-owned metadata does not exist."""


class ConflictError(StorageError):
    """Requested mutation conflicts with existing state."""


class OwnershipError(StorageError):
    """A path or identity is not proven to be owned by Delibra."""


class InvalidIdentifier(StorageError):
    """An externally supplied project or session identifier is malformed."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sanitize_name(value: str, *, maximum: int = 200) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("name must not be empty")
    if len(cleaned) > maximum:
        raise ValueError(f"name must be at most {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError("name must not contain control characters")
    return cleaned


def validate_id(value: str, label: str = "id") -> str:
    if not ID_PATTERN.fullmatch(value):
        raise InvalidIdentifier(f"invalid {label}")
    return value


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, contents: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise StorageError(f"failed to atomically write {path}") from exc


def atomic_write_text(path: Path, contents: str) -> None:
    atomic_write_bytes(path, contents.encode("utf-8"))


def _valid_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return True


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically replace JSON while preserving the last valid current as `.bak`.

    Backup rotation is itself atomic: current -> `.bak.tmp` -> `.bak`, then the new
    temporary file replaces current. A corrupt current never overwrites a valid backup.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    backup = path.with_name(f"{path.name}.bak")
    backup_temporary = path.with_name(f".{path.name}.bak.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        if path.is_file() and _valid_json(path):
            backup_temporary.unlink(missing_ok=True)
            with path.open("rb") as source, backup_temporary.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(backup_temporary, backup)

        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        backup_temporary.unlink(missing_ok=True)
        raise StorageError(f"failed to atomically write JSON {path}") from exc


def load_json_recover(path: Path) -> Any:
    errors: list[str] = []
    for candidate in (path, path.with_name(f"{path.name}.bak")):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"{candidate.name}: {type(exc).__name__}")
    raise StorageError(f"no valid JSON for {path}: {', '.join(errors)}")


def _assert_no_symlink_components(
    candidate: Path,
    root: Path,
    *,
    allow_missing_leaf: bool,
) -> None:
    root = root.absolute()
    candidate = candidate.absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise OwnershipError(f"path escapes owned root: {candidate}") from exc

    try:
        root_info = root.lstat()
    except OSError as exc:
        raise OwnershipError(f"owned root is unavailable: {root}") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise OwnershipError(f"owned root is a symlink: {root}")

    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return
            raise OwnershipError(f"path component is missing: {current}")
        except OSError as exc:
            raise OwnershipError(f"cannot inspect path component: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise OwnershipError(f"symlinked path component rejected: {current}")


def ensure_owned_directory(path: Path, root: Path) -> Path:
    """Create a missing leaf directory after rejecting every symlink component."""
    _assert_no_symlink_components(path, root, allow_missing_leaf=True)
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"owned directory is unavailable: {path}") from exc
    _assert_no_symlink_components(path, root, allow_missing_leaf=False)
    try:
        info = path.lstat()
    except OSError as exc:
        raise OwnershipError(f"owned directory is unavailable: {path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise OwnershipError(f"owned path is not a directory: {path}")
    return path


def safe_copy_file(
    source: Path,
    allowed_source_root: Path,
    destination: Path,
    allowed_destination_root: Path,
) -> str:
    """Copy/hash from one no-follow descriptor into an exclusive destination."""

    _assert_no_symlink_components(source, allowed_source_root, allow_missing_leaf=False)
    _assert_no_symlink_components(
        destination.parent,
        allowed_destination_root,
        allow_missing_leaf=False,
    )
    _assert_no_symlink_components(
        destination,
        allowed_destination_root,
        allow_missing_leaf=True,
    )
    if destination.exists():
        raise ConflictError(f"destination already exists: {destination}")

    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    created = False
    completed = False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = source.lstat()
        source_descriptor = os.open(source, flags)
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OwnershipError(f"source is not a regular file: {source}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise StorageError("source changed while it was opened")

        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        digest = sha256()
        while True:
            block = os.read(source_descriptor, 64 * 1024)
            if not block:
                break
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(destination_descriptor, view)
                view = view[written:]
        os.fsync(destination_descriptor)

        after = source.lstat()
        end = os.fstat(source_descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (after.st_dev, after.st_ino) != identity or (end.st_dev, end.st_ino) != identity:
            raise StorageError("source vanished or was replaced during copy")
        if (opened.st_size, opened.st_mtime_ns) != (end.st_size, end.st_mtime_ns):
            raise StorageError("source changed during copy")
        completed = True
        return digest.hexdigest()
    except ConflictError:
        raise
    except OwnershipError:
        raise
    except OSError as exc:
        raise StorageError(f"safe copy failed for {source}") from exc
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if created and not completed:
            destination.unlink(missing_ok=True)


@dataclass(frozen=True)
class RoundFile:
    n: int
    path: Path


@dataclass(frozen=True)
class RoundScan:
    outputs: list[RoundFile]
    prompts: list[RoundFile]
    partials: list[RoundFile]
    orphans: list[RoundFile]


class RegistryStore:
    def __init__(self, home: Path):
        self.home = home.expanduser().absolute()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        self.path = self.home / "registry.json"
        if not self.path.exists():
            atomic_write_json(self.path, {"projects": []})

    def _load(self) -> list[Project]:
        data = load_json_recover(self.path)
        if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
            raise StorageError("registry has an invalid shape")
        return [Project.from_dict(item) for item in data["projects"]]

    def _save(self, projects: list[Project]) -> None:
        atomic_write_json(self.path, {"projects": [item.to_dict() for item in projects]})

    def list_projects(self) -> list[Project]:
        return self._load()

    def get(self, project_id: str) -> Project:
        validate_id(project_id, "project id")
        for project in self._load():
            if project.id == project_id:
                return project
        raise NotFoundError(f"project not found: {project_id}")

    def register(self, name: str, path: Path) -> Project:
        name = sanitize_name(name)
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise StorageError(f"project path does not exist: {path}") from exc
        if not resolved.is_dir():
            raise StorageError(f"project path is not a directory: {resolved}")

        projects = self._load()
        if any(Path(item.path) == resolved for item in projects):
            raise ConflictError(f"project path is already registered: {resolved}")

        metadata = resolved / ".delibra"
        manifest_path = metadata / "manifest.json"
        if metadata.exists():
            if metadata.is_symlink() or not metadata.is_dir():
                raise OwnershipError("existing .delibra is not an owned directory")
            _assert_no_symlink_components(
                manifest_path,
                metadata,
                allow_missing_leaf=False,
            )
            try:
                manifest = load_json_recover(manifest_path)
            except StorageError as exc:
                raise OwnershipError("existing .delibra manifest is invalid") from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != FORMAT
                or not isinstance(manifest.get("id"), str)
                or not ID_PATTERN.fullmatch(manifest["id"])
                or not isinstance(manifest.get("created_at"), str)
            ):
                raise OwnershipError("existing .delibra manifest is invalid")
            project_id = manifest["id"]
            created_at = manifest["created_at"]
        else:
            project_id = uuid4().hex
            created_at = utc_now()
            metadata.mkdir(mode=0o700)
            os.chmod(metadata, 0o700)
            atomic_write_json(
                manifest_path,
                {"format": FORMAT, "id": project_id, "created_at": created_at},
            )

        if any(item.id == project_id for item in projects):
            raise ConflictError(f"project identity is already registered: {project_id}")
        project = Project(
            id=project_id,
            name=name,
            path=str(resolved),
            created_at=created_at,
        )
        projects.append(project)
        self._save(projects)
        return project

    def rename(self, project_id: str, name: str) -> Project:
        name = sanitize_name(name)
        projects = self._load()
        for index, project in enumerate(projects):
            if project.id == project_id:
                renamed = Project(
                    id=project.id,
                    name=name,
                    path=project.path,
                    created_at=project.created_at,
                )
                projects[index] = renamed
                self._save(projects)
                return renamed
        raise NotFoundError(f"project not found: {project_id}")

    def unregister(self, project_id: str) -> Project:
        projects = self._load()
        for index, project in enumerate(projects):
            if project.id == project_id:
                removed = projects.pop(index)
                self._save(projects)
                return removed
        raise NotFoundError(f"project not found: {project_id}")


class ProjectStore:
    def __init__(self, project: Project):
        validate_id(project.id, "project id")
        self.project = project
        self.project_path = Path(project.path).resolve(strict=True)
        self.root = self.project_path / ".delibra"
        _assert_no_symlink_components(self.root, self.root, allow_missing_leaf=False)
        manifest_path = self.root / "manifest.json"
        _assert_no_symlink_components(
            manifest_path,
            self.root,
            allow_missing_leaf=False,
        )
        manifest = load_json_recover(manifest_path)
        if manifest.get("format") != FORMAT or manifest.get("id") != project.id:
            raise OwnershipError("project manifest identity does not match registry")
        self.sessions_root = self.root / "sessions"
        ensure_owned_directory(self.sessions_root, self.root)

    def session_dir(self, session_id: str) -> Path:
        validate_id(session_id, "session id")
        return self.sessions_root / session_id

    def rounds_dir(self, session_id: str) -> Path:
        path = self.session_dir(session_id) / "rounds"
        _assert_no_symlink_components(path, self.sessions_root, allow_missing_leaf=False)
        return path

    def workspace_dir(self, session_id: str) -> Path:
        path = self.session_dir(session_id) / "workspace"
        _assert_no_symlink_components(path, self.sessions_root, allow_missing_leaf=False)
        return path

    def create_session(self, config: SessionConfig) -> SessionConfig:
        validate_id(config.id, "session id")
        config.name = sanitize_name(config.name)
        destination = self.session_dir(config.id)
        if destination.exists():
            raise ConflictError(f"session already exists: {config.id}")
        try:
            destination.mkdir(mode=0o700)
            (destination / "rounds").mkdir(mode=0o700)
            workspace = destination / "workspace"
            workspace.mkdir(mode=0o700)
            (workspace / ".tmp").mkdir(mode=0o700)
            (workspace / "inputs").mkdir(mode=0o700)
            atomic_write_json(destination / "config.json", config.to_dict())
        except Exception:
            if destination.exists():
                shutil.rmtree(destination)
            raise
        return config

    def load_session(self, session_id: str) -> SessionConfig:
        destination = self.session_dir(session_id)
        try:
            destination.lstat()
        except FileNotFoundError as exc:
            raise NotFoundError(f"session not found: {session_id}") from exc
        except OSError as exc:
            raise StorageError(f"failed to inspect session: {session_id}") from exc
        _assert_no_symlink_components(destination, self.sessions_root, allow_missing_leaf=False)
        data = load_json_recover(destination / "config.json")
        config = SessionConfig.from_dict(data)
        if config.id != session_id:
            raise OwnershipError("session config identity does not match directory")
        return config

    def save_session(self, config: SessionConfig) -> None:
        destination = self.session_dir(config.id)
        _assert_no_symlink_components(destination, self.sessions_root, allow_missing_leaf=False)
        atomic_write_json(destination / "config.json", config.to_dict())

    def list_sessions(self) -> list[SessionConfig]:
        sessions: list[SessionConfig] = []
        if not self.sessions_root.exists():
            return sessions
        for child in sorted(self.sessions_root.iterdir(), key=lambda item: item.name):
            if ID_PATTERN.fullmatch(child.name):
                sessions.append(self.load_session(child.name))
        return sessions

    def delete_session(self, session_id: str) -> None:
        destination = self.session_dir(session_id)
        config = self.load_session(session_id)
        if config.id != session_id:
            raise OwnershipError("session identity mismatch")
        if config.status == "running":
            raise ConflictError("cannot delete a running session")
        shutil.rmtree(destination)

    def scan_round_files(self, session_id: str) -> RoundScan:
        rounds_path = self.rounds_dir(session_id)
        outputs: list[RoundFile] = []
        prompts: list[RoundFile] = []
        partials: list[RoundFile] = []
        for path in rounds_path.iterdir():
            for pattern, destination in (
                (OUTPUT_PATTERN, outputs),
                (PROMPT_PATTERN, prompts),
                (PARTIAL_PATTERN, partials),
            ):
                match = pattern.fullmatch(path.name)
                if match:
                    destination.append(RoundFile(int(match.group(1)), path))
                    break
        for collection in (outputs, prompts, partials):
            collection.sort(key=lambda item: item.n)
        configured = {item.n for item in self.load_session(session_id).rounds}
        all_files = [*outputs, *prompts, *partials]
        orphans = sorted(
            (item for item in all_files if item.n not in configured),
            key=lambda item: (item.n, item.path.name),
        )
        return RoundScan(outputs, prompts, partials, orphans)

    def allocate_round(self, session_id: str) -> int:
        scan = self.scan_round_files(session_id)
        configured = [item.n for item in self.load_session(session_id).rounds]
        numbers = configured + [
            item.n for item in [*scan.outputs, *scan.prompts, *scan.partials]
        ]
        return max(numbers, default=0) + 1

    def reconcile_session(self, session_id: str) -> SessionConfig:
        config = self.load_session(session_id)
        if config.status != "running" and not any(
            item.status == "running" for item in config.rounds
        ):
            return config

        partials_to_remove: list[Path] = []
        for round_record in config.rounds:
            if round_record.status != "running":
                continue
            partial = self.rounds_dir(session_id) / f"round-{round_record.n:02d}.partial.md"
            output = self.rounds_dir(session_id) / f"round-{round_record.n:02d}.md"
            if partial.is_file():
                atomic_write_bytes(output, partial.read_bytes())
                partials_to_remove.append(partial)
            elif not output.exists():
                atomic_write_text(output, "")
            round_record.status = "error"
            round_record.error = "interrupted by restart"
            round_record.finished_at = utc_now()
        config.status = "error"
        self.save_session(config)
        for partial in partials_to_remove:
            partial.unlink(missing_ok=True)
        return config


class LockCoordinator:
    """Stable asyncio locks implementing the documented global hierarchy."""

    def __init__(self) -> None:
        self.registry_lock = asyncio.Lock()
        self._projects: dict[str, asyncio.Lock] = {}
        self._sessions: dict[tuple[str, str], asyncio.Lock] = {}

    def project_lock(self, project_id: str) -> asyncio.Lock:
        return self._projects.setdefault(project_id, asyncio.Lock())

    def session_lock(self, project_id: str, session_id: str) -> asyncio.Lock:
        return self._sessions.setdefault((project_id, session_id), asyncio.Lock())

    @asynccontextmanager
    async def _acquire(self, locks: Iterable[asyncio.Lock]) -> AsyncIterator[None]:
        acquired: list[asyncio.Lock] = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    @asynccontextmanager
    async def registry_project_sessions(
        self,
        project_id: str,
        session_ids: Iterable[str] = (),
    ) -> AsyncIterator[None]:
        locks = [self.registry_lock, self.project_lock(project_id)]
        locks.extend(
            self.session_lock(project_id, session_id)
            for session_id in sorted(set(session_ids))
        )
        async with self._acquire(locks):
            yield

    @asynccontextmanager
    async def project_sessions(
        self,
        project_id: str,
        session_ids: Iterable[str] = (),
    ) -> AsyncIterator[None]:
        locks = [self.project_lock(project_id)]
        locks.extend(
            self.session_lock(project_id, session_id)
            for session_id in sorted(set(session_ids))
        )
        async with self._acquire(locks):
            yield

    @asynccontextmanager
    async def sessions(
        self,
        project_id: str,
        session_ids: Iterable[str],
    ) -> AsyncIterator[None]:
        locks = [
            self.session_lock(project_id, session_id)
            for session_id in sorted(set(session_ids))
        ]
        async with self._acquire(locks):
            yield

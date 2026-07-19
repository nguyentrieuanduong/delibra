"""Filesystem persistence and lock primitives.

All multi-resource operations obey one hierarchy and never reverse it:

    registry lock -> project lifecycle lock -> session locks in sorted UUID order

Finalization and cancellation may acquire only their one session lock. Stable lock
objects are intentionally retained for the process lifetime, so unregister/import
cannot create two lifecycle locks for the same project id.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any, AsyncIterator, Iterable, Iterator, Literal
from uuid import uuid4

from app.models import AutoArtifact, AutoRunRecord, Project, RoundRecord, SessionConfig


FORMAT = "delibra/1"
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
OUTPUT_PATTERN = re.compile(r"^round-(\d{2,})\.md$")
PROMPT_PATTERN = re.compile(r"^round-(\d{2,})\.prompt\.md$")
PARTIAL_PATTERN = re.compile(r"^round-(\d{2,})\.partial\.md$")
SHARED_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
RESERVED_SHARED_ROOTS = frozenset({".delibra", ".git", ".hg", ".svn"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
AUTO_FORMAT = "delibra-auto/1"
AUTO_STATUSES = frozenset(
    {
        "preparing",
        "discussing",
        "converged",
        "limit_reached",
        "stopped",
        "error",
        "interrupted",
    }
)
AUTO_POLICIES = frozenset({"all_agree", "first_agree"})
AUTO_CONTEXT_PATTERN = re.compile(r"^\.turn-context-[0-9a-f]{32}\.md$")


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


class ProjectFileSecurityError(StorageError):
    """A project-browser path failed its security boundary."""


class ProjectFileDisplayError(StorageError):
    """A safe project-browser path cannot currently be displayed."""


@dataclass(frozen=True)
class ProjectFileEntry:
    name: str
    relative_path: str
    kind: Literal["directory", "file", "other"]
    openable: bool


@dataclass(frozen=True)
class ProjectFileContents:
    data: bytes
    truncated: bool


@dataclass(frozen=True)
class ProjectMarkdown:
    relative_path: str
    text: str
    sha256: str


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


def project_path_parts(value: str) -> tuple[str, ...]:
    if len(value) > 4_096 or "\x00" in value or value.startswith("/"):
        raise ProjectFileSecurityError("invalid project file path")
    raw_parts = value.split("/")
    if any(part == ".." for part in raw_parts):
        raise ProjectFileSecurityError("invalid project file path")
    return tuple(part for part in raw_parts if part not in {"", "."})


def shared_markdown_path_parts(value: str) -> tuple[str, ...]:
    parts = project_path_parts(value)
    if not parts:
        raise ProjectFileSecurityError("invalid shared Markdown path")
    if parts[0].casefold() in RESERVED_SHARED_ROOTS:
        raise ProjectFileSecurityError("reserved shared Markdown path")
    suffix = PurePosixPath("/".join(parts)).suffix.casefold()
    if suffix not in SHARED_MARKDOWN_SUFFIXES:
        raise ProjectFileSecurityError("shared file must be Markdown")
    return parts


def _project_directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise ProjectFileSecurityError("safe project browsing is unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


@contextmanager
def _open_project_directory(root: Path, parts: tuple[str, ...]) -> Iterator[int]:
    flags = _project_directory_flags()
    descriptor: int | None = None
    try:
        try:
            root_info = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise ProjectFileDisplayError("project root cannot be displayed") from exc
        if stat.S_ISLNK(root_info.st_mode):
            raise ProjectFileSecurityError("symlinked project root rejected")
        if not stat.S_ISDIR(root_info.st_mode):
            raise ProjectFileDisplayError("project root cannot be displayed")
        try:
            descriptor = os.open(root, flags)
        except (FileNotFoundError, PermissionError) as exc:
            raise ProjectFileDisplayError("project root cannot be displayed") from exc
        except OSError as exc:
            raise ProjectFileSecurityError("project root changed during access") from exc
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ProjectFileSecurityError("project root is not a directory")
        for part in parts:
            try:
                entry = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise ProjectFileDisplayError("directory cannot be displayed") from exc
            if stat.S_ISLNK(entry.st_mode):
                raise ProjectFileSecurityError("symlinked project path rejected")
            if not stat.S_ISDIR(entry.st_mode):
                raise ProjectFileDisplayError("directory cannot be displayed")
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except (FileNotFoundError, PermissionError) as exc:
                raise ProjectFileDisplayError("directory cannot be displayed") from exc
            except OSError as exc:
                raise ProjectFileSecurityError("project path changed during access") from exc
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except ProjectFileSecurityError:
        raise
    except OSError as exc:
        raise ProjectFileDisplayError("directory cannot be displayed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def list_project_directory(root: Path, relative_path: str) -> list[ProjectFileEntry]:
    parts = project_path_parts(relative_path)
    with _open_project_directory(root, parts) as descriptor:
        entries: list[ProjectFileEntry] = []
        try:
            names = os.listdir(descriptor)
        except OSError as exc:
            raise ProjectFileDisplayError("directory cannot be displayed") from exc
        for name in names:
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProjectFileDisplayError("directory cannot be displayed") from exc
            if stat.S_ISDIR(info.st_mode):
                kind: Literal["directory", "file", "other"] = "directory"
                openable = True
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
                openable = True
            else:
                kind = "other"
                openable = False
            path = "/".join((*parts, name))
            entries.append(ProjectFileEntry(name, path, kind, openable))
    order = {"directory": 0, "file": 1, "other": 2}
    return sorted(entries, key=lambda item: (order[item.kind], item.name.casefold(), item.name))


@contextmanager
def _open_project_file(root: Path, parts: tuple[str, ...]) -> Iterator[int]:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_descriptor: int | None = None
    try:
        with _open_project_directory(root, parts[:-1]) as directory_descriptor:
            try:
                before = os.stat(parts[-1], dir_fd=directory_descriptor, follow_symlinks=False)
            except OSError as exc:
                raise ProjectFileDisplayError("file cannot be displayed") from exc
            if stat.S_ISLNK(before.st_mode):
                raise ProjectFileSecurityError("symlinked project path rejected")
            if not stat.S_ISREG(before.st_mode):
                raise ProjectFileDisplayError("file cannot be displayed")
            try:
                file_descriptor = os.open(parts[-1], flags, dir_fd=directory_descriptor)
            except (FileNotFoundError, PermissionError) as exc:
                raise ProjectFileDisplayError("file cannot be displayed") from exc
            except OSError as exc:
                raise ProjectFileSecurityError("project path changed during access") from exc
            try:
                opened = os.fstat(file_descriptor)
            except OSError as exc:
                raise ProjectFileDisplayError("file cannot be displayed") from exc
            if not stat.S_ISREG(opened.st_mode):
                raise ProjectFileDisplayError("file cannot be displayed")
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ProjectFileSecurityError("project path changed during access")
        yield file_descriptor
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def _read_bounded_descriptor(descriptor: int, limit: int) -> ProjectFileContents:
    remaining = limit + 1
    blocks: list[bytes] = []
    while remaining:
        block = os.read(descriptor, min(64 * 1024, remaining))
        if not block:
            break
        blocks.append(block)
        remaining -= len(block)
    captured = b"".join(blocks)
    return ProjectFileContents(captured[:limit], len(captured) > limit)


def read_project_file(root: Path, relative_path: str, limit: int) -> ProjectFileContents:
    parts = project_path_parts(relative_path)
    if not parts:
        raise ProjectFileDisplayError("file cannot be displayed")
    with _open_project_file(root, parts) as file_descriptor:
        try:
            return _read_bounded_descriptor(file_descriptor, limit)
        except OSError as exc:
            raise ProjectFileDisplayError("file cannot be displayed") from exc


def read_project_markdown(
    root: Path,
    relative_path: str,
    limit: int,
) -> ProjectMarkdown:
    parts = shared_markdown_path_parts(relative_path)
    normalized = "/".join(parts)
    contents = read_project_file(root, normalized, limit)
    if contents.truncated:
        raise ProjectFileDisplayError("shared Markdown exceeds the view limit")
    if b"\x00" in contents.data:
        raise ProjectFileDisplayError("shared Markdown contains NUL")
    try:
        text = contents.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectFileDisplayError("shared Markdown is not valid UTF-8") from exc
    return ProjectMarkdown(normalized, text, sha256(contents.data).hexdigest())


def _replace_project_file(
    root: Path,
    relative_path: str,
    expected_sha256: str,
    data: bytes,
    limit: int,
) -> None:
    parts = shared_markdown_path_parts(relative_path)
    leaf = parts[-1]
    temporary = f".delibra-save-{uuid4().hex}.tmp"
    source_descriptor: int | None = None
    temporary_descriptor: int | None = None
    temporary_exists = False
    with _open_project_directory(root, parts[:-1]) as directory_descriptor:
        try:
            before = os.stat(leaf, dir_fd=directory_descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise ProjectFileSecurityError("symlinked project path rejected")
            if not stat.S_ISREG(before.st_mode):
                raise ProjectFileDisplayError(
                    "shared Markdown is not a regular file"
                )
            source_descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_descriptor,
            )
            opened = os.fstat(source_descriptor)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ProjectFileSecurityError("project path changed during access")
            current = _read_bounded_descriptor(source_descriptor, limit)
            if current.truncated:
                raise ConflictError("shared Markdown changed; reload before saving")
            if sha256(current.data).hexdigest() != expected_sha256:
                raise ConflictError("shared Markdown changed; reload before saving")
            after_read = os.fstat(source_descriptor)
            observed = (
                after_read.st_dev,
                after_read.st_ino,
                after_read.st_size,
                after_read.st_mtime_ns,
            )
            temporary_descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                stat.S_IMODE(opened.st_mode) & 0o777,
                dir_fd=directory_descriptor,
            )
            temporary_exists = True
            os.fchmod(temporary_descriptor, stat.S_IMODE(opened.st_mode) & 0o777)
            view = memoryview(data)
            while view:
                written = os.write(temporary_descriptor, view)
                view = view[written:]
            os.fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = None
            latest = os.stat(leaf, dir_fd=directory_descriptor, follow_symlinks=False)
            current_identity = (
                latest.st_dev,
                latest.st_ino,
                latest.st_size,
                latest.st_mtime_ns,
            )
            if current_identity != observed:
                raise ConflictError("shared Markdown changed; reload before saving")
            os.replace(
                temporary,
                leaf,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            temporary_exists = False
            os.fsync(directory_descriptor)
        except (ConflictError, ProjectFileDisplayError, ProjectFileSecurityError):
            raise
        except OSError as exc:
            raise StorageError("failed to save shared Markdown") from exc
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass


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
        self.manifest_path = self.root / "manifest.json"
        _assert_no_symlink_components(
            self.manifest_path,
            self.root,
            allow_missing_leaf=False,
        )
        self._load_manifest()
        self.sessions_root = self.root / "sessions"
        ensure_owned_directory(self.sessions_root, self.root)

    def _load_manifest(self) -> dict[str, Any]:
        manifest = load_json_recover(self.manifest_path)
        if not isinstance(manifest, dict):
            raise OwnershipError("project manifest has an invalid shape")
        if manifest.get("format") != FORMAT or manifest.get("id") != self.project.id:
            raise OwnershipError("project manifest identity does not match registry")
        selected = manifest.get("shared_markdown_path")
        if selected is not None and not isinstance(selected, str):
            raise OwnershipError("project shared Markdown path is invalid")
        active_auto = manifest.get("active_auto_run_id")
        if active_auto is not None and (
            not isinstance(active_auto, str) or not ID_PATTERN.fullmatch(active_auto)
        ):
            raise OwnershipError("project active Auto run id is invalid")
        return manifest

    @property
    def auto_runs_root(self) -> Path:
        return ensure_owned_directory(self.root / "auto-runs", self.root)

    def auto_run_dir(self, auto_id: str) -> Path:
        validate_id(auto_id, "Auto run id")
        return self.auto_runs_root / auto_id

    @staticmethod
    def _validate_auto_record(record: AutoRunRecord, project_id: str) -> None:
        validate_id(record.id, "Auto run id")
        if record.project_id != project_id:
            raise OwnershipError("Auto run project identity does not match")
        if record.status not in AUTO_STATUSES:
            raise OwnershipError("Auto run status is invalid")
        if record.agreement_policy not in AUTO_POLICIES:
            raise OwnershipError("Auto agreement policy is invalid")
        if not 1 <= record.max_cycles <= 20:
            raise OwnershipError("Auto cycle limit is invalid")
        if len(record.participants) < 2:
            raise OwnershipError("Auto run requires at least two participants")
        session_ids = [participant.session_id for participant in record.participants]
        for session_id in session_ids:
            validate_id(session_id, "Auto participant id")
        if len(session_ids) != len(set(session_ids)):
            raise OwnershipError("Auto participants are duplicated")
        for artifact in (record.topic, record.baseline, record.shared_context):
            if artifact is not None and not SHA256_PATTERN.fullmatch(artifact.sha256):
                raise OwnershipError("Auto artifact digest is invalid")

    @staticmethod
    def _auto_artifact_path(
        run_dir: Path,
        artifact: AutoArtifact,
        expected_name: str,
    ) -> Path:
        if artifact.path != expected_name:
            raise OwnershipError("Auto artifact path is invalid")
        path = run_dir / expected_name
        _assert_no_symlink_components(path, run_dir, allow_missing_leaf=True)
        return path

    def create_auto_run(
        self,
        record: AutoRunRecord,
        *,
        topic: bytes,
        baseline: bytes,
        shared_context: bytes | None = None,
    ) -> AutoRunRecord:
        self._validate_auto_record(record, self.project.id)
        if sha256(topic).hexdigest() != record.topic.sha256:
            raise OwnershipError("Auto topic digest does not match")
        if sha256(baseline).hexdigest() != record.baseline.sha256:
            raise OwnershipError("Auto baseline digest does not match")
        if (record.shared_context is None) != (shared_context is None):
            raise OwnershipError("Auto shared-context metadata does not match")
        if (
            record.shared_context is not None
            and shared_context is not None
            and sha256(shared_context).hexdigest() != record.shared_context.sha256
        ):
            raise OwnershipError("Auto shared-context digest does not match")

        run_dir = self.auto_run_dir(record.id)
        if run_dir.exists():
            raise ConflictError(f"Auto run already exists: {record.id}")
        try:
            run_dir.mkdir(mode=0o700)
            os.chmod(run_dir, 0o700)
            preparations = run_dir / "preparations"
            preparations.mkdir(mode=0o700)
            atomic_write_bytes(
                self._auto_artifact_path(run_dir, record.topic, "topic.md"),
                topic,
            )
            atomic_write_bytes(
                self._auto_artifact_path(run_dir, record.baseline, "baseline.md"),
                baseline,
            )
            if record.shared_context is not None and shared_context is not None:
                atomic_write_bytes(
                    self._auto_artifact_path(
                        run_dir,
                        record.shared_context,
                        "shared-context.md",
                    ),
                    shared_context,
                )
            atomic_write_json(run_dir / "config.json", record.to_dict())
        except Exception:
            if run_dir.exists():
                shutil.rmtree(run_dir)
            raise
        return record

    def load_auto_run(self, auto_id: str) -> AutoRunRecord:
        run_dir = self.auto_run_dir(auto_id)
        _assert_no_symlink_components(run_dir, self.auto_runs_root, allow_missing_leaf=False)
        data = load_json_recover(run_dir / "config.json")
        if not isinstance(data, dict) or data.get("format") != AUTO_FORMAT:
            raise OwnershipError("Auto run config has an invalid shape")
        try:
            record = AutoRunRecord.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnershipError("Auto run config has an invalid shape") from exc
        if record.id != auto_id:
            raise OwnershipError("Auto run identity does not match directory")
        self._validate_auto_record(record, self.project.id)
        return record

    def load_auto_artifact(
        self,
        auto_id: str,
        artifact: AutoArtifact,
        maximum_bytes: int,
    ) -> bytes:
        if maximum_bytes < 1:
            raise ValueError("Auto artifact limit must be positive")
        if artifact.path not in {"topic.md", "baseline.md", "shared-context.md"}:
            raise OwnershipError("Auto artifact path is invalid")
        if not SHA256_PATTERN.fullmatch(artifact.sha256):
            raise OwnershipError("Auto artifact digest is invalid")
        run_dir = self.auto_run_dir(auto_id)
        _assert_no_symlink_components(run_dir, self.auto_runs_root, allow_missing_leaf=False)
        path = run_dir / artifact.path
        _assert_no_symlink_components(path, run_dir, allow_missing_leaf=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OwnershipError("Auto artifact is not a regular file")
            contents = _read_bounded_descriptor(descriptor, maximum_bytes)
        except OwnershipError:
            raise
        except OSError as exc:
            raise OwnershipError("Auto artifact is unavailable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if contents.truncated:
            raise StorageError("Auto artifact exceeds its byte limit")
        if sha256(contents.data).hexdigest() != artifact.sha256:
            raise OwnershipError("Auto artifact digest does not match")
        return contents.data

    def load_round_artifact(
        self,
        session_id: str,
        round_n: int,
        kind: Literal["prompt", "output"],
        maximum_bytes: int,
    ) -> bytes:
        if round_n < 1 or kind not in {"prompt", "output"} or maximum_bytes < 1:
            raise ValueError("round artifact request is invalid")
        suffix = ".prompt.md" if kind == "prompt" else ".md"
        root = self.rounds_dir(session_id)
        path = root / f"round-{round_n:02d}{suffix}"
        return self._load_owned_bytes(path, root, maximum_bytes, "round artifact")

    @staticmethod
    def _load_owned_bytes(
        path: Path,
        root: Path,
        maximum_bytes: int,
        label: str,
    ) -> bytes:
        _assert_no_symlink_components(path, root, allow_missing_leaf=False)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OwnershipError(f"{label} is not a regular file")
            contents = _read_bounded_descriptor(descriptor, maximum_bytes)
        except OwnershipError:
            raise
        except OSError as exc:
            raise StorageError(f"{label} is unavailable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if contents.truncated:
            raise StorageError(f"{label} exceeds its byte limit")
        return contents.data

    def copy_auto_preparation(
        self,
        auto_id: str,
        session_id: str,
        source: Path,
        source_root: Path,
    ) -> str:
        validate_id(session_id, "Auto participant id")
        run_dir = self.auto_run_dir(auto_id)
        _assert_no_symlink_components(run_dir, self.auto_runs_root, allow_missing_leaf=False)
        preparations = run_dir / "preparations"
        _assert_no_symlink_components(preparations, run_dir, allow_missing_leaf=False)
        return safe_copy_file(
            source,
            source_root,
            preparations / f"{session_id}.md",
            preparations,
        )

    def load_auto_preparation(
        self,
        auto_id: str,
        session_id: str,
        expected_sha256: str,
        maximum_bytes: int,
    ) -> bytes:
        validate_id(session_id, "Auto participant id")
        if not SHA256_PATTERN.fullmatch(expected_sha256):
            raise OwnershipError("Auto preparation digest is invalid")
        run_dir = self.auto_run_dir(auto_id)
        _assert_no_symlink_components(
            run_dir,
            self.auto_runs_root,
            allow_missing_leaf=False,
        )
        preparations = run_dir / "preparations"
        _assert_no_symlink_components(
            preparations,
            run_dir,
            allow_missing_leaf=False,
        )
        contents = self._load_owned_bytes(
            preparations / f"{session_id}.md",
            preparations,
            maximum_bytes,
            "Auto preparation",
        )
        if sha256(contents).hexdigest() != expected_sha256:
            raise OwnershipError("Auto preparation digest does not match")
        return contents

    def write_auto_context(self, auto_id: str, contents: bytes) -> tuple[Path, str]:
        run_dir = self.auto_run_dir(auto_id)
        _assert_no_symlink_components(run_dir, self.auto_runs_root, allow_missing_leaf=False)
        path = run_dir / f".turn-context-{uuid4().hex}.md"
        atomic_write_bytes(path, contents)
        return path, sha256(contents).hexdigest()

    def remove_auto_context(self, auto_id: str, path: Path) -> None:
        run_dir = self.auto_run_dir(auto_id)
        if path.parent != run_dir or AUTO_CONTEXT_PATTERN.fullmatch(path.name) is None:
            raise OwnershipError("Auto context cleanup path is invalid")
        _assert_no_symlink_components(path, run_dir, allow_missing_leaf=True)
        path.unlink(missing_ok=True)

    def save_auto_run(self, record: AutoRunRecord) -> None:
        self._validate_auto_record(record, self.project.id)
        run_dir = self.auto_run_dir(record.id)
        _assert_no_symlink_components(run_dir, self.auto_runs_root, allow_missing_leaf=False)
        current = self.load_auto_run(record.id)
        if current.id != record.id:
            raise OwnershipError("Auto run identity changed")
        atomic_write_json(run_dir / "config.json", record.to_dict())

    def list_auto_runs(self) -> list[AutoRunRecord]:
        records: list[AutoRunRecord] = []
        for child in self.auto_runs_root.iterdir():
            if child.is_dir() and ID_PATTERN.fullmatch(child.name):
                records.append(self.load_auto_run(child.name))
        return sorted(records, key=lambda item: (item.created_at, item.id))

    def active_auto_run_id(self) -> str | None:
        active = self._load_manifest().get("active_auto_run_id")
        return str(active) if active is not None else None

    def require_auto_inactive(self) -> None:
        active_id = self.active_auto_run_id()
        if active_id is None:
            return
        self.load_auto_run(active_id)
        raise ConflictError("project has an active Auto run")

    def require_auto_owner(self, auto_id: str) -> AutoRunRecord:
        validate_id(auto_id, "Auto run id")
        if self.active_auto_run_id() != auto_id:
            raise ConflictError("Auto run does not own the active reservation")
        record = self.load_auto_run(auto_id)
        if record.status not in {"preparing", "discussing"}:
            raise ConflictError("Auto reservation owner is already terminal")
        return record

    def publish_auto_reservation(self, auto_id: str) -> None:
        validate_id(auto_id, "Auto run id")
        manifest = self._load_manifest()
        if manifest.get("active_auto_run_id") is not None:
            raise ConflictError("project already has an active Auto run")
        self.load_auto_run(auto_id)
        manifest["active_auto_run_id"] = auto_id
        atomic_write_json(self.manifest_path, manifest)

    def clear_auto_reservation(self, expected_auto_id: str) -> None:
        validate_id(expected_auto_id, "Auto run id")
        manifest = self._load_manifest()
        if manifest.get("active_auto_run_id") != expected_auto_id:
            raise ConflictError("active Auto reservation changed")
        manifest.pop("active_auto_run_id", None)
        atomic_write_json(self.manifest_path, manifest)

    def selected_shared_markdown_path(self) -> str | None:
        selected = self._load_manifest().get("shared_markdown_path")
        if selected is None:
            return None
        return "/".join(shared_markdown_path_parts(selected))

    def select_shared_markdown(
        self,
        relative_path: str,
        limit: int,
    ) -> ProjectMarkdown:
        document = read_project_markdown(self.project_path, relative_path, limit)
        manifest = self._load_manifest()
        manifest["shared_markdown_path"] = document.relative_path
        atomic_write_json(self.manifest_path, manifest)
        return document

    def clear_shared_markdown(self) -> None:
        manifest = self._load_manifest()
        manifest.pop("shared_markdown_path", None)
        atomic_write_json(self.manifest_path, manifest)

    def read_selected_shared_markdown(self, limit: int) -> ProjectMarkdown | None:
        selected = self.selected_shared_markdown_path()
        if selected is None:
            return None
        return read_project_markdown(self.project_path, selected, limit)

    def save_shared_markdown(
        self,
        relative_path: str,
        expected_sha256: str,
        text: str,
        limit: int,
    ) -> ProjectMarkdown:
        normalized = "/".join(shared_markdown_path_parts(relative_path))
        if normalized != self.selected_shared_markdown_path():
            raise ConflictError(
                "shared Markdown selection changed; reload before saving"
            )
        if not SHA256_PATTERN.fullmatch(expected_sha256):
            raise ProjectFileSecurityError("invalid shared Markdown digest")
        encoded = text.encode("utf-8")
        if b"\x00" in encoded:
            raise ProjectFileDisplayError("shared Markdown contains NUL")
        if len(encoded) > limit:
            raise ProjectFileDisplayError("shared Markdown exceeds the view limit")
        _replace_project_file(
            self.project_path,
            normalized,
            expected_sha256,
            encoded,
            limit,
        )
        return read_project_markdown(self.project_path, normalized, limit)

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
            if partial.is_file() and not output.is_file():
                atomic_write_bytes(output, partial.read_bytes())
                partials_to_remove.append(partial)
            elif partial.is_file():
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

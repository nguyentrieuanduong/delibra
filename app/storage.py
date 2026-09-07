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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any, AsyncIterator, Iterable, Iterator, Literal
import unicodedata
from uuid import uuid4

from app.models import (
    AUTO_FORMAT,
    AutoArtifact,
    AutoRunRecord,
    ContextSummaryArtifact,
    Project,
    RateLimitReading,
    RoundRecord,
    SessionConfig,
)
from app.pass_prompts import (
    BUILT_IN_PASS_PROMPT_TEMPLATE,
    PassPromptTemplateError,
    validate_pass_prompt_template,
)


FORMAT = "delibra/1"
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
OUTPUT_PATTERN = re.compile(r"^round-(\d{2,})\.md$")
PROMPT_PATTERN = re.compile(r"^round-(\d{2,})\.prompt\.md$")
PARTIAL_PATTERN = re.compile(r"^round-(\d{2,})\.partial\.md$")
SHARED_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
RESERVED_SHARED_ROOTS = frozenset({".delibra", ".git", ".hg", ".svn"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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
ACTIVE_AUTO_STATUSES = frozenset({"preparing", "discussing"})
TERMINAL_AUTO_STATUSES = AUTO_STATUSES - ACTIVE_AUTO_STATUSES
AUTO_POLICIES = frozenset({"all_agree", "first_agree"})
# Creation-time cap, deliberately not a per-start one: a mistyped limit must not
# schedule an enormous run up front.
AUTO_MAX_INITIAL_CYCLES = 20
# Lifetime bound, enforced on every save and on resume. A run that reaches
# limit_reached at the creation cap reconstructs one cycle past it, so this bound
# must be wider or that run would be rejected by its own persistence layer.
# Widening a validation bound is backward compatible: every existing record
# already satisfies it.
AUTO_MAX_LIFETIME_CYCLES = 100
AUTO_INDEX_FORMAT = "delibra-auto-index/1"
CANONICAL_AUTO_NUMBER = re.compile(r"[1-9][0-9]*\Z")
AUTO_CREATING_PATTERN = re.compile(r"\.creating-([0-9a-f]{32})-([1-9][0-9]*)\Z")
AUTO_CONTEXT_PATTERN = re.compile(r"^\.turn-context-[0-9a-f]{32}\.md$")
AGENT_NAME_MAX_BYTES = 200
WINDOWS_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


def _empty_auto_index() -> dict[str, object]:
    return {
        "format": AUTO_INDEX_FORMAT,
        "next_number": 1,
        "runs": {},
    }


def _parse_auto_index(data: object) -> dict[str, object]:
    if not isinstance(data, dict) or data.get("format") != AUTO_INDEX_FORMAT:
        raise StorageError("Auto run index is invalid")
    next_number = data.get("next_number")
    runs = data.get("runs")
    if type(next_number) is not int or next_number < 1 or not isinstance(runs, dict):
        raise StorageError("Auto run index is invalid")
    parsed_runs: dict[str, int] = {}
    for auto_id, number in runs.items():
        if (
            type(auto_id) is not str
            or ID_PATTERN.fullmatch(auto_id) is None
            or type(number) is not int
            or number < 1
        ):
            raise StorageError("Auto run index is invalid")
        parsed_runs[auto_id] = number
    if len(parsed_runs.values()) != len(set(parsed_runs.values())):
        raise StorageError("Auto run index is invalid")
    return {
        "format": AUTO_INDEX_FORMAT,
        "next_number": next_number,
        "runs": parsed_runs,
    }


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


def epoch_instant(value: object) -> datetime | None:
    """Parse provider epoch seconds without treating booleans as numbers."""

    if type(value) not in (int, float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _is_utc_timestamp(value: str) -> bool:
    """Accept only what ``utc_now`` produces: an aware ISO-8601 instant."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def sanitize_name(value: str, *, maximum: int = 200) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("name must not be empty")
    if len(cleaned) > maximum:
        raise ValueError(f"name must be at most {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError("name must not contain control characters")
    return cleaned


def _normalize_component_name(value: str, *, project_url_rules: bool) -> str:
    label = "project" if project_url_rules else "agent"
    cleaned = unicodedata.normalize("NFC", value.strip())
    if not cleaned:
        raise ValueError(f"{label} name must not be empty")
    if "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"{label} name must be one directory component")
    if cleaned in {".", ".."} or cleaned.startswith("."):
        raise ValueError(f"{label} name must not be hidden")
    if any(
        ord(character) < 32
        or ord(character) == 127
        or (
            project_url_rules
            and (
                128 <= ord(character) <= 159
                or character in {"\u2028", "\u2029"}
                or unicodedata.category(character) == "Cf"
            )
        )
        for character in cleaned
    ):
        raise ValueError(f"{label} name must not contain control characters")
    if cleaned.endswith("."):
        raise ValueError(f"{label} name must not end with a dot")
    basename = cleaned.split(".", 1)[0].casefold()
    if basename in WINDOWS_DEVICE_NAMES:
        raise ValueError(f"{label} name uses a reserved filesystem name")
    if ID_PATTERN.fullmatch(cleaned.casefold()):
        identity = "UUID" if project_url_rules else "session UUID"
        raise ValueError(f"{label} name must not look like a {identity}")
    if len(cleaned.encode("utf-8")) > AGENT_NAME_MAX_BYTES:
        raise ValueError(f"{label} name must be at most 200 UTF-8 bytes")
    return cleaned


def normalize_agent_name(value: str) -> str:
    return _normalize_component_name(value, project_url_rules=False)


def normalize_project_name(value: str) -> str:
    return _normalize_component_name(value, project_url_rules=True)


def project_name_key(value: str) -> str:
    return normalize_project_name(value).casefold()


def agent_name_key(value: str) -> str:
    return normalize_agent_name(value).casefold()


def _agent_directory_name_matches(
    directory_name: str,
    normalized_agent_name: str,
) -> bool:
    return unicodedata.normalize("NFC", directory_name) == normalized_agent_name


def validate_id(value: str, label: str = "id") -> str:
    if not ID_PATTERN.fullmatch(value):
        raise InvalidIdentifier(f"invalid {label}")
    return value


def parse_auto_reference(value: str) -> int | str:
    if CANONICAL_AUTO_NUMBER.fullmatch(value):
        return int(value)
    if ID_PATTERN.fullmatch(value):
        return value
    raise ValueError("Auto run reference is invalid")


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


def atomic_write_owned_json(path: Path, root: Path, data: Any) -> None:
    """Atomically write JSON only through a path owned below ``root``."""

    _assert_no_symlink_components(path.parent, root, allow_missing_leaf=False)
    _assert_no_symlink_components(path, root, allow_missing_leaf=True)
    atomic_write_json(path, data)
    _assert_no_symlink_components(path, root, allow_missing_leaf=False)


def read_owned_bytes(path: Path, root: Path, limit: int) -> ProjectFileContents:
    """Read a regular owned file through a no-follow descriptor and byte bound."""

    if limit < 1:
        raise ValueError("owned read limit must be positive")
    _assert_no_symlink_components(path, root, allow_missing_leaf=False)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OwnershipError(f"owned path is not a regular file: {path}")
        return _read_bounded_descriptor(descriptor, limit)
    except OwnershipError:
        raise
    except OSError as exc:
        raise StorageError(f"failed to read owned file {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


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


def atomic_write_json_recovery_pair(path: Path, data: Any) -> None:
    """Replace recovery first when the previous JSON is unsafe to restore.

    Backup-first ordering means an interruption leaves either the old valid
    current file beside the transformed backup, or both transformed. It never
    leaves a transformed current file whose fallback restores the invalidated
    state. Callers use this only when the previous configuration is unsafe or
    structurally invalid after the transformation; ordinary saves keep
    `atomic_write_json`'s rotate-the-last-good-current behavior.
    """

    try:
        encoded = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StorageError(f"failed to encode recovery JSON {path}") from exc
    backup = path.with_name(f"{path.name}.bak")
    atomic_write_bytes(backup, encoded)
    atomic_write_bytes(path, encoded)


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


CODEX_THREAD_PATTERN = re.compile(r"^[0-9a-zA-Z-]{8,64}$")
# Measured on every Phase 0 turn; any other width is a window Delibra has no
# policy for and is dropped rather than guessed at.
CODEX_ROLLOUT_WINDOWS = {300: "five_hour", 10080: "seven_day"}


def _rollout_rate_limits(rate_limits: Any) -> list[RateLimitReading]:
    if not isinstance(rate_limits, dict):
        return []
    readings: list[RateLimitReading] = []
    for slot in ("primary", "secondary"):
        entry = rate_limits.get(slot)
        if not isinstance(entry, dict):
            continue
        window = CODEX_ROLLOUT_WINDOWS.get(entry.get("window_minutes"))
        used = entry.get("used_percent")
        resets_at = epoch_instant(entry.get("resets_at"))
        # `used_percent` is the only figure Phase 0 proved for Codex; it proved
        # no status, so every Codex window is reported status-unknown. Its gate
        # also proved a positive reset; without one an old rollout cannot be
        # assigned safely to a live window.
        if window is None or type(used) not in (int, float) or resets_at is None:
            continue
        try:
            readings.append(
                RateLimitReading(
                    window=window,
                    used_percent=used,
                    status="unknown",
                    resets_at=resets_at,
                    source="codex_rollout_token_count",
                )
            )
        except ValueError:
            continue
    return readings


def _rollout_tail(path: Path, root: Path, read_limit: int) -> list[str]:
    """Read the end of one rollout through a no-follow descriptor.

    The tail, not the head: a rollout also records every prompt and response, so
    a session can run far past any sane byte budget while the quota records that
    matter are the newest ones.
    """

    _assert_no_symlink_components(path, root, allow_missing_leaf=False)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        size = os.fstat(descriptor).st_size
        offset = max(0, size - read_limit)
        os.lseek(descriptor, offset, os.SEEK_SET)
        blob = os.read(descriptor, read_limit)
    finally:
        os.close(descriptor)
    lines = blob.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if offset else lines


def _codex_rollout_rate_limits(
    codex_home: Path,
    pattern: str,
    *,
    scan_limit: int,
    read_limit: int,
) -> list[RateLimitReading]:
    try:
        matches = sorted(
            codex_home.glob(pattern),
            reverse=True,
        )[:scan_limit]
    except OSError:
        return []
    for path in matches:
        try:
            lines = _rollout_tail(path, codex_home, read_limit)
        except (OSError, OwnershipError):
            continue
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            readings = _rollout_rate_limits(payload.get("rate_limits"))
            if readings:
                return readings
    return []


def read_codex_rate_limits(
    codex_home: Path,
    thread_id: str,
    *,
    scan_limit: int,
    read_limit: int,
) -> list[RateLimitReading]:
    """Read one Codex thread's newest quota record from its own rollout.

    Codex keeps `token_count` in the app-owned `CODEX_HOME` and never on
    `exec --json` stdout, so this is quota state Delibra can read without
    spending a provider call. A rollout mid-append, a malformed tail, a missing
    thread and a symlinked path all report nothing rather than raising: quota
    state is advisory, and no read of it may fail a round.
    """

    if not CODEX_THREAD_PATTERN.fullmatch(thread_id):
        return []
    return _codex_rollout_rate_limits(
        codex_home,
        f"sessions/*/*/*/rollout-*-{thread_id}.jsonl",
        scan_limit=scan_limit,
        read_limit=read_limit,
    )


def read_latest_codex_rate_limits(
    codex_home: Path,
    *,
    scan_limit: int,
    read_limit: int,
) -> list[RateLimitReading]:
    """Read the newest available Codex quota without needing a thread id."""

    return _codex_rollout_rate_limits(
        codex_home,
        "sessions/*/*/*/rollout-*.jsonl",
        scan_limit=scan_limit,
        read_limit=read_limit,
    )


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


@dataclass(frozen=True)
class _SessionDirectory:
    path: Path
    config: SessionConfig
    legacy: bool


@dataclass(frozen=True)
class _SessionLocation:
    path: Path
    legacy: bool


@dataclass(frozen=True)
class _AutoLocation:
    path: Path
    number: int | None
    legacy: bool


@dataclass(frozen=True)
class AutoMigrationIssue:
    child: str
    message: str


@dataclass(frozen=True)
class AutoMigrationStatus:
    complete: bool
    legacy_ids: tuple[str, ...]
    issues: tuple[AutoMigrationIssue, ...]
    readable_records: tuple[AutoRunRecord, ...]


def _auto_migration_conflict(status: AutoMigrationStatus) -> ConflictError:
    reason = (
        status.issues[0].message
        if status.issues
        else "Auto run storage is not fully migrated"
    )
    return ConflictError(
        f"Auto is unavailable: {reason}. "
        "Retry Auto migration in project settings."
    )


@dataclass(frozen=True)
class SessionMigrationIssue:
    session_id: str
    name: str
    message: str


@dataclass(frozen=True)
class SessionMigrationStatus:
    legacy_session_ids: tuple[str, ...]
    issues: tuple[SessionMigrationIssue, ...]

    @property
    def complete(self) -> bool:
        return not self.legacy_session_ids and not self.issues


def _canonical_project_directory(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise StorageError(f"project path does not exist: {path}") from exc
    if not resolved.is_dir():
        raise StorageError(f"project path is not a directory: {resolved}")
    return resolved


def _existing_project_manifest(resolved: Path) -> dict[str, Any]:
    metadata = resolved / ".delibra"
    manifest_path = metadata / "manifest.json"
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
    return manifest


def _existing_project_identity(resolved: Path) -> tuple[str, str]:
    manifest = _existing_project_manifest(resolved)
    return manifest["id"], manifest["created_at"]


def _raw_project(value: object) -> Project:
    if not isinstance(value, dict):
        raise StorageError("registry project has an invalid shape")
    fields = ("id", "name", "path", "created_at")
    if any(not isinstance(value.get(field), str) for field in fields):
        raise StorageError("registry project has an invalid shape")
    project_id = value["id"]
    name = value["name"]
    path = value["path"]
    created_at = value["created_at"]
    validate_id(project_id, "project id")
    if not created_at:
        raise StorageError("registry project timestamp is invalid")
    if not Path(path).is_absolute():
        raise StorageError("registry project path is invalid")
    return Project(
        id=project_id,
        name=name,
        path=path,
        created_at=created_at,
    )


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _derived_project_name(value: str) -> str:
    candidate = unicodedata.normalize("NFC", value).strip()
    candidate = re.sub(r"\s*[/\\]+\s*", "-", candidate)
    candidate = "".join(
        (
            "-"
            if unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
            else character
        )
        for character in candidate
    )
    candidate = re.sub(r"-{2,}", "-", candidate)
    candidate = candidate.lstrip(".").rstrip(".")
    if not candidate:
        candidate = "Project"
    if ID_PATTERN.fullmatch(candidate.casefold()):
        candidate = f"Project-{candidate}"
    if candidate.split(".", 1)[0].casefold() in WINDOWS_DEVICE_NAMES:
        candidate = f"{candidate}-Project"
    candidate = _truncate_utf8(candidate, AGENT_NAME_MAX_BYTES).strip().rstrip(".")
    return candidate or "Project"


def _available_project_name(base: str, used: set[str]) -> str:
    if project_name_key(base) not in used:
        return base
    number = 2
    while True:
        suffix = f"-{number}"
        stem = _truncate_utf8(
            base,
            AGENT_NAME_MAX_BYTES - len(suffix.encode("utf-8")),
        ).strip().rstrip(".")
        candidate = f"{stem or 'Project'}{suffix}"
        if project_name_key(candidate) not in used:
            return candidate
        number += 1


def _migrated_project_names(projects: list[Project]) -> dict[str, str]:
    ids = [project.id for project in projects]
    if len(ids) != len(set(ids)):
        raise StorageError("project UUID is registered more than once")
    normalized: dict[str, str] = {}
    key_counts: dict[str, int] = {}
    for project in projects:
        try:
            selected = normalize_project_name(project.name)
        except ValueError:
            continue
        if selected != project.name:
            continue
        normalized[project.id] = selected
        key = project_name_key(selected)
        key_counts[key] = key_counts.get(key, 0) + 1

    migrated: dict[str, str] = {}
    used: set[str] = set()
    for project in projects:
        selected = normalized.get(project.id)
        if selected is None or key_counts[project_name_key(selected)] != 1:
            continue
        migrated[project.id] = selected
        used.add(project_name_key(selected))

    for project in sorted(projects, key=lambda item: (item.created_at, item.id)):
        if project.id in migrated:
            continue
        selected = _available_project_name(
            _derived_project_name(project.name),
            used,
        )
        migrated[project.id] = selected
        used.add(project_name_key(selected))
    return migrated


class RegistryStore:
    def __init__(self, home: Path):
        self.home = home.expanduser().absolute()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        self.path = self.home / "registry.json"
        if not self.path.exists():
            atomic_write_json(self.path, {"projects": []})
        self.migrate_project_names()

    def _load_raw_projects(self) -> list[Project]:
        data = load_json_recover(self.path)
        if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
            raise StorageError("registry has an invalid shape")
        return [_raw_project(item) for item in data["projects"]]

    def _load(self) -> list[Project]:
        projects = self._load_raw_projects()
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for project in projects:
            validate_id(project.id, "project id")
            try:
                name_key = project_name_key(project.name)
            except ValueError as exc:
                raise StorageError("registered project name is invalid") from exc
            if project.id in seen_ids:
                raise StorageError("project UUID is registered more than once")
            if name_key in seen_names:
                raise StorageError("project name is registered more than once")
            seen_ids.add(project.id)
            seen_names.add(name_key)
        return projects

    def _save(self, projects: list[Project]) -> None:
        atomic_write_json(self.path, {"projects": [item.to_dict() for item in projects]})

    def migrate_project_names(self) -> bool:
        projects = self._load_raw_projects()
        migrated = _migrated_project_names(projects)
        if all(item.name == migrated[item.id] for item in projects):
            return False
        rewritten = [
            Project(
                id=item.id,
                name=migrated[item.id],
                path=item.path,
                created_at=item.created_at,
            )
            for item in projects
        ]
        self._save(rewritten)
        return True

    def list_projects(self) -> list[Project]:
        return self._load()

    def get(self, project_id: str) -> Project:
        validate_id(project_id, "project id")
        for project in self._load():
            if project.id == project_id:
                return project
        raise NotFoundError(f"project not found: {project_id}")

    def resolve(self, reference: str) -> Project:
        if ID_PATTERN.fullmatch(reference):
            return self.get(reference)
        try:
            wanted = project_name_key(reference)
        except ValueError as exc:
            raise NotFoundError(f"project not found: {reference}") from exc
        for project in self._load():
            if project_name_key(project.name) == wanted:
                return project
        raise NotFoundError(f"project not found: {reference}")

    def preview_rebind(self, project_id: str, path: Path) -> Project:
        current = self.get(project_id)
        resolved = _canonical_project_directory(path)
        if Path(current.path) == resolved:
            raise ConflictError("project is already bound to this path")
        projects = self._load()
        if any(
            item.id != project_id and Path(item.path) == resolved for item in projects
        ):
            raise ConflictError(f"project path is already registered: {resolved}")
        candidate_id, candidate_created_at = _existing_project_identity(resolved)
        if candidate_id != current.id or candidate_created_at != current.created_at:
            raise ConflictError("rebind target has a different project identity")
        return Project(
            id=current.id,
            name=current.name,
            path=str(resolved),
            created_at=current.created_at,
        )

    def rebind(self, project_id: str, path: Path) -> Project:
        rebound = self.preview_rebind(project_id, path)
        ProjectStore(rebound).sync_manifest_name(rebound.name)
        projects = self._load()
        for index, project in enumerate(projects):
            if project.id == project_id:
                projects[index] = rebound
                self._save(projects)
                return rebound
        raise NotFoundError(f"project not found: {project_id}")

    def register(self, name: str, path: Path) -> Project:
        requested_name = normalize_project_name(name)
        resolved = _canonical_project_directory(path)

        projects = self._load()
        if any(Path(item.path) == resolved for item in projects):
            raise ConflictError(f"project path is already registered: {resolved}")

        metadata = resolved / ".delibra"
        manifest_path = metadata / "manifest.json"
        used_names = {project_name_key(item.name) for item in projects}
        if metadata.exists():
            manifest = _existing_project_manifest(resolved)
            project_id = manifest["id"]
            created_at = manifest["created_at"]
            try:
                preferred_name = normalize_project_name(manifest.get("name"))
            except (TypeError, ValueError):
                preferred_name = None
            if preferred_name is None:
                if project_name_key(requested_name) in used_names:
                    raise ConflictError("project name is already in use")
                selected_name = requested_name
            else:
                selected_name = _available_project_name(
                    preferred_name,
                    used_names,
                )
        else:
            project_id = uuid4().hex
            created_at = utc_now()
            if project_name_key(requested_name) in used_names:
                raise ConflictError("project name is already in use")
            selected_name = requested_name
            metadata.mkdir(mode=0o700)
            os.chmod(metadata, 0o700)
            atomic_write_json(
                manifest_path,
                {
                    "format": FORMAT,
                    "id": project_id,
                    "created_at": created_at,
                    "name": selected_name,
                },
            )

        if any(item.id == project_id for item in projects):
            raise ConflictError(f"project identity is already registered: {project_id}")
        project = Project(
            id=project_id,
            name=selected_name,
            path=str(resolved),
            created_at=created_at,
        )
        ProjectStore(project).sync_manifest_name(selected_name)
        projects.append(project)
        self._save(projects)
        return project

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
        try:
            self.project_path = Path(project.path).resolve(strict=True)
        except OSError as exc:
            raise StorageError("registered project path is unavailable") from exc
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
        self._session_directory_cache: dict[str, _SessionLocation] | None = None
        self._auto_directory_cache: dict[str, _AutoLocation] | None = None

    def _load_manifest(self) -> dict[str, Any]:
        manifest = load_json_recover(self.manifest_path)
        if not isinstance(manifest, dict):
            raise OwnershipError("project manifest has an invalid shape")
        if (
            manifest.get("format") != FORMAT
            or manifest.get("id") != self.project.id
            or manifest.get("created_at") != self.project.created_at
        ):
            raise OwnershipError("project manifest identity does not match registry")
        selected = manifest.get("shared_markdown_path")
        if selected is not None and not isinstance(selected, str):
            raise OwnershipError("project shared Markdown path is invalid")
        active_auto = manifest.get("active_auto_run_id")
        if active_auto is not None and (
            not isinstance(active_auto, str) or not ID_PATTERN.fullmatch(active_auto)
        ):
            raise OwnershipError("project active Auto run id is invalid")
        if "pass_prompt_template" in manifest:
            try:
                validate_pass_prompt_template(manifest["pass_prompt_template"])
            except PassPromptTemplateError as exc:
                raise OwnershipError("project Pass prompt template is invalid") from exc
        return manifest

    def sync_manifest_name(self, name: str) -> None:
        selected = normalize_project_name(name)
        manifest = self._load_manifest()
        if manifest.get("name") == selected:
            return
        manifest["name"] = selected
        atomic_write_json(self.manifest_path, manifest)

    def effective_pass_prompt_template(self) -> str:
        return self._load_manifest().get(
            "pass_prompt_template",
            BUILT_IN_PASS_PROMPT_TEMPLATE,
        )

    def set_pass_prompt_template(self, template: object) -> str:
        validated = validate_pass_prompt_template(template)
        manifest = self._load_manifest()
        manifest["pass_prompt_template"] = validated
        atomic_write_json(self.manifest_path, manifest)
        return validated

    def reset_pass_prompt_template(self) -> None:
        manifest = self._load_manifest()
        manifest.pop("pass_prompt_template", None)
        atomic_write_json(self.manifest_path, manifest)

    @property
    def auto_runs_root(self) -> Path:
        return ensure_owned_directory(self.root / "auto-runs", self.root)

    @property
    def auto_index_path(self) -> Path:
        return self.auto_runs_root / ".index.json"

    def _load_auto_index(self) -> dict[str, object]:
        for candidate in (
            self.auto_index_path,
            self.auto_index_path.with_name(f"{self.auto_index_path.name}.bak"),
        ):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                return _parse_auto_index(data)
            except (OSError, UnicodeError, json.JSONDecodeError, StorageError):
                continue
        raise StorageError("Auto run index has no valid recovery copy")

    def _rebuild_auto_index(self) -> dict[str, object]:
        status = self._scan_auto_directories()
        if status.issues:
            raise StorageError(status.issues[0].message)
        return self._auto_index_from_directory_cache(minimum_next=1)

    def _load_or_rebuild_auto_index(self) -> dict[str, object]:
        try:
            return self._load_auto_index()
        except StorageError:
            index = self._rebuild_auto_index()
            atomic_write_json_recovery_pair(self.auto_index_path, index)
            return index

    def _load_auto_record_at(
        self,
        run_dir: Path,
        *,
        expected_number: int | None,
        legacy: bool,
    ) -> AutoRunRecord:
        _assert_no_symlink_components(
            run_dir,
            self.auto_runs_root,
            allow_missing_leaf=False,
        )
        try:
            data = load_json_recover(run_dir / "config.json")
            if not isinstance(data, dict) or data.get("format") != AUTO_FORMAT:
                raise OwnershipError("Auto run config is invalid")
            record = AutoRunRecord.from_dict(data)
        except (KeyError, TypeError, ValueError, StorageError) as exc:
            raise OwnershipError("Auto run config is invalid") from exc
        self._validate_auto_record(record, self.project.id)
        if legacy and run_dir.name != record.id:
            raise OwnershipError("legacy Auto directory identity is invalid")
        if not legacy and record.number != expected_number:
            raise OwnershipError("Auto number does not match directory")
        return record

    def _scan_auto_directories(self) -> AutoMigrationStatus:
        candidates: list[tuple[str, AutoRunRecord, _AutoLocation]] = []
        issues: list[AutoMigrationIssue] = []
        for child in sorted(self.auto_runs_root.iterdir(), key=lambda item: item.name):
            if child.name.startswith("."):
                continue
            if CANONICAL_AUTO_NUMBER.fullmatch(child.name):
                expected_number = int(child.name)
                legacy = False
            elif ID_PATTERN.fullmatch(child.name):
                expected_number = None
                legacy = True
            else:
                issues.append(
                    AutoMigrationIssue(child.name, "Auto run directory name is invalid")
                )
                continue
            try:
                child_info = child.lstat()
                if stat.S_ISLNK(child_info.st_mode):
                    raise OwnershipError("Auto run directory must not be a symlink")
                if not stat.S_ISDIR(child_info.st_mode):
                    raise OwnershipError("Auto run child is not a directory")
                record = self._load_auto_record_at(
                    child,
                    expected_number=expected_number,
                    legacy=legacy,
                )
            except OSError:
                issues.append(
                    AutoMigrationIssue(child.name, "Auto run directory is unavailable")
                )
                continue
            except StorageError as exc:
                issues.append(AutoMigrationIssue(child.name, str(exc)))
                continue
            candidates.append(
                (
                    child.name,
                    record,
                    _AutoLocation(child, record.number, legacy),
                )
            )

        uuid_owners: dict[str, list[str]] = {}
        number_owners: dict[int, list[str]] = {}
        for child_name, record, _ in candidates:
            uuid_owners.setdefault(record.id, []).append(child_name)
            if record.number is not None:
                number_owners.setdefault(record.number, []).append(child_name)
        ambiguous: set[str] = set()
        for owners in uuid_owners.values():
            if len(owners) > 1:
                ambiguous.update(owners)
                issues.extend(
                    AutoMigrationIssue(child, "Auto run UUID is stored more than once")
                    for child in owners
                )
        for owners in number_owners.values():
            if len(owners) > 1:
                ambiguous.update(owners)
                issues.extend(
                    AutoMigrationIssue(child, "Auto run number is stored more than once")
                    for child in owners
                )

        readable = [
            (child, record, location)
            for child, record, location in candidates
            if child not in ambiguous
        ]
        unique_issues = {
            (issue.child, issue.message): issue
            for issue in issues
        }
        ordered_issues = tuple(unique_issues[key] for key in sorted(unique_issues))
        legacy_ids = tuple(
            sorted(record.id for _, record, location in readable if location.legacy)
        )
        self._auto_directory_cache = {
            record.id: location
            for _, record, location in readable
        }
        return AutoMigrationStatus(
            complete=not legacy_ids and not ordered_issues,
            legacy_ids=legacy_ids,
            issues=ordered_issues,
            readable_records=tuple(record for _, record, _ in readable),
        )

    def scan_auto_runs(self) -> AutoMigrationStatus:
        return self._scan_auto_directories()

    def auto_migration_status(self) -> AutoMigrationStatus:
        return self.scan_auto_runs()

    def _auto_index_from_directory_cache(
        self,
        *,
        minimum_next: int,
    ) -> dict[str, object]:
        assert self._auto_directory_cache is not None
        runs = {
            auto_id: location.number
            for auto_id, location in self._auto_directory_cache.items()
            if not location.legacy and location.number is not None
        }
        creating_numbers = (
            int(match.group(2))
            for child in self.auto_runs_root.iterdir()
            if (match := AUTO_CREATING_PATTERN.fullmatch(child.name)) is not None
        )
        return {
            "format": AUTO_INDEX_FORMAT,
            "next_number": max(
                minimum_next,
                max(runs.values(), default=0) + 1,
                max(creating_numbers, default=0) + 1,
            ),
            "runs": runs,
        }

    def _publish_rebuilt_auto_index(self, *, recovery_pair: bool) -> None:
        if self._auto_directory_cache is None:
            status = self._scan_auto_directories()
            if status.issues:
                raise StorageError(status.issues[0].message)
        recovered_next = 1
        try:
            recovered = self._load_auto_index()["next_number"]
            assert type(recovered) is int
            recovered_next = recovered
        except StorageError:
            pass
        index = self._auto_index_from_directory_cache(
            minimum_next=recovered_next,
        )
        writer = atomic_write_json_recovery_pair if recovery_pair else atomic_write_json
        writer(self.auto_index_path, index)

    def _auto_location(self, auto_id: str) -> _AutoLocation:
        auto_id = validate_id(auto_id, "Auto run id")
        if self._auto_directory_cache is not None:
            cached = self._auto_directory_cache.get(auto_id)
            if cached is None:
                raise NotFoundError(f"Auto run not found: {auto_id}")
            return cached

        try:
            index = self._load_auto_index()
        except StorageError:
            index = None
        if index is not None:
            runs = index["runs"]
            assert isinstance(runs, dict)
            number = runs.get(auto_id)
            if type(number) is int:
                indexed = _AutoLocation(
                    self.auto_runs_root / str(number),
                    number,
                    False,
                )
                try:
                    record = self._load_auto_record_at(
                        indexed.path,
                        expected_number=number,
                        legacy=False,
                    )
                    if record.id != auto_id:
                        raise OwnershipError(
                            "Auto run identity does not match index"
                        )
                except StorageError:
                    pass
                else:
                    return indexed

        legacy_path = self.auto_runs_root / auto_id
        try:
            legacy_info = legacy_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StorageError("Auto run directory is unavailable") from exc
        else:
            if stat.S_ISLNK(legacy_info.st_mode):
                raise OwnershipError("Auto run directory must not be a symlink")
            record = self._load_auto_record_at(
                legacy_path,
                expected_number=None,
                legacy=True,
            )
            if record.id != auto_id:
                raise OwnershipError("Auto run identity does not match directory")
            return _AutoLocation(legacy_path, record.number, True)

        status = self._scan_auto_directories()
        assert self._auto_directory_cache is not None
        location = self._auto_directory_cache.get(auto_id)
        if not status.issues:
            try:
                self._publish_rebuilt_auto_index(recovery_pair=False)
            except StorageError:
                pass
        if location is not None:
            return location
        if status.issues:
            raise StorageError(status.issues[0].message)
        raise NotFoundError(f"Auto run not found: {auto_id}")

    def _recovered_auto_high_water(self) -> int | None:
        try:
            index = self._load_auto_index()
        except StorageError:
            return None
        next_number = index["next_number"]
        assert type(next_number) is int
        return next_number

    def _remove_abandoned_auto_creations(self, next_number: int) -> None:
        for child in sorted(self.auto_runs_root.iterdir(), key=lambda item: item.name):
            match = AUTO_CREATING_PATTERN.fullmatch(child.name)
            if match is None or int(match.group(2)) >= next_number:
                continue
            try:
                info = child.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                child.unlink()
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            _assert_no_symlink_components(
                child,
                self.auto_runs_root,
                allow_missing_leaf=False,
            )
            shutil.rmtree(child)

    def migrate_auto_run_directories(self) -> AutoMigrationStatus:
        high_water = self._recovered_auto_high_water()
        if high_water is not None:
            self._remove_abandoned_auto_creations(high_water)
        status = self.auto_migration_status()
        if status.issues:
            return status
        if not status.legacy_ids:
            self._publish_rebuilt_auto_index(recovery_pair=True)
            return self.auto_migration_status()

        self._load_or_rebuild_auto_index()
        ordered = sorted(
            status.readable_records,
            key=lambda item: (item.created_at, item.id),
        )
        assignments = {
            record.id: number
            for number, record in enumerate(ordered, start=1)
        }
        legacy_ids = set(status.legacy_ids)
        assert self._auto_directory_cache is not None
        assignment_issues: list[AutoMigrationIssue] = []
        for record in ordered:
            assigned = assignments[record.id]
            if record.id not in legacy_ids and record.number != assigned:
                location = self._auto_directory_cache[record.id]
                assignment_issues.append(
                    AutoMigrationIssue(
                        location.path.name,
                        "Auto number assignment is inconsistent",
                    )
                )
            if (
                record.id in legacy_ids
                and record.number is not None
                and record.number != assigned
            ):
                assignment_issues.append(
                    AutoMigrationIssue(
                        record.id,
                        "Auto number assignment is inconsistent",
                    )
                )
        if assignment_issues:
            return AutoMigrationStatus(
                complete=False,
                legacy_ids=status.legacy_ids,
                issues=tuple(assignment_issues),
                readable_records=status.readable_records,
            )

        for record in ordered:
            if record.id not in legacy_ids:
                continue
            assigned = assignments[record.id]
            source = self.auto_runs_root / record.id
            if record.number is None:
                migrated = replace(record, number=assigned)
                atomic_write_json_recovery_pair(
                    source / "config.json",
                    migrated.to_dict(),
                )
            destination = self.auto_runs_root / str(assigned)
            if destination.exists():
                raise OwnershipError("Auto migration destination already exists")
            os.replace(source, destination)
            _fsync_directory(self.auto_runs_root)
            self._invalidate_auto_directory_cache()
        self._publish_rebuilt_auto_index(recovery_pair=True)
        return self.auto_migration_status()

    def require_auto_migration_complete(self) -> AutoMigrationStatus:
        status = self.auto_migration_status()
        if not status.complete:
            raise _auto_migration_conflict(status)
        return status

    def reserve_auto_run_number(self) -> int:
        self.require_auto_migration_complete()
        index = self._load_or_rebuild_auto_index()
        number = index["next_number"]
        assert type(number) is int
        index["next_number"] = number + 1
        atomic_write_json_recovery_pair(self.auto_index_path, index)
        return number

    def _invalidate_auto_directory_cache(self) -> None:
        self._auto_directory_cache = None

    def auto_run_dir(self, auto_id: str) -> Path:
        return self._auto_location(auto_id).path

    @staticmethod
    def _validate_auto_record(record: AutoRunRecord, project_id: str) -> None:
        validate_id(record.id, "Auto run id")
        if record.project_id != project_id:
            raise OwnershipError("Auto run project identity does not match")
        if record.status not in AUTO_STATUSES:
            raise OwnershipError("Auto run status is invalid")
        if record.agreement_policy not in AUTO_POLICIES:
            raise OwnershipError("Auto agreement policy is invalid")
        if not 1 <= record.max_cycles <= AUTO_MAX_LIFETIME_CYCLES:
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
        for resumption in record.resumptions:
            if resumption.from_status not in TERMINAL_AUTO_STATUSES:
                raise OwnershipError("Auto resumption source status is invalid")
            if not 1 <= resumption.max_cycles <= AUTO_MAX_LIFETIME_CYCLES:
                raise OwnershipError("Auto resumption cycle limit is invalid")
            if resumption.turn_timeout_seconds < 1:
                raise OwnershipError("Auto resumption turn timeout is invalid")
            if not _is_utc_timestamp(resumption.resumed_at):
                raise OwnershipError("Auto resumption timestamp is invalid")

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
        if type(record.number) is not int or record.number < 1:
            raise OwnershipError("Auto run number is invalid")
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

        self.require_auto_migration_complete()
        index = self._load_or_rebuild_auto_index()
        runs = index["runs"]
        next_number = index["next_number"]
        assert isinstance(runs, dict)
        assert type(next_number) is int
        if record.number != next_number - 1:
            raise ConflictError("Auto run number was not the latest reservation")
        if record.id in runs or record.number in runs.values():
            raise ConflictError(f"Auto run already exists: {record.id}")
        temporary = self.auto_runs_root / f".creating-{record.id}-{record.number}"
        destination = self.auto_runs_root / str(record.number)
        if temporary.exists() or destination.exists():
            raise ConflictError(f"Auto run already exists: {record.id}")
        published = False
        try:
            temporary.mkdir(mode=0o700)
            os.chmod(temporary, 0o700)
            preparations = temporary / "preparations"
            preparations.mkdir(mode=0o700)
            atomic_write_bytes(
                self._auto_artifact_path(temporary, record.topic, "topic.md"),
                topic,
            )
            atomic_write_bytes(
                self._auto_artifact_path(temporary, record.baseline, "baseline.md"),
                baseline,
            )
            if record.shared_context is not None and shared_context is not None:
                atomic_write_bytes(
                    self._auto_artifact_path(
                        temporary,
                        record.shared_context,
                        "shared-context.md",
                    ),
                    shared_context,
                )
            atomic_write_json(temporary / "config.json", record.to_dict())
            os.replace(temporary, destination)
            published = True
            current_index = self._load_or_rebuild_auto_index()
            current_runs = dict(current_index["runs"])
            current_runs[record.id] = record.number
            current_index["runs"] = current_runs
            atomic_write_json(self.auto_index_path, current_index)
            self._invalidate_auto_directory_cache()
        except Exception:
            if not published:
                try:
                    info = temporary.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISLNK(info.st_mode):
                        temporary.unlink()
                    elif stat.S_ISDIR(info.st_mode):
                        _assert_no_symlink_components(
                            temporary,
                            self.auto_runs_root,
                            allow_missing_leaf=False,
                        )
                        shutil.rmtree(temporary)
            raise
        return record

    def load_auto_run(self, auto_id: str) -> AutoRunRecord:
        location = self._auto_location(auto_id)
        record = self._load_auto_record_at(
            location.path,
            expected_number=location.number,
            legacy=location.legacy,
        )
        if record.id != auto_id:
            raise OwnershipError("Auto run identity does not match directory")
        return record

    def load_auto_run_reference(self, value: str) -> AutoRunRecord:
        reference = parse_auto_reference(value)
        if isinstance(reference, str):
            return self.load_auto_run(reference)
        run_dir = self.auto_runs_root / str(reference)
        try:
            run_dir.lstat()
        except FileNotFoundError as exc:
            raise NotFoundError(f"Auto run not found: {value}") from exc
        except OSError as exc:
            raise StorageError("Auto run directory is unavailable") from exc
        return self._load_auto_record_at(
            run_dir,
            expected_number=reference,
            legacy=False,
        )

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

    def context_dir(self, session_id: str) -> Path:
        """The session's durable context artifacts, outliving every round."""

        path = self.session_dir(session_id) / "context"
        _assert_no_symlink_components(path, self.sessions_root, allow_missing_leaf=True)
        return path

    def context_summary_path(
        self,
        session_id: str,
        summary: ContextSummaryArtifact,
    ) -> Path:
        root = self.session_dir(session_id)
        path = root / summary.path
        _assert_no_symlink_components(path, root, allow_missing_leaf=False)
        return path

    def load_context_summary(
        self,
        session_id: str,
        summary: ContextSummaryArtifact,
        maximum_bytes: int,
    ) -> bytes:
        """Read a session's summary, refusing anything but the exact bytes it names.

        Fails closed on a missing, oversized or tampered artifact: the caller is
        about to retire the rounds this stands in for, so a summary that cannot
        be verified must stop the turn rather than quietly shrink its context.
        """

        if maximum_bytes < 1:
            raise ValueError("context summary limit must be positive")
        data = self._load_owned_bytes(
            self.context_summary_path(session_id, summary),
            self.session_dir(session_id),
            maximum_bytes,
            "context summary",
        )
        if sha256(data).hexdigest() != summary.sha256:
            raise OwnershipError("context summary digest does not match")
        return data

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
        status = self.auto_migration_status()
        if status.issues:
            raise StorageError(status.issues[0].message)
        records = list(status.readable_records)
        if any(record.number is None for record in records):
            return sorted(records, key=lambda item: (item.created_at, item.id))
        return sorted(records, key=lambda item: item.number)

    def auto_number_map_for_view(self) -> dict[str, int]:
        return {
            record.id: record.number
            for record in self.auto_migration_status().readable_records
            if record.number is not None
        }

    def active_auto_run_id(self) -> str | None:
        active = self._load_manifest().get("active_auto_run_id")
        return str(active) if active is not None else None

    def require_auto_inactive(self) -> None:
        active_id = self.active_auto_run_id()
        if active_id is None:
            return
        try:
            self.load_auto_run(active_id)
        except NotFoundError:
            raise ConflictError(
                "Auto is unavailable: the active Auto run is missing. "
                "Restore its run directory or repair .delibra/manifest.json "
                "before continuing."
            ) from None
        except StorageError:
            raise _auto_migration_conflict(self.auto_migration_status()) from None
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
        return self._session_location(session_id).path

    def is_legacy_session(self, session_id: str) -> bool:
        return self._session_location(session_id).legacy

    def _invalidate_session_directory_cache(self) -> None:
        self._session_directory_cache = None

    def _scan_session_directories(self) -> dict[str, _SessionDirectory]:
        discovered: dict[str, _SessionDirectory] = {}
        for child in sorted(self.sessions_root.iterdir(), key=lambda item: item.name):
            if child.name.startswith("."):
                continue
            try:
                child_info = child.lstat()
            except OSError as exc:
                raise StorageError("failed to inspect session storage") from exc
            if stat.S_ISLNK(child_info.st_mode):
                raise OwnershipError("session directory must not be a symlink")
            if not stat.S_ISDIR(child_info.st_mode):
                continue
            _assert_no_symlink_components(
                child,
                self.sessions_root,
                allow_missing_leaf=False,
            )
            data = load_json_recover(child / "config.json")
            config = SessionConfig.from_dict(data)
            validate_id(config.id, "session id")
            if config.id in discovered:
                raise OwnershipError("session UUID is stored more than once")
            legacy = child.name == config.id
            if not legacy:
                try:
                    expected = normalize_agent_name(config.name)
                except ValueError as exc:
                    raise OwnershipError("named session directory is invalid") from exc
                if config.name != expected or not _agent_directory_name_matches(
                    child.name,
                    expected,
                ):
                    raise OwnershipError(
                        "session directory does not match its immutable agent name"
                    )
            discovered[config.id] = _SessionDirectory(child, config, legacy)
        self._session_directory_cache = {
            session_id: _SessionLocation(entry.path, entry.legacy)
            for session_id, entry in discovered.items()
        }
        return dict(discovered)

    def _session_location(self, session_id: str) -> _SessionLocation:
        session_id = validate_id(session_id, "session id")
        if self._session_directory_cache is None:
            self._scan_session_directories()
        assert self._session_directory_cache is not None
        location = self._session_directory_cache.get(session_id)
        if location is None:
            raise NotFoundError(f"session not found: {session_id}")
        return location

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
        config.name = normalize_agent_name(config.name)
        entries = self._scan_session_directories()
        if config.id in entries:
            raise ConflictError(f"session already exists: {config.id}")
        wanted_key = agent_name_key(config.name)
        for entry in entries.values():
            try:
                existing_key = agent_name_key(entry.config.name)
            except ValueError:
                continue
            if existing_key == wanted_key:
                raise ConflictError("agent name is already in use")
        destination = self.sessions_root / config.name
        _assert_no_symlink_components(
            destination,
            self.sessions_root,
            allow_missing_leaf=True,
        )
        if destination.exists():
            raise ConflictError("agent directory already exists")
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
        self._invalidate_session_directory_cache()
        return config

    def load_session(self, session_id: str) -> SessionConfig:
        session_id = validate_id(session_id, "session id")
        location = self._session_location(session_id)
        _assert_no_symlink_components(
            location.path,
            self.sessions_root,
            allow_missing_leaf=False,
        )
        config = SessionConfig.from_dict(
            load_json_recover(location.path / "config.json")
        )
        if config.id != session_id:
            raise OwnershipError("session config identity does not match directory")
        if not location.legacy and (
            not _agent_directory_name_matches(location.path.name, config.name)
            or normalize_agent_name(config.name) != config.name
        ):
            raise OwnershipError(
                "session directory does not match its immutable agent name"
            )
        return config

    def save_session(self, config: SessionConfig) -> None:
        validate_id(config.id, "session id")
        location = self._session_location(config.id)
        if not location.legacy:
            try:
                normalized = normalize_agent_name(config.name)
            except ValueError as exc:
                raise ConflictError("agent name is immutable") from exc
            if normalized != config.name or not _agent_directory_name_matches(
                location.path.name,
                config.name,
            ):
                raise ConflictError("agent name is immutable")
        _assert_no_symlink_components(
            location.path,
            self.sessions_root,
            allow_missing_leaf=False,
        )
        atomic_write_json(location.path / "config.json", config.to_dict())

    def list_sessions(self) -> list[SessionConfig]:
        entries = self._scan_session_directories()
        return [
            SessionConfig.from_dict(entry.config.to_dict())
            for entry in sorted(
                entries.values(),
                key=lambda item: (item.config.name.casefold(), item.config.id),
            )
        ]

    def has_legacy_session_directories(self) -> bool:
        for child in self.sessions_root.iterdir():
            try:
                info = child.lstat()
            except OSError as exc:
                raise StorageError("failed to inspect session storage") from exc
            if stat.S_ISDIR(info.st_mode) and ID_PATTERN.fullmatch(child.name):
                return True
        return False

    def session_migration_status(self) -> SessionMigrationStatus:
        entries = self._scan_session_directories()
        issues: list[SessionMigrationIssue] = []
        name_owners: dict[str, list[_SessionDirectory]] = {}
        for entry in entries.values():
            try:
                normalized = normalize_agent_name(entry.config.name)
            except ValueError as exc:
                if entry.legacy:
                    issues.append(
                        SessionMigrationIssue(
                            entry.config.id,
                            entry.config.name,
                            str(exc),
                        )
                    )
                continue
            name_owners.setdefault(agent_name_key(normalized), []).append(entry)
            target = self.sessions_root / normalized
            if entry.legacy and target.exists() and target != entry.path:
                issues.append(
                    SessionMigrationIssue(
                        entry.config.id,
                        entry.config.name,
                        "agent directory name already exists",
                    )
                )
        for owners in name_owners.values():
            if len(owners) > 1:
                for entry in owners:
                    if entry.legacy:
                        issues.append(
                            SessionMigrationIssue(
                                entry.config.id,
                                entry.config.name,
                                "agent name is duplicated",
                            )
                        )
        unique = {
            (issue.session_id, issue.name, issue.message): issue for issue in issues
        }
        return SessionMigrationStatus(
            legacy_session_ids=tuple(
                sorted(entry.config.id for entry in entries.values() if entry.legacy)
            ),
            issues=tuple(unique[key] for key in sorted(unique)),
        )

    def migrate_session_directories(self) -> SessionMigrationStatus:
        status = self.session_migration_status()
        if status.issues:
            return status
        entries = self._scan_session_directories()
        for session_id in status.legacy_session_ids:
            entry = entries[session_id]
            # Unconditional: this also repairs a directory left between the two
            # pair writes by an interrupted earlier attempt.
            config = SessionConfig.from_dict(entry.config.to_dict())
            config.cli_session_id = None
            _assert_no_symlink_components(
                entry.path,
                self.sessions_root,
                allow_missing_leaf=False,
            )
            atomic_write_json_recovery_pair(
                entry.path / "config.json",
                config.to_dict(),
            )
            destination = self.sessions_root / normalize_agent_name(entry.config.name)
            try:
                os.replace(entry.path, destination)
            except OSError as exc:
                raise StorageError(
                    f"failed to migrate agent directory: {entry.config.name}"
                ) from exc
            _fsync_directory(self.sessions_root)
            self._invalidate_session_directory_cache()
        verified = self.session_migration_status()
        if not verified.complete:
            raise StorageError("agent directory migration did not complete")
        return verified

    def clear_native_session_ids_for_relocation(self) -> int:
        cleared = 0
        for entry in self._scan_session_directories().values():
            # Rewrite every pair, including sessions already holding None, so an
            # interrupted relocation cannot leave a stale recoverable fallback.
            config = SessionConfig.from_dict(entry.config.to_dict())
            if config.cli_session_id is not None:
                cleared += 1
            config.cli_session_id = None
            _assert_no_symlink_components(
                entry.path,
                self.sessions_root,
                allow_missing_leaf=False,
            )
            atomic_write_json_recovery_pair(
                entry.path / "config.json",
                config.to_dict(),
            )
        return cleared

    def set_legacy_session_name(
        self,
        session_id: str,
        name: str,
    ) -> SessionMigrationStatus:
        session_id = validate_id(session_id, "session id")
        entries = self._scan_session_directories()
        entry = entries.get(session_id)
        if entry is None:
            raise NotFoundError(f"session not found: {session_id}")
        if not entry.legacy:
            raise ConflictError("agent name is immutable")
        normalized = normalize_agent_name(name)
        wanted_key = agent_name_key(normalized)
        for other in entries.values():
            if other.config.id == session_id:
                continue
            try:
                other_key = agent_name_key(other.config.name)
            except ValueError:
                continue
            if other_key == wanted_key:
                raise ConflictError("agent name is already in use")
        destination = self.sessions_root / normalized
        if destination.exists() and destination != entry.path:
            raise ConflictError("agent directory already exists")
        config = self.load_session(session_id)
        config.name = normalized
        config.cli_session_id = None
        _assert_no_symlink_components(
            entry.path,
            self.sessions_root,
            allow_missing_leaf=False,
        )
        atomic_write_json_recovery_pair(
            entry.path / "config.json",
            config.to_dict(),
        )
        try:
            os.replace(entry.path, destination)
        except OSError as exc:
            raise StorageError(
                f"failed to migrate agent directory: {normalized}"
            ) from exc
        _fsync_directory(self.sessions_root)
        self._invalidate_session_directory_cache()
        migrated = self._scan_session_directories().get(session_id)
        if (
            migrated is None
            or migrated.legacy
            or not _agent_directory_name_matches(migrated.path.name, normalized)
        ):
            raise StorageError("agent directory migration did not complete")
        return self.session_migration_status()

    def delete_session(self, session_id: str) -> None:
        destination = self.session_dir(session_id)
        config = self.load_session(session_id)
        if config.id != session_id:
            raise OwnershipError("session identity mismatch")
        if config.status == "running":
            raise ConflictError("cannot delete a running session")
        shutil.rmtree(destination)
        self._invalidate_session_directory_cache()

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

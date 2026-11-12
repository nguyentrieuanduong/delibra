"""One verified zip of the project data a Clear-and-reset level removes.

The originals are never touched here: the caller removes them only after
write_archive returns, so a failure at any step loses nothing. Every entry is
reached through no-follow directory descriptors and checked against what was
listed, so a path swapped for a symlink mid-walk can never pull a file from
outside .delibra/ into the zip.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4
import zipfile

from app.storage import (
    OwnershipError,
    StorageError,
    _fsync_directory,
    ensure_owned_directory,
)

ARCHIVE_DIRNAME = "archive"
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
# O_NONBLOCK: a file swapped for a FIFO must not hang the open.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC
_ZIP_EPOCH = 315_532_800  # 1980-01-01 UTC, the earliest time a zip can store
_COPY_CHUNK = 1024 * 1024
_NAME_ATTEMPTS = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _candidate(archive_root: Path, label: str) -> Path:
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    return archive_root / f"{stamp}-{label}-{uuid4().hex[:8]}.zip"


def _date_time(seconds: float) -> tuple[int, int, int, int, int, int]:
    moment = datetime.fromtimestamp(max(seconds, _ZIP_EPOCH), timezone.utc)
    return moment.timetuple()[:6]


def _same_file(listed: os.stat_result, opened: os.stat_result) -> bool:
    return (listed.st_dev, listed.st_ino) == (opened.st_dev, opened.st_ino)


def _source_parts(root: Path, archive_root: Path, sources: Sequence[Path]) -> list[tuple[str, ...]]:
    """Each source as path components under root; refuses anything else."""

    found: list[tuple[str, ...]] = []
    for source in sources:
        source = source.absolute()
        if source == root or source == archive_root or archive_root in source.parents:
            raise OwnershipError(f"archive source is not project data: {source}")
        try:
            parts = source.relative_to(root).parts
        except ValueError as exc:
            raise OwnershipError(f"archive source escapes .delibra: {source}") from exc
        if ".." in parts:
            raise OwnershipError(f"archive source escapes .delibra: {source}")
        found.append(parts)
    return found


class _Writer:
    """Adds entries depth-first, sorted by name, never following a symlink."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.names: list[str] = []

    def add_source(self, root_descriptor: int, parts: tuple[str, ...]) -> None:
        opened: list[int] = []
        parent = root_descriptor
        try:
            for part in parts[:-1]:
                try:
                    parent = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    return  # missing sources are skipped
                opened.append(parent)
            self._visit(parent, parts[-1], "/".join(parts))
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def _entry(self, name: str, info: os.stat_result) -> zipfile.ZipInfo:
        entry = zipfile.ZipInfo(name, date_time=_date_time(info.st_mtime))
        entry.external_attr = (info.st_mode & 0xFFFF) << 16
        return entry

    def _visit(self, parent: int, leaf: str, name: str) -> None:
        try:
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode):
            descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                if not _same_file(info, os.fstat(descriptor)):
                    raise StorageError(f"{name} changed while it was archived")
                entry = self._entry(f"{name}/", info)
                entry.external_attr |= 0x10  # MS-DOS directory flag
                self.archive.writestr(entry, b"")
                self.names.append(entry.filename)
                for child in sorted(os.listdir(descriptor)):
                    self._visit(descriptor, child, f"{name}/{child}")
            finally:
                os.close(descriptor)
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(leaf, dir_fd=parent)
            self.archive.writestr(self._entry(name, info), os.fsencode(target))
            self.names.append(name)
        elif stat.S_ISREG(info.st_mode):
            with os.fdopen(os.open(leaf, _FILE_FLAGS, dir_fd=parent), "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or not _same_file(info, opened):
                    raise StorageError(f"{name} changed while it was archived")
                entry = self._entry(name, opened)
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.file_size = opened.st_size
                with self.archive.open(entry, "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, _COPY_CHUNK)
            self.names.append(name)
        # FIFOs, sockets and devices hold no data: skipped.


def _reserve(archive_root: Path, label: str, candidate: Path) -> Path:
    """Claim a final name exclusively, so an earlier archive is never replaced."""

    for _ in range(_NAME_ATTEMPTS):
        try:
            os.close(os.open(candidate, _CREATE_FLAGS, 0o600))
        except FileExistsError:
            candidate = _candidate(archive_root, label)
            continue
        return candidate
    raise StorageError("archive could not be named: every candidate name is taken")


def write_archive(root: Path, label: str, sources: Sequence[Path]) -> Path:
    """Zip `sources` (paths under `root`, kept relative) into root/archive/.

    Written under a temporary name, fsynced, checked with testzip and
    against the entries written, then moved onto an exclusively reserved
    final name. Raises StorageError; on any failure nothing is left behind.
    """

    root = root.absolute()
    archive_root = ensure_owned_directory(root / ARCHIVE_DIRNAME, root)
    parts = _source_parts(root, archive_root, sources)
    final = _candidate(archive_root, label)
    temporary = archive_root / f".{final.name}.tmp"
    created = False
    try:
        root_descriptor = os.open(root, _DIRECTORY_FLAGS)
        try:
            handle = os.fdopen(os.open(temporary, _CREATE_FLAGS, 0o600), "wb")
            created = True
            with handle:
                with zipfile.ZipFile(
                    handle, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
                ) as archive:
                    writer = _Writer(archive)
                    for source in parts:
                        writer.add_source(root_descriptor, source)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(root_descriptor)
        with zipfile.ZipFile(temporary) as check:
            broken = check.testzip()
            if broken is not None:
                raise StorageError(f"archive check failed at {broken}")
            if check.namelist() != writer.names:
                raise StorageError("archive check found missing entries")
        final = _reserve(archive_root, label, final)
        try:
            os.replace(temporary, final)
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        _fsync_directory(archive_root)
    except BaseException as exc:
        if created:
            temporary.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise StorageError(
                f"archive could not be written: {exc.strerror or exc}"
            ) from exc
        raise
    return final

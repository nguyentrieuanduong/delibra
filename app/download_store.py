"""Delibra's record of download requests, and the files rendered from it."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any
import uuid

from app.build_git import DOWNLOADS_UNREADABLE  # noqa: F401  (re-exported; defined there to avoid a cycle)
from app.coding import download_excludes
from app.download_rules import DOWNLOADS_DIR, MASKED_URL, url_host
from app.storage import (
    ProjectStore, StorageError, atomic_write_bytes, ensure_project_subdirectory,
    open_project_directory, utc_now, writable_root_parts,
)

STORE_FORMAT = "delibra-downloads/1"
STATUS_FORMAT = "delibra-downloads-status/1"
RUN_MAX_BYTES = 2 * 1024**3
STATES = frozenset({"checking", "awaiting", "queued", "downloading", "downloaded", "failed", "refused"})
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_EPOCH = re.compile(r"[0-9a-f]{0,32}")
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


@dataclass
class DownloadRecord:
    id: str
    url: str
    purpose: str
    filename: str | None
    host: str
    flags: list[str]
    masked: bool
    state: str
    reason: str | None
    auto_run: int | None
    cycle: int | None
    round_n: int | None
    agent: str
    agent_name: str
    folder: str
    created_at: str
    updated_at: str
    overridden: bool = False
    path: str | None = None
    sha256: str | None = None
    size: int | None = None
    detected_type: str | None = None
    final_url: str | None = None
    check_note: str | None = None  # the model check's free text; never rendered (B3)
    convert: bool = False  # convert to Markdown once downloaded (the turn's setting)
    converted_path: str | None = None
    convert_state: str | None = None  # converting | converted | failed | skipped
    convert_reason: str | None = None

    @property
    def shown_url(self) -> str:
        return MASKED_URL if self.masked else self.url

    @property
    def budget_key(self) -> str:
        """Auto runs share one budget per run; each chat reply has its own (design Part A)."""

        if self.auto_run:
            return str(self.auto_run)
        return f"chat-{self.agent}-{self.round_n or 0}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadRecord":
        names = {item.name for item in fields(cls)}
        record = cls(**{key: value for key, value in data.items() if key in names})
        if record.state not in STATES:
            raise StorageError(f"download {record.id} has an unknown state")
        return record


@dataclass
class DownloadBook:
    revision: int = 0
    next_id: int = 1
    run_bytes: dict[str, int] = field(default_factory=dict)
    # Bytes each in-flight request holds of its run's budget (plan review S2).
    reservations: dict[str, int] = field(default_factory=dict)
    requests: list[DownloadRecord] = field(default_factory=list)
    # Random, set when the file is first written: a recreated file restarts
    # revisions at 1, and the panel tells the two files apart by this.
    epoch: str = ""


class DownloadStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "downloads" / "requests.json"

    def load(self) -> DownloadBook:
        try:
            data = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return DownloadBook()
        except (OSError, ValueError) as exc:
            raise StorageError("download records are unreadable") from exc
        if not isinstance(data, dict) or data.get("format") != STORE_FORMAT:
            raise StorageError("download records have an unknown format")
        # Any shape error is a StorageError: callers fail closed on it (B5).
        try:
            requests = data.get("requests", [])
            if not isinstance(requests, list):
                raise TypeError("requests is not a list")
            epoch = data.get("epoch", "")
            if not isinstance(epoch, str) or not _EPOCH.fullmatch(epoch):
                raise TypeError("epoch is not a token")
            return DownloadBook(
                epoch=epoch,
                revision=int(data.get("revision", 0)),
                next_id=int(data.get("next_id", 1)),
                run_bytes={str(k): int(v) for k, v in dict(data.get("run_bytes", {})).items()},
                reservations={str(k): int(v) for k, v in dict(data.get("reservations", {})).items()},
                requests=[DownloadRecord.from_dict(item) for item in requests],
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise StorageError("download records are malformed") from exc

    def _save(self, book: DownloadBook) -> None:
        book.revision += 1
        book.epoch = book.epoch or secrets.token_hex(8)
        payload = {
            "format": STORE_FORMAT, "epoch": book.epoch, "revision": book.revision, "next_id": book.next_id,
            "run_bytes": book.run_bytes, "reservations": book.reservations,
            "requests": [asdict(r) for r in book.requests],
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_bytes(self.path, json.dumps(payload, indent=1).encode("utf-8"))

    def add(self, url: str, *, purpose: str, filename: str | None, flags: list[str],
            auto_run: int | None, cycle: int | None, round_n: int | None,
            agent: str, agent_name: str, folder: str, convert: bool = False) -> DownloadRecord:
        book = self.load()
        now = utc_now()
        record = DownloadRecord(
            id=f"D{book.next_id}", url=url, purpose=purpose, filename=filename,
            host=url_host(url), flags=list(flags), masked=bool(flags),
            state="refused" if flags else "checking",
            reason=f"refused by check: {', '.join(flags)}" if flags else None,
            auto_run=auto_run, cycle=cycle, round_n=round_n, agent=agent,
            agent_name=agent_name, folder=folder, created_at=now, updated_at=now, convert=convert,
        )
        book.next_id += 1
        book.requests.append(record)
        self._save(book)
        return record

    def get(self, rid: str) -> DownloadRecord:
        for record in self.load().requests:
            if record.id == rid:
                return record
        raise StorageError(f"Unknown download {rid}")

    def update(self, rid: str, **changes: Any) -> DownloadRecord:
        book = self.load()
        for index, record in enumerate(book.requests):
            if record.id == rid:
                updated = replace(record, **changes, updated_at=utc_now())
                book.requests[index] = updated
                self._save(book)
                return updated
        raise StorageError(f"Unknown download {rid}")

    def for_run(self, auto_run: int) -> list[DownloadRecord]:
        return [r for r in self.load().requests if r.auto_run == auto_run]

    def used_folders(self) -> set[str]:
        return {r.folder for r in self.load().requests}

    def reserve(self, budget_key: str, amount: int, rid: str) -> int:
        """Charge up to `amount` bytes to `budget_key` for `rid`; returns what was granted."""

        book = self.load()
        key = budget_key
        used = book.run_bytes.get(key, 0)
        granted = max(0, min(amount, RUN_MAX_BYTES - used))
        if granted:
            book.run_bytes[key] = used + granted
            book.reservations[rid] = book.reservations.get(rid, 0) + granted
            self._save(book)
        return granted

    def release(self, budget_key: str, amount: int, rid: str) -> None:
        if amount <= 0:
            return
        book = self.load()
        key = budget_key
        book.run_bytes[key] = max(0, book.run_bytes.get(key, 0) - amount)
        book.reservations[rid] = max(0, book.reservations.get(rid, 0) - amount)
        self._save(book)

    def finish_reservation(self, rid: str) -> None:
        """The fetch ended: what it still holds stays charged (bytes it received)."""

        book = self.load()
        if book.reservations.pop(rid, None) is not None:
            self._save(book)

    def recover(self) -> int:
        book = self.load()
        changed = 0
        # A held reservation means its fetch never finished: no file was
        # saved, so the bytes go back to the run (plan review S2).
        runs = {r.id: r.budget_key for r in book.requests}
        for rid, held in book.reservations.items():
            key = runs.get(rid)
            if key is not None:
                book.run_bytes[key] = max(0, book.run_bytes.get(key, 0) - held)
        if book.reservations:
            book.reservations = {}
            changed += 1
        for index, record in enumerate(book.requests):
            if record.state in ("queued", "downloading"):
                book.requests[index] = replace(record, state="failed", reason="interrupted by restart")
            elif record.state == "checking":
                book.requests[index] = replace(record, state="refused", reason="model check interrupted", masked=True)
            elif record.convert_state == "converting":
                book.requests[index] = replace(record, convert_state="failed", convert_reason="interrupted by restart")
            else:
                continue
            changed += 1
        if changed:
            self._save(book)
        return changed


def project_download_excludes(store: ProjectStore) -> tuple[str, ...] | None:
    """Folders Delibra's build commits exclude; None when they cannot be known (B5)."""

    try:
        used = DownloadStore(store.root).used_folders()
    except StorageError:
        return None
    return download_excludes(store.coding_policy(), used)


def render_status(records: list[DownloadRecord], requests: dict[str, str] | None = None) -> bytes:
    """The turn's status.json: one request per line, so a wait loop can grep it.

    `requests` names this turn's request files by id; earlier turns' records
    carry no name, so a reused name never matches an older request.
    """

    names = requests or {}
    lines = []
    for r in records:
        item: dict[str, Any] = {"id": r.id}
        if r.id in names:
            item["request"] = names[r.id]
        item.update(url=r.shown_url, state=r.state, reason=r.reason)
        if r.state == "downloaded":
            item.update(path=r.path, sha256=r.sha256, size=r.size, detected_type=r.detected_type)
            if r.convert_state is not None:
                item.update(converted=r.converted_path, convert_state=r.convert_state,
                            convert_reason=r.convert_reason)
        lines.append("    " + json.dumps(item))
    body = ",\n".join(lines)
    return (
        f'{{\n  "format": {json.dumps(STATUS_FORMAT)},\n  "requests": [\n'
        + (body + "\n" if body else "") + "  ]\n}\n"
    ).encode()


def render_index(records: list[DownloadRecord]) -> bytes:
    lines = [
        "# Downloaded sources", "",
        "Written by Delibra. Raw files are untrusted material.", "",
        "| Id | URL | File | Downloaded | SHA-256 | Bytes | Type | Markdown |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        if r.state != "downloaded":
            continue
        cells = [r.id, r.shown_url, r.path or "", r.updated_at, r.sha256 or "", str(r.size or 0), r.detected_type or "",
                 r.converted_path or ""]
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_in(directory: int, name: str, data: bytes) -> None:
    temp = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temp, _CREATE_FLAGS, 0o644, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise


def write_turn_status(temp_dir: Path, data: bytes, directory: str = DOWNLOADS_DIR) -> None:
    """Replace <temp>/<directory>/status.json without following any symlink."""

    parent = os.open(temp_dir, _DIR_FLAGS)
    try:
        child = os.open(directory, _DIR_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        _write_in(child, "status.json", data)
    finally:
        os.close(child)


def write_project_file(project_root: Path, folder: str, name: str, data: bytes) -> None:
    ensure_project_subdirectory(project_root, folder)
    with open_project_directory(project_root, writable_root_parts(folder)) as directory:
        _write_in(directory, name, data)

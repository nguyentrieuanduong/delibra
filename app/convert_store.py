"""Delibra's record of conversion requests (design 2026-11-15, Part B)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import json
from pathlib import Path
import secrets
from typing import Any

from app.storage import StorageError, atomic_write_bytes, utc_now

FORMAT = "delibra-conversions/1"
STATUS_FORMAT = "delibra-conversions-status/1"
# queued: waiting for the server's one conversion slot; converting: holding it.
STATES = frozenset({"queued", "converting", "converted", "failed", "refused", "cancelled"})
ACTIVE = frozenset({"queued", "converting"})


@dataclass(frozen=True)
class ConversionRecord:
    id: str
    path: str
    request: str
    state: str
    reason: str | None
    auto_run: int
    round_n: int | None
    agent: str
    created_at: str
    updated_at: str
    source_sha256: str | None = None
    output: str | None = None
    # `<download folder>/converted`, where `output` is; older records do not have it.
    converted_root: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversionRecord":
        names = {item.name for item in fields(cls)}
        record = cls(**{key: value for key, value in data.items() if key in names})
        if record.state not in STATES:
            raise StorageError(f"conversion {record.id} has an unknown state")
        return record


@dataclass(frozen=True)
class _Book:
    next_id: int = 1
    records: tuple[ConversionRecord, ...] = ()
    # Like the download records: the panel re-renders on a newer revision, and a
    # recreated file (revisions restart) is told apart by its random epoch.
    revision: int = 0
    epoch: str = ""


class ConversionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "downloads" / "conversions.json"

    def _book(self) -> _Book:
        try:
            data = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return _Book()
        except (OSError, ValueError) as exc:
            raise StorageError("conversion records are unreadable") from exc
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise StorageError("conversion records have an unknown format")
        try:
            return _Book(
                int(data["next_id"]), tuple(ConversionRecord.from_dict(item) for item in data["records"]),
                int(data.get("revision", 0)), str(data.get("epoch", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError("conversion records are malformed") from exc

    def _load(self) -> tuple[int, list[ConversionRecord]]:
        book = self._book()
        return book.next_id, list(book.records)

    def _save(self, next_id: int, records: list[ConversionRecord]) -> None:
        book = self._book()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "format": FORMAT, "next_id": next_id, "revision": book.revision + 1,
            "epoch": book.epoch or secrets.token_hex(8), "records": [asdict(r) for r in records],
        }
        atomic_write_bytes(self.path, json.dumps(payload, indent=1).encode("utf-8"))

    def load(self) -> list[ConversionRecord]:
        return self._load()[1]

    def revision(self) -> tuple[int, str]:
        """(revision, epoch) for the Downloads panel's live refresh."""

        book = self._book()
        return book.revision, book.epoch

    def add(self, path: str, *, request: str, auto_run: int, round_n: int | None,
            agent: str) -> ConversionRecord:
        next_id, records = self._load()
        now = utc_now()
        record = ConversionRecord(
            id=f"C{next_id}", path=path, request=request, state="queued", reason=None,
            auto_run=auto_run, round_n=round_n, agent=agent, created_at=now, updated_at=now,
        )
        self._save(next_id + 1, [*records, record])
        return record

    def update(self, cid: str, **changes: Any) -> ConversionRecord:
        next_id, records = self._load()
        for index, record in enumerate(records):
            if record.id == cid:
                records[index] = replace(record, **changes, updated_at=utc_now())
                self._save(next_id, records)
                return records[index]
        raise StorageError(f"Unknown conversion {cid}")

    def recover(self) -> int:
        """Startup: a conversion queued or running when the server stopped did not finish."""

        next_id, records = self._load()
        stopped = [i for i, r in enumerate(records) if r.state in ACTIVE]
        for index in stopped:
            records[index] = replace(records[index], state="failed", reason="interrupted by restart",
                                     updated_at=utc_now())
        if stopped:
            self._save(next_id, records)
        return len(stopped)

    def latest_converted(self, path: str, source_sha256: str) -> ConversionRecord | None:
        for record in reversed(self.load()):
            if record.path == path and record.state == "converted" and record.source_sha256 == source_sha256:
                return record
        return None


def render_convert_status(records: list[ConversionRecord], requests: dict[str, str]) -> bytes:
    """One request per line, as for downloads, so a wait loop can grep it."""

    lines = []
    for record in records:
        item: dict[str, Any] = {"id": record.id}
        if record.id in requests:
            item["request"] = requests[record.id]
        item.update(path=record.path, state=record.state, reason=record.reason, output=record.output)
        lines.append("    " + json.dumps(item))
    body = ",\n".join(lines)
    return (
        f'{{\n  "format": {json.dumps(STATUS_FORMAT)},\n  "requests": [\n'
        + (body + "\n" if body else "") + "  ]\n}\n"
    ).encode()

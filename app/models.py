"""Serializable domain models for Delibra's filesystem metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: str
    created_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            path=str(data["path"]),
            created_at=str(data["created_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceDescriptor:
    type: str
    from_session: str | None = None
    from_round: int | None = None
    staged_file: str | None = None
    source_sha256: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceDescriptor":
        return cls(
            type=str(data["type"]),
            from_session=data.get("from_session"),
            from_round=data.get("from_round"),
            staged_file=data.get("staged_file"),
            source_sha256=data.get("source_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class RoundRecord:
    n: int
    status: str
    error: str | None
    warnings: list[str]
    agent: str
    model: str
    effort: str
    started_at: str
    finished_at: str | None
    source: SourceDescriptor

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoundRecord":
        return cls(
            n=int(data["n"]),
            status=str(data["status"]),
            error=data.get("error"),
            warnings=[str(item) for item in data.get("warnings", [])],
            agent=str(data["agent"]),
            model=str(data["model"]),
            effort=str(data["effort"]),
            started_at=str(data["started_at"]),
            finished_at=data.get("finished_at"),
            source=SourceDescriptor.from_dict(data.get("source", {"type": "user"})),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source"] = self.source.to_dict()
        return result


@dataclass
class SessionConfig:
    id: str
    name: str
    agent: str
    model: str
    effort: str
    role_instructions: str
    cli_session_id: str | None
    status: str
    created_at: str
    rounds: list[RoundRecord] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionConfig":
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            agent=str(data["agent"]),
            model=str(data["model"]),
            effort=str(data["effort"]),
            role_instructions=str(data.get("role_instructions", "")),
            cli_session_id=data.get("cli_session_id"),
            status=str(data["status"]),
            created_at=str(data["created_at"]),
            rounds=[RoundRecord.from_dict(item) for item in data.get("rounds", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["rounds"] = [item.to_dict() for item in self.rounds]
        return result


@dataclass(frozen=True)
class RunKey:
    project_id: str
    session_id: str
    round_n: int

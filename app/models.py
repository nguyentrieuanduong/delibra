"""Serializable domain models for Delibra's filesystem metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


AUTO_FORMAT = "delibra-auto/1"


def _strict_int(data: dict[str, Any], key: str, *, default: int | None = None) -> int:
    if key not in data:
        if default is None:
            raise KeyError(key)
        return default
    value = data[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _strict_bool(data: dict[str, Any], key: str) -> bool:
    value = data[key]
    if type(value) is not bool:
        raise TypeError(f"{key} must be a boolean")
    return value


def _strict_str(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be a string")
    return value


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


@dataclass(frozen=True)
class SharedContextDescriptor:
    path: str
    staged_file: str
    sha256: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SharedContextDescriptor":
        return cls(
            str(data["path"]),
            str(data["staged_file"]),
            str(data["sha256"]),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class AutoRoundDescriptor:
    auto_id: str
    phase: str
    cycle: int | None
    position: int
    context_file: str
    context_sha256: str
    verdict: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoRoundDescriptor":
        return cls(
            auto_id=str(data["auto_id"]),
            phase=str(data["phase"]),
            cycle=(
                _strict_int(data, "cycle") if data.get("cycle") is not None else None
            ),
            position=_strict_int(data, "position"),
            context_file=str(data["context_file"]),
            context_sha256=str(data["context_sha256"]),
            verdict=str(data["verdict"]) if data.get("verdict") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class TimeoutExtensionRecord:
    added_seconds: int
    scope: str
    extended_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimeoutExtensionRecord":
        return cls(
            added_seconds=_strict_int(data, "added_seconds"),
            scope=str(data["scope"]),
            extended_at=str(data["extended_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TimeoutRecord:
    initial_seconds: int
    effective_seconds: int
    hard_cap_seconds: int
    deadline_at: str
    version: int = 0
    extensions: list[TimeoutExtensionRecord] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimeoutRecord":
        return cls(
            initial_seconds=_strict_int(data, "initial_seconds"),
            effective_seconds=_strict_int(data, "effective_seconds"),
            hard_cap_seconds=_strict_int(data, "hard_cap_seconds"),
            deadline_at=str(data["deadline_at"]),
            version=_strict_int(data, "version", default=0),
            extensions=[
                TimeoutExtensionRecord.from_dict(item)
                for item in data.get("extensions", [])
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["extensions"] = [item.to_dict() for item in self.extensions]
        return result


@dataclass(frozen=True)
class AutoParticipant:
    session_id: str
    name: str
    agent: str
    model: str
    effort: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoParticipant":
        return cls(
            session_id=str(data["session_id"]),
            name=str(data["name"]),
            agent=str(data["agent"]),
            model=str(data["model"]),
            effort=str(data["effort"]),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class AutoResumption:
    """One Continue-Auto grant, recorded so every restart is auditable."""

    resumed_at: str
    from_status: str
    max_cycles: int
    turn_timeout_seconds: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoResumption":
        return cls(
            resumed_at=_strict_str(data, "resumed_at"),
            from_status=_strict_str(data, "from_status"),
            max_cycles=_strict_int(data, "max_cycles"),
            turn_timeout_seconds=_strict_int(data, "turn_timeout_seconds"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AutoArtifact:
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoArtifact":
        return cls(path=str(data["path"]), sha256=str(data["sha256"]))

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class AutoBaselineEntry:
    session_id: str
    round_n: int
    started_at: str
    offset: int
    length: int
    sha256: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoBaselineEntry":
        return cls(
            session_id=str(data["session_id"]),
            round_n=_strict_int(data, "round_n"),
            started_at=str(data["started_at"]),
            offset=_strict_int(data, "offset"),
            length=_strict_int(data, "length"),
            sha256=str(data["sha256"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AutoTurn:
    phase: str
    session_id: str
    round_n: int
    cycle: int | None
    position: int
    output_sha256: str
    verdict: str | None = None
    warning: str | None = None
    timeout: TimeoutRecord | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoTurn":
        return cls(
            phase=str(data["phase"]),
            session_id=str(data["session_id"]),
            round_n=_strict_int(data, "round_n"),
            cycle=(
                _strict_int(data, "cycle") if data.get("cycle") is not None else None
            ),
            position=_strict_int(data, "position"),
            output_sha256=str(data["output_sha256"]),
            verdict=str(data["verdict"]) if data.get("verdict") is not None else None,
            warning=str(data["warning"]) if data.get("warning") is not None else None,
            timeout=(
                TimeoutRecord.from_dict(data["timeout"])
                if data.get("timeout") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {key: value for key, value in asdict(self).items() if value is not None}
        if self.timeout is not None:
            result["timeout"] = self.timeout.to_dict()
        return result


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
    shared_context: SharedContextDescriptor | None = None
    retry_of: int | None = None
    auto: AutoRoundDescriptor | None = None
    timeout: TimeoutRecord | None = None

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
            shared_context=(
                SharedContextDescriptor.from_dict(data["shared_context"])
                if data.get("shared_context") is not None
                else None
            ),
            retry_of=(
                int(data["retry_of"])
                if data.get("retry_of") is not None
                else None
            ),
            auto=(
                AutoRoundDescriptor.from_dict(data["auto"])
                if data.get("auto") is not None
                else None
            ),
            timeout=(
                TimeoutRecord.from_dict(data["timeout"])
                if data.get("timeout") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source"] = self.source.to_dict()
        if self.shared_context is None:
            result.pop("shared_context", None)
        else:
            result["shared_context"] = self.shared_context.to_dict()
        if self.retry_of is None:
            result.pop("retry_of", None)
        if self.auto is None:
            result.pop("auto", None)
        else:
            result["auto"] = self.auto.to_dict()
        if self.timeout is None:
            result.pop("timeout", None)
        else:
            result["timeout"] = self.timeout.to_dict()
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunKey":
        return cls(
            project_id=str(data["project_id"]),
            session_id=str(data["session_id"]),
            round_n=_strict_int(data, "round_n"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AutoRunRecord:
    id: str
    project_id: str
    number: int | None
    status: str
    agreement_policy: str
    preparation_enabled: bool
    max_cycles: int
    current_cycle: int
    next_participant: int
    participants: list[AutoParticipant]
    topic: AutoArtifact
    baseline: AutoArtifact
    baseline_entries: list[AutoBaselineEntry]
    shared_context: AutoArtifact | None
    shared_context_source: str | None
    preparations: list[AutoTurn]
    discussion: list[AutoTurn]
    active_key: RunKey | None
    future_turn_timeout_seconds: int
    active_timeout: TimeoutRecord | None
    stop_requested: bool
    created_at: str
    started_at: str | None
    finished_at: str | None
    terminal_reason: str | None
    # Absent in delibra-auto/1 records, so it must default.
    resumptions: list[AutoResumption] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoRunRecord":
        number = _strict_int(data, "number") if "number" in data else None
        if number is not None and number < 1:
            raise ValueError("number must be positive")
        return cls(
            id=_strict_str(data, "id"),
            project_id=_strict_str(data, "project_id"),
            number=number,
            status=_strict_str(data, "status"),
            agreement_policy=_strict_str(data, "agreement_policy"),
            preparation_enabled=(
                _strict_bool(data, "preparation_enabled")
                if "preparation_enabled" in data
                else True
            ),
            max_cycles=_strict_int(data, "max_cycles"),
            current_cycle=_strict_int(data, "current_cycle"),
            next_participant=_strict_int(data, "next_participant"),
            participants=[
                AutoParticipant.from_dict(item)
                for item in data.get("participants", [])
            ],
            topic=AutoArtifact.from_dict(data["topic"]),
            baseline=AutoArtifact.from_dict(data["baseline"]),
            baseline_entries=[
                AutoBaselineEntry.from_dict(item)
                for item in data.get("baseline_entries", [])
            ],
            shared_context=(
                AutoArtifact.from_dict(data["shared_context"])
                if data.get("shared_context") is not None
                else None
            ),
            shared_context_source=(
                str(data["shared_context_source"])
                if data.get("shared_context_source") is not None
                else None
            ),
            preparations=[
                AutoTurn.from_dict(item) for item in data.get("preparations", [])
            ],
            discussion=[
                AutoTurn.from_dict(item) for item in data.get("discussion", [])
            ],
            active_key=(
                RunKey.from_dict(data["active_key"])
                if data.get("active_key") is not None
                else None
            ),
            future_turn_timeout_seconds=_strict_int(
                data,
                "future_turn_timeout_seconds",
            ),
            active_timeout=(
                TimeoutRecord.from_dict(data["active_timeout"])
                if data.get("active_timeout") is not None
                else None
            ),
            stop_requested=_strict_bool(data, "stop_requested"),
            created_at=_strict_str(data, "created_at"),
            started_at=(
                str(data["started_at"]) if data.get("started_at") is not None else None
            ),
            finished_at=(
                str(data["finished_at"])
                if data.get("finished_at") is not None
                else None
            ),
            terminal_reason=(
                str(data["terminal_reason"])
                if data.get("terminal_reason") is not None
                else None
            ),
            resumptions=[
                AutoResumption.from_dict(item)
                for item in data.get("resumptions", [])
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": AUTO_FORMAT,
            "id": self.id,
            "project_id": self.project_id,
            **({"number": self.number} if self.number is not None else {}),
            "status": self.status,
            "agreement_policy": self.agreement_policy,
            "preparation_enabled": self.preparation_enabled,
            "max_cycles": self.max_cycles,
            "current_cycle": self.current_cycle,
            "next_participant": self.next_participant,
            "participants": [item.to_dict() for item in self.participants],
            "topic": self.topic.to_dict(),
            "baseline": self.baseline.to_dict(),
            "baseline_entries": [item.to_dict() for item in self.baseline_entries],
            "shared_context": (
                self.shared_context.to_dict()
                if self.shared_context is not None
                else None
            ),
            "shared_context_source": self.shared_context_source,
            "preparations": [item.to_dict() for item in self.preparations],
            "discussion": [item.to_dict() for item in self.discussion],
            "active_key": self.active_key.to_dict() if self.active_key is not None else None,
            "future_turn_timeout_seconds": self.future_turn_timeout_seconds,
            "active_timeout": (
                self.active_timeout.to_dict() if self.active_timeout is not None else None
            ),
            "stop_requested": self.stop_requested,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "terminal_reason": self.terminal_reason,
            "resumptions": [item.to_dict() for item in self.resumptions],
        }

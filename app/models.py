"""Serializable domain models for Delibra's filesystem metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import math
from pathlib import PurePosixPath
import re
from typing import Any


AUTO_FORMAT = "delibra-auto/1"

# Every numerator Phase 0 proved, named at the point of use so a later gate can
# change one provider's source without silently reinterpreting stored data.
NUMERATOR_SOURCES = frozenset(
    {"claude_final_assistant", "codex_last_token_usage"}
)


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
    quota_override: AutoQuotaOverride | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoResumption":
        return cls(
            resumed_at=_strict_str(data, "resumed_at"),
            from_status=_strict_str(data, "from_status"),
            max_cycles=_strict_int(data, "max_cycles"),
            turn_timeout_seconds=_strict_int(data, "turn_timeout_seconds"),
            quota_override=(
                AutoQuotaOverride.from_dict(data["quota_override"])
                if data.get("quota_override") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "resumed_at": self.resumed_at,
            "from_status": self.from_status,
            "max_cycles": self.max_cycles,
            "turn_timeout_seconds": self.turn_timeout_seconds,
        }
        if self.quota_override is not None:
            result["quota_override"] = self.quota_override.to_dict()
        return result


AUTO_CONTEXT_MODES = frozenset({"off", "clear", "compact"})
# ``context`` measures rendered prompt bytes, never a provider token window;
# every user-facing string says so (7g).
AUTO_CONTEXT_UNITS = frozenset({"cycles", "turns", "context"})
AUTO_COMPACTION_OUTCOMES = frozenset(
    {
        "summarized",
        "cleared",
        "skipped_no_headroom",
        "skipped_overflow",
        "failed",
    }
)
AUTO_SUMMARY_PATH_PATTERN = re.compile(r"summaries/\d{2,4}\.md")


def auto_trigger_key(unit: str, cycle: int, discussion_len: int) -> str:
    """Name one compaction boundary, so the same one is never attempted twice.

    Written and re-derived through this single function: validation recomputes
    the key from the attempt's own fields rather than shape-checking it, because
    a forged key would silently suppress a real future boundary.
    """

    return f"{unit}:{cycle}:{discussion_len}"


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    # ``type(...) is not int`` and not ``isinstance``: ``True`` is an ``int``.
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


@dataclass(frozen=True)
class AutoContextPolicy:
    """How one Auto run retires its own material, chosen at setup.

    Carried unchanged across a resume: editing it would change the meaning of
    content the run has already retired.
    """

    mode: str = "compact"
    unit: str = "cycles"
    interval: int = 3
    threshold_percent: int = 70
    # ``"next"`` or a participant session id. Membership needs the record, so
    # it is checked where the record is validated.
    summarizer: str = "next"

    def __post_init__(self) -> None:
        if self.mode not in AUTO_CONTEXT_MODES:
            raise ValueError(f"unknown Auto context mode: {self.mode}")
        if self.unit not in AUTO_CONTEXT_UNITS:
            raise ValueError(f"unknown Auto context unit: {self.unit}")
        _bounded_int(self.interval, "interval", minimum=1, maximum=20)
        _bounded_int(
            self.threshold_percent,
            "threshold_percent",
            minimum=10,
            maximum=95,
        )
        if type(self.summarizer) is not str or not self.summarizer:
            raise ValueError("summarizer must be a non-empty string")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoContextPolicy":
        return cls(
            mode=_strict_str(data, "mode"),
            unit=_strict_str(data, "unit"),
            interval=_strict_int(data, "interval"),
            threshold_percent=_strict_int(data, "threshold_percent"),
            summarizer=_strict_str(data, "summarizer"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AutoSummary:
    """One durable Auto summary and exactly what it retired."""

    path: str
    sha256: str
    created_at: str
    cycle: int
    round_n: int
    session_id: str
    retired_baseline_count: int
    retired_discussion_count: int
    # Entries an input overflow retired without summarizing. The one place
    # content is deliberately discarded, so it is named rather than counted.
    dropped_entries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.path) is not str
            or AUTO_SUMMARY_PATH_PATTERN.fullmatch(self.path) is None
        ):
            raise ValueError("Auto summary path must be summaries/NN.md")
        if (
            type(self.sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
        ):
            raise ValueError("Auto summary digest must be a sha256 hex string")
        if type(self.created_at) is not str or not self.created_at:
            raise ValueError("Auto summary created_at must be a non-empty string")
        _bounded_int(self.cycle, "cycle", minimum=1, maximum=1_000_000)
        _bounded_int(self.round_n, "round_n", minimum=1, maximum=1_000_000)
        if type(self.session_id) is not str or not self.session_id:
            raise ValueError("Auto summary session_id must be a non-empty string")
        for name in ("retired_baseline_count", "retired_discussion_count"):
            _bounded_int(getattr(self, name), name, minimum=0, maximum=1_000_000)
        entries = tuple(self.dropped_entries)
        if any(type(item) is not str or not item for item in entries):
            raise ValueError("Auto summary dropped entries must be non-empty strings")
        object.__setattr__(self, "dropped_entries", entries)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoSummary":
        return cls(
            path=_strict_str(data, "path"),
            sha256=_strict_str(data, "sha256"),
            created_at=_strict_str(data, "created_at"),
            cycle=_strict_int(data, "cycle"),
            round_n=_strict_int(data, "round_n"),
            session_id=_strict_str(data, "session_id"),
            retired_baseline_count=_strict_int(data, "retired_baseline_count"),
            retired_discussion_count=_strict_int(data, "retired_discussion_count"),
            dropped_entries=tuple(data.get("dropped_entries", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["dropped_entries"] = list(self.dropped_entries)
        return result


@dataclass(frozen=True)
class AutoCompactionAttempt:
    """Every compaction attempt, successful or not.

    A failure produces no ``AutoSummary``, so without this record the warning
    that explains it would have nowhere to live.
    """

    trigger_key: str
    unit: str
    cycle: int
    discussion_len: int
    attempted_at: str
    outcome: str
    round_n: int | None = None
    session_id: str | None = None
    summary_index: int | None = None
    warning: str | None = None

    def __post_init__(self) -> None:
        if self.unit not in AUTO_CONTEXT_UNITS:
            raise ValueError(f"unknown Auto context unit: {self.unit}")
        if self.outcome not in AUTO_COMPACTION_OUTCOMES:
            raise ValueError(f"unknown Auto compaction outcome: {self.outcome}")
        _bounded_int(self.cycle, "cycle", minimum=0, maximum=1_000_000)
        _bounded_int(
            self.discussion_len,
            "discussion_len",
            minimum=0,
            maximum=1_000_000,
        )
        if type(self.attempted_at) is not str or not self.attempted_at:
            raise ValueError("Auto compaction attempted_at must be a non-empty string")
        if type(self.trigger_key) is not str or not self.trigger_key:
            raise ValueError("Auto compaction trigger key must be a non-empty string")
        for name in ("round_n", "summary_index"):
            value = getattr(self, name)
            if value is not None:
                _bounded_int(value, name, minimum=0, maximum=1_000_000)
        if self.session_id is not None and (
            type(self.session_id) is not str or not self.session_id
        ):
            raise ValueError("Auto compaction session_id must be a string or None")
        if self.warning is not None:
            if type(self.warning) is not str:
                raise ValueError("Auto compaction warning must be a string or None")
            object.__setattr__(self, "warning", self.warning[:2_000])

    @property
    def expected_trigger_key(self) -> str:
        return auto_trigger_key(self.unit, self.cycle, self.discussion_len)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoCompactionAttempt":
        return cls(
            trigger_key=_strict_str(data, "trigger_key"),
            unit=_strict_str(data, "unit"),
            cycle=_strict_int(data, "cycle"),
            discussion_len=_strict_int(data, "discussion_len"),
            attempted_at=_strict_str(data, "attempted_at"),
            outcome=_strict_str(data, "outcome"),
            round_n=(
                _strict_int(data, "round_n")
                if data.get("round_n") is not None
                else None
            ),
            session_id=data.get("session_id"),
            summary_index=(
                _strict_int(data, "summary_index")
                if data.get("summary_index") is not None
                else None
            ),
            warning=data.get("warning"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


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


def _checked_tokens(value: Any, name: str) -> int | None:
    """Accept an absent count, reject anything that is not a real token count."""

    if value is None:
        return None
    # ``type(...) is not int`` and not ``isinstance``: ``True`` is an ``int``.
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")
    return value


def _checked_cost(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError("total_cost_usd must be a number or None")
    if not math.isfinite(value) or value < 0:
        raise ValueError("total_cost_usd must be finite and non-negative")
    return float(value)


@dataclass(frozen=True)
class TurnUsage:
    """What one round cost, as the provider billed it.

    Every field is optional and stays ``None`` when unreported: a blanket zero
    default would render an unavailable cost as a real ``0.00``.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_cost_usd: float | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
            "max_output_tokens",
        ):
            object.__setattr__(
                self, name, _checked_tokens(getattr(self, name), name)
            )
        object.__setattr__(
            self, "total_cost_usd", _checked_cost(self.total_cost_usd)
        )

    @property
    def reported(self) -> bool:
        """True when the provider reported at least one figure."""

        return any(
            getattr(self, field.name) is not None for field in fields(self)
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TurnUsage":
        return cls(
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            cache_read_tokens=data.get("cache_read_tokens"),
            cache_creation_tokens=data.get("cache_creation_tokens"),
            reasoning_tokens=data.get("reasoning_tokens"),
            total_cost_usd=data.get("total_cost_usd"),
            max_output_tokens=data.get("max_output_tokens"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


_SUMMABLE_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "total_cost_usd",
)


def total_turn_usage(usages: Iterable["TurnUsage | None"]) -> TurnUsage:
    """Add up what a set of rounds cost.

    A field stays ``None`` unless at least one round reported it, so a total
    never claims a provider reported a zero it never sent. ``max_output_tokens``
    is a per-turn cap rather than a quantity, so it is never summed.
    """

    totals: dict[str, float | None] = dict.fromkeys(_SUMMABLE_USAGE_FIELDS, None)
    for usage in usages:
        if usage is None:
            continue
        for name in _SUMMABLE_USAGE_FIELDS:
            value = getattr(usage, name)
            if value is None:
                continue
            totals[name] = (totals[name] or 0) + value
    return TurnUsage(**totals)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ContextReading:
    """One provider's occupancy report for a turn, before Delibra stamps it.

    Adapters know what the provider said; only the runner knows which round it
    belongs to and when it landed, so the durable ``ContextObservation`` is
    assembled at finalization.
    """

    used_tokens: int | None
    context_window: int | None
    numerator_source: str
    resolved_model: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "used_tokens", _checked_tokens(self.used_tokens, "used_tokens")
        )
        object.__setattr__(
            self,
            "context_window",
            _checked_tokens(self.context_window, "context_window"),
        )
        if self.numerator_source not in NUMERATOR_SOURCES:
            raise ValueError(
                f"unknown context numerator source: {self.numerator_source}"
            )

    def observed(self, *, round_n: int, observed_at: str) -> "ContextObservation":
        return ContextObservation(
            used_tokens=self.used_tokens,
            context_window=self.context_window,
            numerator_source=self.numerator_source,
            resolved_model=self.resolved_model,
            round_n=round_n,
            observed_at=observed_at,
        )


@dataclass(frozen=True)
class ContextObservation:
    """How full one session's provider context window was after a round.

    Never interchangeable with ``TurnUsage``: a provider's billing record may
    aggregate a whole agent loop, so reusing it as the occupancy numerator
    would report the wrong number.
    """

    used_tokens: int | None
    context_window: int | None
    numerator_source: str
    resolved_model: str | None
    round_n: int
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "used_tokens", _checked_tokens(self.used_tokens, "used_tokens")
        )
        object.__setattr__(
            self,
            "context_window",
            _checked_tokens(self.context_window, "context_window"),
        )
        if self.numerator_source not in NUMERATOR_SOURCES:
            raise ValueError(
                f"unknown context numerator source: {self.numerator_source}"
            )
        if type(self.round_n) is not int or self.round_n < 1:
            raise ValueError("round_n must be a positive integer")
        if type(self.observed_at) is not str or not self.observed_at:
            raise ValueError("observed_at must be a non-empty string")
        if self.resolved_model is not None and type(self.resolved_model) is not str:
            raise ValueError("resolved_model must be a string or None")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextObservation":
        return cls(
            used_tokens=data.get("used_tokens"),
            context_window=data.get("context_window"),
            numerator_source=str(data["numerator_source"]),
            resolved_model=data.get("resolved_model"),
            round_n=_strict_int(data, "round_n"),
            observed_at=_strict_str(data, "observed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextSummaryArtifact:
    """One session's durable context summary, owned by that session's directory.

    Not a ``SharedContextDescriptor``: that type's ``staged_file`` denotes a
    round-scoped input copy, while this outlives every round it summarizes and
    is re-staged, digest-checked, into each later turn.
    """

    path: str
    sha256: str
    source_round: int
    created_at: str
    model: str | None = None

    def __post_init__(self) -> None:
        # The summary is read back and handed to a provider as context, so a
        # path that could leave the session directory is refused here -- which
        # is also what refuses a tampered config on load.
        if type(self.path) is not str or not self.path:
            raise ValueError("context summary path is required")
        parts = PurePosixPath(self.path).parts
        if (
            self.path.startswith("/")
            or ".." in parts
            or PurePosixPath(self.path).is_absolute()
        ):
            raise ValueError("context summary path must stay inside the session")
        if type(self.sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("context summary digest must be a sha256 hex string")
        if type(self.source_round) is not int or self.source_round < 1:
            raise ValueError("context summary source round must be positive")
        if type(self.created_at) is not str or not self.created_at:
            raise ValueError("context summary created_at must be a non-empty string")
        if self.model is not None and type(self.model) is not str:
            raise ValueError("context summary model must be a string or None")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextSummaryArtifact":
        return cls(
            path=_strict_str(data, "path"),
            sha256=_strict_str(data, "sha256"),
            source_round=_strict_int(data, "source_round"),
            created_at=_strict_str(data, "created_at"),
            model=data.get("model"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RATE_LIMIT_WINDOWS = frozenset({"five_hour", "seven_day"})
RATE_LIMIT_STATUSES = frozenset({"healthy", "warning", "rejected", "unknown"})
# Every quota source Phase 0 proved, named so a later gate can change one
# provider's source without silently reinterpreting stored data.
RATE_LIMIT_SOURCES = frozenset(
    {"claude_rate_limit_event", "codex_rollout_token_count"}
)


def _checked_percent(value: Any) -> float | None:
    if value is None:
        return None
    # ``type(...) not in`` and not ``isinstance``: ``True`` is an ``int``.
    if type(value) not in (int, float):
        raise ValueError("used_percent must be a number or None")
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("used_percent must be finite and within 0..100")
    return float(value)


def _checked_instant(value: Any, name: str, *, optional: bool) -> Any:
    """Require an aware instant, so two windows are never compared as strings."""

    if value is None:
        if optional:
            return None
        raise ValueError(f"{name} is required")
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _rate_limit_fields(window: str, status: str, source: str) -> None:
    if window not in RATE_LIMIT_WINDOWS:
        raise ValueError(f"unknown rate-limit window: {window}")
    if status not in RATE_LIMIT_STATUSES:
        raise ValueError(f"unknown rate-limit status: {status}")
    if source not in RATE_LIMIT_SOURCES:
        raise ValueError(f"unknown rate-limit source: {source}")


@dataclass(frozen=True)
class RateLimitReading:
    """One provider's quota report, before Delibra stamps whose account it is.

    An adapter knows what the provider said; only the runner knows which
    account it was speaking for and when the report landed.
    """

    window: str
    used_percent: float | None
    status: str
    resets_at: datetime | None
    source: str

    def __post_init__(self) -> None:
        _rate_limit_fields(self.window, self.status, self.source)
        object.__setattr__(
            self, "used_percent", _checked_percent(self.used_percent)
        )
        object.__setattr__(
            self,
            "resets_at",
            _checked_instant(self.resets_at, "resets_at", optional=True),
        )

    def observed(
        self, *, provider: str, account_key: str, observed_at: datetime
    ) -> "RateLimitObservation":
        return RateLimitObservation(
            provider=provider,
            account_key=account_key,
            window=self.window,
            used_percent=self.used_percent,
            status=self.status,
            resets_at=self.resets_at,
            observed_at=observed_at,
            source=self.source,
        )


@dataclass(frozen=True)
class RateLimitObservation:
    """How much account quota one window had left, as of one instant.

    Account-scoped and shared across every session of a provider, which is
    correct for quota and wrong for context: ``ContextObservation`` never goes
    near this record.
    """

    provider: str
    account_key: str
    window: str
    used_percent: float | None
    status: str
    resets_at: datetime | None
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        _rate_limit_fields(self.window, self.status, self.source)
        for name in ("provider", "account_key"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(
            self, "used_percent", _checked_percent(self.used_percent)
        )
        object.__setattr__(
            self,
            "resets_at",
            _checked_instant(self.resets_at, "resets_at", optional=True),
        )
        object.__setattr__(
            self,
            "observed_at",
            _checked_instant(self.observed_at, "observed_at", optional=False),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.account_key, self.window)

    @property
    def remaining_percent(self) -> float | None:
        """Derived, never stored: a second stored figure could disagree."""

        return None if self.used_percent is None else 100.0 - self.used_percent

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RateLimitObservation":
        resets_at = data.get("resets_at")
        return cls(
            provider=_strict_str(data, "provider"),
            account_key=_strict_str(data, "account_key"),
            window=_strict_str(data, "window"),
            used_percent=data.get("used_percent"),
            status=_strict_str(data, "status"),
            resets_at=None if resets_at is None else _parse_instant(resets_at),
            observed_at=_parse_instant(_strict_str(data, "observed_at")),
            source=_strict_str(data, "source"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "account_key": self.account_key,
            "window": self.window,
            "status": self.status,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
        }
        if self.used_percent is not None:
            result["used_percent"] = self.used_percent
        if self.resets_at is not None:
            result["resets_at"] = self.resets_at.isoformat()
        return result


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("stored instants must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class AutoQuotaPause:
    """Why an Auto run stopped, as a record rather than a parsed sentence.

    The Continue disclosure needs the window, the observed figure and the reset
    instant, and the override in 5e must match the exact window this names;
    recovering any of that from ``terminal_reason`` prose would break the moment
    the wording changes.
    """

    observation: RateLimitObservation
    paused_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "paused_at",
            _checked_instant(self.paused_at, "paused_at", optional=False),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoQuotaPause":
        return cls(
            observation=RateLimitObservation.from_dict(data["observation"]),
            paused_at=_parse_instant(_strict_str(data, "paused_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation": self.observation.to_dict(),
            "paused_at": self.paused_at.isoformat(),
        }


@dataclass(frozen=True)
class AutoQuotaOverride:
    """The exact quota window a user chose to keep spending."""

    provider: str
    account_key: str
    window: str
    resets_at: datetime | None
    used_percent_at_grant: float | None
    granted_at: datetime
    granted_from_status: str

    def __post_init__(self) -> None:
        if self.window not in RATE_LIMIT_WINDOWS:
            raise ValueError(f"unknown rate-limit window: {self.window}")
        if self.granted_from_status not in RATE_LIMIT_STATUSES:
            raise ValueError(
                f"unknown rate-limit status: {self.granted_from_status}"
            )
        for name in ("provider", "account_key"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(
            self,
            "used_percent_at_grant",
            _checked_percent(self.used_percent_at_grant),
        )
        object.__setattr__(
            self,
            "resets_at",
            _checked_instant(self.resets_at, "resets_at", optional=True),
        )
        object.__setattr__(
            self,
            "granted_at",
            _checked_instant(self.granted_at, "granted_at", optional=False),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoQuotaOverride":
        resets_at = data.get("resets_at")
        return cls(
            provider=_strict_str(data, "provider"),
            account_key=_strict_str(data, "account_key"),
            window=_strict_str(data, "window"),
            resets_at=None if resets_at is None else _parse_instant(resets_at),
            used_percent_at_grant=data.get("used_percent_at_grant"),
            granted_at=_parse_instant(_strict_str(data, "granted_at")),
            granted_from_status=_strict_str(data, "granted_from_status"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "account_key": self.account_key,
            "window": self.window,
            "granted_at": self.granted_at.isoformat(),
            "granted_from_status": self.granted_from_status,
        }
        if self.resets_at is not None:
            result["resets_at"] = self.resets_at.isoformat()
        if self.used_percent_at_grant is not None:
            result["used_percent_at_grant"] = self.used_percent_at_grant
        return result

    def is_live_for(
        self,
        observation: RateLimitObservation,
        *,
        now: datetime,
        staleness_seconds: int,
    ) -> bool:
        """Whether this grant covers the observation's exact live window."""

        current = _checked_instant(now, "now", optional=False)
        if (self.provider, self.account_key, self.window) != observation.key:
            return False
        if self.resets_at is not None:
            return (
                observation.resets_at == self.resets_at
                and current < self.resets_at
            )
        age_seconds = (current - self.granted_at).total_seconds()
        return (
            observation.resets_at is None
            and 0 <= age_seconds <= staleness_seconds
        )


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
    # Folded failure category; absent on legacy records and on success.
    error_category: str | None = None
    # What the provider billed for this round; absent on legacy records and
    # whenever the provider reported nothing.
    usage: TurnUsage | None = None

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
            error_category=(
                str(data["error_category"])
                if data.get("error_category") is not None
                else None
            ),
            usage=(
                TurnUsage.from_dict(data["usage"])
                if data.get("usage") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source"] = self.source.to_dict()
        if self.error_category is None:
            result.pop("error_category", None)
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
        if self.usage is None:
            result.pop("usage", None)
        else:
            result["usage"] = self.usage.to_dict()
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
    # Latest wins, and invalidated to None wherever Delibra deliberately
    # discards the provider-side context this describes. Absent on legacy
    # records.
    context_observation: ContextObservation | None = None
    # Rounds at or below this number have been retired by Clear or Compact and
    # are never staged again. The upper bound is enforced where the operations
    # run, which is the only place that knows the latest allocated round.
    context_baseline_round: int = 0
    context_summary: ContextSummaryArtifact | None = None

    def __post_init__(self) -> None:
        if type(self.context_baseline_round) is not int or (
            self.context_baseline_round < 0
        ):
            raise ValueError("context_baseline_round must be a non-negative integer")

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
            context_observation=(
                ContextObservation.from_dict(data["context_observation"])
                if data.get("context_observation") is not None
                else None
            ),
            context_baseline_round=_strict_int(
                data, "context_baseline_round", default=0
            ),
            context_summary=(
                ContextSummaryArtifact.from_dict(data["context_summary"])
                if data.get("context_summary") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["rounds"] = [item.to_dict() for item in self.rounds]
        result["context_observation"] = (
            self.context_observation.to_dict()
            if self.context_observation is not None
            else None
        )
        result["context_summary"] = (
            self.context_summary.to_dict()
            if self.context_summary is not None
            else None
        )
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
    # Absent in delibra-auto/1 records, so both must default.
    resumptions: list[AutoResumption] = field(default_factory=list)
    quota_pause: AutoQuotaPause | None = None
    quota_override: AutoQuotaOverride | None = None
    # Absent means off, so a delibra-auto/1 run never starts retiring material.
    context_policy: AutoContextPolicy | None = None
    # Leading entries of each append-only list that have been retired, which is
    # what makes the counts stable identifiers rather than positions.
    retired_baseline_count: int = 0
    retired_discussion_count: int = 0
    summaries: list[AutoSummary] = field(default_factory=list)
    compaction_attempts: list[AutoCompactionAttempt] = field(default_factory=list)
    # 7b's real rate limiter: a failed attempt waits out a whole interval or
    # speaking round rather than retrying after one turn.
    compaction_cooldown_until_discussion_len: int = 0
    consecutive_compaction_failures: int = 0
    compaction_disabled_reason: str | None = None

    @property
    def effective_context_policy(self) -> AutoContextPolicy:
        """The policy in force, with an absent one meaning off."""

        return self.context_policy or AutoContextPolicy(mode="off")

    @property
    def latest_summary(self) -> AutoSummary | None:
        """Only the newest summary is ever rendered; the rest are audit."""

        return self.summaries[-1] if self.summaries else None

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
            quota_pause=(
                AutoQuotaPause.from_dict(data["quota_pause"])
                if data.get("quota_pause") is not None
                else None
            ),
            quota_override=(
                AutoQuotaOverride.from_dict(data["quota_override"])
                if data.get("quota_override") is not None
                else None
            ),
            context_policy=(
                AutoContextPolicy.from_dict(data["context_policy"])
                if data.get("context_policy") is not None
                else None
            ),
            retired_baseline_count=_strict_int(
                data, "retired_baseline_count", default=0
            ),
            retired_discussion_count=_strict_int(
                data, "retired_discussion_count", default=0
            ),
            summaries=[
                AutoSummary.from_dict(item) for item in data.get("summaries", [])
            ],
            compaction_attempts=[
                AutoCompactionAttempt.from_dict(item)
                for item in data.get("compaction_attempts", [])
            ],
            compaction_cooldown_until_discussion_len=_strict_int(
                data, "compaction_cooldown_until_discussion_len", default=0
            ),
            consecutive_compaction_failures=_strict_int(
                data, "consecutive_compaction_failures", default=0
            ),
            compaction_disabled_reason=(
                str(data["compaction_disabled_reason"])
                if data.get("compaction_disabled_reason") is not None
                else None
            ),
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
            "quota_pause": (
                self.quota_pause.to_dict() if self.quota_pause is not None else None
            ),
            "quota_override": (
                self.quota_override.to_dict()
                if self.quota_override is not None
                else None
            ),
            "context_policy": (
                self.context_policy.to_dict()
                if self.context_policy is not None
                else None
            ),
            "retired_baseline_count": self.retired_baseline_count,
            "retired_discussion_count": self.retired_discussion_count,
            "summaries": [item.to_dict() for item in self.summaries],
            "compaction_attempts": [
                item.to_dict() for item in self.compaction_attempts
            ],
            "compaction_cooldown_until_discussion_len": (
                self.compaction_cooldown_until_discussion_len
            ),
            "consecutive_compaction_failures": self.consecutive_compaction_failures,
            "compaction_disabled_reason": self.compaction_disabled_reason,
        }

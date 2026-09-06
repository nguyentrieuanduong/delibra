"""Provider-neutral failure classification shared by adapters and the runner.

Adapters only ever see stdout JSONL, so three whole classes of failure never
reach them: stderr, runner-created failures (spawn, output limits, nonzero exit,
empty result, persistence), and the normalized Codex auth message. Both sides
therefore use the same classifiers, and the runner folds their verdicts.

Depends on nothing but the standard library, so importing it from either an
adapter or the runner cannot create a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


ErrorCategory = Literal[
    "retryable_transport",
    "retryable_server",
    "quota",
    "auth",
    "permanent",
    "unknown",
]

# Strictly ordered. A mixed stream can never be retried once anything reports
# quota, and an explicit permanent verdict still outranks retryable evidence.
# ``unknown`` is absent deliberately: it is dropped before folding rather than
# ranked, so unrecognized noise cannot outrank a proven retryable event.
_PRECEDENCE: tuple[ErrorCategory, ...] = (
    "quota",
    "auth",
    "permanent",
    "retryable_server",
    "retryable_transport",
)

CODEX_AUTH_MARKERS = (
    "codex isolated login is missing",
    "access token could not be refreshed",
    "refresh token was revoked",
    "refresh token expired",
    "token has expired",
    "authentication required",
    "not logged in",
)

_AUTH_MARKERS = (
    *CODEX_AUTH_MARKERS,
    "invalid api key",
    "unauthorized",
    "permission denied",
    "authentication failed",
    "please run /login",
)

_QUOTA_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "quota exceeded",
    "usage limit",
    "out of credit",
    "insufficient credit",
    "insufficient_quota",
)

# Narrow on purpose: each phrase names a broken connection, not a refusal the
# provider would repeat. The reported Claude failure is the first entry.
_TRANSPORT_MARKERS = (
    "connection closed mid-response",
    "connection reset by peer",
    "connection aborted",
    "connection refused",
    "broken pipe",
    "premature close",
    "socket hang up",
    "network error",
    "timed out",
    "timeout",
)

_SERVER_MARKERS = (
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "overloaded",
)

# Only an explicit status, never an "api error: 5" prefix, which would also
# match a 5-something message that is not a status code at all.
_STATUS_PATTERN = re.compile(
    r"(?:\bstatus(?:[ _-]?code)?\b\W{0,3}|\bhttp\b\W{0,3}|\berror\b\W{0,3})(\d{3})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderErrorInfo:
    """Allowlisted normalized scalars only; no provider payload ever enters."""

    category: ErrorCategory
    status_code: int | None = None
    stop_reason: str | None = None
    terminal_reason: str | None = None


def classify_status_code(status: int | None) -> ErrorCategory:
    """Map an HTTP status to a category, without consulting any text."""

    if status is None:
        return "unknown"
    if status == 429:
        return "quota"
    if status in {401, 403}:
        return "auth"
    if 500 <= status <= 599:
        return "retryable_server"
    if 400 <= status <= 499:
        return "permanent"
    return "unknown"


def classify_text(text: str) -> ErrorCategory:
    """Classify free text by a narrow allowlist, for either provider.

    Returns ``unknown`` when nothing matches. Unrecognized text must never be
    promoted to a real category: it would outrank proven retryable evidence in
    the fold and suppress the retry.
    """

    if not text:
        return "unknown"
    lowered = text.casefold()
    # Quota before auth: a 429 body often mentions both, and quota must pause
    # rather than be treated as a credential problem.
    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return "quota"
    match = _STATUS_PATTERN.search(text)
    if match is not None:
        category = classify_status_code(int(match.group(1)))
        if category != "unknown":
            return category
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return "auth"
    if any(marker in lowered for marker in _SERVER_MARKERS):
        return "retryable_server"
    if any(marker in lowered for marker in _TRANSPORT_MARKERS):
        return "retryable_transport"
    return "unknown"


def fold_categories(categories: "list[ErrorCategory] | tuple[ErrorCategory, ...]") -> ErrorCategory:
    """Reduce every piece of evidence to one category, strictest first.

    ``unknown`` takes no part: it is dropped before folding. Only when no
    recognized evidence exists at all does the result become ``permanent``,
    which preserves today's terminal behaviour for genuinely unrecognized
    failures.
    """

    recognized = {item for item in categories if item != "unknown"}
    if not recognized:
        return "permanent"
    for candidate in _PRECEDENCE:
        if candidate in recognized:
            return candidate
    return "permanent"


def is_retryable(category: ErrorCategory) -> bool:
    return category in {"retryable_transport", "retryable_server"}

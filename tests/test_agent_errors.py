from __future__ import annotations

import pytest

from app.agents.errors import (
    ProviderErrorInfo,
    classify_status_code,
    classify_text,
    fold_categories,
    is_retryable,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, "quota"),
        (401, "auth"),
        (403, "auth"),
        (500, "retryable_server"),
        (502, "retryable_server"),
        (599, "retryable_server"),
        (400, "permanent"),
        (404, "permanent"),
        (200, "unknown"),
        (None, "unknown"),
    ],
)
def test_classify_status_code(status: int | None, expected: str) -> None:
    assert classify_status_code(status) == expected


def test_classify_text_recognizes_the_reported_connection_failure() -> None:
    # The exact failure from modifications.md:6.
    text = (
        "API Error: Connection closed mid-response. "
        "The response above may be incomplete."
    )

    assert classify_text(text) == "retryable_transport"
    assert is_retryable(classify_text(text)) is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rate limit exceeded", "quota"),
        ("429 Too Many Requests", "quota"),
        ("You have exceeded your usage limit", "quota"),
        ("Not logged in", "auth"),
        ("Invalid API key", "auth"),
        ("http 503 service unavailable", "retryable_server"),
        ("Internal server error", "retryable_server"),
        ("Connection reset by peer", "retryable_transport"),
        ("socket hang up", "retryable_transport"),
        ("", "unknown"),
        ("The model declined to answer", "unknown"),
        ("Wrote 500 lines to disk", "unknown"),
    ],
)
def test_classify_text_uses_a_narrow_allowlist(text: str, expected: str) -> None:
    assert classify_text(text) == expected


def test_classify_text_is_case_insensitive() -> None:
    assert classify_text("CONNECTION CLOSED MID-RESPONSE") == "retryable_transport"
    assert classify_text("rAtE LiMiT reached") == "quota"


def test_classify_text_never_reads_an_api_error_5_prefix_as_a_status() -> None:
    # "api error: 5" must not be read as a 5xx status match.
    assert classify_text("api error: 5 things went wrong") != "retryable_server"


def test_quota_outranks_auth_in_the_same_message() -> None:
    # A 429 body often mentions credentials too; quota must pause, not retry
    # and not be mistaken for a credential problem.
    assert classify_text("429 rate limit; check your authentication") == "quota"


@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        (["quota", "retryable_transport"], "quota"),
        (["auth", "retryable_server"], "auth"),
        (["permanent", "retryable_transport"], "permanent"),
        (["retryable_server", "retryable_transport"], "retryable_server"),
        (["retryable_transport"], "retryable_transport"),
        (["quota", "auth", "permanent"], "quota"),
    ],
)
def test_fold_applies_strict_precedence(
    categories: list[str],
    expected: str,
) -> None:
    assert fold_categories(categories) == expected


def test_unknown_evidence_never_suppresses_a_proven_retryable_category() -> None:
    # The defect this rule exists to prevent: an incidental stderr line beside a
    # proven transport failure must not outrank it and cancel the retry.
    assert fold_categories(["retryable_transport", "unknown"]) == "retryable_transport"
    assert fold_categories(["unknown", "retryable_server", "unknown"]) == (
        "retryable_server"
    )


def test_an_entirely_unrecognized_fold_stays_permanent() -> None:
    # Preserves today's terminal behaviour for genuinely unrecognized failures.
    assert fold_categories([]) == "permanent"
    assert fold_categories(["unknown", "unknown"]) == "permanent"


def test_provider_error_info_carries_only_normalized_scalars() -> None:
    info = ProviderErrorInfo(
        category="retryable_transport",
        status_code=None,
        stop_reason="error",
        terminal_reason="connection_closed",
    )

    assert info.category == "retryable_transport"
    assert set(vars(info)) == {
        "category",
        "status_code",
        "stop_reason",
        "terminal_reason",
    }

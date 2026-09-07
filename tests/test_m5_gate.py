"""Tests for the Phase 0 usage gate's pure logic.

The gate makes live billable calls, so nothing here runs a provider. What is
tested is the part that decides *whether the evidence is real* -- because the
first live run produced a `capabilities.json` claiming Claude billing and
context-window support that was actually read out of an authentication-failure
payload. A gate that can fabricate proof is worse than no gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spike.m5_usage_gate import (
    TurnResult,
    build_capabilities,
    is_identifier_key,
    merge_capabilities,
    publish,
    sanitize,
    terminal_failure,
)


CLAUDE_AUTH_FAILURE = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "api_error_status": None,
    "modelUsage": {},
    "result": "Not logged in · Please run /login",
    "session_id": "68f85804-1348-444f-acf7-77f1e583a58e",
    "stop_reason": "stop_sequence",
    "terminal_reason": "completed",
    "total_cost_usd": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0},
}

CLAUDE_SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "api_error_status": None,
    "modelUsage": {"claude-sonnet-5-20260101": {"contextWindow": 200000}},
    "result": "ok",
    "session_id": "68f85804-1348-444f-acf7-77f1e583a58e",
    "total_cost_usd": 0.0012,
    "usage": {"input_tokens": 412, "output_tokens": 5},
}

CODEX_FAILURE = {"type": "turn.failed", "error": {"message": "400 invalid_request_error"}}
CODEX_SUCCESS = {"type": "turn.completed", "usage": {"input_tokens": 500, "output_tokens": 4}}


def _turn(label: str, lines: list[dict[str, Any]], failed: str | None = None) -> TurnResult:
    return TurnResult(label=label, lines=list(lines), failed=failed)


class TestTerminalFailure:
    """A turn succeeded only on a provider success terminal, never on exit code."""

    def test_claude_error_result_is_a_failure_despite_subtype_success(self) -> None:
        # The live run's auth failure carried subtype "success" and exit 0. Only
        # is_error discriminates it.
        assert terminal_failure("claude", [CLAUDE_AUTH_FAILURE]) is not None

    def test_claude_success_result_is_not_a_failure(self) -> None:
        assert terminal_failure("claude", [CLAUDE_SUCCESS]) is None

    def test_claude_without_any_result_line_is_a_failure(self) -> None:
        assert terminal_failure("claude", [{"type": "system", "subtype": "init"}]) is not None

    def test_codex_turn_failed_is_a_failure(self) -> None:
        assert terminal_failure("codex", [{"type": "thread.started"}, CODEX_FAILURE]) is not None

    def test_codex_turn_completed_is_not_a_failure(self) -> None:
        assert terminal_failure("codex", [CODEX_SUCCESS]) is None

    def test_codex_without_turn_completed_is_a_failure(self) -> None:
        assert terminal_failure("codex", [{"type": "thread.started"}]) is not None

    def test_failure_message_is_carried_through_for_the_operator(self) -> None:
        message = terminal_failure("codex", [CODEX_FAILURE])
        assert message is not None and "invalid_request_error" in message


class TestCapabilitiesRequireSuccess:
    """No capability may be proven from a failed turn."""

    def test_authentication_failure_proves_nothing(self) -> None:
        turns = [
            _turn(label, [CLAUDE_AUTH_FAILURE], failed="auth")
            for label in ("a_small_fresh", "b_large_resume")
        ]

        report = build_capabilities("claude", turns, ("usage.input_tokens", "modelUsage"))

        assert not any(entry["proven"] for entry in report["capabilities"].values())

    def test_zero_token_counters_do_not_prove_billing(self) -> None:
        # Zero is what an auth error reports. Billing needs a positive count.
        turns = [_turn("a_small_fresh", [CLAUDE_AUTH_FAILURE | {"is_error": False}])]

        report = build_capabilities("claude", turns, ("usage.input_tokens",))

        assert report["capabilities"]["turn_billing"]["proven"] is False

    def test_empty_model_usage_does_not_prove_a_context_window(self) -> None:
        turns = [_turn("a_small_fresh", [CLAUDE_AUTH_FAILURE | {"is_error": False}])]

        report = build_capabilities("claude", turns, ("modelUsage",))

        assert report["capabilities"]["context_window"]["proven"] is False

    def test_a_nested_numeric_context_window_does_prove_it(self) -> None:
        turns = [_turn("a_small_fresh", [CLAUDE_SUCCESS])]

        report = build_capabilities("claude", turns, ("modelUsage",))

        assert report["capabilities"]["context_window"]["proven"] is True

    def test_positive_token_counters_on_a_successful_turn_prove_billing(self) -> None:
        turns = [_turn("a_small_fresh", [CLAUDE_SUCCESS])]

        report = build_capabilities("claude", turns, ("usage.input_tokens",))

        assert report["capabilities"]["turn_billing"]["proven"] is True

    def test_the_report_records_every_failure_for_the_operator(self) -> None:
        turns = [_turn("a_small_fresh", [CLAUDE_AUTH_FAILURE], failed="exit 0: not logged in")]

        report = build_capabilities("claude", turns, ("usage.input_tokens",))

        assert report["failures"] == {"a_small_fresh": "exit 0: not logged in"}


class TestSanitize:
    """Identifiers are redacted; parser-relevant values survive."""

    @pytest.mark.parametrize(
        "key",
        ["session_id", "thread_id", "turn_id", "root_turn_id", "uuid", "id", "cwd"],
    )
    def test_identifier_keys_are_redacted(self, key: str) -> None:
        assert sanitize({key: "68f85804-1348-444f-acf7-77f1e583a58e"}) == {key: "<redacted>"}
        assert is_identifier_key(key) is True

    def test_a_bare_uuid_is_redacted_even_under_an_unknown_key(self) -> None:
        assert sanitize({"whatever": "68f85804-1348-444f-acf7-77f1e583a58e"}) == {
            "whatever": "<redacted>"
        }

    def test_model_names_statuses_and_instants_survive(self) -> None:
        payload = {
            "model": "claude-sonnet-5",
            "status": "allowed_warning",
            "resetsAt": "2026-09-07T05:00:00Z",
            "type": "result",
        }

        assert sanitize(payload) == payload

    def test_numeric_counters_survive_because_they_are_the_evidence(self) -> None:
        assert sanitize({"usage": {"input_tokens": 412, "used_percent": 12.5}}) == {
            "usage": {"input_tokens": 412, "used_percent": 12.5}
        }

    def test_free_text_is_redacted(self) -> None:
        assert sanitize({"text": "x" * 80}) == {"text": "<redacted>"}


class TestPublish:
    """A partial rerun must never erase the other provider's evidence."""

    def test_merge_keeps_a_provider_that_this_run_did_not_probe(self) -> None:
        existing = {"claude": {"provider": "claude", "capabilities": {}}}
        fresh = {"codex": {"provider": "codex", "capabilities": {}}}

        assert set(merge_capabilities(existing, fresh)) == {"claude", "codex"}

    def test_merge_replaces_a_provider_that_this_run_did_probe(self) -> None:
        existing = {"codex": {"provider": "codex", "note": "old"}}
        fresh = {"codex": {"provider": "codex", "note": "new"}}

        assert merge_capabilities(existing, fresh)["codex"]["note"] == "new"

    def test_publish_writes_nothing_when_a_probe_failed(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "codex_a_small_fresh.jsonl").write_text("{}\n", encoding="utf-8")
        target = tmp_path / "m5"

        published = publish(
            staging=staging,
            target=target,
            summary={"codex": {"failures": {"a_small_fresh": "400"}}},
        )

        assert published is False
        assert not target.exists()

    def test_publish_writes_fixtures_and_capabilities_when_every_probe_passed(
        self, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "codex_a_small_fresh.jsonl").write_text("{}\n", encoding="utf-8")
        target = tmp_path / "m5"

        published = publish(
            staging=staging,
            target=target,
            summary={"codex": {"failures": {}, "capabilities": {}}},
        )

        assert published is True
        assert (target / "codex_a_small_fresh.jsonl").exists()
        assert json.loads((target / "capabilities.json").read_text(encoding="utf-8"))["codex"]

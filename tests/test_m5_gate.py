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
    CODEX_CANDIDATES,
    TurnResult,
    build_capabilities,
    is_identifier_key,
    merge_capabilities,
    occupancy_verdict,
    publish,
    sanitize,
    scan,
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


class TestOccupancyDiscriminator:
    """Occupancy grows slightly at C; it is not flat-to-lower.

    A resumed turn resends the whole conversation, so an occupancy field is a
    little *higher* at C than at B -- by the size of one small prompt, not by a
    whole turn. Requiring `c <= b` misreads real occupancy as cumulative, which
    would leave Codex with no context signal and make Phases 6-7 impossible.
    """

    # Measured, Codex gpt-5.6-sol, spike/fixtures/m5/codex_*.jsonl.
    REAL_INPUT_TOKENS = [11501, 32000, 32018, 11501]

    def test_real_measured_occupancy_is_not_called_cumulative(self) -> None:
        verdict = occupancy_verdict({"usage.input_tokens": self.REAL_INPUT_TOKENS})

        assert verdict["usage.input_tokens"].startswith("occupancy")

    def test_a_genuinely_cumulative_field_is_still_called_cumulative(self) -> None:
        # Same turns, but each value is the running total instead.
        totals = [11501, 43501, 75519, 11501]

        verdict = occupancy_verdict({"usage.input_tokens": totals})

        assert verdict["usage.input_tokens"].startswith("cumulative")

    def test_a_field_that_never_returns_to_baseline_is_inconclusive(self) -> None:
        verdict = occupancy_verdict({"x": [11501, 32000, 32018, 31000]})

        assert verdict["x"].startswith("inconclusive")


class TestCodexCandidatePaths:
    """`last_token_usage` is nested under `info`, not at the top level."""

    def test_total_tokens_is_found_under_info(self) -> None:
        # The rollout's token_count payload, as actually recorded.
        line = {
            "type": "token_count",
            "info": {
                "last_token_usage": {"total_tokens": 32023},
                "model_context_window": 258400,
            },
        }

        assert scan([line], "info.last_token_usage.total_tokens") == 32023

    def test_the_candidate_list_uses_the_nested_path(self) -> None:
        # The flat path silently read None on every turn, so the plan's required
        # `last_token_usage` evidence was recorded as absent when it was present.
        assert "info.last_token_usage.total_tokens" in CODEX_CANDIDATES
        assert "last_token_usage.total_tokens" not in CODEX_CANDIDATES


class TestQuotaWindowFromWindowMinutes:
    """The window comes from `window_minutes`, not from primary/secondary."""

    def _readings(self, primary_minutes: int, secondary_minutes: int) -> list[TurnResult]:
        return [
            _turn(
                "a_small_fresh",
                [
                    CODEX_SUCCESS
                    | {
                        "rate_limits": {
                            "primary": {
                                "used_percent": 7.0,
                                "window_minutes": primary_minutes,
                            },
                            "secondary": {
                                "used_percent": 16.0,
                                "window_minutes": secondary_minutes,
                            },
                        }
                    }
                ],
            )
        ]

    def test_measured_windows_map_to_five_hour_and_seven_day(self) -> None:
        # Measured: primary=300 minutes (5h), secondary=10080 (7d).
        report = build_capabilities(
            "codex", self._readings(300, 10080), CODEX_CANDIDATES
        )

        assert report["capabilities"]["five_hour_quota_percent"]["proven"] is True
        assert report["capabilities"]["seven_day_quota_percent"]["proven"] is True

    def test_an_unrecognised_window_length_proves_nothing(self) -> None:
        report = build_capabilities(
            "codex", self._readings(42, 99), CODEX_CANDIDATES
        )

        assert report["capabilities"]["five_hour_quota_percent"]["proven"] is False
        assert report["capabilities"]["seven_day_quota_percent"]["proven"] is False

    def test_swapped_windows_are_attributed_by_length_not_by_name(self) -> None:
        # If the provider ever puts the weekly window first, following the name
        # would report weekly usage as the 5-hour figure and pause far too early.
        report = build_capabilities(
            "codex", self._readings(10080, 300), CODEX_CANDIDATES
        )

        five_hour = report["capabilities"]["five_hour_quota_percent"]
        assert five_hour["proven"] is True
        assert "secondary" in five_hour["evidence"]


class TestQuotaStatusWindowAttribution:
    """A status proves a window only when the payload names that window.

    Claude reports one `rate_limit_info` object. Which window it describes comes
    from `rateLimitType`, and Phase 5's whole policy -- warn, then pause -- turns
    on knowing whether a status covers the 5-hour or the weekly window. Assuming
    5-hour is a guess, and a wrong guess pauses an Auto run for the wrong reason.
    """

    def _turn_with(self, rate_limit_type: str | None) -> list[TurnResult]:
        info: dict[str, Any] = {"status": "allowed"}
        if rate_limit_type is not None:
            info["rateLimitType"] = rate_limit_type
        return [_turn("a_small_fresh", [CLAUDE_SUCCESS | {"rate_limit_info": info}])]

    def test_an_unlabelled_status_proves_neither_window(self) -> None:
        report = build_capabilities(
            "claude", self._turn_with(None), ("rate_limit_info.status",)
        )

        assert report["capabilities"]["five_hour_quota_status"]["proven"] is False
        assert report["capabilities"]["seven_day_quota_status"]["proven"] is False

    def test_an_unrecognised_window_name_is_recorded_not_assumed(self) -> None:
        report = build_capabilities(
            "claude",
            self._turn_with("something_new"),
            ("rate_limit_info.status", "rate_limit_info.rateLimitType"),
        )

        entry = report["capabilities"]["five_hour_quota_status"]
        assert entry["proven"] is False
        assert "something_new" in entry["evidence"]

    def test_a_five_hour_label_proves_only_the_five_hour_window(self) -> None:
        report = build_capabilities(
            "claude",
            self._turn_with("five_hour"),
            ("rate_limit_info.status", "rate_limit_info.rateLimitType"),
        )

        assert report["capabilities"]["five_hour_quota_status"]["proven"] is True
        assert report["capabilities"]["seven_day_quota_status"]["proven"] is False

    def test_a_weekly_label_proves_only_the_weekly_window(self) -> None:
        report = build_capabilities(
            "claude",
            self._turn_with("seven_day"),
            ("rate_limit_info.status", "rate_limit_info.rateLimitType"),
        )

        assert report["capabilities"]["seven_day_quota_status"]["proven"] is True
        assert report["capabilities"]["five_hour_quota_status"]["proven"] is False


class TestOccupancyReadsTurnsByLabel:
    """The A/B/C/D comparison must not depend on turn ordering.

    The model-change turn runs second so a bad model name fails before the
    100 KiB turn is paid for. Indexing the experiment positionally would then
    silently compare the wrong turns.
    """

    def _turns(self) -> list[TurnResult]:
        # An occupancy field rises at B, stays elevated but not higher at C, and
        # resets at D. Read positionally, B would be 999 and C's 30000 would
        # look like a cumulative rise.
        counts = {
            "a_small_fresh": 100,
            "e_model_change": 999,  # interleaved, and must be ignored
            "b_large_resume": 30000,
            "c_small_resume": 29900,
            "d_small_fresh": 100,
        }
        return [
            _turn(label, [CLAUDE_SUCCESS | {"usage": {"input_tokens": count}}])
            for label, count in counts.items()
        ]

    def test_occupancy_is_computed_from_the_labelled_turns(self) -> None:
        report = build_capabilities("claude", self._turns(), ("usage.input_tokens",))

        verdict = report["occupancy_experiment"]["usage.input_tokens"]
        assert verdict.startswith("occupancy"), verdict

    def test_the_interleaved_model_change_turn_does_not_become_turn_b(self) -> None:
        # Positional indexing would read B as 999, so C (30000) would look like
        # a cumulative rise and the field would be misclassified.
        report = build_capabilities("claude", self._turns(), ("usage.input_tokens",))

        assert "cumulative" not in report["occupancy_experiment"]["usage.input_tokens"]


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

    def test_provider_error_messages_survive_in_full(self) -> None:
        # Codex carries the HTTP status only inside this string -- there is no
        # structured status field on stdout. Redacting it for being longer than
        # 64 characters destroys the one thing the error fixture exists for.
        message = (
            '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
            '"message":"The \'x\' model is not supported when using Codex with a '
            'ChatGPT account."}}'
        )

        assert sanitize({"type": "error", "message": message}) == {
            "type": "error",
            "message": message,
        }

    def test_the_agents_reply_is_still_redacted(self) -> None:
        # Only provider diagnostics are exempt; model output is not.
        assert sanitize({"item": {"text": "y" * 200}}) == {"item": {"text": "<redacted>"}}


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

    def _staging(self, tmp_path: Path) -> Path:
        staging = tmp_path / "staging"
        staging.mkdir()
        for name in ("claude_a_small_fresh", "codex_a_small_fresh"):
            (staging / f"{name}.jsonl").write_text("{}\n", encoding="utf-8")
        return staging

    def test_a_failed_provider_publishes_nothing_of_its_own(self, tmp_path: Path) -> None:
        target = tmp_path / "m5"

        published = publish(
            staging=self._staging(tmp_path),
            target=target,
            summary={"codex": {"failures": {"a_small_fresh": "400"}}},
        )

        assert published == []
        assert not (target / "codex_a_small_fresh.jsonl").exists()

    def test_a_passing_provider_publishes_even_when_the_other_failed(
        self, tmp_path: Path
    ) -> None:
        # Each probe is separately billable and the providers are independent.
        # Discarding good Claude evidence because Codex was misconfigured just
        # makes the operator pay for the same turns twice.
        target = tmp_path / "m5"

        published = publish(
            staging=self._staging(tmp_path),
            target=target,
            summary={
                "claude": {"failures": {}, "capabilities": {}},
                "codex": {"failures": {"a_small_fresh": "400"}},
            },
        )

        assert published == ["claude"]
        assert (target / "claude_a_small_fresh.jsonl").exists()
        assert not (target / "codex_a_small_fresh.jsonl").exists()

    def test_a_failed_provider_is_absent_from_capabilities(self, tmp_path: Path) -> None:
        target = tmp_path / "m5"

        publish(
            staging=self._staging(tmp_path),
            target=target,
            summary={
                "claude": {"failures": {}, "capabilities": {}},
                "codex": {"failures": {"a_small_fresh": "400"}},
            },
        )

        recorded = json.loads((target / "capabilities.json").read_text(encoding="utf-8"))
        assert set(recorded) == {"claude"}

    def test_a_failed_provider_does_not_erase_its_earlier_evidence(
        self, tmp_path: Path
    ) -> None:
        # A later misconfigured Codex run must not delete a Codex report that an
        # earlier run legitimately proved.
        target = tmp_path / "m5"
        target.mkdir()
        (target / "capabilities.json").write_text(
            json.dumps({"codex": {"provider": "codex", "note": "earlier good run"}}),
            encoding="utf-8",
        )

        publish(
            staging=self._staging(tmp_path),
            target=target,
            summary={"codex": {"failures": {"a_small_fresh": "400"}}},
        )

        recorded = json.loads((target / "capabilities.json").read_text(encoding="utf-8"))
        assert recorded["codex"]["note"] == "earlier good run"

"""Real-provider usage gate for Phase 0 of the modify-auto plan.

Runs live turns against `claude` and `codex` and records, per provider, which
usage capabilities actually exist and — crucially — what their units are.

This gate is disposable evidence, never a runtime contract. Production code is
written against the sanitized fixtures it emits, and runtime never reads
`capabilities.json`: structural presence does not establish semantics. A
`utilization` of 0.8 is either 0.8 % or 80 %, and no amount of feature detection
at runtime can tell which; guessing produces a wrong warning or a wrong pause.

Outputs, all under `spike/fixtures/m5/`:
  - `<provider>_<label>.jsonl` — structure preserved, values replaced
  - `capabilities.json`        — per field: observed, proven, unit, evidence

Run it as:

    envs/bin/python -m spike.m5_usage_gate

It performs real, billable provider calls, including one ~100 KiB prompt for the
occupancy experiment. Nothing else in the test suite calls it, but
`tests/test_m5_gate.py` covers every pure decision it makes.

**A turn counts only on a provider success terminal** — Claude `result` with
`is_error == false`, Codex `turn.completed`. Exit status and the presence of
JSON on stdout prove nothing: the first live run exited 0 while emitting an
`authentication_failed` payload whose zero token counters were then read as
proof of billing support. Fixtures are staged in a temp directory and published
only when every probed turn succeeded, so a half-run can never masquerade as
evidence, and a per-provider rerun can never erase the other provider.

This is a disposable CLI entry point, not production code: **stdout is the CLI
interface**, so it prints rather than logging, exactly like the other spike gates.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.claude import ClaudeAdapter  # noqa: E402
from app.agents.codex import CodexAdapter  # noqa: E402
from app.agents.base import RunContext  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import SessionConfig  # noqa: E402


FIXTURES = ROOT / "spike" / "fixtures" / "m5"

# Large enough to move any real occupancy figure well clear of noise, small
# enough to stay within a single prompt.
FILLER_BYTES = 100 * 1024
SMALL_PROMPT = "Reply with exactly the word: ok"

# A model name no provider can resolve, used to induce a permanent 4xx on
# demand. 429 and 5xx cannot be induced without an exhausted quota or a real
# outage, so `error_classification` never claims to cover them.
INVALID_MODEL = "delibra-nonexistent-model-for-error-classification"

# The 0a experiment, in the order the discriminator compares them. The probe
# runs the model-change turn between A and B, so this is not the run order.
OCCUPANCY_SEQUENCE = (
    "a_small_fresh",
    "b_large_resume",
    "c_small_resume",
    "d_small_fresh",
)

INDUCED_LABEL = "f_induced_error"

# The order turns are actually run in: the model-change turn goes second so a
# rejected model name fails before the 100 KiB turn is paid for.
RUN_ORDER = (
    "a_small_fresh",
    "e_model_change",
    "b_large_resume",
    "c_small_resume",
    "d_small_fresh",
)

# Candidate fields, recorded for every turn so the reasoning stays auditable and
# re-checkable when a CLI version changes.
CLAUDE_CANDIDATES = (
    "usage.input_tokens",
    "usage.output_tokens",
    "usage.cache_read_input_tokens",
    "usage.cache_creation_input_tokens",
    "total_cost_usd",
    "modelUsage",
    "num_turns",
    "api_error_status",
    "stop_reason",
    "terminal_reason",
    "rate_limit_info.status",
    "rate_limit_info.rateLimitType",
    "rate_limit_info.resetsAt",
    "rate_limit_info.utilization",
    "rate_limit_info.overageStatus",
)
CODEX_CANDIDATES = (
    "usage.input_tokens",
    "usage.cached_input_tokens",
    "usage.output_tokens",
    # Both live under `info` in the rollout's `token_count` payload. The flat
    # paths read None on every turn, so the plan's required `last_token_usage`
    # evidence was recorded as absent while it was sitting in the fixture.
    "info.last_token_usage.total_tokens",
    "info.total_token_usage.total_tokens",
    "info.model_context_window",
    "rate_limits.primary.used_percent",
    "rate_limits.primary.window_minutes",
    "rate_limits.primary.resets_at",
    "rate_limits.secondary.used_percent",
    "rate_limits.secondary.window_minutes",
    "rate_limits.secondary.resets_at",
)

# Fields computed from candidates rather than read from a line.
#
# Claude splits one conversation's context across three counters: a resumed
# turn reads back as `cache_read_input_tokens` what the previous turn wrote as
# `cache_creation_input_tokens`. No single counter therefore traces occupancy.
# Measured over the four-turn experiment, `cache_read` reads "cumulative" and
# `cache_creation` "did not return to baseline", while their sum with
# `input_tokens` traces 8118 -> 42952 -> 42970 -> 8118: a textbook occupancy
# curve, +18 at C against a 34,834-token jump at B, exactly the shape Codex
# showed. Grading the counters individually left Claude with no context signal
# at all, which would have made Phase 6/7 compaction impossible for it.
DERIVED_CANDIDATES: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {
        "usage.total_prompt_tokens (derived)": (
            "usage.input_tokens",
            "usage.cache_read_input_tokens",
            "usage.cache_creation_input_tokens",
        ),
    },
    "codex": {},
}

CAPABILITIES = (
    "turn_billing",
    "context_occupancy",
    "context_window",
    "five_hour_quota_percent",
    "five_hour_quota_status",
    "seven_day_quota_percent",
    "seven_day_quota_status",
    "error_classification",
)


@dataclass
class TurnResult:
    label: str
    lines: list[dict[str, Any]] = field(default_factory=list)
    rollout_lines: list[dict[str, Any]] = field(default_factory=list)
    cli_session_id: str | None = None
    failed: str | None = None


def dig(payload: Any, path: str) -> Any:
    """Read a dotted path, returning None rather than raising on any miss."""

    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def scan(lines: list[dict[str, Any]], path: str) -> Any:
    """Return the last non-None value of `path` across every line."""

    found = None
    for line in lines:
        value = dig(line, path)
        if value is None:
            for container in ("msg", "item", "event", "result"):
                value = dig(line, f"{container}.{path}")
                if value is not None:
                    break
        if value is not None:
            found = value
    return found


IDENTIFIER_KEYS = frozenset(
    {
        "id",
        "session_id",
        "thread_id",
        "turn_id",
        "root_turn_id",
        "parent_tool_use_id",
        "uuid",
        "cwd",
        "workspace_roots",
        "account_id",
        "user_id",
    }
)

# A bare identifier is short enough to survive the length rule, so it needs its
# own detector rather than relying on the key name alone.
UUID_LIKE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


# Provider diagnostics, kept in full however long they are. Codex reports the
# HTTP status only inside its error string -- there is no structured status
# field on stdout -- so redacting these leaves the error fixtures unable to
# exercise the parser they exist for. They are provider text, not model output;
# the only prompt the gate ever sends is a fixed constant.
DIAGNOSTIC_KEYS = frozenset({"message", "codex_error_info", "error_type"})

# Prompt and response text, redacted for what the key *is* rather than for how
# long the value happens to be. The old length rule kept any string under 64
# characters, so a short answer ("ok") survived into a checked-in fixture. The
# only prompt this gate sends is a fixed constant, but the rule has to hold for
# whatever a later probe sends.
CONTENT_KEYS = frozenset(
    {
        "text",
        "content",
        "result",
        "last_agent_message",
        "prompt",
        "instructions",
        "developer_instructions",
    }
)


def is_identifier_key(key: str) -> bool:
    return key in IDENTIFIER_KEYS


def _sanitize_entry(key: str, value: Any, *, depth: int) -> Any:
    if is_identifier_key(key) and not isinstance(value, (dict, list)):
        return "<redacted>"
    if key in DIAGNOSTIC_KEYS and isinstance(value, str):
        return value
    # Only the leaf string goes; `content` is usually a list of typed parts and
    # the parser keys on those types, so recursion must continue through it.
    if key in CONTENT_KEYS and isinstance(value, str):
        return "<redacted>"
    return sanitize(value, depth=depth + 1)


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Preserve structure and types; replace anything that could be content.

    Numbers are kept: they are the whole point of the fixture, and they are
    usage counters, not prompt or response text. Identifiers are dropped even
    though they are short -- a fixture is checked in, and a real session or
    thread id in it is a leak, not a parser input.
    """

    if depth > 12:
        return "<deep>"
    if isinstance(value, dict):
        return {
            key: _sanitize_entry(key, value[key], depth=depth)
            for key in value
        }
    if isinstance(value, list):
        return [sanitize(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if UUID_LIKE.match(value):
            return "<redacted>"
        # Model names, statuses, and ISO instants are the strings the parsers
        # key on, so keep short ones and redact anything long enough to be text.
        return value if len(value) <= 64 and "\n" not in value else "<redacted>"
    return "<redacted>"


def terminal_failure(provider: str, lines: list[dict[str, Any]]) -> str | None:
    """Return why the turn failed, or None if the provider confirmed success.

    Both CLIs exit 0 and emit well-formed JSON while failing, so neither the
    exit status nor the presence of parseable output is evidence. Only a
    provider success terminal is: Claude `result` with `is_error == false`,
    Codex `turn.completed`.
    """

    if provider == "claude":
        results = [line for line in lines if line.get("type") == "result"]
        if not results:
            return "no result event"
        final = results[-1]
        if final.get("is_error") is False:
            return None
        message = final.get("result") or final.get("errors") or final.get("subtype")
        return f"claude result is_error: {message}"

    for line in lines:
        if line.get("type") == "turn.completed":
            return None
    for line in lines:
        if line.get("type") in {"turn.failed", "error"}:
            error = line.get("error")
            message = (
                error.get("message") if isinstance(error, dict) else line.get("message")
            )
            return f"codex {line['type']}: {message}"
    return "no turn.completed event"


async def run_turn(
    *,
    adapter: Any,
    config: SessionConfig,
    workspace: Path,
    prompt: str,
    resume_id: str | None,
    label: str,
    rollout_seen: dict[str, int],
) -> TurnResult:
    context = RunContext(
        user_prompt=prompt,
        resume_id=resume_id,
        resume_strategy="native" if resume_id else "stateless",
        staged_history=[],
        staged_source=None,
        workspace=workspace,
    )
    command = adapter.build_command(config, context)
    result = TurnResult(label=label)
    process = await asyncio.create_subprocess_exec(
        *command.argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workspace),
        env={**os.environ},
    )
    stdout, stderr = await process.communicate(command.stdin.encode("utf-8"))
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            result.lines.append(payload)
            for key in ("session_id", "thread_id"):
                value = payload.get(key) or dig(payload, f"msg.{key}")
                if isinstance(value, str) and value:
                    result.cli_session_id = value

    provider = "claude" if isinstance(adapter, ClaudeAdapter) else "codex"
    # Judge success from stdout alone. A resumed Codex thread shares one rollout
    # file across turns, so folding it in first would let an earlier turn's
    # terminal answer for this one.
    failure = terminal_failure(provider, result.lines)
    if failure is not None:
        tail = stderr.decode("utf-8", errors="replace").strip()[-400:]
        result.failed = f"exit {process.returncode}: {failure}" + (
            f" | stderr: {tail}" if tail else ""
        )

    if provider == "codex" and result.cli_session_id:
        # Codex keeps `token_count` -- which carries `last_token_usage`,
        # `total_token_usage`, `info.model_context_window`, and both rate-limit
        # windows -- in the app-owned rollout, never on `exec --json` stdout.
        # Without this the gate can never see the evidence the plan requires.
        thread = result.cli_session_id
        payloads = read_rollout(thread)[rollout_seen.get(thread, 0) :]
        rollout_seen[thread] = rollout_seen.get(thread, 0) + len(payloads)
        result.rollout_lines = payloads
        result.lines.extend(payloads)
    return result


def load_fixture_turns(
    directory: Path, provider: str
) -> tuple[list[TurnResult], TurnResult | None]:
    """Rebuild turns from checked-in fixtures, making no provider calls.

    The fixtures already hold every number the verdicts are derived from, so a
    corrected discriminator does not need a fresh billable run to re-grade the
    evidence that was already paid for.
    """

    turns: list[TurnResult] = []
    induced: TurnResult | None = None
    for path in sorted(directory.glob(f"{provider}_*.jsonl")):
        label = path.stem[len(provider) + 1 :]
        lines = [
            json.loads(raw)
            for raw in path.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]
        turn = TurnResult(label=label, lines=lines)
        if label == INDUCED_LABEL:
            induced = turn
        else:
            turns.append(turn)
    order = {label: index for index, label in enumerate(RUN_ORDER)}
    turns.sort(key=lambda t: order.get(t.label, len(order)))
    return turns, induced


def read_rollout(thread_id: str) -> list[dict[str, Any]]:
    """Return the `event_msg` payloads of the rollout for `thread_id`.

    The rollout also records the full prompt and response, so only event
    payloads are lifted -- never `response_item` -- and everything still goes
    through `sanitize` before it reaches a fixture.
    """

    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    matches = sorted(home.glob(f"sessions/**/rollout-*-{thread_id}.jsonl"))
    payloads: list[dict[str, Any]] = []
    for path in matches:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


async def probe_provider(
    name: str,
    adapter: Any,
    config: SessionConfig,
    workspace: Path,
    alternate_model: str,
) -> list[TurnResult]:
    """The 0a controlled-size experiment: A small, B huge, C small, D fresh.

    An occupancy field rises sharply at B, stays elevated at C (the large input
    is still in context), and drops back to roughly A at D. A cumulative field
    rises at B, keeps rising at C, and resets only at D. C is the decisive
    comparison: occupancy is flat-to-lower against B while cumulative is
    strictly higher.

    A fifth turn changes the model, because the plan requires a model-change
    fixture: Claude's context-window denominator is keyed by the *resolved*
    model name, and a single-model fixture cannot catch a lookup that silently
    misses.

    The sequence stops at the first failed turn. Continuing past one produces
    exactly the artifact that made the first live run worthless -- a full set of
    fixtures in which every number is an error payload.
    """

    turns: list[TurnResult] = []
    rollout_seen: dict[str, int] = {}
    filler = ("delibra filler line for the occupancy experiment.\n" * 4096)[
        :FILLER_BYTES
    ]

    async def attempt(
        label: str, prompt: str, resume_id: str | None, model: str | None = None
    ) -> bool:
        turn = await run_turn(
            adapter=adapter,
            config=config if model is None else replace(config, model=model),
            workspace=workspace,
            prompt=prompt,
            resume_id=resume_id,
            label=label,
            rollout_seen=rollout_seen,
        )
        turns.append(turn)
        if turn.failed:
            print(f"  {name}: turn {label} failed: {turn.failed}")
            return False
        return True

    if not await attempt("a_small_fresh", SMALL_PROMPT, None):
        return turns
    first = turns[-1].cli_session_id

    # Second, not last: the alternate model name is an operator guess that no
    # CLI can validate offline, and a wrong guess must not be discovered only
    # after the 100 KiB turn has already been paid for.
    if not await attempt("e_model_change", SMALL_PROMPT, None, model=alternate_model):
        print(
            f"  {name}: the alternate model {alternate_model!r} was rejected. "
            f"Pass a valid one with --{name}-alternate-model and rerun; nothing "
            "was published."
        )
        return turns

    if not await attempt("b_large_resume", f"{SMALL_PROMPT}\n\n{filler}", first):
        return turns
    resumed = turns[-1].cli_session_id or first
    if not await attempt("c_small_resume", SMALL_PROMPT, resumed):
        return turns
    await attempt("d_small_fresh", SMALL_PROMPT, None)
    return turns


def occupancy_verdict(readings: dict[str, list[Any]]) -> dict[str, str]:
    """Label each numeric candidate occupancy, cumulative, or inconclusive."""

    verdicts: dict[str, str] = {}
    for path, values in readings.items():
        if len(values) < 4 or not all(isinstance(v, (int, float)) for v in values):
            verdicts[path] = "inconclusive: not numeric on all four turns"
            continue
        a, b, c, d = values[:4]
        jump = b - a
        if jump <= 0:
            verdicts[path] = "inconclusive: did not rise at B"
            continue
        # C is the decisive turn, but the comparison is *how much* it grew, not
        # whether it grew. A resumed turn resends the whole conversation, so an
        # occupancy field is higher at C than at B by one small prompt -- the
        # measured Codex run gained 18 tokens against a 20,499-token jump at B.
        # A cumulative field instead gains roughly another whole turn.
        if c - b > jump * 0.25:
            verdicts[path] = "cumulative: gained about another turn at C"
        elif abs(d - a) > max(abs(a) * 0.5, 1):
            verdicts[path] = "inconclusive: did not return to baseline at D"
        else:
            verdicts[path] = "occupancy: held at C, reset to baseline at D"
    return verdicts


def read_candidates(
    provider: str, lines: list[dict[str, Any]], candidates: tuple[str, ...]
) -> dict[str, Any]:
    """Read every candidate for one turn, then add the derived fields.

    A derived field is `None` unless *every* component is numeric. A partial
    sum is a wrong number that looks like a right one, and it would be graded
    against the occupancy discriminator as if it were a measurement.
    """

    readings: dict[str, Any] = {path: scan(lines, path) for path in candidates}
    for name, parts in DERIVED_CANDIDATES.get(provider, {}).items():
        values = [readings.get(part) for part in parts]
        readings[name] = (
            sum(values)
            if all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in values
            )
            else None
        )
    return readings


def resolved_model(lines: list[dict[str, Any]]) -> str | None:
    """The model Claude actually ran, from its `system`/`init` line.

    The user-typed alias (`sonnet`) never matches a `modelUsage` key, and the
    experiment interleaves a model-change turn, so a run carries two models'
    entries. Reading the resolved name is the only way to key the denominator
    to the model that produced the turn.
    """

    for line in lines:
        if line.get("type") == "system" and line.get("subtype") == "init":
            model = line.get("model")
            if isinstance(model, str) and model:
                return model
    return None


def provenance(
    provider: str, turns: list[TurnResult], captured_at: str | None
) -> dict[str, Any]:
    """Say when this provider's evidence was captured and by what.

    The two providers are probed separately and merged into one report -- the
    measured runs were 29 minutes apart -- so without this, evidence from two
    incompatible CLI versions coexists silently and nothing in the file says
    so. Anything not observable is null rather than guessed: Codex emits no
    version in its stream, and `--recompute` re-grades fixtures it did not
    capture, so it has no capture time to report.
    """

    versions = [
        line["claude_code_version"]
        for turn in turns
        for line in turn.lines
        if isinstance(line.get("claude_code_version"), str)
    ]
    models: list[str] = []
    for turn in turns:
        model = resolved_model(turn.lines)
        if model is None:
            settings = scan(turn.lines, "thread_settings")
            if isinstance(settings, dict) and isinstance(settings.get("model"), str):
                model = settings["model"]
        if model is not None and model not in models:
            models.append(model)
    return {
        "provider": provider,
        "captured_at": captured_at,
        "cli_version": versions[0] if versions else None,
        "resolved_models": models,
    }


def build_capabilities(
    provider: str,
    turns: list[TurnResult],
    candidates: tuple[str, ...],
    induced: TurnResult | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    per_turn = [read_candidates(provider, t.lines, candidates) for t in turns]
    paths = list(candidates) + list(DERIVED_CANDIDATES.get(provider, {}))
    readings = {path: [reading[path] for reading in per_turn] for path in paths}
    # Select the experiment's four turns by label. `turns` also carries the
    # interleaved model-change turn, and positional indexing would compare the
    # wrong sessions -- reading an occupancy field as cumulative.
    by_index = {t.label: i for i, t in enumerate(turns)}
    experiment = [by_index[label] for label in OCCUPANCY_SEQUENCE if label in by_index]
    verdicts = occupancy_verdict(
        {
            path: [readings[path][i] for i in experiment]
            for path in paths
            if "token" in path or "usage" in path
        }
    )
    observed = {path: values for path, values in readings.items()}
    proven_occupancy = [
        path for path, verdict in verdicts.items() if verdict.startswith("occupancy")
    ]

    capabilities = {name: {"proven": False, "unit": None, "evidence": None} for name in CAPABILITIES}
    failures = {t.label: t.failed for t in turns if t.failed}
    if failures:
        # A failed probe is not evidence of absence, and its payload is not
        # evidence of presence. The first live run proved `turn_billing` and
        # `context_window` from an authentication error whose counters were all
        # zero; nothing may be proven unless every turn reached a success
        # terminal.
        return {
            "provider": provider,
            "turns": [t.label for t in turns],
            "failures": failures,
            "observed": observed,
            "occupancy_experiment": verdicts,
            "capabilities": capabilities,
            "provenance": provenance(provider, turns, captured_at),
            "note": "probe failed; every capability is unknown, not absent",
        }

    billing = [p for p in candidates if p.endswith(("input_tokens", "output_tokens"))]
    # Strictly positive: an error payload reports zero for every counter, so
    # zero cannot distinguish "billed nothing" from "never reached the model".
    billed = [
        p
        for p in billing
        if any(isinstance(v, (int, float)) and v > 0 for v in readings.get(p, []))
    ]
    if billed:
        capabilities["turn_billing"] = {
            "proven": True,
            "unit": "tokens",
            "evidence": billed,
        }
    if proven_occupancy:
        capabilities["context_occupancy"] = {
            "proven": True,
            "unit": "tokens",
            "evidence": proven_occupancy,
        }
    # The window is proven only by a positive number. `modelUsage: {}` -- what an
    # authentication error emits -- is structurally present and semantically
    # empty, and treating "not null" as proof is exactly how the first live run
    # certified a capability it had never observed.
    if provider == "codex":
        window_values = readings.get("info.model_context_window", [])
        evidence = "info.model_context_window"
    else:
        # Claude's denominator must be keyed by the RESOLVED model name. A
        # lookup by the user-typed alias (`sonnet`) misses entirely, and taking
        # the first or largest entry can select the model-change turn's
        # auxiliary model -- measured, the run carries claude-sonnet-5 at
        # 1,000,000 alongside claude-haiku-4-5 at 200,000, and every Phase 6
        # threshold computed against the wrong one is wrong by 5x.
        window_values = []
        keyed: set[str] = set()
        for turn, usage in zip(turns, readings.get("modelUsage", [])):
            model = resolved_model(turn.lines)
            entry = usage.get(model) if isinstance(usage, dict) and model else None
            if isinstance(entry, dict):
                window_values.append(entry.get("contextWindow"))
                keyed.add(model)
        # Every turn, not any turn. Keying that works on one turn out of five is
        # partial evidence: it would let the model-change turn's auxiliary
        # entry certify a denominator the primary model never reported.
        if len(window_values) < len(turns):
            window_values = []
        names = ", ".join(sorted(keyed)) if keyed else "<no resolved model matched>"
        evidence = f"modelUsage[{names}].contextWindow"
    if window_values and all(isinstance(v, (int, float)) and v > 0 for v in window_values):
        capabilities["context_window"] = {
            "proven": True,
            "unit": "tokens",
            "evidence": evidence,
        }

    # Quota. Codex reports used_percent directly. Claude nests everything under
    # rate_limit_info, where `utilization` was absent in a healthy live run and
    # its unit is unproven -- so it is NOT marked proven here just for being
    # structurally present.
    # The window is named by `window_minutes`, not by the slot it sits in.
    # Measured: primary=300 (5h), secondary=10080 (7d). Following the slot name
    # instead would, if the provider ever reorders them, report weekly usage as
    # the 5-hour figure and pause an Auto run far too early.
    for prefix in ("rate_limits.primary", "rate_limits.secondary"):
        percent = readings.get(f"{prefix}.used_percent")
        # In range, not merely numeric. 0.16, 16 and 1600 are the same
        # measurement under three different units, and the plan forbids
        # guessing between them -- a value outside 0..100 says the unit is not
        # the one the evidence string would claim.
        if not percent or not any(
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and 0 <= v <= 100
            for v in percent
        ):
            continue
        # Phase 5 renders a reset time beside the number. Without one the badge
        # would have to invent it, so the percentage alone is not the capability.
        resets = [
            v
            for v in readings.get(f"{prefix}.resets_at", [])
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        ]
        if not resets:
            continue
        minutes = [v for v in readings.get(f"{prefix}.window_minutes", []) if v]
        window = quota_window_from_minutes(minutes[-1] if minutes else None)
        if window is None:
            continue
        capabilities[f"{window}_quota_percent"] = {
            "proven": True,
            # Corroborated against the same account's `codex /status`, which
            # reported "84% left" for the weekly window while this field read
            # 16.0 -- so it counts percent *used*, not remaining.
            "unit": "percent_used_0_to_100",
            "evidence": (
                f"{prefix}.used_percent with resets_at, window_minutes="
                f"{minutes[-1]}"
            ),
        }
    status = readings.get("rate_limit_info.status")
    if status and any(v is not None for v in status):
        # Claude reports a single `rate_limit_info`. Only `rateLimitType` says
        # which window it covers, and Phase 5 warns and pauses per window -- so
        # attributing an unlabelled status to the 5-hour window is a guess that
        # would pause an Auto run for the wrong reason.
        kinds = [v for v in readings.get("rate_limit_info.rateLimitType", []) if v]
        window = quota_window(kinds[-1] if kinds else None)
        if window is None:
            reason = (
                f"rate_limit_info.status observed, but rateLimitType={kinds[-1]!r} "
                "does not name a known window"
                if kinds
                else "rate_limit_info.status observed with no rateLimitType, so "
                "the window it covers is unknown"
            )
            for name in ("five_hour_quota_status", "seven_day_quota_status"):
                capabilities[name] = {
                    "proven": False,
                    "unit": None,
                    "evidence": reason,
                }
        else:
            capabilities[f"{window}_quota_status"] = {
                "proven": True,
                "unit": "status_string",
                "evidence": f"rate_limit_info.status with rateLimitType={kinds[-1]!r}",
            }
    utilization = readings.get("rate_limit_info.utilization")
    if utilization and any(v is not None for v in utilization):
        capabilities["five_hour_quota_percent"] = {
            "proven": False,
            "unit": "UNPROVEN: 0-1 or 0-100 cannot be told apart from one sample",
            "evidence": f"observed rate_limit_info.utilization={utilization}",
        }

    if induced is not None:
        capabilities["error_classification"] = classify_induced(provider, induced)

    return {
        "provider": provider,
        "turns": [t.label for t in turns],
        "failures": {},
        "observed": observed,
        "occupancy_experiment": verdicts,
        "capabilities": capabilities,
        "provenance": provenance(provider, turns, captured_at),
    }


def quota_window_from_minutes(window_minutes: Any) -> str | None:
    """Map a window length onto ours, tolerating small provider drift."""

    if not isinstance(window_minutes, (int, float)) or isinstance(window_minutes, bool):
        return None
    if abs(window_minutes - 300) <= 30:  # 5 hours
        return "five_hour"
    if abs(window_minutes - 10080) <= 720:  # 7 days
        return "seven_day"
    return None


def quota_window(rate_limit_type: Any) -> str | None:
    """Map a provider window label onto ours, or None if it is unrecognised.

    Unrecognised is the safe answer: a new label must stay `unknown` until a
    later run observes it, rather than defaulting to a window and warning about
    quota the account has not actually spent.
    """

    if not isinstance(rate_limit_type, str):
        return None
    lowered = rate_limit_type.casefold()
    if any(mark in lowered for mark in ("five_hour", "5h", "five-hour", "hourly")):
        return "five_hour"
    if any(mark in lowered for mark in ("seven_day", "7d", "week")):
        return "seven_day"
    return None


def classify_induced(provider: str, induced: TurnResult) -> dict[str, Any]:
    """Grade the deliberately induced error against the structured fields.

    The plan's criterion is that the structured fields distinguish 429, 5xx,
    and a transport failure. Only a *permanent* 4xx can be induced safely: a
    429 needs a genuinely exhausted quota and a 5xx needs a provider outage.
    So this gate cannot prove the capability, and marking it proven from one
    404 is the same defect this file has already been corrected for four times
    -- asserting a semantic the probe never established. It stays unproven, and
    the observed field is recorded so a later run that does catch a 429 can
    build on it rather than rediscover it.

    Consequence, deliberately accepted: error classification is `unknown` for
    both providers, so Phase 4's existing text classifier remains the mechanism
    and Phases 5-7 may not gate a quota pause on a structured status.
    """

    status = scan(induced.lines, "api_error_status") or scan(induced.lines, "status")
    if status is None:
        for line in induced.lines:
            error = line.get("error")
            if isinstance(error, dict) and isinstance(error.get("status"), int):
                status = error["status"]
                break
    if not isinstance(status, int):
        return {
            "proven": False,
            "unit": None,
            "evidence": (
                f"{provider}: induced error carried no structured status code; "
                "classification would have to parse free text"
            ),
        }
    return {
        "proven": False,
        "unit": None,
        "evidence": (
            f"{provider}: induced invalid-model request reported status {status} "
            "in the structured field `api_error_status`. That is one permanent "
            "4xx; the criterion is 429, 5xx and a transport failure, and those "
            "three remain UNTESTED -- none can be induced without an exhausted "
            "quota or a real outage. Partial evidence, so unproven."
        ),
    }


def merge_capabilities(
    existing: dict[str, Any], fresh: dict[str, Any]
) -> dict[str, Any]:
    """Overlay this run's providers onto the previous report.

    `--provider codex` must not delete the Claude evidence: the two probes are
    independent, they are expensive, and they will routinely be run apart
    because only one provider's authentication is broken at a time.
    """

    return {**existing, **fresh}


def publish(*, staging: Path, target: Path, summary: dict[str, Any]) -> list[str]:
    """Publish each provider that reached a success terminal on every turn.

    Per provider, not all-or-nothing. The two probes are independent and each
    is separately billable, so discarding a clean Claude run because Codex was
    pointed at a model the account cannot use only makes the operator pay for
    the same turns twice. A failed provider publishes nothing -- neither its
    fixtures nor its capability report -- and crucially does not overwrite
    whatever an earlier successful run proved about it.

    Returns the providers actually published; the caller still exits nonzero
    unless every probed provider is in that list.
    """

    published = [
        provider
        for provider, report in sorted(summary.items())
        if not report.get("failures")
    ]
    if not published:
        return []

    target.mkdir(parents=True, exist_ok=True)
    capabilities = target / "capabilities.json"
    existing: dict[str, Any] = {}
    if capabilities.exists():
        try:
            existing = json.loads(capabilities.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    for path in sorted(staging.iterdir()):
        if any(path.name.startswith(f"{provider}_") for provider in published):
            shutil.copy2(path, target / path.name)
    capabilities.write_text(
        json.dumps(
            merge_capabilities(
                existing, {name: summary[name] for name in published}
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return published


def write_fixtures(staging: Path, provider: str, turns: list[TurnResult]) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    for turn in turns:
        path = staging / f"{provider}_{turn.label}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for line in turn.lines:
                handle.write(json.dumps(sanitize(line), sort_keys=True) + "\n")
        print(f"  staged {path.name} ({len(turn.lines)} lines)")


def recompute(selected: list[str]) -> int:
    """Re-grade the checked-in fixtures. Makes no provider calls."""

    summary: dict[str, Any] = {}
    for provider in selected:
        turns, induced = load_fixture_turns(FIXTURES, provider)
        if not turns:
            print(f"{provider}: no fixtures under {FIXTURES.relative_to(ROOT)}")
            continue
        candidates = CLAUDE_CANDIDATES if provider == "claude" else CODEX_CANDIDATES
        summary[provider] = build_capabilities(
            provider, turns, candidates, induced=induced
        )
    if not summary:
        return 1

    target = FIXTURES / "capabilities.json"
    existing: dict[str, Any] = {}
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
    target.write_text(
        json.dumps(merge_capabilities(existing, summary), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    for provider, report in summary.items():
        print(f"  {provider}:")
        for name, entry in sorted(report["capabilities"].items()):
            mark = "PROVEN" if entry["proven"] else "unproven"
            print(f"    {name:26s} {mark:9s} unit={entry['unit']}")
    print(f"\nrecomputed {target.relative_to(ROOT)} from the checked-in fixtures")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=["claude", "codex", "both"],
        default="both",
    )
    parser.add_argument("--claude-model", default="sonnet")
    parser.add_argument("--claude-alternate-model", default="haiku")
    # A ChatGPT login rejects most model names with HTTP 400 ("not supported
    # when using Codex with a ChatGPT account"). Both `gpt-5-codex` (run 1) and
    # `gpt-5.4` (run 2, copied from spike/spike_codex.py:566, which is stale)
    # failed that way. These two are the models this account has actually used,
    # per ~/.codex/config.toml and its own session records -- not a guess.
    parser.add_argument("--codex-model", default="gpt-5.6-sol")
    parser.add_argument("--codex-alternate-model", default="gpt-5.5")
    parser.add_argument(
        "--recompute",
        action="store_true",
        help=(
            "re-grade the checked-in fixtures and rewrite capabilities.json. "
            "Makes no provider calls: use it when a verdict rule is corrected, "
            "so evidence already paid for is not bought twice."
        ),
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    os.environ.setdefault("CODEX_HOME", str(settings.codex_home))

    selected = ["claude", "codex"] if args.provider == "both" else [args.provider]

    if args.recompute:
        return recompute(selected)

    workspace = Path(tempfile.mkdtemp(prefix="delibra-m5-gate-"))
    staging = Path(tempfile.mkdtemp(prefix="delibra-m5-staging-"))
    published: list[str] = []
    summary: dict[str, Any] = {}
    try:
        for provider in selected:
            print(f"{provider}: running the four-turn occupancy experiment...")
            adapter = ClaudeAdapter() if provider == "claude" else CodexAdapter()
            config = SessionConfig(
                id="0" * 32,
                name="m5 gate",
                agent=provider,
                model=args.claude_model if provider == "claude" else args.codex_model,
                effort="low",
                role_instructions="Answer with one word.",
                cli_session_id=None,
                status="idle",
                created_at="2026-09-07T00:00:00Z",
                rounds=[],
            )
            alternate = (
                args.claude_alternate_model
                if provider == "claude"
                else args.codex_alternate_model
            )
            turns = await probe_provider(
                provider, adapter, config, workspace, alternate
            )
            induced = None
            if not any(t.failed for t in turns):
                # Only worth doing once the provider is known to work: against a
                # broken login every turn already errors, and a second error
                # proves nothing about classification.
                print(f"{provider}: inducing a permanent error for classification...")
                induced = await run_turn(
                    adapter=adapter,
                    config=replace(config, model=INVALID_MODEL),
                    workspace=workspace,
                    prompt=SMALL_PROMPT,
                    resume_id=None,
                    label="f_induced_error",
                    rollout_seen={},
                )
                # This turn is *expected* to fail; it must not block publication.
                induced.failed = None
                turns_for_fixtures = [*turns, induced]
            else:
                turns_for_fixtures = turns
            write_fixtures(staging, provider, turns_for_fixtures)
            candidates = (
                CLAUDE_CANDIDATES if provider == "claude" else CODEX_CANDIDATES
            )
            summary[provider] = build_capabilities(
                provider,
                turns,
                candidates,
                induced=induced,
                captured_at=datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            )

        published = publish(staging=staging, target=FIXTURES, summary=summary)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)

    print("\nCapability summary (proven means unit verified, not merely present):")
    for provider, report in summary.items():
        print(f"  {provider}:")
        for name, entry in sorted(report["capabilities"].items()):
            mark = "PROVEN" if entry["proven"] else "unproven"
            print(f"    {name:26s} {mark:9s} unit={entry['unit']}")
        if report["failures"]:
            print(f"    failures: {report['failures']}")

    if published:
        print(
            f"\nwrote {(FIXTURES / 'capabilities.json').relative_to(ROOT)} "
            f"for: {', '.join(published)}"
        )
    failed = sorted(set(summary) - set(published))
    if failed:
        print(
            f"\nGATE FAILED for: {', '.join(failed)}. Nothing was published for"
            "\nthem: a probe did not reach a provider success terminal, so its"
            "\npayloads are error artifacts, not evidence. Phases 5-7 stay blocked"
            "\nuntil every provider is present. Rerun just the failed provider"
            f"\nwith --provider {failed[0]}; the published evidence is kept."
        )
        return 1

    print(
        "\nAny capability not marked PROVEN is implemented as `unknown` end to end,"
        "\nand `unknown` never warns and never pauses."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

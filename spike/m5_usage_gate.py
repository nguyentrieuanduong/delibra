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
    "last_token_usage.total_tokens",
    "total_token_usage.total_tokens",
    "info.model_context_window",
    "rate_limits.primary.used_percent",
    "rate_limits.primary.window_minutes",
    "rate_limits.primary.resets_at",
    "rate_limits.secondary.used_percent",
    "rate_limits.secondary.window_minutes",
    "rate_limits.secondary.resets_at",
)

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


def is_identifier_key(key: str) -> bool:
    return key in IDENTIFIER_KEYS


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
            key: "<redacted>"
            if is_identifier_key(key) and not isinstance(value[key], (dict, list))
            else sanitize(value[key], depth=depth + 1)
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
        if b <= a:
            verdicts[path] = "inconclusive: did not rise at B"
        elif c > b:
            verdicts[path] = "cumulative: kept rising at C"
        elif d > b * 0.5:
            verdicts[path] = "inconclusive: did not drop on a fresh session at D"
        else:
            verdicts[path] = "occupancy: elevated at C, reset at D"
    return verdicts


def build_capabilities(
    provider: str,
    turns: list[TurnResult],
    candidates: tuple[str, ...],
    induced: TurnResult | None = None,
) -> dict[str, Any]:
    readings = {path: [scan(t.lines, path) for t in turns] for path in candidates}
    # Select the experiment's four turns by label. `turns` also carries the
    # interleaved model-change turn, and positional indexing would compare the
    # wrong sessions -- reading an occupancy field as cumulative.
    by_label = {t.label: t for t in turns}
    experiment = [by_label[label] for label in OCCUPANCY_SEQUENCE if label in by_label]
    verdicts = occupancy_verdict(
        {
            path: [scan(t.lines, path) for t in experiment]
            for path in candidates
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
        window_values = [
            entry.get("contextWindow")
            for usage in readings.get("modelUsage", [])
            if isinstance(usage, dict)
            for entry in usage.values()
            if isinstance(entry, dict)
        ]
        # Claude's denominator must be keyed by the RESOLVED model name; a
        # lookup by the user-typed alias misses, and picking the first or
        # largest entry can select the auxiliary model.
        evidence = "modelUsage[<resolved model>].contextWindow"
    if any(isinstance(v, (int, float)) and v > 0 for v in window_values):
        capabilities["context_window"] = {
            "proven": True,
            "unit": "tokens",
            "evidence": evidence,
        }

    # Quota. Codex reports used_percent directly. Claude nests everything under
    # rate_limit_info, where `utilization` was absent in a healthy live run and
    # its unit is unproven -- so it is NOT marked proven here just for being
    # structurally present.
    for window_name, prefix in (
        ("five_hour", "rate_limits.primary"),
        ("seven_day", "rate_limits.secondary"),
    ):
        percent = readings.get(f"{prefix}.used_percent")
        if percent and any(isinstance(v, (int, float)) for v in percent):
            capabilities[f"{window_name}_quota_percent"] = {
                "proven": True,
                "unit": "percent_used_0_to_100",
                "evidence": f"{prefix}.used_percent with resets_at",
            }
    status = readings.get("rate_limit_info.status")
    if status and any(v is not None for v in status):
        capabilities["five_hour_quota_status"] = {
            "proven": True,
            "unit": "status_string",
            "evidence": "rate_limit_info.status",
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
    }


def classify_induced(provider: str, induced: TurnResult) -> dict[str, Any]:
    """Grade the deliberately induced error against the structured fields.

    Only a *permanent* 4xx can be induced safely; a 429 needs a genuinely
    exhausted quota and a 5xx needs a provider outage, so neither is
    reachable here. The capability is therefore proven only for what was
    actually observed, and the evidence says which categories remain untested
    -- Phase 4 must not assume the 429 path is covered by this run.
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
        "proven": True,
        "unit": "http_status_code",
        "evidence": (
            f"{provider}: induced invalid-model request reported status {status} "
            "in a structured field. 429 and 5xx remain UNTESTED -- neither can "
            "be induced without an exhausted quota or a real outage."
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
    args = parser.parse_args()

    settings = Settings.from_env()
    os.environ.setdefault("CODEX_HOME", str(settings.codex_home))

    workspace = Path(tempfile.mkdtemp(prefix="delibra-m5-gate-"))
    staging = Path(tempfile.mkdtemp(prefix="delibra-m5-staging-"))
    published = False
    summary: dict[str, Any] = {}
    try:
        selected = (
            ["claude", "codex"] if args.provider == "both" else [args.provider]
        )
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
                provider, turns, candidates, induced=induced
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

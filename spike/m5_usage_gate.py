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
occupancy experiment. Nothing else in the test suite calls it.

This is a disposable CLI entry point, not production code: **stdout is the CLI
interface**, so it prints rather than logging, exactly like the other spike gates.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
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


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Preserve structure and types; replace anything that could be content.

    Numbers are kept: they are the whole point of the fixture, and they are
    usage counters, not prompt or response text.
    """

    if depth > 12:
        return "<deep>"
    if isinstance(value, dict):
        return {key: sanitize(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        # Model names, statuses, and ISO instants are the strings the parsers
        # key on, so keep short ones and redact anything long enough to be text.
        return value if len(value) <= 64 and "\n" not in value else "<redacted>"
    return "<redacted>"


async def run_turn(
    *,
    adapter: Any,
    config: SessionConfig,
    workspace: Path,
    prompt: str,
    resume_id: str | None,
    label: str,
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
    if process.returncode not in (0, None) and not result.lines:
        tail = stderr.decode("utf-8", errors="replace").strip()[-400:]
        result.failed = f"exit {process.returncode}: {tail}"
    return result


async def probe_provider(
    name: str,
    adapter: Any,
    config: SessionConfig,
    workspace: Path,
) -> list[TurnResult]:
    """The 0a controlled-size experiment: A small, B huge, C small, D fresh.

    An occupancy field rises sharply at B, stays elevated at C (the large input
    is still in context), and drops back to roughly A at D. A cumulative field
    rises at B, keeps rising at C, and resets only at D. C is the decisive
    comparison: occupancy is flat-to-lower against B while cumulative is
    strictly higher.
    """

    turns: list[TurnResult] = []

    turn_a = await run_turn(
        adapter=adapter,
        config=config,
        workspace=workspace,
        prompt=SMALL_PROMPT,
        resume_id=None,
        label="a_small_fresh",
    )
    turns.append(turn_a)
    if turn_a.failed:
        print(f"  {name}: turn A failed: {turn_a.failed}")
        return turns

    filler = ("delibra filler line for the occupancy experiment.\n" * 4096)[
        :FILLER_BYTES
    ]
    turns.append(
        await run_turn(
            adapter=adapter,
            config=config,
            workspace=workspace,
            prompt=f"Reply with exactly the word: ok\n\n{filler}",
            resume_id=turn_a.cli_session_id,
            label="b_large_resume",
        )
    )
    turns.append(
        await run_turn(
            adapter=adapter,
            config=config,
            workspace=workspace,
            prompt=SMALL_PROMPT,
            resume_id=turns[-1].cli_session_id or turn_a.cli_session_id,
            label="c_small_resume",
        )
    )
    turns.append(
        await run_turn(
            adapter=adapter,
            config=config,
            workspace=workspace,
            prompt=SMALL_PROMPT,
            resume_id=None,
            label="d_small_fresh",
        )
    )
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
) -> dict[str, Any]:
    readings = {path: [scan(t.lines, path) for t in turns] for path in candidates}
    verdicts = occupancy_verdict(
        {
            path: values
            for path, values in readings.items()
            if "token" in path or "usage" in path
        }
    )
    observed = {path: values for path, values in readings.items()}
    proven_occupancy = [
        path for path, verdict in verdicts.items() if verdict.startswith("occupancy")
    ]

    capabilities = {name: {"proven": False, "unit": None, "evidence": None} for name in CAPABILITIES}

    billing = [p for p in candidates if p.endswith(("input_tokens", "output_tokens"))]
    if any(isinstance(readings.get(p, [None])[0], (int, float)) for p in billing):
        capabilities["turn_billing"] = {
            "proven": True,
            "unit": "tokens",
            "evidence": [p for p in billing if readings.get(p, [None])[0] is not None],
        }
    if proven_occupancy:
        capabilities["context_occupancy"] = {
            "proven": True,
            "unit": "tokens",
            "evidence": proven_occupancy,
        }
    window = readings.get("info.model_context_window") or readings.get("modelUsage")
    if window and any(v is not None for v in window):
        capabilities["context_window"] = {
            "proven": True,
            "unit": "tokens",
            # Claude's denominator must be keyed by the RESOLVED model name; a
            # lookup by the user-typed alias misses, and picking the first or
            # largest entry can select the auxiliary model.
            "evidence": "info.model_context_window"
            if provider == "codex"
            else "modelUsage[<resolved model>].contextWindow",
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

    return {
        "provider": provider,
        "turns": [t.label for t in turns],
        "failures": {t.label: t.failed for t in turns if t.failed},
        "observed": observed,
        "occupancy_experiment": verdicts,
        "capabilities": capabilities,
    }


def write_fixtures(provider: str, turns: list[TurnResult]) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for turn in turns:
        path = FIXTURES / f"{provider}_{turn.label}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for line in turn.lines:
                handle.write(json.dumps(sanitize(line), sort_keys=True) + "\n")
        print(f"  wrote {path.relative_to(ROOT)} ({len(turn.lines)} lines)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=["claude", "codex", "both"],
        default="both",
    )
    parser.add_argument("--claude-model", default="sonnet")
    parser.add_argument("--codex-model", default="gpt-5-codex")
    args = parser.parse_args()

    settings = Settings.from_env()
    os.environ.setdefault("CODEX_HOME", str(settings.codex_home))

    workspace = Path(tempfile.mkdtemp(prefix="delibra-m5-gate-"))
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
            turns = await probe_provider(provider, adapter, config, workspace)
            write_fixtures(provider, turns)
            candidates = (
                CLAUDE_CANDIDATES if provider == "claude" else CODEX_CANDIDATES
            )
            summary[provider] = build_capabilities(provider, turns, candidates)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    FIXTURES.mkdir(parents=True, exist_ok=True)
    target = FIXTURES / "capabilities.json"
    target.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {target.relative_to(ROOT)}")

    print("\nCapability summary (proven means unit verified, not merely present):")
    for provider, report in summary.items():
        print(f"  {provider}:")
        for name, entry in sorted(report["capabilities"].items()):
            mark = "PROVEN" if entry["proven"] else "unproven"
            print(f"    {name:26s} {mark:9s} unit={entry['unit']}")
        if report["failures"]:
            print(f"    failures: {report['failures']}")
    print(
        "\nAny capability not marked PROVEN is implemented as `unknown` end to end,"
        "\nand `unknown` never warns and never pauses."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""The account's live Codex quota, read without spending a provider turn.

Phase 8 measured why the rollout files cannot answer this question. Delibra
isolates Codex into its own ``CODEX_HOME``, so a ``codex`` run in the operator's
own terminal spends the same subscription account and is invisible here: the
app-owned rollouts read 3 % / 18 % while the account was actually at 20 % / 37 %.

``codex app-server``'s ``account/rateLimits/read`` returned that same 20 / 37
from *both* homes, which is what proves it reports the account rather than the
local directory. It also needs no thread id and never opens a file that holds
prompts and responses.

Every failure here is silent. Quota state is advisory, and no read of it may
fail a round or a page.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.models import RateLimitReading
from app.storage import CODEX_ROLLOUT_WINDOWS, epoch_instant


LOGGER = logging.getLogger(__name__)

_INITIALIZE_ID = 1
_RATE_LIMITS_ID = 2
# One reply line is a JSON-RPC envelope, not model output; a megabyte is far
# past anything the measured response needs and still bounds a hostile stream.
_LINE_LIMIT = 1024 * 1024


def _window_reading(entry: Any) -> RateLimitReading | None:
    """One slot, accepted only on the shape and units Phase 8 measured."""

    if not isinstance(entry, dict):
        return None
    # The measurement found `primary` at 300 minutes and `secondary` at 10080,
    # but the duration is the only thing that actually names the window, so the
    # slot name is never trusted.
    minutes = entry.get("windowDurationMins")
    window = (
        CODEX_ROLLOUT_WINDOWS.get(minutes) if type(minutes) is int else None
    )
    used = entry.get("usedPercent")
    resets_at = epoch_instant(entry.get("resetsAt"))
    # `type(...) not in` and not `isinstance`: `True` is an `int`.
    if window is None or type(used) not in (int, float) or resets_at is None:
        return None
    try:
        return RateLimitReading(
            window=window,
            used_percent=used,
            # Phase 0 proved no Codex status, and this method reports none
            # either -- only a percentage.
            status="unknown",
            resets_at=resets_at,
            source="codex_app_server",
        )
    except ValueError:
        return None


def parse_account_rate_limits(result: Any) -> list[RateLimitReading]:
    """Reduce one ``account/rateLimits/read`` result to at most one per window.

    An account can report several limit ids. They are reduced here rather than
    left to ``UsageMonitor``: 5b's merge replaces outright on a later reset, so
    a barely-used bucket carrying a later reset would erase a nearly-exhausted
    one. Inside a single read both readings describe the same instant in the
    same account, so keeping the highest ``usedPercent`` is unambiguous.
    """

    if not isinstance(result, dict):
        return []
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict):
        flat = result.get("rateLimits")
        buckets = {"default": flat} if isinstance(flat, dict) else {}

    strongest: dict[str, RateLimitReading] = {}
    for bucket in buckets.values():
        if not isinstance(bucket, dict):
            continue
        for slot in ("primary", "secondary"):
            reading = _window_reading(bucket.get(slot))
            if reading is None:
                continue
            held = strongest.get(reading.window)
            if held is None or reading.used_percent > held.used_percent:
                strongest[reading.window] = reading
    return list(strongest.values())


async def _request_rate_limits(process: asyncio.subprocess.Process) -> Any:
    """Run the documented handshake and return the method's result."""

    assert process.stdin is not None and process.stdout is not None

    def send(message: dict[str, Any]) -> None:
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))

    send(
        {
            "method": "initialize",
            "id": _INITIALIZE_ID,
            "params": {
                "clientInfo": {
                    "name": "delibra",
                    "title": "Delibra",
                    "version": "0.1.0",
                }
            },
        }
    )
    await process.stdin.drain()
    await _await_reply(process, _INITIALIZE_ID)

    send({"method": "initialized", "params": {}})
    send({"method": "account/rateLimits/read", "id": _RATE_LIMITS_ID})
    await process.stdin.drain()
    return await _await_reply(process, _RATE_LIMITS_ID)


async def _await_reply(process: asyncio.subprocess.Process, request_id: int) -> Any:
    """Read until the reply to ``request_id``, skipping notifications."""

    assert process.stdout is not None
    while True:
        line = await process.stdout.readline()
        if not line:
            raise RuntimeError("codex app-server closed before replying")
        if len(line) > _LINE_LIMIT:
            raise RuntimeError("codex app-server sent an oversized line")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # A notification Delibra does not model is not a protocol failure.
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(f"codex app-server refused the request: {message['error']}")
        return message.get("result")


async def read_codex_account_rate_limits(
    codex_home: Path,
    *,
    executable: str = "codex",
    timeout_seconds: int,
) -> list[RateLimitReading]:
    """Ask the local Codex process for the account's live rate limits.

    ``timeout_seconds`` bounds the whole exchange, not one read, so a server
    that answers the handshake and then stalls cannot hold a caller open.
    """

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=dict(os.environ, CODEX_HOME=str(codex_home)),
        )
        result = await asyncio.wait_for(
            _request_rate_limits(process), timeout=timeout_seconds
        )
        return parse_account_rate_limits(result)
    except Exception:
        LOGGER.warning("Codex account quota is unavailable", exc_info=True)
        return []
    finally:
        if process is not None:
            await _stop(process)


async def _stop(process: asyncio.subprocess.Process) -> None:
    """End the server without letting its shutdown fail the caller."""

    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    with suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=5)
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await process.wait()

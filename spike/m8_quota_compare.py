"""Phase 8 evidence probe: where does Delibra's Codex quota figure disagree?

Read-only. Spends no provider turn. Records, in one pass and within the same
minute, the three figures Phase 8b's acceptance requires:

1. the account's own live state, from ``codex app-server``'s documented
   ``account/rateLimits/read`` method, run against each candidate ``CODEX_HOME``;
2. Delibra's monitor, from ``$DELIBRA_HOME/usage.json``; and
3. the raw record they are supposed to derive from -- the newest ``token_count``
   in each home's rollouts, read through Delibra's own production reader.

Run: ``envs/bin/python -m spike.m8_quota_compare``
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, TextIO

from app.storage import read_latest_codex_rollout_state


SCAN_LIMIT = 200
READ_LIMIT = 4 * 1024 * 1024


def _send(stream: TextIO, message: dict[str, Any]) -> None:
    stream.write(json.dumps(message) + "\n")
    stream.flush()


def _receive(stream: TextIO, request_id: int) -> dict[str, Any]:
    for line in stream:
        message = json.loads(line)
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(message["error"])
            return message["result"]
    raise RuntimeError("codex app-server closed before replying")


def read_app_server_limits(codex_home: Path) -> dict[str, Any]:
    """Ask the local Codex process for the account's live rate limits."""

    environment = dict(os.environ, CODEX_HOME=str(codex_home))
    process = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        _send(
            process.stdin,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "delibra",
                        "title": "Delibra",
                        "version": "0.1.0",
                    }
                },
            },
        )
        _receive(process.stdout, 1)
        _send(process.stdin, {"method": "initialized", "params": {}})
        _send(process.stdin, {"method": "account/rateLimits/read", "id": 2})
        return _receive(process.stdout, 2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def newest_rollout(codex_home: Path) -> dict[str, Any]:
    """What Delibra's production reader sees in this home, and from which file."""

    state = read_latest_codex_rollout_state(
        codex_home, scan_limit=SCAN_LIMIT, read_limit=READ_LIMIT
    )
    matches = sorted(codex_home.glob("sessions/*/*/*/rollout-*.jsonl"), reverse=True)
    return {
        "newest_by_name": matches[0].name if matches else None,
        "newest_by_mtime": (
            max(matches, key=lambda path: path.stat().st_mtime).name
            if matches
            else None
        ),
        "file_count": len(matches),
        "context_window": state.context_window,
        "rate_limits": [
            {
                "window": reading.window,
                "used_percent": reading.used_percent,
                "resets_at": reading.resets_at.isoformat()
                if reading.resets_at
                else None,
            }
            for reading in state.rate_limits
        ],
    }


def main() -> int:
    delibra_home = Path(os.environ.get("DELIBRA_HOME", Path.home() / ".delibra"))
    homes = {
        "delibra": delibra_home / "codex-home",
        "operator": Path.home() / ".codex",
    }
    report: dict[str, Any] = {"read_at": datetime.now(UTC).isoformat(), "homes": {}}

    usage_path = delibra_home / "usage.json"
    report["monitor"] = (
        json.loads(usage_path.read_text("utf-8")) if usage_path.exists() else None
    )

    for name, home in homes.items():
        entry: dict[str, Any] = {"path": str(home), "exists": home.is_dir()}
        if entry["exists"]:
            entry["rollout"] = newest_rollout(home)
            try:
                entry["app_server"] = read_app_server_limits(home)
            except Exception as error:  # evidence probe: record, never raise
                entry["app_server_error"] = repr(error)
        report["homes"][name] = entry

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

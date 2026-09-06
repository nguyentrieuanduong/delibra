"""Deterministic subprocess fixture used by runner integration tests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


CODEX_REVOKED = (
    "Your access token could not be refreshed because your refresh token was revoked."
)
TRANSIENT_FAILURE = (
    "API Error: Connection closed mid-response. "
    "The response above may be incomplete."
)


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="success")
    parser.add_argument("--delay", type=float, default=0.01)
    parser.add_argument("--bytes", type=int, default=2_000)
    parser.add_argument("--source")
    args = parser.parse_args()
    sys.stdin.read()

    if args.source:
        Path(args.source).write_text("mutated by target\n", encoding="utf-8")

    mode = args.mode
    if mode == "codex-auth-event":
        emit({"kind": "error", "text": CODEX_REVOKED})
        return 1
    if mode == "codex-auth-stderr":
        sys.stderr.write(CODEX_REVOKED + "\n")
        sys.stderr.flush()
        return 1
    if mode == "transient":
        # The exact failure reported in modifications.md:6.
        emit({"kind": "error", "text": TRANSIENT_FAILURE})
        return 1
    if mode == "transient-noisy-stderr":
        # A proven retryable event beside text no classifier recognizes: the
        # noise must not outrank the structured verdict.
        emit({"kind": "error", "text": TRANSIENT_FAILURE})
        sys.stderr.write("note: workspace cache warmed in 12ms\n")
        sys.stderr.flush()
        return 1
    if mode == "quota-stderr":
        # Quota evidence that never reaches the adapter, which sees stdout only.
        sys.stderr.write("Error: 429 rate limit exceeded for this account\n")
        sys.stderr.flush()
        return 1
    if mode == "transient-then-quota":
        emit({"kind": "error", "text": TRANSIENT_FAILURE})
        emit({"kind": "error", "text": "429 rate limit exceeded"})
        return 1
    if mode == "spawn-child":
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(300)
        return 0
    if mode == "sleep":
        time.sleep(300)
        return 0
    if mode != "no-session":
        emit({"kind": "init", "session_id": "fake-native-session"})
    if mode == "huge-line":
        emit({"kind": "delta", "text": "x" * args.bytes})
        time.sleep(0.2)
        return 0

    emit({"kind": "delta", "text": "Hel"})
    time.sleep(args.delay)
    emit({"kind": "progress"})
    if mode == "malformed":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
    if mode == "provider-error":
        emit({"kind": "error", "text": "provider rejected request"})
    if mode == "flood":
        for _ in range(max(1, args.bytes // 10)):
            emit({"kind": "delta", "text": "0123456789"})
    time.sleep(args.delay)
    emit({"kind": "delta", "text": "lo"})
    if mode != "empty-result":
        emit({"kind": "result", "text": "Hello"})
    if mode in {"nonzero", "stderr"}:
        sys.stderr.write("deliberate stderr tail\n")
        sys.stderr.flush()
    return 7 if mode == "nonzero" else 0


if __name__ == "__main__":
    raise SystemExit(main())

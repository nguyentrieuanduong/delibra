"""Real-provider gate for native resume after a model/effort change.

This disposable probe retains no provider transcript. Its stdout is the CLI
interface and contains only command shapes, native IDs, resolved provider
metadata, and whether a unique first-turn canary was recalled after resume.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from spike.spike_claude import (
    allowed_env as claude_env,
    base_command as claude_command,
    cli_path as claude_path,
    event_session_ids,
    final_text as claude_final_text,
    invoke as invoke_claude,
)
from spike.spike_codex import (
    allowed_env as codex_env,
    final_text as codex_final_text,
    first_command as codex_first_command,
    global_options as codex_options,
    invoke as invoke_codex,
    resume_command as codex_resume_command,
    thread_ids,
)


CLAUDE_CANARY = "m4-claude-saffron-8472"
CODEX_CANARY = "m4-codex-ember-3916"


def prompt(canary: str, *, first: bool) -> str:
    if first:
        return (
            f"Remember this exact canary for my next message: {canary}. "
            "Reply only FIRST-TURN-STORED. Do not use tools."
        )
    return "Reply with only the exact canary I asked you to remember. Do not use tools."


def provider_models(events: list[dict[str, Any]]) -> list[str]:
    models: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        model = value.get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)
        for item in value.values():
            visit(item)

    visit(events)
    return models


def claude_terminal_diagnostics(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "result":
            continue
        diagnostics.append(
            {
                key: event.get(key)
                for key in ("subtype", "is_error", "result")
                if key in event
            }
        )
    return diagnostics


def rollout_turn_contexts(codex_home: Path) -> list[dict[str, str]]:
    contexts: list[dict[str, str]] = []
    for path in sorted(codex_home.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn_context":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            context: dict[str, str] = {}
            for key in ("turn_id", "model", "effort", "reasoning_effort"):
                value = payload.get(key)
                if isinstance(value, str):
                    context[key] = value
            contexts.append(context)
    return contexts


def run_claude(old_model: str, new_model: str) -> dict[str, Any]:
    executable = claude_path()
    version = subprocess.run(
        [executable, "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="delibra-m4-claude-") as temporary:
        workspace = Path(temporary).resolve() / "workspace"
        private_tmp = workspace / ".tmp"
        workspace.mkdir()
        private_tmp.mkdir()
        home = Path(os.environ["HOME"]).resolve()

        first_argv = claude_command(executable, old_model, "low")
        first = invoke_claude(
            first_argv,
            prompt(CLAUDE_CANARY, first=True),
            cwd=workspace,
            env=claude_env(home, private_tmp),
        )
        first_ids = event_session_ids(first.events)
        if first.returncode != 0 or len(first_ids) != 1:
            raise AssertionError(
                f"Claude first turn failed: rc={first.returncode}, ids={len(first_ids)}, "
                f"terminal={claude_terminal_diagnostics(first.events)!r}, "
                f"stderr={first.stderr[-500:]!r}"
            )
        session_id = next(iter(first_ids))

        resume_argv = [
            *claude_command(executable, new_model, "medium"),
            "--resume",
            session_id,
        ]
        resumed = invoke_claude(
            resume_argv,
            prompt(CLAUDE_CANARY, first=False),
            cwd=workspace,
            env=claude_env(home, private_tmp),
        )
        resumed_ids = event_session_ids(resumed.events)
        recalled = CLAUDE_CANARY in claude_final_text(resumed.events)
        same_id = resumed_ids == {session_id}
        first_models = provider_models(first.events)
        resumed_models = provider_models(resumed.events)
        if resumed.returncode != 0 or not recalled or not same_id:
            raise AssertionError(
                "Claude config-change resume failed: "
                f"rc={resumed.returncode}, recall={recalled}, same_id={same_id}, "
                f"stderr={resumed.stderr[-500:]!r}"
            )
        if not first_models or not resumed_models or set(first_models) == set(resumed_models):
            raise AssertionError(
                f"Claude did not expose changed model metadata: {first_models} -> {resumed_models}"
            )

        return {
            "version": version,
            "old": {"requested_model": old_model, "effort": "low", "resolved_models": first_models},
            "new": {"requested_model": new_model, "effort": "medium", "resolved_models": resumed_models},
            "first_command": "claude ... --model <old-model> --effort low ...",
            "resume_command": "claude ... --model <new-model> --effort medium ... --resume <session-id>",
            "native_id": session_id,
            "same_native_id": same_id,
            "canary_recalled": recalled,
            "resume_after_config_change": True,
        }


def run_codex(old_model: str, new_model: str) -> dict[str, Any]:
    executable = shutil.which("codex")
    if executable is None:
        raise RuntimeError("codex executable not found")
    version = subprocess.run(
        [executable, "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="delibra-m4-codex-") as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        private_tmp = workspace / ".tmp"
        home = root / "home"
        codex_home = root / "codex-home"
        for path in (workspace, private_tmp, home, codex_home):
            path.mkdir(parents=True, exist_ok=True)
        real_auth = Path(os.environ["HOME"]) / ".codex" / "auth.json"
        if not real_auth.is_file():
            raise RuntimeError(f"Codex auth file not found: {real_auth}")
        shutil.copyfile(real_auth, codex_home / "auth.json")
        (codex_home / "auth.json").chmod(0o600)
        environment = codex_env(home, private_tmp, codex_home)

        first_options = codex_options(executable, workspace, old_model, "low")
        first = invoke_codex(
            codex_first_command(first_options),
            prompt(CODEX_CANARY, first=True),
            cwd=workspace,
            env=environment,
        )
        first_ids = thread_ids(first.events)
        if first.returncode != 0 or len(first_ids) != 1:
            raise AssertionError(
                f"Codex first turn failed: rc={first.returncode}, ids={len(first_ids)}, "
                f"stderr={first.stderr[-500:]!r}"
            )
        thread_id = next(iter(first_ids))
        first_contexts = rollout_turn_contexts(codex_home)

        resume_options = codex_options(executable, workspace, new_model, "medium")
        resumed = invoke_codex(
            codex_resume_command(resume_options, thread_id),
            prompt(CODEX_CANARY, first=False),
            cwd=workspace,
            env=environment,
        )
        resumed_ids = thread_ids(resumed.events)
        contexts = rollout_turn_contexts(codex_home)
        new_contexts = contexts[len(first_contexts) :]
        recalled = CODEX_CANARY in codex_final_text(resumed.events)
        same_id = resumed_ids == {thread_id}
        if resumed.returncode != 0 or not recalled or not same_id:
            raise AssertionError(
                "Codex config-change resume failed: "
                f"rc={resumed.returncode}, recall={recalled}, same_id={same_id}, "
                f"stderr={resumed.stderr[-500:]!r}"
            )
        if not first_contexts or not new_contexts:
            raise AssertionError(
                f"Codex rollout lacked turn contexts: first={first_contexts}, new={new_contexts}"
            )

        return {
            "version": version,
            "old": {"requested_model": old_model, "effort": "low", "turn_contexts": first_contexts},
            "new": {"requested_model": new_model, "effort": "medium", "turn_contexts": new_contexts},
            "first_command": "codex --model <old-model> ... model_reasoning_effort=low ... exec ...",
            "resume_command": "codex --model <new-model> ... model_reasoning_effort=medium ... exec resume <thread-id> ...",
            "native_id": thread_id,
            "same_native_id": same_id,
            "canary_recalled": recalled,
            "resume_after_config_change": True,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["all", "claude", "codex"], default="all")
    parser.add_argument("--claude-old-model", default="sonnet")
    parser.add_argument("--claude-new-model", default="opus")
    parser.add_argument("--codex-old-model", default="gpt-5.4")
    parser.add_argument("--codex-new-model", default="gpt-5.4-mini")
    args = parser.parse_args()
    result: dict[str, Any] = {}
    if args.provider in {"all", "claude"}:
        result["claude"] = run_claude(args.claude_old_model, args.claude_new_model)
    if args.provider in {"all", "codex"}:
        result["codex"] = run_codex(args.codex_old_model, args.codex_new_model)
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

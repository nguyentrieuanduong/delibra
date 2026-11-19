"""The optional model check: a tool-less one-off CLI call over URLs only."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import uuid

from app.prompts import ANSWER_FORMAT, BUILT_IN, Prompts

CHECK_TIMEOUT_SECONDS = 60
BATCH_WINDOW_SECONDS = 2.0
BATCH_MAX = 20
REASON_MAX_CHARS = 300
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "verdict", "reason"],
        "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": ["allow", "flag"]},
                       "reason": {"type": "string"}}}}},
}


class CheckError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_prompt(items: list[tuple[str, str]], *, prompts: Prompts = BUILT_IN) -> str:
    return (
        prompts.render("model_check", answer_format=ANSWER_FORMAT)
        + "\n\nInput (JSON):\n"
        + json.dumps([{"id": rid, "url": url} for rid, url in items])
    )


def claude_argv(executable: str, model: str) -> list[str]:
    return [
        executable, "-p", "--output-format", "json", "--model", model, "--effort", "low",
        "--tools", "", "--permission-mode", "dontAsk", "--safe-mode", "--setting-sources", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--no-chrome",
    ]


def codex_argv(executable: str, model: str, workdir: Path) -> list[str]:
    argv = [
        executable, "--model", model, "--ask-for-approval", "never", "--sandbox", "read-only",
        "--cd", str(workdir), "--config", 'model_reasoning_effort="low"',
        "--config", "project_root_markers=[]", "--config", "project_doc_max_bytes=0",
    ]
    for feature in ("shell_tool", "view_image", "hooks", "plugins", "apps", "memories",
                    "goals", "multi_agent", "computer_use", "browser_use"):
        argv += ["--disable", feature]
    return argv + [
        "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
        "--ignore-rules", "--strict-config", "--output-schema", str(workdir / "schema.json"),
        "-o", str(workdir / "last.json"), "-",
    ]


def parse_verdicts(text: str, ids: list[str]) -> dict[str, tuple[str, str]]:
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise CheckError("model check unavailable: answer was not JSON") from exc
    items = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise CheckError("model check unavailable: answer had no verdicts")
    result: dict[str, tuple[str, str]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("verdict") not in ("allow", "flag") \
                or item.get("id") not in ids or item["id"] in result:
            raise CheckError("model check unavailable: answer did not match the request")
        # A JSON escape can decode to a lone surrogate, which no page encodes.
        reason = str(item.get("reason", ""))[:REASON_MAX_CHARS].encode("utf-8", "replace").decode("utf-8")
        result[item["id"]] = (item["verdict"], reason)
    if set(result) != set(ids):
        raise CheckError("model check unavailable: answer missed a URL")
    return result


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def run_model_check(provider: str, executable: str, model: str, items: list[tuple[str, str]],
                          work_root: Path, *, prompts: Prompts = BUILT_IN) -> dict[str, tuple[str, str]]:
    workdir = work_root / f"download-check-{uuid.uuid4().hex}"
    workdir.mkdir(mode=0o700, parents=True)
    try:
        if provider == "codex":
            (workdir / "schema.json").write_text(json.dumps(SCHEMA))
            argv = codex_argv(executable, model, workdir)
        else:
            argv = claude_argv(executable, model)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=dict(os.environ),
            )
        except OSError as exc:  # plan review B1: fail closed, never escape
            raise CheckError("model check unavailable: the CLI could not start") from exc
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(check_prompt(items, prompts=prompts).encode("utf-8")), CHECK_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            await _stop(process)
            raise CheckError("model check unavailable: timed out") from exc
        except BaseException:
            # Cancelled by shutdown or settle: never leave the CLI running
            # (spending quota) in a folder about to be removed.
            await asyncio.shield(_stop(process))
            raise
        if process.returncode != 0:
            raise CheckError(f"model check unavailable: exit {process.returncode}")
        ids = [rid for rid, _ in items]
        if provider == "codex":
            try:
                text = (workdir / "last.json").read_text(encoding="utf-8")
            except OSError as exc:
                raise CheckError("model check unavailable: no answer file") from exc
        else:
            try:
                envelope = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise CheckError("model check unavailable: output was not JSON") from exc
            if not isinstance(envelope, dict) or envelope.get("is_error") or not isinstance(envelope.get("result"), str):
                raise CheckError("model check unavailable: the CLI reported an error")
            text = envelope["result"]
        return parse_verdicts(text, ids)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

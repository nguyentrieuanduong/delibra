import json, os, stat
from pathlib import Path
import pytest
from app.download_model_check import (
    CheckError, claude_argv, codex_argv, parse_verdicts, run_model_check,
)

def script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "cli"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)

def test_claude_argv_has_no_tools() -> None:
    argv = claude_argv("claude", "sonnet")
    assert argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" in argv and argv[argv.index("--model") + 1] == "sonnet"

def test_codex_argv_disables_the_shell(tmp_path: Path) -> None:
    argv = codex_argv("codex", "gpt-5.5-mini", tmp_path)
    assert ["--disable", "shell_tool"] == argv[argv.index("shell_tool") - 1: argv.index("shell_tool") + 1]
    assert "--search" not in argv and "read-only" in argv and "--output-schema" in argv

def test_parse_accepts_exact_ids() -> None:
    text = json.dumps({"verdicts": [{"id": "D1", "verdict": "allow", "reason": "ok"},
                                    {"id": "D2", "verdict": "flag", "reason": "base64"}]})
    assert parse_verdicts(text, ["D1", "D2"]) == {"D1": ("allow", "ok"), "D2": ("flag", "base64")}

@pytest.mark.parametrize("text", [
    "not json", '<invoke name="Glob">', json.dumps({"verdicts": [{"id": "D1", "verdict": "allow", "reason": ""}]}),
    json.dumps({"verdicts": [{"id": "D1", "verdict": "maybe", "reason": ""}, {"id": "D2", "verdict": "allow", "reason": ""}]}),
    json.dumps({"verdicts": [{"id": "D1", "verdict": "allow", "reason": ""}] * 2 + [{"id": "D2", "verdict": "allow", "reason": ""}]}),
])
def test_parse_fails_closed(text: str) -> None:
    with pytest.raises(CheckError):
        parse_verdicts(text, ["D1", "D2"])

@pytest.mark.asyncio
async def test_claude_run_reads_the_result_field(tmp_path: Path) -> None:
    verdicts = json.dumps({"verdicts": [{"id": "D1", "verdict": "flag", "reason": "token"}]})
    cli = script(tmp_path, f"cat >/dev/null; printf '%s' '{json.dumps({'is_error': False, 'result': verdicts})}'\n")
    result = await run_model_check("claude", cli, "sonnet", [("D1", "https://a.org")], tmp_path)
    assert result == {"D1": ("flag", "token")}

@pytest.mark.asyncio
async def test_a_missing_cli_fails_closed(tmp_path: Path) -> None:
    # Plan review B1.
    with pytest.raises(CheckError, match="could not start"):
        await run_model_check("claude", str(tmp_path / "missing"), "m", [("D1", "u")], tmp_path)

@pytest.mark.asyncio
async def test_a_failing_or_slow_cli_fails_closed(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(CheckError, match="exit"):
        await run_model_check("claude", script(tmp_path, "exit 3\n"), "m", [("D1", "u")], tmp_path)
    monkeypatch.setattr("app.download_model_check.CHECK_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(CheckError, match="timed out"):
        await run_model_check("claude", script(tmp_path, "sleep 5\n"), "m", [("D1", "u")], tmp_path)

@pytest.mark.asyncio
async def test_a_cancelled_check_kills_the_cli(tmp_path: Path) -> None:
    # Review 2026-11-15: shutdown or settle cancels checks; the CLI must not
    # keep running (and spending quota) after Delibra stopped waiting.
    import asyncio
    pid_file = tmp_path / "pid"
    cli = script(tmp_path, f"echo $$ > {pid_file}\nexec sleep 30\n")
    task = asyncio.create_task(run_model_check("claude", cli, "m", [("D1", "u")], tmp_path))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

def test_a_reason_that_cannot_be_utf8_is_made_safe() -> None:
    # Review 2026-11-15: a lone surrogate in the stored note broke "Show full URL".
    text = '{"verdicts": [{"id": "D1", "verdict": "flag", "reason": "x\\ud800y"}]}'
    verdict, reason = parse_verdicts(text, ["D1"])["D1"]
    assert verdict == "flag" and reason.encode("utf-8") and reason.startswith("x") and reason.endswith("y")

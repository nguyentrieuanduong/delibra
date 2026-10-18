"""Auto questions spike: can each turn kind read the store and write its outbox?

Task 0 of `docs/specs/2026-10-17-auto-questions-design.md`. Not part of pytest:
it drives the real CLIs and spends subscriptions. Run only with operator
approval:

    envs/bin/python spike/spike_questions.py --provider claude
    envs/bin/python spike/spike_questions.py --provider codex

Each provider runs one discussion turn and one build turn with Delibra's
pinned argv. A build turn's Claude argv gains the one rule the design adds,
`Edit(//<temp>/questions/**)`. Verdicts come from filesystem bytes and from the
store token appearing in the provider's events, never from model prose.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.base import CodingContext, RunContext, build_command_environment  # noqa: E402
from app.agents.claude import ClaudeAdapter  # noqa: E402
from app.agents.codex import CodexAdapter  # noqa: E402
from app.coding_env import developer_tools_dir  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import SessionConfig  # noqa: E402
from spike.spike_add_dir import cli_path, cli_version, invoke, with_write_rules  # noqa: E402

FINDINGS = ROOT / "spike" / "FINDINGS.md"
SEED = b"SEEDED-MUST-NOT-CHANGE\n"
OUTBOX = {"questions": [{"question": "spike", "assumption": "none", "blocking": False}]}
# One marker per forbidden write, so an attempt is visible in the events and
# attributable to its step; unchanged bytes alone do not prove a refusal.
STORE_MARKER = "CHANGED-STORE"
OTHER_MARKER = "CHANGED-OTHER"


@dataclass
class Tree:
    root: Path
    project: Path
    workspace: Path
    temp: Path
    codex_home: Path
    store: Path
    token: str
    other_outbox: Path


def build_tree(root: Path, provider: str) -> Tree:
    project = root / "project"
    subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
    delibra = project / ".delibra"
    workspace = delibra / "sessions" / "Spike" / "workspace"
    (workspace / "questions").mkdir(parents=True)
    other = delibra / "sessions" / "Other" / "workspace" / "questions"
    other.mkdir(parents=True)
    other_outbox = other / "outbox.json"
    other_outbox.write_bytes(SEED)
    token = "QTOKEN-" + secrets.token_hex(8)
    store = delibra / "questions.json"
    store.write_text(json.dumps({"version": 1, "token": token}) + "\n", encoding="utf-8")
    temp = root / "build-tmp" / ("a" * 32)
    (temp / "questions").mkdir(parents=True, mode=0o700)
    codex_home = root / "codex-home"
    codex_home.mkdir(mode=0o700)
    if provider == "codex":
        shutil.copyfile(Settings.from_env().codex_home / "auth.json", codex_home / "auth.json")
        (codex_home / "auth.json").chmod(0o600)
    return Tree(root, project, workspace, temp, codex_home, store, token, other_outbox)


def outbox_path(tree: Tree, kind: str) -> Path:
    base = tree.temp if kind == "build" else tree.workspace
    return base / "questions" / "outbox.json"


def prompt(tree: Tree, kind: str) -> str:
    return (
        "Do these steps in order using your tools. Continue after any refusal; "
        "do not ask for permission.\n"
        f"1. Read the file {tree.store} and quote the value of its \"token\" key.\n"
        f"2. Create the file {outbox_path(tree, kind)} containing exactly this "
        f"JSON on one line: {json.dumps(OUTBOX)}\n"
        f"3. Overwrite the file {tree.store} with the text {STORE_MARKER}.\n"
        f"4. Overwrite the file {tree.other_outbox} with the text {OTHER_MARKER}.\n"
        "Reply DONE."
    )


def session(provider: str) -> SessionConfig:
    return SessionConfig(
        id="a" * 32,
        name="Spike",
        agent=provider,
        model="claude-opus-5-5" if provider == "claude" else "gpt-6-sol",
        effort="low",
        role_instructions="Follow the user's instructions exactly.",
        cli_session_id=None,
        status="idle",
        created_at="2026-10-17T00:00:00Z",
        rounds=[],
    )


def coding(tree: Tree) -> CodingContext:
    return CodingContext(
        project_root=tree.project,
        temp_dir=tree.temp,
        env_dir=tree.project / "envs",
        env_tool="venv",
        network_allowlist=(),
        read_roots=tuple(
            path for path in (developer_tools_dir(dict(os.environ)),) if path is not None
        ),
    )


def context(tree: Tree, kind: str) -> RunContext:
    return RunContext(
        user_prompt=prompt(tree, kind),
        resume_id=None,
        resume_strategy="native",
        staged_history=[],
        staged_source=None,
        workspace=tree.project if kind == "build" else tree.workspace,
        project_root=tree.project,
        coding=coding(tree) if kind == "build" else None,
    )


def environment(provider: str, tree: Tree, kind: str) -> dict[str, str]:
    # Mirrors RunManager._subprocess_environment.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM") if k in os.environ}
    tmpdir = tree.workspace / ".tmp"
    tmpdir.mkdir(exist_ok=True)
    env["TMPDIR"] = str(tmpdir)
    if kind == "build":
        env.update(build_command_environment(coding(tree)))
    if provider == "codex":
        env["CODEX_HOME"] = str(tree.codex_home)
    return env


def command_argv(provider: str, tree: Tree, kind: str) -> tuple[list[str], str]:
    adapter = ClaudeAdapter(cli_path("claude")) if provider == "claude" else CodexAdapter(cli_path("codex"))
    command = adapter.build_command(session(provider), context(tree, kind))
    argv = command.argv
    if provider == "claude" and kind == "build":
        argv = with_write_rules(argv, [tree.temp / "questions"])
    return argv, command.stdin


def judge(tree: Tree, kind: str, events_text: str) -> dict[str, str]:
    """Score one turn from bytes on disk and the provider's own events."""

    rows: dict[str, str] = {}
    rows[f"{kind}_read_store"] = (
        "PASS" if tree.token in events_text else "FAIL (store token never appeared)"
    )
    outbox = outbox_path(tree, kind)
    try:
        written = json.loads(outbox.read_text(encoding="utf-8"))
    except FileNotFoundError:
        rows[f"{kind}_write_outbox"] = "FAIL (outbox not created)"
    except (OSError, ValueError) as exc:
        rows[f"{kind}_write_outbox"] = f"FAIL (outbox unreadable: {type(exc).__name__})"
    else:
        rows[f"{kind}_write_outbox"] = (
            "PASS" if written == OUTBOX else "FAIL (outbox content differs)"
        )
    store_text = tree.store.read_text(encoding="utf-8")
    rows[f"{kind}_store_protected"] = protected(
        STORE_MARKER in events_text,
        tree.token in store_text and STORE_MARKER not in store_text,
        "store was overwritten",
    )
    rows[f"{kind}_other_outbox_protected"] = protected(
        OTHER_MARKER in events_text,
        tree.other_outbox.read_bytes() == SEED,
        "another agent's outbox changed",
    )
    return rows


def protected(attempted: bool, unchanged: bool, breach: str) -> str:
    if not unchanged:
        return f"FAIL ({breach})"
    return "PASS" if attempted else "INCONCLUSIVE (no attempt in events)"


def reset(tree: Tree) -> None:
    """Restore the seeded state between the two turns."""

    tree.store.write_text(json.dumps({"version": 1, "token": tree.token}) + "\n", encoding="utf-8")
    tree.other_outbox.write_bytes(SEED)
    for kind in ("discussion", "build"):
        outbox_path(tree, kind).unlink(missing_ok=True)


def run_turn(provider: str, tree: Tree, kind: str, argv_log: list[list[str]]) -> dict[str, str]:
    argv, stdin = command_argv(provider, tree, kind)
    # "/<TMP>" keeps a `//`-anchored permission rule visibly `//`.
    argv_log.append([part.replace(str(tree.root), "/<TMP>").replace(str(Path.home()), "~") for part in argv])
    cwd = tree.project if kind == "build" else tree.workspace
    result = invoke(argv, stdin, cwd=cwd, env=environment(provider, tree, kind), provider=provider, timeout=600)
    events_text = "".join(json.dumps(event) + "\n" for event in result.events)
    (tree.root / f"{kind}.jsonl").write_text(events_text, encoding="utf-8")
    (tree.root / f"{kind}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    return judge(tree, kind, events_text)


def render(provider: str, version: str, rows: dict[str, str], argv: list[list[str]]) -> str:
    verdict = (
        "FAIL" if any(v.startswith("FAIL") for v in rows.values())
        else "INCONCLUSIVE" if any(v.startswith("INCONCLUSIVE") for v in rows.values())
        else "PASS"
    )
    table = "\n".join(f"| {name} | {value} |" for name, value in rows.items())
    argv_text = "\n".join(shlex.join(item) for item in argv)
    return (
        f"## Auto questions spike: {provider}\n\n"
        f"- Version: `{version}`\n- Verdict: **{verdict}**\n\n"
        f"| Probe | Result |\n|---|---|\n{table}\n\n"
        f"Argv (sanitized):\n\n```text\n{argv_text}\n```\n"
    )


def upsert(provider: str, section: str) -> None:
    start = f"<!-- QUESTIONS-SPIKE-{provider.upper()}:START -->"
    end = f"<!-- QUESTIONS-SPIKE-{provider.upper()}:END -->"
    existing = FINDINGS.read_text(encoding="utf-8")
    block = f"{start}\n{section.rstrip()}\n{end}"
    if start in existing and end in existing:
        existing = re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: block, existing, flags=re.DOTALL)
    else:
        existing = existing.rstrip() + "\n\n" + block + "\n"
    FINDINGS.write_text(existing, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    parser.add_argument("--keep", action="store_true", help="keep the temp tree (Codex tree holds copied auth.json)")
    args = parser.parse_args()
    provider = args.provider
    rows: dict[str, str] = {}
    argv_log: list[list[str]] = []
    version = "unknown"
    # Under HOME, like the build spike: real projects and ~/.delibra/build-tmp
    # live there, and Claude's sandbox denyRead covers ~/.
    root = Path(tempfile.mkdtemp(prefix=".delibra-questions-spike-", dir=Path.home()))
    try:
        version = cli_version(cli_path(provider))
        tree = build_tree(root, provider)
        rows.update(run_turn(provider, tree, "discussion", argv_log))
        reset(tree)
        rows.update(run_turn(provider, tree, "build", argv_log))
    except Exception as exc:  # the findings section must survive any failure
        rows["spike_error"] = f"INCONCLUSIVE ({type(exc).__name__}: {exc})"
    finally:
        section = render(provider, version, rows, argv_log)
        upsert(provider, section)
        # A CLI harness: stdout is its interface, as in spike_build_sandbox.py.
        print(section)
        if args.keep:
            print(f"kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    return 0 if all(v.startswith("PASS") for v in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

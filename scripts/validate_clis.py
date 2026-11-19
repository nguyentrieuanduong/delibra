"""Validate the installed Claude and Codex CLIs against what Delibra passes them.

Tier 1 spends no model calls: every flag and value Delibra's argv builders produce is
checked against the CLI's own help, Codex `-c` keys are parsed by `codex exec
--strict-config` up to config load (a trailing unknown probe key stops it there, with
an empty CODEX_HOME), the build sandbox runs model-free under `codex sandbox`.
Tier 2 spends about three tiny real turns per provider (ask the user first): a
sandbox-probe build turn, a chat turn read by Delibra's own parsers, and a native
resume with a changed system prompt.

Usage:
    envs/bin/python scripts/validate_clis.py --tier 1 [--provider claude|codex|all]
    envs/bin/python scripts/validate_clis.py --tier 2 [--provider claude|codex|all]

Stdout is this script's interface: one PASS/FAIL table. Exit 0 only when no gating
check (PASS/FAIL/INCONCLUSIVE) failed. Scratch goes to tmp/validate-clis/ (git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.base import AgentEvent, CodingContext, DockerAccess, RunContext  # noqa: E402
from app.agents.claude import ClaudeAdapter  # noqa: E402
from app.agents.codex import CodexAdapter  # noqa: E402
from app.download_model_check import claude_argv, codex_argv  # noqa: E402
from app.models import SessionConfig  # noqa: E402

SCRATCH = ROOT / "tmp" / "validate-clis"
PROBE_KEY = "zzzz_delibra_probe"
MARKER = "DELIBRA-PROMPT-INPUT-MARKER"
GATING = ("PASS", "FAIL", "INCONCLUSIVE")
Arity = Literal["none", "one", "optional", "many"]


# ---------------------------------------------------------------- help parsing


@dataclass(frozen=True)
class OptionSpec:
    names: tuple[str, ...]
    arity: Arity
    choices: tuple[str, ...] = ()
    description: str = ""


_OPTION_START = re.compile(r"^ {2,6}-")
_NAME = re.compile(r"(?<![\w-])(--?[A-Za-z0-9][\w-]*)")
_VALUE = re.compile(r"([<\[])([^>\]]*)([>\]])(\.\.\.)?")


def _blocks(text: str) -> list[list[str]]:
    """Option entries: a line starting with a dash, plus its indented continuation."""

    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if _OPTION_START.match(line):
            current = [line]
            blocks.append(current)
        elif current is not None and (line.startswith(" ") or not line.strip()):
            current.append(line)
        else:
            current = None  # a section header ends the entry
    return blocks


def _choices(body: str, lines: list[str]) -> tuple[str, ...]:
    clap = re.search(r"\[possible values: ([^\]]+)\]", body)
    if clap:
        return tuple(item.strip() for item in clap.group(1).split(","))
    commander = re.search(r'\(choices: ((?:"[^"]*",? ?)+)', body)
    if commander:
        return tuple(re.findall(r'"([^"]*)"', commander.group(1)))
    if "Possible values:" in body:
        return tuple(match.group(1) for line in lines if (match := re.match(r"^\s+- ([\w-]+):", line)))
    return ()


def parse_help(text: str) -> dict[str, OptionSpec]:
    """Every option a CLI help text lists, under each of its names."""

    options: dict[str, OptionSpec] = {}
    for block in _blocks(text):
        head, _, inline = block[0].strip().partition("  ")
        value = _VALUE.search(head)
        names = tuple(_NAME.findall(head[: value.start()] if value else head))
        if not names:
            continue
        if value is None:
            arity: Arity = "none"
        elif value.group(1) == "[":
            arity = "optional"
        else:
            arity = "many" if value.group(4) or value.group(2).endswith("...") else "one"
        description = " ".join(part.strip() for part in [inline, *block[1:]] if part.strip())
        spec = OptionSpec(names, arity, _choices(description, block[1:]), description)
        options.update({name: spec for name in names})
    return options


def parse_subcommands(text: str) -> frozenset[str]:
    names: set[str] = set()
    inside = False
    for line in text.splitlines():
        if line.startswith("Commands:"):
            inside = True
        elif inside and line and not line.startswith(" "):
            break
        elif inside and (match := re.match(r"^  ([a-z][\w-]*)(?:\|[\w-]+)?\b", line)):
            names.add(match.group(1))
    return frozenset(names)


def listed_levels(spec: OptionSpec) -> tuple[str, ...]:
    """A value list written in the description, e.g. `(low, medium, high)`."""

    match = re.search(r"\(([a-z]+(?:, [a-z]+)+)\)", spec.description)
    return tuple(match.group(1).split(", ")) if match else ()


# ----------------------------------------------------------------- argv checks


@dataclass(frozen=True)
class FlagUse:
    scope: str
    flag: str
    value: str | None
    problem: str | None


SUBCOMMANDS = {"codex": {"exec": "codex exec"}, "codex exec": {"resume": "codex exec resume"}}


def check_argv(provider: str, argv: list[str], scopes: dict[str, dict[str, OptionSpec]]) -> list[FlagUse]:
    """Walk an argv the way the CLI parses it; one entry per option use."""

    scope = provider
    uses: list[FlagUse] = []
    tokens = argv[1:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("-") or token == "-":
            scope = SUBCOMMANDS.get(scope, {}).get(token, scope)
            continue
        flag, inline_sep, inline = token.partition("=")
        spec = scopes[scope].get(flag)
        if spec is None:
            uses.append(FlagUse(scope, flag, None, f"not an option of `{scope}`"))
            continue
        values: list[str] = [inline] if inline_sep else []
        if not inline_sep and spec.arity == "one" and index < len(tokens):
            values.append(tokens[index])
            index += 1
        elif not inline_sep and spec.arity in ("many", "optional"):
            while index < len(tokens) and not tokens[index].startswith("-"):
                values.append(tokens[index])
                index += 1
                if spec.arity == "optional":
                    break
        value = " ".join(values) if values else None
        problem = None
        if spec.choices and values and any(item not in spec.choices for item in values):
            bad = next(item for item in values if item not in spec.choices)
            problem = f"value {bad!r} is not one of {', '.join(spec.choices)}"
        uses.append(FlagUse(scope, flag, value, problem))
    return uses


def config_keys(argv: list[str]) -> list[str]:
    keys = []
    for flag, value in zip(argv, argv[1:]):
        if flag in ("-c", "--config"):
            keys.append(value.split("=", 1)[0])
    return keys


def disabled_features(argv: list[str]) -> list[str]:
    return [value for flag, value in zip(argv, argv[1:]) if flag == "--disable"]


# -------------------------------------------------------------- check records


@dataclass(frozen=True)
class Check:
    provider: str
    name: str
    status: str
    detail: str = ""


def flag_checks(provider: str, variants: dict[str, list[str]], scopes: dict[str, dict[str, OptionSpec]]) -> list[Check]:
    seen: dict[tuple[str, str], list[str]] = {}
    problems: dict[tuple[str, str], str] = {}
    for label, argv in variants.items():
        for use in check_argv(provider, argv, scopes):
            key = (use.scope, use.flag)
            seen.setdefault(key, []).append(label)
            if use.problem:
                problems.setdefault(key, f"{use.problem} ({label})")
    return [
        Check(provider, f"`{scope}` {flag}", "FAIL" if (scope, flag) in problems else "PASS",
              problems.get((scope, flag), ", ".join(dict.fromkeys(labels))))
        for (scope, flag), labels in seen.items()
    ]


def parse_features(text: str) -> dict[str, tuple[str, bool]]:
    table = {}
    for line in text.splitlines():
        match = re.match(r"^(\S+)\s+(stable|experimental|under development|deprecated|removed)\s+(true|false)\s*$", line)
        if match:
            table[match.group(1)] = (match.group(2), match.group(3) == "true")
    return table


def check_features(provider: str, names: list[str], table: dict[str, tuple[str, bool]]) -> list[Check]:
    checks = []
    for name in dict.fromkeys(names):
        if name not in table:
            checks.append(Check(provider, f"feature {name}", "FAIL", "unknown to `codex features list`"))
        elif table[name][0] == "removed":
            checks.append(Check(provider, f"feature {name}", "INCONCLUSIVE",
                                "removed in this CLI: check whether a replacement is enabled"))
        else:
            checks.append(Check(provider, f"feature {name}", "PASS",
                                "deprecated" if table[name][0] == "deprecated" else ""))
    return checks


def dry_parse_status(returncode: int, output: str) -> tuple[str, str]:
    """`codex exec --strict-config` stops at the first unknown field; the probe key is last."""

    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if f"unknown configuration field `{PROBE_KEY}`" in output:
        return "PASS", "argv parsed; every -c key known and well-typed"
    if returncode == 0:
        return "INCONCLUSIVE", "the CLI accepted the probe key"
    return "FAIL", first[:300]


def codex_effort_check(levels: list[str], catalog: dict[str, Any]) -> Check:
    offered: set[str] = set()
    for model in catalog.get("models", []):
        if model.get("visibility") == "list":
            offered.update(
                item.get("effort") if isinstance(item, dict) else item
                for item in model.get("supported_reasoning_levels", [])
            )
    missing = [level for level in levels if level not in offered]
    if missing:
        return Check("codex", "effort levels (model catalog)", "WARN",
                     f"no listed model supports: {', '.join(missing)}")
    return Check("codex", "effort levels (model catalog)", "PASS")


def verdict(checks: list[Check], provider: str) -> str:
    statuses = {check.status for check in checks if check.provider == provider}
    for status in ("FAIL", "INCONCLUSIVE"):
        if status in statuses:
            return status
    return "PASS"


def render_table(checks: list[Check]) -> str:
    rows = ["| Provider | Check | Status | Detail |", "|---|---|---|---|"]
    rows += [
        "| " + " | ".join(cell.replace("|", "\\|") for cell in (c.provider, c.name, c.status, c.detail)) + " |"
        for c in checks
    ]
    return "\n".join(rows)


# ----------------------------------------------------------- argv variants


def _session(agent: str, *, model: str = "m", role: str = "Be brief.") -> SessionConfig:
    return SessionConfig(
        id=("a" if agent == "claude" else "b") * 32, name="Validate", agent=agent, model=model,
        effort="low", role_instructions=role, cli_session_id=None, status="idle",
        created_at="2026-11-19T00:00:00Z",
    )


def _chat(root: Path, *, resume: str | None = None, stateless: bool = False, prompt: str = "Hi") -> RunContext:
    return RunContext(
        user_prompt=prompt, resume_id=resume, resume_strategy="stateless" if stateless else "native",
        staged_history=[Path("inputs/round-02/history-01.md")] if stateless else [], staged_source=None,
        workspace=root / "workspace", writable_roots=(root / "out",), project_root=root / "project",
    )


def _build(root: Path, *, docker: bool) -> RunContext:
    return RunContext(
        user_prompt="Build.", resume_id=None, resume_strategy="native", staged_history=[],
        staged_source=None, workspace=root / "project", project_root=root / "project",
        coding=CodingContext(
            project_root=root / "project", temp_dir=root / "build-tmp", env_dir=root / "project" / "envs",
            env_tool="venv", network_allowlist=("pypi.org",),
            read_roots=(Path("/Library/Developer/CommandLineTools"),),
            docker=DockerAccess(socket=root / "docker.sock", cli_read_roots=(root / "docker-bin",))
            if docker else None,
            downloads=True, convert=True,
        ),
    )


def argv_variants(provider: str, executable: str, root: Path) -> dict[str, list[str]]:
    """Every argv shape Delibra builds, from the adapters themselves."""

    for name in ("workspace", "out", "project", "build-tmp", "check"):
        (root / name).mkdir(parents=True, exist_ok=True)
    resume = "00000000-0000-4000-8000-000000000000"
    adapter = ClaudeAdapter(executable) if provider == "claude" else CodexAdapter(executable)
    session = _session(provider, role=f"Be brief. {MARKER}")
    variants = {
        "chat": adapter.build_command(session, _chat(root)).argv,
        "chat (CLI default model)": adapter.build_command(replace(session, model=""), _chat(root)).argv,
        "chat resume": adapter.build_command(session, _chat(root, resume=resume)).argv,
        "stateless and compaction": adapter.build_command(session, _chat(root, stateless=True)).argv,
        "build": adapter.build_command(session, _build(root, docker=False)).argv,
        "build with Docker": adapter.build_command(session, _build(root, docker=True)).argv,
    }
    variants["model check"] = (
        claude_argv(executable, "m") if provider == "claude" else codex_argv(executable, "m", root / "check")
    )
    return variants


# ------------------------------------------------------------------- Tier 1


def _run(argv: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None,
         stdin: str = "", timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)


def _empty_codex_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    env["CODEX_HOME"] = str(home)
    return env


def tier1_claude(executable: str, root: Path) -> list[Check]:
    help_text = _run([executable, "--help"]).stdout
    scopes = {"claude": parse_help(help_text)}
    checks = flag_checks("claude", argv_variants("claude", executable, root), scopes)
    levels = listed_levels(scopes["claude"].get("--effort", OptionSpec(("--effort",), "one")))
    missing = [level for level in ClaudeAdapter.EFFORT_LEVELS if level not in levels]
    checks.append(Check("claude", "effort levels (--effort help)", "FAIL" if missing else "PASS",
                        f"missing: {', '.join(missing)}" if missing else ", ".join(levels)))
    checks += [
        Check("claude", "--tools names", "UNCHECKED", "no model-free list; Tier 2 compares the init event's tools"),
        Check("claude", "--settings sandbox keys, --allowedTools rules", "UNCHECKED",
              "no model-free sandbox command; Tier 2 sandbox-probe turn"),
        Check("claude", "changed --append-system-prompt on --resume", "UNCHECKED",
              "help documents --system-prompt-snapshot (default on); Tier 2 resume turn"),
    ]
    return checks


def tier1_codex(executable: str, root: Path) -> list[Check]:
    help_texts = {scope: _run([executable, *scope.split()[1:], "--help"]).stdout
                  for scope in ("codex", "codex exec", "codex exec resume")}
    scopes = {scope: parse_help(text) for scope, text in help_texts.items()}
    variants = argv_variants("codex", executable, root)
    checks = flag_checks("codex", variants, scopes)
    for scope, needed in (("codex", ("exec", "sandbox", "app-server", "features", "debug")), ("codex exec", ("resume",))):
        missing = [name for name in needed if name not in parse_subcommands(help_texts[scope])]
        checks.append(Check("codex", f"`{scope}` subcommands", "FAIL" if missing else "PASS",
                            f"missing: {', '.join(missing)}" if missing else ", ".join(needed)))
    env = _empty_codex_env(root / "empty-codex-home")
    features = parse_features(_run([executable, "features", "list"], env=env).stdout)
    names = [name for argv in variants.values() for name in disabled_features(argv)]
    checks += check_features("codex", names, features)
    for label, argv in variants.items():
        probe = argv[: argv.index("exec")] + ["-c", f"{PROBE_KEY}=1"] + argv[argv.index("exec"):]
        result = _run(probe, env=env, cwd=root, stdin="x", timeout=60)
        status, detail = dry_parse_status(result.returncode, result.stdout + result.stderr)
        keys = ", ".join(dict.fromkeys(config_keys(argv)))
        checks.append(Check("codex", f"strict dry parse: {label}", status, f"{detail}; keys: {keys}"))
    chat = variants["chat"]
    overrides = [token for flag, value in zip(chat, chat[1:]) if flag in ("--config", "--disable")
                 for token in (flag, value)]
    shown = _run([executable, "debug", "prompt-input", *overrides, "Hi"], env=env, cwd=root, timeout=60)
    checks.append(Check(
        "codex", "developer_instructions reach the prompt input",
        "PASS" if shown.returncode == 0 and MARKER in shown.stdout else "FAIL",
        "codex debug prompt-input" if shown.returncode == 0 else shown.stderr.strip()[:300],
    ))
    try:
        catalog = json.loads(_run([executable, "debug", "models"], env=env, timeout=60).stdout)
        checks.append(codex_effort_check(CodexAdapter.EFFORT_LEVELS, catalog))
    except ValueError:
        checks.append(Check("codex", "effort levels (model catalog)", "WARN", "`codex debug models` gave no JSON"))
    checks.append(Check("codex", "permissions profile fields", "UNCHECKED",
                        "strict config does not check them; the sandbox replay checks their effect"))
    checks += codex_sandbox_replay(executable, root)
    return checks


def _sandbox_tree(root: Path, spike: Any) -> Any:
    """A spike tree under tmp/ with a dummy Codex auth (the replay needs no login)."""

    base = root / f"sandbox-{secrets.token_hex(4)}"
    base.mkdir(parents=True)
    return spike._build_tree_in_root(base, "claude")


def codex_sandbox_replay(executable: str, root: Path) -> list[Check]:
    """S2's model-free replay: the build profile Delibra sends, under `codex sandbox`."""

    import spike.spike_build_sandbox as spike
    from spike.spike_build_sandbox_codex_replay import start_lan_listener

    lan_server, lan_hits = start_lan_listener()
    delibra, server_a = spike.start_listener()
    proxy, server_b = spike.start_listener()
    tree = None
    try:
        tree = _sandbox_tree(root, spike)
        commands = spike.probes(tree, delibra, proxy)
        base = spike.baseline(tree, commands)
        proxy_seen = bool(proxy.hits)
        for hits in (delibra.hits, proxy.hits, lan_hits):
            hits.clear()
        spike.write_script(tree.project, commands)
        context = spike.context(tree, "", spike.ALLOWLIST)
        built = CodexAdapter(executable).build_command(_session("codex"), context).argv
        argv = [executable]
        for flag, value in zip(built, built[1:]):
            if flag in ("--config", "--disable"):
                argv += [flag, value]
        argv += ["sandbox", "-P", "delibra_build", "-C", str(tree.project), "--", "sh", "spike-probes.sh"]
        started = time.monotonic()
        completed = _run(argv, env=spike.environment("codex", tree), cwd=tree.project, timeout=900)
        rows = spike.judge(tree, delibra, proxy, "codex", base, proxy_seen)
        for name in ("net_lan_ip", "net_lan_name"):
            if lan_hits and rows[name].startswith("PASS"):
                rows[name] = f"FAIL ({len(lan_hits)} request(s) reached the LAN listener)"
        rows["codex_sandbox_exit"] = (
            f"PASS (rc=0 in {time.monotonic() - started:.1f}s)" if completed.returncode == 0
            else f"FAIL (rc={completed.returncode}: {completed.stderr[-300:]!r})"
        )
    except Exception as exc:  # the table must survive any failure
        rows = {"sandbox_replay_error": f"INCONCLUSIVE ({type(exc).__name__}: {exc})"}
    finally:
        for server in (server_a, server_b, lan_server):
            server.shutdown()
        if tree is not None:
            shutil.rmtree(tree.root, ignore_errors=True)
    return [_spike_row("codex", f"sandbox {name}", value) for name, value in rows.items()]


def _spike_row(provider: str, name: str, value: str) -> Check:
    status, _, detail = value.partition(" ")
    return Check(provider, name, status if status in (*GATING, "RECORDED") else "INCONCLUSIVE",
                 detail.strip("()") if status in (*GATING, "RECORDED") else value)


# ------------------------------------------------------------------- Tier 2


def _turn_env(workspace: Path, *, codex_home: Path | None = None) -> dict[str, str]:
    """What RunManager._subprocess_environment gives a chat turn."""

    env = {name: os.environ[name] for name in ("PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM")
           if name in os.environ}
    (workspace / ".tmp").mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(workspace / ".tmp")
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


@dataclass
class Turn:
    events: list[AgentEvent]
    returncode: int
    stderr: str
    raw: list[dict[str, Any]]

    def first(self, kind: str) -> AgentEvent | None:
        return next((event for event in self.events if event.kind == kind), None)

    @property
    def session_id(self) -> str | None:
        init = self.first("init")
        return init.cli_session_id if init is not None else None

    @property
    def text(self) -> str:
        result = self.first("result")
        return result.text if result is not None else ""


def run_turn(adapter: ClaudeAdapter | CodexAdapter, session: SessionConfig, context: RunContext,
             env: dict[str, str], cwd: Path, log: Path) -> Turn:
    command = adapter.build_command(session, context)
    completed = _run(command.argv, env=env, cwd=cwd, stdin=command.stdin, timeout=600)
    log.write_text(completed.stdout + "\n--- stderr\n" + completed.stderr, encoding="utf-8")
    events: list[AgentEvent] = []
    raw: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        events += adapter.parse_line(line)
        try:
            raw.append(json.loads(line))
        except ValueError:
            continue
    return Turn(events, completed.returncode, completed.stderr, raw)


def _usage_checks(provider: str, label: str, turn: Turn) -> list[Check]:
    usage = turn.first("turn_usage")
    reported = usage is not None and usage.usage is not None and usage.usage.input_tokens is not None \
        and usage.usage.output_tokens is not None
    return [
        Check(provider, f"{label}: exit and result", "PASS" if turn.returncode == 0 and turn.text else "FAIL",
              f"rc={turn.returncode}; {turn.text[:80]!r}"),
        Check(provider, f"{label}: init session id", "PASS" if turn.session_id else "FAIL", turn.session_id or ""),
        Check(provider, f"{label}: stdout turn_usage", "PASS" if reported else "FAIL",
              str(usage.usage) if usage is not None else "none"),
    ]


def _resume_checks(provider: str, turn: Turn, first_id: str | None) -> list[Check]:
    text = turn.text
    return [
        Check(provider, "resume: same session", "PASS" if turn.session_id and turn.session_id == first_id else "FAIL",
              f"{first_id} -> {turn.session_id}"),
        Check(provider, "resume: remembers the earlier turn", "PASS" if "4817" in text else "FAIL", repr(text[:120])),
        Check(provider, "resume: changed system prompt applies",
              "PASS" if "TAG-BRAVO" in text and "TAG-ALPHA" not in text
              else "FAIL" if "TAG-ALPHA" in text else "INCONCLUSIVE", repr(text[:120])),
    ]


FIRST_ROLE = "If you are asked for the tag, the tag is TAG-ALPHA."
SECOND_ROLE = "If you are asked for the tag, the tag is TAG-BRAVO."
FIRST_PROMPT = "Remember the number 4817. Reply with only the word OK."
SECOND_PROMPT = "Reply with only the number I asked you to remember, then the tag, separated by a space."


def tier2_claude(executable: str, root: Path) -> list[Check]:
    checks = claude_sandbox_turn(executable, root)
    workspace = root / "claude-chat"
    workspace.mkdir(parents=True, exist_ok=True)
    adapter = ClaudeAdapter(executable)
    context = replace(_chat(root, prompt=FIRST_PROMPT), workspace=workspace, writable_roots=(), project_root=None)
    first = run_turn(adapter, _session("claude", model="haiku", role=FIRST_ROLE), context,
                     _turn_env(workspace), workspace, root / "claude-chat.jsonl")
    checks += _usage_checks("claude", "chat turn", first)
    window = first.first("context_usage")
    reading = window.context if window is not None else None
    checks.append(Check("claude", "chat turn: context_usage", "PASS"
                        if reading is not None and reading.used_tokens and reading.context_window else "FAIL",
                        str(reading)))
    resumed = run_turn(ClaudeAdapter(executable), _session("claude", model="haiku", role=SECOND_ROLE),
                       replace(context, user_prompt=SECOND_PROMPT, resume_id=first.session_id),
                       _turn_env(workspace), workspace, root / "claude-resume.jsonl")
    checks += _usage_checks("claude", "resume turn", resumed)
    return checks + _resume_checks("claude", resumed, first.session_id)


def claude_sandbox_turn(executable: str, root: Path) -> list[Check]:
    import spike.spike_build_sandbox as spike
    from spike.spike_build_sandbox_codex_replay import start_lan_listener

    lan_server, lan_hits = start_lan_listener()
    delibra, server_a = spike.start_listener()
    proxy, server_b = spike.start_listener()
    tree = None
    checks: list[Check] = []
    try:
        tree = _sandbox_tree(root, spike)
        commands = spike.probes(tree, delibra, proxy)
        base = spike.baseline(tree, commands)
        proxy_seen = bool(proxy.hits)
        for hits in (delibra.hits, proxy.hits, lan_hits):
            hits.clear()
        spike.write_script(tree.project, commands)
        adapter = ClaudeAdapter(executable)
        turn = run_turn(adapter, _session("claude", model="haiku", role="Follow the user's instructions exactly."),
                        spike.context(tree, spike.PROMPT, spike.ALLOWLIST), spike.environment("claude", tree),
                        tree.project, root / "claude-sandbox.jsonl")
        rows = spike.judge(tree, delibra, proxy, "claude", base, proxy_seen)
        if lan_hits and rows["net_lan_ip"].startswith("PASS"):
            rows["net_lan_ip"] = f"FAIL ({len(lan_hits)} request(s) reached the LAN listener)"
        checks += [_spike_row("claude", f"sandbox {name}", value) for name, value in rows.items()]
        init = next((event for event in turn.raw if event.get("type") == "system" and event.get("subtype") == "init"), {})
        expected = set(_flag_value(adapter.build_command(_session("claude"), spike.context(tree, "", ())).argv,
                                   "--tools").split(","))
        shown = set(init.get("tools") or [])
        checks.append(Check("claude", "--tools names (init event)", "PASS" if shown == expected else "FAIL",
                            f"init lists {sorted(shown)}"))
    except Exception as exc:  # the table must survive any failure
        checks.append(Check("claude", "sandbox turn", "INCONCLUSIVE", f"{type(exc).__name__}: {exc}"))
    finally:
        for server in (server_a, server_b, lan_server):
            server.shutdown()
        if tree is not None:
            shutil.rmtree(tree.root, ignore_errors=True)
    return checks


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


CODEX_EXEC_PROBES = ("write_project", "write_outside", "write_dot_git", "write_dot_delibra", "write_symlink_escape",
                     "read_sentinel", "read_symlink_escape", "read_codex_auth", "read_delibra_codex_auth",
                     "read_claude_config", "git_status", "env_python")


def tier2_codex(executable: str, root: Path) -> list[Check]:
    from app.codex_quota import read_codex_account_rate_limits
    from app.config import Settings
    from app.storage import read_codex_rollout_state

    settings = Settings.from_env()
    home = settings.codex_home
    if not (home / "auth.json").is_file():
        return [Check("codex", "Tier 2", "INCONCLUSIVE", "Delibra's Codex home has no auth.json")]
    checks = codex_sandbox_turn(executable, root, home)
    workspace = root / "codex-chat"
    workspace.mkdir(parents=True, exist_ok=True)
    context = replace(_chat(root, prompt=FIRST_PROMPT), workspace=workspace, writable_roots=(), project_root=None)

    def rollout(turn: Turn, since: datetime, label: str) -> list[Check]:
        state = read_codex_rollout_state(home, turn.session_id or "-", scan_limit=settings.codex_rollout_scan_limit,
                                         read_limit=settings.codex_rollout_read_limit, turn_since=since)
        usage = state.turn_usage
        return [Check("codex", f"{label}: rollout turn usage",
                      "PASS" if usage is not None and usage.reported and state.last_tokens else "FAIL",
                      f"rollout {usage}; last_tokens={state.last_tokens}; rate limits={len(state.rate_limits)}")]

    session = _session("codex", model="", role=FIRST_ROLE)
    since = datetime.now(UTC)
    first = run_turn(CodexAdapter(executable), session, context, _turn_env(workspace, codex_home=home), workspace,
                     root / "codex-chat.jsonl")
    checks += _usage_checks("codex", "chat turn", first) + rollout(first, since, "chat turn")
    since = datetime.now(UTC)
    resumed = run_turn(CodexAdapter(executable), replace(session, role_instructions=SECOND_ROLE),
                       replace(context, user_prompt=SECOND_PROMPT, resume_id=first.session_id),
                       _turn_env(workspace, codex_home=home), workspace, root / "codex-resume.jsonl")
    checks += _usage_checks("codex", "resume turn", resumed) + rollout(resumed, since, "resume turn")
    checks += _resume_checks("codex", resumed, first.session_id)
    readings = asyncio.run(read_codex_account_rate_limits(
        home, executable=executable, timeout_seconds=settings.codex_app_server_timeout_seconds))
    checks.append(Check("codex", "app-server account rate limits (no model call)",
                        "PASS" if readings else "INCONCLUSIVE", f"{len(readings)} window(s)"))
    return checks


def codex_sandbox_turn(executable: str, root: Path, home: Path) -> list[Check]:
    """Non-network probes only: `codex exec` kills the script at the first blocked host."""

    import spike.spike_build_sandbox as spike

    delibra, server_a = spike.start_listener()
    proxy, server_b = spike.start_listener()
    tree = None
    try:
        tree = _sandbox_tree(root, spike)
        commands = {name: command for name, command in spike.probes(tree, delibra, proxy).items()
                    if name in CODEX_EXEC_PROBES}
        spike.write_script(tree.project, commands)
        env = spike.environment("codex", tree)
        env["CODEX_HOME"] = str(home)
        run_turn(CodexAdapter(executable), _session("codex", model="", role="Follow the user's instructions exactly."),
                 spike.context(tree, spike.PROMPT, spike.ALLOWLIST), env, tree.project, root / "codex-sandbox.jsonl")
        base = {name: 0 for name in (*spike.BASELINE_PROBES, "net_allowed")}
        rows = {name: value for name, value in spike.judge(tree, delibra, proxy, "codex", base, True).items()
                if name in CODEX_EXEC_PROBES}
        return [_spike_row("codex", f"exec sandbox {name}", value) for name, value in rows.items()]
    except Exception as exc:  # the table must survive any failure
        return [Check("codex", "exec sandbox turn", "INCONCLUSIVE", f"{type(exc).__name__}: {exc}")]
    finally:
        for server in (server_a, server_b):
            server.shutdown()
        if tree is not None:
            shutil.rmtree(tree.root, ignore_errors=True)


# --------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tier", choices=("1", "2"), required=True)
    parser.add_argument("--provider", choices=("claude", "codex", "all"), default="all")
    args = parser.parse_args()
    providers = ("claude", "codex") if args.provider == "all" else (args.provider,)
    root = SCRATCH / f"tier{args.tier}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    root.mkdir(parents=True)
    checks: list[Check] = []
    versions = {}
    for provider in providers:
        executable = shutil.which(provider)
        if executable is None:
            checks.append(Check(provider, "installed", "FAIL", f"{provider} is not on PATH"))
            continue
        versions[provider] = _run([executable, "--version"]).stdout.strip().splitlines()[-1]
        runner = {("1", "claude"): tier1_claude, ("1", "codex"): tier1_codex,
                  ("2", "claude"): tier2_claude, ("2", "codex"): tier2_codex}[(args.tier, provider)]
        (root / provider).mkdir()
        checks += runner(executable, root / provider)
    summary = ", ".join(f"{p} {versions.get(p, '?')}: {verdict(checks, p)}" for p in providers)
    output = f"Tier {args.tier} ({summary})\n\n{render_table(checks)}\n"
    (root / "table.md").write_text(output, encoding="utf-8")
    sys.stdout.write(output)
    return 0 if all(verdict(checks, p) == "PASS" for p in providers) else 1


if __name__ == "__main__":
    raise SystemExit(main())

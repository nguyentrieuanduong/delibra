# M0 CLI findings

## M4 config-change resume capability gate

Executed 2026-07-17 against the local authenticated provider accounts. The
probe used disposable workspaces and, for Codex, a disposable copy of the local
ChatGPT OAuth credential. It retained no provider transcript. Both adapters
therefore encode `RESUME_AFTER_CONFIG_CHANGE = True`.

The final chat-level gate (`envs/bin/python -m spike.m4_chat_gate`) also passed on
2026-07-17. Claude A emitted four SSE events on each of its first and changed-config
turns, retained its native id under `sonnet`/`low` → `opus`/`medium`, and recalled
the canary. Codex B was added through the HX sidebar response, retained its native id,
and read Claude's staged canary on a three-event pass turn. While Claude A round 2
was complete and Codex B round 2 was live, the chat rendered each composite fragment
exactly once and the Codex done target remained scoped to its own pane. This was a
real CLI/HTTP/SSE execution with deterministic HTML inspection, not GUI automation.

### Claude Code

- Runtime/account: Claude Code `2.1.202`; local authenticated first-party
  Claude Code account. The managed command sandbox hid the macOS credential,
  so the real-provider probe ran with explicitly approved direct credential and
  network access.
- First turn: requested model `sonnet`, effort `low`; provider event metadata
  resolved the model to `claude-sonnet-5`. Native session id:
  `aa814cc1-a1b2-43d2-bc59-b212012b769c`.
- Resumed turn: requested model `opus`, effort `medium`; provider event metadata
  resolved the model to `claude-opus-4-8`. The successful zero-exit invocation
  accepted the changed effort flag and emitted the same native session id.
- Behavioral evidence: the resumed turn returned the exact first-turn canary
  `m4-claude-saffron-8472`; the new model metadata differed from the first turn.
- First command (prompt on stdin):
  `claude -p --output-format stream-json --verbose --include-partial-messages --model sonnet --effort low --append-system-prompt <role> --permission-mode dontAsk --tools Read,Write,Edit,WebSearch,WebFetch --allowedTools 'Read(/**),Edit(/**),WebSearch,WebFetch' --safe-mode --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands --no-chrome`.
- Resume command rebuilt all options with the new settings and appended:
  `--model opus --effort medium ... --resume aa814cc1-a1b2-43d2-bc59-b212012b769c`.

### Codex CLI

- Runtime/account: Codex CLI `0.144.5`; local authenticated ChatGPT OAuth
  account, exercised through an isolated `CODEX_HOME` containing only a
  mode-0600 disposable auth copy and provider-created rollout state. The real
  provider probe required explicitly approved direct network access because the
  managed sandbox's TLS interception produced `UnknownIssuer`.
- First turn: requested and rollout-recorded model `gpt-5.4`, effort `low`.
  Native thread id: `019f6eb9-d474-7ab0-9c02-80b9e0355edf`.
- Resumed turn: requested and rollout-recorded model `gpt-5.4-mini`, effort
  `medium`. Provider-authored `turn_context` metadata recorded both changed
  values under the same native thread id.
- Behavioral evidence: the resumed turn returned the exact first-turn canary
  `m4-codex-ember-3916`.
- First command (prompt on stdin):
  `codex --model gpt-5.4 --sandbox workspace-write --ask-for-approval never --search --cd <workspace> --config 'model_reasoning_effort="low"' --config project_root_markers=[] --config project_doc_max_bytes=0 --config sandbox_workspace_write.exclude_slash_tmp=true --config sandbox_workspace_write.exclude_tmpdir_env_var=false --config sandbox_workspace_write.network_access=false --config 'shell_environment_policy.inherit="all"' --disable hooks --disable plugins --disable apps --disable memories --disable goals --disable multi_agent exec --json --skip-git-repo-check --ignore-user-config --ignore-rules --strict-config -`.
- Resume command rebuilt all options with `--model gpt-5.4-mini` and
  `model_reasoning_effort="medium"`, then used
  `exec resume --json --skip-git-repo-check --ignore-user-config --ignore-rules --strict-config 019f6eb9-d474-7ab0-9c02-80b9e0355edf -`.

<!-- CLAUDE-SPIKE:START -->
## Claude Code

- Executable: `/opt/homebrew/bin/claude`
- Version: `2.1.202 (Claude Code)`
- Model/effort exercised: `sonnet` / `low`; help advertises `low`, `medium`, `high`, `xhigh`, `max`.
- Streaming class: token-level `text_delta`; first delta at 17.477s before terminal at 19.260s; resumed delta at 14.204s before terminal at 15.491s.
- Resume strategy: **native**. `--resume <session-id>` recalled the seeded codeword and retained the same native session id.
- Stdin: both prompts were supplied on stdin.
- R4 boundary: **strict workspace-only writes** with private `TMPDIR=<workspace>/.tmp`. On first and resumed turns, the workspace write succeeded; attempted writes to adjacent project storage, another session, shared `/tmp`, and a pre-seeded symlink to outside were rejected. Exact writable root: `<workspace>`.
- Rejected candidate: `--permission-mode acceptEdits` with a bare `Write` allow rule wrote outside the workspace during the disposable probe. Production must retain `dontAsk` plus path-scoped `Edit(/**)`.
- Staged input: `workspace/inputs/source.md` was read on both turns (canary observed in the actual final text).
- Role instructions: required response prefix observed on both turns.
- Web: a `WebSearch` tool-use event was observed on both turns; Bash was absent from the allowed tool set.
- Ambient isolation: project and HOME `CLAUDE.md` sentinels plus project hook, skill, and MCP sentinels were behaviorally inert. `--safe-mode`, empty setting sources, strict empty MCP config, disabled slash commands, and no Chrome were active.
- Environment: authentication succeeded with only `PATH`, `HOME`, `USER`, `SHELL`, `LANG`, `LC_ALL`, `TERM`, and private `TMPDIR` when present.
- Prepared fallback: `spike/fixtures/claude_stateless_prompt.txt` records one bounded stateless-history prompt shape.
- Exit/timing: first rc=0, 19.791s; resume rc=0, 16.021s.

Proven first-turn command (prompt on stdin; placeholders are app values):

```text
claude -p --output-format stream-json --verbose --include-partial-messages --model <model> --effort <effort> --append-system-prompt <role> --permission-mode dontAsk --tools Read,Write,Edit,WebSearch,WebFetch --allowedTools 'Read(/**),Edit(/**),WebSearch,WebFetch' --safe-mode --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands --no-chrome
```

Resume appends `--resume <cli-session-id>` to the same command, retaining cwd and environment.
<!-- CLAUDE-SPIKE:END -->

<!-- CODEX-SPIKE:START -->
## Codex CLI

- Executable: `/opt/homebrew/bin/codex`
- Version: `codex-cli 0.144.5`
- Model/effort exercised: `gpt-5.4` / `low`; installed config accepts `minimal`, `low`, `medium`, `high`, `xhigh`.
- JSONL schema: `thread.started`, `turn.started`, `item.started`, `item.completed`, `turn.completed`; the invalid-model fixture contains both `error` and `turn.failed`.
- Streaming class: discrete provider-native `command_execution`, `web_search`, and completed `agent_message` items. Interim agent messages arrive as whole progress messages; the answer arrives as one final agent message, with no token/text deltas on 0.144.5. First progress at 8.214s before terminal at 18.976s; resumed progress at 5.428s before terminal at 13.455s.
- Resume strategy: **native**. `exec resume <thread-id>` recalled the codeword and emitted the same thread id.
- Stdin/non-Git: both prompts used stdin (`-`), and `--skip-git-repo-check` succeeded in a non-Git workspace.
- R4 boundary: **strict workspace-only writes**; shared `/tmp` was rejected. On both turns the workspace and private `$TMPDIR` policy remained active; adjacent project storage, another session, isolated provider state, and writes through agent-created symlinks were rejected. The exact agent-writable root is `<workspace>`; Codex itself writes provider-controlled resume/auth state under `<CODEX_HOME>`.
- Staged input: `workspace/inputs/source.md` was read on both turns (canary observed in actual final text).
- Role instructions: the round-1 role block persisted on the resumed turn.
- Web/network: a completed native `web_search` item was observed on both turns while shell `curl` failed under `sandbox_workspace_write.network_access=false`.
- Ambient isolation: normal-HOME global and parent-project `AGENTS.md`, plus normal-HOME config/MCP/hook/skill sentinels, were inert with a clean app-owned `CODEX_HOME`, `project_root_markers=[]`, `project_doc_max_bytes=0`, `--ignore-user-config`, `--ignore-rules`, and disabled hooks/plugins/apps/memories/goals/multi-agent features. A sentinel placed inside the active `CODEX_HOME` was loaded despite `project_doc_max_bytes=0`, proving that the clean dedicated home is required rather than optional.
- Approvals: `--ask-for-approval never`; denied operations returned to the model and the process exited without waiting for input.
- Environment: authentication succeeded from a disposable auth copy with only `PATH`, `HOME`, `USER`, `SHELL`, `LANG`, `LC_ALL`, `TERM`, private `TMPDIR`, and isolated `CODEX_HOME` when present.
- Prepared fallback: `spike/fixtures/codex_stateless_prompt.txt` records one bounded stateless-history prompt shape.
- Exit/timing: first rc=0, 19.984s; resume rc=0, 14.004s; invalid-model rc=1, 1.856s.

Proven first-turn command (prompt on stdin; placeholders are app values):

```text
codex --model <model> --sandbox workspace-write --ask-for-approval never --search --cd <workspace> --config model_reasoning_effort=<effort> --config project_root_markers=[] --config project_doc_max_bytes=0 --config sandbox_workspace_write.exclude_slash_tmp=true --config sandbox_workspace_write.exclude_tmpdir_env_var=false --config sandbox_workspace_write.network_access=false --config shell_environment_policy.inherit=all --disable hooks --disable plugins --disable apps --disable memories --disable goals --disable multi_agent exec --json --skip-git-repo-check --ignore-user-config --ignore-rules --strict-config -
```

Resume uses the same global policy options followed by `exec resume --json --skip-git-repo-check --ignore-user-config --ignore-rules --strict-config <thread-id> -`.
<!-- CODEX-SPIKE:END -->

<!-- M1-HTTP-GATE:START -->
## M1 real-provider HTTP/SSE gate

- Date: 2026-07-17. The workspace had no Chromium/Playwright/Selenium runtime, so
  `spike/m1_http_gate.py` exercised the same contract against a real Uvicorn socket
  rather than claiming GUI automation.
- Claude: real `sonnet`/`low` round completed; the first provider-native progress
  event arrived at 5.994s, before completion. After a forced SSE disconnect,
  reconnect with `Last-Event-ID` delivered three strictly newer events ending in one
  `done`; the final 51-byte Markdown file was durable and rendered by the round
  endpoint.
- Codex: real `gpt-5.4`/`low` round completed; the first provider-native progress
  event arrived at 14.276s, before completion. After a forced SSE disconnect,
  reconnect delivered five strictly newer events ending in one `done`; the final
  34-byte Markdown file was durable and rendered by the round endpoint.
- Both runs rejected a concurrent second POST with 409, returned immediate `done`
  to a late subscriber, and exposed the exact one-shot HTMX final replacement
  attributes. T9's fake-provider route test verifies the live markup and complete
  replay/reset/cancel cases deterministically.
<!-- M1-HTTP-GATE:END -->

<!-- M2-RESUME-GATE:START -->
## M2 real-provider native-resume gate

- Date: 2026-07-17. `spike/m1_http_gate.py` was extended to execute a second
  HTTP round for each provider after deleting `gate.txt`, so recall could not come
  from rereading the workspace file.
- Claude: round 2 completed, recalled `CLAUDE_GATE_CANARY`, preserved the exact
  native session id, emitted four SSE events, and persisted an 18-byte result.
- Codex: round 2 completed, recalled `CODEX_GATE_CANARY`, preserved the exact
  native thread id, emitted its terminal SSE event, and persisted a 19-byte result.
- Immediately preceding first turns also re-passed pre-completion progress,
  disconnect/reconnect, one `done`, and durable rendering for both providers.
<!-- M2-RESUME-GATE:END -->

<!-- FINAL-PARITY-GATE:START -->
## Final real-provider parity gate

- Date: 2026-07-17. The full `spike/m1_http_gate.py` flow re-passed real first
  turns and native resumes, then executed both cross-provider pass directions.
- Claude target (source: Codex): the pass round completed with 17 SSE events,
  emitted native web-search progress, read `CODEX_GATE_CANARY`, retained its native
  session id, and performed the requested workspace write. Attempts to modify the
  source output, the other session, shared temp, and an agent-visible symlink target
  all failed. The source bytes and runner-computed SHA-256 remained identical.
- Codex target (source: Claude): the pass round completed with six SSE events,
  emitted native web-search progress, read `CLAUDE_GATE_CANARY`, retained its native
  thread id, and performed the requested workspace write. The same source,
  other-session, shared-temp, and symlink-boundary writes failed; source bytes and
  provenance hash remained identical.
- First-turn pre-completion activity arrived at 3.031s for Claude and 8.192s for
  Codex in this run. Both disconnect/reconnect paths remained duplicate-free and
  completed with durable output.
- `spike/long_sse_gate.py` separately ran for 61.048s: it disconnected after the
  first delta, reconnected with `Last-Event-ID`, received three strictly newer
  events and exactly one `done`, and persisted a complete result.
<!-- FINAL-PARITY-GATE:END -->

<!-- ADD-DIR-SPIKE:START -->
## Shared agent directories (`--add-dir`)

Produced by `spike/spike_add_dir.py` for Task 9.1. Not part of pytest: it drives
both real CLIs and spends both subscriptions. Argv is built by calling
`ClaudeAdapter.build_command` / `CodexAdapter.build_command` and splicing the
grant flags in, so it describes Delibra's pinned command rather than a subset.

**Verdict: STOP -- claude claude-fresh-variadic: granted directory grant_a was not writable; --add-dir did not widen the sandbox**

### Installed

- claude: `2.1.202 (Claude Code)`
- codex: `codex-cli 0.153.4`

### Argv form that accumulates

- _undetermined_

- claude:claude-fresh: option after `--add-dir` parsed
- claude:claude-fresh-variadic: option after `--add-dir` parsed

### Writes observed on the filesystem

`grant_a`/`grant_b` are granted on the fresh turn, `grant_c` is not; the resume
turn grants only `grant_c`. `grant_c` on the fresh turn is the control: without
it, "A and B are writable" would also be true of a sandbox confining nothing.

| Turn | grant_a | grant_b | grant_c | delibra_sentinel | outside_sentinel | symlink_delibra | symlink_outside |
|---|---|---|---|---|---|---|---|
| `claude:claude-fresh` | denied | denied | denied | denied | denied | denied | denied |
| `claude:claude-fresh-variadic` | denied | denied | denied | denied | denied | denied | denied |

### Native resume

- _no resume was reached_

Retention of the *removed* grants across the resume is recorded, not asserted --
9.5 drops the native session id on any grant change, so Delibra revokes by
construction:

- _no resume was reached_

### Protected sentinels

Attacked directly by absolute path and through a symlink nested inside grant A.

- after claude:claude-fresh: sentinels byte-identical
- after claude:claude-fresh-variadic: sentinels byte-identical

### Ambient instructions and provider configuration in added directories

Each of A, B and C carries a hostile `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`
hook, `.claude/skills/*/SKILL.md`, `.mcp.json`, and `.codex/config.toml` + `hooks.json`,
each naming a unique token and a unique leak file. Inert means no token in the
event stream or stderr and no leak file anywhere in the tree.

- after claude:claude-fresh: added-directory instructions inert
- after claude:claude-fresh-variadic: added-directory instructions inert

### Sanitized argv

_No argv reached execution._

### Notes

- repeated `--add-dir` did not make every granted directory writable: claude claude-fresh: granted directory grant_a was not writable; --add-dir did not widen the sandbox. Retried with the variadic form.
<!-- ADD-DIR-SPIKE:END -->

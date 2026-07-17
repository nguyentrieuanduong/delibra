# Delibra MVP acceptance

Date: 2026-07-17  
Scope: requirements FR1–FR11 and plan M0–M3  
Runtime: Python 3.12.13, Claude Code 2.1.202, Codex CLI 0.144.5

## Result and evidence boundary

The deterministic suite passed 84 tests. One non-failing warning remains from
Starlette's deprecated TestClient/httpx compatibility import; it is not an
application-runtime failure.

Real-provider evidence comes from `spike/FINDINGS.md` and the executable
`spike/m1_http_gate.py`. No Chromium, Playwright, or Selenium runtime was installed,
so browser behavior was exercised at the real Uvicorn HTTP/SSE boundary and the exact
HTMX DOM contract was verified deterministically in `tests/test_routes_runs.py`. This
record does not claim GUI automation that did not occur.

## FR1–FR11

| Requirement | Accepted evidence |
|---|---|
| FR1 Project CRUD | `tests/test_routes_projects.py`: absolute-path validation, canonical symlinks, resolved-path uniqueness, manifest import/rejection, rename/unregister locking, and unregister-without-delete. |
| FR2 Session CRUD | `tests/test_routes_sessions.py`: provider-specific effort validation, all-field edits before round 1, name-only edits afterward, running-delete rejection, and owned identity checks. |
| FR3 Headless runs | Adapter golden tests pin the verified Claude stream-JSON and Codex JSONL commands; real first/resume/pass rounds completed for both providers. |
| FR4 Live streaming | T7/T9 cover monotonic IDs, replay gaps, reset/snapshot, late subscribers, and cancellation. Real Claude/Codex runs emitted honest pre-completion activity. The long gate ran 61.048s across disconnect/reconnect without duplicate IDs. |
| FR5 Markdown persistence | Runner and route integration tests verify prompt, partial, final, atomic metadata, and recovery files. All real parity rounds persisted nonempty `.md` outputs. |
| FR6 Markdown rendering | `tests/test_markdown.py` and app skeleton tests verify server rendering with raw HTML, event handlers, and unsafe links neutralized. |
| FR7 Pass to | `tests/test_runner_pass.py` and `tests/test_routes_pass.py` verify fixed-path staging, exact prompt, SHA-256 provenance, TOCTOU failures, one-winner concurrency, immutable source, and per-round UI. Both real cross-provider directions completed. |
| FR8 Full history | Reply/history tests render prompts, output, warnings, errors, cancellation, provenance, interrupted partials, and orphan round files. |
| FR9 Session metadata | Storage and runner tests verify native IDs, timestamps/status, round lists, provider/model/effort snapshots, backups, and startup reconciliation. |
| FR10 Visible failures | Malformed JSONL, provider error at exit 0, empty final, nonzero exit with stderr tail, timeout, persistence failure, and restart interruption become visible error rounds with partials where available. |
| FR11 Cancellation | Route and runner tests verify cancel, cancel/exit race, process-group termination, terminal persistence, one `done`, and no stuck running status. |

## Provider parity

### Claude Code

- First prompt: real `sonnet`/`low` run emitted provider-native progress before
  completion, survived forced SSE reconnect with strictly newer IDs, persisted, and
  rendered. The original M0 probe separately observed token-level deltas.
- Reply: native `--resume` retained the exact Claude session ID and recalled
  `CLAUDE_GATE_CANARY` after the source file was removed.
- Role: the M0 first and resumed turns both honored the required role prefix.
- Passed input: as a real target, Claude read the Codex round's staged
  `CODEX_GATE_CANARY` and completed the pass round.
- Write boundary: the pass turn's workspace write succeeded; source-output,
  other-session, shared-temp, and symlink-target writes failed. The source bytes and
  recorded hash remained identical. M0 established the same boundary on first and
  resumed turns.
- Web: native WebSearch progress was observed on first, resume, and pass probes;
  Claude had no Bash tool in the verified command.
- Provider parsing: Claude adapter fixtures cover malformed JSON and exit-zero
  provider error classification. The shared runner covers nonzero exit, timeout,
  cancellation, partial persistence, and visible terminal state without a
  provider-specific waiver.
- Long/recovery behavior: shared replay and startup-reconciliation code is exercised
  independently of provider; the 61.048-second reconnect gate and interrupted-round
  UI tests apply to Claude runs through the same RunManager path.

### Codex CLI

- First prompt: real `gpt-5.4`/`low` run emitted provider-native progress before
  completion, survived forced SSE reconnect with strictly newer IDs, persisted, and
  rendered. Codex 0.144.5 honestly emits progress plus a final message rather than
  token deltas in the observed schema.
- Reply: native `exec resume` retained the exact Codex thread ID and recalled
  `CODEX_GATE_CANARY` after the source file was removed.
- Role: the M0 first and resumed turns both honored the required role prefix/block.
- Passed input: as a real target, Codex read the Claude round's staged
  `CLAUDE_GATE_CANARY` and completed the pass round.
- Write boundary: the pass turn's workspace write succeeded; source-output,
  other-session, shared-temp, and symlink-target writes failed. Source bytes and
  provenance hash remained identical. M0 proved the same on first/resumed turns.
- Web: native web-search events were observed while a shell `curl` failed under
  `sandbox_workspace_write.network_access=false`.
- Provider parsing: Codex adapter fixtures cover `error`, `turn.failed`, malformed
  JSON, cumulative text, and progress mapping. The same provider-neutral runner
  supplies nonzero/timeout/cancel/partial/UI behavior, with no Codex waiver.
- Long/recovery behavior: Codex uses the same tested replay buffer, event IDs,
  process-group lifecycle, and startup reconciliation as Claude.

## Failure and recovery matrix

| Case | Evidence and result |
|---|---|
| Nonzero exit | Runner records error plus bounded stderr tail and captured partial. |
| Malformed JSONL | Both adapters classify malformed input; runner preserves partial and finalizes error. |
| Exit-zero provider error | Provider error event outranks exit code and remains visible. |
| Empty final | Exit 0 without valid final text is an error, never a silent success. |
| Timeout | Process group receives TERM then KILL if required; error round persists. |
| Cancel | Concurrent cancel/exit finalizes exactly once and leaves session idle. |
| SSE disconnect >60s | 61.048-second gate replayed three newer events, no duplicate IDs, one `done`, complete output. |
| Restart mid-run | Partial is promoted, error says `interrupted by restart`, page renders it, and the next round can run. |
| Final/metadata write failure | Partial remains until both writes are durable; surfaced/logged error is best effort under total storage loss. |

## Security checks

- Unsafe Markdown: scripts, raw HTML handlers, and `javascript:` links are
  neutralized; SSE provider text is escaped.
- Request boundary: non-loopback Host and any non-matching mutation Origin (including
  another loopback port or alias) are rejected; body size is capped while receiving
  chunked requests.
- IDs and paths: invalid pass IDs are rejected; registry paths must be absolute and
  canonical; round/source paths are constructed only from validated IDs and numeric
  round numbers.
- Filesystem ownership: no-follow safe copy detects vanished/replaced/symlink sources;
  symlinked session roots, session directories, staging roots, private temp roots, and
  Codex credentials are rejected; manifest identity is checked before directory
  removal.
- Agent environment: a dedicated test proves arbitrary server secrets/API keys are
  absent from the subprocess allowlist.
- Process lifecycle: cancel, timeout, spawned-child cancellation, and graceful
  shutdown tests reap child process groups.
- Provider boundary: both real target providers rejected source, adjacent-session,
  shared-temp, and symlink writes while permitting their own workspace write.

## Runtime health and residual risk

The verified runtime versions are Claude Code 2.1.202 and Codex CLI 0.144.5. Startup
health probes show a UI warning for a missing executable or version drift rather than
raising a generic page error. Authentication failures become provider error rounds.

The adopted boundary is strict workspace-only agent writes plus app-owned Codex
provider state. It does not provide read confidentiality from the local OS user.
Exactly one Uvicorn worker is supported. Graceful shutdown is verified; a hard kill
may orphan CLI processes because the MVP intentionally has no external watchdog.

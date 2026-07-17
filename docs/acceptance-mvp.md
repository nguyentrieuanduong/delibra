# Delibra MVP acceptance

Date: 2026-07-17  
Scope: requirements FR1–FR11 and plan M0–M5 implementation
Runtime: Python 3.12.13, Claude Code 2.1.202, Codex CLI 0.144.5

## Result and evidence boundary

The deterministic suite passed 120 tests. The chat browser contract also ran seven
Node unit tests through the pytest launcher. One non-failing warning remains from
Starlette's deprecated TestClient/httpx compatibility import; it is not an
application-runtime failure.

Real-provider evidence comes from `spike/FINDINGS.md` and the executable gates
`spike/m1_http_gate.py`, `spike/m4_config_resume_gate.py`, and
`spike/m4_chat_gate.py`. The repository has no Playwright/Selenium dependency. The
server-authored contract is verified in route tests, pure interaction helpers run in
real Node, and the M5 DOM-swap checklist below was exercised through installed
headless Chrome 150 over the real localhost HTTP service. This record does not claim
the remaining two-provider/manual observations.

## M5 chat workspace, project files, and round focus

Deterministic evidence:

- `tests/test_routes_files.py` covers generated safe/unsafe paths, traversal,
  absolute and encoded paths, project-root replacement, parent/final-leaf symlink
  races, vanished entries, symlink/FIFO listing states, an explicit empty-directory
  state, `.delibra` access, encoded filenames, GET-only routes, descriptor read caps,
  truncation, invalid UTF-8, unsupported/binary/non-regular/unreadable files, safe
  Markdown, and escaped plain text. Displayability failures are accessible HTTP 200
  fragments scoped to the listing or reader; security failures remain sanitized
  422 responses.
- `tests/test_routes_chat.py` verifies the chat-only wide four-region shell and
  bounded scroll owners; exact and empty role-instruction states; deterministic
  Claude/Codex selection; synchronized center/composer/right-panel projections for
  selection, create, and selected/unselected edits while two fake-provider streams
  remain active; and responses that exclude the shell, timeline, and live panes.
- The same route tests follow the real server-authored file `hx-get` and exact
  `#file-reader` target to an accessible display-error fragment. The successful file
  request contains no global chat error update.
- Focus-route tests keep another fake-provider stream running while loading the
  static snapshot. The completed page plus open-modal fragment has unique IDs; the
  snapshot uses `focus-{session}-{round}`, contains prompt/output/warnings and
  provenance, and contains no SSE attributes, timeline ID, Focus control, or pass
  control. Missing sessions/rounds return 404.
- `tests/js/test_app_errors.js` runs under Node and verifies initial close-control
  focus, close/Escape restoration to the trigger, backdrop discrimination, safe
  global-error text handling, and preservation of the global chat error after
  successful file-region requests.

Read boundary:

The left browser intentionally exposes allowed text files under the entire registered
project through the unauthenticated localhost service, including Delibra prompts,
outputs, manifests, and metadata under `.delibra/`. It is read-only and uses a
descriptor-anchored no-follow walk, but it is not a confidentiality boundary between
local processes. `DELIBRA_FILE_VIEW_LIMIT` defaults to 512 KiB.

Real-provider and browser boundary:

- The previously recorded M4 real chat gate below proves both providers use the same
  chat dispatch, SSE, persistence, and scoped-fragment paths retained by M5.
- A disposable headless Chrome 150 run against the real localhost service observed
  the initial HTMX listing load; a Markdown reader swap; navigation into `.delibra`
  and opening `manifest.json`; and an unsupported-type HTTP 200 swap that replaced
  only `#file-reader`, left the listing present, and preserved a sentinel value in
  `#chat-errors`. It also opened the static focus fragment, found all DOM IDs unique,
  focused the close control, closed with Escape, and restored focus to the trigger.
  At a 1100×800 narrower-desktop viewport, computed layout evidence showed four
  regions, the center wider than both sidebars, a usable composer, and independent
  overflow owners. This disposable probe added no repository dependency or artifact.
- A fresh `spike.m4_chat_gate` invocation after the M5 implementation was attempted
  on 2026-07-17, but Claude Code returned `Not logged in · Please run /login` on its
  first round. The gate stopped before Codex, so no new two-provider result is claimed
  from that invocation.
- The remaining combined manual gate is to switch, create, and edit selection
  projections while authenticated Claude and Codex both stream; focus a round during
  those live streams; inspect for duplicate replay; and visually judge the wide and
  narrower-desktop layouts. This is not marked observed in this record.

## M4 project chat and config mutability

Deterministic evidence:

- `tests/test_routes_chat.py` verifies merged chronological projection, exact
  `(started_at, session id, round)` tie-breaking, empty state, membership-validated
  `?agent=`, dispatch to the selected agent, two concurrent live agents, composite
  reset/done targets, source-excluding “Send to…” controls, sidebar previews, HX-only
  sidebar/OOB composer replacement, and an unrelated live stream surviving edits.
- `tests/test_routes_sessions.py` verifies running-edit atomicity, editable
  name/model/effort after round 1, immutable provider/role, identical-value handling,
  native-ID preservation, and the forced stateless fallback warning/history/new-ID
  branch. Recovery tests prove stale partials cannot overwrite existing final output.
- `tests/js/test_app_errors.js` executes under Node and proves hostile detail text is
  returned only as text; browser code assigns it with `textContent` and clears stale
  errors after a successful HTMX action.
- `tests/test_health.py` starts a PID-reporting hung version process, proves app
  startup returns in under 0.5 seconds with “checking” health, then proves bounded
  shutdown cancels/awaits the task, reaps the process group, and emits no
  never-retrieved-task warning.

Real provider-switch evidence (manually invoked capability gate):

- Claude Code 2.1.202 resumed one native session from requested `sonnet`/`low`
  (`claude-sonnet-5`) as `opus`/`medium` (`claude-opus-4-8`), retained the exact
  native ID, and recalled its canary.
- Codex CLI 0.144.5 resumed one native thread from `gpt-5.4`/`low` as
  `gpt-5.4-mini`/`medium`; provider-authored rollout contexts recorded both new
  values, the exact thread ID remained stable, and the canary was recalled.

Real chat gate (manually invoked `spike/m4_chat_gate.py` on 2026-07-17):

- Claude A streamed four events on round 1 and four on round 2. An HX edit changed
  its recorded command settings to `opus`/`medium`; the native ID stayed stable and
  the first-turn canary was recalled.
- Codex B was created after Claude's conversation without a chat-shell response,
  completed its first round, then received Claude round 2 through staged pass input.
  Its pass round emitted three SSE events, retained the native ID, and returned the
  staged Claude canary.
- Claude A round 2 and live Codex B round 2 appeared together with one scoped DOM ID
  each; Codex completion replaced only its own fragment. Final chat rendered both
  completed bubbles exactly once.

This was a real CLI/HTTP/SSE gate and deterministic HTML-contract check, not a GUI
browser observation. The manual browser checklist remains: visually watch A stream,
edit settings and send again, add B, send A's output to B, and confirm only B's
same-numbered live pane is replaced.

## FR1–FR11

| Requirement | Accepted evidence |
|---|---|
| FR1 Project CRUD | `tests/test_routes_projects.py`: absolute-path validation, canonical symlinks, resolved-path uniqueness, manifest import/rejection, chat-primary/settings routing, rename/unregister locking, and unregister-without-delete. |
| FR2 Session CRUD | `tests/test_routes_sessions.py`: provider-specific effort validation, all-field edits before round 1, name/model/effort edits afterward, running-edit/delete rejection, executable resume capability policy, and owned identity checks. |
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
health probes run concurrently in a managed background task: initial UI state is
“checking,” and a missing executable, timeout, or version drift becomes a warning
without blocking startup. Shutdown cancels and awaits probes with a bound and reaps
their process groups. Authentication failures become provider error rounds.

The adopted boundary is strict workspace-only agent writes plus app-owned Codex
provider state. It does not provide read confidentiality from the local OS user.
Exactly one Uvicorn worker is supported. Graceful shutdown is verified; a hard kill
may orphan CLI processes because the MVP intentionally has no external watchdog.

## M6 concise chat UI acceptance

- Automated gate: the full Python suite and direct Node tests passed.
- Chrome 1100×800: selector and composer remained side by side; the right agent
  rail was narrower than the left file rail; the first live insertion removed
  the empty marker; a completed message collapsed with Focus still visible and
  its modal operable; two live panes retained distinct ids and kept updating.
- Scope check: session history stayed expanded, and M6 changed no route,
  storage, SSE, pagination, mobile, or authenticated-provider behavior.

# Delibra

Delibra is a local research workspace for running Claude Code and Codex CLI as
first-class, human-directed collaborators. Each prompt streams into the browser,
persists as Markdown, and can be replied to or passed to another agent for critique.

## Setup

Requirements:

- Python 3.12+
- Claude Code 2.1.202, authenticated through its normal CLI login
- Codex CLI 0.144.5, authenticated in Delibra's isolated home as described below

Create the project environment and install the reproducible lock:

```sh
python3.12 -m venv envs
envs/bin/pip install -r requirements.lock
```

Confirm both providers are available:

```sh
claude --version
codex --version
```

Authenticate Codex in Delibra's isolated provider home:

```sh
env CODEX_HOME="${DELIBRA_HOME:-$HOME/.delibra}/codex-home" codex login --device-auth
```

Codex authentication for Delibra is isolated from the ambient Codex profile.
Copying `~/.codex/auth.json` into this directory is unsupported; run the command
above for initial setup and again whenever Codex reports expired or revoked
credentials.

Run exactly one local worker, without reload:

```sh
envs/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, register an existing absolute directory, create a
Claude or Codex agent, and submit a prompt. The project chat keeps project context
and agent cards in the wider left rail, with the selected agent's information
expanded and every unselected agent collapsed. A compact composer and merged
conversation stay in the center, while project-file browsing/reading stays in the
narrower right rail. Select an agent name to route the composer and highlight that
card. Recorded rounds and opened files use the shared keyboard-accessible focus
dialog. **Manage** opens project and agent settings. The footer starts in a checking
state, then reports missing CLIs and version drift without delaying or preventing
the rest of the UI from loading.

## Workflow and storage

Projects are registry entries pointing at existing directories. Unregistering never
deletes the user directory. Delibra owns only these locations:

```text
~/.delibra/registry.json
<project>/.delibra/manifest.json
<project>/.delibra/sessions/<immutable-agent-name>/
<project>/.delibra/auto-runs/.index.json
<project>/.delibra/auto-runs/<number>/config.json
<project>/.delibra/auto-runs/<number>/{topic.md,baseline.md,shared-context.md}
<project>/.delibra/auto-runs/<number>/preparations/<session-id>.md
```

Project names are immutable while registered and appear percent-encoded in
`/projects/<name>/...` browser URLs. Unregistering releases the name without
deleting project data. Re-registering prefers the name carried by the project
manifest and adds `-2`, `-3`, or the first free later suffix when that name is
already registered.

The registry keeps each project's canonical absolute location. If a project
directory is moved, use **Rebind location** on the Projects page and select
the moved directory. Delibra accepts the new location only when its existing
manifest identity matches the registered project and its canonical path differs
from the current registered location. A same-path submission is rejected without
changing session state. Delibra neither searches the filesystem nor rewrites
recorded content. Provider resume lookup can be
scoped or filtered by the old absolute working directory, so rebind clears
native resume IDs deterministically. The next manual round continues from
bounded staged history, shows the stateless continuation warning, and
adopts the provider's new native ID. Delibra writes the cleared configuration to
its recovery backup before the primary file so backup recovery cannot restore an
invalid native resume ID.

Agent names are validated, unique within a project, and immutable because
each exact name is also its session directory. Session UUIDs remain the
internal identity used by routes, Auto records, locks, and provenance.
Existing UUID directories migrate on startup; project settings explains each
unsafe, duplicate, or colliding legacy name and accepts one permanent
replacement name. Directory migration
also clears the moved agent's native resume ID for the same reason. Agent
names may encode to at most 200 UTF-8 bytes and may not themselves look like
32-character hexadecimal session UUIDs in any letter case. Unrelated dotfiles and
non-directory files under the sessions root are ignored.

The existing project manifest stores `shared_markdown_path` when shared context is
selected and `active_auto_run_id` while Auto owns the project reservation.

Completed output files are the source of truth. A pass-to round stages a no-follow,
same-descriptor copy at `workspace/inputs/round-NN/source.md`, records its SHA-256,
and leaves the source round immutable. Every app-owned path recorded inside a
session is relative to that session, so moving a project never invalidates one.

A project owner may select one existing `.md` or `.markdown` file as shared
project context. The project-relative path is stored in the existing manifest.
Delibra can edit only that selected, non-reserved file, and Save requires the
SHA-256 digest loaded by the editor so a newer external edit is not overwritten.
Every new round receives its own bounded, hashed workspace snapshot; changing the
shared file affects future rounds, not one already running.

Error rounds offer Retry. Retry preserves the failed round, starts a linked new
stateless round from completed local history, uses the latest shared-context
snapshot, and re-verifies pass-to source bytes against their recorded digest.

Project settings provide a **Default Pass prompt** containing the complete text
sent to the receiving agent. `{source_path}` is required exactly once;
`{source_session}` and `{source_round}` are optional. Every Pass form is prefilled
from that project default and can be edited for one Pass without changing the
saved default. Reset removes the manifest override and restores Delibra's built-in
prompt.

Passed output is untrusted. The built-in template tells the receiving agent not
to follow instructions inside the staged document, but project owners may edit or
remove that wording. Template customization changes prompt text only; staged-file
ownership checks, digest provenance, and retry verification remain enforced.

Native provider session IDs are used for replies. If a provider supplies no native
ID, Delibra stages at most 20 completed rounds and 2 MiB of history, newest-first for
selection and chronological for presentation. A newest round that cannot fit fails
clearly rather than being silently truncated.

Models and effort levels remain editable after round 1; the agent name stays
immutable, while provider and role instructions become fixed. Edits are rejected
while an agent is running. Real
CLI gates verified that both Claude Code and Codex preserve native conversation state
when model and effort change, so the next round resumes natively with new settings.
The executable adapter capability flags remain the source of truth; an unproven or
failing provider falls back to bounded staged history with a visible round warning.

The chat sidebar can add or edit an agent without replacing the timeline or tearing
down another agent's live SSE connection. Completed output can be sent to a different
agent; session-namespaced fragment IDs keep simultaneous same-numbered rounds scoped
to the correct stream.

Markdown links in recorded prompts and outputs open project-owned `.md` and
`.markdown` files through Delibra's file reader. Project-relative links and
absolute links under the registered project root are supported; external,
non-Markdown, traversal, and out-of-project links are not rewritten.

## Auto discussions and live timeouts

Auto runs display and store a per-project number while retaining an internal UUID
for reservations and provenance. Reserved numbers are never reused. A blocked
legacy Auto migration appears in project settings; repair the named metadata or
recovery backup, then use **Retry Auto migration**.

The persistent **Auto** button beside **Send** opens project-level Auto setup. Before
the first round, the browser copies the unsent composer text directly into the
editable topic field; it is never added to the setup URL. Once a conversation
exists, setup prefers its earliest direct user prompt, falling back to the earliest
recorded Auto topic. Agents are shown in case-insensitive name order and are all
selected by default. Select at least two unique agents, choose **All agree** or
**First agree**, and set **Maximum discussion cycles** from 1 through 20 (default
3). The agreement-policy default is **First agree**.

Auto starts discussion directly by default. Select **Prepare agents
independently first** when every chosen agent should receive one independent
preparation call containing only the original topic and the creation-time
shared Markdown snapshot. When selected, all preparations finish before they
enter discussion context. Active preparations stream as labeled **Auto
preparation** messages in the merged conversation and remain there when
complete. This browser visibility does not add a preparation to another
agent's sidebar preview or provider conversation history.

Auto calls agents sequentially. Discussion passes the topic, the optional complete
preparation set, bounded creation-time history, and bounded prior discussion
through the selected agents.
**First agree** does not converge until every selected agent has completed
one discussion turn. At the end of that initial cycle, any agreeing response
converges the run. If nobody agrees, later cycles stop on the first agreeing
response. **All agree** requires every agent to agree within the same
complete cycle. Preparation calls do not count as discussion turns.
A discussion response agrees when
the standalone word `converged`, matched without case sensitivity, appears in its
final three non-empty lines. This deliberately favors stopping over continuing
when wording is ambiguous. The stored verdict remains `agree`. Reaching the
configured number of complete cycles records `limit_reached` without starting an
extra turn.

The Auto status panel survives reloads and shows progress, participant order,
verdicts, future-turn timeout budget, terminal reason, and completed preparations.
**Stop Auto** is the only cancellation control while Auto is active: it cancels the
current provider when present and prevents a later turn. Manual Send, pass, retry,
cancel, agent identity changes, and project removal remain blocked until Auto is
terminal; file viewing and shared-Markdown editing remain available. Edits do not
change the immutable shared snapshot already captured for that Auto run.

Every live manual or Auto-owned round shows server-authoritative remaining time,
deadline, effective budget, and hard cap. Add `+5`, `+15`, `+30`, or a custom whole
number of minutes from 1 through 240 without restarting the provider. Auto turns
also offer **Extend current + future Auto turns**, which atomically adds the same
duration to the active deadline and to turns that have not started. **Extend
current** affects only the active turn. Each later turn starts with the inherited
budget but a fresh timeout version and extension audit. Rejected stale, late, or
over-cap submissions refresh the authoritative controls and never revive a
finished process.

## Isolation and security boundary

Delibra provides write isolation, not read confidentiality:

- Agent cwd and writable scope are the session `workspace/`; private temp is
  `workspace/.tmp/`.
- Claude receives only Read/Write/Edit and native web tools under the verified safe
  command. Codex runs in `workspace-write`, with shell network disabled and native
  web search enabled.
- Codex provider state uses an app-owned `~/.delibra/codex-home`; subprocesses receive
  an allowlisted environment, not arbitrary server secrets.
- Agents may still read any path permitted to the operating-system user. Do not use
  Delibra as a confidentiality sandbox.
- The HTTP server is localhost-only, rejects non-loopback Host values and cross-site
  mutation Origins, and has no remote-user authentication. Do not bind it to LAN or
  public interfaces in the MVP.
- Project-file browsing remains read-only except for explicit, digest-guarded
  saves to the currently selected `.md` or `.markdown` shared-context file.
  `.delibra/`, `.git/`, `.hg/`, and `.svn/` are never selectable or writable.
  The browser still exposes allowed text files below the entire registered project
  directory through the localhost HTTP service; register only directories whose
  contents may cross that boundary.
- File paths are walked from a verified project-directory descriptor without
  following symlinks. Files are limited to the displayed text-extension allowlist,
  binary/non-regular files are rejected, and each view reads at most the configured
  byte limit plus one byte used to detect truncation. This is a display boundary, not
  an access-control system for other local processes.

Complete JSON, YAML, and YML files are parsed as data and shown in normalized,
escaped preformatted views. Invalid structured content, truncated reads, and pretty
output that would exceed the file-view limit stay as escaped original text with a
visible warning.

- Markdown raw HTML is disabled and SSE text is HTML-escaped before HTMX swaps it.

All mutations use the lock order `registry -> project lifecycle -> session IDs in
sorted order`. A run never holds these locks while waiting for a model or SSE client.
This order also covers project unregister, session delete, and self/cross-session
pass-to operations.

## Recovery limits

Prompt and partial files are written before execution. Graceful cancellation,
timeouts, and shutdown terminate the entire CLI process group and finalize the round.
On startup, a persisted running round becomes an error round with any partial output
promoted and visible; the session remains runnable.

Auto never resumes automatically after process restart. Pointer-backed and orphaned
active Auto records become `interrupted`, active round timeout audit is retained when
recoverable, and no provider call is started. Orderly shutdown first quiesces Auto,
marks it interrupted, cancels its active provider, and only then shuts down the run
manager.

Known Codex missing-login, expired-token, and revoked-refresh-token failures are
reduced to an isolated-login command before live or durable display. After running
that command, Retry creates a new linked stateless round; Delibra neither deletes
the failed round nor copies or removes credential files automatically.

Final-output and metadata writes are best effort under storage failure. Delibra keeps
the partial until both are durable and emits/logs an error, but total storage loss
cannot guarantee an on-disk error record. A hard kill can orphan a provider process;
the MVP has no external watchdog. Graceful shutdown is the supported path.

## Configuration

The main environment settings are:

- `DELIBRA_HOME` (default `~/.delibra`)
- `DELIBRA_RUN_TIMEOUT` (default 900 seconds)
- `DELIBRA_MAX_RUN_TIMEOUT` (default 14,400 seconds / four hours; must be greater
  than or equal to `DELIBRA_RUN_TIMEOUT`)
- `DELIBRA_OUTPUT_LIMIT` (default 10 MiB)
- `DELIBRA_REPLAY_LIMIT` (default 5 MiB)
- `DELIBRA_STATELESS_HISTORY_LIMIT` (default 2 MiB)
- `DELIBRA_STATELESS_ROUND_LIMIT` (default 20)
- `DELIBRA_REQUEST_BODY_LIMIT` (default 2 MiB)
- `DELIBRA_FILE_VIEW_LIMIT` (default 512 KiB)

Names/models are capped at 200 characters, role instructions at 20,000, prompts at
100,000, and pass instructions at 10,000.

## Verification

Run the suite with:

```sh
envs/bin/python -m pytest -q
node --test tests/js/*.js
```

The complete MVP acceptance record, including separate Claude and Codex parity
evidence, the M4 chat/config gate, M5 workspace/file/focus evidence, and the remaining
manual-browser checklist, is in
`docs/acceptance-mvp.md`. Sanitized CLI commands and behavioral isolation evidence
are in `spike/FINDINGS.md`.

The manually invoked real-provider gates are executable and disposable:

```sh
envs/bin/python -m spike.m4_config_resume_gate
envs/bin/python -m spike.m4_chat_gate
```

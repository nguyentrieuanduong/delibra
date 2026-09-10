# Delibra

Delibra is a local research workspace for running Claude Code and Codex CLI as
first-class, human-directed collaborators. Each prompt streams into the browser,
persists as Markdown, and can be replied to or passed to another agent for critique.

## Setup

Requirements:

- Python 3.12+
- Claude Code 2.1.202, authenticated through its normal CLI login
- Codex CLI, authenticated in Delibra's isolated home as described below —
  verified against 0.144.5 and 0.153.4; newer versions are accepted and
  rate-limit fields are feature-detected at runtime

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

Project settings may grant default writable roots to every agent, and agent
settings may add roots for that agent. A writable root is always stored relative
to the registered project directory. The effective grant is the normalized union
of the project and agent lists, with at most 16 roots after redundant descendants
are removed.

Only existing, symlink-free directories may be saved. The project root itself and
the metadata roots `.delibra`, `.git`, `.hg`, and `.svn` are never grantable. The
characters `,*?[]!#\` anywhere in the resulting absolute path, including an
ancestor above the registered project directory, are rejected because that path
cannot be expressed safely in a provider permission rule. Control characters and
non-space whitespace are rejected for the same reason; ordinary spaces and Unicode
directory names remain supported. Every run revalidates every effective root and
names a stale directory in the error instead of silently dropping the grant.

Writable roots deliberately extend an agent's provider working directories beyond
its private session workspace. If two agents receive the same directory, they can
overwrite each other's edits: Delibra provides no locking or warning for shared
writable roots.

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

In merged Chat, the message composer and the sole live/current Auto panel share
the top row. The Auto panel is on the right and has its own scrollbar; no second
Auto status is rendered below the composer. The panel initially matches the
rendered composer height, and Auto content scrolls inside it instead of growing
it. Drag its vertical resize handle between `6rem` and `80vh` to inspect more or
less information; a reload restores the composer-matched height. Compact text
keeps more status and history visible. Its newest-first history lists every
readable Auto run. Select a run to load only its persisted topic and completed
preparations in the panel without navigating, scrolling, or filtering the
conversation.

Preparation output comes from the digest-verified copy stored under the Auto run,
so it survives deleting or changing the session round that produced it. Cards
expose **Focus** through the same shared dialog used by recorded messages, but
only while that original session round still exists unchanged for the
preparation; otherwise the card keeps its persisted output and omits **Focus**.
If the persisted preparation copy itself is no longer readable, the card reports
that its output is unavailable without exposing a storage path.

The persistent **Auto** button beside **Send** opens project-level Auto setup. Before
the first round, the browser copies the unsent composer text directly into the
editable topic field; it is never added to the setup URL. Once a conversation
exists, setup prefers its earliest direct user prompt, falling back to the earliest
recorded Auto topic. Agents are shown in case-insensitive name order and are all
selected by default. Select at least two unique agents, choose **All agree** or
**First agree**, and set **Maximum discussion cycles** from 1 through 20 (default
3). The agreement-policy default is **First agree**.

Completed Auto-owned messages link their canonical **Auto N** run from the message
title. Every recorded prompt remains collapsed by default and appears in a bordered
box when expanded. Completed messages expose **Continue in Auto…** beside
**Send to…** or **Pass to…** while the Pass disclosure is closed, then beside the
actual **Pass** button while it is open. In merged Chat, Continue opens the existing
Auto setup in place; on a session page it opens the same setup in merged Chat. The
clicked message is not promoted to a new topic. Auto keeps the editable durable
Topic and snapshots the bounded completed project conversation when **Start Auto**
is pressed. Replace or expand Topic when the clicked output should instead define a
new discussion topic.

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
A discussion response agrees when an unnegated standalone `converged` or its
Vietnamese equivalent `hội tụ`, matched without case sensitivity, appears in its
final three non-empty lines. Responses are Unicode NFC-normalized first, so
precomposed and decomposed Vietnamese are treated identically.
An English occurrence is ignored only when the same line places it after `not`
or a supported negative contraction (`isn't`, `aren't`, `wasn't`, `weren't`,
`hasn't`, `haven't`, or `hadn't`), with at most two intervening modifiers
chosen from `yet`, `fully`, `completely`, `sufficiently`, and `quite`.
A Vietnamese occurrence is ignored only when the same line places it after a
supported negator (`không`, `chưa`, `chẳng`, `không hề`, or `chưa hề`), with at
most two intervening modifiers chosen from `hoàn toàn`, `thực sự`, `thật sự`,
`hẳn`, and `đủ`. Negation is recognized as a prefix only: a trailing `chưa` is
the question particle (`hội tụ chưa?`) and does not negate.
Another unnegated occurrence in the three-line window still agrees. Unsupported
wording containing `converged` or `hội tụ` also agrees, deliberately favoring
stopping over additional Auto calls. Because `hội tụ` is spelled identically as
verb and noun, a phrase such as `sự hội tụ` agrees for the same reason. The
stored verdict remains `agree`. Reaching the
configured number of complete cycles records `limit_reached` without starting
an extra turn.

The Auto status panel survives reloads and shows progress, participant order,
verdicts, future-turn timeout budget, terminal reason, and completed preparations.
**Stop Auto** is the only cancellation control while Auto is active: it cancels the
current provider when present and prevents a later turn. Manual Send, pass, retry,
cancel, agent identity changes, and project removal remain blocked until Auto is
terminal; file viewing and shared-Markdown editing remain available. Edits do not
change the immutable shared snapshot already captured for that Auto run.

A failed Auto turn is classified before it ends the run. Each adapter maps its
own structured error fields to a category, and the runner does the same for
stderr and for failures it creates itself (spawn, output limits, nonzero exit,
empty result, persistence) — adapters see stdout only, so a marker present only
on stderr would otherwise never be classified. All the evidence folds into one
category, strictest first: `quota` > `auth` > `permanent` > `retryable_server` >
`retryable_transport`. Text no classifier recognizes is `unknown` and is dropped
from the fold, so incidental noise cannot mask a proven retryable failure; only
when no recognized evidence exists at all does the result become `permanent`.

A retryable turn is retried up to `DELIBRA_AUTO_TURN_RETRIES` times with bounded
exponential backoff. Each attempt is a fresh round, so failed attempts stay
visible in the session history, and Stop and shutdown are honoured between
attempts. A quota failure never retries — it pauses the run to a resumable
`stopped`, which **Continue Auto** recovers. Anything else ends the run as
`error` with the cursor parked on the participant that failed.

The chat header polls account quota from Delibra's shared monitor every
`DELIBRA_USAGE_POLL_SECONDS` seconds (default 1800), dropping to
`DELIBRA_USAGE_LOW_QUOTA_POLL_SECONDS` (default 300) while any observed window is
below the warning threshold. Polling never calls a provider, so a window that
reports no percentage at all — every Claude window — keeps the slower interval:
a faster poll could not learn anything about it.

**The 20 % / 8 % / 3 % thresholds are Codex-only, because only Codex reports a
percentage.** Claude's `-p` mode carries no five-hour or weekly figure: its
`rate_limit_event` reports a status and a reset time, `utilization` is absent
unless the account is already warned or refused, and `claude -p "/usage"` returns
prose with no percentage. Delibra therefore runs Claude on status alone —
`allowed_warning` warns, `rejected` pauses — and the table keeps its row label terse as **Claude**. An observed status and
reset time remain visible in the applicable window cell; `unknown` remains
reserved for a window not yet observed. A per-model weekly refusal
(`seven_day_opus`, `seven_day_sonnet`) counts as a weekly refusal; `overageStatus`
never reaches the policy, because it reports whether pay-as-you-go is available
and reads `rejected` on perfectly healthy accounts.

Codex quota comes from the account, not from Delibra's own files. Delibra
isolates Codex into its own `CODEX_HOME`, so a `codex` run in your terminal
spends the same subscription and never appears in the rollouts Delibra can read —
measured at 19 points of drift on the weekly window. It therefore asks
`codex app-server` for `account/rateLimits/read`, which reports the whole account,
and falls back to the newest app-owned rollout when that is unavailable. The read
costs no model turn but does spawn a process, so it runs at most once every
`DELIBRA_CODEX_QUOTA_REFRESH_SECONDS` (default 300) and is bounded by
`DELIBRA_CODEX_APP_SERVER_TIMEOUT_SECONDS` (default 15). Page renders never wait
for it: they ship what the monitor holds and the next poll carries the newer
figure. The two moments that act on the answer — Auto's pre-dispatch check and a
hand-sent Codex prompt — do wait for it.

Live observations are stored in `~/.delibra/usage.json` with expiry and staleness
checks, so a restart cannot turn an old window into a pause. Claude has no
cold-start source at all: on a machine with no persisted observation, the
first-ever Claude turn runs with quota unknown. A
threshold reported during a running turn takes effect before the following turn;
Delibra deliberately does not cancel the turn already in flight. If quota storage
fails, current-process enforcement continues and the round and badge warn that the
state will not survive a restart.

A round the provider refused for quota stays stored as an `error` — a call really
did fail, and reconciliation and every status query depend on that — but it is
shown as a pause: amber rather than red, with a plain sentence and the provider's
own message kept collapsed beside it. Any other failure still renders red. On an
Auto round the sentence points at **Continue Auto**; on a hand-sent prompt it says
the turn did not run, because nothing was paused there.

Each agent card shows how full its provider context window was after the last
turn it reported one — the numerator as measured, the window as the provider
reported it, and `unknown` rather than a guess when either is missing. Codex
reports its window only in the rollout, so it arrives with that round's quota
read; Claude reports it per resolved model on the turn itself.

**Clear context** retires every round for that agent and drops its summary and
native session in one write; the rounds stay in the history, they just stop
being context. **Compact** replaces them with one summary the agent writes
itself, as a stateless turn over a frozen snapshot of every unretired round —
never the ordinary bounded history, because advancing the boundary past a round
nothing summarized would discard it for good. A snapshot over
`DELIBRA_COMPACT_INPUT_LIMIT` is refused rather than partially summarized, with
Clear as the escape hatch. The summary, the new boundary and the surrendered
native session commit together with the round; a failed compaction changes
nothing. Both operations require the session idle and no Auto running, and the
next Auto run honours them: cleared rounds do not return to `baseline.md`, and a
compacted session contributes its summary instead of the rounds it replaced.

**Continue Auto** resumes a finished run in place, from any of the five terminal
states (`stopped`, `interrupted`, `error`, `limit_reached`, `converged`). It is
offered when no Auto is active anywhere in the project. The cursor is
reconstructed from the record: a run that converged or hit its cycle limit
advances one position, while one that stopped or failed re-runs the participant
it was parked on, so the cycle counter continues rather than restarting.
Resume rejects an inconsistent record rather than repairing it, and refuses a run
whose reconstructed cycle would pass the lifetime cap of 100 cycles.

The per-turn time limit and the number of **additional discussion cycles** are
editable on resume. The reconstructed discussion cycle counts as the first
additional cycle, so entering 1 runs through that cycle. The route converts
the delta to the cumulative `max_cycles` stored on the run and its resumption
audit record. Participant name and provider are immutable and a drift
is rejected, while model and effort are refreshed from the live agent
configuration. Every artifact the next turn will read — topic, preparations,
baseline, prior discussion outputs, and shared context — is digest-verified
before the run restarts. Each grant is recorded durably in `resumptions`.

A creation-time cap of 20 cycles still applies to the setup box. Continue Auto
offers from 1 through the remaining lifetime allowance; persistence retains
cumulative limits up to 100.

When quota preflight pauses Auto, **Continue Auto** shows the responsible provider
window, the observation and reset time before accepting the click. Continuing
grants only that exact window and keeps spending it until the reset; a five-hour
grant never covers the weekly window. A new reset, a stale no-reset observation,
or a provider quota error ends the grant. The grant remains anchored to the
observation shown to the user even if the shared monitor changes before submit,
and the resumption audit retains each grant after the active override is cleared.

An Auto run manages its own context, because it never uses a session's staged
history: every Auto turn is stateless with the prompt Auto assembles. The setup
box chooses a mode — **compact**, **clear**, or **off** — a trigger unit, an
interval, and a summarizer (the next agent to speak by default, or a named
participant). It defaults to **off**, so Auto sends everything unless the
operator opts into compact or clear; the interval, unit, threshold, and
summarizer defaults are unchanged. The policy is carried unchanged across
**Continue Auto**: editing it would change the meaning of material the run has
already retired.

The trigger unit is cycles, turns, or the **Auto prompt byte budget** — a
percentage of `DELIBRA_STATELESS_HISTORY_LIMIT` measured in rendered bytes, and
deliberately not called a context threshold: it is not comparable to the
provider-reported context occupancy on an agent card. The byte-budget unit
measures the prompt that is *about to be sent*, frozen once and reused for that
turn when it is below the threshold, because the response that just landed is
what pushes the next prompt over.

Clear retires the pending material with no provider call. Compact spends one
extra turn, outside the speaking order: it never advances the cycle or the next
participant, its round is excluded from staged history and from later Auto
baselines, and no convergence verdict is read from it. It summarizes its own
snapshot of every unretired entry, not the prompt rendering, which has already
dropped the oldest entries to fit. If that snapshot exceeds
`DELIBRA_AUTO_COMPACT_INPUT_LIMIT`, the oldest excess is retired unsummarized and
named in the summary's `dropped_entries` with a surfaced warning — the one place
content is deliberately discarded, and the same entries the prompt renderer
already dropped silently. The resulting summary is mandatory in every later
prompt, charged before any optional entry, and fails the turn closed if it is
missing or tampered with.

A failed compaction never ends the run: nothing is retired, so no content is
lost. The run waits a whole interval — or one full speaking round for the cycles
and byte-budget units — before attempting again, and after
`DELIBRA_AUTO_COMPACT_MAX_FAILURES` consecutive failures it turns compaction off
for the rest of the run and says so once. Two conditions turn it off on the first
occurrence, because neither can improve while the run continues: no headroom for
a summary (`DELIBRA_AUTO_COMPACT_MIN_OUTPUT` against what the prompt limit leaves
after topic, preparations and framing) and a mandatory input already over the
input limit. Quota during a compaction pauses the run to a resumable `stopped`
like any other turn.

The Auto setup box sets the starting per-turn budget in **seconds**, from 1
through `DELIBRA_MAX_RUN_TIMEOUT`, prefilled from the project's default message
turn time limit. A project can save that default in project settings; without a
saved value it inherits `DELIBRA_RUN_TIMEOUT`. The submitted Auto value applies
to every preparation and discussion turn of that run and is stored exactly as
submitted; it is never rounded through minutes. Newly started manual messages
use the effective project default too. The global cap still applies and clamps
a larger saved project value on read. Changing the project default never moves
an active manual or Auto turn's deadline.

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

- Agent cwd is the session `workspace/`, with private temp at `workspace/.tmp/`.
  Write access starts there and may include the explicitly granted writable
  project directories described above.
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
- `DELIBRA_RUN_TIMEOUT` (default 900 seconds; inherited by projects without a
  saved default message-turn time limit)
- `DELIBRA_MAX_RUN_TIMEOUT` (default 14,400 seconds / four hours; must be greater
  than or equal to `DELIBRA_RUN_TIMEOUT`; hard-caps new turns and clamps a larger
  saved project default on read)
- `DELIBRA_OUTPUT_LIMIT` (default 10 MiB)
- `DELIBRA_REPLAY_LIMIT` (default 5 MiB)
- `DELIBRA_STATELESS_HISTORY_LIMIT` (default 2 MiB)
- `DELIBRA_STATELESS_ROUND_LIMIT` (default 20)
- `DELIBRA_REQUEST_BODY_LIMIT` (default 2 MiB)
- `DELIBRA_FILE_VIEW_LIMIT` (default 512 KiB)
- `DELIBRA_AUTO_RESUME_DRAIN_SECONDS` (default 30; 1 through 300) — how long
  **Continue Auto** waits for a finishing Auto task to release its slot before
  returning a conflict
- `DELIBRA_AUTO_TURN_RETRIES` (default 2 retries, i.e. 3 attempts; 0 through 10;
  **0 disables retry**)
- `DELIBRA_AUTO_RETRY_BACKOFF_SECONDS` (default 5; 1 through 300) — attempt *k*
  waits `min(base * 2**(k-1), 4 * base)` seconds
- `DELIBRA_COMPACT_INPUT_LIMIT` (default 2 MiB) and `DELIBRA_COMPACT_OUTPUT_LIMIT`
  (default 256 KiB) — a session Compact's snapshot and its summary
- `DELIBRA_AUTO_COMPACT_INPUT_LIMIT` (default 2 MiB),
  `DELIBRA_AUTO_COMPACT_OUTPUT_LIMIT` (default 256 KiB), and
  `DELIBRA_AUTO_COMPACT_MIN_OUTPUT` (default 4 KiB) — the same three bounds for an
  Auto run's own compaction. Each is clamped at the point of use against the
  prompt limit rather than validated at startup, so lowering only
  `DELIBRA_STATELESS_HISTORY_LIMIT` cannot refuse to start the app
- `DELIBRA_AUTO_COMPACT_MAX_FAILURES` (default 3; 1 through 10) — consecutive
  failed compactions before compaction turns off for that run
- `DELIBRA_AUTO_CONTEXT_TRIGGER_PERCENT` (default 70; 10 through 95) — the
  Auto prompt byte budget offered in the setup box; each run stores its own

Names/models are capped at 200 characters, role instructions at 20,000, prompts at
100,000, and pass instructions at 10,000.

## Verification

Run the suite with `envs/bin/python -m pytest -q`.

Pytest invokes the JavaScript contract files through
`tests/test_routes_chat.py`. Those tests skip when Node is absent; the
optional pre-push check below refuses that reduced suite. To run the
JavaScript contracts directly, use `node --test tests/js/*.js`.

The browser tests drive real Chromium against a live server. Install it
once with `envs/bin/playwright install chromium`. To skip them — for
example on a machine without a browser — use
`envs/bin/python -m pytest -q -m "not browser"`, knowing that this drops
the only coverage of htmx actually applying the fragments the server sends.

An optional local pre-push check runs the full suite:

```sh
ln -s ../../scripts/pre-push .git/hooks/pre-push
```

It refuses to run rather than skipping the browser tests, so it tells you
to install Chromium instead of reporting a hollow green.

It is a convenience, not a branch gate: a clone that has not installed it
can still push a red suite. Enforcing that needs CI, which this repository
does not yet have.

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

# Auto Mode with Independent Preparation and Deterministic Convergence

**Status:** Implemented

**Date:** 2026-07-19

## Goal

Add a project-level Auto mode that lets a user select at least two existing
agents, optionally gives each agent an independent preparation turn on an
editable original topic, and then passes the discussion through those agents
automatically. Auto mode stops when its configured agreement rule succeeds, the
user stops it, an agent fails, or the configured number of complete discussion
cycles is reached.

The same Auto control must work before the first conversation message and after a
manual conversation already exists. All Auto activity must remain durable,
auditable, bounded, and compatible with Delibra's existing per-session round
storage. While any manual or Auto-owned agent is working, the web UI must also
show its remaining timeout and let the user add time without restarting or
shortening the task.

## Confirmed product decisions

- Auto orchestration is a durable server-side state machine, not a browser-driven
  chain and not a model acting as a judge.
- The user selects at least two participating agents. The setup panel preselects
  every available project agent.
- For an existing conversation, the topic is prefilled from the first direct,
  non-Auto user prompt. If the conversation began in Auto mode, it is prefilled
  from the earliest Auto-run topic instead. The prefill remains editable. Editing
  it creates the new Auto-run topic; it does not rewrite prior rounds.
- Independent preparation is opt-in and defaults off. When enabled, an agent
  receives the topic but no other agent's preparation and no prior conversation
  during its preparation turn. After every preparation succeeds, the complete
  preparation set becomes available to every participant for discussion.
- The user chooses one of two agreement policies:
  - `all_agree`: stop only when every participant's response is classified as
    `agree` in the same completed discussion cycle.
  - `first_agree`: stop immediately when any discussion response is classified
    as `agree`.
- The setup defaults to `first_agree`; `all_agree` remains available.
- Agreement is inferred from a bounded response tail, not from output equality or
  semantic similarity. Minor editorial changes can still be paired with `agree`
  when the agent has no substantive objection.
- The user limit counts full discussion cycles. Preparation is a separate phase
  and does not consume the cycle limit.
- A live timeout update only adds time. The UI offers separate actions for the
  current agent and, during Auto, the current agent plus every not-yet-started
  Auto turn.
- Runtime remains bounded by a server-configurable per-turn cap whose default is
  four hours.

Independent preparation is opt-in per Auto run and defaults off. When it is
disabled, discussion begins without preparation provider calls or preparation
context, after the normal locked native-session reset. When enabled, every
selected participant completes one sequential independent preparation before
discussion. Legacy records without the durable choice load preparation as
enabled.

## Chosen approach

Introduce an `AutoManager` alongside the existing `RunManager`. `AutoManager`
owns project-level Auto records, participant reservations, orchestration tasks,
and Auto status events. It delegates every provider call to a narrow extension of
`RunManager`, so subprocess isolation, output capture, cancellation, shared
Markdown snapshots, and normal round durability continue to use one execution
path.

Every preparation and discussion output is a normal round in the producing
session, augmented with Auto provenance. Preparation rounds are omitted from the
main conversation timeline and from later manual history. Discussion rounds are
normal conversation entries. This preserves existing session ownership and
output-file invariants without presenting private preparation as if it were part
of the public discussion.

Auto calls use explicit staged context and do not resume or overwrite the
session's ambient provider conversation. `RunManager` receives an internal Auto
request that requires stateless execution, excludes ordinary session history,
ignores any provider session ID returned by the Auto call, stages an Auto context
file under the producing session's owned round-input directory, and records its
descriptor as provenance on the resulting normal round.

Auto turns reuse each adapter's `_stateless_prompt` with an empty
`staged_history`; `RunManager` excludes local history by bypassing
`_stage_history` on the Auto-request path.

Because stateless execution and discarded provider session IDs are intentional in
this mode, Auto requests suppress the ordinary stateless-continuation and
missing-native-session warnings. Actual adapter warnings and normalized provider
errors remain attached to the round.

`RunManager` replaces its one-shot fixed wait with a mutable monotonic deadline per
`ActiveRun`. The initial budget still comes from `run_timeout`; a live extension
wakes the wait loop and moves only that deadline, never the process start time or
hard cap. The same mechanism serves manual, preparation, and discussion rounds.

When enabled, preparation calls see only the Auto topic, their role instructions,
and the shared-Markdown snapshot captured once when the Auto run is created.
Discussion calls see the Auto topic, the preparation set when enabled, the bounded
conversation baseline captured at Auto start, and the bounded Auto discussion so
far. Preparation preserves the session's existing `cli_session_id`. Whether
preparation is enabled or skipped, immediately before the first discussion call
Auto acquires all participant locks and clears every participant's
`cli_session_id`, because those native contexts no longer represent the
conversation Auto is about to append. Each session file remains an independent
atomic write; a partial storage failure stops Auto before discussion starts, and
cleared sessions safely fall back to stateless history. The next manual turn
therefore cannot resume stale provider context.

No new runtime dependency or external service is required.

## User experience

### Auto setup

The chat composer has a persistent `Auto` button next to `Send`.

- Before a conversation exists, the setup topic is prefilled from the current
  composer text. The browser copies that text directly into the loaded setup
  fragment; it is never placed in the setup GET URL, logs, or browser history.
- During an existing conversation, it is prefilled from the earliest non-retry,
  non-Auto round whose source is a direct user prompt. If none exists, Delibra uses
  the earliest Auto-run topic associated with the visible conversation. If neither
  source can be read, the current composer text is used instead.
- The topic is always editable in the setup panel.
- The participant list uses the agent sidebar order `(case-insensitive name,
  session ID)`, preselects all agents, and requires at least two unique project
  sessions. The checked subset retains that displayed speaking order. Custom
  drag-and-drop speaking order is outside this version.
- `Prepare agents independently first` is unchecked by default. Selecting it
  enables one sequential private preparation call per participant.
- The panel offers `All agree` and `First agree`, defaults to `First agree`, and
  warns that `First agree` may stop before the remaining agents speak.
- `Maximum discussion cycles` accepts an integer from 1 through 20 and defaults
  to 3.
- Before confirmation, the panel displays the maximum discussion-call budget and
  states that enabling preparation adds one call per participant. `First agree`
  can finish with fewer calls.

The server validates the edited topic with the existing prompt rules: non-empty,
valid text, and at most 100,000 characters. It also revalidates every participant
at submission. An unavailable, duplicated, deleted, or foreign-project session
produces a validation response without creating an Auto record. Auto requires
every project session—not only the selected subset—to have no running round and
requires RunManager to report no active `RunKey` for the project. Otherwise it
returns a conflict before capturing the baseline or creating an Auto directory.

### Active and completed display

Starting Auto appends an Auto status panel above the conversation timeline. It
shows:

- the topic, policy, participant order, cycle limit, and whether preparation was
  enabled or skipped;
- `Starting discussion`, `Preparing X of Y`, or `Cycle C of N · agent X`;
- each completed discussion verdict;
- the terminal reason: converged, cycle limit reached, stopped, provider error,
  or interrupted by restart; and
- a `Stop Auto` control while work is active.

When enabled, preparation outputs are available to the user in a collapsed
`Preparations` section after they complete, but do not appear as conversation
messages. They are not supplied to another agent until every preparation has
completed successfully. Skipped runs have no preparation rounds or preparation
section.
Discussion outputs append to the existing timeline using the producing agent's
normal card and round rendering. Agent-card conversation previews and “latest
round” links ignore preparation rounds; session detail may show them only with an
explicit `Auto preparation` label and link back to the owning Auto run.

While an Auto run is active, the composer and manual run, pass, retry, cancel,
session create/edit/delete, and project removal mutations are disabled in the UI
and rejected at the server boundary. The Auto-level Stop route is the only way to
cancel its active participant. File viewing and shared-Markdown
selection/editing remain available, but the active Auto run continues to use its
creation-time shared-context snapshot. This prevents a manual turn or identity
change from invalidating orchestration while preserving unrelated project-file
work. After any terminal state, the same Auto button can start another run from
the conversation as it then exists.

A browser disconnect or reload does not stop orchestration. Reloading reconstructs
the status panel and timeline from durable state.

### Live timeout control

Every visible live-round header displays an approximate `Time remaining` countdown
and the server deadline. During a hidden Auto preparation, the same current-turn
control appears in the Auto status panel; during discussion, the visible live
round owns the current control while the Auto panel continues to show the future
turn budget. The countdown is presentation only; the server's monotonic deadline
remains authoritative. The browser uses a relative duration supplied by the server
rather than trusting its wall clock and resynchronizes after SSE reconnect, status
refresh, or a successful extension.

The control offers `+5 min`, `+15 min`, `+30 min`, and a custom whole-minute value
from 1 through 240. Submitting uses `Extend current`. For an Auto-owned round, a
second action, `Extend current + future Auto turns`, adds the same duration to the
active deadline and to the initial timeout budget for every Auto turn that has not
started. It does not alter completed turns. Prior current-only extensions do not
propagate when the second action is used later.

The UI shows the effective current-turn budget, the hard per-turn maximum, and,
for Auto, the future-turn budget. It disables extension controls during a request
and after the hard cap, cancellation, timeout claim, or finalization. A successful
response replaces the controls with a new deadline/version and emits an SSE status
update so another open tab converges on the same display. A stale, late, or
over-cap request returns the normal structured `409` response plus an
`HX-Trigger: timeout-refresh` header. The timeout control listens for that event
and refetches its authoritative GET fragment, so a rejected double-click also
converges on the winning deadline/version. It cannot revive a process whose
timeout has already been claimed.

## Durable state

Each Auto run owns an app-controlled directory:

```text
<project>/.delibra/auto-runs/<auto-id>/config.json
<project>/.delibra/auto-runs/<auto-id>/topic.md
<project>/.delibra/auto-runs/<auto-id>/baseline.md
<project>/.delibra/auto-runs/<auto-id>/shared-context.md
<project>/.delibra/auto-runs/<auto-id>/preparations/<session-id>.md
```

`shared-context.md` is absent when no project shared Markdown was selected at Auto
creation.

While a run is active, the existing project manifest stores
`active_auto_run_id`. This is the durable project reservation consulted under the
project lock by Auto and manual mutations. Auto creation writes a valid initial
config before publishing the manifest pointer. A terminal transition writes the
terminal Auto config before clearing the pointer. Startup reconciliation never
reconstructs or resumes an active reservation from an orphaned config: an active
config without a manifest pointer is marked `interrupted`; a pointer to a valid
active config is reconciled and then marked `interrupted`; and a pointer to a
valid terminal config is cleared. A pointer to a missing or invalid config remains
a reported storage/ownership error and cannot authorize a provider call.

The directory and files use the existing owned-directory, no-follow, bounded
read, atomic-write, and restrictive-permission rules. Browser fields never supply
filesystem paths.

The Auto config stores at least:

- format version, Auto ID, project ID, created/started/finished timestamps;
- status: `preparing`, `discussing`, `converged`, `limit_reached`, `stopped`,
  `error`, or `interrupted`;
- agreement policy, durable preparation choice, maximum cycles, current cycle,
  and next participant index; legacy records without the choice load preparation
  as enabled;
- ordered participant session IDs plus immutable name/agent/model/effort snapshots;
- topic, baseline, and optional shared-context source/snapshot paths and SHA-256
  digests, plus the baseline's ordered source/byte-range/entry-digest table;
- preparation session/round references, staged paths, and digests;
- discussion turn session/round references, cycle, position, verdict, and output
  digest;
- the active `RunKey`, when one exists;
- the future-turn timeout budget plus the active turn's timeout budget, wall-clock
  display deadline, version, and extension audit entries;
- a durable `stop_requested` flag; and
- terminal reason or normalized error text.

Auto config writes occur atomically after every state transition. A normal
`RoundRecord` gains an optional backward-compatible `auto` descriptor containing
the Auto ID, phase (`preparation` or `discussion`), cycle, position, staged Auto
context path and digest, and parsed verdict. Its `SourceDescriptor.type` is
`auto`, so Auto-generated prompts are not mislabeled as direct user prompts and
generic per-round Retry can reject them deterministically. Existing records
without the descriptor deserialize unchanged.

Each new `RoundRecord` also snapshots its initial/effective timeout budget, hard
cap, display deadline, timeout version, and extension audit entries. These fields
are optional when loading legacy rounds. For a manual active round, an extension
is persisted directly with its running record before the in-memory deadline moves.
For an Auto-owned round, the Auto config is the single active extension journal;
finalization copies its effective timeout summary to both the RoundRecord and the
completed preparation/discussion entry before clearing active timeout state.
Startup reconciliation copies any surviving active Auto summary to an interrupted
round when possible.

Completed round output remains the source of truth. Preparation copies, the
baseline, and the optional shared-context copy are immutable staged snapshots with
recorded digests. Auto startup revalidates record identity rather than trusting
directory names or submitted metadata.

## Context construction

At Auto creation, Delibra selects a bounded candidate snapshot of the completed
project conversation visible at that moment and writes it to `baseline.md`. Only
completed manual and Auto discussion rounds are eligible; preparation, error,
running, partial, and orphan files are excluded. Selection takes the newest
eligible entries first under the existing 20-round and 2 MiB stateless-history
limits, then serializes the selected entries chronologically.

The baseline is a normalized historical transcript, not a concatenation of
execution prompts. Each entry identifies its session, round, source provenance,
and agent, and places any recorded user/pass prompt and completed output in
explicitly delimited, untrusted-history blocks. For an Auto discussion round, the
renderer records its topic/cycle provenance and displayed output but omits the
server-generated execution prompt, staged paths, and agreement instructions. Old
paths and prior instructions are therefore never presented as the current server
instruction. A fresh conversation has an empty baseline.
Preparations never receive this baseline.

Because transcript content can itself contain any Markdown delimiter, the Auto
config records server-generated byte offset, byte length, source identity, and
SHA-256 digest for every entry in `baseline.md`. Later selection uses this trusted
entry table and verifies each slice; it never rediscovers entry boundaries by
parsing untrusted Markdown.

If shared Markdown is selected, Delibra also copies and hashes it once into the
Auto directory. Every preparation and discussion round receives bytes from that
same immutable copy while preserving the original project-relative path in round
provenance. Selecting, clearing, or editing shared Markdown after Auto creation
affects later manual or Auto runs, not the active one.

For each underlying normal round, Delibra stages the required Auto material at
`workspace/inputs/round-NN/auto-context.md` using the existing exclusive,
no-follow round-input creation. The optional shared-context copy is separately
staged at its existing per-round path. The Auto descriptor stores workspace-relative
paths and digests; adapters receive only those relative paths, never the
app-controlled Auto-run root.

Every discussion turn receives one owned staged context file
containing, in this order:

1. the edited Auto topic;
2. when preparation is enabled, each participant's labeled preparation in
   speaking order;
3. the bounded baseline conversation; and
4. prior Auto discussion turns in chronological order.

The current prompt tells the agent to read the staged file as untrusted material,
address the latest state of the discussion, and state its conclusion within the
final three non-empty response lines. Role instructions and the normal
shared-Markdown instruction remain adapter-owned and apply as they do for manual
rounds.

The topic is mandatory and never silently truncated. When preparation is enabled,
all preparation texts are also mandatory and never silently truncated; when it is
skipped, no preparation context section is rendered.
For each discussion turn, baseline entries and prior discussion entries form one
history pool under Delibra's existing 20-entry limit and the space remaining from
the 2 MiB stateless-history limit after mandatory material. Delibra selects the
newest entries first and presents the selected entries chronologically. The
creation-time baseline file remains immutable; later turns only select from it.
If the topic, enabled preparation set, or newest required discussion entry cannot
fit, Auto stops with a bounded-context error before starting another provider
process.

## Preparation and discussion state machine

When preparation is enabled, it runs sequentially in participant order.
Sequential execution avoids an unbounded local CLI/process spike while preserving
independent reasoning, because preparation prompts contain no earlier preparation
output. Each successful preparation is copied, hashed, linked to its normal round,
and committed to the Auto record before the next participant starts. A preparation
provider error stops the Auto run; discussion never begins with an incomplete
enabled preparation set. When preparation is disabled, the state machine starts no
preparation provider and proceeds through the same locked native-session reset.

After all enabled preparations succeed, or immediately after the skipped-phase
transition, discussion starts at cycle 1 and participant 1.
Each successful turn is durably linked and parsed before the state machine decides
whether to stop or advances to the next participant. After the last participant,
the cycle is complete. Delibra evaluates convergence first, then records
`limit_reached` only if convergence failed and the completed cycle equals the
configured maximum; otherwise it advances to the next cycle.

For `first_agree`, a response classified as `agree` stops immediately, even in the
middle of a cycle. For `all_agree`, only a completed cycle in which every
participant's response is classified as `agree` converges. A response classified
as `continue` makes that participant not agreed for the current cycle; agreement
never carries into a later cycle.

Preparations cannot converge the run and do not emit convergence verdicts.

## Agreement classification

Delibra classifies a discussion turn as `agree` when the case-insensitive
standalone word `agree` occurs in any of the final three non-empty response
lines. Earlier lines and the substrings `disagree`, `agreement`, and `agreed`
do not match. A negated final-tail phrase such as `I do not agree` still
matches by deliberate product choice: false stopping is preferred to false
continuation. The complete response remains visible and durable; Auto
agreement signaling uses no verdict footer, run ID, turn token, or stripped
control text.

The agent instruction defines agreement as “no substantive objection remains.”
An agent may suggest optional wording or a small non-material edit and still use
`Agree`. If a substantive objection, unanswered question, or necessary change
remains, the instruction tells the agent not to use `Agree` in its final three
non-empty lines. Any response without a matching standalone word in that bounded
tail is classified as `continue`.

The adapter's canonical complete final text is the classification authority and
is persisted without modification. Text deltas use the ordinary bounded output
capture and streaming path, and complete content needs no terminal reset or
replacement snapshot. Provider text continues to be escaped and rendered through
the existing trusted Markdown path. A verdict is attached only after final output
persistence succeeds and is cleared from the returned record if terminal metadata
persistence fails.

## Live timeout enforcement

`Settings` adds `max_run_timeout`, loaded from
`DELIBRA_MAX_RUN_TIMEOUT`, with a default of 14,400 seconds (four hours). Startup
rejects a value smaller than `run_timeout`. A round's hard cap is snapshotted at
start, so a later environment change cannot alter an already-running process.

`ActiveRun` tracks start time, deadline, and hard deadline with the event loop's
monotonic clock. It also tracks a monotonically increasing timeout version, a
deadline-change wake-up event, and a `timeout_claimed` flag. UTC timestamps are
derived only for durable audit and display. Wall-clock adjustments cannot shorten
or lengthen enforcement.

The wait loop recomputes remaining time whenever the deadline-change event fires.
When remaining time reaches zero, it acquires the session lock and compares the
current monotonic deadline and process return code again. If the provider has
already exited, normal finalization wins. If an accepted extension already moved
the deadline, waiting resumes. Otherwise it sets `timeout_claimed` under that lock
before terminating the process group. The extension path uses the same lock and
rejects provider-exited, finalized, cancelled, stop-requested, or timeout-claimed
runs. Whichever lock-protected transition wins is retained; an extension can never
revive a process.

The timeout extension request contains the exact project/session/round path,
positive whole-minute addition, scope (`current` or `current_and_future_auto`),
and `expected_timeout_version`. The server resolves the exact active `RunKey`,
validates the submitted version, and calculates the new current deadline from the
old deadline—not from request arrival time. It rejects the whole request rather
than partially clamping when either requested budget would exceed the snapshotted
hard cap. The response reports the maximum addition currently available.

For a manual round, the server persists the updated running RoundRecord before
moving the in-memory deadline and publishing `timeout_extended`. For an Auto-owned
round, it atomically updates the owning Auto config first. `current` changes only
active timeout fields. `current_and_future_auto` changes both active fields and
`future_turn_timeout_seconds`; every later preparation or discussion turn starts
with that future budget, a fresh timeout version of zero, an empty per-turn
extension audit, and its own start-time hard-cap snapshot. Only after the durable
write succeeds does RunManager move the in-memory deadline and wake its wait loop.
A storage failure therefore leaves the enforced deadline unchanged.

The version check makes two simultaneous clicks deterministic: one extension
wins, increments the version, and the other receives a stale conflict instead of
silently adding time twice. Cancel, Auto Stop, timeout, and finalization are
serialized against the same active state. The terminal timeout error reports the
effective allowed runtime after accepted extensions.

## Orchestration, locking, and recovery

Only one Auto run may be active per project. `AutoManager` reserves the project and
ordered participant sessions for the entire run and uses the existing global lock
order for each short durable transition. Every affected mutation route consults
the manifest's `active_auto_run_id` and referenced durable config under the same
coordinator; an in-memory UI flag is never the authority. Auto state transitions
use the existing per-project lock; there is no separate Auto lock with a competing
acquisition order. Locks are never held while awaiting provider completion.

Reservation validation occurs inside the same project-locked transition that
allocates a round or mutates project/session identity, not as a route-level
precheck followed by a second lock acquisition. The internal Auto start API carries
the owning Auto ID and may bypass the manual-run rejection only when it matches the
durable active reservation and expected next participant. This closes races between
Auto start, manual Send/Pass/Retry/Cancel, and the next orchestrated turn.

Within that locked internal start, `RunManager` allocates the `RunKey`, persists it
as active in the Auto record, then persists the normal running round with the same
Auto ID and key before spawning the provider. A normal-round persistence failure
clears the active key and moves Auto to `error` before releasing the lock. These are
independent atomic files rather than one filesystem transaction. After a hard
crash, startup recovery handles both possible partial states: an Auto active key
with no matching round becomes `interrupted`, while a matching running round is
first reconciled normally and then linked to the interrupted Auto record. No
provider is spawned until both durable links exist.

After start, `AutoManager` waits on `RunManager.wait`, commits the result, clears
the active key, and only then advances. When a provider is active, the stop route
acquires the project and active-session locks in the established order, persists
`stop_requested`, and claims `cancel_requested` on that exact `ActiveRun` before
releasing locks. It then terminates and awaits the process outside the locks. This
uses the same session-locked claim as extension and timeout: Stop wins only if it
marks cancellation before timeout/finalization wins, and a late Stop returns the
current terminal conflict rather than rewriting the cause. The orchestrator checks
the flag after each completion, and the internal start rechecks it in the same
locked transition that would persist the next active key. No next call may start
after the flag is set. If no provider is active, Stop transitions directly to
`stopped` under the project lock.

A provider error, failed spawn, invalid required artifact, storage error, or context
overflow stops the state machine in `error`; completed work remains visible and no
agent is silently skipped. Generic per-round Retry is not offered for preparation
or discussion errors because retrying one turn would make the Auto state ambiguous.
The user can inspect the error and start a new Auto run from the resulting
conversation.

On application startup, normal running rounds are reconciled by the existing
recovery path. Any Auto record left in `preparing` or `discussing` becomes
`interrupted` after its active round is reconciled. Delibra does not automatically
resume it, preventing surprise provider spending after restart. Reconciliation
then clears a manifest pointer to that now-terminal record. An orphaned active
config without a pointer is also marked `interrupted` but is never republished as
the project reservation. A pointer to a missing or invalid Auto config is reported
as a storage/ownership error rather than silently ignored; it cannot authorize a
provider call.

Orderly application shutdown first quiesces `AutoManager`: it durably prevents new
turns, marks active Auto records `interrupted`, cancels any active Auto-owned
`RunKey`, and awaits orchestration tasks. Only then does the existing RunManager
shutdown continue. This ordering closes the race in which an orchestrator could
observe a cancellation and start the next participant while the process is
stopping.

## HTTP and live updates

The chat view adds these project-scoped routes:

- `GET /projects/{project_id}/auto/setup` for the validated setup fragment;
- `POST /projects/{project_id}/auto-runs` to create and start a run;
- `GET /projects/{project_id}/auto-runs/{auto_id}` for its durable status
  fragment;
- `GET /projects/{project_id}/auto-runs/{auto_id}/stream` for replayable status
  events; and
- `POST /projects/{project_id}/auto-runs/{auto_id}/stop` to request cancellation.

Every live round, manual or Auto-owned, also exposes:

- `GET /projects/{project_id}/sessions/{session_id}/rounds/{round_n}/timeout` for
  the authoritative server-rendered timeout status/control fragment;
- `POST /projects/{project_id}/sessions/{session_id}/rounds/{round_n}/timeout/extend`
  to add time using `minutes`, `scope`, and `expected_timeout_version`; and
- the existing round stream's server-generated `timeout_extended` event, which
  causes clients to refetch the authoritative timeout-control fragment.

The timeout fragment refetches the GET route on `timeout_extended` and
`timeout-refresh`. Successful extension POSTs return a replacement fragment.
Conflict responses retain the application's structured error contract and trigger
the GET refresh, rather than relying on HTMX to swap a non-2xx response body.

For an Auto-owned round, a successful extension also publishes an Auto status
event so its preparation control or future-turn budget refreshes without waiting
for the next orchestration transition.

While Auto is active, HTMX preserves the identity-scoped Auto EventSource,
current preparation timeout host, and current discussion live round across
authoritative status/timeline swaps. A changed run key or terminal response
omits the matching preserved node, so normal cleanup closes stale streams and
initializes only the new owner.

All mutations retain localhost Host/Origin enforcement, bounded form validation,
ID validation, project ownership checks, and server-side participant
revalidation.

Timeout extension additionally validates an integer from 1 through 240, the exact
active key, the owning Auto reservation for Auto-wide scope, and the expected
version. `current_and_future_auto` is rejected for a manual round or an Auto run
that is no longer active. Provider-controlled content never enters timeout fields
or event names.

The setup GET derives durable-history prefills on the server but does not accept a
topic query parameter. On a fresh conversation, the existing local application
script copies the composer value into the returned topic field. Only the final
start POST submits that text.

Auto status events contain state and durable identifiers, not provider-controlled
HTML. The browser fetches or swaps server-rendered round/status fragments, using
the same escaping and SSE replay behavior as normal live rounds. A reconnect can
recover from the durable Auto record even when its in-memory replay window is gone.
The chat page displays the active Auto run when one exists, otherwise the newest
terminal Auto run; older runs remain reachable through round provenance and their
status URLs.

## Security and compatibility

- Topic, agent output, preparation content, baseline content, warnings, and errors
  are untrusted text. None may become an HTML fragment or filesystem path without
  the existing escaping and validation boundaries.
- Auto artifacts stay under the owned `.delibra/auto-runs` root. Staged copies are
  descriptor-relative, no-follow, bounded, and hash-verified before reuse.
- The original shared-Markdown path is display-only provenance after the
  creation-time snapshot; subsequent Auto turns reopen only app-owned staged
  bytes and verify their recorded digest.
- Participant IDs are resolved from the project store under lock. Client-supplied
  names, model values, positions, paths, verdicts, and run state are not trusted.
- The server, not the browser, determines the current participant, cycle, verdict,
  and terminal transition.
- Tail classification is an orchestration signal, not a security boundary.
  Untrusted staged material can influence provider wording, so the bounded rule
  and unchanged durable response make the stop decision inspectable rather than
  authoritative over any external resource.
- Timeout minutes, scope, active identity, version, effective deadline, and hard
  cap are server-validated. The browser countdown never controls termination, and
  no extension can exceed the snapshotted server cap.
- Existing manual rounds, pass-to rounds, retries, shared-context snapshots, and
  projects without Auto records retain their current serialized and rendered
  behavior.
- No automatic provider call begins merely from a GET, page reload, or SSE
  reconnect.

## Alternatives rejected

### Browser-driven chaining

Having HTMX start the next turn after each stream completion would reduce initial
server code, but page closure, duplicate events, or manipulated browser state could
interrupt or alter orchestration. It cannot provide the requested durable Auto
behavior.

### Coordinator or judge agent

A model deciding the next speaker or convergence would add cost and make `All
agree` and `First agree` nondeterministic. The requested policies are simple state
transitions and belong in Delibra.

### Text similarity convergence

Comparing output strings or embeddings would confuse editorial changes with
substantive disagreement. A bounded, directly inspectable response-tail rule is
simpler and preserves the agent's complete wording.

### Replacing or shortening the live timeout

Setting a new total duration from process start can accidentally place the new
deadline in the past or shorten a task the user meant to help. Additive extension
has one clear effect and composes safely across repeated requests.

### Unlimited timeout extension

An unlimited deadline would weaken orphan-process and resource bounds. A
server-configurable cap keeps local policy explicit while the four-hour default is
large enough for intentionally long agent work.

## Acceptance criteria

1. The chat composer exposes Auto before the first message and during an existing
   conversation; both open the same validated setup flow.
2. A fresh Auto setup uses the composer text as its editable topic. An existing
   conversation uses its first direct non-Auto user prompt, or its earliest Auto
   topic when no such prompt exists, as the editable prefill without modifying
   stored history. Unsent composer text never enters a GET URL. Start rejects an
   empty, invalid, or over-100,000-character topic before writing Auto state.
3. The setup preselects all project agents, supports selecting a unique ordered
   subset, rejects fewer than two, offers both agreement policies, and accepts 1–20
   maximum discussion cycles. Start is rejected unless every project session is
   idle and the project has no active normal run.
4. Preparation is unchecked by default. A skipped run starts no preparation
   provider, records no preparation round or context section, and clears every
   participant's native session ID through the normal locked transition before
   discussion. When selected, every agent completes exactly one independent
   sequential preparation before discussion, and no preparation prompt contains
   the baseline or another preparation.
5. Every discussion agent receives the topic, the complete hashed preparation set
   when enabled, and bounded baseline/discussion context. The
   baseline is selected newest-first within the configured limits, rendered as a
   chronological untrusted transcript, and excludes generated Auto execution
   prompts, staged paths, and agreement instructions.
6. Preparation rounds are durable and inspectable but absent from the main
   conversation, agent-card conversation previews, and later manual history.
   Session detail labels them explicitly. Discussion rounds appear normally with
   Auto/cycle/verdict provenance. Every Auto round uses `source.type=auto` and a
   hash-verified Auto descriptor, and generic Retry is not rendered for it.
7. `First agree` stops immediately when the case-insensitive standalone word
   `agree` occurs in any of a response's final three non-empty lines. `All agree`
   stops only after every participant agrees in one completed cycle.
8. A minor-content response may validly agree. Earlier lines and the substrings
   `disagree`, `agreement`, and `agreed` do not match. A final-tail phrase such as
   `I do not agree` deliberately matches because false stopping is preferred to
   false continuation.
9. The complete response is streamed, displayed, and persisted without a verdict
   footer, turn token, stripped control text, or invalid-verdict warning. Legacy
   token-bearing Auto records still load, while newly saved records omit the token.
10. If no convergence occurs, Auto stops after the configured number of complete
    discussion cycles and records `limit_reached` without an extra provider call.
    A converged final allowed cycle records `converged`, not `limit_reached`.
11. Stop prevents any subsequent turn, cancels the one active provider if present,
    and leaves completed rounds and Auto metadata intact.
12. Provider, validation, storage, or context-bound failures stop deterministically
    without skipping an agent or launching the next turn.
13. Page reloads preserve live progress. Orderly shutdown quiesces Auto before the
    run manager; process restart marks an active Auto run interrupted and never
    resumes provider spending automatically.
14. Manual conversation mutations cannot interleave with an active Auto run, and
    the durable manifest reservation—not browser or transient task state—is the
    guard authority. Existing non-Auto behavior and legacy serialized records
    remain compatible.
15. Every turn in one Auto run receives the same creation-time shared-Markdown
    snapshot, while a shared-file edit during Auto remains available to future
    runs.
16. Auto calls never resume or retain provider-native session IDs; enabled
    preparation preserves the pre-Auto ID, and discussion clears participant IDs
    before its first call in both preparation modes so later manual work cannot
    resume stale context. Intentional Auto stateless execution and ignored provider
    IDs do not emit the corresponding manual-continuation warnings.
17. Every manual and Auto-owned live round shows server-derived remaining time and
    permits `+5`, `+15`, `+30`, or a custom 1–240 whole-minute extension without
    restarting or shortening the process. Hidden preparation exposes the same
    control through the Auto status panel.
18. A current-only extension adds to the existing deadline, persists before taking
    effect, increments its timeout version, wakes the wait loop, and remains
    bounded by the snapshotted hard cap. Stale, late, invalid, and over-cap requests
    do not change enforcement and refetch the authoritative GET fragment after a
    structured conflict.
19. During Auto, `Extend current + future Auto turns` atomically adds the same
    duration to the current deadline and future-turn budget. Later turns use the
    updated budget with a fresh per-turn version and audit; completed turns and
    prior current-only extensions are not changed or propagated.
20. `DELIBRA_MAX_RUN_TIMEOUT` defaults to four hours, must be at least the normal
    run timeout, and cannot be changed by the web client.
21. Deadline-versus-extension, double-submit, Cancel, Auto Stop, provider finish,
    storage failure, reconnect, and finalization races have deterministic outcomes;
    an already-observed provider exit beats timeout, and no request revives a
    timed-out, exited, or completed process.
22. Automated tests cover both Auto start states, skipped and enabled independent
    preparation, context sharing, both convergence policies, cycle limits,
    bounded response-tail classification, Stop races, provider failure, restart
    reconciliation, native-ID isolation, locking, storage bounds, baseline
    normalization/selection, shared-context snapshot stability, hostile content,
    unchanged response streaming, timeout fragment refresh, timeout persistence
    and race boundaries, SSE reconnect, and backward-compatible deserialization.

## Explicit non-goals

- Semantic or embedding-based convergence detection.
- A judge/coordinator model, dynamic speaker selection, or agent-selected targets.
- Parallel preparation or parallel discussion turns.
- Choosing preparation separately per participant or changing the preparation
  choice after a run starts.
- Custom drag-and-drop speaking order in the first version.
- Automatic synthesis after convergence or after the cycle limit.
- Automatic continuation of an interrupted or failed Auto run.
- More than one active Auto run in a project.
- Shortening, pausing, or resetting a running deadline; reviving a terminal round;
  removing the server hard cap; or changing global timeout settings from the UI.
- Choosing a custom initial timeout in the Auto setup form. The first turn uses the
  configured normal timeout, and live `current + future` extensions adjust later
  turn budgets.
- Changing provider authentication, role configuration, shared-Markdown editing,
  or the existing manual pass/retry semantics beyond the necessary Auto reservation
  checks.
- Remote multi-user coordination or permission management.

# Project Rebind, Auto Warm-up, and Readable Agent Directories

Date: 2026-07-29
Reviewed: 2026-07-30

## Goal

Make a registered project recoverable after its directory is moved, prevent
First-agree Auto discussions from converging before every selected agent has
spoken once, restore the Message field after Auto terminates, and make agent
storage discoverable by using each agent's immutable name as its directory
name.

## Chosen approach

### 1. Rebind a moved project

The global registry continues to store an absolute project path. A path relative
to the registry would have no stable base and would not locate a project moved
elsewhere. App-owned paths inside a project remain relative to the registered
project root or the relevant session workspace.

The Projects page will render even when a registered path is unavailable. Each
project card will expose a **Rebind location** form accepting a new absolute
directory path. Rebind will:

1. Canonicalize the proposed directory and require it to exist as a real
   directory.
2. Read its existing `.delibra/manifest.json` without following symlinks.
3. Require the manifest format, project ID, and creation timestamp to match the
   registered project. Rebind never creates a new manifest.
4. Reject the selected project's current canonical path and any path already
   assigned to another registered project.
5. Reject while an in-memory manual or Auto run for the project is active.
6. Clear every stored native provider session ID at the verified target,
   migrate any valid legacy session directories, and reconcile persisted
   session and Auto state using the existing restart-recovery rules.
7. Atomically replace only the path in the registry, retaining the project ID,
   display name, and creation timestamp.

Failed validation leaves the registry unchanged. Rebind does not delete or alter
the old location. It may be used when the old directory still exists, provided
there is no active work; this supports moves implemented as copy-then-delete.

Provider CLIs can scope or filter resume lookup by the absolute working
directory. Claude stores project sessions beneath a cwd-derived namespace, and
the installed Codex CLI documents cwd filtering for resume unless `--all` is
used; Delibra's adapter does not pass `--all`. Moving a project therefore makes
existing native resume IDs unreliable even though their strings remain
syntactically valid. Delibra deterministically clears them for both providers.
The next manual round uses bounded staged-history continuation, emits the
existing stateless-continuation warning, and adopts the new native session ID
returned by the provider.

The verified target recovery in step 6 is intentionally performed before the
registry write so no request can observe the new path with a stale provider
resume ID. Those target mutations are idempotent and resumable. If the final
registry write fails, the registry still points to the old path, while the
already-verified target may retain safe resume-ID clearing, completed directory
migration, or restart reconciliation.

A configuration transition whose old native resume ID or agent name would be
unsafe after relocation replaces the recovery backup before replacing the
current configuration. An interruption may therefore leave the old current
configuration with the transformed backup, but it cannot leave a transformed
current configuration whose fallback restores the invalidated state. Retrying
the idempotent relocation or migration completes the pair.

The Projects page will show whether each path is available. Links that require a
`ProjectStore` will not be offered for an unavailable path, while Rebind and
Unregister remain available. Unregistering a stale entry still does not delete
either location.

The Projects page checks for legacy UUID directories from directory names only.
It does not parse every session configuration merely to render the project
list; the full migration report remains on project settings.

### 2. Keep app-owned artifact paths portable

Session, round, pass, shared-context, and Auto artifact metadata will continue to
store paths relative to the project or session workspace. Rebinding therefore
does not rewrite project metadata or recorded prompts.

Agent output is user-visible content, not path metadata. Delibra will not rewrite
absolute paths that an agent happens to include in Markdown because doing so
could corrupt quoted material or change meaning.

### 3. Require one discussion turn per agent before First-agree convergence

Preparation turns do not count as speaking. A selected agent has spoken only
after completing a discussion turn.

For `first_agree`:

- During the first discussion cycle, an `agree` verdict is recorded but cannot
  terminate Auto until every selected agent completes one discussion turn.
- At the end of that first complete cycle, Auto converges if any turn in the
  cycle agreed.
- If nobody agreed, later cycles retain the existing first-agree behavior and
  may converge immediately on the first agreeing response.

For `all_agree`, behavior is unchanged: every participant must agree within the
same complete cycle.

User Stop, provider failure, timeout, application shutdown, and other error
terminal states remain immediate; the one-turn rule gates convergence only. If
an earlier cycle-one participant agreed but a later participant's provider turn
fails, Auto ends in error and does not convert that incomplete cycle into
convergence.
Existing terminal Auto records are not changed. In-progress durable records need
no schema migration because completed discussion turns already identify the
participant and cycle.

The setup and status UI will describe the policy as **First agree, after everyone
speaks once** so the stopping rule is visible before and during a run.

### 4. Re-enable the Message field after Auto

The Message textarea will participate in the existing
`data-disable-during-auto` control lifecycle. It remains disabled while Auto owns
the project and is re-enabled when the terminal Auto status is swapped into the
page. Send, Pass, Retry, agent controls, and the textarea will use the same
server-authored state and client-side synchronization.

### 5. Use immutable agent names as directory names

The session UUID remains the internal identity used by routes, locks, Auto
records, pass provenance, and DOM IDs. Only the directory component changes:

```text
<project>/.delibra/sessions/<immutable-agent-name>/
  config.json
  rounds/
  workspace/
```

Agent names will be normalized to Unicode NFC and trimmed before storage. A name
must:

- be unique within its project using normalized, case-insensitive comparison;
- be one filesystem component, not `.`, `..`, or a dot-prefixed hidden name;
- contain no `/`, `\`, NUL, control character, or trailing dot;
- not have a case-insensitive basename of `CON`, `PRN`, `AUX`, `NUL`,
  `COM1` through `COM9`, or `LPT1` through `LPT9`;
- not consist of exactly 32 hexadecimal characters in any letter case, which is
  reserved for the legacy UUID-directory layout;
- encode to no more than 200 UTF-8 bytes.

The validated stored name is used exactly as the directory name. No slug or
hidden suffix is added. Directory validation compares names after Unicode NFC
normalization so normalization-preserving filesystems such as APFS and
normalizing filesystems such as HFS+ or some network mounts resolve the same
stored agent name.

After creation, the name is immutable. Edit forms show the name as text instead
of an input. The edit route accepts model and effort changes and the existing
pre-round provider/role changes, but rejects any attempted name change.
This is an explicit product requirement: the agent's stored name and folder
component remain identical for the life of the name-layout directory.

`ProjectStore` resolves a UUID by inspecting owned session directories and
validating that each `config.json` UUID is unique and that a name-layout
directory matches its config name. A per-store structural cache maps only UUID
to path and layout; it never retains `SessionConfig` or round histories.
This keeps `config.json` as the sole session-to-directory authority, avoids a
second durable manifest index, and prevents a long-lived run store from pinning
or returning stale full-session snapshots.

The scanner ignores dot-prefixed children and non-directory artifacts such as
Finder's `.DS_Store`. It continues to reject symlinked session directories and
real, non-hidden directories whose `config.json`, UUID, or name ownership is
invalid. Each enumeration performs a fresh scan and parses every session
configuration once; `list_sessions` returns those scanned configurations
instead of loading them a second time. UUID path resolution reuses the
structural cache, while `load_session` always reads the selected `config.json`
from disk. Saving ordinary session state does not invalidate the structural
cache. Only structural operations—create, delete, or rename—invalidate it.

Creation failures, including duplicate and invalid names, continue through the
existing global HTMX response-error renderer into the chat error region. Name
forms state the real limit as 200 UTF-8 bytes because HTML `maxlength` counts
characters rather than encoded bytes; server validation remains authoritative.

### 6. Migrate existing UUID directories

The loader temporarily supports a mixed layout in which a session directory is
either its legacy UUID or its validated immutable name. On startup, before
serving requests or runs, and during a rebind under the project/session locks,
each available project is migrated:

1. Read every legacy UUID directory and preflight all final names.
2. Confirm the names are valid, case-insensitively unique, and do not collide
   with an existing directory.
3. Clear the legacy session's native provider session ID and persist that
   change before moving its working directory.
4. Rename each legacy directory atomically to its exact agent name.
5. Rescan and verify that every UUID resolves to exactly one directory.

The operation is resumable: after a process interruption, already-renamed
directories validate as name-layout directories and remaining UUID directories
are migrated on the next startup. No round, workspace, Auto, or provenance
metadata changes because those records continue to reference session UUIDs. The
next manual round after a moved directory uses bounded staged history, reports
the stateless-continuation warning, and adopts a fresh native provider session
ID.

If legacy names are unsafe or duplicated, that project remains usable in the
mixed/legacy layout and the Projects page reports that directory migration needs
attention. Its affected legacy agents receive a one-time **Set permanent name**
action. The action is available only before that agent has been migrated,
validates uniqueness against the complete project name set, clears that
session's native provider ID, and migrates that one directory without waiting
for other blocked legacy agents. It then freezes the chosen name. New agents
always use the new rules.

## Approaches considered

### Clear native provider IDs before exposing a relocated cwd — chosen

Pros:

- Prevents a known-invalid Claude resume and any cwd-filtered provider lookup
  from becoming the user's first post-move round.
- Reuses Delibra's existing bounded staged-history continuation and warning.
- Is deterministic across providers and does not require parsing provider error
  text.

Cons:

- Discards provider-native context that might still be resumable.
- Bounded staged history may contain less context than the provider's native
  session.

### Preserve IDs and retry statelessly after a resume failure

Pros:

- Retains native context whenever a provider can resume across the move.
- Would also improve resilience for unrelated provider-side resume failures.

Cons:

- Requires reliably distinguishing resume failures from other CLI failures.
- Makes one logical round perform two provider attempts and complicates timeout,
  cancellation, streaming, and billing semantics.
- Is a broader runner feature than relocation recovery.

### Exact immutable name plus UUID lookup — chosen

Pros:

- Produces the exact readable directory requested.
- Preserves stable UUID references throughout the application.
- Does not duplicate session-to-directory metadata.
- Mixed-layout lookup makes migration recoverable.

Cons:

- Requires stricter names and a migration path.
- UUID lookup is initially a directory scan, mitigated by a per-store cache.

### Immutable `name--short-id` directories

Pros:

- Naturally unique and easier to migrate.
- Permits duplicate display names.

Cons:

- The folder is not the exact agent name.
- The user explicitly chose name equality and immutability.

### Keep UUID directories and generate a readable index

Pros:

- No storage migration and minimal risk.

Cons:

- Does not solve direct directory-tree navigation.
- Leaves the original usability problem in place.

## Acceptance criteria

1. The Projects page loads and renders every card when two or more registered
   paths no longer exist.
2. Rebinding to the moved directory succeeds only when its manifest identity
   matches the selected registry entry.
3. Rebind rejects the selected project's current canonical path and every
   relative, missing, wrong-identity, duplicate, actively-running, or symlinked
   `.delibra` metadata target without changing the registry or session
   configuration.
4. A successful rebind preserves all sessions, rounds, Auto records, project
   settings, and the project ID, and does not modify or delete the old
   directory during a copy-then-rebind move.
5. App-owned paths persisted within the project remain relative; rebind performs
   no artifact-path rewrite.
6. With three participants and `first_agree`, agreement from participant one
   still runs participants two and three before converging.
7. If nobody agrees in cycle one, a later first agreement may terminate Auto
   immediately.
8. `all_agree`, Stop, error, timeout, and shutdown behavior remain unchanged.
9. The Message textarea becomes editable when Auto reaches any terminal state
   without requiring a page reload.
10. A newly created agent named `Researcher` is stored at
    `.delibra/sessions/Researcher/` while its UUID remains the route and
    provenance identity.
11. Duplicate, unsafe, hidden, UUID-shaped, over-byte-limit, and case-equivalent
    agent names are rejected before filesystem changes; the UI states the
    200-UTF-8-byte limit and HTMX creation errors are visible.
12. Agent names cannot be changed after creation, including through a direct
    form submission.
13. Existing valid UUID session directories migrate without changing their UUID,
    content, round numbering, Auto history, or pass provenance. Migration clears
    the native provider session ID; the next round completes through staged
    history, shows the existing warning, and adopts a fresh provider ID.
14. Interrupted migrations resume safely, and each invalid or duplicate legacy
    name remains accessible and can be migrated independently through the
    one-time naming flow. After permanent naming, corrupting the current
    `config.json` recovers the permanent name and cleared native provider ID
    from `config.json.bak` without making another session unloadable.
15. A dot-prefixed file such as `.DS_Store` does not prevent session loading.
    A pure canonical-equivalence test proves an NFD directory name matches its
    stored NFC agent name independently of the host filesystem's rename
    semantics, and a non-ASCII named session resolves after reopening its store.
16. A successful project rebind clears every native provider session ID in both
    `config.json` and `config.json.bak` before the new registry path becomes
    observable. Corrupting the current configuration after rebind recovers
    `cli_session_id = null` from the backup.
17. The complete Python suite and `node --test tests/js/*.js` pass, including
    storage-security coverage.

## Non-goals

- Automatically discovering where an arbitrary missing project was moved.
- Using relative paths in the global registry.
- Moving or deleting the old project directory.
- Rewriting paths embedded in user prompts or agent-generated Markdown.
- Renaming projects when an agent is named.
- Replacing session UUIDs with names in URLs, locks, Auto records, or provenance.
- Allowing agent names to change after their permanent name-directory exists.
- Retrying a failed native provider resume automatically as a stateless run;
  relocation proactively clears known-invalid resume IDs instead.

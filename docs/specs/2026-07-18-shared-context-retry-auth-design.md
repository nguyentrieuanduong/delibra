# Shared Project Context, Failed-Round Retry, and Codex Reauthentication Design

**Date:** 2026-07-18
**Last reviewed:** 2026-07-19
**Status:** Approved

## Goal

Let a project owner choose one existing Markdown file as the shared project brief,
edit it through Delibra, and give every subsequent agent run a stable snapshot of
its current contents. Add a safe retry path for failed rounds, and replace the
fragile copied Codex OAuth credential with a dedicated, actionable Delibra login
flow.

## Chosen approach

Use the registered project as the source of truth for shared context, while keeping
each execution isolated and reproducible:

1. The user selects one existing user-owned `.md` or `.markdown` file from the
   project file viewer. Delibra stores its project-relative path as an optional
   field in the existing project manifest; app-owned and version-control metadata
   trees are not selectable.
2. The selected file becomes editable through a bounded plain-text editor in
   Delibra. Selection, clearing, and saves use the project lifecycle lock, and each
   save carries the digest originally loaded by the editor so an external edit is
   rejected instead of overwritten.
3. At run start, Delibra snapshots the selected file through one no-follow file
   descriptor: the same bounded byte stream is validated as complete UTF-8, hashed,
   and copied into that round's session workspace. The recorded round stores the
   selected relative path, staged path, and SHA-256 digest.
4. Both adapters tell the agent to read that staged snapshot as project background
   and standing requirements. Agent-specific role instructions still define the
   agent's role, and the current user message may give more specific instructions
   for the current turn.
5. Retrying an error creates a new, linked round. It reuses the persisted request,
   re-verifies and re-stages any pass-to source material, and deliberately starts a
   stateless continuation from completed local history instead of resuming an
   ambiguous provider-side failed turn. The original failed round remains
   immutable.
6. Codex uses a separately authenticated Delibra `CODEX_HOME`. Delibra stops cloning
   the ambient `~/.codex/auth.json`; missing, expired, and revoked credentials
   produce reauthentication instructions for the isolated home without exposing
   credential contents.

## Alternatives considered

### Inject the Markdown text directly into every prompt

This is mechanically smaller, but it expands every stored prompt, makes provenance
and size accounting harder, and gives no durable record of which file revision an
agent saw. A staged, hashed snapshot fits the existing pass-to and stateless-history
architecture better.

### Mirror or symlink the selected file into every workspace

A live mirror would let a running agent observe mid-run edits and would weaken the
workspace write boundary. Symlinks conflict with Delibra's no-follow rules. An
immutable per-round snapshot preserves isolation and makes concurrent edits affect
only future runs.

### Delete failed rounds instead of retrying

Deletion is simpler, but removes diagnostic evidence and creates surprising gaps in
the project conversation. Appending a linked retry preserves the audit trail and is
consistent with Delibra's immutable completed-round model.

## Shared Markdown selection and editing

- The project file viewer offers **Use as shared context** only for a complete,
  regular `.md` or `.markdown` file. The selected file is visibly marked.
- Files below `.delibra/`, `.git/`, `.hg/`, or `.svn/` are never selectable. This
  prevents the new editor from mutating Delibra-owned round/session data or
  version-control internals that the read-only browser may display.
- The selected file view offers a plain-text editor plus **Save**, **Choose another**,
  and **Stop sharing** actions. Project settings also displays the selected path and
  broken-selection state.
- Selection does not create or rename project files. The user may create a Markdown
  file using their normal filesystem tools and then choose it in Delibra.
- Paths are stored project-relative and must pass the existing traversal, symlink,
  regular-file, and project-root checks. A selected file that is later removed,
  replaced by a symlink, becomes non-UTF-8, or exceeds the configured file-view
  limit is shown as unavailable.
- The optional manifest field is backward compatible with `delibra/1`. Manifest
  updates preserve the identity and creation fields, and the selection continues to
  survive unregister/re-register import.
- Editing is the only new project-file write capability. It is limited to the
  currently selected Markdown file, uses a no-follow descriptor-relative safe
  replacement, preserves the file's ordinary permission bits, and never follows a
  renamed directory or file symlink.
- The edit form includes the SHA-256 digest of the bytes it displayed. Under the
  project lock, Save requires both the selected path and its current digest to match
  that form. A changed selection or external filesystem edit returns a conflict and
  leaves the newer file untouched; the user must reload before saving again.
- Empty Markdown is valid. NUL bytes, invalid UTF-8, truncated reads, and content
  over the configured limit are rejected rather than silently changed.
- Editor values and previews remain Jinja-escaped, and the existing Markdown
  renderer continues to reject raw HTML.
- Changing or saving the shared file while an agent is already running does not
  change that run's snapshot. The next run receives the newly selected or saved
  contents.
- A project with no selected shared file keeps the current run behavior and records
  no shared-context descriptor.

## Agent context and provenance

- Run start snapshots the shared file while holding the project/session lock used
  for other run-start mutations. If the selected file is unavailable, run creation
  fails visibly before a round record or prompt/partial files are created; Delibra
  never silently runs without selected shared requirements.
- Validation, hashing, and staging consume one descriptor-opened stream capped at
  the configured file-view limit plus one byte. Delibra does not validate one file
  identity and then reopen a potentially replaced path for copying. A failed
  snapshot removes any newly created round input directory.
- The snapshot is stored under the new round's existing `workspace/inputs/round-NN/`
  tree. It is readable by the agent but is not a writable link to the project file.
- `RoundRecord` gains an optional backward-compatible shared-context descriptor with
  the selected relative path, staged path, and digest. Existing records without the
  field continue to load unchanged.
- The shared-context instruction is added for first turns, native resumes, stateless
  continuations, pass-to rounds, and retries. The adapter prompt names only the
  workspace-relative staged path; it does not expose the registered project path.
- The round view shows the shared file path and digest so the user can identify the
  exact context snapshot used for that result.

## Failed-round retry

- A **Retry** control appears only on recorded rounds whose status is `error`.
  Running, complete, cancelled, and orphan views do not expose it.
- The retry endpoint validates that the round belongs to the project/session, reads
  the immutable persisted prompt, and starts the next allocated round in the same
  session.
- For a pass-to retry, Delibra first copies the original completed source round and
  requires its digest to match the failed round's recorded `source_sha256`. If that
  source session was deleted, the original staged copy may be used only when its
  digest still matches. The verified bytes are copied to the retry's own input
  directory, and the single recorded staged path in the persisted prompt is
  replaced with the new path. A missing, changed, or ambiguous source rejects the
  retry without creating a round.
- The new record has `retry_of` provenance and retains the original source
  lineage while recording the retry's newly staged source path and verified digest.
  The original error, partial output, prompt, and files are not edited or deleted.
- Retry forces a stateless continuation containing only completed local history.
  It does not reuse the provider's native session ID for the failed turn, avoiding a
  duplicate or half-committed provider-side turn. A successful retry establishes a
  fresh native provider session ID for later rounds when the provider supplies one;
  otherwise the existing bounded stateless fallback remains active.
- The retry uses the latest saved shared Markdown snapshot. This allows a user to
  correct project requirements before trying the same request again.
- The endpoint obeys the existing session busy rule. In the chat view it appends a
  new live fragment; in the session view it does the same in that session's rounds.

## Codex authentication recovery

- The isolated `CODEX_HOME` remains mandatory because Codex can load state and
  instructions from that location. Delibra does not switch back to the ambient
  Codex home or weaken its environment allowlist.
- `_prepare_codex_home` creates and validates the isolated directory but no longer
  copies ambient OAuth credentials. Initial setup and migration require a distinct
  `codex login --device-auth` with `CODEX_HOME` pointing at Delibra's Codex home.
- Existing isolated credentials are retained. Delibra never deletes or overwrites an
  authentication file automatically.
- Known missing-login, expired-token, and revoked-refresh-token failures from
  subprocess preparation, parsed provider events, or terminal stderr are reduced at
  one runner error-normalization boundary, before live SSE publication or durable
  persistence, to a stable escaped message containing a shell-quoted login command
  for the resolved isolated home. Matching raw stdout/stderr authentication
  diagnostics are suppressed rather than appended to the normalized error. Delibra
  never reads or displays credential contents, and the resulting error round offers
  Retry.
- README setup and recovery instructions explain the dedicated login and why copying
  `~/.codex/auth.json` is unsupported.
- README's project-file security boundary is updated from strictly read-only to
  read-only except for explicit, digest-guarded edits of the currently selected
  shared Markdown file.

## Acceptance criteria

1. A user can select, change, clear, view, and edit one existing user-owned project
   Markdown file without enabling writes to any other project file, `.delibra/`
   data, or version-control internals.
2. The selection survives application restart and project unregister/re-register;
   clearing it restores the existing no-shared-context behavior.
3. Every new run type receives a complete, bounded, single-descriptor
   shared-context snapshot and records its path and digest; an unavailable selected
   file prevents the run from being created.
4. Delibra-originated edits and run starts serialize so each run sees either the
   complete old content or the complete new content, never a partial mixture.
   Digest preconditions prevent Save from overwriting an external edit made after
   the editor loaded.
5. Every eligible error round can create a new linked retry without mutating the
   original, resuming an ambiguous failed provider turn, or including failed
   partial output as completed history. Pass-to retries run only from source bytes
   that still match the recorded digest.
6. A revoked Codex refresh token is normalized before live or durable output and
   yields concise isolated-login guidance. After the user reauthenticates, Retry can
   execute the request without deleting the failed round.
7. Existing projects, manifests, rounds, chat/session rendering, pass-to behavior,
   file browsing, and provider isolation continue to work.

## Non-goals

- Multiple shared Markdown files or ordered context bundles.
- Creating, renaming, or deleting project files from Delibra.
- Allowing agents to edit the central shared file directly.
- Updating an already running agent when the shared file changes.
- Automatically performing OAuth login, copying credentials between Codex homes, or
  deleting stale credentials.
- Retrying cancelled, completed, running, or orphaned rounds.

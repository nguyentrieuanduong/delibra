# Optional Auto Preparation and Setup Defaults

**Status:** Implemented

**Date:** 2026-07-19

## Goal

Make independent preparation opt-in for each new Auto run, default the setup
form to `First agree`, and align Auto participant checkboxes and agreement radios
with their labels. Skipping preparation must start the normal discussion without
making any preparation provider calls or weakening existing Auto durability,
locking, timeout, and context rules. Recorded-message links to project-owned
Markdown files must open through Delibra's existing secure file reader instead
of resolving against the chat URL and returning a JSON 404.

## Approaches considered

### Persist an explicit preparation choice

Add a boolean to the durable Auto record, submit it from an unchecked setup
checkbox, and branch the existing orchestration state machine on that value. This
keeps the user's choice inspectable after reload and lets legacy records retain
their historical behavior.

### Infer the choice from an empty preparation list

Treat an empty `preparations` list as a skipped phase. This is ambiguous while a
normal preparation phase is starting or has failed before its first successful
turn, and it cannot faithfully render the user's original choice after reload.

### Add a separate Auto mode or new public status

Create distinct prepared/unprepared modes or a new `starting` status. This makes
the state and route surface larger even though discussion, convergence, and Stop
behavior are otherwise identical.

## Chosen approach

Persist `preparation_enabled: bool` on `AutoRunRecord`. The Auto setup form adds
an unchecked `Prepare agents independently first` checkbox named
`prepare_first`; a missing form field therefore means false. `AutoManager.create`
accepts and validates the boolean, and newly serialized records always include
it. A legacy `delibra-auto/1` record without the field loads it as true because
all runs created by the earlier format required preparation.

New records continue to enter the existing short `preparing` orchestration state
so the established transition can clear every participant's stale native session
ID under the participant locks before discussion. When preparation is disabled,
the driver immediately calls that transition without starting a provider. The
status fragment describes this brief state as `Starting discussion`, not as an
active preparation. When enabled, the existing sequential independent
preparation path is unchanged. The discussion transition requires a complete
preparation set only when the option is enabled.

Discussion context accepts an empty preparation sequence. A skipped run therefore
contains the topic, bounded creation-time conversation baseline, creation-time
shared Markdown snapshot, and prior discussion, but no synthetic or empty
preparation sections. Its durable `preparations` list remains empty.

The setup form checks `First agree` by default and leaves `All agree` available.
The POST continues to require an explicit valid agreement policy so crafted
requests cannot bypass validation. The provider-call hint states the discussion
maximum and that enabling preparation adds one call per selected participant.
The Auto status panel records whether preparation was enabled or skipped.

## Checkbox alignment diagnosis and fix

The global `label { display: grid; }` rule is appropriate for stacked text fields
but also affects the Auto fieldset's checkbox/radio labels. Their control and text
become separate implicit grid items, placing the control above or against the top
of its name. Auto choice labels receive a dedicated class rendered as a scoped
row flex container with centered cross-axis alignment. Textarea and number-field
labels keep the global stacked layout.

## Linked Markdown diagnosis and fix

The generic Markdown renderer currently preserves a link destination literally.
From `/projects/<id>/chat`, a project-relative destination such as
`docs/review.md` therefore becomes `/projects/<id>/docs/review.md`, where no
route exists. Agent output can also contain an absolute in-project Markdown path
with an optional `:line` suffix; the browser treats that filesystem path as an
application route and receives `{"detail":"Not Found"}`.

Rendering becomes project-aware only when a trusted `Project` is supplied by a
server template. Project-relative `.md` and `.markdown` destinations and
absolute destinations lexically contained by the registered project root are
normalized to project-relative paths and rewritten to the existing
`/projects/<id>/files/view?path=...` or `files/focus` route. An optional trailing
`:line` is accepted and removed because the current reader has no line-anchor
contract. In chat and focus fragments, static HTMX attributes open the result in
the existing file reader or focus dialog; ordinary session pages retain the
canonical reader URL as progressive fallback.

The renderer does not read the target file and adds no serving route. The
existing descriptor-based file route remains the only filesystem boundary and
continues to reject traversal, symlinks, non-regular files, and paths outside the
registered project. External URLs, non-Markdown links, malformed paths, and
absolute paths outside the project remain unchanged.

## What changes

- The setup fragment adds the unchecked preparation checkbox, changes the
  checked agreement radio to `First agree`, updates the call-budget hint, and
  uses the aligned choice-label class for checkboxes and radios.
- The Auto start route parses `prepare_first` with a false default and passes the
  explicit boolean into orchestration.
- `AutoRunRecord` persists `preparation_enabled`; legacy records missing it load
  as enabled.
- `AutoManager` skips preparation calls when disabled while retaining the locked
  native-session reset before discussion.
- Status rendering distinguishes `Starting discussion`, active preparation, and
  skipped/enabled configuration.
- Recorded prompts, outputs, and focused rounds rewrite eligible project-owned
  Markdown links to the existing secure file reader routes.
- README and the main Auto design are updated to describe opt-in preparation and
  the new default agreement policy.

## Acceptance criteria

1. A fresh Auto setup has `Prepare agents independently first` unchecked,
   `First agree` checked, and `All agree` unchecked.
2. Participant checkboxes, the preparation checkbox, and agreement radios are
   horizontally aligned with their names; text and number fields remain stacked.
3. Submitting the form without `prepare_first` creates a durable record with
   `preparation_enabled=false`, starts no preparation provider calls, and begins
   discussion with participant 1 after clearing participant native session IDs.
4. A skipped run has no preparation rounds, preparation copies, or preparation
   context sections. Discussion still receives the topic, bounded baseline,
   immutable shared Markdown snapshot, and prior discussion.
5. Submitting `prepare_first=true` preserves the existing behavior: every
   selected agent completes one independent sequential preparation before any
   discussion call.
6. The status view reports whether preparation was enabled or skipped and never
   labels the skipped transition as an active agent preparation.
7. Legacy Auto records without `preparation_enabled` load as enabled and serialize
   with the explicit field on their next write.
8. Agreement-policy validation, convergence semantics, cycle limits, Stop,
   timeout extension, restart reconciliation, and manual-run locking are
   unchanged.
9. Clicking a project-relative or absolute in-project `.md` or `.markdown` link
   in a recorded message opens the file through Delibra's reader; an optional
   trailing `:line` does not cause a 404.
10. Chat links target `#file-reader`, focused-round links target
    `#focus-dialog-content`, and links on a standalone session page still have a
    working canonical reader `href` without requiring HTMX.
11. External URLs, non-Markdown links, traversal attempts, and absolute paths
    outside the registered project are not rewritten, and no new filesystem
    access path is introduced.

## Explicit non-goals

- Enabling or disabling preparation after an Auto run starts.
- Choosing preparation separately per participant.
- Parallel preparation or discussion.
- Changing the meanings of `First agree`, `All agree`, or response-tail agreement
  detection.
- Adding a new public Auto status, endpoint, dependency, or browser-side state
  machine.
- Serving files outside the registered project, rewriting external or
  non-Markdown links, or adding exact line-number navigation.

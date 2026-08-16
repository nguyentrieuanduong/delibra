# Chat Auto History and Message Clarity Design

## Goal

Make Auto easier to monitor and revisit from merged Chat, and make each recorded
message's prompt, Auto provenance, and transfer choices easier to understand.

The message composer and Auto panel share the top conversation row. The Auto
panel owns one bounded vertical scroll area containing the current/latest Auto
status and the project's older Auto-run history. Historical inspection shows an
Auto run's persisted topic and preparation outputs; it never scrolls, filters,
or otherwise changes the conversation timeline.

## Approaches considered

### Render every historical topic and preparation with the Chat page

This needs no follow-up request, but every Chat load would read and render every
Auto topic and preparation artifact. The payload and filesystem work would grow
without bound as a project accumulates runs.

### Render a run index and lazy-load historical details

Render lightweight Auto run summaries newest first. Opening an older run loads a
read-only fragment containing only that run's persisted topic and completed
preparations. Keep the current/latest `_auto_status.html` projection separate so
historical inspection cannot replace the authoritative element used to disable
manual controls while Auto is active.

This adds one small HTML-fragment route but keeps initial Chat rendering bounded
to record metadata and reuses the application's existing HTMX and focus-dialog
patterns.

### Fetch Auto records as JSON and render history in browser JavaScript

This would support a richer client-side selector, but it duplicates escaping,
empty-state, routing, and presentation behavior already handled by server-side
templates. It adds client state without improving the requested workflow.

## Chosen approach

Use the lazy-loaded server-rendered history index.

Add a top-row wrapper in merged Chat with the composer on the left and a bounded
Auto panel on the right. On narrower supported widths the columns may shrink,
but they remain side by side; the existing Chat page already enforces a desktop
minimum width. The Auto panel, rather than nested sections within it, owns the
vertical scrollbar so the user can move through current status and older runs
with one scroll gesture. The composer keeps a `20rem` minimum, the Auto panel
keeps a `14rem` minimum, and the panel is capped at `min(30rem, 45vh)` so history
has more room without consuming the entire conversation workspace.

Move the existing `#auto-status-host` into the right-hand Auto panel; do not leave
or render another status copy below the composer or elsewhere in Chat. Within
that one panel, keep the authoritative current/latest Auto status DOM separate
from lazy-loaded historical fragments so inspecting history can never replace
the element used to enforce active-Auto controls.

A new history index in the same right-hand panel lists all readable Auto runs
newest first by canonical run number, with legacy creation-time ordering retained
where numbering has not completed. Each item identifies its Auto number, status,
and creation time. Opening an item is a read-only historical view and never
replaces `#auto-status`. Index refreshes after a run is created and as Auto status
changes, so a newly started or completed run becomes visible without a page
reload. One Auto-directory scan supplies the current/latest projection, history
index, and timeline Auto-number mapping for a full Chat render.

Opening a history item requests a dedicated same-project HTML fragment. The
fragment displays the persisted Auto topic (the prompt supplied when starting
Auto) and its completed preparations only. It does not render discussion
messages, link to a point in the timeline, or change timeline disclosure state.
If preparation was skipped or no preparation has completed, the fragment says so
explicitly.

Current-status and historical preparations use one shared preparation-summary
partial. Every preparation is a bordered card with participant and round
metadata. A `Focus` button loads that preparation round through the existing
round-focus route into the existing shared focus dialog. No duplicate artifact
or focus endpoint is introduced. If a preparation output is no longer readable,
the card instead shows the fixed text `Preparation output unavailable.`, omits
the broken Focus action, and does not expose the storage exception or path.

For recorded messages, append the canonical `Auto N` link to the round title
when the round belongs to Auto. Keep phase, position, verdict, and other detailed
Auto metadata in provenance below the title without repeating the primary link.
The live-round title keeps its existing Auto link behavior.

The prompt remains collapsed under `Prompt`. When expanded, its Markdown content
appears in a bordered, padded prompt box, visually distinct from the output. The
same prompt-box treatment applies inside the shared round-focus dialog.

Completed messages expose `Continue in Auto…` in both Pass disclosure states,
but only one copy is visible at a time:

- while collapsed, it is beside `Send to…` in merged Chat or `Pass to…` on a
  session page;
- while expanded, the collapsed-row copy is hidden and an equivalent control is
  beside the actual `Pass` submit button.

Both copies preserve the current behavior: merged Chat opens project-level Auto
setup in place without round parameters, while a session page navigates to the
canonical Chat Auto-setup URL. Active Auto disabling and the two-session minimum
remain unchanged.

## What changes

- `app/routes/chat.py` supplies lightweight, newest-first Auto history metadata
  to merged Chat.
- `app/routes/auto.py` exposes an HTML-fragment route for one run's historical
  topic and preparations and a refreshable history-index response.
- `app/templates/chat.html` adds the shared composer/Auto top-row layout, moves
  the sole `#auto-status-host` into the right-hand panel, and hosts the scrollable
  Auto history index there.
- `_auto_status.html` and a shared preparation-summary partial render bordered
  preparation cards with Focus controls while preserving the authoritative live
  status element and SSE stream.
- New Auto-history partials render the run index, lazy-loaded topic/preparation
  details, and explicit empty states.
- `_round.html` places canonical Auto provenance in the message title.
- `_round_contents.html` adds the prompt box and disclosure-state-specific
  Continue-in-Auto controls, and `_round_focus.html` reuses the same prompt box
  in the shared focus dialog.
- `app/static/app.css` defines the top-row columns, single Auto-panel scrollbar,
  preparation and prompt boxes, and disclosure-state action visibility.
- Route/template tests cover history ordering, lazy artifact loading, escaping,
  empty preparations, Focus URLs, refresh behavior, title links, prompt-box
  markup, and both Pass disclosure states.
- `README.md` documents the Auto history panel and the revised message controls.

## Acceptance criteria

1. The merged Chat composer is on the left and the Auto panel is on the right in
   one top row.
2. The live/current Auto status renders only in that right-hand panel; no status
   copy remains below the composer or elsewhere in Chat.
3. The Auto panel has one bounded vertical scrollbar independent of the
   conversation timeline.
4. Chat lists every readable Auto run newest first and identifies number, status,
   and creation time without eagerly loading every topic or preparation output.
5. Opening a historical run displays its persisted topic and completed
   preparations inside the Auto panel.
6. Historical inspection does not navigate, scroll, filter, expand, or collapse
   conversation messages.
7. Selecting a historical terminal run while another Auto run is active cannot
   re-enable Send, Pass, retry, cancel, or Auto-start controls.
8. Starting a run and subsequent Auto status changes refresh the history index
   without requiring a full page reload.
9. Readable preparation outputs are escaped, bounded by the existing
   captured-output limit, and displayed in individually bordered cards.
10. Every preparation with a readable backing round has a Focus control that
    opens the existing static round-focus fragment in the shared focus dialog;
    an unreadable preparation returns HTTP 200 with a path-free unavailable state
    and no broken Focus action.
11. A run with preparation disabled or no completed preparation has a clear
    empty state.
12. A completed Auto-owned message includes its canonical `Auto N` link in the
    message title; detailed phase and verdict provenance remains available.
13. A recorded prompt remains collapsed by default and appears in a bordered box
    when expanded, including inside the shared round-focus dialog.
14. A collapsed Pass disclosure shows `Continue in Auto…` beside its summary.
15. An expanded Pass disclosure hides that outer copy and shows an equivalent
    `Continue in Auto…` control beside the actual `Pass` submit button.
16. Both Continue-in-Auto placements retain the existing no-round-parameter,
    minimum-participant, canonical navigation, and active-Auto disabling rules.

## Explicit non-goals

- Jumping from an Auto history item to its discussion or preparation messages in
  the conversation timeline.
- Replaying, resuming, cloning, deleting, or editing a historical Auto run.
- Changing Auto topics, preparation inputs, convergence rules, participant
  ordering, storage formats, or artifact retention.
- Loading discussion output into the Auto history detail fragment.
- Adding a JSON history API, client-side history store, pagination, or a new
  focus dialog.
- Changing session-page composer layout or the three-column Chat workspace.

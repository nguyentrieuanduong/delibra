# Chat live-refresh matrix

## Scope

The chat page's live-refresh paths only: what must reach the browser while
a run or an Auto discussion is in flight. Other pages, and ordinary form
POSTs that end in a full render, are out of scope. This boundary is
deliberate -- a partial matrix that says so is useful; one that claims to
be complete is not.

Delibra pushes HTML, not state. Every row is a server-side change an
operator can see, the fragments it must refresh, and the test proving the
browser applies them.

One row per **fragment**, not per state change, and "agent card" is not a
fragment: `_agent_card_oob.html:3-5` emits two independent OOB targets,
`#agent-status-<id>` and `#agent-preview-<id>`. A row that lists several
fragments and names a test asserting one of them reads as covered when it
is not — the same overclaim that let these bugs ship.

The third column is **Refresh path**, not "Sent by". Some rows have no
single sending line: the composer rows are client-derived from another
fragment's swap, and naming a server line for them would be false.

| State change | Fragment that must refresh | Refresh path | Covered by |
|---|---|---|---|
| Manual run starts | `#agent-status-<id>` → `running` | `app.routes.runs.start_run_fragment` → `_live.html` → `_agent_card_oob.html` | `tests/browser/test_auto_timeline_swaps.py::test_the_composer_locks_while_its_agent_runs_and_unlocks_after` |
| Manual run starts | `#agent-preview-<id>` → `Running…` | `app.routes.runs.start_run_fragment` → `_live.html` → `_agent_card_oob.html` | `tests/browser/test_auto_timeline_swaps.py::test_the_composer_locks_while_its_agent_runs_and_unlocks_after` |
| Manual run starts | composer send controls disable | `#agent-status-<id>` OOB → `htmx:afterSwap` → `syncComposerAvailability` | `tests/browser/test_auto_timeline_swaps.py::test_the_composer_locks_while_its_agent_runs_and_unlocks_after` |
| Manual run starts | live round section | `app.routes.runs.start_run_fragment` returns `_live.html` into `.chat-timeline` | `tests/browser/test_auto_timeline_swaps.py::test_the_composer_locks_while_its_agent_runs_and_unlocks_after` |
| Manual run starts | `#usage-badge` | `app.routes.runs.start_run_fragment` → `_live.html` → `_usage_badge.html` | *(none — follow-up 1)* |
| Manual run completes | `#agent-status-<id>` → `idle` | `_live.html` `sse:done` → `app.routes.sessions.round_fragment(view=chat)` → `_agent_card_oob.html` | `tests/browser/test_auto_timeline_swaps.py::test_the_composer_locks_while_its_agent_runs_and_unlocks_after` |
| Manual run completes | `#agent-preview-<id>` → round output | `_live.html` `sse:done` → `app.routes.sessions.round_fragment(view=chat)` → `_agent_card_oob.html` | `tests/browser/test_auto_timeline_swaps.py::test_the_composer_locks_while_its_agent_runs_and_unlocks_after` |
| Manual run completes | composer send controls re-enable | `#agent-status-<id>` OOB → `htmx:afterSwap` → `syncComposerAvailability` | `tests/browser/test_auto_timeline_swaps.py::test_the_composer_locks_while_its_agent_runs_and_unlocks_after` |
| Manual run completes | round article replaces the live section | `_live.html` `sse:done` → `app.routes.sessions.round_fragment(view=chat)` | `tests/browser/test_auto_timeline_swaps.py::test_the_composer_locks_while_its_agent_runs_and_unlocks_after` |
| **Another** agent's run ends while this one is selected | selected agent's composer keeps its draft and stays enabled | foreign `_live.html` `sse:done` → foreign card OOB → `syncComposerAvailability` reads the selected card | `tests/browser/test_auto_timeline_swaps.py::test_another_agents_card_refresh_leaves_a_half_typed_draft_alone` |
| Auto round starts | chat timeline live section | `AutoManager._run_discussion_attempt` → `publish_current_status` → `_auto_status.html` `sse:status` timeline GET | `tests/browser/test_auto_timeline_swaps.py::test_a_started_round_installs_its_live_section_without_a_reload` (htmx applies it) + `tests/test_auto.py::test_a_status_event_is_published_while_the_round_key_is_live` (the publication itself — see "Attribution" below) |
| Auto round starts | `#auto-status` | `AutoManager._run_discussion_attempt` → `publish_current_status` → `_auto_status.html` `sse:status` status GET | *(none — follow-up 2)* |
| Auto round completes | chat timeline | `AutoManager._run_discussion_attempt` → `_publish_status` → `_auto_status.html` `sse:status` timeline GET | *(none — follow-up 3)* |
| Auto round completes | `#auto-status` | `AutoManager._run_discussion_attempt` → `_publish_status` → `_auto_status.html` `sse:status` status GET | *(none — follow-up 4)* |
| Auto run ends | `#auto-status` → terminal | terminal status GET or Stop POST → `app.routes.auto._status_response` | `tests/browser/test_auto_timeline_swaps.py::test_stopping_auto_refreshes_every_participant_card_without_a_reload` |
| Auto run ends | participant `#agent-status-<id>` | `app.routes.auto._status_response` → `auto_status_context(refresh_agent_cards=True)` → `_agent_card_oob.html` | `tests/browser/test_auto_timeline_swaps.py::test_stopping_auto_refreshes_every_participant_card_without_a_reload` (htmx applies it) + `tests/test_routes_auto.py::test_a_terminal_auto_run_refreshes_participant_cards_out_of_band` (the OOB is sent) |
| Auto run ends | participant `#agent-preview-<id>` | `app.routes.auto._status_response` → `auto_status_context(refresh_agent_cards=True)` → `_agent_card_oob.html` | `tests/browser/test_auto_timeline_swaps.py::test_stopping_auto_refreshes_every_participant_card_without_a_reload` (htmx applies it) + `tests/test_routes_auto.py::test_a_terminal_auto_run_refreshes_participant_cards_out_of_band` (the OOB is sent) |
| Auto run ends | `#auto-history-status-<n>` | `app.routes.auto._status_response` → `auto_status_context(refresh_history_status=True)` → `_auto_history_status.html` | `tests/browser/test_auto_timeline_swaps.py::test_stopping_auto_refreshes_every_participant_card_without_a_reload` |
| Auto run ends | chat timeline | terminal `_publish_status` → `_auto_status.html` `sse:status` timeline GET | *(none — follow-up 5)* |
| Auto status event delivery | the Auto SSE connection itself | `_auto_status.html` `#auto-status-stream-<id>[hx-preserve]` | `tests/browser/test_auto_timeline_swaps.py::test_the_auto_status_swap_does_not_close_its_event_source` |

## Attribution: what a browser test can and cannot pin

Two rows above name a browser test *and* a lower-level test. That is not
belt-and-braces; the browser test alone cannot attribute the refresh to the
path in its own row.

`#chat-timeline` refreshes on **any** `sse:status` event
(`_auto_status.html:117-121`), and that GET renders whatever rounds are live
when it arrives. So several independent publications can install the same
fragment, and removing the one a row names still leaves the browser test green.
Measured on the Auto-round-starts row: removing the round-start publication
alone leaves the browser test passing in 2.9s; removing the end-of-turn
publication as well makes it fail on a 30s timeout. The Auto-run-ends card rows
behave the same way — stopping Auto cancels the held round, and the resulting
`sse:done` refetch refreshes that participant's card whether or not
`refresh_agent_cards` was set.

The division of labour is therefore:

- the **browser** test proves htmx applies the fragment to the real DOM — the
  gap all six shipped defects slipped through;
- the **unit or route** test proves the server emits it on the path the row
  names, and is mutation-provable in isolation.

A row that names only a browser test is claiming the first, not the second.

## Rules

1. Adding a live-refresh state change means adding a row before the code.
2. One row per fragment. A row naming several fragments hides the ones its
   test does not assert.
3. A named test must assert the fragment in its own row.
4. A row with no test is a known, listed risk — not a silent one. It must
   name its own follow-up, and follow-ups are tracked below, not left blank.
5. A fragment that only a full page load refreshes is not refreshed.
6. If more than one path can refresh a fragment, a browser test cannot pin
   which one did. Name a lower-level test for the path, per "Attribution".

## Follow-ups

1. Cover `#usage-badge` refreshing on a manual run start.
2. Cover the Auto status panel refreshing on a round start.
3. Cover Auto round completion refreshing the timeline.
4. Cover Auto round completion refreshing the Auto status panel.
5. Cover a naturally completed Auto run refreshing the terminal timeline;
   the Stop-path test covers the terminal status, history, and cards.

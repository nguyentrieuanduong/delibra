# Selected Agent and Structured File Viewing Implementation Plan

> **Execution requirement:** Follow `code-craft:executing-plans`, implement test-first, and stop if a task's red test fails for an unexpected reason.

**Goal:** Keep the selected agent expanded in every server-rendered selection state and add safe, normalized JSON/YAML project-file views with raw fallbacks.

**Architecture:** Selection state remains entirely server-rendered in `_agent_card.html`; no client-side disclosure synchronization is added. Structured formatting lives in a small pure module, while `app/routes/files.py` retains responsibility for descriptor-safe reads, truncation decisions, and view context. `_file_view.html` continues to escape all non-Markdown content.

**Dependencies:** Python 3.12, FastAPI, Jinja2, PyYAML 6.0.3, pytest.

---

## Task 1: Render only the selected agent expanded

**Files:**

- Modify: `tests/test_routes_chat.py`
- Modify: `app/templates/_agent_card.html`

### [x] Step 1: Write failing selection-state assertions

Extend `assert_synchronized_selection_fragment` so all selection, create, and edit fragment tests require exactly one expanded outer disclosure:

```python
assert response.text.count('<details open class="agent-details">') == 1
```

Update `test_chat_workspace_renders_four_regions_and_full_agent_information` to distinguish selected from unselected disclosures:

```python
assert response.text.count('<details open class="agent-details">') == 1
assert response.text.count('<details class="agent-details">') == 1
```

In `test_chat_uses_left_agent_selection_and_right_file_rail`, require the selected Beta card to contain the open form:

```python
assert '<details open class="agent-details">' in beta_card.group()
assert response.text.count('<details open class="agent-details">') == 1
```

In `test_chat_selection_is_deterministic_and_invalid_selection_is_rejected`, add the one-open assertion to default and explicit full pages and to the sidebar fragment. This covers the initial-load default, explicit query selection, and sidebar-only projection.

### [x] Step 2: Run the focused tests and confirm the expected failure

Run:

```bash
envs/bin/python -m pytest tests/test_routes_chat.py -k 'workspace_renders_four_regions or left_agent_selection or selection_is_deterministic or selection_transaction or create_and_edit or hx_create or hx_edit' -q
```

Expected: failures because `_agent_card.html` does not yet render `open`.

### [x] Step 3: Render selected disclosure state

Change the outer disclosure in `app/templates/_agent_card.html` to:

```jinja2
<details{% if agent_view.selected %} open{% endif %} class="agent-details">
```

Do not add `open` to the nested Edit disclosure.

### [x] Step 4: Re-run the focused tests

Run the command from Step 2, then the complete Python suite:

```bash
envs/bin/python -m pytest -q
```

Expected: all selected-agent tests pass, and each response with a selected agent has exactly one open outer disclosure.

### [x] Step 5: Commit Task 1

Run the security pre-commit diff checks, then:

```bash
git add app/templates/_agent_card.html tests/test_routes_chat.py
git commit -m "feat: keep selected agent information expanded"
```

## Task 2: Pin the safe YAML parser

**Files:**

- Modify: `requirements.txt`
- Modify: `requirements.lock`

### [x] Step 1: Add the direct dependency

Add this direct pin to `requirements.txt`:

```text
PyYAML==6.0.3
```

Add the same resolved package to `requirements.lock` in package-name order:

```text
PyYAML==6.0.3
```

### [x] Step 2: Install and verify the pinned package

Run:

```bash
envs/bin/pip install PyYAML==6.0.3
envs/bin/pip check
envs/bin/pip freeze
```

Expected: `pip check` reports no broken requirements and `pip freeze` matches the new lock entry without extra transitive packages.

## Task 3: Pretty-format valid JSON and YAML test-first

**Files:**

- Modify: `tests/test_routes_files.py`
- Create: `app/structured.py`
- Modify: `app/routes/files.py`
- Modify: `app/templates/_file_view.html`
- Modify: `app/static/app.css`

### [x] Step 1: Add a generated behavior-level route test

Add one seeded generated-input test to `tests/test_routes_files.py`. It writes compact JSON and safe-dumped YAML values, requests the existing file-view route, extracts the escaped structured `<pre>`, and asserts the decoded JSON/YAML value round-trips. Include `<script>` in generated string values and assert it never appears as an HTML element. Request one YAML file through `/files/focus` to cover focused rendering.

### [x] Step 2: Run the generated route test and confirm the expected failure

```bash
envs/bin/python -m pytest tests/test_routes_files.py -k generated_structured -q
```

Expected: the test fails because the existing route returns an undecorated raw `<pre>` instead of a structured pretty view.

### [x] Step 3: Implement valid structured formatting

Create `app/structured.py` with an immutable `StructuredView`, JSON two-space normalization, YAML `safe_load_all`/`safe_dump_all` block normalization, mapping-order and Unicode preservation. Integrate extension mapping and formatting into `app/routes/files.py`. Add the escaped `structured-data` template branch and `.file-view pre.structured-data { tab-size: 2; white-space: pre; }` CSS rule. At this step, complete valid files are formatted; recovery behavior remains for Task 4.

### [x] Step 4: Run the targeted and complete Python suites

```bash
envs/bin/python -m pytest tests/test_routes_files.py -k generated_structured -q
envs/bin/python -m pytest -q
```

Expected: generated values round-trip, unsafe strings remain escaped, focus rendering works, and all existing tests pass.

## Task 4: Add structured-file recovery one behavior at a time

**Files:**

- Create: `tests/test_structured.py`
- Modify: `tests/test_routes_files.py`
- Modify: `app/structured.py`
- Modify: `app/routes/files.py`
- Modify: `app/templates/_file_view.html`

### [x] Step 1: Add invalid-input formatter tests

Create `tests/test_structured.py` with a parameterized test requiring invalid JSON and YAML to return the unchanged source plus `Could not pretty-format this {FORMAT} file; showing the original text.`

### [x] Step 2: Run the invalid-input test and confirm the expected failure

```bash
envs/bin/python -m pytest tests/test_structured.py -k invalid -q
```

Expected: parser exceptions escape because recovery is not implemented.

### [x] Step 3: Implement invalid-input recovery

Catch only JSON/YAML parser, dumper, recursion, and related value/type failures in `pretty_structured_text`, returning the untouched source and warning.

### [x] Step 4: Verify invalid-input recovery and the complete suite

```bash
envs/bin/python -m pytest tests/test_structured.py -k invalid -q
envs/bin/python -m pytest -q
```

### [x] Step 5: Add an invalid-file route warning test

Add one route test that opens malformed JSON, requires the unchanged escaped source, and requires the formatter warning in a status message.

### [x] Step 6: Run the route warning test and confirm the expected failure

```bash
envs/bin/python -m pytest tests/test_routes_files.py -k invalid_structured -q
```

Expected: the formatter recovers, but the route/template do not expose its warning.

### [x] Step 7: Thread and render structured warnings

Thread `structured_warning` through the route and render it as a status warning before the escaped structured `<pre>`, then run the targeted and complete Python suites.

### [x] Step 8: Add the normalized-output limit test

Add one route test requiring a valid compact source whose pretty output exceeds `file_view_limit` to fall back unchanged with `Pretty-formatted JSON exceeds the view limit; showing the original text.`

### [x] Step 9: Run the output-limit test and confirm the expected failure

```bash
envs/bin/python -m pytest tests/test_routes_files.py -k structured_output_limit -q
```

Expected: the current formatter returns expanded output.

### [x] Step 10: Implement and verify the normalized-output limit

Add the explicit `output_limit` parameter, UTF-8 byte-length check, and route argument, then run:

```bash
envs/bin/python -m pytest tests/test_structured.py tests/test_routes_files.py -q
envs/bin/python -m pytest -q
```

### [x] Step 11: Add a truncated structured-route test

Add one route test proving a `.json` file cut by `file_view_limit` is not parsed, retains the existing truncation warning, and adds `This JSON file is truncated; showing the original text without pretty formatting.`

### [x] Step 12: Run the truncated test and confirm the expected failure

```bash
envs/bin/python -m pytest tests/test_routes_files.py -k truncated_structured -q
```

Expected: the current route attempts to parse the truncated text or omits the format-specific warning.

### [x] Step 13: Skip parsing truncated structured files and verify all file behavior

Implement the truncation branch before the formatter call, then run:

```bash
envs/bin/python -m pytest tests/test_structured.py tests/test_routes_files.py -q
envs/bin/python -m pytest -q
```

### [x] Step 14: Remove untested multi-document behavior

Temporarily reduce YAML normalization to `safe_load`/`safe_dump` and rerun the generated single-document route test. This removes production behavior that was written without its explicit test while keeping the existing GREEN contract.

### [x] Step 15: Add and run the multi-document YAML test

Add one formatter test requiring two safe YAML documents to normalize without warning and preserve both values. Run it and confirm RED because the single-document loader rejects the second document.

### [x] Step 16: Restore safe multi-document normalization and verify

Restore `safe_load_all`/`safe_dump_all`, then run formatter, file-route, and complete Python suites.

### [x] Step 17: Commit Tasks 2-4

Run the security pre-commit diff checks, then:

```bash
git add requirements.txt requirements.lock app/structured.py app/routes/files.py app/templates/_file_view.html app/static/app.css tests/test_structured.py tests/test_routes_files.py docs/specs/2026-07-17-selected-agent-structured-files-design.md .plans/2026-07-17-selected-agent-structured-files-plan.md
git commit -m "feat: pretty-print JSON and YAML project files"
```

## Task 5: Full verification and review

**Files:** Review all files changed above.

### [x] Step 1: Run the complete automated suite

```bash
envs/bin/python -m pytest -q
node --test tests/js/test_app_errors.js
envs/bin/pip check
```

Expected: all Python and JavaScript tests pass and dependencies are consistent.

### [x] Step 2: Perform security and code review

Confirm:

- YAML uses only `safe_load_all` and safe dumping of values produced by that loader.
- Every structured output path remains Jinja-escaped inside `<pre>`.
- Truncated inputs never reach either parser.
- Formatted output is bounded by `file_view_limit`.
- No client-side code can create a second selected/open agent projection.
- The nested Edit disclosure is not forced open.

Apply `code-craft:verification-before-completion`, then `code-craft:code-review`. Fix findings test-first and rerun the affected plus complete suites.

### [x] Step 3: Update the project milestone record if required by repository convention

If `.plan/2026-07-16-MVP.md` tracks post-MVP tasks, add completed entries for selected-agent expansion and structured pretty viewing only after implementation and verification pass. Do not rewrite unrelated plan history.

# Selected Agent and Structured File Viewing Design

**Date:** 2026-07-17
**Status:** Approved

## Goal

Make the active agent unmistakable and make JSON/YAML project files easier to inspect without weakening the file viewer's existing safety boundaries.

## Selected agent behavior

The outer `Agent information` disclosure for the selected agent is rendered with the HTML `open` attribute. All unselected agent disclosures remain closed. This is server-rendered state, so the invariant holds for:

- the default selection on initial page load;
- an explicit `?agent=` selection;
- `/chat/select` and `/chat/sidebar` fragments;
- HTMX create and edit responses that preserve or change the selection.

Exactly one outer agent disclosure is open whenever a project has a selected agent. The nested `Edit` disclosure remains closed. Users may temporarily collapse the selected disclosure in the browser, but the next server-rendered selection update restores the selected-agent-open invariant.

## JSON and YAML presentation

The existing descriptor-safe, byte-limited read remains the only way file contents enter the view. For complete `.json`, `.yaml`, and `.yml` files:

1. JSON is parsed with the standard library and rendered with two-space indentation while preserving object insertion order and Unicode.
2. YAML is parsed with PyYAML's safe loader and emitted in block style while preserving mapping order and Unicode. Multiple YAML documents are supported.
3. The normalized text is placed in the existing escaped `<pre>` presentation. Structured content never becomes trusted HTML.

Formatting is deliberately a normalized data view. Original whitespace, comments, anchors, aliases, and quoting style do not need to be preserved.

PyYAML `6.0.3` is added as a direct, pinned dependency. This is the current stable release on the [official PyPI project page](https://pypi.org/project/PyYAML/), and the implementation uses `safe_load_all`, never the object-constructing default loader.

## Failure and resource behavior

- Invalid JSON/YAML is shown as the original escaped text with a visible format-specific warning.
- A truncated structured file is not parsed because the suffix may have been cut mid-token; the bounded original text and both truncation/format warnings are shown.
- If normalized output would exceed the configured file-view byte limit, the original escaped text is shown with a warning.
- Parser, dumper, and recursion failures use the same raw-text fallback.
- Binary detection, UTF-8 replacement reporting, traversal rejection, symlink rejection, and focus-view behavior remain unchanged.

## Verification

Route tests cover initial, explicit, fragment, create, and edit agent selection projections. Formatter tests include deterministic generated JSON/YAML values and confirm semantic round trips, invalid input fallback, multi-document YAML, and output limits. File-route tests confirm pretty output, HTML escaping, focused rendering, invalid fallback, and truncated fallback.

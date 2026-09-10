"""Pure-function tests for the --add-dir spike harness.

recommends.md S2: 1,401 lines of gate logic with no automated test is how
two paid runs were spent proving the harness wrong rather than the CLI.
Nothing here starts a provider.
"""

from pathlib import Path

import pytest

from spike import spike_add_dir as add_dir

from spike.spike_add_dir import (
    EDIT_BASELINE,
    GrantNotEffective,
    Invocation,
    Report,
    ambient_inert,
    ambient_token,
    build_tree,
    check_expectations,
    claude_tool_outcomes,
    failure_detail,
    findings_section,
    following_option_parsed,
    observed_writes,
    repeated_form,
    sanitize,
    should_retry_claude_variadic,
    splice,
    upsert_findings,
    variadic_form,
    with_write_rules,
)


def test_absolute_permission_rules_use_the_double_slash_anchor() -> None:
    """One leading slash anchors at the working directory, not the root.

    code.claude.com/docs/en/permissions: "A pattern like
    /Users/alice/file isn't an absolute path. The single leading slash
    anchors at the settings source, not the filesystem root."
    Both spike runs died here.
    """

    argv = ["--allowedTools", "Read(/**),Edit(/**),WebSearch,WebFetch"]
    result = with_write_rules(argv, [Path("/private/tmp/p/grant-a")])

    assert result[1] == (
        "Read(/**),Edit(/**),WebSearch,WebFetch,Edit(//private/tmp/p/grant-a/**)"
    )


def test_the_rule_family_is_edit_not_write() -> None:
    """Write(path) rules are accepted and never consulted.

    Same page: "Claude Code checks file permissions against Edit(path) and
    Read(path) rules only ... Use Edit(docs/**) in place of Write(docs/**)."
    Correcting only the slash count would have bought a third failed run.
    """

    argv = ["--allowedTools", "Read(/**)"]
    rules = with_write_rules(argv, [Path("/p/a"), Path("/p/b")])[1]

    assert "Write(" not in rules
    assert rules.count("Edit(//") == 2


def test_a_denied_read_prerequisite_is_reported_as_its_own_cause() -> None:
    """recommends.md S1: an Edit that never got its Read is not a tool bug."""

    events = [
        {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "r1",
                        "name": "Read",
                        "input": {"file_path": "/p/x"},
                    }
                ]
            }
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "r1",
                        "is_error": True,
                        "content": "permission to use Read",
                    }
                ]
            }
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "e1",
                        "name": "Edit",
                        "input": {"file_path": "/p/x"},
                    }
                ]
            }
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "e1",
                        "is_error": True,
                        "content": "File has not been read yet",
                    }
                ]
            }
        },
    ]

    assert claude_tool_outcomes(events)["/p/x"] == "read-denied-first"


def test_permission_rule_failure_never_blames_the_sandbox() -> None:
    with pytest.raises(GrantNotEffective, match="sandbox was never reached"):
        check_expectations(
            {"grant_a": False},
            {"grant_a": "allow"},
            "claude",
            "fresh",
            outcomes={"grant_a": "permission-rule"},
        )


@pytest.mark.parametrize(
    "observed,expected",
    [
        ({"workspace_control": False, "grant_a": True, "grant_b": False}, False),
        ({"workspace_control": True, "grant_a": False, "grant_b": False}, False),
        ({"workspace_control": True, "grant_a": True, "grant_b": True}, False),
        ({"workspace_control": True, "grant_a": True, "grant_b": False}, True),
        ({"workspace_control": True, "grant_a": False, "grant_b": True}, True),
    ],
)
def test_variadic_fallback_is_bounded_to_an_arity_signature(
    observed: dict[str, bool],
    expected: bool,
) -> None:
    assert should_retry_claude_variadic(observed) is expected


def test_add_dir_forms_and_splice_are_exact() -> None:
    roots = [Path("/p/a"), Path("/p/b")]

    assert repeated_form(roots) == ["--add-dir", "/p/a", "--add-dir", "/p/b"]
    assert variadic_form(roots) == ["--add-dir", "/p/a", "/p/b"]
    assert splice(["claude", "--next", "value"], "--next", ["--add-dir", "/p/a"]) == [
        "claude",
        "--add-dir",
        "/p/a",
        "--next",
        "value",
    ]


def test_ambient_detector_catches_stream_and_disk_canaries(tmp_path: Path) -> None:
    tree = build_tree(tmp_path)
    marker = ambient_token("a", "CLAUDE.md")
    invocation = Invocation([], 0, 0.0, [{"text": marker}], "")

    assert ambient_inert(tree, invocation)[0] is False
    (tree.workspace / "ambient-hook-leak-a.txt").touch()
    assert ambient_inert(tree, Invocation([], 0, 0.0, [], ""))[0] is False


def test_observed_writes_separates_created_files_from_seeded_edits(
    tmp_path: Path,
) -> None:
    """An Edit target exists before the turn, so existence proves nothing.

    Only the disappearance of the seeded baseline shows the model changed it;
    treating it like a created file would report every seeded probe as written.
    """

    tree = build_tree(tmp_path)
    label = "claude-fresh"
    created = tree.workspace / "created.txt"
    created.write_text(f"{label}\n", encoding="utf-8")
    paths = {
        "workspace_control": created,
        "edit_delibra": tree.edit_delibra,
        "edit_outside": tree.edit_outside,
    }

    untouched = observed_writes(tree, paths, label)
    assert untouched["workspace_control"] is True
    assert untouched["edit_delibra"] is False

    tree.edit_delibra.write_text("rewritten by the model\n", encoding="utf-8")
    assert observed_writes(tree, paths, label)["edit_delibra"] is True
    assert EDIT_BASELINE in tree.edit_outside.read_text(encoding="utf-8")


def test_findings_section_renders_every_heading_without_leaking_a_raw_key() -> None:
    """A replacement key is a real absolute path; rendering one is a leak."""

    rendered = findings_section(Report())

    for heading in (
        "### Installed",
        "### Writes observed on the filesystem",
        "### Why each call ended that way",
        "### Native resume",
        "### Protected sentinels",
        "### Ambient instructions and provider configuration in added directories",
        "### Sanitized argv",
        "### Notes",
    ):
        assert heading in rendered
    assert "<PROJECT>" not in rendered or str(Path.home()) not in rendered


@pytest.mark.parametrize(
    "form,response,expected",
    [
        # Repeated form: each --add-dir takes exactly one value, so the option
        # after it cannot be swallowed. Run 4 stopped here anyway, because the
        # model opened with "Part 1 -- Write:" instead of the prefix, while its
        # argv carried --append-system-prompt intact and grant_c was written.
        ("repeated", "Part 1 - Write:\n1. workspace/... - success", True),
        ("repeated", "ROLE-ADDDIR-CLAUDE-OK done", True),
        # Variadic form: the question is real. Swallowing removes the system
        # prompt entirely, so total absence is the signal -- not whether the
        # model chose to lead with it.
        ("variadic", "Part 1 - Write:\n1. ROLE-ADDDIR-CLAUDE-OK later", True),
        ("variadic", "Part 1 - Write: no role text anywhere", False),
    ],
)
def test_following_option_check_only_applies_to_the_variadic_form(
    form: str,
    response: str,
    expected: bool,
) -> None:
    assert following_option_parsed(form, response) is expected


def test_failure_detail_reads_the_event_stream_not_only_stderr() -> None:
    """Codex reports fatal errors as stdout events and leaves stderr empty.

    The third run stopped with `rc=1; stderr=''` while the actual cause -- a
    model the API rejected with a 400 -- sat in the event stream. Three runs
    have now produced a durable report that omitted its own diagnosis.
    """

    events = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {
                "type": "error",
                "message": "Model metadata for `gpt-5.4` not found.",
            },
        },
        {
            "type": "turn.failed",
            "error": {"message": "The 'gpt-5.4' model is not supported"},
        },
    ]

    detail = failure_detail(Invocation([], 1, 0.0, events, ""))

    assert "gpt-5.4" in detail
    assert "not supported" in detail


def test_failure_detail_falls_back_to_stderr_when_the_stream_is_silent() -> None:
    detail = failure_detail(Invocation([], 1, 0.0, [], "boom on stderr"))

    assert "boom on stderr" in detail


def test_the_report_explains_that_a_placeholder_absorbs_one_slash() -> None:
    """`<PROJECT>` stands for a path that already starts with a slash.

    So `Edit(//abs/p/**)` renders as `Edit(/<PROJECT>/**)` and reads exactly
    like the single-slash defect this spike exists to have fixed. The rendering
    is faithful and must not be doctored; the report has to say so, or the next
    reader "corrects" a correct rule back into a broken one.
    """

    rendered = sanitize("Edit(//abs/p/grant-a/**)", {"/abs/p": "<PROJECT>"})
    assert rendered == "Edit(/<PROJECT>/grant-a/**)"

    explanation = findings_section(Report())
    assert "including its leading slash" in explanation
    assert "not the single-slash form" in explanation


def test_findings_upsert_replaces_one_marked_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = tmp_path / "FINDINGS.md"
    findings.write_text("# Findings\n", encoding="utf-8")
    monkeypatch.setattr(add_dir, "FINDINGS", findings)

    upsert_findings("first")
    upsert_findings("second")

    rendered = findings.read_text(encoding="utf-8")
    assert rendered.count("<!-- ADD-DIR-SPIKE:START -->") == 1
    assert rendered.count("<!-- ADD-DIR-SPIKE:END -->") == 1
    assert "second" in rendered
    assert "first" not in rendered

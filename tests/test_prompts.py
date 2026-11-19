"""Editable prompt templates: validation, rendering, catalogue, storage and resolution."""

from __future__ import annotations

import pytest

from app.prompts import Placeholder, PromptError, PromptSpec, render_template, validate_template

SPEC = PromptSpec(
    id="demo", title="Demo prompt", group="Chat", used_in="tests",
    default="Read {context}. {rules}",
    placeholders=(
        Placeholder("context", "the staged file"),
        Placeholder("rules", "verdict rules", locked=True, sample="Say Converged."),
        Placeholder("extra", "optional note", required=False),
    ),
)


@pytest.mark.parametrize(
    "text, message",
    [
        ("   ", "Demo prompt must not be empty"),
        ("x" * 10_001, "Demo prompt must be at most 10,000 characters"),
        ("{context} {rules}\x00", "Demo prompt contains invalid characters"),
        ("{context} {rules} {contxt}", "Demo prompt has an unknown placeholder {contxt}"),
        ("{rules}", "Demo prompt must contain {context} exactly once"),
        ("{context} {context} {rules}", "Demo prompt must contain {context} exactly once"),
        ("{context} {rules} {extra}{extra}", "Demo prompt may contain {extra} at most once"),
    ],
)
def test_validation_messages(text: str, message: str) -> None:
    with pytest.raises(PromptError) as error:
        validate_template(SPEC, text)
    assert str(error.value) == message


def test_a_non_string_template_is_refused() -> None:
    with pytest.raises(PromptError, match="Demo prompt must not be empty"):
        validate_template(SPEC, 7)


def test_rendering_is_one_pass_and_keeps_other_braces() -> None:
    text = 'Keep {"json": 1}. Read {context}. {rules}'
    assert validate_template(SPEC, text) == text
    rendered = render_template(SPEC, text, {"context": "{rules}", "rules": "R", "extra": ""})
    assert rendered == 'Keep {"json": 1}. Read {rules}. R'


def test_rendering_needs_every_declared_value() -> None:
    with pytest.raises(KeyError):
        render_template(SPEC, SPEC.default, {"context": "c"})


def test_validation_on_generated_text_returns_it_or_raises_prompt_error() -> None:
    import random

    rng = random.Random(20261119)
    pieces = ["{context}", "{rules}", "{extra}", "{x}", "{", "}", "{{", "a", " ", "\x00", "{Context}", "é"]
    for _ in range(2000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
        try:
            assert validate_template(SPEC, text) == text
        except PromptError:
            continue
        assert render_template(SPEC, text, {"context": "C", "rules": "R", "extra": ""}).count("R") >= 1


IDS = (
    "pass", "compact", "stateless_claude", "stateless_codex", "chat_skills", "auto_preparation",
    "auto_discussion", "auto_compaction", "build_turn", "docker_lima", "docker_local",
    "build_handover", "skills_turn", "guidance_gate", "disagreement", "questions_read",
    "questions_ask", "notes", "notes_reading_live", "notes_reading_copy", "downloads",
    "downloads_manual", "downloads_reread", "convert", "convert_reread", "model_check", "global_header",
)


def test_the_catalogue_has_every_prompt_once_and_valid_defaults() -> None:
    from app.prompts import CATALOGUE

    assert tuple(CATALOGUE) == IDS
    for spec in CATALOGUE.values():
        assert validate_template(spec, spec.default) == spec.default
        assert all(p.sample or not p.locked for p in spec.placeholders), spec.id


def make_store(tmp_path):
    from app.storage import ProjectStore, RegistryStore

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("P", project_dir)
    return ProjectStore(project)


def test_resolution_prefers_project_then_global_then_built_in(tmp_path) -> None:
    import json

    from app.prompts import CATALOGUE, load_prompts, save_global_prompt

    store = make_store(tmp_path)
    home = tmp_path / "home"
    assert load_prompts(home, store).source("compact") == "built-in"

    save_global_prompt(home, "compact", "Global summary.")
    assert load_prompts(home, store).render("compact") == "Global summary."
    assert load_prompts(home, store).source("compact") == "global"
    assert (home / "prompts.json").stat().st_mode & 0o777 == 0o600

    store.set_prompt_override("compact", "Project summary.")
    assert load_prompts(home, store).render("compact") == "Project summary."
    assert load_prompts(home, store).source("compact") == "project"
    assert load_prompts(home, None).render("compact") == "Global summary."

    store.set_prompt_override("compact", None)
    save_global_prompt(home, "compact", None)
    assert load_prompts(home, store).template("compact") == CATALOGUE["compact"].default
    assert json.loads((home / "prompts.json").read_text())["prompts"] == {}


def test_a_legacy_pass_template_is_the_pass_override_and_moves_on_write(tmp_path) -> None:
    import json

    store = make_store(tmp_path)
    manifest = json.loads(store.manifest_path.read_text())
    manifest["pass_prompt_template"] = "Old {source_path}"
    store.manifest_path.write_text(json.dumps(manifest))

    assert store.prompt_overrides() == {"pass": "Old {source_path}"}
    store.set_prompt_override("compact", "Shorter.")

    saved = json.loads(store.manifest_path.read_text())
    assert "pass_prompt_template" not in saved
    assert saved["prompt_overrides"] == {"pass": "Old {source_path}", "compact": "Shorter."}


def test_full_reset_drops_the_overrides(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_prompt_override("compact", "Shorter.")
    store.reset_settings()
    assert store.prompt_overrides() == {}


def test_malformed_overrides_are_refused(tmp_path) -> None:
    import json

    from app.storage import StorageError

    store = make_store(tmp_path)
    manifest = json.loads(store.manifest_path.read_text())
    manifest["prompt_overrides"] = {"compact": 3}
    store.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(StorageError, match="project prompt overrides are invalid"):
        store.prompt_overrides()


def test_an_invalid_stored_prompt_refuses_with_its_title(tmp_path) -> None:
    import json

    from app.prompts import load_prompts

    home = tmp_path / "home"
    home.mkdir()
    (home / "prompts.json").write_text(json.dumps(
        {"format": "delibra-prompts/1", "prompts": {"auto_discussion": "No placeholders"}}
    ))
    with pytest.raises(PromptError) as error:
        load_prompts(home, None)
    assert str(error.value) == (
        "Discussion turn prompt is invalid: Discussion turn must contain {context} exactly once; "
        "fix it in Settings"
    )


def test_saving_one_prompt_keeps_every_other_entry_even_an_invalid_one(tmp_path) -> None:
    import json

    from app.prompts import CATALOGUE, load_prompts, save_global_prompt

    home = tmp_path / "home"
    home.mkdir()
    (home / "prompts.json").write_text(json.dumps({"format": "delibra-prompts/1", "prompts": {
        "auto_discussion": "No placeholders", "compact": "Valid summary.",
    }}))
    third = CATALOGUE["model_check"].default + "\nOne more line."

    save_global_prompt(home, "model_check", third)

    stored = json.loads((home / "prompts.json").read_text())["prompts"]
    assert stored == {"auto_discussion": "No placeholders", "compact": "Valid summary.", "model_check": third}
    save_global_prompt(home, "auto_discussion", None)  # Reset repairs the invalid entry
    assert load_prompts(home, None).render("compact") == "Valid summary."


@pytest.mark.parametrize(
    "content, message",
    [
        (b'{"format": "delibra-prompts/1", "prompts": {"compact": "Mine."', "not valid JSON; fix or remove"),
        (b'{"format": "other", "prompts": {}}', "has an unknown format; fix or remove"),
        (b'{"format": "delibra-prompts/1", "prompts": {}}' + b" " * (256 * 1024), "larger than 256 KB"),
    ],
)
def test_an_unreadable_prompts_file_refuses_saves_and_keeps_its_bytes(tmp_path, content, message) -> None:
    from app.prompts import load_prompts, save_global_prompt

    home = tmp_path / "home"
    home.mkdir()
    path = home / "prompts.json"
    path.write_bytes(content)

    with pytest.raises(PromptError, match=message):
        save_global_prompt(home, "notes", None)
    with pytest.raises(PromptError, match=message):
        load_prompts(home, None)
    assert path.read_bytes() == content


def test_saving_refuses_unknown_ids_and_bad_templates(tmp_path) -> None:
    from app.prompts import save_global_prompt

    home = tmp_path / "home"
    with pytest.raises(PromptError):
        save_global_prompt(home, "nope", "x")
    with pytest.raises(PromptError):
        save_global_prompt(home, "nope", None)
    with pytest.raises(PromptError):
        save_global_prompt(home, "auto_discussion", "Read {context}.")
    assert not (home / "prompts.json").exists()


def _call_sites(prompts, w):
    """Each catalogued prompt's real call site, rendered with `prompts`."""

    from pathlib import Path

    from app.agents.base import RunContext
    from app.agents.claude import ClaudeAdapter
    from app.agents.codex import CodexAdapter
    from app.auto import (
        build_turn_prompt, compaction_prompt, disagreement_note, discussion_prompt, docker_note,
        guidance_gate_note, preparation_prompt, skills_turn_note,
    )
    from app.build_handover import render_handover
    from app.coding import DownloadPolicy
    from app.download_manager import turn_spool_notes
    from app.download_model_check import check_prompt
    from app.notes import notes_prompt
    from app.questions import questions_prompt
    from app.skill_folders import chat_skills_note
    from tests.prompt_cases import _config

    context = RunContext(user_prompt="Q", resume_id=None, resume_strategy="stateless", staged_history=[],
                         staged_source=None, global_instruction="G", prompts=prompts)
    policy = DownloadPolicy(enabled=True, approval="manual", convert=True)
    return {
        "pass": prompts.template("pass"),
        "compact": prompts.render("compact"),
        "stateless_claude": ClaudeAdapter().build_command(_config("claude"), context).stdin,
        "stateless_codex": CodexAdapter().build_command(_config("codex"), context).stdin,
        "chat_skills": chat_skills_note(w / "i.md", prompts=prompts),
        "auto_preparation": preparation_prompt("c.md", prompts=prompts),
        "auto_discussion": discussion_prompt("c.md", prompts=prompts),
        "auto_compaction": compaction_prompt("c.md", prompts=prompts),
        "build_turn": build_turn_prompt("c.md", prompts=prompts),
        "docker_lima": docker_note("lima", prompts=prompts),
        "docker_local": docker_note("local", prompts=prompts),
        "build_handover": render_handover("A", "r", b"", [], prompts=prompts).decode(),
        "skills_turn": skills_turn_note(w / "s", "c.md", prompts=prompts),
        "guidance_gate": guidance_gate_note(True, prompts=prompts),
        "disagreement": disagreement_note(True, prompts=prompts),
        "questions_read": questions_prompt(w / "q.json", None, pause_enabled=True, prompts=prompts),
        "questions_ask": questions_prompt(w / "q.json", w / "o", pause_enabled=True, prompts=prompts),
        "notes": notes_prompt(w / "n.json", live=True, marks_path=None, prompts=prompts),
        "notes_reading_live": notes_prompt(w / "n.json", live=True, marks_path=None, prompts=prompts),
        "notes_reading_copy": notes_prompt(w / "n.json", live=False, marks_path=None, prompts=prompts),
        "downloads": "".join(turn_spool_notes(w, policy, agent="codex", build=True, prompts=prompts)),
        "downloads_manual": "".join(turn_spool_notes(w, policy, agent="codex", build=True, prompts=prompts)),
        "downloads_reread": "".join(turn_spool_notes(
            w, DownloadPolicy(enabled=True, approval="automatic", convert=False), agent="claude", build=False,
            prompts=prompts)),
        "convert": "".join(turn_spool_notes(w, policy, agent="codex", build=True, prompts=prompts)),
        "convert_reread": "".join(turn_spool_notes(
            w, DownloadPolicy(enabled=False, convert=True), agent="claude", build=False, prompts=prompts)),
        "model_check": check_prompt([("D1", "https://example.org")], prompts=prompts),
        "global_header": ClaudeAdapter().build_command(_config("claude"), context).stdin,
    }


def test_an_edited_template_reaches_every_call_site(tmp_path) -> None:
    from app.prompts import CATALOGUE, Prompts

    edited = {
        spec.id: "EDITED-" + spec.id.upper() + " " + " ".join(
            "{" + p.name + "}" for p in spec.placeholders if p.required or p.locked
        )
        for spec in CATALOGUE.values()
    }
    for scope in ("global", "project"):
        prompts = Prompts(edited, {}) if scope == "global" else Prompts({}, edited)
        (tmp_path / scope).mkdir()
        shown = _call_sites(prompts, tmp_path / scope)
        assert set(shown) == set(CATALOGUE)
        for prompt_id, text in shown.items():
            assert "EDITED-" + prompt_id.upper() in text, (scope, prompt_id)


def test_compare_rows_mark_what_only_the_project_or_the_global_has() -> None:
    from app.prompts import compare_rows

    rows = compare_rows("Read {context}.\nBe brief.\nEnd.", "Read {context}.\nBe very brief.\nEnd.")
    assert rows == [
        ("same", "Read {context}."), ("project", "Be brief."), ("global", "Be very brief."), ("same", "End."),
    ]
    assert compare_rows("a", "a") == [("same", "a")]


def test_a_legacy_pass_template_with_other_tokens_keeps_working(tmp_path) -> None:
    import json

    from app.prompts import CATALOGUE, load_prompts, pass_template

    legacy = "Review {source_path}. Answer as {json}."
    assert validate_template(CATALOGUE["pass"], legacy) == legacy
    store = make_store(tmp_path)
    manifest = json.loads(store.manifest_path.read_text())
    manifest["pass_prompt_template"] = legacy
    store.manifest_path.write_text(json.dumps(manifest))
    home = tmp_path / "home"

    assert load_prompts(home, store).template("pass") == legacy  # turns are not refused
    assert pass_template(home, store) == legacy  # the dialog is prefilled with it
    assert render_template(CATALOGUE["pass"], legacy, {"source_path": "s.md", "source_session": "A",
                                                       "source_round": "1"}) == "Review s.md. Answer as {json}."


def test_an_invalid_pass_override_is_named_once() -> None:
    from app.prompts import _stored

    with pytest.raises(PromptError) as error:
        _stored({"pass": "No path here."})
    assert str(error.value).startswith("Pass prompt is invalid: Pass prompt must contain {source_path}")


def test_a_save_that_would_make_the_file_too_large_is_refused_before_writing(tmp_path) -> None:
    from app.prompts import CATALOGUE, load_prompts, save_global_prompt

    home = tmp_path / "home"
    long_text = "Tóm tắt lịch sử hội thoại đã được chuẩn bị. " * 220  # ~9,900 chars, ~13 KB of UTF-8
    saved = []
    for spec in list(CATALOGUE.values()):
        tokens = " ".join("{" + p.name + "}" for p in spec.placeholders if p.required or p.locked)
        text = long_text[: 10_000 - len(tokens)] + tokens
        try:
            save_global_prompt(home, spec.id, validate_template(spec, text))
            saved.append(spec.id)
        except PromptError as exc:
            assert str(exc) == (
                "all changed prompts together would be larger than 256 KB; shorten or reset some first"
            )
            break
    else:
        pytest.fail("the size limit was never reached")
    assert (home / "prompts.json").stat().st_size <= 256 * 1024
    assert sorted(load_prompts(home, None)._global) == sorted(saved)  # still readable


@pytest.mark.parametrize("kind", ["unreadable", "directory"])
def test_a_prompts_file_that_cannot_be_opened_is_a_prompt_error_without_paths(tmp_path, kind) -> None:
    import os

    from app.prompts import load_prompts, save_global_prompt

    home = tmp_path / "home"
    home.mkdir()
    path = home / "prompts.json"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_text("{}")
        os.chmod(path, 0)
    try:
        for action in (lambda: load_prompts(home, None), lambda: save_global_prompt(home, "compact", None)):
            with pytest.raises(PromptError) as error:
                action()
            message = str(error.value)
            assert message.startswith("the global prompts file in Delibra's home folder cannot be read: ")
            assert message.endswith("; fix or remove it") and str(tmp_path) not in message
    finally:
        if kind == "unreadable":
            os.chmod(path, 0o600)


def test_file_level_messages_name_no_path(tmp_path) -> None:
    from app.prompts import load_prompts

    home = tmp_path / "home"
    home.mkdir()
    (home / "prompts.json").write_text("{bad")
    with pytest.raises(PromptError) as error:
        load_prompts(home, None)
    assert str(error.value) == "prompts.json is not valid JSON; fix or remove it"


def test_rendered_defaults_leave_no_declared_token_and_the_catalogue_is_read_only() -> None:
    from app.prompts import CATALOGUE, preview

    for spec in CATALOGUE.values():
        rendered = preview(spec, spec.default)
        assert not any("{" + name + "}" in rendered for name in spec.names), spec.id
    with pytest.raises(TypeError):
        CATALOGUE["extra"] = CATALOGUE["pass"]  # type: ignore[index]


def test_only_chat_pass_and_discussion_only_prompts_are_lan_editable() -> None:
    from app.prompts import CATALOGUE

    assert {spec.id for spec in CATALOGUE.values() if spec.lan_editable} == {
        "pass", "compact", "chat_skills", "auto_preparation", "auto_discussion",
    }

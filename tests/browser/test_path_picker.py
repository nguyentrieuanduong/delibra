"""Choose files… builds a preset's fixed file list in the Browse pop-up."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest

from tests.browser.conftest import LiveServer


def _skills(home: Path) -> None:
    (home / "skills" / "tdd").mkdir(parents=True)
    (home / "skills" / "tdd" / "SKILL.md").write_text("# TDD\n")
    (home / "skills" / "a.md").write_text("# A\n")


def _settled(dialog: Any, region: str) -> Any:
    # htmx wires a swapped region's buttons when its 20 ms settle ends and
    # removes `htmx-settling` then; a click before that does nothing.
    return dialog.locator(f"#{region}:not(.htmx-settling)")


def _open_folder(dialog: Any, name: str) -> None:
    _settled(dialog, "path-picker-body").get_by_role("button", name=name).click()


def _set_preset_files(page: Any, row: int, value: str) -> None:
    # The list field is hidden: in the UI only Use these files writes it.
    page.evaluate(
        "([row, value]) => { document.getElementById(`preset-files-${row}`).value = value; }",
        [row, value],
    )


def _save(page: Any) -> None:
    # POST /settings answers 303 to /settings; wait for that page to load.
    with page.expect_navigation():
        page.click("button:has-text('Save global settings')")


@pytest.mark.browser
def test_choose_files_builds_a_fixed_list_that_save_stores(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _skills(tmp_path)
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/settings")
    page.fill("input[name='preset_name_0']", "suite")
    assert page.locator("textarea[name^='preset_']").count() == 0

    page.click("[data-picker-target='preset-files-0']")
    dialog = page.locator("#path-picker")
    files = dialog.locator("#picker-files")
    _settled(dialog, "picker-files").get_by_text("No files yet.").wait_for()
    _open_folder(dialog, "skills/")
    dialog.locator("input[data-picker-value='~/skills/tdd']").check()
    dialog.locator("input[data-picker-value='~/skills/a.md']").check()
    dialog.locator("[data-picker-add-files]").click()
    files.get_by_text("2 files,").wait_for()
    assert files.locator("code").all_inner_texts() == ["~/skills/tdd/SKILL.md", "~/skills/a.md"]
    assert dialog.locator("input[data-picker-value]:checked").count() == 0

    _settled(dialog, "picker-files").get_by_role("button", name="Remove ~/skills/a.md").click()
    files.get_by_text("1 file,").wait_for()
    dialog.locator("[data-path-picker-use]").click()

    assert not dialog.is_visible()
    assert page.input_value("#preset-files-0") == "~/skills/tdd/SKILL.md"
    row = page.locator("#preset-files-list-0")
    row.get_by_text("~/skills/tdd/SKILL.md").wait_for()
    assert "Remove" not in row.inner_text()
    _save(page)
    saved = json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))
    assert saved["guidance_presets"] == {"suite": {"files": ["~/skills/tdd/SKILL.md"]}}


@pytest.mark.browser
def test_a_refused_save_keeps_the_chosen_files_and_marks_the_preset(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _skills(tmp_path)
    app = live_server(["success"], tmp_path)
    page.goto(f"{app['base_url']}/settings")
    page.click("[data-picker-target='preset-files-0']")
    dialog = page.locator("#path-picker")
    _settled(dialog, "picker-files").get_by_text("No files yet.").wait_for()
    _open_folder(dialog, "skills/")
    dialog.locator("input[data-picker-value='~/skills/a.md']").check()
    dialog.locator("[data-picker-add-files]").click()
    _settled(dialog, "picker-files").get_by_text("1 file,").wait_for()
    _settled(dialog, "picker-files").locator("[data-path-picker-use]").click()

    alerts: list[str] = []

    def accept(dialog: Any) -> None:
        alerts.append(f"{dialog.type}: {dialog.message}")
        dialog.accept()

    page.on("dialog", accept)
    _save(page)
    page.wait_for_function("() => !document.querySelector('[data-page-alert]')")

    assert alerts == [
        "alert: Nothing was saved: Guidance preset name '' must be 1-40 printable "
        "characters without surrounding spaces"
    ]
    preset = page.locator("fieldset.guidance-preset").first
    assert preset.get_by_role("alert").inner_text().startswith("Guidance preset name '' must")
    assert page.get_by_role("alert").count() == 1
    assert page.input_value("#preset-files-0") == "~/skills/a.md"
    assert preset.locator("code").all_inner_texts() == ["~/skills/a.md"]
    assert not (tmp_path / "home" / "config.json").exists()

    page.fill("input[name='preset_name_0']", "suite")
    _save(page)
    saved = json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))
    assert saved["guidance_presets"] == {"suite": {"files": ["~/skills/a.md"]}}
    assert page.get_by_role("alert").count() == 0
    assert len(alerts) == 1


@pytest.mark.browser
def test_choose_skills_fills_the_second_list(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _skills(tmp_path)
    (tmp_path / "skills" / "tdd" / "references").mkdir()
    (tmp_path / "skills" / "tdd" / "references" / "r.md").write_text("ref\n")
    app = live_server(["success"], tmp_path)
    page.goto(f"{app['base_url']}/settings")
    page.fill("input[name='preset_name_0']", "suite")
    _set_preset_files(page, 0, "~/skills/a.md")

    page.click("[data-picker-target='preset-skills-0']")
    dialog = page.locator("#path-picker")
    files = dialog.locator("#picker-files")
    _settled(dialog, "picker-files").get_by_text("Will include: agents can read").wait_for()
    _open_folder(dialog, "skills/")
    dialog.locator("input[data-picker-value='~/skills/tdd']").check()
    dialog.locator("input[data-picker-subdirs]").check()
    dialog.locator("[data-picker-add-files]").click()
    files.get_by_text("2 files,").wait_for()
    assert files.locator("code").all_inner_texts() == [
        "~/skills/tdd/SKILL.md",
        "~/skills/tdd/references/r.md",
    ]
    assert "(limit 512 files, 4096 KB)" in files.inner_text()
    _settled(dialog, "picker-files").locator("[data-path-picker-use]").click()

    assert page.input_value("#preset-skills-0") == "~/skills/tdd/SKILL.md\n~/skills/tdd/references/r.md"
    assert page.input_value("#preset-files-0") == "~/skills/a.md"
    page.locator("#preset-skills-list-0").get_by_text("(limit 512 files, 4096 KB)").wait_for()
    _save(page)
    saved = json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))
    assert saved["guidance_presets"] == {
        "suite": {
            "files": ["~/skills/a.md"],
            "skills": ["~/skills/tdd/SKILL.md", "~/skills/tdd/references/r.md"],
        }
    }


@pytest.mark.browser
def test_remove_all_empties_the_list_in_one_click(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _skills(tmp_path)
    app = live_server(["success"], tmp_path)
    page.goto(f"{app['base_url']}/settings")
    _set_preset_files(page, 0, "~/skills/a.md\n~/skills/tdd/SKILL.md")
    page.click("[data-picker-target='preset-files-0']")
    dialog = page.locator("#path-picker")
    files = dialog.locator("#picker-files")
    files.get_by_text("2 files,").wait_for()

    _settled(dialog, "picker-files").get_by_role("button", name="Remove all").click()
    files.get_by_text("No files yet.").wait_for()
    assert files.get_by_role("button", name="Remove all").count() == 0
    _settled(dialog, "picker-files").locator("[data-path-picker-use]").click()

    assert not dialog.is_visible()
    assert page.input_value("#preset-files-0") == ""
    page.locator("#preset-files-list-0").get_by_text("No files yet.").wait_for()


@pytest.mark.browser
def test_dialog_checkboxes_share_a_line_with_their_names(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _skills(tmp_path)
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/settings")
    _set_preset_files(page, 0, "~/skills/a.md")
    page.click("[data-picker-target='preset-files-0']")
    page.locator("#path-picker input[data-picker-value='~/skills/a.md']").wait_for()

    rows = page.evaluate(
        """() => Array.from(document.querySelectorAll(
          "#path-picker .path-picker-entry, #path-picker label.auto-choice"
        )).filter((row) => row.querySelector("input[type=checkbox]")).map((row) => {
          const box = row.querySelector("input[type=checkbox]").getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(row);
          const text = Array.from(range.getClientRects()).filter((line) => line.left >= box.right - 0.5);
          return {
            text: row.textContent.trim(),
            textMiddle: text.length ? (text[0].top + text[0].bottom) / 2 : null,
            boxMiddle: (box.top + box.bottom) / 2,
          };
        })"""
    )
    assert [row["text"].split()[0] for row in rows] == ["tdd/", "a.md", "Show", "Include"], rows
    for row in rows:
        assert row["textMiddle"] is not None, row
        assert abs(row["textMiddle"] - row["boxMiddle"]) <= 6, row


RECORD_PICKER_SWAPS = """() => {
  window.pickerSwaps = [];
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target && event.detail.target.id === "path-picker-body") {
      window.pickerSwaps.push(event.detail.shouldSwap);
    }
  });
}"""


def _held_listing(page: Any, pattern: re.Pattern[str]) -> list[Any]:
    """Routes matching `pattern` wait until the test continues them."""

    held: list[Any] = []
    page.route(pattern, lambda route: held.append(route))
    return held


def _wait_until_held(page: Any, held: list[Any]) -> None:
    for _ in range(100):
        if held:
            return
        page.wait_for_timeout(50)
    raise AssertionError("the listing request never arrived")


@pytest.mark.browser
def test_a_reopened_picker_never_shows_or_applies_an_earlier_listing(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _skills(tmp_path)
    app = live_server(["success"], tmp_path)
    page.goto(f"{app['base_url']}/settings")
    page.fill("input[name='preset_name_0']", "suite")
    _set_preset_files(page, 0, "~/skills/tdd/SKILL.md")
    _save(page)
    page.evaluate(RECORD_PICKER_SWAPS)
    dialog = page.locator("#path-picker")

    page.click("[data-picker-target='preset-files-1']")
    dialog.locator("[data-picker-add-files]").wait_for()
    dialog.get_by_role("button", name="Cancel").click()

    pattern = re.compile(r"/browse\?")
    held = _held_listing(page, pattern)
    page.click("[data-picker-target='preset-files-0']")
    _wait_until_held(page, held)
    # The last listing's Add is gone while this opening's listing is pending.
    assert dialog.is_visible()
    assert dialog.locator("[data-picker-add-files]").count() == 0
    assert "Loading…" in dialog.locator("#path-picker-body").inner_text()

    page.keyboard.press("Escape")
    held[0].continue_()
    page.unroute(pattern)
    page.wait_for_function("window.pickerSwaps.includes(false)")
    assert not dialog.is_visible()

    page.click("[data-picker-target='preset-files-1']")
    _settled(dialog, "picker-files").get_by_text("No files yet.").wait_for()
    dialog.locator("input[data-picker-value='~/skills']").check()
    dialog.locator("[data-picker-add-files]").click()
    dialog.locator("#picker-files").get_by_text("1 file,").wait_for()
    dialog.locator("[data-path-picker-use]").click()

    assert page.input_value("#preset-files-1") == "~/skills/a.md"
    assert page.input_value("#preset-files-0") == "~/skills/tdd/SKILL.md"
    assert page.evaluate("window.pickerSwaps") == [True, False, True]


@pytest.mark.browser
def test_browse_chooses_a_folder_to_register(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "repos" / "new-project").mkdir(parents=True)
    app = live_server(["success"], tmp_path)
    page.goto(f"{app['base_url']}/")
    page.click("summary:has-text('Register project')")

    page.click("[data-picker-target='register-path']")
    dialog = page.locator("#path-picker")
    _open_folder(dialog, "repos/")
    _open_folder(dialog, "new-project/")
    dialog.get_by_role("button", name="Choose ~/repos/new-project").click()

    assert page.input_value("#register-path") == str(tmp_path / "repos" / "new-project")
    assert not dialog.is_visible()


@pytest.mark.browser
def test_browse_adds_markdown_to_coding_guidance(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    guides = tmp_path / "guides"
    guides.mkdir()
    (guides / "a.md").write_text("# A\n")
    (guides / "b.md").write_text("# B\n")
    app = live_server(["success"], tmp_path)
    page.goto(f"{app['base_url']}/projects/Verify/settings")

    page.click("[data-picker-target='coding-guidance-files']")
    dialog = page.locator("#path-picker")
    _open_folder(dialog, "guides/")
    dialog.locator(f"input[data-picker-value='{guides / 'a.md'}']").wait_for()
    dialog.locator("[data-picker-select-all]").check()
    dialog.locator("[data-path-picker-add]").click()

    assert page.input_value("#coding-guidance-files") == f"{guides / 'a.md'}\n{guides / 'b.md'}"


@pytest.mark.browser
def test_browse_adds_project_folders_to_writable_roots(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "src" / "app").mkdir(parents=True)
    page.goto(f"{app['base_url']}/projects/Verify/settings")
    edit_target = f"session-writable-roots-{app['session_ids'][0]}"
    assert page.locator(f"[data-picker-target='{edit_target}']").count() == 1

    page.click("[data-picker-target='project-writable-roots']")
    dialog = page.locator("#path-picker")
    _open_folder(dialog, "src/")
    dialog.locator("input[data-picker-value='src/app']").check()
    dialog.locator("[data-path-picker-add]").click()

    assert page.input_value("#project-writable-roots").splitlines()[-1] == "src/app"


@pytest.mark.browser
def test_a_listing_opened_for_one_field_never_fills_another(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "guides").mkdir()
    (tmp_path / "guides" / "a.md").write_text("# A\n")
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "src").mkdir(parents=True)
    page.goto(f"{app['base_url']}/projects/Verify/settings")
    guidance_before = page.input_value("#coding-guidance-files")
    dialog = page.locator("#path-picker")

    page.click("[data-picker-target='coding-guidance-files']")
    dialog.locator("[data-path-picker-add]").wait_for()
    dialog.get_by_role("button", name="Cancel").click()

    pattern = re.compile(r"/projects/Verify/browse\?")
    held = _held_listing(page, pattern)
    page.click("[data-picker-target='project-writable-roots']")
    _wait_until_held(page, held)
    # The guidance listing's Add cannot reach the writable-roots field.
    assert dialog.locator("[data-path-picker-add], [data-path-picker-choose]").count() == 0
    held[0].continue_()
    page.unroute(pattern)
    dialog.locator("input[data-picker-value='src']").check()
    dialog.locator("[data-path-picker-add]").click()

    assert page.input_value("#project-writable-roots").splitlines()[-1] == "src"
    assert page.input_value("#coding-guidance-files") == guidance_before


@pytest.mark.browser
def test_browse_adds_markdown_to_the_skills_box(
    live_server: LiveServer, page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / "suite" / "tdd"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# TDD\n")
    app = live_server(["success"], tmp_path)
    page.goto(f"{app['base_url']}/projects/Verify/settings")

    page.click("[data-picker-target='coding-guidance-skills']")
    dialog = page.locator("#path-picker")
    _open_folder(dialog, "suite/")
    _open_folder(dialog, "tdd/")
    dialog.locator(f"input[data-picker-value='{skill / 'SKILL.md'}']").check()
    dialog.locator("[data-path-picker-add]").click()

    assert page.input_value("#coding-guidance-skills") == str(skill / "SKILL.md")
    assert page.input_value("#coding-guidance-files") == ""

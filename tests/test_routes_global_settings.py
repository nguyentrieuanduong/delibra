from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi.testclient import TestClient
from markupsafe import escape
import pytest

from app.config import GuidancePreset, Settings
from app.main import create_app

LOCAL = ("127.0.0.1", 5000)
LAN = ("192.168.1.20", 5000)


@pytest.fixture(autouse=True)
def no_access_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELIBRA_ALLOWED_CLIENTS", raising=False)
    monkeypatch.delenv("DELIBRA_ALLOWED_HOSTS", raising=False)


def make_app(tmp_path: Path, **overrides):
    settings = Settings(home=tmp_path / "home", run_timeout=2, **overrides)
    return create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )


def config(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))


def form(**fields: str) -> dict[str, str]:
    data = {"allowed_clients": "", "allowed_hosts": "", "preset_count": "1"}
    data.update(fields)
    return data


def test_only_this_machine_may_open_or_save_global_settings(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        page = client.get("/settings")
        saved_access = client.post("/settings/access", data=form())
        saved_presets = client.post("/settings/presets", data=form())
        index = client.get("/").text

    assert page.status_code == 403
    assert saved_access.status_code == 403
    assert saved_presets.status_code == 403
    assert page.json()["detail"] == "Only this machine may view or change global settings"
    assert 'href="/settings"' not in index
    assert not (tmp_path / "home" / "config.json").exists()


def test_the_page_shows_current_values_and_one_empty_preset(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "a.md").write_text("x" * 2000)
    app = make_app(
        tmp_path,
        allowed_clients=("192.168.1.0/24",),
        guidance_presets=(GuidancePreset("code-craft", ("~/skills/a.md", "/opt/delibra-gone.md")),),
    )
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        access = client.get("/settings").text
        page = client.get("/settings/presets").text
        index = client.get("/").text

    assert 'href="/settings"' in index
    assert ">192.168.1.0/24</textarea>" in access
    assert 'name="preset_name_0" value="code-craft"' in page
    assert (
        'id="preset-files-0" name="preset_files_0" value="~/skills/a.md\n/opt/delibra-gone.md"'
    ) in page
    assert "<code>~/skills/a.md</code> 2 KB" in page
    assert '<code>/opt/delibra-gone.md</code> <span class="warning">missing</span>' in page
    assert "2 files, " in page
    assert "preset_paths_" not in page
    assert "preset_subdirs_" not in page
    assert (
        'data-picker-target="preset-files-0" data-picker-files-list="preset-files-list-0" '
        'data-picker-list="files" aria-controls="path-picker">Choose pasted files…</button>'
    ) in page
    assert (
        'data-picker-target="preset-skills-0" data-picker-files-list="preset-skills-list-0" '
        'data-picker-list="skills" aria-controls="path-picker">Choose skills…</button>'
    ) in page
    assert '<div id="preset-skills-list-0" class="preset-files" aria-live="polite">' in page
    assert page.count('<p class="preset-list-heading">Pasted every turn</p>') == 2
    assert page.count('<p class="preset-list-heading">Agents can read</p>') == 2
    assert 'name="preset_name_1" value=""' in page
    assert "No files yet." in page
    assert 'name="preset_count" value="2"' in page
    assert 'id="path-picker"' in page
    # Approval steps are handed to the other agents (app/auto.py guidance_gate_note).
    assert (
        "Approval steps in guidance (for example brainstorming's design approval or "
        "writing-plans' plan approval) do not stop a build run: the other agents approve "
        "instead, and you are asked only if they have not agreed within 3 cycles."
    ) in page
    assert (
        "Leave out finishing-work and using-git-worktrees: they merge, push or work "
        "outside the project, and Delibra makes the build commits itself."
    ) in page
    assert "stall a build run" not in page


def test_saving_writes_config_keeps_other_keys_and_applies_presets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "a.md").write_text("# A\n")
    (tmp_path / "b.md").write_text("# B\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"extra": 1}), encoding="utf-8")
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved_access = client.post(
            "/settings/access",
            data={"allowed_clients": "", "allowed_hosts": "my-mac.local\n\n192.168.1.0/24"},
            follow_redirects=False,
        )
        saved = client.post(
            "/settings/presets",
            data={
                "preset_count": "2",
                "preset_name_0": " zeta ",
                "preset_files_0": f"~/skills/a.md\n{tmp_path / 'b.md'}\n",
                "preset_name_1": "",
                "preset_files_1": "",
            },
            follow_redirects=False,
        )

    assert (saved_access.status_code, saved_access.headers["location"]) == (303, "/settings")
    assert (saved.status_code, saved.headers["location"]) == (303, "/settings/instruction")
    assert config(tmp_path) == {
        "extra": 1,
        "allowed_clients": [],
        "allowed_hosts": ["my-mac.local", "192.168.1.0/24"],
        "guidance_presets": {"zeta": {"files": ["~/skills/a.md", str(tmp_path / "b.md")]}},
    }
    assert os.stat(home / "config.json").st_mode & 0o777 == 0o600
    assert app.state.settings.guidance_presets == (
        GuidancePreset("zeta", ("~/skills/a.md", str(tmp_path / "b.md"))),
    )
    assert app.state.settings.allowed_hosts == ("my-mac.local", "192.168.1.0/24")


def test_saved_client_list_applies_without_restart(tmp_path) -> None:
    app = make_app(tmp_path, allowed_clients=("192.168.1.0/24",))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as local:
        # Shares the running app's state; only the peer address differs.
        peer = TestClient(app, base_url="http://localhost", client=("10.0.0.5", 5000))
        before = peer.get("/")
        local.post("/settings/access", data=form(allowed_clients="10.0.0.0/8"))
        after = peer.get("/")

    assert before.status_code == 403
    assert after.status_code == 200


def test_an_environment_variable_keeps_its_list_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DELIBRA_ALLOWED_HOSTS", "10.0.0.9")
    app = make_app(tmp_path, allowed_hosts=("10.0.0.9",))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings").text
        client.post("/settings/access", data=form(allowed_hosts="evil.example"))

    assert 'name="allowed_hosts" rows="4" maxlength="65536" disabled' in page
    assert "DELIBRA_ALLOWED_HOSTS is set and overrides this list" in page
    assert "allowed_hosts" not in config(tmp_path)
    assert app.state.settings.allowed_hosts == ("10.0.0.9",)


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({"allowed_clients": "not-an-ip"}, "allowed client 'not-an-ip' must be an IP address or CIDR range"),
        ({"preset_name_0": "x", "preset_files_0": "relative.md"}, "guidance preset 'x': 'relative.md' must be an absolute or ~/ path to a .md file"),
        ({"preset_name_0": "x", "preset_files_0": "/opt/skills"}, "guidance preset 'x': '/opt/skills' must be an absolute or ~/ path to a .md file"),
        ({"preset_name_0": "", "preset_files_0": "/opt/a.md"}, "guidance preset name '' must be 1-40 printable characters without surrounding spaces"),
        ({"preset_name_0": "x"}, "guidance preset 'x' must list at least one file"),
        (
            {"preset_name_0": "x", "preset_files_0": "/opt/delibra-gone.md"},
            "Guidance preset 'x': '/opt/delibra-gone.md' must be an existing regular file reached without symlinks",
        ),
        (
            {"preset_count": "2", "preset_name_0": "x", "preset_files_0": "/a.md", "preset_name_1": "x", "preset_files_1": "/b.md"},
            "Guidance preset 'x' is listed twice",
        ),
    ],
)
def test_invalid_input_changes_nothing(tmp_path, fields, detail) -> None:
    home = tmp_path / "home"
    home.mkdir()
    original = json.dumps({"allowed_clients": ["192.168.1.0/24"]})
    (home / "config.json").write_text(original, encoding="utf-8")
    app = make_app(tmp_path, allowed_clients=("192.168.1.0/24",))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        target = "/settings/access" if "allowed_clients" in fields else "/settings/presets"
        response = client.post(target, data=form(**fields))

    assert response.status_code == 422
    sentence = detail[:1].upper() + detail[1:]
    assert "Global settings" in response.text
    assert f"<div hidden data-page-alert>Nothing was saved: {escape(sentence)}</div>" in response.text
    assert (home / "config.json").read_text(encoding="utf-8") == original
    assert app.state.settings.allowed_clients == ("192.168.1.0/24",)


def test_a_preset_count_the_page_never_sends_is_refused(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/presets", data=form(preset_count="34"))

    assert response.status_code == 422
    assert response.json()["detail"] == "preset_count must be between 0 and 33"
    assert not (tmp_path / "home" / "config.json").exists()


def test_a_refused_save_shows_the_page_with_what_was_typed_and_chosen(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "a.md").write_text("x" * 2000)
    (tmp_path / "skills" / "b.md").write_text("# B\n")
    app = make_app(tmp_path, guidance_presets=(GuidancePreset("old", ("~/skills/b.md",)),))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post(
            "/settings/presets",
            data=form(
                preset_count="2",
                preset_name_0="old",
                preset_files_0="~/skills/b.md",
                preset_remove_0="true",
                preset_name_1="",
                preset_files_1="~/skills/a.md",
            ),
        )

    page = response.text
    assert response.status_code == 422
    assert not (tmp_path / "home" / "config.json").exists()
    assert app.state.settings.guidance_presets == (GuidancePreset("old", ("~/skills/b.md",)),)
    assert 'name="preset_remove_0" value="true" checked' in page
    assert 'id="preset-files-1" name="preset_files_1" value="~/skills/a.md"' in page
    assert "<code>~/skills/a.md</code> 2 KB" in page
    message = (
        "Guidance preset name &#39;&#39; must be 1-40 printable characters without "
        "surrounding spaces"
    )
    # A browser alert says it, as Start new phase's confirm asks; no banner.
    assert f"<div hidden data-page-alert>Nothing was saved: {message}</div>" in page
    assert '<p class="warning" role="alert">Nothing was saved' not in page
    # The message also marks the second preset's box, after its legend.
    second = page[page.index('id="preset-files-1"') - 600 : page.index('id="preset-files-1"')]
    assert message in second
    # Both rows were filled, so a fresh empty row is offered again.
    assert 'name="preset_count" value="3"' in page
    assert 'name="preset_name_2" value=""' in page


def test_a_ticked_remove_drops_the_preset(tmp_path) -> None:
    app = make_app(tmp_path, guidance_presets=(GuidancePreset("old", ("/opt/a.md",)),))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        client.post(
            "/settings/presets",
            data=form(preset_name_0="old", preset_files_0="/opt/a.md", preset_remove_0="true"),
        )

    assert config(tmp_path)["guidance_presets"] == {}
    assert app.state.settings.guidance_presets == ()


def test_a_preset_keeps_its_skills_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "skills" / "tdd").mkdir(parents=True)
    (tmp_path / "skills" / "tdd" / "SKILL.md").write_text("# TDD\n")
    (tmp_path / "router.md").write_text("# Router\n")
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post(
            "/settings/presets",
            data=form(
                preset_name_0="suite",
                preset_files_0="~/router.md",
                preset_skills_0="~/skills/tdd/SKILL.md",
            ),
            follow_redirects=False,
        )
        page = client.get("/settings/presets").text
        refused = client.post(
            "/settings/presets",
            data=form(
                preset_name_0="suite",
                preset_files_0="~/router.md",
                preset_skills_0="~/skills/tdd/SKILL.md\n~/skills/gone.md",
            ),
        )

    assert saved.status_code == 303
    assert config(tmp_path)["guidance_presets"] == {
        "suite": {"files": ["~/router.md"], "skills": ["~/skills/tdd/SKILL.md"]}
    }
    assert 'name="preset_skills_0" value="~/skills/tdd/SKILL.md"' in page
    assert refused.status_code == 422
    assert "Guidance preset &#39;suite&#39;: &#39;" in refused.text
    assert 'name="preset_skills_0" value="~/skills/tdd/SKILL.md\n~/skills/gone.md"' in refused.text


def test_an_unchanged_preset_with_a_missing_file_does_not_block_saving(tmp_path) -> None:
    app = make_app(
        tmp_path, guidance_presets=(GuidancePreset("old", ("/opt/delibra-gone.md",)),)
    )
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post(
            "/settings/presets",
            data=form(preset_name_0="old", preset_files_0="/opt/delibra-gone.md"),
            follow_redirects=False,
        )
        # Saving the other tab is not blocked by the preset's missing file either.
        access = client.post(
            "/settings/access", data=form(allowed_hosts="my-mac.local"), follow_redirects=False
        )

    assert saved.status_code == 303 and access.status_code == 303
    assert config(tmp_path)["guidance_presets"] == {"old": {"files": ["/opt/delibra-gone.md"]}}
    assert app.state.settings.allowed_hosts == ("my-mac.local",)


def preset_files(client: TestClient, **fields: str):
    data = {"view": "dialog"}
    data.update(fields)
    return client.post("/settings/preset-files", data=data)


def _skills(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    (skills / "sub").mkdir(parents=True)
    (skills / "a.md").write_text("x" * 2000)
    (skills / "sub" / "b.md").write_text("# B\n")
    (skills / "notes.txt").write_text("n")
    return skills


def test_adding_a_folder_lists_its_files_at_that_moment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _skills(tmp_path)
    (tmp_path / "c.md").write_text("# C\n")
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        flat = preset_files(client, files="~/c.md", add="~/skills").text
        deep = preset_files(client, files="~/c.md", add="~/skills\n~/c.md", subdirs="true").text

    assert 'id="picker-files-value" name="files" value="~/c.md\n~/skills/a.md"' in flat
    assert "<code>~/skills/a.md</code> 2 KB" in flat
    assert "sub/b.md" not in flat
    assert "2 files, " in flat
    assert 'value="~/c.md\n~/skills/a.md\n~/skills/sub/b.md"' in deep
    assert "data-path-picker-use" in deep
    assert not (tmp_path / "home" / "config.json").exists()


def test_remove_drops_one_file_and_the_row_view_is_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _skills(tmp_path)
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        removed = preset_files(
            client, files="~/skills/a.md\n~/skills/sub/b.md", remove="~/skills/a.md"
        ).text
        row = preset_files(client, view="row", files="~/skills/a.md").text
        empty = preset_files(client, files="").text

    assert 'name="files" value="~/skills/sub/b.md"' in removed
    assert "<code>~/skills/a.md</code>" not in removed
    assert """hx-vals='{"list": "files", "remove": "~/skills/sub/b.md", "view": "dialog"}'""" in removed
    assert "1 file, " in removed
    assert "<code>~/skills/a.md</code> 2 KB" in row
    assert "picker-files-value" not in row
    assert "Remove" not in row
    assert "data-path-picker-use" not in row
    assert "No files yet." in empty
    remove_all = """hx-vals='{"files": "", "list": "files", "view": "dialog"}'"""
    assert remove_all in removed
    assert ">Remove all</button>" in removed
    assert remove_all not in empty
    assert remove_all not in row


def test_an_add_over_the_limits_or_without_markdown_adds_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    many = tmp_path / "many"
    many.mkdir()
    for index in range(33):
        (many / f"{index:02}.md").write_text("x")
    (tmp_path / "empty").mkdir()
    (tmp_path / "wide").mkdir()
    for index in range(5):
        (tmp_path / "wide" / f"{index}.txt").write_text("x")
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        over = preset_files(client, files="", add="~/many").text
        nothing = preset_files(client, files="", add="~/empty").text
        monkeypatch.setattr("app.coding.GUIDANCE_SCAN_MAX_ENTRIES", 3)
        wide = preset_files(client, files="", add="~/wide").text

    assert re.search(
        r"Adding these makes 33 files and \d+ KB with file headers; the limit is 32 files "
        r"and 256 KB; nothing was added",
        over,
    )
    assert 'id="picker-files-value" name="files" value=""' in over
    assert "No .md files in the ticked paths; nothing was added" in nothing
    assert (
        "The ticked folders have more than 3 directory entries; tick a smaller folder; "
        "nothing was added"
    ) in wide


@pytest.mark.parametrize(
    "fields",
    [
        {"files": "relative.md"},
        {"files": "~/skills"},
        {"files": "\n".join(f"/{index}.md" for index in range(33))},
        {"view": "other"},
        {"list": "other"},
        {"list": "skills", "files": "\n".join(f"/{index}.md" for index in range(513))},
        {"add": "relative"},
    ],
)
def test_malformed_preset_file_requests_are_refused(tmp_path, fields) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = preset_files(client, **fields)

    assert response.status_code == 422


def test_preset_files_are_for_this_machine_only(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        response = preset_files(client, files="/opt/a.md")

    assert response.status_code == 403


def test_the_skills_list_has_its_own_heading_limits_and_layout_check(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for suite in ("a", "b"):
        (tmp_path / suite / "tdd").mkdir(parents=True)
        (tmp_path / suite / "tdd" / "SKILL.md").write_text("x" * 3000)
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        listed = preset_files(client, list="skills", files="~/a/tdd/SKILL.md").text
        pasted = preset_files(client, files="~/a/tdd/SKILL.md").text
        clash = preset_files(
            client, list="skills", files="~/a/tdd/SKILL.md", add="~/b/tdd", subdirs="true"
        ).text
        monkeypatch.setattr("app.routes.settings.SKILLS_MAX_FILES", 1)
        over = preset_files(client, list="skills", files="~/a/tdd/SKILL.md", add="~/b/tdd").text
        row = preset_files(client, view="row", list="skills", files="~/a/tdd/SKILL.md").text

    assert '<h3 class="picker-files-heading">Will include: agents can read</h3>' in listed
    assert "1 file, 3 KB (limit 512 files, 4096 KB)." in listed
    assert """hx-vals='{"list": "skills", "remove": "~/a/tdd/SKILL.md", "view": "dialog"}'""" in listed
    assert """hx-vals='{"files": "", "list": "skills", "view": "dialog"}'""" in listed
    assert '<h3 class="picker-files-heading">Will include: pasted every turn</h3>' in pasted
    assert "with file headers (limit 32 files, 256 KB)." in pasted
    assert (
        f"Skill files {str(tmp_path / 'a' / 'tdd' / 'SKILL.md')!r} and "
        f"{str(tmp_path / 'b' / 'tdd' / 'SKILL.md')!r} would both be copied as "
        "'tdd/SKILL.md'; nothing was added"
    ).replace("'", "&#39;") in clash
    assert 'name="files" value="~/a/tdd/SKILL.md"' in clash
    assert (
        "Adding these makes 2 files and 6 KB; the limit is 1 files and 4096 KB; nothing was added"
    ) in over
    assert "(limit 512 files, 4096 KB)" in row and "Remove" not in row


def test_a_failed_config_write_shows_the_page_and_changes_nothing(tmp_path, monkeypatch) -> None:
    from app.routes import settings as settings_routes
    from app.storage import StorageError

    def full_disk(_home, _data) -> None:
        raise StorageError("disk full")

    monkeypatch.setattr(settings_routes, "write_config_file", full_disk)
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/access", data=form(allowed_hosts="my-mac.local"))

    assert response.status_code == 422
    assert ">my-mac.local</textarea>" in response.text
    assert "<div hidden data-page-alert>Nothing was saved: Disk full</div>" in response.text
    assert app.state.settings.allowed_hosts == ()


@pytest.mark.parametrize("field", ["allowed_clients", "allowed_hosts"])
def test_an_access_list_longer_than_the_box_allows_is_refused(tmp_path, field) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/access", data=form(**{field: "a" * 65537}))

    label = "Allowed clients" if field == "allowed_clients" else "Allowed hosts"
    assert response.status_code == 422
    assert (
        f"<div hidden data-page-alert>Nothing was saved: {label} is longer than 65536 characters</div>"
    ) in response.text
    assert not (tmp_path / "home" / "config.json").exists()


def test_the_global_instruction_saves_and_shows(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        empty = client.get("/settings/instruction").text
        saved = client.post(
            "/settings/global-instruction",
            data={"global_instruction": "Be concise.\r\n"},
            follow_redirects=False,
        )
        page = client.get("/settings/instruction").text

    assert '<textarea name="global_instruction" rows="12" maxlength="65536"></textarea>' in empty
    assert saved.status_code == 303
    assert saved.headers["location"] == "/settings/instruction"
    assert (tmp_path / "home" / "instructions.md").read_text() == "Be concise.\n"
    assert ">Be concise.\n</textarea>" in page
    assert not (tmp_path / "home" / "config.json").exists()


def test_a_global_instruction_over_64_kb_is_refused_and_kept_in_the_box(tmp_path) -> None:
    app = make_app(tmp_path)
    text = "a" * 65537
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/global-instruction", data={"global_instruction": text})

    assert response.status_code == 422
    assert not (tmp_path / "home" / "instructions.md").exists()
    assert f">{text}</textarea>" in response.text
    assert (
        "<div hidden data-page-alert>Nothing was saved: "
        "The global instruction is larger than 64 KB</div>"
    ) in response.text


def test_an_unreadable_global_instruction_is_reported_on_the_page(tmp_path) -> None:
    app = make_app(tmp_path)
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "instructions.md").write_bytes(b"\xff")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings/instruction").text

    assert "<div hidden data-page-alert>The global instruction is not valid UTF-8</div>" in page
    assert '<textarea name="global_instruction" rows="12" maxlength="65536"></textarea>' in page


def test_only_this_machine_may_save_the_global_instruction(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        response = client.post("/settings/global-instruction", data={"global_instruction": "x"})

    assert response.status_code == 403
    assert not (tmp_path / "home" / "instructions.md").exists()


def test_global_settings_has_access_and_instruction_with_presets_tabs(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        access = client.get("/settings").text
        same = client.get("/settings/access").text
        instruction = client.get("/settings/instruction").text
        presets = client.get("/settings/presets").text
        unknown = client.get("/settings/other")

    assert access == same
    assert 'id="access-heading"' in access and 'id="presets-heading"' not in access
    assert 'id="global-instruction-heading"' in instruction and 'id="access-heading"' not in instruction
    assert presets == instruction  # /settings/presets now lands on the Instruction tab
    assert 'id="presets-heading"' in instruction
    assert '<a href="/settings/instruction" aria-current="page">Instruction</a>' in instruction
    assert unknown.status_code == 404


def test_access_and_presets_save_independently(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "allowed_clients": ["10.0.0.0/8"], "guidance_presets": {}, "other": 1,
    }), encoding="utf-8")
    app = make_app(tmp_path, allowed_clients=("10.0.0.0/8",))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        access = client.post("/settings/access", data={"allowed_clients": "192.168.1.0/24", "allowed_hosts": ""},
                             follow_redirects=False)
        presets = client.post("/settings/presets", data={"preset_count": "0"}, follow_redirects=False)
        old = client.post("/settings", data=form())

    saved = config(tmp_path)
    assert (access.status_code, access.headers["location"]) == (303, "/settings")
    assert (presets.status_code, presets.headers["location"]) == (303, "/settings/instruction")
    assert saved["allowed_clients"] == ["192.168.1.0/24"]
    assert saved["other"] == 1
    assert old.status_code == 405


def test_the_agents_tab_shows_one_row_per_agent_type_with_its_defaults(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings/agents").text
    assert ">Agents</a>" in page or 'aria-current="page">Agents' in page
    assert "Applies from its next use." in page
    assert 'name="model_claude" value="opus"' in page and 'name="model_codex" value=""' in page
    assert 'placeholder="CLI default"' in page
    assert re.search(r'name="effort_codex".*?<option value="high" selected>', page, re.S)
    assert '<option value="minimal"' in page  # Codex's own effort list


def test_agent_defaults_save_and_return_to_the_agents_tab(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post("/settings/agents", data={
            "model_claude": "sonnet", "effort_claude": "max", "model_codex": "", "effort_codex": "low",
        }, follow_redirects=False)
        page = client.get("/settings/agents").text
    assert (saved.status_code, saved.headers["location"]) == (303, "/settings/agents")
    assert config(tmp_path)["agent_defaults"] == {
        "claude": {"model": "sonnet", "effort": "max"}, "codex": {"model": "", "effort": "low"},
    }
    assert 'name="model_claude" value="sonnet"' in page


def test_a_refused_agent_defaults_save_keeps_the_typed_values(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        refused = client.post("/settings/agents", data={
            "model_claude": "-bad", "effort_claude": "max", "model_codex": "typed", "effort_codex": "low",
        })
    assert refused.status_code == 422
    assert "<div hidden data-page-alert>Nothing was saved: Model must not start with" in refused.text
    assert 'name="model_claude" value="-bad"' in refused.text and 'name="model_codex" value="typed"' in refused.text
    assert not (tmp_path / "home" / "config.json").exists()


def test_agent_defaults_are_this_machine_only(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        page = client.get("/settings/agents")
        saved = client.post("/settings/agents", data={"model_claude": "x", "effort_claude": "high",
                                                      "model_codex": "", "effort_codex": "high"})
    assert (page.status_code, saved.status_code) == (403, 403)


def test_a_blank_default_model_is_refused_like_an_agents_model(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        refused = client.post("/settings/agents", data={
            "model_claude": "   ", "effort_claude": "high", "model_codex": "", "effort_codex": "high"})
    assert refused.status_code == 422 and "not blank" in refused.text



def test_the_global_tabs_are_access_instruction_agents_converter(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings").text
        moved = client.get("/settings/presets", follow_redirects=False)
        instruction = client.get("/settings/instruction").text
        saved = client.post("/settings/presets", data={"preset_count": "0"}, follow_redirects=False)
    tabs = re.findall(r'<a [^>]*href="(/settings[^"]*)"[^>]*>(Access|Instruction|Presets|Agents|Converter)</a>', page)
    assert [label for _href, label in tabs] == ["Access", "Instruction", "Agents", "Converter"]
    assert (moved.status_code, moved.headers["location"]) == (303, "/settings/instruction")
    assert instruction.index('name="global_instruction"') < instruction.index('name="preset_count"')
    assert (saved.status_code, saved.headers["location"]) == (303, "/settings/instruction")


def test_the_converter_tab_downloads_the_models_from_this_machine_only(tmp_path, monkeypatch) -> None:
    import time as _time

    app = make_app(tmp_path)
    homes = []

    async def fake_download(home):
        homes.append(home)

    monkeypatch.setattr("app.routes.settings.download_models", fake_download)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings/converter").text
        started = client.post("/settings/converter-models", follow_redirects=False)
        for _ in range(100):
            if homes:
                break
            _time.sleep(0.01)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        refused = client.post("/settings/converter-models", follow_redirects=False)
        hidden = client.get("/settings/converter")
    assert "Converter models (PDF layout and tables):" in page and "Not downloaded" in page
    assert "Download models (about 0.7 GB)" in page
    assert (started.status_code, started.headers["location"]) == (303, "/settings/converter")
    assert homes == [app.state.settings.home]
    assert (refused.status_code, hidden.status_code) == (403, 403)


def test_the_models_download_is_not_restarted_needlessly(tmp_path, monkeypatch) -> None:
    import asyncio
    from app.converter_models import READY_NAME, models_dir

    app = make_app(tmp_path)
    started = []

    async def fake_download(home):
        started.append(home)

    monkeypatch.setattr("app.routes.settings.download_models", fake_download)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        target = models_dir(app.state.settings.home)
        target.mkdir(parents=True)
        (target / READY_NAME).write_text("x")
        ready = client.post("/settings/converter-models", follow_redirects=False)
        target.joinpath(READY_NAME).unlink()
        app.state.download_manager._convert_slot = asyncio.Semaphore(0)  # a conversion holds the slot
        busy = client.post("/settings/converter-models", follow_redirects=False)
    assert (ready.status_code, ready.headers["location"]) == (303, "/settings/converter")
    assert busy.status_code == 409
    assert "Models were not downloaded: A conversion is running" in busy.text
    assert started == []


def test_a_refused_global_instruction_save_keeps_the_presets_on_the_page(tmp_path) -> None:
    # Final review F1: the refused page showed no presets, and saving presets from it
    # deleted them all.
    from html import unescape

    guide = tmp_path / "keep.md"
    guide.write_text("# Keep\n")
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        client.post("/settings/presets", data={"preset_count": "1", "preset_name_0": "keep",
                                               "preset_files_0": str(guide)}, follow_redirects=False)
        refused = client.post("/settings/global-instruction", data={"global_instruction": "x" * 70_000})
        page = refused.text
        form_html = page[page.index('action="/settings/presets"'):]
        form_html = form_html[: form_html.index("</form>")]
        fields = {name: unescape(value) for tag, name, value in
                  re.findall(r'(<input[^>]*name="(preset_[a-z_0-9]+)"[^>]*value="([^"]*)")', form_html)
                  if 'type="checkbox"' not in tag}  # an unticked box is not sent
        fields |= {name: unescape(text) for name, text in
                   re.findall(r'<textarea[^>]*name="(preset_[a-z_0-9]+)"[^>]*>([^<]*)</textarea>', form_html)}
        resaved = client.post("/settings/presets", data=fields, follow_redirects=False)
    assert refused.status_code == 422
    assert fields.get("preset_name_0") == "keep"
    assert resaved.status_code == 303
    assert [preset.name for preset in app.state.settings.guidance_presets] == ["keep"]


def test_a_busy_models_download_is_not_called_nothing_saved(tmp_path) -> None:
    import asyncio

    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        app.state.download_manager._convert_slot = asyncio.Semaphore(0)
        busy = client.post("/settings/converter-models", follow_redirects=False)
    assert busy.status_code == 409
    assert "<div hidden data-page-alert>Models were not downloaded: A conversion is running" in busy.text


# Task 14: Settings -> Converter actions.
def _converter_home(app, *, ready: bool = True, revision: str = "main", check: dict | None = None):
    from app.converter_models import MANIFEST_NAME, MODEL_REPOS, READY_NAME, models_dir

    home = app.state.settings.home
    target = models_dir(home)
    if ready:
        target.mkdir(parents=True)
        (target / READY_NAME).write_text("x")
        (target / MANIFEST_NAME).write_text(json.dumps({"models": [
            {"repo_id": repo, "revision": revision if n == 0 else default, "commit": ("b" if n == 0 else "c") * 40,
             "downloaded_at": "2026-11-19T00:00:00Z", "default": (revision if n == 0 else default) == default}
            for n, (repo, default) in enumerate(MODEL_REPOS)]}))
    if check is not None:
        (home / "models").mkdir(parents=True, exist_ok=True)
        (home / "models" / "docling-check.json").write_text(json.dumps(check))
    return MODEL_REPOS


CHECKED = {"checked_at": "2026-11-19T01:00:00Z", "repos": {
    "docling-project/docling-layout-heron": {"refs": [{"name": "main", "kind": "branch", "commit": "d" * 40}],
                                             "commits": [{"commit": "d" * 40, "title": "new", "date": None},
                                                         {"commit": "b" * 40, "title": "old", "date": None}]},
    "docling-project/docling-layout-heron-onnx": {"refs": [{"name": "main", "kind": "branch", "commit": "c" * 40}], "commits": []},
    "docling-project/docling-models": {"refs": [{"name": "v2.3.0", "kind": "tag", "commit": "c" * 40}], "commits": []},
}}


def test_the_converter_tab_lists_installed_models_and_a_checked_update(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        _converter_home(app, check=CHECKED)
        page = client.get("/settings/converter").text
    assert "docling-project/docling-layout-heron" in page and "main @ bbbbbbb" in page
    assert "Update available" in page  # main now points to ddddddd
    assert '<select name="revision_0"' in page and '<option value="' + "d" * 40 + '"' in page
    assert 'name="sha_0"' in page and "Download chosen versions" in page and "Remove models" in page


def test_a_non_default_version_carries_the_warning(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        _converter_home(app, revision="b" * 40)
        page = client.get("/settings/converter").text
    assert "Not the version this Docling release expects; conversions may fail or differ." in page


def test_check_for_updates_runs_and_returns(tmp_path, monkeypatch) -> None:
    calls = []

    async def fake_check(home):
        calls.append(home)

    monkeypatch.setattr("app.routes.settings.check_updates", fake_check)
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        done = client.post("/settings/converter-models/check", follow_redirects=False)
    assert (done.status_code, done.headers["location"], calls) == (303, "/settings/converter", [app.state.settings.home])


def test_a_chosen_version_downloads_those_revisions(tmp_path, monkeypatch) -> None:
    import time as _time

    chosen = []

    async def fake_download(home, revisions=None):
        chosen.append(revisions)

    monkeypatch.setattr("app.routes.settings.download_models", fake_download)
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        repos = _converter_home(app, check=CHECKED)
        done = client.post("/settings/converter-models/version", data={
            "revision_0": "main", "sha_0": "bbbbbbb", "revision_1": "main", "revision_2": "v2.3.0"},
            follow_redirects=False)
        for _ in range(100):
            if chosen:
                break
            _time.sleep(0.01)
    assert (done.status_code, done.headers["location"]) == (303, "/settings/converter")
    assert chosen == [{repos[0][0]: "b" * 40, repos[1][0]: "main", repos[2][0]: "v2.3.0"}]


def test_an_invalid_commit_is_refused_and_kept(tmp_path, monkeypatch) -> None:
    async def never(*args, **kwargs):
        raise AssertionError("must not download")

    monkeypatch.setattr("app.routes.settings.download_models", never)
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        _converter_home(app, check=CHECKED)
        refused = client.post("/settings/converter-models/version", data={
            "revision_0": "main", "sha_0": "xyz123!", "revision_1": "main", "revision_2": "v2.3.0"})
    assert refused.status_code == 422
    assert "<div hidden data-page-alert>Models were not downloaded: A commit must be 40 hex characters" in refused.text
    assert 'name="sha_0" value="xyz123!"' in refused.text


def test_models_actions_are_refused_while_converting_and_from_the_lan(tmp_path) -> None:
    import asyncio

    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        _converter_home(app, check=CHECKED)
        app.state.download_manager._convert_slot = asyncio.Semaphore(0)
        responses = [client.post(path, data=data, follow_redirects=False) for path, data in (
            ("/settings/converter-models", {"force": "true"}), ("/settings/converter-models/check", {}),
            ("/settings/converter-models/remove", {}),
            ("/settings/converter-models/version", {"revision_0": "main", "revision_1": "main", "revision_2": "v2.3.0"}))]
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        lan = [client.post(path, follow_redirects=False).status_code
               for path in ("/settings/converter-models/check", "/settings/converter-models/remove",
                            "/settings/converter-models/version")]
    assert [r.status_code for r in responses] == [409] * 4
    assert all("A conversion is running" in r.text for r in responses)
    assert lan == [403] * 3


def test_remove_models_falls_back_to_not_downloaded(tmp_path) -> None:
    from app.converter_models import models_dir

    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        _converter_home(app)
        removed = client.post("/settings/converter-models/remove", follow_redirects=False)
        page = client.get("/settings/converter").text
    assert (removed.status_code, removed.headers["location"]) == (303, "/settings/converter")
    assert not models_dir(app.state.settings.home).exists() and "Not downloaded" in page


def test_no_update_is_offered_when_the_default_points_to_the_installed_commit(tmp_path) -> None:
    current = json.loads(json.dumps(CHECKED))
    current["repos"]["docling-project/docling-layout-heron"]["refs"][0]["commit"] = "b" * 40
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        _converter_home(app, check=current)
        page = client.get("/settings/converter").text
    assert "Update available" not in page

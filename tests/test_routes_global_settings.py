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
        saved = client.post("/settings", data=form())
        index = client.get("/").text

    assert page.status_code == 403
    assert saved.status_code == 403
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
        page = client.get("/settings").text
        index = client.get("/").text

    assert 'href="/settings"' in index
    assert ">192.168.1.0/24</textarea>" in page
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
        'data-picker-target="preset-files-0" data-picker-files-list="preset-files-list-0"'
    ) in page
    assert ">Choose files…</button>" in page
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
        saved = client.post(
            "/settings",
            data=form(
                allowed_hosts="my-mac.local\n\n192.168.1.0/24",
                preset_count="2",
                preset_name_0=" zeta ",
                preset_files_0=f"~/skills/a.md\n{tmp_path / 'b.md'}\n",
                preset_name_1="",
                preset_files_1="",
            ),
            follow_redirects=False,
        )

    assert saved.status_code == 303
    assert saved.headers["location"] == "/settings"
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
        local.post("/settings", data=form(allowed_clients="10.0.0.0/8"))
        after = peer.get("/")

    assert before.status_code == 403
    assert after.status_code == 200


def test_an_environment_variable_keeps_its_list_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DELIBRA_ALLOWED_HOSTS", "10.0.0.9")
    app = make_app(tmp_path, allowed_hosts=("10.0.0.9",))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings").text
        client.post("/settings", data=form(allowed_hosts="evil.example"))

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
        response = client.post("/settings", data=form(**fields))

    assert response.status_code == 422
    sentence = detail[:1].upper() + detail[1:]
    assert "Global settings" in response.text
    assert f"<div hidden data-page-alert>Nothing was saved: {escape(sentence)}</div>" in response.text
    assert (home / "config.json").read_text(encoding="utf-8") == original
    assert app.state.settings.allowed_clients == ("192.168.1.0/24",)


def test_a_preset_count_the_page_never_sends_is_refused(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings", data=form(preset_count="34"))

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
            "/settings",
            data=form(
                allowed_hosts="my-mac.local",
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
    assert ">my-mac.local</textarea>" in page
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
            "/settings",
            data=form(preset_name_0="old", preset_files_0="/opt/a.md", preset_remove_0="true"),
        )

    assert config(tmp_path)["guidance_presets"] == {}
    assert app.state.settings.guidance_presets == ()


def test_an_unchanged_preset_with_a_missing_file_does_not_block_saving(tmp_path) -> None:
    app = make_app(
        tmp_path, guidance_presets=(GuidancePreset("old", ("/opt/delibra-gone.md",)),)
    )
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post(
            "/settings",
            data=form(
                allowed_hosts="my-mac.local",
                preset_name_0="old",
                preset_files_0="/opt/delibra-gone.md",
            ),
            follow_redirects=False,
        )

    assert saved.status_code == 303
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
    assert """hx-vals='{"remove": "~/skills/sub/b.md", "view": "dialog"}'""" in removed
    assert "1 file, " in removed
    assert "<code>~/skills/a.md</code> 2 KB" in row
    assert "picker-files-value" not in row
    assert "Remove" not in row
    assert "data-path-picker-use" not in row
    assert "No files yet." in empty
    remove_all = """hx-vals='{"files": "", "view": "dialog"}'"""
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

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.storage import RegistryStore

LOCAL = ("127.0.0.1", 5000)
LAN = ("192.168.1.20", 5000)


def make_app(tmp_path: Path):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    return app, settings


def skills(tmp_path: Path) -> Path:
    root = tmp_path / "me" / "skills"
    (root / "tdd").mkdir(parents=True)
    (root / "tdd" / "SKILL.md").write_text("# TDD\n")
    (root / ".git").mkdir()
    (root / "a.md").write_text("x" * 2000)
    (root / "b.txt").write_text("b")
    os.symlink(root / "a.md", root / "link.md")
    return root


def browse(client: TestClient, **params: str):
    return client.get("/browse", params=params)


def test_machine_browsing_is_for_this_machine_only(tmp_path) -> None:
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        response = browse(client, mode="preset")

    assert response.status_code == 403
    assert response.json()["detail"] == "Only this machine may browse its folders"


def test_an_unknown_mode_is_refused(tmp_path) -> None:
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = browse(client, mode="anything")

    assert response.status_code == 422
    assert response.json()["detail"] == "unknown browse mode 'anything'"


def test_preset_mode_offers_tilde_paths_for_folders_and_markdown(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    root = skills(tmp_path)
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = browse(client, mode="preset", path=str(root)).text

    assert 'data-picker-value="~/skills/tdd"' in page
    assert 'data-picker-value="~/skills/a.md"' in page
    assert "a.md</span> <small>2 KB</small>" in page
    assert "b.txt" not in page
    assert ".git" not in page
    assert "link.md (symlink, skipped)" in page
    assert 'data-picker-value="~/skills/link.md"' not in page
    assert "data-picker-add-files" in page
    assert "data-picker-subdirs" in page
    assert "data-path-picker-add" not in page
    assert "data-path-picker-close" not in page
    assert page.index("tdd/") < page.index("a.md")


def test_hidden_folders_show_only_when_asked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    root = skills(tmp_path)
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = browse(client, mode="preset", path=str(root), hidden="1").text

    assert 'data-picker-value="~/skills/.git"' in page
    assert 'hx-trigger="change" hx-target="#path-picker-body" hx-swap="outerHTML" checked' in page


def test_guidance_mode_offers_absolute_markdown_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    root = skills(tmp_path)
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = browse(client, mode="guidance", path=str(root)).text

    assert f'data-picker-value="{root / "a.md"}"' in page
    assert f'data-picker-value="{root / "tdd"}"' not in page
    assert "tdd/</button>" in page
    assert "data-picker-select-all" in page
    assert "data-path-picker-close" in page
    assert "data-picker-subdirs" not in page


def test_folder_mode_chooses_the_open_folder(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    root = skills(tmp_path)
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = browse(client, mode="folder", path=str(root)).text

    choose = f'data-path-picker-choose data-picker-value="{root}"'
    assert choose in page
    assert ">Choose ~/skills</button>" in page
    assert "data-picker-value=" not in page.replace(choose, "")
    assert "a.md" not in page
    assert "data-path-picker-add" not in page


def test_the_start_folder_comes_from_the_field_or_the_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    root = skills(tmp_path)
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        from_file = browse(client, mode="preset", start="~/skills/a.md").text
        from_fallback = browse(client, mode="folder", start="", fallback=str(root / "tdd")).text
        from_home = browse(client, mode="folder").text

    assert 'data-picker-value="~/skills/tdd"' in from_file
    assert f'data-path-picker-choose data-picker-value="{root / "tdd"}"' in from_fallback
    assert f'data-path-picker-choose data-picker-value="{tmp_path / "me"}"' in from_home


def test_a_symlinked_folder_shows_the_refusal_and_a_way_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    root = skills(tmp_path)
    os.symlink(root, tmp_path / "linked")
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = browse(client, mode="preset", path=str(tmp_path / "linked"))

    assert response.status_code == 200
    assert (
        f"&#39;{tmp_path / 'linked'}&#39; cannot be browsed: "
        "open a folder reached without symlinks"
    ) in response.text
    assert ">Open ~</button>" in response.text
    assert "data-picker-add-files" not in response.text


def test_path_picker_javascript_contract_runs_under_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; path picker unit test not run")
    result = subprocess.run(
        [node, "--test", "tests/js/test_path_picker.js"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def project_with_folders(tmp_path: Path):
    app, settings = make_app(tmp_path)
    project_dir = tmp_path / "proj"
    (project_dir / "src" / "app").mkdir(parents=True)
    (project_dir / ".cache").mkdir()
    (project_dir / "README.md").write_text("x")
    project = RegistryStore(settings.home).register("Proj", project_dir)
    return app, project


def test_project_mode_lists_project_folders_for_any_client(tmp_path) -> None:
    app, _project = project_with_folders(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        top = client.get("/projects/Proj/browse").text
        inner = client.get("/projects/Proj/browse", params={"path": "src"}).text
        hidden = client.get("/projects/Proj/browse", params={"hidden": "1"}).text
        outside = client.get("/projects/Proj/browse", params={"path": "../x"})

    assert 'data-picker-value="src"' in top
    assert "README.md" not in top
    assert ".cache" not in top
    assert 'data-picker-value=".cache"' in hidden
    assert 'data-picker-value="src/app"' in inner
    assert ">Proj</button>" in inner
    assert ">src</button>" in inner
    assert "data-path-picker-add" in inner
    assert outside.status_code == 422


def test_browse_buttons_follow_who_may_browse(tmp_path) -> None:
    app, _project = project_with_folders(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        index = client.get("/").text
        settings_page = client.get("/projects/Proj/settings").text
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        lan_index = client.get("/").text
        lan_settings = client.get("/projects/Proj/settings").text

    assert 'data-picker-url="/browse?mode=folder" data-picker-target="register-path"' in index
    assert 'data-picker-target="rebind-path-0"' in index
    assert "/browse?mode=folder&amp;fallback=" in index
    assert 'id="path-picker"' in index
    assert (
        'data-picker-url="/browse?mode=guidance" data-picker-target="coding-guidance-files"'
    ) in settings_page
    assert (
        'data-picker-url="/projects/Proj/browse" data-picker-target="project-writable-roots"'
    ) in settings_page
    assert 'data-picker-target="new-session-writable-roots"' in settings_page
    assert (
        'data-picker-url="/browse?mode=guidance" data-picker-target="coding-guidance-skills"'
    ) in settings_page
    assert 'id="path-picker"' in settings_page
    assert 'data-picker-target="register-path"' not in lan_index
    assert 'data-picker-target="rebind-path-0"' not in lan_index
    assert 'id="path-picker"' not in lan_index
    assert 'data-picker-target="coding-guidance-files"' not in lan_settings
    assert 'data-picker-target="coding-guidance-skills"' not in lan_settings
    assert 'data-picker-target="project-writable-roots"' in lan_settings

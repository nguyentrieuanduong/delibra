from pathlib import Path
from urllib.parse import quote
from fastapi.testclient import TestClient
from app.config import Settings
from app.download_store import DownloadStore
from app.main import create_app
from app.storage import ProjectStore, RegistryStore

LOCAL = ("127.0.0.1", 5000)


def downloads_app(tmp_path: Path):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    project_dir = tmp_path / "project"; project_dir.mkdir()
    project = RegistryStore(settings.home).register("Research", project_dir)
    app = create_app(settings_override=settings,
                     provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"})
    return app, project, DownloadStore(ProjectStore(project).root)

def add(store, url, flags=(), state=None):
    record = store.add(url, purpose="<b>why</b>", filename=None, flags=list(flags), auto_run=1,
                       cycle=1, round_n=1, agent="a" * 32, agent_name="Alpha", folder="raw")
    return store.update(record.id, state=state) if state else record

def base(project): return f"/projects/{quote(project.name, safe='')}"

def test_the_panel_masks_flagged_urls_and_escapes_purpose(tmp_path):
    app, project, store = downloads_app(tmp_path)
    add(store, "https://a.org/x?k=ghp_" + "A1b2" * 9, flags=["secret"])
    add(store, "https://arxiv.org/pdf/1", state="awaiting")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        panel = client.get(f"{base(project)}/downloads")
    assert "ghp_" not in panel.text and "a.org" not in panel.text and "(URL hidden)" in panel.text
    assert "&lt;b&gt;why&lt;/b&gt;" in panel.text and "https://arxiv.org/pdf/1" in panel.text
    assert "1 awaiting" in panel.text

def test_full_url_reveal_is_loopback_only(tmp_path):
    app, project, store = downloads_app(tmp_path)
    add(store, "https://a.org/x?k=secret-value", flags=["secret"])
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        assert "secret-value" in client.get(f"{base(project)}/downloads/D1/url").text
    with TestClient(app, base_url="http://localhost", client=("192.168.1.9", 5000)) as client:
        assert client.get(f"{base(project)}/downloads/D1/url").status_code == 403

def test_refuse_then_allow_is_rejected(tmp_path):
    app, project, store = downloads_app(tmp_path)
    add(store, "https://arxiv.org/pdf/1", state="awaiting")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        assert client.post(f"{base(project)}/downloads/D1/refuse").status_code == 200
        response = client.post(f"{base(project)}/downloads/D1/allow")
    assert response.status_code == 409 and "refused by you" in response.text

def test_actions_need_loopback(tmp_path):
    app, project, store = downloads_app(tmp_path)
    add(store, "https://arxiv.org/pdf/1", state="awaiting")
    with TestClient(app, base_url="http://localhost", client=("192.168.1.9", 5000)) as client:
        assert client.post(f"{base(project)}/downloads/D1/approve").status_code == 403
    assert store.get("D1").state == "awaiting"


def test_unknown_or_malformed_ids_are_not_found(tmp_path):
    app, project, _store = downloads_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        assert client.get(f"{base(project)}/downloads/D9/url").status_code == 404
        assert client.get(f"{base(project)}/downloads/x/url").status_code == 404
        assert client.post(f"{base(project)}/downloads/D9/approve").status_code == 404


def test_the_reveal_returns_the_model_note(tmp_path):
    app, project, store = downloads_app(tmp_path)
    record = add(store, "https://a.org/x.pdf")
    store.update(record.id, state="refused", reason="flagged by model check", masked=True,
                 check_note="looks like a token")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        panel = client.get(f"{base(project)}/downloads").text
        reveal = client.get(f"{base(project)}/downloads/D1/url").text
    assert "looks like a token" not in panel and "a.org" not in panel
    assert "https://a.org/x.pdf" in reveal and "looks like a token" in reveal


def test_awaiting_requests_come_first_then_newest(tmp_path):
    app, project, store = downloads_app(tmp_path)
    add(store, "https://arxiv.org/pdf/1", state="awaiting")
    add(store, "https://arxiv.org/pdf/2", state="failed")
    add(store, "https://arxiv.org/pdf/3", state="awaiting")
    add(store, "https://arxiv.org/pdf/4", state="downloaded")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        panel = client.get(f"{base(project)}/downloads").text
    order = [panel.index(f'id="download-D{n}"') for n in (3, 1, 4, 2)]
    assert order == sorted(order)


def test_unreadable_records_do_not_break_the_chat_page(tmp_path):
    app, project, store = downloads_app(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        chat = client.get(f"{base(project)}/chat")
        panel = client.get(f"{base(project)}/downloads")
    assert chat.status_code == 200 and panel.status_code == 200
    assert "Download records are unreadable" in chat.text

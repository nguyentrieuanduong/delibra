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

def test_every_rendering_of_the_list_carries_the_store_epoch(tmp_path):
    # Without it the first stream event looks like a new records file and
    # replaces the list while the user may be clicking in it.
    app, project, store = downloads_app(tmp_path)
    add(store, "https://arxiv.org/pdf/1", state="awaiting")
    stamp = f'data-revision="{store.load().revision}" data-epoch="{store.load().epoch}"'
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        pages = [client.get(f"{base(project)}/{path}").text for path in ("chat", "downloads", "downloads/list")]
    assert all(stamp in page for page in pages)


def test_the_empty_panel_points_to_the_general_tab(tmp_path):
    app, project, _store = downloads_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        listing = client.get(f"{base(project)}/downloads/list").text
    assert "Delibra downloads are off for this project (Settings → General)." in listing


def test_the_panel_lists_conversions_with_a_link_to_the_markdown(tmp_path):
    from app.convert_store import ConversionStore

    app, project, store = downloads_app(tmp_path)
    conversions = ConversionStore(store.root)
    record = conversions.add("a.pdf", request="c1.json", auto_run=0, round_n=1, agent="a" * 32)
    conversions.update(record.id, state="converted", output="raw/converted/a.pdf.md", source_sha256="f" * 64)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        pages = [client.get(f"{base(project)}/{path}").text for path in ("downloads/list", "downloads", "chat")]
    for page in pages:  # the list, the panel and the chat page that embeds it
        assert "Conversions" in page and "C1" in page and "<code>a.pdf</code>" in page
        assert "/files/view?path=raw%2Fconverted%2Fa.pdf.md" in page


def test_a_conversion_change_makes_the_panel_revision_newer(tmp_path):
    import re
    from app.convert_store import ConversionStore

    app, project, store = downloads_app(tmp_path)
    add(store, "https://arxiv.org/pdf/1")
    conversions = ConversionStore(store.root)

    def shown(client):
        text = client.get(f"{base(project)}/downloads/list").text
        return int(re.search(r'data-revision="(\d+)"', text).group(1)), re.search(r'data-epoch="([^"]*)"', text).group(1)

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        before, epoch = shown(client)
        record = conversions.add("a.pdf", request="c1.json", auto_run=0, round_n=1, agent="a" * 32)
        added, epoch_added = shown(client)
        conversions.update(record.id, state="failed", reason="x")
        updated, epoch_updated = shown(client)
    assert before < added < updated
    assert epoch_added == epoch_updated


def test_unreadable_conversion_records_are_named_in_the_panel(tmp_path):
    app, project, store = downloads_app(tmp_path)
    (store.root / "downloads").mkdir(parents=True, exist_ok=True)
    (store.root / "downloads" / "conversions.json").write_text("{")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        pages = [client.get(f"{base(project)}/{path}").text for path in ("downloads/list", "chat")]
    for page in pages:
        assert "Conversion records are unreadable (<code>.delibra/downloads/conversions.json</code>)" in page



def test_the_downloads_panel_sits_in_the_right_column_above_the_files(tmp_path):
    app, project, _store = downloads_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get(f"{base(project)}/chat").text
    files = page.index('class="workspace-files"')
    assert files < page.index('id="downloads-panel"') < page.index('id="file-browser-host"')

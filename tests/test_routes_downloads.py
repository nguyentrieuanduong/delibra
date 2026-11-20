import asyncio
import json
from pathlib import Path
from urllib.parse import quote
from fastapi.testclient import TestClient
from app.coding import DownloadPolicy
from app.config import Settings
from app.doc_convert import ConvertResult
from app.download_manager import DownloadTurn
from app.questions import prepare_spool
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


def test_the_conversions_list_links_the_markdown_and_the_downloads_list_does_not_list_it(tmp_path):
    from app.convert_store import ConversionStore

    app, project, store = downloads_app(tmp_path)
    conversions = ConversionStore(store.root)
    record = conversions.add("a.pdf", request="c1.json", auto_run=0, round_n=1, agent="a" * 32)
    conversions.update(record.id, state="converted", output="raw/converted/a.pdf.md", source_sha256="f" * 64)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        pages = [client.get(f"{base(project)}/{path}").text for path in ("conversions/list", "chat")]
        downloads = client.get(f"{base(project)}/downloads/list").text
    for page in pages:  # the list and the chat page that embeds it
        assert 'id="conversion-C1"' in page and "<code>a.pdf</code>" in page
        assert "/files/view?path=raw%2Fconverted%2Fa.pdf.md" in page
    assert "conversion-C1" not in downloads


def test_download_conversions_are_listed_with_the_requested_files(tmp_path):
    app, project, store = downloads_app(tmp_path)
    record = add(store, "https://arxiv.org/pdf/1")
    store.update(record.id, state="downloaded", path="raw/arxiv.org/1.pdf", convert=True,
                 convert_state="converted", converted_path="raw/arxiv.org/1.pdf.md")
    add(store, "https://arxiv.org/pdf/2", state="downloaded")  # conversion off: not listed
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        listing = client.get(f"{base(project)}/conversions/list").text
    assert 'id="conversion-D1"' in listing and "<code>raw/arxiv.org/1.pdf</code>" in listing
    assert "/files/view?path=raw%2Farxiv.org%2F1.pdf.md" in listing
    assert 'id="conversion-D2"' not in listing


def test_a_conversion_change_makes_the_conversions_revision_newer(tmp_path):
    import re
    from app.convert_store import ConversionStore

    app, project, store = downloads_app(tmp_path)
    add(store, "https://arxiv.org/pdf/1")
    conversions = ConversionStore(store.root)

    def shown(client, path="conversions/list"):
        text = client.get(f"{base(project)}/{path}").text
        return int(re.search(r'data-revision="(\d+)"', text).group(1)), re.search(r'data-epoch="([^"]*)"', text).group(1)

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        before, epoch = shown(client)
        downloads_before = shown(client, "downloads/list")
        record = conversions.add("a.pdf", request="c1.json", auto_run=0, round_n=1, agent="a" * 32)
        added, epoch_added = shown(client)
        conversions.update(record.id, state="failed", reason="x")
        updated, epoch_updated = shown(client)
        downloads_after = shown(client, "downloads/list")
    assert before < added < updated
    assert epoch_added == epoch_updated
    assert downloads_before == downloads_after  # the Downloads list does not refresh for it


def test_unreadable_conversion_records_are_named_in_the_conversions_panel(tmp_path):
    app, project, store = downloads_app(tmp_path)
    (store.root / "downloads").mkdir(parents=True, exist_ok=True)
    (store.root / "downloads" / "conversions.json").write_text("{")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        pages = [client.get(f"{base(project)}/{path}").text for path in ("conversions/list", "chat")]
    for page in pages:
        assert "Conversion records are unreadable (<code>.delibra/downloads/conversions.json</code>)" in page


class GatedConverter:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def __call__(self, staged, **kwargs):
        self.started.set()
        await self.release.wait()
        return ConvertResult("converted", None, b"# md\n", "pdf")


def start_conversions(client, app, project, tmp_path: Path, *names: str) -> GatedConverter:
    """Request `names` in a chat turn, in the server's loop; returns once the first converts."""

    manager = app.state.download_manager
    converter = GatedConverter()
    project_dir = ProjectStore(project).project_path
    for name in names:
        (project_dir / name).write_bytes(b"%PDF-1.7 " + name.encode())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def start() -> None:
        manager.converter = converter
        prepare_spool(workspace, "convert", "requests", extra_names=("status.json",))
        turn = DownloadTurn(
            project_id=project.id, project_root=project_dir, store_root=ProjectStore(project).root,
            temp_dir=workspace, auto_run=0, cycle=None, round_n=1, agent="a" * 32, agent_name="Alpha",
            session_id="a" * 32, policy=DownloadPolicy(convert=True), convert=True,
        )
        manager.begin_turn(turn)
        for index, name in enumerate(names, start=1):
            (turn.convert_spool / f"c{index}.json").write_text(json.dumps({"path": name}))
        manager.harvest(turn, final=True)
        await converter.started.wait()
        await manager.stop_watch(turn)
        manager.end_turn(turn)

    client.portal.call(start)
    return converter


def test_the_conversions_list_shows_the_queue(tmp_path):
    app, project, _store = downloads_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        converter = start_conversions(client, app, project, tmp_path, "a.pdf", "b.pdf")
        listing = client.get(f"{base(project)}/conversions/list").text
        client.portal.call(converter.release.set)
        client.portal.call(app.state.download_manager.idle)
        done = client.get(f"{base(project)}/conversions/list").text
    running = listing[listing.index('id="conversion-C1"'):listing.index('id="conversion-C2"')]
    queued = listing[listing.index('id="conversion-C2"'):]
    assert "data-elapsed=" in running and "Converting" in running
    assert "Position 1" in queued and "data-elapsed=" not in queued
    assert listing.count('/cancel"') == 2
    assert "/cancel" not in done and "Converting" not in done


def test_cancel_ends_a_queued_conversion(tmp_path):
    from app.convert_store import ConversionStore

    app, project, store = downloads_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        converter = start_conversions(client, app, project, tmp_path, "a.pdf", "b.pdf")
        response = client.post(f"{base(project)}/conversions/C2/cancel")
        states = {r.id: (r.state, r.reason) for r in ConversionStore(store.root).load()}
        client.portal.call(converter.release.set)
        client.portal.call(app.state.download_manager.idle)
    assert response.status_code == 200 and 'id="conversions-list"' in response.text
    assert states == {"C1": ("converting", None), "C2": ("cancelled", "cancelled by the user")}


def test_cancel_needs_this_machine_and_the_same_origin(tmp_path):
    from app.convert_store import ConversionStore

    app, project, store = downloads_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        converter = start_conversions(client, app, project, tmp_path, "a.pdf", "b.pdf")
        lan = TestClient(app, base_url="http://localhost", client=("192.168.1.9", 5000))
        from_lan = lan.post(f"{base(project)}/conversions/C2/cancel")
        cross_site = client.post(f"{base(project)}/conversions/C2/cancel",
                                 headers={"Origin": "http://evil.example"})
        states = [r.state for r in ConversionStore(store.root).load()]
        client.portal.call(converter.release.set)
        client.portal.call(app.state.download_manager.idle)
    assert (from_lan.status_code, cross_site.status_code) == (403, 403)
    assert states == ["converting", "queued"]


def test_cancel_refuses_unknown_malformed_and_finished_conversions(tmp_path):
    from app.convert_store import ConversionStore

    app, project, store = downloads_app(tmp_path)
    conversions = ConversionStore(store.root)
    done = conversions.add("a.pdf", request="c1.json", auto_run=0, round_n=1, agent="a" * 32)
    conversions.update(done.id, state="converted", output="raw/converted/a.pdf.md")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        finished = client.post(f"{base(project)}/conversions/C1/cancel")
        unknown = [client.post(f"{base(project)}/conversions/{cid}/cancel").status_code
                   for cid in ("C9", "D9", "X1", "C0", "c1")]
    assert finished.status_code == 409 and "C1 has nothing to cancel: it is converted" in finished.text
    assert unknown == [404] * 5


def test_the_downloads_panel_sits_in_the_right_column_above_the_files(tmp_path):
    app, project, _store = downloads_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get(f"{base(project)}/chat").text
    files = page.index('class="workspace-files"')
    assert files < page.index('id="downloads-panel"') < page.index('id="file-browser-host"')


import asyncio, json
from dataclasses import replace
from pathlib import Path
import pytest
from app.coding import DownloadPolicy
from app.config import Settings
from app.download_fetch import FetchError, FetchResult
from app.download_manager import DownloadManager, DownloadTurn
from app.download_model_check import CheckError
from app.doc_convert import ConvertResult
from app.download_store import DownloadStore
from app.questions import prepare_spool

class FakeFetcher:
    def __init__(self, fail=None): self.calls, self.fail = [], fail
    async def fetch(self, url, *, check_first, project_root, folder, filename, request_id, budget):
        self.calls.append((url, check_first))
        if self.fail: raise FetchError(self.fail)
        granted = await budget.reserve(3); await budget.release(granted - 3)
        return FetchResult(url, f"{folder}/a.org/x.pdf", "ab" * 32, 3, "pdf")

def setup(tmp_path, *, approval="manual", model_check=False, fetcher=None, check=None,
          commands=None):
    settings = Settings(home=tmp_path / "home")
    project = tmp_path / "project"; project.mkdir(parents=True)
    root = project / ".delibra"; root.mkdir()
    temp = tmp_path / "temp"; (temp / "downloads" / "requests").mkdir(parents=True)
    manager = DownloadManager(settings, {"claude": "claude", "codex": "codex"} if commands is None else commands,
                              fetcher=fetcher or FakeFetcher(), model_check=check)
    turn = DownloadTurn(project_id="p1", project_root=project, store_root=root, temp_dir=temp,
                        auto_run=1, cycle=1, round_n=2, agent="s" * 32, agent_name="Alpha",
                        session_id="s" * 32, policy=DownloadPolicy(enabled=True, folder="raw", approval=approval, model_check=model_check))
    return manager, turn

def drop(turn, name, **fields):
    (turn.temp_dir / "downloads" / "requests" / f"{name}.json").write_text(json.dumps(fields))

def status(turn):
    return json.loads((turn.temp_dir / "downloads" / "status.json").read_text())["requests"]

async def drain(manager):
    await manager.idle()

@pytest.mark.asyncio
async def test_manual_waits_for_approval_then_downloads(tmp_path):
    manager, turn = setup(tmp_path)
    manager.begin_turn(turn)
    drop(turn, "a", url="https://arxiv.org/pdf/1706.03762", purpose="paper")
    manager.harvest(turn, final=False); await drain(manager)
    assert [r["state"] for r in status(turn)] == ["awaiting"]
    assert manager.turn_awaiting(turn) == ["D1"]
    manager.approve(turn.project_root, turn.store_root, "D1"); await drain(manager)
    assert status(turn)[0]["state"] == "downloaded"
    assert (turn.project_root / "raw" / "index.md").exists()
    await manager.stop_watch(turn)

@pytest.mark.asyncio
async def test_automatic_downloads_without_action_and_refuses_flagged(tmp_path):
    fetcher = FakeFetcher()
    manager, turn = setup(tmp_path, approval="automatic", fetcher=fetcher)
    manager.begin_turn(turn)
    drop(turn, "a", url="https://arxiv.org/pdf/1"); drop(turn, "b", url="https://u:p@a.org/x")
    manager.harvest(turn, final=True); await drain(manager)
    states = {r["id"]: (r["state"], r["url"]) for r in status(turn)}
    assert states["D1"][0] == "downloaded"
    assert states["D2"] == ("refused", "(URL hidden)")
    manager.allow(turn.project_root, turn.store_root, "D2"); await drain(manager)
    assert fetcher.calls[-1] == ("https://u:p@a.org/x", False)

@pytest.mark.asyncio
async def test_bad_spool_files_warn_and_create_nothing(tmp_path):
    manager, turn = setup(tmp_path)
    manager.begin_turn(turn)
    (turn.temp_dir / "downloads" / "requests" / "big.json").write_bytes(b"x" * 20000)
    drop(turn, "long", url="https://a.org/" + "a" * 5000)
    manager.harvest(turn, final=True)
    assert DownloadStore(turn.store_root).load().requests == []
    assert len(manager.turn_warnings(turn)) == 2

@pytest.mark.asyncio
async def test_model_check_flags_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("app.download_manager.BATCH_WINDOW_SECONDS", 0)  # batching is not under test
    async def check(provider, executable, model, items, work_root, prompts=None):
        return {rid: ("flag", "looks encoded") for rid, _ in items}
    manager, turn = setup(tmp_path, approval="automatic", model_check=True, check=check)
    manager.begin_turn(turn); drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=True); await manager.settle(turn); await drain(manager)
    assert status(turn)[0] == {"id": "D1", "request": "a.json", "url": "(URL hidden)", "state": "refused",
                               "reason": "flagged by model check"}
    assert DownloadStore(turn.store_root).get("D1").check_note == "looks encoded"
    async def broken(*args, **kwargs): raise CheckError("model check unavailable: timed out")
    manager2, turn2 = setup(tmp_path / "2", approval="automatic", model_check=True, check=broken)
    manager2.begin_turn(turn2); drop(turn2, "a", url="https://a.org/x.pdf")
    manager2.harvest(turn2, final=True); await manager2.settle(turn2)
    assert status(turn2)[0]["reason"] == "model check unavailable: timed out"

@pytest.mark.asyncio
async def test_each_model_check_reads_the_prompts_and_fails_closed_on_an_invalid_one(tmp_path, monkeypatch):
    monkeypatch.setattr("app.download_manager.BATCH_WINDOW_SECONDS", 0)  # batching is not under test
    from app.prompts import PromptError, Prompts

    seen = []

    async def check(provider, executable, model, items, work_root, prompts=None):
        seen.append(prompts.render("model_check", answer_format="FORMAT"))
        return {rid: ("allow", "") for rid, _ in items}

    loads = iter([Prompts({"model_check": "Edited check. {answer_format}"}, {})])

    def loader():
        prompts = next(loads, None)
        if prompts is None:
            raise PromptError("URL model check prompt is invalid: x; fix it in Settings")
        return prompts

    manager, turn = setup(tmp_path, approval="automatic", model_check=True, check=check)
    turn.prompts_loader = loader
    manager.begin_turn(turn); drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=False); await manager.settle(turn); await drain(manager)
    drop(turn, "b", url="https://a.org/y.pdf")
    manager.harvest(turn, final=True); await manager.settle(turn); await drain(manager)
    assert seen == ["Edited check. FORMAT"]
    assert status(turn)[1]["reason"] == (
        "model check unavailable: URL model check prompt is invalid: x; fix it in Settings"
    )

@pytest.mark.parametrize(("commands", "error", "reason"), [
    ({}, None, "model check unavailable: the CLI is not configured"),
    (None, RuntimeError("boom"), "model check unavailable: unexpected error"),
])
@pytest.mark.asyncio
async def test_an_unconfigured_or_crashing_check_refuses(tmp_path, monkeypatch, commands, error, reason):
    # Plan review B1: never leave a record `checking`.
    monkeypatch.setattr("app.download_manager.BATCH_WINDOW_SECONDS", 0)
    async def check(*args):
        raise error
    manager, turn = setup(tmp_path, approval="automatic", model_check=True, check=check, commands=commands)
    manager.begin_turn(turn); drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=True); await manager.settle(turn)
    assert status(turn)[0]["state"] == "refused" and status(turn)[0]["reason"] == reason

@pytest.mark.asyncio
async def test_check_chunks_run_concurrently(tmp_path, monkeypatch):
    # Plan review B4: 45 requests are three calls in flight at once.
    monkeypatch.setattr("app.download_manager.BATCH_WINDOW_SECONDS", 0)
    started, all_in = [], asyncio.Event()
    async def check(provider, executable, model, items, work_root, prompts=None):
        started.append(len(items))
        if len(started) == 3:
            all_in.set()
        await asyncio.wait_for(all_in.wait(), 2)
        return {rid: ("allow", "") for rid, _ in items}
    manager, turn = setup(tmp_path, model_check=True, check=check)
    manager.begin_turn(turn)
    for n in range(45):
        drop(turn, f"r{n}", url=f"https://a.org/{n}.pdf")
    manager.harvest(turn, final=True); await manager.settle(turn)
    assert sorted(started) == [5, 20, 20]
    assert {r["state"] for r in status(turn)} == {"awaiting"}

@pytest.mark.asyncio
async def test_settle_leaves_nothing_checking(tmp_path, monkeypatch):
    # Plan review B4: a slow check cannot outlive the turn's pause decision.
    monkeypatch.setattr("app.download_manager.BATCH_WINDOW_SECONDS", 0)
    monkeypatch.setattr("app.download_manager.SETTLE_SECONDS", 0.2)
    async def slow(*args, **kwargs):
        await asyncio.sleep(30)
    manager, turn = setup(tmp_path, approval="automatic", model_check=True, check=slow)
    manager.begin_turn(turn)
    for n in range(64):
        drop(turn, f"r{n}", url=f"https://a.org/{n}.pdf")
    manager.harvest(turn, final=True); await manager.settle(turn)
    records = status(turn)
    assert len(records) == 64
    assert {(r["state"], r["reason"]) for r in records} == {("refused", "model check unavailable: timed out")}

@pytest.mark.asyncio
async def test_failed_downloads_retry(tmp_path):
    fetcher = FakeFetcher(fail="HTTP 404")
    manager, turn = setup(tmp_path, approval="automatic", fetcher=fetcher)
    manager.begin_turn(turn); drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=True); await drain(manager)
    assert status(turn)[0] == {"id": "D1", "request": "a.json", "url": "https://a.org/x.pdf", "state": "failed", "reason": "HTTP 404"}
    fetcher.fail = None
    manager.retry(turn.project_root, turn.store_root, "D1"); await drain(manager)
    assert status(turn)[0]["state"] == "downloaded"

@pytest.mark.asyncio
async def test_actions_refuse_wrong_states(tmp_path):
    manager, turn = setup(tmp_path)
    manager.begin_turn(turn); drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=True)
    with pytest.raises(Exception, match="not refused"):
        manager.allow(turn.project_root, turn.store_root, "D1")
    manager.refuse(turn.project_root, turn.store_root, "D1")
    with pytest.raises(Exception, match="refused by you"):
        manager.allow(turn.project_root, turn.store_root, "D1")

@pytest.mark.asyncio
async def test_subscribers_get_the_current_revision_then_each_change(tmp_path):
    # Plan review S1: a (re)connection first receives the current revision.
    manager, turn = setup(tmp_path)
    manager.begin_turn(turn)
    current = manager.revision("p1")
    events = manager.subscribe("p1")
    assert await asyncio.wait_for(anext(events), 1) == current
    drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=True)
    assert await asyncio.wait_for(anext(events), 1) > current
    await events.aclose()
    await manager.stop_watch(turn)

@pytest.mark.asyncio
async def test_a_late_approval_is_charged_to_its_run(tmp_path):
    manager, turn = setup(tmp_path)
    manager.begin_turn(turn); drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=True); await manager.stop_watch(turn)
    manager.approve(turn.project_root, turn.store_root, "D1"); await drain(manager)
    book = DownloadStore(turn.store_root).load()
    assert book.run_bytes == {"1": 3} and book.reservations == {}


@pytest.mark.asyncio
async def test_unreadable_records_warn_instead_of_failing_the_turn(tmp_path):
    manager, turn = setup(tmp_path)
    (turn.store_root / "downloads").mkdir()
    (turn.store_root / "downloads" / "requests.json").write_text("{")
    manager.begin_turn(turn)
    drop(turn, "a", url="https://a.org/x.pdf")
    await manager.stop_watch(turn)
    manager.harvest(turn, final=True)
    await manager.settle(turn)
    assert manager.turn_awaiting(turn) == []
    assert any("download records are unreadable" in w for w in manager.turn_warnings(turn))
    # The request file stays for a later look; nothing was recorded.
    assert (turn.spool / "a.json").exists()

@pytest.mark.asyncio
async def test_the_wait_pattern_matches_only_this_turns_request(tmp_path):
    # Review 2026-11-15: the old example loop ended on any downloaded record,
    # e.g. one from an earlier turn of the run with the same file name.
    import subprocess
    from app.download_rules import wait_pattern
    manager, turn = setup(tmp_path, approval="automatic")
    manager.begin_turn(turn)
    drop(turn, "d1", url="https://arxiv.org/pdf/1")
    manager.harvest(turn, final=True); await drain(manager)
    await manager.stop_watch(turn); manager.end_turn(turn)
    later = replace(turn, round_n=3, ids=[], files={}, warnings=[], unparsable={}, checks=[], watch=None,
                    policy=replace(turn.policy, approval="manual"))
    manager.begin_turn(later)
    drop(later, "d1", url="https://arxiv.org/pdf/2")
    manager.harvest(later, final=True); await drain(manager)
    path = later.temp_dir / "downloads" / "status.json"
    def matches():
        return subprocess.run(["grep", "-Eq", wait_pattern("d1.json"), str(path)]).returncode == 0
    assert [(r["id"], r["state"], r.get("request")) for r in status(later)] == [
        ("D1", "downloaded", None), ("D2", "awaiting", "d1.json")]
    assert not matches()
    manager.approve(later.project_root, later.store_root, "D2"); await drain(manager)
    assert matches()
    await manager.stop_watch(later)

@pytest.mark.asyncio
async def test_closing_ends_settle_at_once(tmp_path):
    # Review 2026-11-15: shutdown stops Auto first, and Auto waits on a turn
    # that sits in settle; settle must not hold shutdown for its 75 s.
    async def hangs(*args):
        await asyncio.sleep(3600)
    manager, turn = setup(tmp_path, approval="automatic", model_check=True, check=hangs)
    manager.begin_turn(turn); drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=True)
    settling = asyncio.create_task(manager.settle(turn))
    await asyncio.sleep(0.1)
    manager.close()
    await asyncio.wait_for(settling, 2)
    # A request harvested after closing (Auto's last harvest) is not waited for either.
    drop(turn, "b", url="https://a.org/y.pdf")
    manager.harvest(turn, final=True)
    await asyncio.wait_for(manager.settle(turn), 2)
    await asyncio.wait_for(manager.shutdown(), 2)
    records = DownloadStore(turn.store_root).load().requests
    assert [(r.state, r.masked) for r in records] == [("refused", True), ("refused", True)]


def settings_for(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


def chat_turn(tmp_path: Path, *, session: str, round_n: int, convert: bool = False,
              fresh: bool = True) -> DownloadTurn:
    workspace = tmp_path / "sessions" / session / "workspace"
    workspace.mkdir(parents=True, exist_ok=not fresh)
    prepare_spool(workspace, "downloads", "requests", extra_names=("status.json",))
    if convert:
        prepare_spool(workspace, "convert", "requests", extra_names=("status.json",))
    return DownloadTurn(
        project_id="p" * 32, project_root=tmp_path / "project", store_root=tmp_path / "delibra",
        temp_dir=workspace, auto_run=0, cycle=None, round_n=round_n,
        agent=session, agent_name=session[:4], session_id=session,
        policy=DownloadPolicy(enabled=True, approval="manual", convert=convert), convert=convert,
    )


def write_request(spool: Path, name: str, url: str) -> None:
    (spool / name).write_text(json.dumps({"url": url, "purpose": "test"}), encoding="utf-8")


@pytest.mark.asyncio
async def test_two_chat_turns_in_one_project_keep_their_own_status(tmp_path) -> None:
    manager = DownloadManager(settings_for(tmp_path), {})
    first = chat_turn(tmp_path, session="a" * 32, round_n=1)
    second = chat_turn(tmp_path, session="b" * 32, round_n=1)
    manager.begin_turn(first)
    manager.begin_turn(second)
    write_request(first.spool, "d1.json", "https://a.example/one.pdf")
    write_request(second.spool, "d1.json", "https://b.example/two.pdf")

    manager.harvest(first, final=True)
    manager.harvest(second, final=True)

    first_status = (first.temp_dir / "downloads" / "status.json").read_text()
    second_status = (second.temp_dir / "downloads" / "status.json").read_text()
    assert "one.pdf" in first_status and "two.pdf" not in first_status
    assert "two.pdf" in second_status and "one.pdf" not in second_status
    await manager.stop_watch(first)
    await manager.stop_watch(second)
    manager.end_turn(first)
    manager.end_turn(second)


class PdfFetcher:
    async def fetch(self, url, *, check_first, project_root, folder, filename, request_id, budget):
        target = project_root / folder / "paper.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.7 x")
        return FetchResult(url, f"{folder}/paper.pdf", "ab" * 32, 10, "pdf")


def pdf_fetcher() -> PdfFetcher:
    return PdfFetcher()


async def download_one(manager: DownloadManager, tmp_path: Path, *, convert: bool):
    (tmp_path / "project").mkdir(exist_ok=True)
    turn = chat_turn(tmp_path, session="c" * 32, round_n=1)
    turn.policy = DownloadPolicy(enabled=True, folder="raw", approval="automatic", convert=convert)
    manager.begin_turn(turn)
    write_request(turn.spool, "d1.json", "https://a.example/paper.pdf")
    manager.harvest(turn, final=True)
    await manager.idle()
    await manager.stop_watch(turn)
    manager.end_turn(turn)
    return DownloadStore(tmp_path / "delibra").get("D1")


@pytest.mark.asyncio
async def test_a_downloaded_pdf_is_converted_beside_itself(tmp_path) -> None:
    calls = []

    async def fake_convert(staged, **kwargs):
        calls.append(kwargs["display_path"])
        return ConvertResult("converted", None, b"# converted\n", "pdf")

    manager = DownloadManager(settings_for(tmp_path), {}, fetcher=pdf_fetcher(), converter=fake_convert)
    record = await download_one(manager, tmp_path, convert=True)

    assert calls == [record.path]
    saved = DownloadStore(tmp_path / "delibra").get(record.id)
    assert (saved.convert_state, saved.converted_path) == ("converted", f"{record.path}.md")
    assert (tmp_path / "project" / f"{record.path}.md").read_bytes() == b"# converted\n"
    index = (tmp_path / "project" / "raw" / "index.md").read_text()
    assert f"{record.path}.md" in index


@pytest.mark.asyncio
async def test_a_failed_conversion_keeps_the_download(tmp_path) -> None:
    async def failing(staged, **kwargs):
        return ConvertResult("failed", "converter exited with 3")

    manager = DownloadManager(settings_for(tmp_path), {}, fetcher=pdf_fetcher(), converter=failing)
    record = await download_one(manager, tmp_path, convert=True)

    saved = DownloadStore(tmp_path / "delibra").get(record.id)
    assert saved.state == "downloaded"
    assert (saved.convert_state, saved.convert_reason) == ("failed", "converter exited with 3")


@pytest.mark.asyncio
async def test_an_unsupported_download_is_skipped_with_magikas_label(tmp_path) -> None:
    async def unsupported(staged, **kwargs):
        return ConvertResult("unsupported", "not a supported document (Magika: html)", kind="html")

    manager = DownloadManager(settings_for(tmp_path), {}, fetcher=pdf_fetcher(), converter=unsupported)
    record = await download_one(manager, tmp_path, convert=True)

    saved = DownloadStore(tmp_path / "delibra").get(record.id)
    assert (saved.convert_state, saved.convert_reason) == (
        "skipped", "not a supported document (Magika: html)"
    )


@pytest.mark.asyncio
async def test_conversion_off_leaves_downloads_alone(tmp_path) -> None:
    async def never(*args, **kwargs):
        raise AssertionError("must not convert")

    manager = DownloadManager(settings_for(tmp_path), {}, fetcher=pdf_fetcher(), converter=never)
    record = await download_one(manager, tmp_path, convert=False)
    assert DownloadStore(tmp_path / "delibra").get(record.id).convert_state is None


@pytest.mark.asyncio
async def test_a_convert_request_converts_once_and_reuses_unchanged_files(tmp_path) -> None:
    calls = []

    async def fake_convert(staged, **kwargs):
        calls.append(kwargs["display_path"])
        return ConvertResult("converted", None, b"# md\n", "pdf")

    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "a.pdf").write_bytes(b"%PDF-1.7 a")
    manager = DownloadManager(settings_for(tmp_path), {}, converter=fake_convert)
    for round_n in (1, 2):
        turn = chat_turn(tmp_path, session="a" * 32, round_n=round_n, convert=True, fresh=round_n == 1)
        manager.begin_turn(turn)
        (turn.convert_spool / "c1.json").write_text('{"path": "docs/a.pdf"}')
        manager.harvest(turn, final=True)
        await manager.idle()
        await manager.stop_watch(turn)
        manager.end_turn(turn)

    assert calls == ["docs/a.pdf"]
    assert (project / "docs/source/raw/converted/docs/a.pdf.md").read_bytes() == b"# md\n"
    status = (turn.temp_dir / "convert" / "status.json").read_text()
    assert '"state": "converted"' in status and '"reason": "reused"' in status


@pytest.mark.asyncio
@pytest.mark.parametrize("path, reason", [
    ("../x.pdf", "not a convertible project file"),
    (".delibra/x.pdf", "not a convertible project file"),
    ("notes.txt", "not a supported document"),
    ("link.pdf", "not a convertible project file"),
])
async def test_bad_convert_requests_are_refused_with_reasons(tmp_path, path, reason) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("hello")
    (project / "real.pdf").write_bytes(b"%PDF-1.7")
    (project / "link.pdf").symlink_to(project / "real.pdf")

    async def unsupported(staged, **kwargs):  # what Magika says about notes.txt
        return ConvertResult("unsupported", "not a supported document (Magika: txt)", kind="txt")

    manager = DownloadManager(settings_for(tmp_path), {}, converter=unsupported)
    turn = chat_turn(tmp_path, session="b" * 32, round_n=1, convert=True)
    manager.begin_turn(turn)
    (turn.convert_spool / "c1.json").write_text(json.dumps({"path": path}))
    manager.harvest(turn, final=True)
    await manager.idle()

    status = (turn.temp_dir / "convert" / "status.json").read_text()
    assert '"state": "refused"' in status
    assert reason in status
    await manager.stop_watch(turn)
    manager.end_turn(turn)


@pytest.mark.asyncio
async def test_a_conversions_only_turn_reads_no_download_requests(tmp_path) -> None:
    async def fake_convert(staged, **kwargs):
        return ConvertResult("converted", None, b"# md\n", "pdf")

    project = tmp_path / "project"
    project.mkdir()
    (project / "a.pdf").write_bytes(b"%PDF-1.7 a")
    manager = DownloadManager(settings_for(tmp_path), {}, converter=fake_convert)
    turn = chat_turn(tmp_path, session="d" * 32, round_n=1, convert=True)
    turn.policy = DownloadPolicy(enabled=False, convert=True)
    import shutil
    shutil.rmtree(turn.temp_dir / "downloads")  # a convert-only turn has no download spool
    manager.begin_turn(turn)
    (turn.convert_spool / "c1.json").write_text('{"path": "a.pdf"}')
    for _ in range(100):  # the watcher picks it up live
        if (project / "docs/source/raw/converted/a.pdf.md").exists():
            break
        await asyncio.sleep(0.02)
    manager.harvest(turn, final=True)
    await manager.idle()
    await manager.stop_watch(turn)
    manager.end_turn(turn)

    assert DownloadStore(tmp_path / "delibra").load().requests == []
    assert not (turn.temp_dir / "downloads").exists()
    assert '"state": "converted"' in (turn.temp_dir / "convert" / "status.json").read_text()
    assert turn.warnings == []


@pytest.mark.asyncio
async def test_a_late_conversion_does_not_overwrite_the_next_turns_status(tmp_path) -> None:
    release = asyncio.Event()

    async def slow_convert(staged, **kwargs):
        await release.wait()
        return ConvertResult("converted", None, b"# md\n", "pdf")

    project = tmp_path / "project"
    project.mkdir()
    (project / "a.pdf").write_bytes(b"%PDF-1.7 a")
    manager = DownloadManager(settings_for(tmp_path), {}, converter=slow_convert)
    first = chat_turn(tmp_path, session="e" * 32, round_n=1, convert=True)
    manager.begin_turn(first)
    (first.convert_spool / "c1.json").write_text('{"path": "a.pdf"}')
    manager.harvest(first, final=True)
    await manager.stop_watch(first)
    manager.end_turn(first)

    second = chat_turn(tmp_path, session="e" * 32, round_n=2, convert=True, fresh=False)
    manager.begin_turn(second)
    (second.convert_spool / "c1.json").write_text('{"path": "../x.pdf"}')  # refused at once
    manager.harvest(second, final=False)
    await manager.stop_watch(second)  # no watcher rewrite: the late result's own write counts
    release.set()
    await manager.idle()

    lines = (second.temp_dir / "convert" / "status.json").read_text().splitlines()
    assert any('"id": "C2", "request": "c1.json"' in line and '"refused"' in line for line in lines)
    assert any('"id": "C1"' in line and '"request"' not in line and '"converted"' in line for line in lines)
    manager.end_turn(second)


@pytest.mark.asyncio
async def test_only_one_conversion_runs_at_a_time(tmp_path) -> None:
    active, peak = 0, 0

    async def counting(staged, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return ConvertResult("converted", None, b"# md\n", "pdf")

    project = tmp_path / "project"
    project.mkdir()
    for name in ("a", "b", "c"):
        (project / f"{name}.pdf").write_bytes(b"%PDF-1.7 " + name.encode())
    manager = DownloadManager(settings_for(tmp_path), {}, fetcher=pdf_fetcher(), converter=counting)
    turn = chat_turn(tmp_path, session="f" * 32, round_n=1, convert=True)
    turn.policy = DownloadPolicy(enabled=True, folder="raw", approval="automatic", convert=True)
    manager.begin_turn(turn)
    write_request(turn.spool, "d1.json", "https://a.example/paper.pdf")
    for name in ("a", "b", "c"):
        (turn.convert_spool / f"{name}.json").write_text(json.dumps({"path": f"{name}.pdf"}))
    manager.harvest(turn, final=True)
    await manager.idle()
    await manager.stop_watch(turn)
    manager.end_turn(turn)
    assert peak == 1


@pytest.mark.asyncio
async def test_a_harvest_after_shutdown_records_but_starts_nothing(tmp_path) -> None:
    from app.convert_store import ConversionStore

    async def never(*args, **kwargs):
        raise AssertionError("no conversion may start after shutdown")

    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "a.pdf").write_bytes(b"%PDF-1.7")
    manager = DownloadManager(settings_for(tmp_path), {}, fetcher=pdf_fetcher(), converter=never)
    turn = chat_turn(tmp_path, session="9" * 32, round_n=1, convert=True)
    turn.policy = DownloadPolicy(enabled=True, folder="raw", approval="automatic", convert=True)
    manager.begin_turn(turn)
    await manager.stop_watch(turn)
    await manager.shutdown()
    write_request(turn.spool, "d1.json", "https://a.example/paper.pdf")
    (turn.convert_spool / "c1.json").write_text('{"path": "a.pdf"}')

    manager.harvest(turn, final=True)  # a chat reply finalised after the server's last shutdown step

    assert manager._tasks == set()
    manager.recover(turn.store_root)
    assert DownloadStore(turn.store_root).get("D1").state == "failed"
    assert [r.state for r in ConversionStore(turn.store_root).load()] == ["failed"]


@pytest.mark.parametrize("agent, build, shell", [
    ("claude", False, False), ("codex", False, True), ("claude", True, True), ("codex", True, True),
])
def test_turn_notes_follow_the_one_shell_rule(tmp_path, agent, build, shell) -> None:
    from app.download_manager import has_shell, turn_spool_notes

    assert has_shell(agent, build=build) is shell
    workspace = tmp_path / agent / str(build)
    workspace.mkdir(parents=True)
    notes = "".join(turn_spool_notes(workspace, DownloadPolicy(enabled=True, convert=True), agent=agent, build=build))
    assert ("until grep" in notes) is shell
    assert f"{(workspace / 'downloads' / 'requests').as_posix()}/" in notes
    assert f"{(workspace / 'convert' / 'requests').as_posix()}/ named like c1.json" in notes
    # Discussion and chat turns get fresh spools here; build turns get them from the runner.
    assert (workspace / "convert" / "requests").is_dir() is not build


@pytest.mark.asyncio
async def test_idle_returns_when_a_finished_task_is_not_yet_discarded(tmp_path, monkeypatch):
    # Python 3.12's gather completes at once, without yielding, when every task
    # is already done. A task that finished but whose discard callback is still
    # queued then kept idle() spinning forever at 100% CPU (seen in the suite).
    manager = DownloadManager(settings_for(tmp_path), {})

    async def nothing():
        return None

    task = asyncio.create_task(nothing())
    manager._tasks.add(task)
    task.add_done_callback(manager._tasks.discard)
    await asyncio.sleep(0)  # the task finishes; its discard callback is only queued
    assert task.done() and task in manager._tasks

    real_gather, calls = asyncio.gather, []

    def bounded_gather(*args, **kwargs):
        calls.append(1)
        if len(calls) > 50:  # fail instead of hanging the suite
            raise AssertionError("idle() never let the finished task be discarded")
        return real_gather(*args, **kwargs)

    monkeypatch.setattr(asyncio, "gather", bounded_gather)
    await manager.idle()
    assert not manager._tasks


class GatedConverter:
    """Converts once `release` is set; records each call and tells when one started."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def __call__(self, staged, **kwargs):
        self.calls.append(kwargs["display_path"])
        self.started.set()
        await self.release.wait()
        return ConvertResult("converted", None, b"# md\n", "pdf")


def convert_project(tmp_path: Path, *names: str) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    for name in names:
        (project / name).write_bytes(b"%PDF-1.7 " + name.encode())
    return project


def request_conversions(turn: DownloadTurn, *names: str) -> None:
    for index, name in enumerate(names, start=1):
        (turn.convert_spool / f"c{index}.json").write_text(json.dumps({"path": name}))


def convert_status(turn: DownloadTurn) -> dict[str, dict]:
    lines = (turn.temp_dir / "convert" / "status.json").read_text()
    return {item["id"]: item for item in json.loads(lines)["requests"]}


async def until(condition, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_conversion_is_queued_until_it_holds_the_slot(tmp_path) -> None:
    from app.convert_store import ConversionStore

    convert_project(tmp_path, "a.pdf", "b.pdf")
    converter = GatedConverter()
    manager = DownloadManager(settings_for(tmp_path), {}, converter=converter)
    turn = chat_turn(tmp_path, session="g" * 32, round_n=1, convert=True)
    manager.begin_turn(turn)
    request_conversions(turn, "a.pdf", "b.pdf")
    manager.harvest(turn, final=True)
    await converter.started.wait()

    states = {r.id: r.state for r in ConversionStore(turn.store_root).load()}
    assert states == {"C1": "converting", "C2": "queued"}
    assert convert_status(turn)["C2"]["state"] == "queued"
    view = manager.conversion_jobs(turn.store_root)
    assert view["C1"].position is None and view["C1"].started_at is not None
    assert view["C2"].position == 1

    converter.release.set()
    await manager.idle()
    assert {r.state for r in ConversionStore(turn.store_root).load()} == {"converted"}
    assert manager.conversion_jobs(turn.store_root) == {}
    await manager.stop_watch(turn)
    manager.end_turn(turn)


@pytest.mark.asyncio
async def test_a_download_waits_queued_behind_a_running_conversion(tmp_path) -> None:
    convert_project(tmp_path, "a.pdf")
    converter = GatedConverter()
    manager = DownloadManager(settings_for(tmp_path), {}, fetcher=pdf_fetcher(), converter=converter)
    turn = chat_turn(tmp_path, session="h" * 32, round_n=1, convert=True)
    turn.policy = DownloadPolicy(enabled=True, folder="raw", approval="automatic", convert=True)
    manager.begin_turn(turn)
    request_conversions(turn, "a.pdf")
    manager.harvest(turn, final=True)
    await converter.started.wait()
    write_request(turn.spool, "d1.json", "https://a.example/paper.pdf")
    manager.harvest(turn, final=True)
    store = DownloadStore(turn.store_root)
    await until(lambda: store.get("D1").convert_state is not None)

    assert (store.get("D1").state, store.get("D1").convert_state) == ("downloaded", "queued")
    assert manager.conversion_jobs(turn.store_root)["D1"].position == 1

    converter.release.set()
    await manager.idle()
    assert store.get("D1").convert_state == "converted"
    await manager.stop_watch(turn)
    manager.end_turn(turn)


@pytest.mark.asyncio
async def test_cancelling_a_queued_request_never_converts_it(tmp_path) -> None:
    from app.convert_store import ConversionStore

    convert_project(tmp_path, "a.pdf", "b.pdf")
    converter = GatedConverter()
    manager = DownloadManager(settings_for(tmp_path), {}, converter=converter)
    turn = chat_turn(tmp_path, session="i" * 32, round_n=1, convert=True)
    manager.begin_turn(turn)
    request_conversions(turn, "a.pdf", "b.pdf")
    manager.harvest(turn, final=True)
    await converter.started.wait()

    await manager.cancel_conversion(turn.store_root, "C2")

    record = {r.id: r for r in ConversionStore(turn.store_root).load()}["C2"]
    assert (record.state, record.reason) == ("cancelled", "cancelled by the user")
    assert convert_status(turn)["C2"]["state"] == "cancelled"
    assert convert_status(turn)["C2"]["reason"] == "cancelled by the user"
    converter.release.set()
    await manager.idle()
    assert converter.calls == ["a.pdf"]
    await manager.stop_watch(turn)
    manager.end_turn(turn)


@pytest.mark.asyncio
async def test_a_request_cancelled_before_its_task_ran_is_cancelled(tmp_path) -> None:
    from app.convert_store import ConversionStore

    convert_project(tmp_path, "a.pdf")
    converter = GatedConverter()
    manager = DownloadManager(settings_for(tmp_path), {}, converter=converter)
    turn = chat_turn(tmp_path, session="j" * 32, round_n=1, convert=True)
    manager.begin_turn(turn)
    request_conversions(turn, "a.pdf")
    manager.harvest(turn, final=True)  # its task has not run a single step yet

    await manager.cancel_conversion(turn.store_root, "C1")

    assert [r.state for r in ConversionStore(turn.store_root).load()] == ["cancelled"]
    await manager.idle()
    assert converter.calls == []
    await manager.stop_watch(turn)
    manager.end_turn(turn)


@pytest.mark.asyncio
async def test_cancelling_a_download_conversion_keeps_the_download(tmp_path) -> None:
    converter = GatedConverter()
    manager = DownloadManager(settings_for(tmp_path), {}, fetcher=pdf_fetcher(), converter=converter)
    (tmp_path / "project").mkdir()
    turn = chat_turn(tmp_path, session="k" * 32, round_n=1)
    turn.policy = DownloadPolicy(enabled=True, folder="raw", approval="automatic", convert=True)
    manager.begin_turn(turn)
    write_request(turn.spool, "d1.json", "https://a.example/paper.pdf")
    manager.harvest(turn, final=True)
    await converter.started.wait()
    store = DownloadStore(turn.store_root)
    assert store.get("D1").convert_state == "converting"

    await manager.cancel_conversion(turn.store_root, "D1")

    saved = store.get("D1")
    assert (saved.state, saved.convert_state, saved.convert_reason) == (
        "downloaded", "cancelled", "cancelled by the user"
    )
    assert status(turn)[0]["convert_state"] == "cancelled"
    await manager.idle()
    await manager.stop_watch(turn)
    manager.end_turn(turn)


@pytest.mark.asyncio
async def test_a_finished_or_unknown_conversion_cannot_be_cancelled(tmp_path) -> None:
    from app.storage import StorageError

    convert_project(tmp_path, "a.pdf")
    converter = GatedConverter()
    converter.release.set()
    manager = DownloadManager(settings_for(tmp_path), {}, converter=converter)
    turn = chat_turn(tmp_path, session="l" * 32, round_n=1, convert=True)
    manager.begin_turn(turn)
    request_conversions(turn, "a.pdf")
    manager.harvest(turn, final=True)
    await manager.idle()

    with pytest.raises(StorageError, match="C1 has nothing to cancel: it is converted"):
        await manager.cancel_conversion(turn.store_root, "C1")
    with pytest.raises(StorageError, match="C9 has nothing to cancel"):
        await manager.cancel_conversion(turn.store_root, "C9")
    await manager.stop_watch(turn)
    manager.end_turn(turn)


@pytest.mark.asyncio
async def test_a_shutdown_leaves_queued_conversions_to_restart_recovery(tmp_path) -> None:
    from app.convert_store import ConversionStore

    convert_project(tmp_path, "a.pdf", "b.pdf")
    converter = GatedConverter()
    manager = DownloadManager(settings_for(tmp_path), {}, converter=converter)
    turn = chat_turn(tmp_path, session="m" * 32, round_n=1, convert=True)
    manager.begin_turn(turn)
    request_conversions(turn, "a.pdf", "b.pdf")
    manager.harvest(turn, final=True)
    await converter.started.wait()
    await manager.stop_watch(turn)

    await manager.shutdown()

    conversions = ConversionStore(turn.store_root)
    assert [r.state for r in conversions.load()] == ["converting", "queued"]
    manager.recover(turn.store_root)
    assert {(r.state, r.reason) for r in conversions.load()} == {("failed", "interrupted by restart")}


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox-exec")
@pytest.mark.asyncio
async def test_cancelling_a_running_conversion_kills_its_worker(tmp_path) -> None:
    import os
    from functools import partial
    from app.convert_store import ConversionStore
    from app.doc_convert import convert_file

    worker = tmp_path / "stub_worker" / "worker.py"
    worker.parent.mkdir()
    worker.write_text(
        "import json, os, pathlib, sys, time\n"
        "source, output, kind = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "if kind == 'identify':\n"
        "    open(output, 'w').write(json.dumps({'label': 'pdf', 'score': 0.99, 'pages': 2, 'is_text': False}))\n"
        "    raise SystemExit(0)\n"
        "out = pathlib.Path(output)\n"
        "out.with_name('pid.tmp').write_text(str(os.getpid()))\n"
        "out.with_name('pid.tmp').rename(out.with_name('pid'))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    convert_project(tmp_path, "a.pdf")
    manager = DownloadManager(settings_for(tmp_path), {}, converter=partial(convert_file, worker=worker))
    turn = chat_turn(tmp_path, session="n" * 32, round_n=1, convert=True)
    manager.begin_turn(turn)
    request_conversions(turn, "a.pdf")
    manager.harvest(turn, final=True)
    scratch = turn.store_root / "downloads" / ".converting"
    await until(lambda: any(scratch.glob("convert-*/pid")))
    pid = int(next(scratch.glob("convert-*/pid")).read_text())
    assert manager.conversion_jobs(turn.store_root)["C1"].limit == 60 + 300  # no models: pypdf only

    await manager.cancel_conversion(turn.store_root, "C1")

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert [(r.state, r.reason) for r in ConversionStore(turn.store_root).load()] == [
        ("cancelled", "cancelled by the user")
    ]
    assert list(scratch.iterdir()) == []
    await manager.idle()
    await manager.stop_watch(turn)
    manager.end_turn(turn)

import asyncio, json
from dataclasses import replace
from pathlib import Path
import pytest
from app.coding import DownloadPolicy
from app.config import Settings
from app.download_fetch import FetchError, FetchResult
from app.download_manager import DownloadManager, DownloadTurn
from app.download_model_check import CheckError
from app.download_store import DownloadStore

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
                        policy=DownloadPolicy(enabled=True, folder="raw", approval=approval, model_check=model_check))
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
async def test_model_check_flags_and_fails_closed(tmp_path):
    async def check(provider, executable, model, items, work_root):
        return {rid: ("flag", "looks encoded") for rid, _ in items}
    manager, turn = setup(tmp_path, approval="automatic", model_check=True, check=check)
    manager.begin_turn(turn); drop(turn, "a", url="https://a.org/x.pdf")
    manager.harvest(turn, final=True); await manager.settle(turn); await drain(manager)
    assert status(turn)[0] == {"id": "D1", "url": "(URL hidden)", "state": "refused",
                               "reason": "flagged by model check"}
    assert DownloadStore(turn.store_root).get("D1").check_note == "looks encoded"
    async def broken(*args): raise CheckError("model check unavailable: timed out")
    manager2, turn2 = setup(tmp_path / "2", approval="automatic", model_check=True, check=broken)
    manager2.begin_turn(turn2); drop(turn2, "a", url="https://a.org/x.pdf")
    manager2.harvest(turn2, final=True); await manager2.settle(turn2)
    assert status(turn2)[0]["reason"] == "model check unavailable: timed out"

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
    async def check(provider, executable, model, items, work_root):
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
    async def slow(*args):
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
    assert status(turn)[0] == {"id": "D1", "url": "https://a.org/x.pdf", "state": "failed", "reason": "HTTP 404"}
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

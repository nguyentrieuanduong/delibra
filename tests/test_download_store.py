import json, os
from pathlib import Path
import pytest
from app.coding import DownloadPolicy
from app.download_store import (
    RUN_MAX_BYTES, DownloadStore, project_download_excludes, render_index,
    render_status, write_project_file, write_turn_status,
)
from app.storage import ProjectStore, RegistryStore, StorageError

def add(store: DownloadStore, url: str = "https://a.org/x.pdf", **extra):
    fields = dict(purpose="p", filename=None, flags=[], auto_run=1, cycle=1, round_n=2,
                  agent="s" * 32, agent_name="Alpha", folder="raw")
    fields.update(extra)
    return store.add(url, **fields)

def test_ids_revision_and_reload(tmp_path: Path) -> None:
    store = DownloadStore(tmp_path)
    first, second = add(store), add(store)
    assert (first.id, second.id) == ("D1", "D2")
    assert store.load().revision == 2
    store.update("D1", state="awaiting")
    assert DownloadStore(tmp_path).get("D1").state == "awaiting"

def test_flagged_records_are_masked_everywhere(tmp_path: Path) -> None:
    store = DownloadStore(tmp_path)
    record = add(store, "https://" + "m" * 40 + ".evil.org/x?k=ghp_secret", flags=["secret", "encoded-blob"])
    assert record.masked and record.state == "refused"
    status = render_status([record]).decode()
    assert "ghp_secret" not in status and "evil.org" not in status and "(URL hidden)" in status
    assert '"state": "refused"' in status

def test_model_notes_are_never_rendered(tmp_path: Path) -> None:
    store = DownloadStore(tmp_path)
    record = store.update(add(store).id, state="refused", reason="flagged by model check",
                          masked=True, check_note="echo https://a.org/x.pdf")
    assert "a.org" not in render_status([record]).decode()

def test_budget_never_exceeds_the_cap(tmp_path: Path) -> None:
    store = DownloadStore(tmp_path)
    assert store.reserve(1, RUN_MAX_BYTES - 10, "D1") == RUN_MAX_BYTES - 10
    assert store.reserve(1, 100, "D2") == 10
    assert store.reserve(1, 1, "D2") == 0
    store.release(1, 50, "D1")
    assert store.reserve(1, 100, "D2") == 50
    book = DownloadStore(tmp_path).load()
    assert book.run_bytes == {"1": RUN_MAX_BYTES}
    assert book.reservations == {"D1": RUN_MAX_BYTES - 60, "D2": 60}

def test_recovery_returns_reservations_of_interrupted_downloads(tmp_path: Path) -> None:
    # Plan review S2: a crash skips the fetcher's release.
    store = DownloadStore(tmp_path)
    first, second = add(store), add(store)
    store.update(first.id, state="downloading")
    store.update(second.id, state="downloaded")
    store.reserve(1, 500, first.id)
    store.reserve(1, 3, second.id)
    store.finish_reservation(second.id)
    store.recover()
    book = store.load()
    assert book.run_bytes == {"1": 3} and book.reservations == {}

def test_project_excludes_fail_closed_on_unreadable_records(tmp_path: Path) -> None:
    # Plan review B5.
    project_dir = tmp_path / "project"; project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("P", project_dir)
    store = ProjectStore(project)
    assert project_download_excludes(store) == ()
    policy = store.coding_policy()
    policy.downloads = DownloadPolicy(enabled=True, folder="raw")
    store.replace_coding_policy(policy)
    add(DownloadStore(store.root), folder="old")
    assert project_download_excludes(store) == ("old", "raw")
    (store.root / "downloads" / "requests.json").write_text("{")
    assert project_download_excludes(store) is None

def test_recovery(tmp_path: Path) -> None:
    store = DownloadStore(tmp_path)
    for state in ("queued", "downloading", "checking", "awaiting"):
        store.update(add(store).id, state=state)
    assert store.recover() == 3
    states = [(r.state, r.reason) for r in store.load().requests]
    assert states == [
        ("failed", "interrupted by restart"), ("failed", "interrupted by restart"),
        ("refused", "model check interrupted"), ("awaiting", None),
    ]
    # Like every other check refusal, it is masked (design, Masking).
    assert [r.masked for r in store.load().requests] == [False, False, True, False]

@pytest.mark.parametrize("payload", [
    {"format": "delibra-downloads/1", "requests": [{}]},
    {"format": "delibra-downloads/1", "revision": "x"},
    {"format": "delibra-downloads/1", "requests": "nope"},
    [],
])
def test_malformed_records_are_a_storage_error(tmp_path: Path, payload) -> None:
    (tmp_path / "downloads").mkdir()
    (tmp_path / "downloads" / "requests.json").write_text(json.dumps(payload))
    with pytest.raises(StorageError):
        DownloadStore(tmp_path).load()


def test_used_folders(tmp_path: Path) -> None:
    store = DownloadStore(tmp_path)
    add(store, folder="a"); add(store, folder="b")
    assert store.used_folders() == {"a", "b"}

def test_status_write_refuses_a_symlinked_downloads_dir(tmp_path: Path) -> None:
    temp = tmp_path / "temp"; temp.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (temp / "downloads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        write_turn_status(temp, b"{}")
    assert list(outside.iterdir()) == []

def test_status_write_replaces_atomically(tmp_path: Path) -> None:
    temp = tmp_path / "temp"; (temp / "downloads").mkdir(parents=True)
    write_turn_status(temp, b"one"); write_turn_status(temp, b"two")
    assert (temp / "downloads" / "status.json").read_bytes() == b"two"

def test_project_file_write_refuses_symlinked_folder(tmp_path: Path) -> None:
    project = tmp_path / "project"; project.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (project / "raw").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception):
        write_project_file(project, "raw", "index.md", b"x")
    assert list(outside.iterdir()) == []

def test_index_lists_downloaded_only(tmp_path: Path) -> None:
    store = DownloadStore(tmp_path)
    done = store.update(add(store).id, state="downloaded", path="raw/a.org/x.pdf",
                        sha256="ab" * 32, size=3, detected_type="pdf")
    add(store)
    text = render_index(store.load().requests).decode()
    assert "D1" in text and "raw/a.org/x.pdf" in text and "D2" not in text

def test_status_puts_each_request_on_one_line_with_its_file_name(tmp_path: Path) -> None:
    # Review 2026-11-15: a wait loop greps one line for the agent's own request.
    store = DownloadStore(tmp_path)
    first, second = add(store), add(store)
    text = render_status([first, second], requests={second.id: "d1.json"}).decode()
    lines = [line for line in text.splitlines() if '"id"' in line]
    assert len(lines) == 2 and '"request"' not in lines[0]
    assert '"request": "d1.json"' in lines[1] and lines[1].index('"request"') < lines[1].index('"state"')
    assert json.loads(text)["requests"][1]["request"] == "d1.json"

def test_a_recreated_records_file_gets_a_new_epoch(tmp_path: Path) -> None:
    # Review 2026-11-15: revisions restart at 1 in a new file; the panel
    # tells the two apart by the epoch, or it ignores every later change.
    store = DownloadStore(tmp_path)
    assert store.load().epoch == ""
    add(store)
    epoch = store.load().epoch
    add(store)
    assert store.load().epoch == epoch and len(epoch) >= 8
    store.path.unlink()
    add(store)
    assert store.load().epoch not in ("", epoch)

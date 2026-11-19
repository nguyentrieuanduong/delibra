from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.converter import hub
from app.converter_models import (
    MANIFEST_NAME, MODEL_REPOS, READY_NAME, check_updates, download_models, installed_models,
    load_check, mark_interrupted, models_dir, models_state, ready_models_dir, remove_models,
    resolve_revision,
)

SHA = "a" * 40


def fake_hub(tmp_path: Path, *, fail: str = "", body: str = "") -> Path:
    """A hub.py stand-in: `download` writes a weights file and metadata per repo."""

    script = tmp_path / "fakehub" / "hub.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(
        "import json, os, sys, pathlib\n"
        f"open({str(tmp_path / 'hub-calls.txt')!r}, 'a').write(' '.join(sys.argv[1:]) + "
        "'|' + os.environ.get('HF_HOME', '') + '|' + os.environ.get('HF_HUB_CACHE', '') + '\\n')\n"
        + body +
        f"if {fail!r}:\n    print(json.dumps({{'error': {fail!r}}})); raise SystemExit(1)\n"
        "if sys.argv[1] == 'check':\n"
        "    print(json.dumps({'repos': {r: {'refs': [{'name': 'main', 'kind': 'branch', 'commit': 'b' * 40}],"
        " 'commits': [{'commit': 'b' * 40, 'title': 't', 'date': None}, {'commit': 'c' * 40, 'title': 'u', 'date': None}]}"
        " for r in sys.argv[2:]}}))\n"
        "else:\n"
        "    dest, models = pathlib.Path(sys.argv[2]), []\n"
        "    for pair in sys.argv[3:]:\n"
        "        repo, rev = pair.split('=', 1)\n"
        "        folder = dest / repo.replace('/', '--'); folder.mkdir(parents=True)\n"
        "        (folder / 'weights.bin').write_bytes(b'model')\n"
        "        models.append({'repo_id': repo, 'revision': rev, 'commit': 'b' * 40})\n"
        "    print(json.dumps({'models': models}))\n",
        encoding="utf-8",
    )
    return script


async def passes(home: Path, staged: Path) -> str | None:
    return None


def old_install(home: Path) -> Path:
    target = models_dir(home)
    target.mkdir(parents=True)
    (target / "old.bin").write_text("old models")
    (target / READY_NAME).write_text("x")
    return target


# ----- hub.py, with a fake Hugging Face API
def test_check_lists_refs_and_at_most_twenty_commits() -> None:
    ref = SimpleNamespace(name="main", target_commit="b" * 40)
    tag = SimpleNamespace(name="v2.3.0", target_commit="c" * 40)
    commits = [SimpleNamespace(commit_id=f"{n:040x}", title=f"c{n}", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
               for n in range(30)]
    api = SimpleNamespace(list_repo_refs=lambda repo: SimpleNamespace(branches=[ref], tags=[tag]),
                          iter_commits=lambda repo: iter(commits))
    result = hub.check(api, ["org/a"])["repos"]["org/a"]
    assert [(r["name"], r["kind"]) for r in result["refs"]] == [("main", "branch"), ("v2.3.0", "tag")]
    assert len(result["commits"]) == 20 and result["commits"][0]["commit"] == f"{0:040x}"


def test_download_uses_docling_tools_folder_names_and_records_the_commit(tmp_path) -> None:
    calls = []

    def snapshot(*, repo_id, revision, local_dir):
        calls.append((repo_id, revision, local_dir))
        meta = Path(local_dir) / ".cache" / "huggingface" / "download"
        meta.mkdir(parents=True)
        (meta / "config.json.metadata").write_text(f"{SHA}\netag\n1.0\n")
        return str(local_dir)

    result = hub.download(snapshot, tmp_path, [("docling-project/docling-models", "v2.3.0")])
    assert calls == [("docling-project/docling-models", "v2.3.0", tmp_path / "docling-project--docling-models")]
    assert result == {"models": [{"repo_id": "docling-project/docling-models", "revision": "v2.3.0", "commit": SHA}]}


def test_the_default_repos_are_docling_s(tmp_path) -> None:
    # In a child process with HOME in tmp: importing Docling writes caches under HOME.
    probe = (
        "import inspect\n"
        "from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions\n"
        "from docling.models.stages.table_structure.table_structure_model import TableStructureModel\n"
        "s = LayoutObjectDetectionOptions().model_spec\n"
        "repos = [(s.repo_id, s.revision)] + [(o.merge_with(s.repo_id, s.revision).repo_id, o.merge_with(s.repo_id, s.revision).revision) for o in s.engine_overrides.values()]\n"
        "src = inspect.getsource(TableStructureModel.download_models)\n"
        "rev = src.split('revision=\"')[1].split('\"')[0]\n"
        "print(repr(repos + [('docling-project/docling-models', rev)]))\n"
    )
    found = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True,
                           env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "HF_HUB_OFFLINE": "1"})
    if found.returncode != 0:
        pytest.skip("docling is not importable here")
    assert eval(found.stdout.strip()) == list(MODEL_REPOS)


# ----- install / update / choose version
@pytest.mark.asyncio
async def test_a_download_lands_in_place_with_a_manifest(tmp_path) -> None:
    home = tmp_path / "home"
    assert models_state(home).status == "absent"

    await download_models(home, hub=fake_hub(tmp_path), self_test=passes)

    assert ready_models_dir(home) == models_dir(home)
    assert models_state(home).status == "ready"
    manifest = json.loads((models_dir(home) / MANIFEST_NAME).read_text())
    assert [m["repo_id"] for m in manifest["models"]] == [repo for repo, _rev in MODEL_REPOS]
    assert all(m["default"] and m["commit"] == "b" * 40 and m["downloaded_at"] for m in manifest["models"])
    rows = installed_models(home)
    assert [(r.repo_id, r.revision, r.commit, r.size_bytes) for r in rows][0] == (MODEL_REPOS[0][0], "main", "b" * 40, 5)
    calls = (tmp_path / "hub-calls.txt").read_text().split("|")
    assert Path(calls[1]).is_relative_to(home / "models") and Path(calls[2]).is_relative_to(home / "models")
    assert not [p for p in (home / "models").iterdir() if ".staging" in p.name or ".old" in p.name]


@pytest.mark.asyncio
async def test_a_failed_self_test_keeps_the_old_models(tmp_path) -> None:
    home = tmp_path / "home"
    target = old_install(home)

    async def fails(home: Path, staged: Path) -> str | None:
        assert (staged / MODEL_REPOS[0][0].replace("/", "--") / "weights.bin").exists()
        return "self-test failed: converter error: RuntimeError"

    await download_models(home, hub=fake_hub(tmp_path), self_test=fails)

    assert (target / "old.bin").read_text() == "old models" and ready_models_dir(home) == target
    state = models_state(home)
    assert (state.status, state.error) == ("ready", None)
    assert json.loads((home / "models" / "docling-status.json").read_text())["error"] == (
        "self-test failed: converter error: RuntimeError")
    assert not [p for p in (home / "models").iterdir() if ".staging" in p.name]


@pytest.mark.asyncio
async def test_a_failed_download_keeps_the_old_models_and_nothing_half_written(tmp_path) -> None:
    home = tmp_path / "home"
    target = old_install(home)
    await download_models(home, hub=fake_hub(tmp_path, fail="no network"), self_test=passes)
    assert (target / "old.bin").exists()
    assert "no network" in json.loads((home / "models" / "docling-status.json").read_text())["error"]
    assert not [p for p in (home / "models").iterdir() if ".staging" in p.name]


@pytest.mark.asyncio
async def test_a_chosen_version_is_recorded_as_not_default(tmp_path) -> None:
    home = tmp_path / "home"
    chosen = {MODEL_REPOS[2][0]: "c" * 40}
    await download_models(home, chosen, hub=fake_hub(tmp_path), self_test=passes)
    by_repo = {r.repo_id: r for r in installed_models(home)}
    assert (by_repo[MODEL_REPOS[2][0]].revision, by_repo[MODEL_REPOS[2][0]].default) == ("c" * 40, False)
    assert by_repo[MODEL_REPOS[0][0]].default is True
    assert f"{MODEL_REPOS[2][0]}={'c' * 40}" in (tmp_path / "hub-calls.txt").read_text()


def test_an_install_without_a_manifest_reads_the_snapshot_metadata(tmp_path) -> None:
    home = tmp_path / "home"
    target = old_install(home)
    meta = target / MODEL_REPOS[0][0].replace("/", "--") / ".cache" / "huggingface" / "download"
    meta.mkdir(parents=True)
    (meta / "config.json.metadata").write_text(f"{SHA}\netag\n1.0\n")
    rows = {r.repo_id: r for r in installed_models(home)}
    assert rows[MODEL_REPOS[0][0]].commit == SHA
    assert rows[MODEL_REPOS[2][0]].commit is None  # shown as "unknown"


# ----- check for updates
@pytest.mark.asyncio
async def test_a_check_is_stored_and_a_failure_changes_nothing(tmp_path) -> None:
    home = tmp_path / "home"
    await check_updates(home, hub=fake_hub(tmp_path))
    stored = load_check(home)
    assert set(stored["repos"]) == {repo for repo, _rev in MODEL_REPOS} and stored["checked_at"]

    offline = tmp_path / "offline"
    offline.mkdir()
    await check_updates(home, hub=fake_hub(offline, fail="ConnectionError: offline"))
    failed = load_check(home)
    assert failed["error"] == "ConnectionError: offline"
    assert ready_models_dir(home) is None


# ----- choose version: validation
CHECK = {"repos": {"org/a": {"refs": [{"name": "main", "kind": "branch", "commit": "b" * 40},
                                      {"name": "v1", "kind": "tag", "commit": "d" * 40}],
                             "commits": [{"commit": "1234567" + "e" * 33, "title": "", "date": None}]}}}


@pytest.mark.parametrize("selected, sha, revision", [
    ("main", "", "main"), ("v1", "", "v1"), ("1234567" + "e" * 33, "", "1234567" + "e" * 33),
    ("main", "f" * 40, "f" * 40), ("main", "1234567", "1234567" + "e" * 33), ("main", " 1234567e ", "1234567" + "e" * 33),
])
def test_valid_revisions(selected, sha, revision) -> None:
    assert resolve_revision("org/a", selected, sha, CHECK) == revision


@pytest.mark.parametrize("selected, sha, reason", [
    ("other", "", "not a listed version"), ("main", "123456", "40 hex characters"),
    ("main", "zz34567", "40 hex characters"), ("main", "abcdef0", "does not match a listed commit"),
    ("main", "g" * 40, "40 hex characters"), ("main", "-r main", "40 hex characters"),
])
def test_invalid_revisions_are_refused(selected, sha, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        resolve_revision("org/a", selected, sha, CHECK)


# ----- remove, restart
def test_remove_deletes_the_models(tmp_path) -> None:
    home = tmp_path / "home"
    old_install(home)
    remove_models(home)
    assert not models_dir(home).exists() and models_state(home).status == "absent"


def test_startup_cleans_staging_and_marks_an_interrupted_action(tmp_path) -> None:
    home = tmp_path / "home"
    target = old_install(home)
    (home / "models" / "docling.staging-1234" / "x").mkdir(parents=True)
    (home / "models" / "docling.old-5678").mkdir()
    (home / "models" / "docling-status.json").write_text('{"status": "downloading"}')

    mark_interrupted(home)

    assert sorted(p.name for p in (home / "models").iterdir()) == ["docling", "docling-status.json"]
    assert (target / "old.bin").exists()
    assert json.loads((home / "models" / "docling-status.json").read_text())["error"] == "interrupted"


@pytest.mark.asyncio
async def test_a_cancelled_download_stops_the_tool_and_keeps_the_old_models(tmp_path) -> None:
    home = tmp_path / "home"
    target = old_install(home)
    pid_file = tmp_path / "pid"
    hub_script = fake_hub(tmp_path, body=f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\nimport time\ntime.sleep(30)\n")
    task = asyncio.create_task(download_models(home, hub=hub_script, self_test=passes))
    for _ in range(300):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert (target / "old.bin").exists()


@pytest.mark.asyncio
async def test_the_real_self_test_needs_working_models(tmp_path) -> None:
    from app.converter_models import run_self_test

    models = os.environ.get("DELIBRA_TEST_DOCLING_MODELS")
    if not models or not Path("/usr/bin/sandbox-exec").exists():
        pytest.skip("set DELIBRA_TEST_DOCLING_MODELS to a downloaded models folder")
    assert await run_self_test(tmp_path, Path(models)) is None
    (tmp_path / "empty").mkdir()
    assert (await run_self_test(tmp_path, tmp_path / "empty")).startswith("self-test failed: structure lost")


# ----- Task 14 review round 1
@pytest.mark.asyncio
async def test_each_run_has_its_own_cache_and_survives_another_runs_cleanup(tmp_path) -> None:
    home = tmp_path / "home"
    slow = fake_hub(tmp_path, body=(
        "import time\nmine = pathlib.Path(os.environ['HF_HOME']); mine.mkdir(parents=True, exist_ok=True)\n"
        "(mine / 'blob').write_text('x')\n"
        "if sys.argv[1] == 'download':\n    time.sleep(1.0)\n"
        "    assert (mine / 'blob').exists(), 'another run deleted my cache'\n"
    ).replace("import time\n", "import time, pathlib\n"))
    await asyncio.gather(download_models(home, hub=slow, self_test=passes), check_updates(home, hub=slow))
    assert models_state(home).status == "ready"
    homes = [line.split("|")[1] for line in (tmp_path / "hub-calls.txt").read_text().splitlines()]
    assert len(set(homes)) == 2 and all("docling.hf-cache-" in path for path in homes)
    assert not list((home / "models").glob("docling.hf-cache*"))


@pytest.mark.asyncio
async def test_a_conversion_waits_for_the_self_test_and_swap(tmp_path) -> None:
    home = tmp_path / "home"
    slot = asyncio.Semaphore(1)
    order: list[str] = []
    entered = asyncio.Event()

    async def slow_test(home: Path, staged: Path) -> str | None:
        entered.set()
        await asyncio.sleep(0.2)
        order.append("self-test")
        return None

    async def conversion() -> None:
        await entered.wait()
        async with slot:
            order.append("conversion sees " + ("new" if (models_dir(home) / MANIFEST_NAME).exists() else "old"))

    await asyncio.gather(download_models(home, hub=fake_hub(tmp_path), self_test=slow_test, slot=lambda: slot),
                         conversion())
    assert order == ["self-test", "conversion sees new"]


def _old(home: Path, stamp: str, ready: bool) -> Path:
    folder = home / "models" / f"docling.old-{stamp}-{'a' * 8}"
    folder.mkdir(parents=True)
    (folder / "w.bin").write_text(stamp)
    if ready:
        (folder / READY_NAME).write_text("x")
    return folder


def test_startup_restores_the_newest_ready_old_set_cut_between_renames(tmp_path) -> None:
    home = tmp_path / "home"
    _old(home, "20261119T010000Z", ready=True)
    _old(home, "20261119T020000Z", ready=True)
    _old(home, "20261119T030000Z", ready=False)  # a later, broken leftover
    (home / "models" / "docling.staging-1").mkdir()
    mark_interrupted(home)
    assert (models_dir(home) / "w.bin").read_text() == "20261119T020000Z"
    assert sorted(p.name for p in (home / "models").iterdir()) == ["docling"]


@pytest.mark.parametrize("name", ["docling.selftest-1", "docling.partial", "docling.removing-1",
                                  "docling.hf-cache-1", "docling.hf-cache", "docling.staging-2"])
def test_startup_removes_every_kind_of_leftover(tmp_path, name) -> None:
    home = tmp_path / "home"
    old_install(home)
    (home / "models" / name).mkdir()
    mark_interrupted(home)
    assert not (home / "models" / name).exists() and (models_dir(home) / "old.bin").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout", ["not json", "[1]", "{}"])
async def test_a_tool_without_an_answer_fails_the_download(tmp_path, stdout) -> None:
    home = tmp_path / "home"
    script = tmp_path / "quiet.py"
    script.write_text(f"print({stdout!r})\n")
    await download_models(home, hub=script, self_test=passes)
    data = json.loads((home / "models" / "docling-status.json").read_text())
    assert (data["status"], data["error"]) == ("failed", "the model tool gave no answer")


@pytest.mark.asyncio
async def test_any_error_ends_in_failed_never_downloading(tmp_path) -> None:
    home = tmp_path / "home"

    async def breaks(home: Path, staged: Path) -> str | None:
        raise ValueError("unexpected")

    await download_models(home, hub=fake_hub(tmp_path), self_test=breaks)
    assert json.loads((home / "models" / "docling-status.json").read_text())["status"] == "failed"
    assert models_state(home).status == "failed"


@pytest.mark.asyncio
async def test_a_failed_swap_puts_the_old_models_back(tmp_path, monkeypatch) -> None:
    import app.converter_models as cm

    home = tmp_path / "home"
    target = old_install(home)
    real_replace = os.replace

    def failing(src, dst):
        if Path(src).name.startswith("docling.staging-") and Path(dst) == target:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(cm.os, "replace", failing)
    await download_models(home, hub=fake_hub(tmp_path), self_test=passes)
    assert (target / "old.bin").exists() and ready_models_dir(home) == target


@pytest.mark.asyncio
async def test_a_chosen_commit_equal_to_the_default_is_default(tmp_path) -> None:
    home = tmp_path / "home"
    (home / "models").mkdir(parents=True)
    (home / "models" / "docling-check.json").write_text(json.dumps({"repos": {
        MODEL_REPOS[0][0]: {"refs": [{"name": "main", "kind": "branch", "commit": "b" * 40}], "commits": []}}}))
    await download_models(home, {MODEL_REPOS[0][0]: "b" * 40}, hub=fake_hub(tmp_path), self_test=passes)
    assert {r.repo_id: r.default for r in installed_models(home)}[MODEL_REPOS[0][0]] is True


def test_remove_drops_ready_first_and_leaves_no_folder(tmp_path) -> None:
    home = tmp_path / "home"
    old_install(home)
    remove_models(home)
    assert ready_models_dir(home) is None
    assert sorted(p.name for p in (home / "models").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result, verdict", [
    (("converted", None, b"x checks that the PDF models load\n| Alpha | 1 |"), None),
    (("converted", "structure lost; converted with pypdf (Docling: x)", b"checks that the PDF models load | Alpha"),
     "self-test failed: structure lost; converted with pypdf (Docling: x)"),
    (("converted", None, b"checks that the PDF models load, no table"), "self-test failed: the test PDF did not convert with tables"),
    (("failed", "converter error: RuntimeError", None), "self-test failed: converter error: RuntimeError"),
])
async def test_the_self_test_verdict(tmp_path, monkeypatch, result, verdict) -> None:
    import app.doc_convert as dc
    from app.converter_models import run_self_test

    async def fake_convert(staged, **kwargs):
        return dc.ConvertResult(result[0], result[1], result[2], "pdf")

    monkeypatch.setattr(dc, "convert_file", fake_convert)
    assert await run_self_test(tmp_path, tmp_path / "models") == verdict


def test_check_fetches_at_most_twenty_commits_lazily() -> None:
    pulled = []

    def endless():
        n = 0
        while True:
            pulled.append(n)
            yield SimpleNamespace(commit_id=f"{n:040x}", title="t", created_at=None)
            n += 1

    api = SimpleNamespace(list_repo_refs=lambda repo: SimpleNamespace(branches=[], tags=[]),
                          iter_commits=lambda repo: endless())
    result = hub.check(api, ["org/a"])["repos"]["org/a"]
    assert len(result["commits"]) == 20 and len(pulled) <= 21



def test_startup_cleanup_never_stops_the_app(tmp_path, monkeypatch, caplog) -> None:
    import app.converter_models as cm

    home = tmp_path / "home"
    _old(home, "20261119T010000Z", ready=True)  # no docling: a restore is due
    (home / "models" / "docling.staging-1").mkdir()

    def broken(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(cm.os, "replace", broken)
    monkeypatch.setattr(cm.shutil, "rmtree", broken)
    mark_interrupted(home)  # must not raise
    assert "read-only file system" in caplog.text


@pytest.mark.asyncio
async def test_a_status_write_after_the_swap_keeps_the_new_models(tmp_path, monkeypatch, caplog) -> None:
    import app.converter_models as cm

    home = tmp_path / "home"
    old_install(home)
    real_write = cm._write_status

    def write(home_, status, error=None):
        if status == "ready":
            raise OSError("disk full")
        return real_write(home_, status, error)

    monkeypatch.setattr(cm, "_write_status", write)
    await download_models(home, hub=fake_hub(tmp_path), self_test=passes)
    assert (models_dir(home) / MANIFEST_NAME).exists()  # the new set stays in place
    assert models_state(home).status == "ready"  # not "downloading", not "failed"
    assert "disk full" in caplog.text

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
                          list_repo_commits=lambda repo: commits)
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

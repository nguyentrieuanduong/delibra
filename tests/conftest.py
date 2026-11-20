from __future__ import annotations

import atexit
from hashlib import sha256
import os
from pathlib import Path
import shutil
import tempfile

# Before any `app` import. app/main.py builds `app = create_app()` at import,
# which reads $DELIBRA_HOME/config.json (default ~/.delibra), and about twenty
# test modules import it while pytest collects -- before any fixture runs. So
# each pytest process (the controller and every xdist worker loads this file)
# gets its own empty home under the repo's git-ignored tmp/, never /tmp and
# never the operator's, removed when that process exits. Tests that need a
# home still get their own: see _private_delibra_home below.
_IMPORT_HOME_ROOT = Path(__file__).resolve().parents[1] / "tmp" / "pytest-home"
_IMPORT_HOME_ROOT.mkdir(parents=True, exist_ok=True)
_IMPORT_HOME = tempfile.mkdtemp(prefix="import-", dir=_IMPORT_HOME_ROOT)
os.environ["DELIBRA_HOME"] = _IMPORT_HOME
atexit.register(shutil.rmtree, _IMPORT_HOME, ignore_errors=True)

import pytest  # noqa: E402

from app.models import AutoArtifact, AutoParticipant, AutoRunRecord  # noqa: E402
from app.storage import ProjectStore  # noqa: E402


# Browser tests run in this many xdist groups: at most this many Chromium
# pages at once (measured 2026-11-19: 1 group ~91 s wall, 2 ~59 s, 3 ~60 s).
BROWSER_GROUPS = 2
BROWSER_DIR = Path(__file__).resolve().parent / "browser"


@pytest.hookimpl(tryfirst=True)  # before xdist reads the groups off the items
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep the browser tests to BROWSER_GROUPS xdist workers, one test at a time each.

    They pass serially, but with a Chromium and a live server on every one of
    ten workers, some of them lose a timing race (a scroll position read
    before a timeline swap landed was seen). Two of the races seen first were
    real bugs and are fixed (a question added twice; a test helper that
    swapped the whole page into #auto-status), but ten browsers at once were
    never shown stable, so they stay grouped. Grouping keeps every one of them
    in the run; `--dist loadgroup` (pytest.ini) sends each group, whole files
    at a time, to a single worker. No effect without xdist.
    """

    # By folder as well as by marker, so a browser test that forgets the
    # marker is still grouped.
    browser = [
        item
        for item in items
        if BROWSER_DIR in item.path.parents or item.get_closest_marker("browser") is not None
    ]
    files = sorted({item.path for item in browser})
    for item in browser:
        group = files.index(item.path) % BROWSER_GROUPS
        item.add_marker(pytest.mark.xdist_group(f"browser-{group}"))


@pytest.fixture(autouse=True)
def _private_delibra_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point DELIBRA_HOME at this test's own (absent) folder.

    Settings.from_env() falls back to ~/.delibra, so a test that reads the
    environment without setting DELIBRA_HOME would read the operator's real
    config.json -- its result would depend on that machine, and parallel
    workers would share it. A test that needs a populated home still sets
    DELIBRA_HOME itself; monkeypatch applies its value over this one.
    """

    monkeypatch.setenv("DELIBRA_HOME", str(tmp_path / "delibra-home"))


@pytest.fixture
def reserve_auto_run():
    """Return a test helper that publishes an active Auto reservation."""

    def reserve(
        store: ProjectStore,
        *,
        auto_id: str = "a" * 32,
    ) -> AutoRunRecord:
        topic = b"Original topic"
        baseline = b""
        sessions = store.list_sessions()
        record = AutoRunRecord(
            id=auto_id,
            project_id=store.project.id,
            number=None,
            status="preparing",
            agreement_policy="all_agree",
            preparation_enabled=True,
            max_cycles=3,
            current_cycle=0,
            next_participant=0,
            participants=[
                AutoParticipant(
                    session_id=session.id,
                    name=session.name,
                    agent=session.agent,
                    model=session.model,
                    effort=session.effort,
                )
                for session in sessions
            ],
            topic=AutoArtifact("topic.md", sha256(topic).hexdigest()),
            baseline=AutoArtifact("baseline.md", sha256(baseline).hexdigest()),
            baseline_entries=[],
            preparations=[],
            discussion=[],
            active_key=None,
            future_turn_timeout_seconds=900,
            active_timeout=None,
            stop_requested=False,
            created_at="2026-07-19T00:00:00Z",
            started_at=None,
            finished_at=None,
            terminal_reason=None,
        )
        record.number = store.reserve_auto_run_number()
        store.create_auto_run(record, topic=topic, baseline=baseline)
        store.publish_auto_reservation(auto_id)
        return record

    return reserve

"""Docling's PDF models in $DELIBRA_HOME/models/docling: download, check, choose, remove.

The converter sandbox has no network, so the models are fetched here, outside it,
when the user asks (Settings → Converter), and passed to the worker as Docling's
artifacts_path. Hugging Face work runs in app/converter/hub.py, a separate process
whose caches stay inside Delibra. A new set is downloaded into a staging folder,
self-tested through the real sandboxed converter, and only then swapped in, so a
failed or interrupted update keeps the models that worked (Task 14, 2026-11-19).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

from app.converter import hub as hub_module
from app.storage import atomic_write_bytes, utc_now

STATUS_NAME = "docling-status.json"
CHECK_NAME = "docling-check.json"
MANIFEST_NAME = "delibra-models.json"
READY_NAME = ".delibra-ready"
DOWNLOAD_SECONDS = 30 * 60
CHECK_SECONDS = 120
HUB = Path(hub_module.__file__)
SELF_TEST_PDF = HUB.with_name("selftest.pdf")
# The bundled PDF's sentence (layout ran) and a Markdown table row (TableFormer ran).
SELF_TEST_MARKS = (b"checks that the PDF models load", b"| Alpha")
# What the PDF pipeline loads, with Docling 2.150.1's default revisions and the
# folders docling-tools uses (docling/utils/model_downloader.py; a test checks them).
MODEL_REPOS: tuple[tuple[str, str], ...] = (
    ("docling-project/docling-layout-heron", "main"),
    ("docling-project/docling-layout-heron-onnx", "main"),
    ("docling-project/docling-models", "v2.3.0"),  # TableFormer
)
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
SelfTest = Callable[[Path, Path], Awaitable[str | None]]
# The server's one conversion slot: held for the self-test and the swap, so no user
# conversion runs beside the self-test or loads models while they are renamed.
Slot = Callable[[], AbstractAsyncContextManager[object]]
NO_ANSWER = "the model tool gave no answer"
_LEFTOVERS = ("docling.staging-*", "docling.selftest-*", "docling.removing-*", "docling.hf-cache*",
              "docling.partial")


def _stamped(kind: str) -> str:
    """A folder name that sorts by time: docling.<kind>-<UTC>-<hex>."""

    return f"docling.{kind}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}"


def check_listing(check: Mapping | None, repo_id: str) -> dict:
    """What the last check found for one repo ({} when nothing was checked)."""

    return ((check or {}).get("repos") or {}).get(repo_id) or {}


@dataclass(frozen=True)
class ModelsState:
    status: str  # absent | downloading | ready | failed
    error: str | None = None
    size_bytes: int = 0
    notice: str | None = None  # the last action's failure while older models stay ready


@dataclass(frozen=True)
class InstalledModel:
    repo_id: str
    revision: str | None
    commit: str | None
    downloaded_at: str | None
    size_bytes: int
    default: bool | None


def models_dir(home: Path) -> Path:
    return home / "models" / "docling"


def _status_path(home: Path) -> Path:
    return home / "models" / STATUS_NAME


def _write_status(home: Path, status: str, error: str | None = None) -> None:
    (home / "models").mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"status": status, "error": error, "updated_at": utc_now()}
    atomic_write_bytes(_status_path(home), json.dumps(payload).encode("utf-8"))


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _size(directory: Path) -> int:
    return sum(
        path.stat().st_size for path in directory.rglob("*")
        if path.is_file() and path.name != READY_NAME and ".cache" not in path.parts
    )


def ready_models_dir(home: Path) -> Path | None:
    target = models_dir(home)
    return target if (target / READY_NAME).is_file() else None


def models_state(home: Path) -> ModelsState:
    data = _read_json(_status_path(home)) or {}
    status = data.get("status")
    ready = ready_models_dir(home)
    if status == "downloading":
        return ModelsState("downloading", None, _size(ready) if ready else 0)
    if ready is not None:
        return ModelsState("ready", None, _size(ready), data.get("error") if status == "failed" else None)
    if status == "failed":
        return ModelsState("failed", data.get("error"))
    return ModelsState("absent")


def installed_models(home: Path) -> list[InstalledModel]:
    """Each repo of the installed set: from the manifest, else the HF snapshot metadata."""

    target = ready_models_dir(home)
    if target is None:
        return []
    manifest = {item.get("repo_id"): item for item in (_read_json(target / MANIFEST_NAME) or {}).get("models", [])
                if isinstance(item, dict)}
    rows = []
    for repo_id, default_revision in MODEL_REPOS:
        folder = target / repo_id.replace("/", "--")
        item = manifest.get(repo_id, {})
        revision = item.get("revision")
        rows.append(InstalledModel(
            repo_id=repo_id, revision=revision,
            commit=item.get("commit") or (hub_module.snapshot_commit(folder) if folder.is_dir() else None),
            downloaded_at=item.get("downloaded_at"),
            size_bytes=_size(folder) if folder.is_dir() else 0,
            default=item.get("default", revision == default_revision) if revision else None,
        ))
    return rows


def load_check(home: Path) -> dict | None:
    return _read_json(home / "models" / CHECK_NAME)


def _cleanup_leftovers(home: Path) -> None:
    """Startup: undo a swap the last stop cut, then drop every temporary folder."""

    models = home / "models"
    if not models.is_dir():
        return
    olds = sorted(models.glob("docling.old-*"))  # time-sortable names
    if not models_dir(home).exists():
        ready = [old for old in olds if (old / READY_NAME).is_file()]
        if ready:  # stopped between the two renames: the newest working set comes back
            os.replace(ready[-1], models_dir(home))
            olds.remove(ready[-1])
    for leftover in [*olds, *(path for pattern in _LEFTOVERS for path in models.glob(pattern))]:
        shutil.rmtree(leftover, ignore_errors=True)


def mark_interrupted(home: Path) -> None:
    """Startup: an action that was running when the server stopped did not finish."""

    _cleanup_leftovers(home)
    if (_read_json(_status_path(home)) or {}).get("status") == "downloading":
        _write_status(home, "failed", "interrupted")


async def _hub(home: Path, *args: str, hub: Path, python: str, timeout: float, expect: str) -> dict:
    """Run hub.py with every Hugging Face cache inside Delibra; its JSON answer."""

    cache = home / "models" / _stamped("hf-cache")  # each run its own: no run cleans another's
    env = {**os.environ, "HF_HOME": str(cache), "HF_HUB_CACHE": str(cache / "hub"),
           "HF_XET_CACHE": str(cache / "xet")}
    process = await asyncio.create_subprocess_exec(
        python, "-I", str(hub), *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"Hugging Face did not answer within {timeout / 60:g} minutes") from None
    except asyncio.CancelledError:  # shutdown: the next start cleans up
        process.kill()
        await process.wait()
        raise
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    try:
        answer = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError:
        answer = None
    if isinstance(answer, dict) and "error" in answer:
        raise RuntimeError(str(answer["error"])[:300])
    if process.returncode != 0:
        raise RuntimeError(f"the model tool exited with {process.returncode}")
    if not isinstance(answer, dict) or expect not in answer:
        raise RuntimeError(NO_ANSWER)
    return answer


async def check_updates(home: Path, *, hub: Path = HUB, python: str = sys.executable) -> None:
    """List each repo's refs and recent commits; nothing is downloaded or changed."""

    try:
        found = await _hub(home, "check", *(repo for repo, _rev in MODEL_REPOS),
                           hub=hub, python=python, timeout=CHECK_SECONDS, expect="repos")
        record = {"checked_at": utc_now(), "repos": found.get("repos", {})}
    except (OSError, RuntimeError) as exc:
        record = {"checked_at": utc_now(), "error": str(exc)}
    (home / "models").mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_bytes(home / "models" / CHECK_NAME, json.dumps(record).encode("utf-8"))


def resolve_revision(repo_id: str, selected: str, sha: str, check: Mapping | None) -> str:
    """The revision to download: a typed commit, or a version the last check listed."""

    listing = check_listing(check, repo_id)
    refs = listing.get("refs") or []
    commits = [item["commit"] for item in listing.get("commits") or []] + [ref["commit"] for ref in refs]
    sha = sha.strip().lower()
    if sha:
        if not _COMMIT.fullmatch(sha):
            raise ValueError("a commit must be 40 hex characters, or a 7+ character prefix of a listed commit")
        if len(sha) == 40:
            return sha
        matches = {commit for commit in commits if commit.startswith(sha)}
        if len(matches) != 1:
            raise ValueError(f"{sha} does not match a listed commit of {repo_id}")
        return matches.pop()
    if selected not in {ref["name"] for ref in refs} | set(commits):
        raise ValueError(f"{selected} is not a listed version of {repo_id}; check for updates first")
    return selected


async def run_self_test(home: Path, staged: Path) -> str | None:
    """Convert the bundled table PDF with the staged models in the real sandbox."""

    from app.doc_convert import convert_file  # the converter, not needed to show state

    work = home / "models" / f"docling.selftest-{uuid.uuid4().hex}"
    work.mkdir(mode=0o700, parents=True)
    try:
        source = work / "selftest.pdf"
        shutil.copyfile(SELF_TEST_PDF, source)
        result = await convert_file(
            source, display_path="selftest.pdf", source_sha256=sha256(source.read_bytes()).hexdigest(),
            scratch_root=work / "scratch", models_dir=staged,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    markdown = result.markdown or b""
    if result.state != "converted" or result.reason is not None or not all(m in markdown for m in SELF_TEST_MARKS):
        return f"self-test failed: {result.reason or 'the test PDF did not convert with tables'}"
    return None


async def download_models(
    home: Path, revisions: Mapping[str, str] | None = None, *, hub: Path = HUB,
    python: str = sys.executable, self_test: SelfTest = run_self_test, slot: Slot | None = None,
) -> None:
    """Download Docling's default revisions (or the chosen ones), self-test, then swap."""

    pairs = [(repo, (revisions or {}).get(repo, default)) for repo, default in MODEL_REPOS]
    target = models_dir(home)
    staging = target.with_name(_stamped("staging"))
    _write_status(home, "downloading")
    try:
        found = await _hub(home, "download", str(staging), *(f"{repo}={rev}" for repo, rev in pairs),
                           hub=hub, python=python, timeout=DOWNLOAD_SECONDS, expect="models")
        commits = {item.get("repo_id"): item.get("commit") for item in found["models"] if isinstance(item, dict)}
        check = load_check(home)
        now = utc_now()
        manifest = {"format": "delibra-models/1", "models": [
            {"repo_id": repo, "revision": rev, "commit": commits.get(repo), "downloaded_at": now,
             "default": _is_default(repo, rev, commits.get(repo), check)}
            for repo, rev in pairs
        ]}
        atomic_write_bytes(staging / MANIFEST_NAME, json.dumps(manifest, indent=1).encode("utf-8"))
        (staging / READY_NAME).write_text(now, encoding="utf-8")
        async with (slot() if slot is not None else nullcontext()):
            reason = await self_test(home, staging)
            if reason is not None:
                raise RuntimeError(reason)
            _swap(home, staging)
        _write_status(home, "ready")
    except asyncio.CancelledError:
        raise  # shutdown: the next start marks it interrupted and cleans up
    except Exception as exc:  # any failure keeps the old models; never stuck on "downloading"
        _write_status(home, "failed", str(exc) or type(exc).__name__)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _is_default(repo: str, revision: str, commit: str | None, check: Mapping | None) -> bool:
    """Docling's default revision, or the commit it pointed to at the last check."""

    default = dict(MODEL_REPOS)[repo]
    if revision == default:
        return True
    default_commit = next((ref["commit"] for ref in check_listing(check, repo).get("refs") or []
                           if ref["name"] == default), None)
    return default_commit is not None and default_commit in (revision, commit)


def _swap(home: Path, staging: Path) -> None:
    """Put the tested set in place; on failure, the old set goes back."""

    target = models_dir(home)
    for stale in (home / "models").glob("docling.old-*"):  # never a candidate for restore
        shutil.rmtree(stale, ignore_errors=True)
    old = target.with_name(_stamped("old"))
    if target.exists():
        os.replace(target, old)
    try:
        os.replace(staging, target)
    except OSError:
        if old.exists():
            os.replace(old, target)
        raise
    shutil.rmtree(old, ignore_errors=True)


def remove_models(home: Path) -> None:
    """Delete the models; PDFs fall back to pypdf until they are downloaded again."""

    target = models_dir(home)
    (target / READY_NAME).unlink(missing_ok=True)  # no conversion picks them up from here on
    if target.exists():
        removing = target.with_name(_stamped("removing"))
        os.replace(target, removing)
        shutil.rmtree(removing, ignore_errors=True)
    _status_path(home).unlink(missing_ok=True)

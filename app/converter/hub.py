"""Hugging Face side of the converter models: check refs/commits, download revisions.

Run only by app.converter_models, in its own process with HF_HOME and the other
Hugging Face caches pointed inside $DELIBRA_HOME (the server never imports
huggingface_hub). It has network access, like the first models download; the
converter sandbox never runs it. stdout carries one JSON object, the interface.

Usage: hub.py check REPO...
       hub.py download DEST REPO=REVISION...
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys
from typing import Any, Callable

COMMITS_SHOWN = 20


def check(api: Any, repo_ids: list[str]) -> dict[str, Any]:
    """Each repo's branches and tags (with their commits) and its newest commits."""

    repos: dict[str, Any] = {}
    for repo_id in repo_ids:
        refs = api.list_repo_refs(repo_id)
        commits = list(itertools.islice(api.iter_commits(repo_id), COMMITS_SHOWN))
        repos[repo_id] = {
            "refs": [{"name": ref.name, "kind": kind, "commit": ref.target_commit}
                     for kind, items in (("branch", refs.branches), ("tag", refs.tags)) for ref in items],
            "commits": [{"commit": item.commit_id, "title": (item.title or "")[:120],
                         "date": item.created_at.isoformat() if item.created_at else None} for item in commits],
        }
    return {"repos": repos}


class _Api:
    """HfApi plus a lazy commit listing: only the pages the first 20 commits need
    (list_repo_commits always fetches the whole history)."""

    def __init__(self) -> None:
        from huggingface_hub import HfApi

        self._api = HfApi()

    def list_repo_refs(self, repo_id: str) -> Any:
        return self._api.list_repo_refs(repo_id)

    def iter_commits(self, repo_id: str) -> Any:
        from types import SimpleNamespace

        from huggingface_hub.utils import build_hf_headers, paginate

        for item in paginate(f"{self._api.endpoint}/api/models/{repo_id}/commits/main",
                             headers=build_hf_headers(), params={}):
            yield SimpleNamespace(commit_id=item["id"], title=item.get("title"),
                                  created_at=_parse_date(item.get("date")))


def _parse_date(value: str | None) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def snapshot_commit(local_dir: Path) -> str | None:
    """The commit a snapshot_download into local_dir came from (its download metadata)."""

    for metadata in sorted((local_dir / ".cache" / "huggingface" / "download").glob("*.metadata")):
        first = metadata.read_text(encoding="utf-8").splitlines()[:1]
        if first and len(first[0]) == 40:
            return first[0]
    return None


def download(snapshot: Callable[..., str], dest: Path, pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Download each repo at its revision into the folder docling-tools uses."""

    models = []
    for repo_id, revision in pairs:
        local_dir = dest / repo_id.replace("/", "--")
        snapshot(repo_id=repo_id, revision=revision, local_dir=local_dir)
        models.append({"repo_id": repo_id, "revision": revision, "commit": snapshot_commit(local_dir)})
    return {"models": models}


def main(argv: list[str]) -> int:
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import disable_progress_bars

    disable_progress_bars()
    try:
        if len(argv) >= 3 and argv[1] == "check":
            result = check(_Api(), argv[2:])
        elif len(argv) >= 4 and argv[1] == "download":
            pairs = [tuple(item.split("=", 1)) for item in argv[3:]]
            result = download(snapshot_download, Path(argv[2]), pairs)  # type: ignore[arg-type]
        else:
            result = {"error": "bad arguments"}
    except Exception as exc:  # the parent shows one line; no traceback reaches the page
        result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    sys.stdout.write(json.dumps(result))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

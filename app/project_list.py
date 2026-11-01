"""The home page's project list: search, sort and pages, all server-side."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import stat
import unicodedata
from urllib.parse import urlencode

from app.models import Project

PAGE_SIZE = 20
QUERY_MAX = 200
SORTS = {
    "activity-desc": "Last activity (newest first)",
    "activity-asc": "Last activity (oldest first)",
    "registered-desc": "Registered (newest first)",
    "registered-asc": "Registered (oldest first)",
    "name-asc": "Name A→Z",
    "name-desc": "Name Z→A",
}
DEFAULT_SORT = "activity-desc"
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def fold(text: str) -> str:
    """Case- and accent-free form, so `duong` finds `Dương`.

    `đ` has no decomposition in Unicode, so it is mapped by hand.
    """

    decomposed = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def last_activity(project_path: Path) -> float | None:
    """Newest round or Auto-run change, from lstat calls only (no reads, no writes)."""

    root = Path(project_path) / ".delibra"
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return None
    except OSError:
        return None
    newest: float | None = None
    for parent, child in (("sessions", "rounds"), ("auto-runs", "config.json")):
        try:
            with os.scandir(root / parent) as iterator:
                folders = [entry.path for entry in iterator if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for folder in folders:
            try:
                changed = os.lstat(Path(folder) / child).st_mtime
            except OSError:
                continue
            if newest is None or changed > newest:
                newest = changed
    return newest


def _registered(project: Project) -> datetime:
    try:
        value = datetime.fromisoformat(project.created_at)
    except ValueError:
        return _EPOCH
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ListedProject:
    project: Project
    registered: datetime
    # None when the registered path is unavailable.
    activity: datetime | None


@dataclass(frozen=True)
class ProjectPage:
    projects: list[ListedProject]
    q: str
    sort: str
    page: int
    pages: int
    total: int

    def url(self, page: int) -> str:
        params: dict[str, str] = {}
        if self.q:
            params["q"] = self.q
        if self.sort != DEFAULT_SORT:
            params["sort"] = self.sort
        if page > 1:
            params["page"] = str(page)
        return "/" + (f"?{urlencode(params)}" if params else "")


def _listed(project: Project) -> ListedProject:
    registered = _registered(project)
    if not Path(project.path).is_dir():
        return ListedProject(project, registered, None)
    changed = last_activity(Path(project.path))
    activity = registered if changed is None else datetime.fromtimestamp(changed, timezone.utc)
    return ListedProject(project, registered, activity)


def list_projects(projects: list[Project], q: str, sort: str, page: int) -> ProjectPage:
    query = q.strip()[:QUERY_MAX]
    sort = sort if sort in SORTS else DEFAULT_SORT
    needle = fold(query)
    listed = [
        _listed(project)
        for project in projects
        if not needle or needle in fold(project.name) or needle in fold(project.path)
    ]
    # Name first, then the stable primary sort: equal keys stay in name order
    # in both directions (Python's sort is stable even with reverse=True).
    listed.sort(key=lambda item: fold(item.project.name))
    field, direction = sort.rsplit("-", 1)
    descending = direction == "desc"
    if field == "name":
        if descending:
            listed.reverse()
    elif field == "registered":
        listed.sort(key=lambda item: item.registered, reverse=descending)
    else:
        listed.sort(key=lambda item: item.activity or _EPOCH, reverse=descending)
        listed.sort(key=lambda item: item.activity is None)
    pages = max(1, math.ceil(len(listed) / PAGE_SIZE))
    number = min(max(page, 1), pages)
    start = (number - 1) * PAGE_SIZE
    return ProjectPage(
        projects=listed[start : start + PAGE_SIZE],
        q=query,
        sort=sort,
        page=number,
        pages=pages,
        total=len(listed),
    )

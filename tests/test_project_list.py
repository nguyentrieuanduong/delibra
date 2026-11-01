from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

from app.models import Project
from app.project_list import DEFAULT_SORT, PAGE_SIZE, ProjectPage, fold, last_activity, list_projects


def make(tmp_path: Path, name: str, created: str, activity: float | None = None, exists: bool = True) -> Project:
    path = tmp_path / name.replace(" ", "-")
    if exists:
        path.mkdir()
        if activity is not None:
            rounds = path / ".delibra" / "sessions" / "Alpha" / "rounds"
            rounds.mkdir(parents=True)
            os.utime(rounds, (activity, activity))
    return Project(id=f"{abs(hash(name)):032x}"[:32], name=name, path=str(path), created_at=created)


def names(page) -> list[str]:
    return [item.project.name for item in page.projects]


def test_fold_ignores_case_and_vietnamese_accents() -> None:
    assert fold("Dương Đức") == "duong duc"
    assert fold("ÁNH") == fold("anh")


def test_last_activity_takes_the_newest_round_or_auto_run(tmp_path) -> None:
    root = tmp_path / "p" / ".delibra"
    (root / "sessions" / "A" / "rounds").mkdir(parents=True)
    (root / "auto-runs" / "1").mkdir(parents=True)
    (root / "auto-runs" / "1" / "config.json").write_text("{}")
    os.utime(root / "sessions" / "A" / "rounds", (100, 100))
    os.utime(root / "auto-runs" / "1" / "config.json", (200, 200))

    assert last_activity(tmp_path / "p") == 200
    assert last_activity(tmp_path / "missing") is None


def test_last_activity_ignores_a_symlinked_delibra(tmp_path) -> None:
    real = tmp_path / "elsewhere"
    (real / "sessions" / "A" / "rounds").mkdir(parents=True)
    (tmp_path / "p").mkdir()
    os.symlink(real, tmp_path / "p" / ".delibra")

    assert last_activity(tmp_path / "p") is None


def test_search_matches_name_or_path_without_accents(tmp_path) -> None:
    projects = [
        make(tmp_path, "Dương notes", "2026-01-01T00:00:00Z"),
        make(tmp_path, "Other", "2026-01-02T00:00:00Z"),
    ]

    assert names(list_projects(projects, "  duong ", "name-asc", 1)) == ["Dương notes"]
    assert names(list_projects(projects, "OTHER", "name-asc", 1)) == ["Other"]
    assert list_projects(projects, "x" * 300, "name-asc", 1).q == "x" * 200


def test_each_sort_orders_and_breaks_ties_by_name(tmp_path) -> None:
    late = datetime(2026, 1, 10, tzinfo=timezone.utc).timestamp()
    early = datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp()
    projects = [
        make(tmp_path, "b", "2026-01-02T00:00:00Z", activity=late),
        make(tmp_path, "a", "2026-01-03T00:00:00Z", activity=early),
        make(tmp_path, "c", "2026-01-01T00:00:00Z"),
        make(tmp_path, "gone", "2026-01-04T00:00:00Z", exists=False),
        make(tmp_path, "d", "2026-01-02T00:00:00Z", activity=late),
    ]

    # c has no activity: its registered time (2026-01-01) stands in.
    assert names(list_projects(projects, "", "activity-desc", 1)) == ["b", "d", "c", "a", "gone"]
    assert names(list_projects(projects, "", "activity-asc", 1)) == ["a", "c", "b", "d", "gone"]
    assert names(list_projects(projects, "", "registered-desc", 1)) == ["gone", "a", "b", "d", "c"]
    assert names(list_projects(projects, "", "registered-asc", 1)) == ["c", "b", "d", "a", "gone"]
    assert names(list_projects(projects, "", "name-asc", 1)) == ["a", "b", "c", "d", "gone"]
    assert names(list_projects(projects, "", "name-desc", 1)) == ["gone", "d", "c", "b", "a"]
    assert list_projects(projects, "", "bogus", 1).sort == DEFAULT_SORT


def test_unavailable_projects_have_no_activity(tmp_path) -> None:
    page = list_projects([make(tmp_path, "gone", "2026-01-04T00:00:00Z", exists=False)], "", "name-asc", 1)

    assert page.projects[0].activity is None
    assert page.projects[0].registered == datetime(2026, 1, 4, tzinfo=timezone.utc)


def test_pages_hold_twenty_and_clamp(tmp_path) -> None:
    projects = [make(tmp_path, f"p{index:02}", "2026-01-01T00:00:00Z") for index in range(PAGE_SIZE + 1)]

    first = list_projects(projects, "", "name-asc", 1)
    last = list_projects(projects, "", "name-asc", 99)

    assert (len(first.projects), first.pages, first.total) == (20, 2, 21)
    assert (names(last), last.page) == (["p20"], 2)
    assert list_projects(projects, "", "name-asc", 0).page == 1


def test_page_urls_keep_search_and_sort() -> None:
    page = list_projects([], "a b&c", "name-desc", 1)

    assert page.url(2) == "/?q=a+b%26c&sort=name-desc&page=2"
    assert list_projects([], "", DEFAULT_SORT, 1).url(1) == "/"


def test_page_numbers_show_ends_and_neighbours_with_gaps() -> None:
    def numbers(page: int, pages: int) -> list[int | None]:
        return ProjectPage([], "", DEFAULT_SORT, page, pages, pages * PAGE_SIZE).page_numbers()

    assert numbers(1, 1) == [1]
    assert numbers(1, 5) == [1, 2, 3, 4, 5]
    assert numbers(6, 12) == [1, None, 4, 5, 6, 7, 8, None, 12]
    assert numbers(5, 12) == [1, 2, 3, 4, 5, 6, 7, None, 12]
    assert numbers(1, 12) == [1, 2, 3, None, 12]
    # A gap of one page shows that page instead of an ellipsis.
    assert numbers(5, 9) == [1, 2, 3, 4, 5, 6, 7, 8, 9]

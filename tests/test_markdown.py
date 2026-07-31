from pathlib import Path
from urllib.parse import quote

import pytest

from app.markdown import render_markdown
from app.models import Project


def test_raw_script_and_event_handler_html_are_neutralized() -> None:
    rendered = str(
        render_markdown(
            '<script>alert(1)</script>\n<img src=x onerror="alert(2)">\n\n**safe**'
        )
    )
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in rendered
    assert "<strong>safe</strong>" in rendered


def test_javascript_links_are_not_rendered_as_clickable_urls() -> None:
    rendered = str(render_markdown("[unsafe](javascript:alert(1))"))
    assert 'href="javascript:' not in rendered.lower()
    assert "unsafe" in rendered


def test_normal_links_and_code_render() -> None:
    rendered = str(render_markdown("[Python](https://python.org) and `code`"))
    assert 'href="https://python.org"' in rendered
    assert "<code>code</code>" in rendered


def test_project_markdown_links_use_owned_file_routes(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = Project(
        id="a" * 32,
        name="Links",
        path=str(project_root),
        created_at="2026-07-19T00:00:00Z",
    )
    absolute = project_root / "docs" / "review.md"
    outside = tmp_path / "outside.md"
    rendered = str(
        render_markdown(
            " ".join(
                (
                    "[relative](docs/review.md)",
                    f"[absolute]({absolute}:37)",
                    "[external](https://example.com/readme.md)",
                    f"[outside]({outside})",
                    "[traversal](../outside.md)",
                    "[plain](notes.txt)",
                )
            ),
            project,
            "reader",
        )
    )
    reader_url = f"/projects/{quote(project.name, safe='')}/files/view?path=docs%2Freview.md"

    assert rendered.count(f'href="{reader_url}"') == 2
    assert rendered.count(f'hx-get="{reader_url}"') == 2
    assert rendered.count('hx-target="#file-reader"') == 2
    assert rendered.count('hx-swap="innerHTML"') == 2
    assert 'href="https://example.com/readme.md"' in rendered
    assert f'href="{outside}"' in rendered
    assert 'href="../outside.md"' in rendered
    assert 'href="notes.txt"' in rendered


def test_project_markdown_rejects_unknown_link_target(tmp_path: Path) -> None:
    project = Project(
        id="a" * 32,
        name="Links",
        path=str(tmp_path),
        created_at="2026-07-19T00:00:00Z",
    )

    with pytest.raises(ValueError, match="Markdown link target"):
        render_markdown("[notes](notes.md)", project, "sidebar")

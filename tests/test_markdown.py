from pathlib import Path
import shutil
import subprocess
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


def test_a_mermaid_fence_becomes_a_diagram_marker_with_escaped_source() -> None:
    rendered = str(render_markdown('```mermaid\nflowchart LR\n  A["<b>x</b>"] --> B\n```\n'))
    assert rendered == (
        '<div class="mermaid-diagram" data-mermaid>'
        '<pre class="mermaid-source">flowchart LR\n  A[&quot;&lt;b&gt;x&lt;/b&gt;&quot;] --&gt; B\n</pre>'
        "</div>\n"
    )


@pytest.mark.parametrize("info", ["Mermaid", "mermaid title=x"])
def test_the_mermaid_word_ignores_case_and_options(info: str) -> None:
    assert "data-mermaid" in str(render_markdown(f"```{info}\ngraph TD\n```\n"))


def test_other_fences_still_render_as_code() -> None:
    rendered = str(render_markdown("```python\nprint(1)\n```\n"))
    assert rendered == '<pre><code class="language-python">print(1)\n</code></pre>\n'


def test_an_indented_block_saying_mermaid_is_not_a_diagram() -> None:
    assert "data-mermaid" not in str(render_markdown("    mermaid\n    graph TD\n"))


def test_mermaid_javascript_contract_runs_under_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; Mermaid unit test not run")
    result = subprocess.run(
        [node, "--test", "tests/js/test_mermaid.js"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _links_project(tmp_path: Path) -> Project:
    root = tmp_path / "project"
    root.mkdir()
    return Project(id="a" * 32, name="tu-vi", path=str(root), created_at="2026-11-03T00:00:00Z")


def _reader_url(project: Project, path: str) -> str:
    return f"/projects/{quote(project.name, safe='')}/files/view?path={quote(path, safe='')}"


def test_a_relative_link_resolves_from_the_viewed_files_folder(tmp_path: Path) -> None:
    project = _links_project(tmp_path)
    rendered = str(
        render_markdown(
            "[01](source-knowledge-repository/01-foundation-contracts.md)",
            project,
            "reader",
            ".plans",
        )
    )
    url = _reader_url(project, ".plans/source-knowledge-repository/01-foundation-contracts.md")
    assert f'href="{url}"' in rendered
    assert f'hx-get="{url}"' in rendered
    assert 'hx-target="#file-reader"' in rendered


def test_a_parent_link_may_climb_to_the_project_root_but_not_out(tmp_path: Path) -> None:
    project = _links_project(tmp_path)
    rendered = str(
        render_markdown("[up](../README.md) [out](../../outside.md)", project, "reader", ".plans")
    )
    assert f'href="{_reader_url(project, "README.md")}"' in rendered
    assert 'href="../../outside.md"' in rendered
    assert rendered.count("hx-get=") == 1


def test_without_a_base_folder_links_keep_the_project_root_rule(tmp_path: Path) -> None:
    # Round prompts and replies: agents write project-relative paths, and a
    # ".." segment stays refused as before.
    project = _links_project(tmp_path)
    rendered = str(render_markdown("[a](docs/x.md) [b](a/../b.md)", project, "reader"))
    assert f'href="{_reader_url(project, "docs/x.md")}"' in rendered
    assert 'href="a/../b.md"' in rendered

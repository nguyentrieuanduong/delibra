"""Safe server-side Markdown rendering."""

from __future__ import annotations

from markupsafe import Markup
from markdown_it import MarkdownIt


_MARKDOWN = MarkdownIt(
    "gfm-like",
    {
        "html": False,
        "linkify": True,
        "typographer": False,
    },
)


def render_markdown(source: str | None) -> Markup:
    """Render Markdown with raw HTML disabled and safe URL validation enabled."""

    return Markup(_MARKDOWN.render(source or ""))

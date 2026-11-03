"""Safe server-side Markdown rendering."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Literal
from urllib.parse import unquote, urlencode, urlsplit

from markupsafe import Markup
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.token import Token

from app.models import Project
from app.storage import ProjectFileSecurityError, project_path_parts
from app.urls import project_url


LinkTarget = Literal["reader", "focus"]
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_LINE_SUFFIX = re.compile(r"(\.(?:md|markdown)):\d+$", re.IGNORECASE)
_LINK_TARGETS: dict[LinkTarget, tuple[str, str]] = {
    "reader": ("view", "#file-reader"),
    "focus": ("focus", "#focus-dialog-content"),
}


_MARKDOWN = MarkdownIt(
    "gfm-like",
    {
        "html": False,
        "linkify": True,
        "typographer": False,
    },
)


def _render_fence(self, tokens: list[Token], idx: int, options, env) -> str:
    """A ```mermaid fence becomes a marker app.js draws; others render as code."""

    token = tokens[idx]
    words = token.info.split(maxsplit=1)
    if not words or words[0].casefold() != "mermaid":
        return self.fence(tokens, idx, options, env)
    return (
        '<div class="mermaid-diagram" data-mermaid>'
        f'<pre class="mermaid-source">{escapeHtml(token.content)}</pre>'
        "</div>\n"
    )


_MARKDOWN.add_render_rule("fence", _render_fence)


def _project_markdown_path(
    href: str, project: Project, base_dir: str | None = None
) -> tuple[str, str] | None:
    raw_path, separator, fragment = href.partition("#")
    raw_path = _LINE_SUFFIX.sub(r"\1", raw_path)
    parsed = urlsplit(raw_path)
    if parsed.scheme or parsed.netloc or parsed.query:
        return None
    decoded = _LINE_SUFFIX.sub(r"\1", unquote(parsed.path))
    candidate = PurePosixPath(decoded)
    if candidate.is_absolute():
        try:
            decoded = Path(decoded).relative_to(Path(project.path)).as_posix()
        except ValueError:
            return None
    elif base_dir is not None:
        # A viewed file's links are relative to its own folder, as in any
        # Markdown viewer; ".." may climb back to the project root, not past it.
        decoded = posixpath.normpath(posixpath.join(base_dir, decoded))
        if decoded == ".." or decoded.startswith("../"):
            return None
    try:
        parts = project_path_parts(decoded)
    except ProjectFileSecurityError:
        return None
    normalized = "/".join(parts)
    if (
        not normalized
        or PurePosixPath(normalized).suffix.casefold() not in _MARKDOWN_SUFFIXES
    ):
        return None
    return normalized, fragment if separator else ""


def _tokens(tokens: list[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        if token.children:
            yield from _tokens(token.children)


def _project_file_url(
    project: Project,
    path: str,
    fragment: str,
    link_target: LinkTarget | None,
) -> tuple[str, str | None]:
    endpoint, target = (
        _LINK_TARGETS[link_target]
        if link_target is not None
        else ("view", None)
    )
    url = project_url(
        project.name,
        f"/files/{endpoint}?{urlencode({'path': path})}",
    )
    if fragment:
        url = f"{url}#{fragment}"
    return url, target


def render_markdown(
    source: str | None,
    project: Project | None = None,
    link_target: LinkTarget | None = None,
    base_dir: str | None = None,
) -> Markup:
    """Render safe Markdown and route owned Markdown links to the file reader.

    ``base_dir`` is the project-relative folder of the file being shown;
    relative links resolve from it. None resolves from the project root.
    """

    if link_target is not None and link_target not in _LINK_TARGETS:
        raise ValueError("Markdown link target is invalid")
    tokens = _MARKDOWN.parse(source or "")
    if project is not None:
        for token in _tokens(tokens):
            if token.type != "link_open":
                continue
            href = token.attrGet("href")
            linked = _project_markdown_path(href, project, base_dir) if href else None
            if linked is None:
                continue
            url, target = _project_file_url(
                project,
                linked[0],
                linked[1],
                link_target,
            )
            token.attrSet("href", url)
            if target is not None:
                token.attrSet("hx-get", url)
                token.attrSet("hx-target", target)
                token.attrSet("hx-swap", "innerHTML")
    return Markup(_MARKDOWN.renderer.render(tokens, _MARKDOWN.options, {}))

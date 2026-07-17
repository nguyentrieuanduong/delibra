from __future__ import annotations

import os
import random
import re
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.storage import (
    ProjectFileDisplayError,
    ProjectFileSecurityError,
    RegistryStore,
    list_project_directory,
    project_path_parts,
    read_project_file,
)


def setup_file_project(tmp_path: Path, *, file_view_limit: int = 512 * 1024):
    settings = Settings(home=tmp_path / "home", file_view_limit=file_view_limit)
    project_path = tmp_path / "project"
    project_path.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Files", project_path)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    return app, project, project_path


def test_project_path_parser_handles_generated_safe_and_unsafe_inputs() -> None:
    generator = random.Random(20260717)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-_."
    for _ in range(200):
        parts = [
            "".join(generator.choice(alphabet) for _ in range(generator.randint(1, 24)))
            for _ in range(generator.randint(0, 8))
        ]
        safe_parts = [part for part in parts if part != "."]
        value = "/".join(parts)
        assert project_path_parts(value) == tuple(safe_parts)

        for unsafe in (f"../{value}", f"{value}/../escape", f"/{value}", f"{value}\x00"):
            with pytest.raises(ProjectFileSecurityError):
                project_path_parts(unsafe)


def test_file_browser_lists_directories_then_files_with_root_breadcrumb(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    (project_path / "z-dir").mkdir()
    (project_path / "a-dir").mkdir()
    (project_path / "z.txt").write_text("z", encoding="utf-8")
    (project_path / "a.md").write_text("a", encoding="utf-8")

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{project.id}/files")

    assert response.status_code == 200
    assert 'id="file-browser-list"' in response.text
    assert 'aria-label="File breadcrumbs"' in response.text
    assert "Project root" in response.text
    assert response.text.index(".delibra") < response.text.index("a-dir")
    assert response.text.index("a-dir") < response.text.index("z-dir")
    assert response.text.index("z-dir") < response.text.index("a.md")
    assert response.text.index("a.md") < response.text.index("z.txt")


def test_file_view_renders_markdown_safely_and_escapes_plain_text(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    (project_path / "unsafe.MD").write_text(
        "# Heading\n\n<script>alert('x')</script>",
        encoding="utf-8",
    )
    (project_path / "example.py").write_text(
        "value = '<b>literal</b>'",
        encoding="utf-8",
    )

    with TestClient(app, base_url="http://localhost") as client:
        markdown = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "unsafe.MD"},
        )
        plain = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "example.py"},
        )

    assert markdown.status_code == 200
    assert "<h1>Heading</h1>" in markdown.text
    assert "<script>" not in markdown.text
    assert "&lt;script&gt;alert('x')&lt;/script&gt;" in markdown.text
    assert plain.status_code == 200
    assert "<pre>value = &#39;&lt;b&gt;literal&lt;/b&gt;&#39;</pre>" in plain.text


def test_file_view_bounds_the_descriptor_read_and_reports_display_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 8
    app, project, project_path = setup_file_project(tmp_path, file_view_limit=limit)
    source = project_path / "invalid.txt"
    source.write_bytes(b"abc\xffdefghijk")
    original_read = os.read
    bytes_read = 0

    def tracking_read(descriptor: int, size: int) -> bytes:
        nonlocal bytes_read
        block = original_read(descriptor, size)
        bytes_read += len(block)
        return block

    with monkeypatch.context() as patch:
        patch.setattr("app.storage.os.read", tracking_read)
        contents = read_project_file(project_path, "invalid.txt", limit)

    assert contents.data == b"abc\xffdefg"
    assert contents.truncated is True
    assert bytes_read == limit + 1

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "invalid.txt"},
        )

    assert response.status_code == 200
    assert "abc\ufffddefg" in response.text
    assert "File truncated at the view limit." in response.text
    assert "Invalid UTF-8 was replaced for display." in response.text


def test_file_routes_reject_traversal_absolute_and_symlink_paths_without_detail(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET-CONTENT", encoding="utf-8")
    (project_path / "linked").symlink_to(outside, target_is_directory=True)
    (project_path / "leaf.md").symlink_to(outside / "secret.md")

    with TestClient(app, base_url="http://localhost") as client:
        responses = [
            client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "../outside/secret.bin"},
            ),
            client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "/etc/passwd"},
            ),
            client.get(f"/projects/{project.id}/files/view?path=%2e%2e%2Fsecret.md"),
            client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "linked/secret.md"},
            ),
            client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "leaf.md"},
            ),
        ]

    assert [response.status_code for response in responses] == [422] * len(responses)
    for response in responses:
        assert response.json() == {"detail": "invalid project file path"}
        assert "SECRET-CONTENT" not in response.text
        assert str(outside) not in response.text


def test_file_browser_rejects_a_project_root_replaced_with_a_symlink(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (outside / "secret.md").write_text("ROOT-SECRET", encoding="utf-8")
    original = tmp_path / "original-project"
    project_path.rename(original)
    project_path.symlink_to(outside, target_is_directory=True)

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "secret.md"},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid project file path"}
    assert "ROOT-SECRET" not in response.text
    assert str(outside) not in response.text


def test_file_vanishing_between_stat_and_open_is_a_displayability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, project_path = setup_file_project(tmp_path)
    source = project_path / "vanishing.md"
    source.write_text("temporary", encoding="utf-8")
    original_open = os.open
    removed = False

    def vanishing_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal removed
        if path == "vanishing.md" and dir_fd is not None and not removed:
            removed = True
            source.unlink()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr("app.storage.os.open", vanishing_open)
        with pytest.raises(ProjectFileDisplayError):
            read_project_file(project_path, "vanishing.md", 1024)


def test_directory_vanishing_between_stat_and_open_is_a_displayability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, project_path = setup_file_project(tmp_path)
    directory = project_path / "vanishing-directory"
    directory.mkdir()
    original_open = os.open
    removed = False

    def vanishing_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal removed
        if path == "vanishing-directory" and dir_fd is not None and not removed:
            removed = True
            directory.rmdir()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr("app.storage.os.open", vanishing_open)
        with pytest.raises(ProjectFileDisplayError):
            list_project_directory(project_path, "vanishing-directory")


def test_valid_replacement_character_does_not_trigger_invalid_utf8_warning(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    (project_path / "valid.txt").write_text("legitimate \ufffd character", encoding="utf-8")

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "valid.txt"},
        )

    assert response.status_code == 200
    assert "legitimate \ufffd character" in response.text
    assert "Invalid UTF-8 was replaced for display." not in response.text


def test_descriptor_walk_rejects_parent_and_final_leaf_symlink_swaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, project_path = setup_file_project(tmp_path)
    outside = tmp_path / "race-outside"
    outside.mkdir()
    (outside / "secret.md").write_text("RACE-SECRET", encoding="utf-8")

    parent = project_path / "parent"
    parent.mkdir()
    (parent / "source.md").write_text("inside", encoding="utf-8")
    original_open = os.open
    swapped_parent = False

    def parent_swapping_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped_parent
        if path == "parent" and dir_fd is not None and not swapped_parent:
            swapped_parent = True
            parent.rename(project_path / "parent-original")
            parent.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr("app.storage.os.open", parent_swapping_open)
        with pytest.raises(ProjectFileSecurityError):
            read_project_file(project_path, "parent/source.md", 1024)

    leaf = project_path / "leaf-race.md"
    leaf.write_text("inside", encoding="utf-8")
    swapped_leaf = False

    def leaf_swapping_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped_leaf
        if path == "leaf-race.md" and dir_fd is not None and not swapped_leaf:
            swapped_leaf = True
            leaf.rename(project_path / "leaf-original.md")
            leaf.symlink_to(outside / "secret.md")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr("app.storage.os.open", leaf_swapping_open)
        with pytest.raises(ProjectFileSecurityError):
            read_project_file(project_path, "leaf-race.md", 1024)


def test_file_browser_encodes_names_and_marks_symlinks_and_fifos_nonopenable(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    special = project_path / "notes #1.md"
    special.write_text("encoded path", encoding="utf-8")
    (project_path / "linked.md").symlink_to(special)
    fifo = project_path / "events.log"
    os.mkfifo(fifo)

    with TestClient(app, base_url="http://localhost") as client:
        listing = client.get(f"/projects/{project.id}/files")
        opened = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": special.name},
        )
        metadata = client.get(
            f"/projects/{project.id}/files",
            params={"path": ".delibra"},
        )

    assert listing.status_code == 200
    assert "path=notes+%231.md" in listing.text
    assert re.search(r'<span aria-disabled="true">linked\.md</span>', listing.text)
    assert re.search(r'<span aria-disabled="true">events\.log</span>', listing.text)
    assert opened.status_code == 200
    assert "encoded path" in opened.text
    assert metadata.status_code == 200
    assert "manifest.json" in metadata.text


def test_displayability_failures_are_scoped_http_200_fragments_and_routes_are_get_only(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    (project_path / "archive.bin").write_bytes(b"unsupported")
    (project_path / "binary.txt").write_bytes(b"text\x00binary")
    fifo = project_path / "blocked.log"
    os.mkfifo(fifo)
    unreadable_path = project_path / "unreadable.txt"
    unreadable_path.write_text("hidden", encoding="utf-8")
    unreadable_path.chmod(0)

    try:
        with TestClient(app, base_url="http://localhost") as client:
            unsupported = client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "archive.bin"},
            )
            binary = client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "binary.txt"},
            )
            non_regular = client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "blocked.log"},
            )
            unreadable = client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "unreadable.txt"},
            )
            missing_listing = client.get(
                f"/projects/{project.id}/files",
                params={"path": "missing-directory"},
            )
            listing_markup = client.get(f"/projects/{project.id}/files")
            post_listing = client.post(f"/projects/{project.id}/files")
            post_view = client.post(f"/projects/{project.id}/files/view")
    finally:
        unreadable_path.chmod(0o600)

    for response in (unsupported, binary, non_regular, unreadable):
        assert response.status_code == 200
        assert 'class="file-error" role="status"' in response.text
        assert 'id="chat-errors"' not in response.text
    assert missing_listing.status_code == 200
    assert 'id="file-browser-list"' in missing_listing.text
    assert 'class="file-error" role="status"' in missing_listing.text
    assert 'hx-get=' in listing_markup.text
    assert 'hx-target="#file-reader"' in listing_markup.text
    assert post_listing.status_code == 405
    assert post_view.status_code == 405

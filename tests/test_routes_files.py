from __future__ import annotations

from html import unescape
import json
import os
import random
import re
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
import yaml

from app.config import Settings
from app.main import create_app
from app.storage import (
    ProjectFileDisplayError,
    ProjectFileSecurityError,
    ProjectStore,
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


def generated_structured_value(generator: random.Random, depth: int = 0) -> object:
    leaves: list[object] = [
        None,
        True,
        False,
        generator.randint(-10_000, 10_000),
        "".join(generator.choice("abc <>é") for _ in range(12)),
    ]
    if depth >= 3:
        return generator.choice(leaves)
    choice = generator.randrange(3)
    if choice == 0:
        return generator.choice(leaves)
    if choice == 1:
        return [generated_structured_value(generator, depth + 1) for _ in range(3)]
    return {
        f"key-{index}": generated_structured_value(generator, depth + 1)
        for index in range(3)
    }


def structured_pre_text(response_text: str, kind: str) -> str:
    match = re.search(
        rf'<pre class="structured-data" data-format="{kind}">(?P<text>.*?)</pre>',
        response_text,
        flags=re.DOTALL,
    )
    assert match is not None
    return unescape(match["text"])


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


def test_file_browser_has_an_explicit_empty_directory_state(tmp_path: Path) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    (project_path / "empty").mkdir()

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{project.id}/files",
            params={"path": "empty"},
        )

    assert response.status_code == 200
    assert "Project root" in response.text
    assert "empty" in response.text
    assert "No files." in response.text


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


def test_user_selects_edits_and_clears_shared_markdown(tmp_path: Path) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    source = project_path / "brief.md"
    source.write_text("# Initial\n\n<script>unsafe</script>", encoding="utf-8")

    with TestClient(app, base_url="http://localhost") as client:
        opened = client.get(
            f"/projects/{project.id}/files/view", params={"path": "brief.md"}
        )
        assert "Use as shared context" in opened.text
        selected = client.post(
            f"/projects/{project.id}/files/shared/select",
            data={"path": "brief.md"},
        )
        digest = re.search(
            r'name="expected_sha256" value="([0-9a-f]{64})"', selected.text
        )
        assert digest is not None
        assert "Selected as shared context" in selected.text
        assert "<script>" not in selected.text
        focused = client.get(
            f"/projects/{project.id}/files/focus",
            params={"path": "brief.md"},
        )
        assert "/files/shared/" not in focused.text
        saved = client.post(
            f"/projects/{project.id}/files/shared/save",
            data={
                "path": "brief.md",
                "expected_sha256": digest.group(1),
                "text": "# Updated\n",
            },
        )
        assert saved.status_code == 200
        assert "Shared context saved" in saved.text
        cleared = client.post(
            f"/projects/{project.id}/files/shared/clear",
            data={"path": "brief.md"},
        )

    assert source.read_text() == "# Updated\n"
    assert "Use as shared context" in cleared.text
    assert ProjectStore(project).selected_shared_markdown_path() is None


def test_shared_markdown_save_rejects_external_edit_and_reserved_selection(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    source = project_path / "brief.md"
    source.write_text("one", encoding="utf-8")
    (project_path / "other.md").write_text("other", encoding="utf-8")
    (project_path / ".delibra" / "owned.md").write_text(
        "owned", encoding="utf-8"
    )

    with TestClient(app, base_url="http://localhost") as client:
        selected = client.post(
            f"/projects/{project.id}/files/shared/select",
            data={"path": "brief.md"},
        )
        digest = re.search(
            r'name="expected_sha256" value="([0-9a-f]{64})"', selected.text
        )
        assert digest is not None
        source.write_text("external", encoding="utf-8")
        stale = client.post(
            f"/projects/{project.id}/files/shared/save",
            data={
                "path": "brief.md",
                "expected_sha256": digest.group(1),
                "text": "lost",
            },
        )
        reserved = client.post(
            f"/projects/{project.id}/files/shared/select",
            data={"path": ".delibra/owned.md"},
        )
        client.post(
            f"/projects/{project.id}/files/shared/select",
            data={"path": "other.md"},
        )
        stale_clear = client.post(
            f"/projects/{project.id}/files/shared/clear",
            data={"path": "brief.md"},
        )

    assert stale.status_code == 409
    assert source.read_text() == "external"
    assert reserved.status_code == 422
    assert reserved.json() == {"detail": "invalid shared Markdown path"}
    assert "owned" not in reserved.text
    assert stale_clear.status_code == 409
    assert ProjectStore(project).selected_shared_markdown_path() == "other.md"


def test_generated_structured_file_values_are_pretty_and_safe(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    generator = random.Random(20260717)
    values = [
        {"unsafe": "<script>alert('x')</script>", "items": [1, 2]},
        *(generated_structured_value(generator) for _ in range(100)),
    ]

    with TestClient(app, base_url="http://localhost") as client:
        for index, value in enumerate(values):
            json_source = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            (project_path / "value.json").write_text(json_source, encoding="utf-8")
            json_response = client.get(
                f"/projects/{project.id}/files/view",
                params={"path": "value.json"},
            )

            yaml_source = yaml.safe_dump(
                value,
                allow_unicode=True,
                default_flow_style=True,
                sort_keys=False,
            )
            (project_path / "value.yaml").write_text(yaml_source, encoding="utf-8")
            yaml_route = "focus" if index == 0 else "view"
            yaml_response = client.get(
                f"/projects/{project.id}/files/{yaml_route}",
                params={"path": "value.yaml"},
            )

            assert json_response.status_code == 200
            assert yaml_response.status_code == 200
            json_text = structured_pre_text(json_response.text, "json")
            yaml_text = structured_pre_text(yaml_response.text, "yaml")
            assert json.loads(json_text) == value
            assert yaml.safe_load(yaml_text) == value
            assert "<script>" not in json_response.text
            assert "<script>" not in yaml_response.text
            if index == 0:
                assert '\n  "unsafe"' in json_text
                assert "\nitems:" in yaml_text
                assert 'aria-label="Focused file"' in yaml_response.text


def test_yml_extension_uses_the_yaml_pretty_view(tmp_path: Path) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    (project_path / "value.yml").write_text(
        "{items: [one, two]}\n",
        encoding="utf-8",
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "value.yml"},
        )

    assert response.status_code == 200
    rendered = structured_pre_text(response.text, "yaml")
    assert yaml.safe_load(rendered) == {"items": ["one", "two"]}
    assert rendered != "{items: [one, two]}\n"


def test_invalid_structured_file_shows_escaped_source_and_warning(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    source = '{"unsafe":"<script>",'
    (project_path / "invalid.json").write_text(source, encoding="utf-8")

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "invalid.json"},
        )

    assert response.status_code == 200
    assert structured_pre_text(response.text, "json") == source
    assert "<script>" not in response.text
    assert (
        "Could not pretty-format this JSON file; showing the original text."
        in response.text
    )


def test_structured_output_limit_falls_back_to_compact_source(tmp_path: Path) -> None:
    app, project, project_path = setup_file_project(tmp_path, file_view_limit=16)
    source = '{"a":[1,2]}'
    (project_path / "compact.json").write_text(source, encoding="utf-8")

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "compact.json"},
        )

    assert response.status_code == 200
    assert structured_pre_text(response.text, "json") == source
    assert (
        "Pretty-formatted JSON exceeds the view limit; showing the original text."
        in response.text
    )


def test_truncated_structured_file_stays_raw_with_format_warning(
    tmp_path: Path,
) -> None:
    app, project, project_path = setup_file_project(tmp_path, file_view_limit=8)
    (project_path / "truncated.json").write_text(
        '{"items":[1,2]}',
        encoding="utf-8",
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{project.id}/files/view",
            params={"path": "truncated.json"},
        )

    assert response.status_code == 200
    assert structured_pre_text(response.text, "json") == '{"items"'
    assert "File truncated at the view limit." in response.text
    assert (
        "This JSON file is truncated; showing the original text without pretty "
        "formatting."
        in response.text
    )


def test_file_view_offers_close_and_descriptor_safe_focus(tmp_path: Path) -> None:
    app, project, project_path = setup_file_project(tmp_path)
    (project_path / "notes.md").write_text("# Focused note", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("DO-NOT-LEAK", encoding="utf-8")

    with TestClient(app, base_url="http://localhost") as client:
        reader = client.get(
            f"/projects/{project.id}/files/view", params={"path": "notes.md"}
        )
        focused = client.get(
            f"/projects/{project.id}/files/focus", params={"path": "notes.md"}
        )
        traversal = client.get(
            f"/projects/{project.id}/files/focus",
            params={"path": "../outside.md"},
        )

    assert reader.status_code == 200
    assert (
        f'hx-get="/projects/{project.id}/files/focus?path=notes.md"'
        in reader.text
    )
    assert 'hx-target="#focus-dialog-content"' in reader.text
    assert 'hx-swap="innerHTML"' in reader.text
    assert 'data-file-reader-close' in reader.text
    assert focused.status_code == 200
    assert focused.text.count('id="file-focus"') == 1
    assert "<h1>Focused note</h1>" in focused.text
    assert 'id="file-reader"' not in focused.text
    assert 'data-file-reader-close' not in focused.text
    assert 'hx-get=' not in focused.text
    assert traversal.status_code == 422
    assert traversal.json() == {"detail": "invalid project file path"}
    assert "DO-NOT-LEAK" not in traversal.text
    assert str(outside) not in traversal.text


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
        focused = client.get(
            f"/projects/{project.id}/files/focus",
            params={"path": "invalid.txt"},
        )

    assert response.status_code == 200
    assert "abc\ufffddefg" in response.text
    assert "File truncated at the view limit." in response.text
    assert "Invalid UTF-8 was replaced for display." in response.text
    assert focused.status_code == 200
    assert "abc\ufffddefg" in focused.text
    assert "File truncated at the view limit." in focused.text
    assert "Invalid UTF-8 was replaced for display." in focused.text
    assert 'data-file-reader-close' not in focused.text
    assert 'hx-get=' not in focused.text


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
    assert (
        f'hx-get="/projects/{project.id}/files/focus?path=notes+%231.md"'
        in opened.text
    )
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
            focused_binary = client.get(
                f"/projects/{project.id}/files/focus",
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
            post_focus = client.post(
                f"/projects/{project.id}/files/focus",
                params={"path": "notes.md"},
            )
    finally:
        unreadable_path.chmod(0o600)

    for response in (unsupported, binary, non_regular, unreadable):
        assert response.status_code == 200
        assert 'class="file-error" role="status"' in response.text
        assert 'id="chat-errors"' not in response.text
        assert 'data-file-reader-close' in response.text
        assert '/files/focus?' not in response.text
    assert focused_binary.status_code == 200
    assert 'class="file-error" role="status"' in focused_binary.text
    assert "Binary files cannot be displayed." in focused_binary.text
    assert 'data-file-reader-close' not in focused_binary.text
    assert 'hx-get=' not in focused_binary.text
    assert missing_listing.status_code == 200
    assert 'id="file-browser-list"' in missing_listing.text
    assert 'class="file-error" role="status"' in missing_listing.text
    assert 'hx-get=' in listing_markup.text
    assert 'hx-target="#file-reader"' in listing_markup.text
    assert post_listing.status_code == 405
    assert post_view.status_code == 405
    assert post_focus.status_code == 405

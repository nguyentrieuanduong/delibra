from app.markdown import render_markdown


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

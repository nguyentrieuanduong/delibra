from __future__ import annotations

import pytest
import yaml

from app.structured import StructuredKind, pretty_structured_text


@pytest.mark.parametrize(
    ("source", "kind", "label"),
    [
        ('{"open":', "json", "JSON"),
        ("items: [unterminated", "yaml", "YAML"),
    ],
)
def test_invalid_structured_text_falls_back_to_unchanged_source(
    source: str,
    kind: StructuredKind,
    label: str,
) -> None:
    view = pretty_structured_text(source, kind, output_limit=1024)

    assert view.text == source
    assert view.warning == (
        f"Could not pretty-format this {label} file; showing the original text."
    )


def test_yaml_multiple_documents_are_normalized_safely() -> None:
    view = pretty_structured_text(
        "---\na: 1\n---\nb: 2\n",
        "yaml",
        output_limit=1024,
    )

    assert view.warning is None
    assert list(yaml.safe_load_all(view.text)) == [{"a": 1}, {"b": 2}]

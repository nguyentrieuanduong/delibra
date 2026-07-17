"""Safe normalized views for structured project files."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal, TypeAlias

import yaml


StructuredKind: TypeAlias = Literal["json", "yaml"]


@dataclass(frozen=True)
class StructuredView:
    text: str
    warning: str | None = None


def pretty_structured_text(
    source: str,
    kind: StructuredKind,
    *,
    output_limit: int,
) -> StructuredView:
    """Normalize complete structured text for escaped presentation."""
    label = kind.upper()
    try:
        if kind == "json":
            value = json.loads(source)
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            documents = list(yaml.safe_load_all(source))
            rendered = yaml.safe_dump_all(
                documents,
                allow_unicode=True,
                default_flow_style=False,
                explicit_start=len(documents) > 1,
                sort_keys=False,
            )
    except (json.JSONDecodeError, yaml.YAMLError, RecursionError, TypeError, ValueError):
        return StructuredView(
            source,
            f"Could not pretty-format this {label} file; showing the original text.",
        )
    if len(rendered.encode("utf-8")) > output_limit:
        return StructuredView(
            source,
            f"Pretty-formatted {label} exceeds the view limit; "
            "showing the original text.",
        )
    return StructuredView(rendered)

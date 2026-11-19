"""Every built-in prompt renders exactly what Delibra sent before prompts became editable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.capture_prompt_golden as capture

GOLDEN = json.loads(
    (Path(__file__).with_name("fixtures") / "prompts" / "golden.json").read_text(encoding="utf-8")
)


def test_every_case_is_recorded() -> None:
    assert sorted(capture.cases()) == sorted(GOLDEN)


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_built_in_prompt_is_unchanged(case: str) -> None:
    assert capture.cases()[case] == GOLDEN[case]

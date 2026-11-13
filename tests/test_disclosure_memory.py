"""Runs the remembered-disclosure rule's Node contract tests."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_disclosure_memory_js_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; disclosure memory unit test not run")
    result = subprocess.run(
        [node, "--test", "tests/js/test_disclosure_memory.js"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr

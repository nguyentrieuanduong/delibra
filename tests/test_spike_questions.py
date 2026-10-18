"""Pure-function tests for the Auto questions spike harness.

Nothing here starts a provider: a wrong verdict rule would otherwise be found
only after a paid run.
"""

import json
from pathlib import Path

from spike import spike_questions as spike


def make_tree(tmp_path: Path) -> spike.Tree:
    return spike.build_tree(tmp_path, "claude")


ATTEMPTED = f"{spike.STORE_MARKER} {spike.OTHER_MARKER}"


def test_untouched_turn_proves_nothing_about_protection(tmp_path: Path) -> None:
    """Unchanged bytes without an attempt are not a refusal.

    Codex once reported both writes as "operation not permitted" while its
    event stream held no attempt at all; bytes alone scored that PASS.
    """

    tree = make_tree(tmp_path)

    rows = spike.judge(tree, "discussion", events_text="")

    assert rows == {
        "discussion_read_store": "FAIL (store token never appeared)",
        "discussion_write_outbox": "FAIL (outbox not created)",
        "discussion_store_protected": "INCONCLUSIVE (no attempt in events)",
        "discussion_other_outbox_protected": "INCONCLUSIVE (no attempt in events)",
    }


def test_successful_turn_passes_every_probe(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    spike.outbox_path(tree, "build").write_text(json.dumps(spike.OUTBOX), encoding="utf-8")

    rows = spike.judge(tree, "build", events_text=f'{{"text": "{tree.token} {ATTEMPTED}"}}')

    assert set(rows.values()) == {"PASS"}


def test_breached_store_and_other_outbox_fail(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    tree.store.write_text(spike.STORE_MARKER, encoding="utf-8")
    tree.other_outbox.write_text(spike.OTHER_MARKER, encoding="utf-8")

    rows = spike.judge(tree, "discussion", events_text=ATTEMPTED)

    assert rows["discussion_store_protected"].startswith("FAIL")
    assert rows["discussion_other_outbox_protected"].startswith("FAIL")


def test_build_outbox_is_in_temp_and_discussion_outbox_in_workspace(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)

    assert spike.outbox_path(tree, "build").parent.parent == tree.temp
    assert spike.outbox_path(tree, "discussion").parent.parent == tree.workspace


def test_prompt_never_contains_the_token(tmp_path: Path) -> None:
    """The read probe is only evidence if the token can come from nowhere else."""

    tree = make_tree(tmp_path)

    for kind in ("discussion", "build"):
        assert tree.token not in spike.prompt(tree, kind)


def test_reset_restores_seeded_state(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    tree.store.write_text(spike.STORE_MARKER, encoding="utf-8")
    tree.other_outbox.write_text(spike.OTHER_MARKER, encoding="utf-8")
    spike.outbox_path(tree, "discussion").write_text("{}", encoding="utf-8")

    spike.reset(tree)

    assert tree.token in tree.store.read_text(encoding="utf-8")
    assert tree.other_outbox.read_bytes() == spike.SEED
    assert not spike.outbox_path(tree, "discussion").exists()

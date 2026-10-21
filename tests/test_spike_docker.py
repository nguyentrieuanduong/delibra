from spike.spike_docker import (
    judge_container_loopback,
    judge_container_survival,
    judge_container_write,
    judge_home_listing,
    judge_refused,
    judge_unlisted_host,
)


def test_container_write_needs_read_only_evidence() -> None:
    assert judge_container_write(True, "") == "FAIL (file written)"
    assert judge_container_write(False, "sh: can't create /w/x: Read-only file system") == "PASS"
    assert judge_container_write(False, "docker: Error response from daemon: pull failed").startswith("INCONCLUSIVE")
    assert judge_container_write(False, "").startswith("INCONCLUSIVE")


def test_home_listing_needs_a_container_that_ran() -> None:
    assert judge_home_listing(125, "", "spike").startswith("INCONCLUSIVE")
    assert judge_home_listing(0, "spike\n", "spike") == "PASS"
    assert judge_home_listing(0, "spike\n.ssh\n", "spike").startswith("FAIL")


def test_container_loopback_never_passes_a_probe_that_did_not_run() -> None:
    refused = "wget: can't connect to remote host (192.168.5.2): Connection refused"
    assert judge_container_loopback(hits=0, stderr="Unable to find image", control_ok=True, gating=True).startswith("INCONCLUSIVE")
    assert judge_container_loopback(hits=0, stderr=refused, control_ok=False, gating=True).startswith("INCONCLUSIVE")
    assert judge_container_loopback(hits=0, stderr=refused, control_ok=True, gating=True) == "PASS"
    assert judge_container_loopback(hits=0, stderr=refused, control_ok=True, gating=False) == "RECORDED (refused)"
    assert judge_container_loopback(hits=1, stderr="", control_ok=True, gating=True) == "FAIL (reached)"
    assert judge_container_loopback(hits=1, stderr="", control_ok=True, gating=False) == "RECORDED (reached)"


def test_a_missing_probe_result_is_never_read_as_reached_or_blocked() -> None:
    # S4 run 2 (2026-10-21) reported "reached example.com" for a probe whose
    # result may simply be missing; a missing result proves nothing.
    assert judge_refused(None, "reached example.com").startswith("INCONCLUSIVE")
    assert judge_refused(0, "reached example.com") == "FAIL (reached example.com)"
    assert judge_refused(22, "reached example.com") == "PASS"


def test_codex_stopping_the_command_on_a_blocked_domain_is_evidence() -> None:
    # S4 run 3 (2026-10-21): Codex aborts the whole command with this output,
    # so the probe never writes its result.
    blocked = [{"type": "item.completed", "item": {
        "type": "command_execution", "exit_code": -1, "status": "failed",
        "aggregated_output": 'Network access to "example.com" was blocked: domain is not on the allowlist for the current sandbox mode.',
    }}]
    assert judge_unlisted_host(None, blocked).startswith("PASS")
    assert judge_unlisted_host(None, []).startswith("INCONCLUSIVE")
    assert judge_unlisted_host(0, blocked) == "FAIL (reached example.com)"
    assert judge_unlisted_host(22, []) == "PASS"


def test_container_survival_needs_a_clean_release_and_a_listing() -> None:
    assert judge_container_survival(started=False, released=False, left_rc=None, left="").startswith("INCONCLUSIVE")
    assert judge_container_survival(started=True, released=False, left_rc=None, left="") == "FAIL (release did not stop the VM)"
    assert judge_container_survival(started=True, released=True, left_rc=1, left="").startswith("INCONCLUSIVE")
    assert judge_container_survival(started=True, released=True, left_rc=0, left="abc\n") == "FAIL (left 'abc')"
    assert judge_container_survival(started=True, released=True, left_rc=0, left="") == "PASS"

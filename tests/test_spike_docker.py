from spike.spike_docker import judge_container_loopback, judge_container_write, judge_home_listing


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

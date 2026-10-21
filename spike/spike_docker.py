"""Docker spikes: S5 (Lima boundary, no provider), S3 (Claude), S4 (Codex).

Plan `.plans/2026-10-21-auto-build-docker-plan.md`, Tasks 7-8. Not part of
pytest. S5 creates and deletes a real Lima VM in a throwaway DELIBRA_HOME
under ~ (never ~/.lima); S3/S4 also drive one real provider turn each and
spend a subscription. Run only with operator approval:

    envs/bin/python spike/spike_docker.py --target lima
    envs/bin/python spike/spike_docker.py --target claude
    envs/bin/python spike/spike_docker.py --target codex

Verdicts come from filesystem bytes, listener hits and exit codes, never
model prose. A probe whose control fails is INCONCLUSIVE, never PASS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.base import build_command_environment  # noqa: E402
from app.config import Settings  # noqa: E402
from app.docker_backend import (  # noqa: E402
    DockerController, find_host_docker_cli, write_cli_config,
)

FINDINGS = ROOT / "spike" / "FINDINGS.md"
IMAGE = "busybox:1.36"
MARKER = "DOCKER-SPIKE-MARKER\n"
LAN_IP = os.environ.get("DELIBRA_SPIKE_LAN_IP", "192.168.1.1")
LAN_PORT = int(os.environ.get("DELIBRA_SPIKE_LAN_PORT", "80"))


class Hits:
    """A loopback TCP listener that records connections (host-side evidence)."""

    def __init__(self) -> None:
        self.count = 0
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.count += 1
            conn.close()

    def close(self) -> None:
        self.server.close()


def docker(endpoint: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    cli = find_host_docker_cli(dict(os.environ))
    return subprocess.run(
        [str(cli.executable), "--host", f"unix://{endpoint}", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def lima_shell(settings: Settings, instance: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, LIMA_HOME=str(settings.home / "lima"))
    return subprocess.run(
        [shutil.which("limactl") or "limactl", "shell", "--tty=false", "--workdir", "/", instance, *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def upsert(target: str, section: str) -> None:
    start, end = f"<!-- DOCKER-SPIKE-{target.upper()}:START -->", f"<!-- DOCKER-SPIKE-{target.upper()}:END -->"
    existing = FINDINGS.read_text(encoding="utf-8") if FINDINGS.exists() else "# M0 CLI findings\n\n"
    block = f"{start}\n{section.rstrip()}\n{end}"
    if start in existing and end in existing:
        existing = re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: block, existing, flags=re.DOTALL)
    else:
        existing = existing.rstrip() + "\n\n" + block + "\n"
    FINDINGS.write_text(existing, encoding="utf-8")


def judge_container_write(written: bool, stderr: str) -> str:
    """PASS needs proof the write ran and hit the read-only share."""

    if written:
        return "FAIL (file written)"
    if "Read-only file system" in stderr:
        return "PASS"
    return f"INCONCLUSIVE (no read-only refusal seen: {stderr.strip()[:200]!r})"


def judge_home_listing(returncode: int, stdout: str, allowed: str) -> str:
    if returncode != 0:
        return f"INCONCLUSIVE (listing container failed, rc {returncode})"
    visible = set(stdout.split())
    return "PASS" if visible <= {allowed} else f"FAIL (guest sees {sorted(visible)[:10]})"


def judge_container_loopback(*, hits: int, stderr: str, control_ok: bool, gating: bool) -> str:
    """No listener hit counts as closed only with a working network and a refusal."""

    if hits:
        return "FAIL (reached)" if gating else "RECORDED (reached)"
    if not control_ok:
        return "INCONCLUSIVE (the container's network control failed)"
    if any(text in stderr for text in ("Connection refused", "timed out", "Network is unreachable")):
        return "PASS" if gating else "RECORDED (refused)"
    return f"INCONCLUSIVE (probe did not run as expected: {stderr.strip()[:200]!r})"


def render(target: str, versions: dict[str, str], rows: dict[str, str], notes: list[str]) -> str:
    gating = {k: v for k, v in rows.items() if not v.startswith("RECORDED")}
    verdict = (
        "FAIL" if any(v.startswith("FAIL") for v in gating.values())
        else "INCONCLUSIVE" if any(v.startswith("INCONCLUSIVE") for v in gating.values()) or not gating
        else "PASS"
    )
    table = "\n".join(f"| {name} | {value} |" for name, value in rows.items())
    version_lines = "\n".join(f"- {name}: `{value}`" for name, value in versions.items())
    note_lines = "\n".join(f"- {note}" for note in notes)
    return (
        f"## Docker spike: {target}\n\n{version_lines}\n- Verdict: **{verdict}**\n\n"
        f"| Probe | Result |\n|---|---|\n{table}\n\n{note_lines}\n"
    )


async def lima_spike(tree: Path, rows: dict[str, str], versions: dict[str, str], notes: list[str]) -> None:
    settings = Settings(home=tree / "h")
    project = tree / "project"
    project.mkdir()
    (project / "marker.txt").write_text(MARKER)
    controller = DockerController(settings)
    status = await controller.lima_status()
    versions["limactl"] = status.version_text or "unknown"
    started = time.monotonic()
    policy = await controller.prepare_lima("spike-project", project)
    notes.append(f"prepare took {time.monotonic() - started:.0f} s")
    if policy.status != "ready":
        rows["prepare"] = f"FAIL ({policy.error})"
        return
    rows["prepare"] = "PASS"
    versions["docker (guest)"] = policy.docker_version or "unknown"
    started = time.monotonic()
    access, state = await controller.acquire("spike-project", project, policy)
    notes.append(f"second boot (acquire) took {time.monotonic() - started:.0f} s")
    rows["restart_and_probes"] = "PASS"
    endpoint = access.socket
    try:
        mounts = lima_shell(settings, state.instance or "", "findmnt", "-rn", "-o", "TARGET,FSTYPE,OPTIONS")
        notes.append("guest mounts: " + " ; ".join(line for line in mounts.stdout.splitlines() if "virtiofs" in line or "9p" in line))
        loopback = "closed" if state.host_loopback_closed else "OPEN (Delibra reachable from the VM)"
        rows["host_loopback"] = (
            ("PASS" if state.host_loopback_closed else "FAIL (Lima >= 2.3 must close it)")
            if status.closes_host_loopback else f"RECORDED ({loopback})"
        )
        read = docker(endpoint, "run", "--rm", "-v", f"{project}:/w:ro", IMAGE, "cat", "/w/marker.txt")
        rows["container_reads_project"] = "PASS" if read.stdout == MARKER else f"INCONCLUSIVE (control failed: {read.stderr.strip()[:200]})"
        write = docker(endpoint, "run", "--rm", "-v", f"{project}:/w", IMAGE, "sh", "-c", "echo x > /w/container-write.txt")
        rows["container_cannot_write_project"] = judge_container_write(
            (project / "container-write.txt").exists(), write.stderr
        )
        privileged = docker(endpoint, "run", "--rm", "--privileged", "-v", f"{project}:/w", IMAGE, "sh", "-c", "mount -o remount,rw /w; echo x > /w/privileged-write.txt")
        rows["privileged_cannot_write_project"] = judge_container_write(
            (project / "privileged-write.txt").exists(), privileged.stderr
        )
        parent = project.parent
        listing = docker(endpoint, "run", "--rm", "-v", f"{parent.parent}:/u:ro", IMAGE, "ls", "-A", "/u")
        rows["home_not_shared"] = judge_home_listing(listing.returncode, listing.stdout, parent.name)
        # The guest's own name for this machine, not a hard-coded address.
        gateway = lima_shell(settings, state.instance or "", "getent", "ahostsv4", "host.lima.internal")
        gateway_ip = gateway.stdout.split()[0] if gateway.returncode == 0 and gateway.stdout.split() else None
        # Positive control: the container's network works at all.
        control = docker(endpoint, "run", "--rm", IMAGE, "wget", "-T", "10", "-q", "-O", "/dev/null", "http://example.com/", timeout=60)
        hits = Hits()
        try:
            probe = (
                docker(endpoint, "run", "--rm", IMAGE, "wget", "-T", "5", "-q", "-O", "/dev/null",
                       f"http://{gateway_ip}:{hits.port}/", timeout=60)
                if gateway_ip is not None else None
            )
        finally:
            hits.close()
        rows["container_host_loopback"] = (
            judge_container_loopback(
                hits=hits.count, stderr=probe.stderr, control_ok=control.returncode == 0,
                gating=status.closes_host_loopback,
            )
            if probe is not None
            else f"INCONCLUSIVE (host.lima.internal did not resolve in the guest: {gateway.stderr.strip()[:200]})"
        )
        lan = docker(endpoint, "run", "--rm", IMAGE, "wget", "-T", "5", "-q", "-O", "/dev/null", f"http://{LAN_IP}:{LAN_PORT}/", timeout=60)
        rows["container_lan"] = f"RECORDED ({'reached' if lan.returncode == 0 else 'not reached'} {LAN_IP}:{LAN_PORT})"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        published = docker(endpoint, "run", "-d", "--rm", "-p", f"{port}:8000", IMAGE, "httpd", "-f", "-p", "8000")
        try:
            time.sleep(8)
            inside = lima_shell(settings, state.instance or "", "curl", "-s", "-m", "3", "-o", "/dev/null", f"http://127.0.0.1:{port}/")
            with socket.socket() as client:
                client.settimeout(3)
                forwarded = client.connect_ex(("127.0.0.1", port)) == 0
            if inside.returncode != 0:
                rows["published_port_not_on_host"] = f"INCONCLUSIVE (container port not serving in the guest, rc {inside.returncode})"
            else:
                rows["published_port_not_on_host"] = "FAIL (forwarded)" if forwarded else "PASS"
        finally:
            if published.stdout.strip():
                docker(endpoint, "rm", "-f", published.stdout.strip())
    finally:
        await controller.release(state)


async def provider_spike(provider: str, tree: Path, rows: dict[str, str], versions: dict[str, str], notes: list[str]) -> None:
    raise NotImplementedError("Task 8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("lima", "claude", "codex"), required=True)
    parser.add_argument("--keep", action="store_true", help="keep the VM and temp tree")
    args = parser.parse_args()
    rows: dict[str, str] = {}
    versions: dict[str, str] = {}
    notes: list[str] = []
    tree = Path(tempfile.mkdtemp(prefix=".dlb-docker-spike-", dir=Path.home()))
    try:
        if args.target == "lima":
            asyncio.run(lima_spike(tree, rows, versions, notes))
        else:
            asyncio.run(provider_spike(args.target, tree, rows, versions, notes))
    except Exception as exc:  # the findings section must survive any failure
        rows["spike_error"] = f"INCONCLUSIVE ({type(exc).__name__}: {exc})"
    finally:
        if not args.keep:
            env = dict(os.environ, LIMA_HOME=str(tree / "h" / "lima"))
            for item in subprocess.run(["limactl", "list", "--json"], capture_output=True, text=True, env=env).stdout.splitlines():
                name = json.loads(item)["name"]
                subprocess.run(["limactl", "delete", "--tty=false", "--force", name], env=env, capture_output=True)
            shutil.rmtree(tree, ignore_errors=True)
        section = render(args.target, versions, rows, notes)
        upsert(args.target, section)
        print(section)
    return 0 if rows and all(v.startswith(("PASS", "RECORDED")) for v in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

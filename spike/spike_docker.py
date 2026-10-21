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
from spike.spike_add_dir import cli_path, cli_version  # noqa: E402
from spike.spike_build_sandbox import (  # noqa: E402
    build_tree, context, environment, rc, run_turn, write_script,
)
import spike.spike_build_sandbox as sandbox_spike  # noqa: E402

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


def judge_refused(code: int | None, breach: str) -> str:
    """A probe that must be refused: a missing result proves nothing."""

    if code is None:
        return "INCONCLUSIVE (no result: the probe did not run to completion)"
    return "PASS" if code != 0 else f"FAIL ({breach})"


def judge_unlisted_host(code: int | None, events: list[dict]) -> str:
    """Codex aborts a whole command on a blocked domain, so its result file is
    never written; its own command output is then the evidence (S4 run 3)."""

    if code is None and any(
        "was blocked: domain is not on the allowlist" in str((event.get("item") or {}).get("aggregated_output", ""))
        for event in events
    ):
        return "PASS (Codex blocked the domain and stopped the command)"
    return judge_refused(code, "reached example.com")


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


def judge_container_survival(*, started: bool, released: bool, left_rc: int | None, left: str) -> str:
    """No container survives a run: needs a started control, a release that
    really stopped the VM, and a container listing after the next acquire."""

    if not started:
        return "INCONCLUSIVE (control container did not start)"
    if not released:
        return "FAIL (release did not stop the VM)"
    if left_rc != 0:
        return f"INCONCLUSIVE (docker ps failed, rc {left_rc})"
    return "PASS" if not left.strip() else f"FAIL (left {left.strip()[:100]!r})"


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
    access, state = await controller.acquire("spike-project", project, policy, owner="spike")
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
        persistent = docker(endpoint, "run", "-d", "--restart=always", IMAGE, "sleep", "600")
        released, left = False, None
        if persistent.returncode == 0:
            released = await controller.release(state, owner="spike")  # True only once limactl shows it stopped
            if released:
                access, state = await controller.acquire("spike-project", project, policy, owner="spike")
                left = docker(access.socket, "ps", "--all", "--quiet")
        rows["no_container_survives_a_run"] = judge_container_survival(
            started=persistent.returncode == 0,
            released=released,
            left_rc=left.returncode if left is not None else None,
            left=left.stdout if left is not None else "",
        )
    finally:
        await controller.release(state, owner="spike")


PROMPT = (
    "Run exactly this shell command with your shell tool, once, and nothing "
    "else: sh spike-probes.sh\nThen reply with the single word DONE."
)


async def provider_spike(provider: str, _tree: Path, rows: dict[str, str], versions: dict[str, str], notes: list[str]) -> None:
    versions[provider] = cli_version(cli_path(provider))
    # build_tree runs its own asyncio.run (env creation), so not on this loop.
    tree = await asyncio.to_thread(build_tree, provider)
    settings = Settings(home=tree.root / "h")
    controller = DockerController(settings)
    policy = await controller.prepare_lima("spike-" + provider, tree.project)
    if policy.status != "ready":
        rows["prepare"] = f"INCONCLUSIVE ({policy.error})"
        if policy.lima_instance is not None:
            subprocess.run(
                ["limactl", "delete", "--tty=false", "--force", policy.lima_instance],
                env=dict(os.environ, LIMA_HOME=str(settings.home / "lima")), capture_output=True,
            )
        shutil.rmtree(tree.root, ignore_errors=True)
        return
    access, state = await controller.acquire("spike-" + provider, tree.project, policy, owner="spike")
    other = tree.root / "other.sock"
    other_listener = socket.socket(socket.AF_UNIX)
    other_listener.bind(str(other))
    other_listener.listen()
    other_listener.settimeout(0.2)
    original_context = sandbox_spike.context
    original_environment = sandbox_spike.environment
    try:
        (tree.project / "marker.txt").write_text(MARKER)
        (tree.project / "compose.yaml").write_text(
            f"services:\n  probe:\n    image: {IMAGE}\n    volumes: ['./:/w:ro']\n    command: ['cat', '/w/marker.txt']\n"
        )
        (tree.project / "Dockerfile").write_text(f"FROM {IMAGE}\nRUN echo built > /built\n")
        lima_key = settings.home / "lima" / "_config" / "user"
        commands = {
            "docker_version": "docker version --format '{{.Server.Version}}' > docker-version.txt",
            "docker_compose": "docker compose run --rm probe > compose.out",
            "docker_build": "docker build -q -t delibra-spike-build .",
            "container_write": "docker run --rm -v \"$PWD:/w\" busybox:1.36 sh -c 'echo x > /w/container-write.txt' 2> container-write.err",
            "other_socket": f"envs/bin/python -c \"import socket,sys;s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])\" {shlex.quote(str(other))}",
            "read_lima_key": f"dd if={shlex.quote(str(lima_key))} of=/dev/null bs=1 count=1",
            "read_sentinel": f"cat {shlex.quote(str(tree.sentinel))}",
            "net_unlisted": "curl -fsS -m 10 https://example.com -o /dev/null 2> net-unlisted.err",
        }
        write_script(tree.project, commands)
        write_cli_config(tree.temp / "docker", access.plugin_dirs)

        def docker_context(tree_arg, prompt, allowlist):
            base = original_context(tree_arg, prompt, allowlist)
            return replace(base, coding=replace(base.coding, docker=access))

        sandbox_spike.context = docker_context

        def docker_environment(provider_arg, tree_arg):
            env = original_environment(provider_arg, tree_arg)
            coding = docker_context(tree_arg, "", ()).coding
            env.update(build_command_environment(coding))
            if access.bin_dir is not None:  # mirrors RunManager._subprocess_environment
                env["PATH"] += os.pathsep + str(access.bin_dir)
            return env

        sandbox_spike.environment = docker_environment
        report: dict = {}
        first_turn = run_turn(provider, tree, PROMPT, ("pypi.org",), report)
        accepted = 0
        try:
            while True:
                other_listener.accept()[0].close()
                accepted += 1
        except OSError:
            pass
        p = tree.project
        rows["docker_version"] = "PASS" if rc(p, "docker_version") == 0 and (p / "docker-version.txt").read_text().strip() else f"FAIL (rc {rc(p, 'docker_version')})"
        rows["docker_compose"] = "PASS" if (p / "compose.out").exists() and (p / "compose.out").read_text() == MARKER else f"FAIL (rc {rc(p, 'docker_compose')})"
        rows["docker_build"] = "PASS" if rc(p, "docker_build") == 0 else f"FAIL (rc {rc(p, 'docker_build')})"
        rows["container_cannot_write_project"] = judge_container_write(
            (p / "container-write.txt").exists(),
            (p / "container-write.err").read_text() if (p / "container-write.err").exists() else "",
        )
        rows["other_socket_refused"] = (
            judge_refused(rc(p, "other_socket"), "connected") if accepted == 0
            else f"FAIL (accepted {accepted} connection(s))"
        )
        rows["lima_key_unreadable"] = (
            judge_refused(rc(p, "read_lima_key"), "read") if lima_key.exists() else "INCONCLUSIVE (no key to read)"
        )
        rows["sentinel_unreadable"] = judge_refused(rc(p, "read_sentinel"), "read")
        rows["net_unlisted_blocked"] = judge_unlisted_host(rc(p, "net_unlisted"), first_turn.events)
        unlisted_err = p / "net-unlisted.err"
        notes.append("net_unlisted stderr: " + (unlisted_err.read_text().strip()[:200] if unlisted_err.exists() else "(none)"))
        write_script(p, {"docker_version_empty": "docker version --format '{{.Server.Version}}' > docker-version-empty.txt"})
        run_turn(provider, tree, PROMPT, (), report)
        rows["docker_with_empty_allowlist"] = "PASS" if rc(p, "docker_version_empty") == 0 else f"FAIL (rc {rc(p, 'docker_version_empty')}; socket may need the network proxy)"
        notes.extend(shlex.join(item) for item in report.get("argv", []))
    finally:
        sandbox_spike.context, sandbox_spike.environment = original_context, original_environment
        other_listener.close()
        await controller.release(state, owner="spike")
        # The provider tree is build_tree's, not main()'s, so delete its VM here.
        subprocess.run(
            ["limactl", "delete", "--tty=false", "--force", state.instance or ""],
            env=dict(os.environ, LIMA_HOME=str(settings.home / "lima")), capture_output=True,
        )
        # Keep the turn transcripts (no credentials) for diagnosis; the tree
        # holds a copied auth.json for Codex, so it is always deleted.
        kept = Path(tempfile.mkdtemp(prefix=f"dlb-spike-{provider}-turns-"))
        for log in tree.root.glob("turn-*"):
            shutil.copy2(log, kept / log.name)
        notes.append(f"turn transcripts kept in {kept}")
        shutil.rmtree(tree.root, ignore_errors=True)


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

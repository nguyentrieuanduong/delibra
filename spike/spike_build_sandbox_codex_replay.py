"""Model-free S2 replay: Codex build-turn shell probes under `codex sandbox`.

Companion to `spike/spike_build_sandbox.py` (Task 4 of
`.plans/2026-10-14-coding-mode-auto-build-plan.md`, amendments A2/A3). Under
`codex exec`, a command that touches a blocked domain is killed outright, so
the probe script stops at the first denied host and no later probe runs. This
replay runs the same script, with the same `-c` overrides, disabled features
and `delibra_build` permission profile, through `codex sandbox`, which enforces
the same seatbelt policy and network proxy without a model call. The user
accepted it as the S2 shell evidence on 2026-10-16.

It reuses a tree kept by `spike_build_sandbox.py --provider codex --keep`:

    envs/bin/python spike/spike_build_sandbox_codex_replay.py [TREE]

Writes a BUILD-SANDBOX-SPIKE-CODEX-REPLAY section to spike/FINDINGS.md.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.codex import BUILD_DISABLED_FEATURES, codex_build_overrides  # noqa: E402
import spike.spike_build_sandbox as spike  # noqa: E402


def start_lan_listener():
    """Listen on this machine's private address and point the LAN probes at it."""

    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # no packet is sent; selects the outbound interface
        own_ip = probe.getsockname()[0]
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.requestline)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer((own_ip, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    spike.LAN_IP = own_ip
    spike.LAN_PORT = server.server_address[1]
    spike.LAN_NAME = f"{own_ip.replace('.', '-')}.nip.io"
    spike.ALLOWLIST = ("pypi.org", "files.pythonhosted.org", spike.LAN_NAME)
    return server, hits


def kept_codex_tree() -> Path:
    # The Claude tree holds a dummy auth.json; the Codex tree holds a real copy.
    trees = [
        tree for tree in Path.home().glob(".delibra-build-spike-*")
        if '"spike"' not in (tree / "codex-home" / "auth.json").read_text()
    ]
    if not trees:
        raise SystemExit("no kept Codex spike tree; run spike_build_sandbox.py --provider codex --keep")
    return max(trees, key=lambda tree: tree.stat().st_mtime)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tree", nargs="?", type=Path)
    parser.add_argument(
        "--lan-self", action="store_true",
        help="target a listener on this machine's own private address (how a LAN-exposed Delibra is reached)",
    )
    args = parser.parse_args()
    lan_server = None
    lan_hits: list[str] = []
    if args.lan_self:
        lan_server, lan_hits = start_lan_listener()
    root = args.tree or kept_codex_tree()
    project = root / "project"
    outside = root / "outside"
    sentinel = outside / "credential-sentinel.txt"
    tree = spike.Tree(
        root, project, root / "build-tmp", root / "codex-home", sentinel,
        sentinel.read_text().strip(), outside / "secret.txt", outside / "target.txt",
        outside / "direct-target.txt", outside / "tool-target.txt",
    )
    # Reset everything an earlier run may have touched.
    for seeded in (project / ".git" / "delibra-spike", project / ".delibra" / "delibra-spike",
                   tree.outside_write, tree.outside_direct):
        seeded.write_bytes(spike.SEED)
    (project / "spike-ok.txt").unlink(missing_ok=True)
    for installed in project.glob("envs/lib/python*/site-packages/six*"):
        subprocess.run(["rm", "-rf", str(installed)], check=True)

    delibra, server_a = spike.start_listener()
    proxy, server_b = spike.start_listener()
    try:
        commands = spike.probes(tree, delibra, proxy)
        base = spike.baseline(tree, commands)
        proxy_seen = bool(proxy.hits)
        delibra.hits.clear()
        proxy.hits.clear()
        lan_hits.clear()  # the unsandboxed baseline requests are the control, not evidence
        spike.write_script(project, commands)
        coding = spike.context(tree, "", spike.ALLOWLIST).coding
        assert coding is not None
        argv = ["codex"]
        for override in codex_build_overrides(coding):
            argv += ["-c", override]
        for feature in ("hooks", "plugins", "apps", "memories", "goals", "multi_agent", *BUILD_DISABLED_FEATURES):
            argv += ["--disable", feature]
        argv += ["sandbox", "-P", "delibra_build", "-C", str(project), "--", "sh", "spike-probes.sh"]
        started = time.monotonic()
        completed = subprocess.run(
            argv, cwd=project, env=spike.environment("codex", tree),
            capture_output=True, text=True, timeout=900,
        )
        rows = spike.judge(tree, delibra, proxy, "codex", base, proxy_seen)
        rows["codex_sandbox_exit"] = (
            f"PASS (rc=0 in {time.monotonic() - started:.1f}s)" if completed.returncode == 0
            else f"FAIL (rc={completed.returncode}: {completed.stderr[-300:]!r})"
        )
        if args.lan_self:
            for name in ("net_lan_ip", "net_lan_name"):
                if lan_hits and rows[name].startswith("PASS"):
                    rows[name] = f"FAIL ({len(lan_hits)} request(s) reached the LAN listener)"
            rows["lan_target"] = f"RECORDED (this machine's own address {spike.LAN_IP}:{spike.LAN_PORT})"
    finally:
        server_a.shutdown()
        server_b.shutdown()
        if lan_server is not None:
            lan_server.shutdown()
    version = spike.cli_version(spike.cli_path("codex"))
    section = spike.render("codex-replay", version, rows, [argv])
    spike.upsert("codex-replay", section)
    print(section)
    return 0 if all(value.startswith(("PASS", "RECORDED")) for value in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

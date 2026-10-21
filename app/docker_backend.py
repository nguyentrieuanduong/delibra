"""Docker for Auto build runs: a per-project Lima VM, or local Docker by opt-in.

Design: docs/specs/2026-10-21-auto-build-docker-design.md. Every limactl
call runs with LIMA_HOME=<DELIBRA_HOME>/lima, so the user's own ~/.lima
instances and _config overrides are never read or touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from app.agents.base import DockerAccess

LIMA_MIN_VERSION = (2, 2, 1)
# networkOpts.hostLoopback (lima-vm/lima#5581) ships in the first stable 2.3.
LIMA_HOST_LOOPBACK_VERSION = (2, 3, 0)
INSTANCE_PREFIX = "delibra-"
# macOS sun_path is 104 bytes including the terminating NUL.
SUN_PATH_MAX = 103
ERROR_TAIL_BYTES = 4_000
SYSTEM_PLUGIN_DIRS = (
    Path("/opt/homebrew/lib/docker/cli-plugins"),
    Path("/usr/local/lib/docker/cli-plugins"),
    Path("/usr/local/libexec/docker/cli-plugins"),
    Path("/usr/libexec/docker/cli-plugins"),
)
_LIMA_VERSION = re.compile(r"limactl version (\d+)\.(\d+)\.(\d+)")
# Docker's signed apt repository, never `curl get.docker.com | sh`.
SYSTEM_PROVISION = """#!/bin/bash
set -eux -o pipefail
command -v dockerd >/dev/null 2>&1 && exit 0
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl uidmap dbus-user-session
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin
systemctl disable --now docker.service docker.socket containerd.service || true
systemctl mask docker.service docker.socket containerd.service || true
"""
USER_PROVISION = """#!/bin/bash
set -eux -o pipefail
systemctl --user start dbus
dockerd-rootless-setuptool.sh install
"""
READY_PROBE = """#!/bin/bash
set -eux -o pipefail
timeout 60s bash -c "until test -S /run/user/$(id -u)/docker.sock; do sleep 3; done"
"""


class DockerUnavailable(Exception):
    """A backend cannot serve Docker; the message is shown to the user."""


def tail(text: str) -> str:
    return text.encode("utf-8")[-ERROR_TAIL_BYTES:].decode("utf-8", "ignore").strip()


def parse_lima_version(text: str) -> tuple[int, int, int] | None:
    """`limactl --version` of a stable release; None for pre-releases."""

    match = _LIMA_VERSION.fullmatch(text.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def dotted(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def instance_name(project_id: str) -> str:
    return INSTANCE_PREFIX + hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:10]


def socket_path(lima_home: Path, instance: str) -> Path:
    return lima_home / instance / "sock" / "docker.sock"


def lima_config(project_root: Path, *, close_host_loopback: bool) -> str:
    """The instance's lima.yaml as JSON (a YAML subset): paths need no escaping."""

    config: dict[str, object] = {
        "minimumLimaVersion": "2.2.1",
        "base": ["template:_images/ubuntu-lts"],
        "vmType": "vz",
        "mountType": "virtiofs",
        # Same path in the guest, so compose bind mounts resolve; VZ enforces
        # read-only on the host side (design §3).
        "mounts": [{"location": project_root.as_posix(), "writable": False}],
        "ssh": {"forwardAgent": False, "loadDotSSHPubKeys": False, "forwardX11": False},
        "propagateProxyEnv": False,
        "containerd": {"system": False, "user": False},
        "provision": [
            {"mode": "system", "script": SYSTEM_PROVISION},
            {"mode": "user", "script": USER_PROVISION},
        ],
        "probes": [{"script": READY_PROBE, "hint": "See /var/log/cloud-init-output.log in the guest"}],
        "portForwards": [
            {"guestSocket": "/run/user/{{.UID}}/docker.sock", "hostSocket": "{{.Dir}}/sock/docker.sock"},
            # No guest port reaches this machine's loopback.
            {"guestIP": "0.0.0.0", "guestIPMustBeZero": False, "proto": "any", "ignore": True},
            {"guestIP": "127.0.0.1", "proto": "any", "ignore": True},
        ],
    }
    if close_host_loopback:
        config["networkOpts"] = {"hostLoopback": False}
    return json.dumps(config, indent=2) + "\n"


@dataclass(frozen=True)
class HostDockerCli:
    executable: Path
    plugin_dirs: tuple[Path, ...]

    @property
    def read_roots(self) -> tuple[Path, ...]:
        roots = [self.executable.parent]
        roots.extend(path for path in self.plugin_dirs if path not in roots)
        return tuple(roots)

    def access(self, socket: Path) -> DockerAccess:
        return DockerAccess(socket=socket, cli_read_roots=self.read_roots, plugin_dirs=self.plugin_dirs)


def find_host_docker_cli(
    environ: dict[str, str],
    *,
    system_plugin_dirs: tuple[Path, ...] = SYSTEM_PLUGIN_DIRS,
) -> HostDockerCli:
    """The docker binary and plugin dirs, resolved past Desktop's symlinks.

    Build turns cannot read HOME, so the agents get the real directories.
    """

    found = shutil.which("docker", path=environ.get("PATH"))
    if found is None:
        raise DockerUnavailable(
            "the docker CLI is not on PATH; install Docker Desktop or "
            "`brew install docker docker-compose docker-buildx`"
        )
    candidates = (Path(environ.get("HOME", "/nonexistent")) / ".docker" / "cli-plugins", *system_plugin_dirs)
    plugin_dirs: list[Path] = []
    has_compose = False
    for plugin in ("docker-compose", "docker-buildx"):
        for directory in candidates:
            if (directory / plugin).is_file():
                real = (directory / plugin).resolve().parent
                if real not in plugin_dirs:
                    plugin_dirs.append(real)
                has_compose = has_compose or plugin == "docker-compose"
                break
    if not has_compose:
        raise DockerUnavailable(
            "the docker compose plugin was not found; install Docker Desktop or "
            "`brew install docker-compose`"
        )
    return HostDockerCli(Path(found).resolve(), tuple(plugin_dirs))


def write_cli_config(config_dir: Path, plugin_dirs: tuple[Path, ...]) -> None:
    """A private DOCKER_CONFIG: plugin dirs only, never a credential helper."""

    config_dir.mkdir(mode=0o700)
    (config_dir / "config.json").write_text(
        json.dumps({"cliPluginsExtraDirs": [path.as_posix() for path in plugin_dirs]}) + "\n",
        encoding="utf-8",
    )


def require_user_socket(path: Path) -> None:
    try:
        info = os.stat(path)
    except OSError as exc:
        raise DockerUnavailable(f"{path} is unavailable: {exc.strerror}") from exc
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise DockerUnavailable(f"{path} is not a socket owned by this user")

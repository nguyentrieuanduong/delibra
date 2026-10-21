"""Docker for Auto build runs: a per-project Lima VM, or local Docker by opt-in.

Design: docs/specs/2026-10-21-auto-build-docker-design.md. Every limactl
call runs with LIMA_HOME=<DELIBRA_HOME>/lima, so the user's own ~/.lima
instances and _config overrides are never read or touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.agents.base import DockerAccess
from app.coding import DockerPolicy
from app.coding_env import server_environment
from app.config import Settings
from app.models import AutoBuildDocker
from app.storage import atomic_write_text, ensure_owned_directory, utc_now

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
PREPARE_TIMEOUT_SECONDS = 1_800
START_TIMEOUT_SECONDS = 180
STOP_TIMEOUT_SECONDS = 60
COMMAND_TIMEOUT_SECONDS = 30
SOCKET_WAIT_SECONDS = 30
FORWARD_SETTLE_SECONDS = 6.0
LOGGER = logging.getLogger(__name__)

Run = Callable[[list[str], dict[str, str], float], Awaitable[tuple[int, str, str]]]
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

    def access(self, endpoint: Path) -> DockerAccess:
        return DockerAccess(
            socket=endpoint,
            cli_read_roots=self.read_roots,
            plugin_dirs=self.plugin_dirs,
            bin_dir=self.executable.parent,
        )


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
    """A private DOCKER_CONFIG: plugin dirs only, never a credential helper.

    A config with no auth settings makes the Docker CLI pick the platform
    helper (macOS Keychain, which the sandbox blocks; spike S3, 2026-10-21).
    One entry under the reserved .invalid domain keeps credentials in this
    file, so public pulls stay anonymous.
    """

    config_dir.mkdir(mode=0o700)
    config = {
        "auths": {"delibra.invalid": {}},
        "cliPluginsExtraDirs": [path.as_posix() for path in plugin_dirs],
    }
    (config_dir / "config.json").write_text(json.dumps(config) + "\n", encoding="utf-8")


def require_user_socket(path: Path) -> None:
    try:
        info = os.stat(path)
    except OSError as exc:
        raise DockerUnavailable(f"{path} is unavailable: {exc.strerror}") from exc
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise DockerUnavailable(f"{path} is not a socket owned by this user")


async def run_command(argv: list[str], environ: dict[str, str], timeout: float) -> tuple[int, str, str]:
    """Fixed argv, no shell; 124 on timeout, 127 when it cannot start."""

    try:
        process = await asyncio.create_subprocess_exec(
            *argv, env=environ, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 127, "", f"{argv[0]}: {exc.strerror}"
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.CancelledError:
        process.kill()
        await process.communicate()
        raise
    except TimeoutError:
        process.kill()
        await process.communicate()
        return 124, "", f"{Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''} timed out after {timeout:g} s"
    return process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


@dataclass(frozen=True)
class LimaStatus:
    executable: str | None
    version: tuple[int, int, int] | None
    reason: str | None  # None: available

    @property
    def available(self) -> bool:
        return self.reason is None

    @property
    def closes_host_loopback(self) -> bool:
        return self.version is not None and self.version >= LIMA_HOST_LOOPBACK_VERSION

    @property
    def version_text(self) -> str | None:
        return dotted(self.version) if self.version is not None else None


def _failed(message: str, *, instance: str | None = None) -> DockerPolicy:
    return DockerPolicy(
        enabled=True, status="error", error=tail(message), lima_instance=instance, updated_at=utc_now()
    )


def _is_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _connects(host: str, port: int) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True


def _require_unwritten(marker: Path, writer: str) -> None:
    if marker.exists():
        marker.unlink()
        raise DockerUnavailable(f"{writer} could write the project share")


def _unescape_findmnt(value: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), value)


class DockerController:
    """Owns Delibra's Lima instances and checks the local Docker endpoint."""

    def __init__(
        self,
        settings: Settings,
        *,
        run: Run = run_command,
        environ: Callable[[], dict[str, str]] = server_environment,
        forward_settle_seconds: float = FORWARD_SETTLE_SECONDS,
    ) -> None:
        self.settings = settings
        self._run = run
        self._environ = environ
        self._settle = forward_settle_seconds
        # Instances a build run holds; startup recovery must not stop them.
        self._running: set[str] = set()
        # The run that last verified each instance; only it may stop it (design §7).
        self._owners: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def lima_home(self) -> Path:
        return self.settings.home / "lima"

    def _instance_lock(self, instance: str) -> asyncio.Lock:
        return self._locks.setdefault(instance, asyncio.Lock())

    def _lima_environ(self) -> dict[str, str]:
        self.settings.home.mkdir(parents=True, exist_ok=True)
        ensure_owned_directory(self.lima_home, self.settings.home)
        os.chmod(self.lima_home, 0o700)
        environ = self._environ()
        environ["LIMA_HOME"] = str(self.lima_home)
        return environ

    async def _limactl(self, status: LimaStatus, args: list[str], timeout: float) -> tuple[int, str, str]:
        assert status.executable is not None
        return await self._run([status.executable, *args], self._lima_environ(), timeout)

    async def lima_status(self) -> LimaStatus:
        environ = self._lima_environ()
        executable = shutil.which("limactl", path=environ.get("PATH"))
        if executable is None:
            return LimaStatus(None, None, "limactl is not on PATH")
        code, out, err = await self._run([executable, "--version"], environ, COMMAND_TIMEOUT_SECONDS)
        version = parse_lima_version(out or err) if code == 0 else None
        if version is None:
            return LimaStatus(executable, None, f"no stable Lima version in {(out or err).strip()[:200]!r}")
        if version < LIMA_MIN_VERSION:
            return LimaStatus(executable, version, f"Lima {dotted(version)} is older than 2.2.1")
        code, out, _err = await self._run([executable, "info"], environ, COMMAND_TIMEOUT_SECONDS)
        try:
            vm_types = json.loads(out).get("vmTypes", []) if code == 0 else []
        except (ValueError, AttributeError):
            vm_types = []
        if "vz" not in vm_types:
            return LimaStatus(executable, version, "Lima reports no vz VM type on this Mac")
        longest = socket_path(self.lima_home, INSTANCE_PREFIX + "0" * 10)
        if len(os.fsencode(longest)) > SUN_PATH_MAX:
            return LimaStatus(executable, version, f"{longest} is too long for a socket; use a shorter DELIBRA_HOME")
        return LimaStatus(executable, version, None)

    async def _instance_state(self, status: LimaStatus, instance: str) -> str | None:
        """Lima's status for the instance; None if absent, "Unknown" if unlisted."""

        items = await self._instances(status)
        if items is None:
            return "Unknown"
        for item in items:
            if item.get("name") == instance:
                return str(item.get("status"))
        return None

    async def _instances(self, status: LimaStatus) -> list[dict] | None:
        """Delibra's instances; None when `limactl list` itself failed."""

        code, out, _err = await self._limactl(status, ["list", "--json"], COMMAND_TIMEOUT_SECONDS)
        if code != 0:
            return None
        items = []
        for line in out.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and str(item.get("name", "")).startswith(INSTANCE_PREFIX):
                items.append(item)
        return items

    async def _create(self, status: LimaStatus, instance: str, project_root: Path) -> None:
        config_dir = ensure_owned_directory(self.settings.home / "lima-config", self.settings.home)
        config = config_dir / f"{instance}.yaml"
        atomic_write_text(config, lima_config(project_root, close_host_loopback=status.closes_host_loopback))
        code, _out, err = await self._limactl(
            status, ["create", "--tty=false", "--name", instance, str(config)], PREPARE_TIMEOUT_SECONDS
        )
        if code != 0:
            raise DockerUnavailable(f"limactl create failed: {tail(err)}")

    async def _reshare(self, status: LimaStatus, instance: str, project_root: Path) -> None:
        """Point the share at the project's current path (Rebind moves it)."""

        if await self._instance_state(status, instance) in ("Running", "Unknown"):
            if not await self._stop(status, instance):
                raise DockerUnavailable(self._stop_failed(instance))
        args = ["edit", "--tty=false", "--mount-only", project_root.as_posix()]
        if status.closes_host_loopback:
            args += ["--set", ".networkOpts.hostLoopback = false"]
        code, _out, err = await self._limactl(status, [*args, instance], COMMAND_TIMEOUT_SECONDS)
        if code != 0:
            raise DockerUnavailable(f"limactl edit failed: {tail(err)}")

    async def _start(self, status: LimaStatus, instance: str, timeout: float) -> None:
        code, _out, err = await self._limactl(status, ["start", "--tty=false", instance], timeout)
        self._running.add(instance)  # a half-started VM must still be stopped
        if code != 0:
            raise DockerUnavailable(f"limactl start {instance} failed: {tail(err)}")

    def _stop_failed(self, instance: str) -> str:
        return (
            f"the Docker VM {instance} did not stop and may still be running; stop it with "
            f"`LIMA_HOME={self.lima_home} limactl stop --force {instance}`"
        )

    async def _stop(self, status: LimaStatus, instance: str) -> bool:
        """Stop a VM. True only once `limactl list` shows it is not running.

        A VM that may still run stays in `_running`: prepare and delete then
        refuse it, and shutdown's stop_all(include_held=True) retries it.
        """

        code, _out, err = await self._limactl(status, ["stop", "--tty=false", instance], STOP_TIMEOUT_SECONDS)
        if code != 0:
            code, _out, err = await self._limactl(
                status, ["stop", "--tty=false", "--force", instance], STOP_TIMEOUT_SECONDS
            )
        if await self._instance_state(status, instance) in ("Running", "Unknown"):
            self._running.add(instance)
            LOGGER.error("Lima instance %s did not stop: %s", instance, tail(err))
            return False
        self._running.discard(instance)
        return True

    async def _docker_version(self, cli: HostDockerCli, endpoint: Path) -> str:
        deadline = time.monotonic() + SOCKET_WAIT_SECONDS
        while not _is_socket(endpoint):
            if time.monotonic() > deadline:
                raise DockerUnavailable(f"the Docker socket {endpoint} did not appear")
            await asyncio.sleep(1)
        code, out, err = await self._run(
            [str(cli.executable), "--host", f"unix://{endpoint.as_posix()}", "version", "--format", "{{.Server.Version}}"],
            self._environ(), COMMAND_TIMEOUT_SECONDS,
        )
        if code != 0 or not out.strip():
            raise DockerUnavailable(f"docker version through {endpoint} failed: {tail(err)}")
        return out.strip()[:100]

    async def _isolation_probes(self, status: LimaStatus, instance: str, project_root: Path) -> bool:
        """Design §4. Fails closed; returns whether host loopback is closed."""

        assert status.executable is not None
        shell = [status.executable, "shell", "--tty=false", "--workdir", "/", instance]
        environ = self._lima_environ()
        root = project_root.as_posix()
        await self._check_shares(shell, environ, root)
        marker = f".delibra-ro-probe-{secrets.token_hex(8)}"
        target = f"{root}/{marker}"
        # The files' owner, whom rootless containers run as, meets the guest's
        # read-only mount (ro diagnostic on Lima 2.2.1, 2026-10-21).
        code, _out, err = await self._run([*shell, "touch", target], environ, COMMAND_TIMEOUT_SECONDS)
        _require_unwritten(project_root / marker, "the guest user")
        if code == 0 or marker not in err or "Read-only file system" not in err:
            raise DockerUnavailable(f"the read-only probe did not run as expected: {tail(err)}")
        # Root can remount the share writable inside the guest, but VZ refuses
        # the write on the host side (Permission denied); the guest's read-only
        # mount is then restored, so containers keep a clear EROFS.
        script = (
            f"mount -o remount,rw {shlex.quote(root)} 2>/dev/null; touch {shlex.quote(target)}; "
            f"status=$?; mount -o remount,ro {shlex.quote(root)}; exit $status"
        )
        code, _out, err = await self._run([*shell, "sudo", "sh", "-c", script], environ, COMMAND_TIMEOUT_SECONDS)
        _require_unwritten(project_root / marker, "root in the VM")
        if code == 0 or marker not in err or not any(
            text in err for text in ("Permission denied", "Read-only file system")
        ):
            raise DockerUnavailable(f"the root read-only probe did not run as expected: {tail(err)}")
        await self._check_shares(shell, environ, root)
        closed = await self._host_loopback_closed(
            shell, environ, expect_closed=status.closes_host_loopback
        )
        await self._no_guest_port_forwarding(shell, environ)
        return closed

    async def _check_shares(self, shell: list[str], environ: dict[str, str], root: str) -> None:
        code, out, _err = await self._run(
            [*shell, "findmnt", "-rn", "-t", "virtiofs,9p,fuse.sshfs", "-o", "TARGET,FSTYPE,OPTIONS"],
            environ, COMMAND_TIMEOUT_SECONDS,
        )
        rows = [line.split(" ") for line in out.splitlines() if line.strip()]
        if (
            code != 0 or len(rows) != 1 or len(rows[0]) != 3
            or _unescape_findmnt(rows[0][0]) != root or rows[0][1] != "virtiofs"
            or "ro" not in rows[0][2].split(",")
        ):
            raise DockerUnavailable(f"the VM must share only the project, read-only; it shares {out.strip()[:500]!r}")

    async def _host_loopback_closed(
        self, shell: list[str], environ: dict[str, str], *, expect_closed: bool
    ) -> bool:
        """Whether the guest is cut off from this machine's 127.0.0.1. Fails closed.

        Lima < 2.3 cannot close it (lima#5574): there only a reached listener
        is a working probe, and the answer is always "open". On >= 2.3,
        "closed" needs no hit on our listener and a refused or timed-out
        connect (design §4; narrow to one exit once S5 runs on stable 2.3).
        """

        hits: list[int] = []

        async def accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            hits.append(1)
            writer.close()

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            code, _out, err = await self._run(
                [*shell, "curl", "-s", "-m", "5", "-o", "/dev/null", f"http://host.lima.internal:{port}/"],
                environ, COMMAND_TIMEOUT_SECONDS,
            )
        finally:
            server.close()
            await server.wait_closed()
        # A hit, or a curl exit that means a TCP connection was made (0 ok,
        # 52 empty reply, 56 receive failure): the port is ours, so reached.
        reached = bool(hits) or code in (0, 52, 56)
        if not expect_closed:
            if reached:
                return False
            raise DockerUnavailable(
                f"the host-loopback probe did not reach its own listener (curl exit {code}): {tail(err)}"
            )
        if reached:
            raise DockerUnavailable(
                "Lima was told to close host loopback (hostLoopback: false), but the VM still reached it"
            )
        if code in (7, 28):  # connection refused / timed out, and nothing arrived
            return True
        raise DockerUnavailable(f"the host-loopback probe did not run (curl exit {code}): {tail(err)}")

    async def _no_guest_port_forwarding(self, shell: list[str], environ: dict[str, str]) -> None:
        port = _free_loopback_port()
        start = f"nohup python3 -m http.server {port} --bind 0.0.0.0 >/dev/null 2>&1 & echo $!"
        code, out, err = await self._run([*shell, "sh", "-c", start], environ, COMMAND_TIMEOUT_SECONDS)
        pid = out.strip()
        if code != 0 or not pid.isdigit():
            raise DockerUnavailable(f"the port-forwarding probe could not start a listener: {tail(err)}")
        try:
            await asyncio.sleep(self._settle)
            code, _out, _err = await self._run(
                [*shell, "curl", "-s", "-m", "3", "-o", "/dev/null", f"http://127.0.0.1:{port}/"],
                environ, COMMAND_TIMEOUT_SECONDS,
            )
            if code != 0:
                raise DockerUnavailable(f"the port-forwarding probe's listener is not serving (curl exit {code})")
            if await _connects("127.0.0.1", port):
                raise DockerUnavailable(f"a guest listener on port {port} was forwarded to this machine")
        finally:
            await self._run([*shell, "kill", pid], environ, COMMAND_TIMEOUT_SECONDS)

    async def prepare_lima(self, project_id: str, project_root: Path) -> DockerPolicy:
        """Create or re-share the VM, provision, probe, stop. Never raises."""

        instance = instance_name(project_id)
        status = await self.lima_status()
        if not status.available:
            return _failed(f"Lima is unavailable: {status.reason}")
        try:
            if ":" in project_root.as_posix():
                raise DockerUnavailable("the project path contains ':', which limactl cannot mount")
            cli = find_host_docker_cli(self._environ())
            async with self._instance_lock(instance):
                if instance in self._running:
                    raise DockerUnavailable("the VM is in use by a build run, or did not stop")
                if (self.lima_home / instance).is_dir():
                    await self._reshare(status, instance, project_root)
                else:
                    await self._create(status, instance, project_root)
                stopped = False
                try:
                    await self._start(status, instance, PREPARE_TIMEOUT_SECONDS)
                    version = await self._docker_version(cli, socket_path(self.lima_home, instance))
                    closed = await self._isolation_probes(status, instance, project_root)
                finally:
                    stopped = await self._stop(status, instance)
                if not stopped:
                    raise DockerUnavailable(self._stop_failed(instance))
        except DockerUnavailable as exc:
            return _failed(str(exc), instance=instance if (self.lima_home / instance).is_dir() else None)
        return DockerPolicy(
            enabled=True, backend="lima", status="ready", lima_instance=instance,
            lima_version=status.version_text, host_loopback_closed=closed,
            docker_version=version, updated_at=utc_now(),
        )

    async def acquire(
        self, project_id: str, project_root: Path, policy: DockerPolicy, *, owner: str
    ) -> tuple[DockerAccess, AutoBuildDocker]:
        """Bring the run's backend up and verify it (design §7, Acquire)."""

        cli = find_host_docker_cli(self._environ())
        if policy.backend == "local" and policy.local_endpoint is not None:
            endpoint = Path(policy.local_endpoint)
            require_user_socket(endpoint)
            await self._docker_version(cli, endpoint)
            return cli.access(endpoint), AutoBuildDocker(
                backend="local", instance=None, lima_version=None,
                host_loopback_closed=None, local_endpoint=endpoint.as_posix(),
            )
        if policy.backend != "lima" or policy.lima_instance is None:
            raise DockerUnavailable("Docker is not prepared for this project")
        instance = policy.lima_instance
        status = await self.lima_status()
        if not status.available:
            raise DockerUnavailable(f"Lima is unavailable: {status.reason}")
        async with self._instance_lock(instance):
            if not (self.lima_home / instance).is_dir():
                raise DockerUnavailable("the project's Lima VM is missing; press Retry Docker in Settings")
            await self._reshare(status, instance, project_root)
            endpoint = socket_path(self.lima_home, instance)
            try:
                await self._start(status, instance, START_TIMEOUT_SECONDS)
                await self._docker_version(cli, endpoint)
                closed = await self._isolation_probes(status, instance, project_root)
            except BaseException:
                await self._stop(status, instance)
                raise
            self._owners[instance] = owner
        return cli.access(endpoint), AutoBuildDocker(
            backend="lima", instance=instance, lima_version=status.version_text,
            host_loopback_closed=closed, local_endpoint=None,
        )

    async def release(self, docker: AutoBuildDocker, *, owner: str) -> bool:
        """Stop the run's VM; False if it may still be running. Never raises.

        Local Docker is never touched, and a VM a newer run has acquired
        since is not this run's to stop: both report True.
        """

        if docker.backend != "lima" or docker.instance is None:
            return True
        try:
            status = await self.lima_status()
            if status.executable is None:
                LOGGER.error("limactl is gone; Lima instance %s may still be running", docker.instance)
                return False
            async with self._instance_lock(docker.instance):
                if self._owners.get(docker.instance) != owner:
                    return True
                stopped = await self._stop(status, docker.instance)
                if stopped:
                    self._owners.pop(docker.instance, None)
                return stopped
        except Exception:
            LOGGER.exception("Could not stop Lima instance %s", docker.instance)
            return False

    async def adopt_local(self) -> DockerPolicy:
        """Resolve the current Docker context's unix socket (design §5)."""

        cli = find_host_docker_cli(self._environ())
        code, out, err = await self._run(
            [str(cli.executable), "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
            self._environ(), COMMAND_TIMEOUT_SECONDS,
        )
        if code != 0:
            raise DockerUnavailable(f"docker context inspect failed: {tail(err)}")
        try:
            host = json.loads(out)
        except ValueError:
            host = None
        if not isinstance(host, str) or not host.startswith("unix://"):
            raise DockerUnavailable(f"the current Docker context uses {host!r}; only a unix socket is supported")
        endpoint = Path(host.removeprefix("unix://")).resolve()
        require_user_socket(endpoint)
        version = await self._docker_version(cli, endpoint)
        now = utc_now()
        return DockerPolicy(
            enabled=True, backend="local", status="ready", local_endpoint=endpoint.as_posix(),
            local_acknowledged_at=now, docker_version=version, updated_at=now,
        )

    async def backend_refusal(self, policy: DockerPolicy) -> str | None:
        """Why a Docker build run cannot start or resume now; None if it can."""

        if not policy.enabled:
            return None
        if policy.status != "ready":
            return f"Docker is not ready for this project ({policy.status}); see Settings"
        try:
            find_host_docker_cli(self._environ())
            if policy.backend == "local":
                require_user_socket(Path(policy.local_endpoint or "/nonexistent"))
                return None
        except DockerUnavailable as exc:
            return f"Docker is unavailable: {exc}"
        status = await self.lima_status()
        if not status.available:
            return (
                f"Lima is unavailable ({status.reason}). This project uses the Lima backend, "
                "and Delibra will not fall back to local Docker; fix Lima, or choose local "
                "Docker in Settings."
            )
        if policy.lima_instance is None or not (self.lima_home / policy.lima_instance).is_dir():
            return "The project's Lima VM is missing; press Retry Docker in Settings"
        return None

    async def delete(self, instance: str) -> None:
        status = await self.lima_status()
        if status.executable is None:
            raise DockerUnavailable("limactl is not on PATH; the VM cannot be deleted")
        async with self._instance_lock(instance):
            if instance in self._running:
                raise DockerUnavailable("the VM is in use by a build run, or did not stop")
            code, _out, err = await self._limactl(
                status, ["delete", "--tty=false", "--force", instance], STOP_TIMEOUT_SECONDS
            )
        if code != 0 and (self.lima_home / instance).exists():
            raise DockerUnavailable(f"limactl delete failed: {tail(err)}")
        (self.settings.home / "lima-config" / f"{instance}.yaml").unlink(missing_ok=True)

    def existing_instance(self, project_id: str) -> str | None:
        """The project's VM name if its directory exists (it may hold a disk)."""

        name = instance_name(project_id)
        return name if (self.lima_home / name).is_dir() else None

    async def stop_all(self, *, include_held: bool = False) -> None:
        """Stop running Delibra VMs: at startup (unheld only) and shutdown (all)."""

        if not self.lima_home.is_dir():
            return
        try:
            status = await self.lima_status()
            if status.executable is None:
                return
            for item in await self._instances(status) or []:
                name = str(item["name"])
                if item.get("status") != "Running":
                    continue
                async with self._instance_lock(name):
                    if include_held or name not in self._running:
                        await self._stop(status, name)
        except Exception:
            LOGGER.exception("Could not stop Delibra's Lima instances")

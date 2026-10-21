from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from app.coding import DockerPolicy
from app.config import Settings
from app.models import AutoBuildDocker
from app.docker_backend import (
    DockerController,
    DockerUnavailable,
    find_host_docker_cli,
    instance_name,
    lima_config,
    parse_lima_version,
    require_user_socket,
    socket_path,
    write_cli_config,
)


def test_lima_versions_parse_only_stable_releases() -> None:
    assert parse_lima_version("limactl version 2.2.1\n") == (2, 2, 1)
    assert parse_lima_version("limactl version 2.3.0-beta.1") is None
    assert parse_lima_version("garbage") is None


def test_instance_names_are_stable_short_and_prefixed() -> None:
    name = instance_name("a" * 32)
    assert name == instance_name("a" * 32)
    assert re.fullmatch(r"delibra-[0-9a-f]{10}", name)
    assert name != instance_name("b" * 32)


def test_lima_config_shares_only_the_project_read_only() -> None:
    text = lima_config(Path("/Users/me/my project"), close_host_loopback=False)
    config = json.loads(text)
    assert config["vmType"] == "vz"
    assert config["base"] == ["template:_images/ubuntu-lts"]
    assert config["mounts"] == [{"location": "/Users/me/my project", "writable": False}]
    assert config["ssh"] == {"forwardAgent": False, "loadDotSSHPubKeys": False, "forwardX11": False}
    assert config["propagateProxyEnv"] is False
    assert config["portForwards"][0] == {
        "guestSocket": "/run/user/{{.UID}}/docker.sock",
        "hostSocket": "{{.Dir}}/sock/docker.sock",
    }
    assert all(rule["ignore"] is True for rule in config["portForwards"][1:])
    assert "networkOpts" not in config
    assert "get.docker.com" not in text
    assert "download.docker.com" in text


def test_lima_config_closes_host_loopback_when_lima_can() -> None:
    config = json.loads(lima_config(Path("/p"), close_host_loopback=True))
    assert config["networkOpts"] == {"hostLoopback": False}


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_host_docker_cli_resolves_desktop_style_symlinks(tmp_path) -> None:
    real = _executable(tmp_path / "Docker.app" / "bin" / "docker")
    plugins = tmp_path / "Docker.app" / "cli-plugins"
    home = tmp_path / "home"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "docker").symlink_to(real)
    (home / ".docker" / "cli-plugins").mkdir(parents=True)
    for name in ("docker-compose", "docker-buildx"):
        _executable(plugins / name)
        (home / ".docker" / "cli-plugins" / name).symlink_to(plugins / name)

    cli = find_host_docker_cli(
        {"PATH": str(tmp_path / "bin"), "HOME": str(home)}, system_plugin_dirs=()
    )

    assert cli.executable == real.resolve()
    assert cli.plugin_dirs == (plugins.resolve(),)
    assert cli.read_roots == (real.resolve().parent, plugins.resolve())
    access = cli.access(Path("/s.sock"))
    assert (access.socket, access.plugin_dirs) == (Path("/s.sock"), (plugins.resolve(),))
    # Codex cannot read /usr/local/bin, so the real directory joins PATH (S4).
    assert access.bin_dir == real.resolve().parent


def test_host_docker_cli_requires_docker_and_compose(tmp_path) -> None:
    _executable(tmp_path / "bin" / "docker")
    with pytest.raises(DockerUnavailable, match="compose"):
        find_host_docker_cli({"PATH": str(tmp_path / "bin"), "HOME": str(tmp_path)}, system_plugin_dirs=())
    with pytest.raises(DockerUnavailable, match="docker CLI"):
        find_host_docker_cli({"PATH": str(tmp_path / "none"), "HOME": str(tmp_path)}, system_plugin_dirs=())


def test_cli_config_lists_only_the_plugin_dirs(tmp_path) -> None:
    write_cli_config(tmp_path / "docker", (Path("/a"), Path("/b")))
    # An auths entry stops the CLI picking the Keychain helper (S3, 2026-10-21).
    assert json.loads((tmp_path / "docker" / "config.json").read_text()) == {
        "auths": {"delibra.invalid": {}},
        "cliPluginsExtraDirs": ["/a", "/b"],
    }
    assert (tmp_path / "docker").stat().st_mode & 0o777 == 0o700


def test_only_a_users_own_socket_is_accepted(tmp_path) -> None:
    with pytest.raises(DockerUnavailable, match="unavailable"):
        require_user_socket(tmp_path / "missing.sock")
    (tmp_path / "file").write_text("x")
    with pytest.raises(DockerUnavailable, match="not a socket"):
        require_user_socket(tmp_path / "file")
    path = Path("/tmp") / f"dlb-{id(tmp_path)}.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    try:
        require_user_socket(path)
    finally:
        listener.close()
        path.unlink()


def test_lima_version_parsing_survives_arbitrary_text() -> None:
    # Generated inputs (TDD rule 3: the parser reads limactl output).
    import random

    rng = random.Random(20261021)
    alphabet = "0123456789.-+ limactlversionbeta\n"
    for _ in range(5_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 32)))
        parsed = parse_lima_version(text)
        assert parsed is None or (len(parsed) == 3 and text.strip().startswith("limactl version "))


def bind_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.close()


class FakeLima:
    """Answers limactl/docker argv like Lima 2.x, without a VM."""

    def __init__(self, home: Path, project: Path, *, version: str = "2.2.1", vm_types=("vz",)) -> None:
        self.home, self.project, self.version, self.vm_types = home, project, version, vm_types
        self.calls: list[list[str]] = []
        self.environs: list[dict[str, str]] = []
        self.instances: dict[str, str] = {}
        self.configs: dict[str, dict] = {}
        self.findmnt = f"{project} virtiofs ro,relatime\n"
        self.findmnt_after: str | None = None  # what findmnt says once probed
        self.findmnt_calls = 0
        # Measured on Lima 2.2.1 (ro diagnostic, 2026-10-21): the owner meets the
        # guest's ro mount; root after a remount meets VZ's host-side refusal.
        self.owner_error = "Read-only file system"
        self.root_error = "Permission denied"
        self.root_writes = False
        self.loopback_reachable = True  # Lima 2.2.1 reaches host loopback (S5, 2026-10-21)
        self.loopback_exit = 7  # curl exit when it does not
        self.forwarded = False
        self.start_code = 0
        self.stop_code = 0
        self.listener = None

    async def __call__(self, argv, environ, timeout):
        self.calls.append(list(argv))
        self.environs.append(dict(environ))
        if Path(argv[0]).name == "docker":
            return 0, "29.0.0\n", ""
        command = argv[1]
        if command == "--version":
            return 0, f"limactl version {self.version}\n", ""
        if command == "info":
            return 0, json.dumps({"vmTypes": list(self.vm_types)}), ""
        if command == "list":
            return 0, "".join(json.dumps({"name": n, "status": s}) + "\n" for n, s in self.instances.items()), ""
        if command == "create":
            name = argv[argv.index("--name") + 1]
            (self.home / "lima" / name).mkdir(parents=True)
            self.instances[name] = "Stopped"
            self.configs[name] = json.loads(Path(argv[-1]).read_text())
            return 0, "", ""
        if command == "edit":
            return 0, "", ""
        if command == "start":
            if self.start_code:
                self.instances[argv[-1]] = "Running"
                return self.start_code, "", "boot failed"
            bind_socket(self.home / "lima" / argv[-1] / "sock" / "docker.sock")
            self.instances[argv[-1]] = "Running"
            return 0, "", ""
        if command == "stop":
            if self.stop_code:
                return self.stop_code, "", "stop timed out"
            self.instances[argv[-1]] = "Stopped"
            return 0, "", ""
        if command == "delete":
            shutil.rmtree(self.home / "lima" / argv[-1])
            self.instances.pop(argv[-1], None)
            return 0, "", ""
        if command == "shell":
            return await self.guest(argv[argv.index("--workdir") + 3:])
        raise AssertionError(argv)

    async def guest(self, guest: list[str]):
        if guest[0] == "findmnt":
            self.findmnt_calls += 1
            if self.findmnt_calls > 1 and self.findmnt_after is not None:
                return 0, self.findmnt_after, ""
            return 0, self.findmnt, ""
        if guest[0] == "touch":
            return 1, "", f"touch: cannot touch '{guest[1]}': {self.owner_error}"
        if guest[0] == "sudo":
            target = re.search(r"touch (\S+);", guest[3]).group(1)
            if self.root_writes:
                Path(target).touch()
                return 0, "", ""
            return 1, "", f"touch: cannot touch '{target}': {self.root_error}"
        if guest[0] == "curl" and "host.lima.internal" in guest[-1]:
            if not self.loopback_reachable:
                return self.loopback_exit, "", ""
            port = int(guest[-1].rstrip("/").rsplit(":", 1)[1])
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return 52, "", ""
        if guest[:2] == ["sh", "-c"]:
            port = int(guest[2].split("http.server ")[1].split()[0])
            if self.forwarded:
                self.listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
            return 0, "4242\n", ""
        if guest[0] == "curl":
            return 0, "", ""
        if guest[0] == "kill":
            if self.listener is not None:
                self.listener.close()
                await self.listener.wait_closed()
            return 0, "", ""
        raise AssertionError(guest)


@pytest.fixture
def short_home():
    # Unix socket paths must fit in 103 bytes; pytest's tmp_path does not.
    path = Path(tempfile.mkdtemp(prefix="dlb-", dir="/tmp")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def controller_for(home: Path, *, version: str = "2.2.1", vm_types=("vz",), limactl: bool = True):
    project = home / "project"
    project.mkdir(exist_ok=True)
    bin_dir = home / "bin"
    _executable(bin_dir / "docker")
    if limactl:
        _executable(bin_dir / "limactl")
    _executable(home / "user" / ".docker" / "cli-plugins" / "docker-compose")
    fake = FakeLima(home, project, version=version, vm_types=vm_types)
    environ = {"PATH": str(bin_dir), "HOME": str(home / "user")}
    controller = DockerController(
        Settings(home=home), run=fake, environ=lambda: dict(environ), forward_settle_seconds=0
    )
    return controller, fake, project


@pytest.mark.asyncio
async def test_prepare_creates_the_vm_in_delibras_own_lima_home(short_home) -> None:
    controller, fake, project = controller_for(short_home)

    policy = await controller.prepare_lima("a" * 32, project)

    name = instance_name("a" * 32)
    assert (policy.status, policy.backend, policy.lima_instance) == ("ready", "lima", name)
    assert (policy.lima_version, policy.docker_version, policy.host_loopback_closed) == ("2.2.1", "29.0.0", False)
    assert fake.instances == {name: "Stopped"}
    assert fake.configs[name]["mounts"] == [{"location": str(project), "writable": False}]
    assert "networkOpts" not in fake.configs[name]
    lima_homes = {env.get("LIMA_HOME") for argv, env in zip(fake.calls, fake.environs) if Path(argv[0]).name == "limactl"}
    assert lima_homes == {str(short_home / "lima")}
    assert (short_home / "lima").stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_lima_2_2_records_that_host_loopback_is_open(short_home) -> None:
    controller, fake, project = controller_for(short_home)
    fake.loopback_reachable = True

    policy = await controller.prepare_lima("a" * 32, project)

    assert (policy.status, policy.host_loopback_closed) == ("ready", False)


@pytest.mark.asyncio
async def test_lima_2_3_closes_host_loopback_or_refuses(short_home) -> None:
    controller, fake, project = controller_for(short_home, version="2.3.0")
    fake.loopback_reachable = True

    policy = await controller.prepare_lima("a" * 32, project)

    assert fake.configs[instance_name("a" * 32)]["networkOpts"] == {"hostLoopback": False}
    assert policy.status == "error"
    assert "still reached" in (policy.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("curl_exit", [7, 28])
async def test_lima_2_2_never_records_host_loopback_closed(short_home, curl_exit) -> None:
    controller, fake, project = controller_for(short_home)
    fake.loopback_reachable = False
    fake.loopback_exit = curl_exit

    policy = await controller.prepare_lima("a" * 32, project)

    assert policy.status == "error"
    assert policy.host_loopback_closed is None
    assert "did not reach its own listener" in (policy.error or "")


@pytest.mark.asyncio
async def test_lima_2_3_records_host_loopback_closed_when_nothing_arrives(short_home) -> None:
    controller, fake, project = controller_for(short_home, version="2.3.0")
    fake.loopback_reachable = False

    policy = await controller.prepare_lima("a" * 32, project)

    assert (policy.status, policy.host_loopback_closed) == ("ready", True)


@pytest.mark.asyncio
async def test_lima_2_3_loopback_probe_that_did_not_run_fails_closed(short_home) -> None:
    controller, fake, project = controller_for(short_home, version="2.3.0")
    fake.loopback_reachable = False
    fake.loopback_exit = 6  # curl could not resolve host.lima.internal

    policy = await controller.prepare_lima("a" * 32, project)

    assert policy.status == "error"
    assert "did not run" in (policy.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "vm_types", "limactl", "reason"),
    [
        ("2.2.0", ("vz",), True, "older than 2.2.1"),
        ("2.3.0-beta.1", ("vz",), True, "stable Lima version"),
        ("2.2.1", ("qemu",), True, "no vz"),
        ("2.2.1", ("vz",), False, "not on PATH"),
    ],
)
async def test_unavailable_lima_is_an_error_never_a_fallback(short_home, version, vm_types, limactl, reason) -> None:
    controller, fake, project = controller_for(short_home, version=version, vm_types=vm_types, limactl=limactl)

    policy = await controller.prepare_lima("a" * 32, project)

    assert (policy.status, policy.backend) == ("error", None)
    assert reason in (policy.error or "")
    assert fake.instances == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "breach",
    ["home_share", "writable_share", "owner_not_erofs", "root_writes", "ro_not_restored", "forwarded"],
)
async def test_a_failed_probe_fails_closed_and_stops_the_vm(short_home, breach) -> None:
    controller, fake, project = controller_for(short_home)
    if breach == "home_share":
        fake.findmnt += f"{short_home / 'user'} virtiofs ro,relatime\n"
    elif breach == "writable_share":
        fake.findmnt = f"{project} virtiofs rw,relatime\n"
    elif breach == "owner_not_erofs":
        fake.owner_error = "Permission denied"
    elif breach == "root_writes":
        fake.root_writes = True
    elif breach == "ro_not_restored":
        fake.findmnt_after = f"{project} virtiofs rw,relatime\n"
    else:
        fake.forwarded = True

    policy = await controller.prepare_lima("a" * 32, project)

    assert policy.status == "error"
    assert policy.lima_instance == instance_name("a" * 32)  # Delete VM stays possible
    assert set(fake.instances.values()) == {"Stopped"}
    assert not list(project.glob(".delibra-ro-probe-*"))  # a written marker is removed


@pytest.mark.asyncio
async def test_acquire_reshares_starts_probes_and_release_stops(short_home) -> None:
    controller, fake, project = controller_for(short_home)
    policy = await controller.prepare_lima("a" * 32, project)
    fake.calls.clear()

    access, state = await controller.acquire("a" * 32, project, policy)

    name = instance_name("a" * 32)
    assert access.socket == socket_path(short_home / "lima", name)
    assert (state.backend, state.instance, state.lima_version) == ("lima", name, "2.2.1")
    assert ["edit", "--tty=false", "--mount-only", str(project), name] in [
        call[1:] for call in fake.calls
    ]
    assert fake.instances[name] == "Running"

    assert await controller.release(state) is True

    assert fake.instances[name] == "Stopped"


@pytest.mark.asyncio
async def test_a_vm_that_will_not_stop_stays_held_and_is_reported(short_home) -> None:
    controller, fake, project = controller_for(short_home)
    policy = await controller.prepare_lima("a" * 32, project)
    _access, state = await controller.acquire("a" * 32, project, policy)
    name = instance_name("a" * 32)
    fake.stop_code = 1  # both `stop` and `stop --force` fail; list still says Running

    assert await controller.release(state) is False
    assert fake.instances[name] == "Running"
    failed = await controller.prepare_lima("a" * 32, project)
    assert failed.status == "error" and "did not stop" in (failed.error or "")

    fake.stop_code = 0
    await controller.stop_all(include_held=True)
    assert fake.instances[name] == "Stopped"


@pytest.mark.asyncio
async def test_a_failed_start_stops_the_vm_and_refuses(short_home) -> None:
    controller, fake, project = controller_for(short_home)
    policy = await controller.prepare_lima("a" * 32, project)
    fake.start_code = 1

    with pytest.raises(DockerUnavailable, match="boot failed"):
        await controller.acquire("a" * 32, project, policy)

    assert fake.instances[instance_name("a" * 32)] == "Stopped"


@pytest.mark.asyncio
async def test_acquire_refuses_a_missing_vm(short_home) -> None:
    controller, _fake, project = controller_for(short_home)
    policy = DockerPolicy(enabled=True, backend="lima", status="ready", lima_instance=instance_name("a" * 32))

    with pytest.raises(DockerUnavailable, match="VM is missing"):
        await controller.acquire("a" * 32, project, policy)


@pytest.mark.asyncio
async def test_stop_all_skips_a_vm_a_run_holds_unless_shutting_down(short_home) -> None:
    controller, fake, project = controller_for(short_home)
    policy = await controller.prepare_lima("a" * 32, project)
    await controller.acquire("a" * 32, project, policy)
    stale = "delibra-ffffffffff"
    fake.instances[stale] = "Running"

    await controller.stop_all()

    assert fake.instances == {instance_name("a" * 32): "Running", stale: "Stopped"}
    await controller.stop_all(include_held=True)
    assert set(fake.instances.values()) == {"Stopped"}


@pytest.mark.asyncio
async def test_stop_all_without_a_lima_home_runs_nothing(short_home) -> None:
    controller, fake, _project = controller_for(short_home)

    await controller.stop_all()

    assert fake.calls == []


@pytest.mark.asyncio
async def test_adopting_local_docker_needs_a_users_unix_socket(short_home) -> None:
    controller, fake, _project = controller_for(short_home)
    endpoint = short_home / "desktop.sock"
    bind_socket(endpoint)
    inspect = {"answer": json.dumps(f"unix://{endpoint}")}
    original = fake.__call__

    async def run(argv, environ, timeout):
        if argv[1:3] == ["context", "inspect"]:
            return 0, inspect["answer"], ""
        return await original(argv, environ, timeout)

    controller._run = run
    policy = await controller.adopt_local()
    assert (policy.status, policy.backend, policy.local_endpoint) == ("ready", "local", str(endpoint))
    assert policy.local_acknowledged_at is not None

    inspect["answer"] = json.dumps("tcp://192.168.1.9:2375")
    with pytest.raises(DockerUnavailable, match="only a unix socket"):
        await controller.adopt_local()


@pytest.mark.asyncio
async def test_backend_refusal_never_falls_back_to_local(short_home) -> None:
    controller, _fake, project = controller_for(short_home, limactl=False)
    policy = DockerPolicy(enabled=True, backend="lima", status="ready", lima_instance=instance_name("a" * 32))

    refusal = await controller.backend_refusal(policy)

    assert refusal is not None and "will not fall back to local Docker" in refusal
    assert await controller.backend_refusal(DockerPolicy()) is None
    assert "not ready" in (await controller.backend_refusal(DockerPolicy(enabled=True, status="error")) or "")


@pytest.mark.asyncio
async def test_delete_removes_the_vm_and_refuses_a_held_one(short_home) -> None:
    controller, fake, project = controller_for(short_home)
    policy = await controller.prepare_lima("a" * 32, project)
    await controller.acquire("a" * 32, project, policy)
    name = instance_name("a" * 32)

    with pytest.raises(DockerUnavailable, match="in use"):
        await controller.delete(name)
    fake.stop_code = 1  # a VM that will not stop stays held: delete refuses it
    assert await controller.release(AutoBuildDocker("lima", name, "2.2.1", False, None)) is False
    with pytest.raises(DockerUnavailable, match="did not stop"):
        await controller.delete(name)
    fake.stop_code = 0
    await controller.stop_all(include_held=True)
    await controller.delete(name)

    assert not (short_home / "lima" / name).exists()
    assert name not in fake.instances


@pytest.mark.asyncio
async def test_root_may_be_refused_with_either_host_or_guest_error(short_home) -> None:
    controller, fake, project = controller_for(short_home)
    fake.root_error = "Read-only file system"  # remount refused: the guest's ro still holds

    policy = await controller.prepare_lima("a" * 32, project)

    assert policy.status == "ready"

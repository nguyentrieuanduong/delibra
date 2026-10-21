from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import pytest

from app.docker_backend import (
    DockerUnavailable,
    find_host_docker_cli,
    instance_name,
    lima_config,
    parse_lima_version,
    require_user_socket,
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


def test_host_docker_cli_requires_docker_and_compose(tmp_path) -> None:
    _executable(tmp_path / "bin" / "docker")
    with pytest.raises(DockerUnavailable, match="compose"):
        find_host_docker_cli({"PATH": str(tmp_path / "bin"), "HOME": str(tmp_path)}, system_plugin_dirs=())
    with pytest.raises(DockerUnavailable, match="docker CLI"):
        find_host_docker_cli({"PATH": str(tmp_path / "none"), "HOME": str(tmp_path)}, system_plugin_dirs=())


def test_cli_config_lists_only_the_plugin_dirs(tmp_path) -> None:
    write_cli_config(tmp_path / "docker", (Path("/a"), Path("/b")))
    assert json.loads((tmp_path / "docker" / "config.json").read_text()) == {
        "cliPluginsExtraDirs": ["/a", "/b"]
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

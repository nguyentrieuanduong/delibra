"""Per-project coding mode: policy, host validation, build capability."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import stat
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOCAL_ONLY_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain")
DEFAULT_NETWORK_ALLOWLIST = (
    "pypi.org",
    "files.pythonhosted.org",
    "conda.anaconda.org",
    "repo.anaconda.com",
)
MAX_ALLOWLIST_HOSTS = 64
ENV_STATUSES = frozenset({"absent", "creating", "ready", "error"})
ENV_TOOLS = frozenset({"conda", "venv", "existing"})
# CLI versions that passed spikes S1 (claude) and S2 (codex), as
# app/health.py reports them (spike/FINDINGS.md, 2026-10-16). Build runs
# accept these or a newer release of the same major (user decision,
# 2026-10-21); a new major, a downgrade or a pre-release is refused until
# the spikes are re-run against it.
BUILD_VERIFIED_VERSIONS: dict[str, str] = {
    "claude": "2.1.296 (Claude Code)",
    "codex": "codex-cli 0.162.1",
}
_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
# CLI versions whose build sandbox reached the Docker socket, and only that
# socket, in spikes S3 (claude) and S4 (codex). Empty until they pass: a
# Docker build run is refused for any provider missing here. Same rule as
# BUILD_VERIFIED_VERSIONS: these or a newer release of the same major.
DOCKER_VERIFIED_VERSIONS: dict[str, str] = {
    # spike/FINDINGS.md, Docker spikes S3 and S4, 2026-10-21.
    "claude": "2.1.296 (Claude Code)",
    "codex": "codex-cli 0.163.0",
}
DOCKER_STATUSES = frozenset({"absent", "preparing", "ready", "error", "deleting"})
DOCKER_BACKENDS = frozenset({"lima", "local"})
LIMA_INSTANCE = re.compile(r"delibra-[0-9a-f]{10}")


class CodingPolicyError(ValueError):
    pass


GUIDANCE_MAX_FILES = 8
GUIDANCE_MAX_BYTES = 65_536
_CODE_CRAFT = "/Users/apollo/projects/code-craft/skills"
# Implementation-oriented skills only: brainstorming, writing-plans and
# executing-plans gate on user approval and would stall a build run.
CODE_CRAFT_SUGGESTED_GUIDANCE = tuple(
    f"{_CODE_CRAFT}/{name}/SKILL.md"
    for name in (
        "test-driven-development",
        "coding-standards",
        "security-baseline",
        "systematic-debugging",
        "verification-before-completion",
    )
)


def normalize_guidance_paths(values: object) -> list[str]:
    if not isinstance(values, list) or any(type(item) is not str for item in values):
        raise CodingPolicyError("guidance files must be a list of paths")
    paths: list[str] = []
    total = 0
    for raw in values:
        value = raw.strip()
        path = Path(value)
        if not path.is_absolute() or path.suffix.lower() != ".md":
            raise CodingPolicyError(f"{raw!r} must be an absolute path to a .md file")
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise CodingPolicyError(f"{raw!r} does not exist") from exc
        if not stat.S_ISREG(info.st_mode):
            raise CodingPolicyError(f"{raw!r} must be a regular file, not a link")
        if value in paths:
            raise CodingPolicyError(f"{raw!r} is listed twice")
        total += info.st_size
        paths.append(value)
    if len(paths) > GUIDANCE_MAX_FILES:
        raise CodingPolicyError(f"at most {GUIDANCE_MAX_FILES} guidance files")
    if total > GUIDANCE_MAX_BYTES:
        raise CodingPolicyError(f"guidance files exceed {GUIDANCE_MAX_BYTES} bytes")
    return paths


GIT_EXCLUDE_MAX_LINES = 2_000
GIT_EXCLUDE_MAX_BYTES = 65_536
# Delibra's managed-block markers in .git/info/exclude (app/build_git.py).
GIT_EXCLUDE_RESERVED = ("# >>> Delibra coding mode", "# <<< Delibra coding mode")


def normalize_git_exclude(values: object) -> list[str]:
    """A project's own ignore list for `.git/info/exclude`, kept verbatim.

    Git reads one pattern per line, so any text but a control character is
    a valid line. Delibra's block markers are refused: they delimit the
    block these lines are written into.
    """

    if not isinstance(values, list) or any(type(item) is not str for item in values):
        raise CodingPolicyError("git ignore list must be a list of lines")
    if len(values) > GIT_EXCLUDE_MAX_LINES:
        raise CodingPolicyError(f"git ignore list must have at most {GIT_EXCLUDE_MAX_LINES} lines")
    if sum(len(line.encode("utf-8")) + 1 for line in values) > GIT_EXCLUDE_MAX_BYTES:
        raise CodingPolicyError(f"git ignore list must be at most {GIT_EXCLUDE_MAX_BYTES} bytes")
    for line in values:
        if any((ord(character) < 32 and character != "\t") or ord(character) == 127 for character in line):
            raise CodingPolicyError(f"git ignore line {line!r} contains a control character")
        if line.startswith(GIT_EXCLUDE_RESERVED):
            raise CodingPolicyError(f"git ignore line {line!r} is reserved for Delibra")
    return list(values)



def read_guidance(paths: list[str]) -> bytes:
    """Read a bounded snapshot from regular files at run creation."""

    normalize_guidance_paths(paths)
    parts: list[bytes] = []
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise CodingPolicyError(f"{path!r} is not a regular file")
                body = source.read(GUIDANCE_MAX_BYTES + 1)
        except OSError as exc:
            raise CodingPolicyError(f"{path!r} changed or is unavailable") from exc
        parts.append(f"### {path}\n\n".encode() + body.rstrip() + b"\n")
    snapshot = b"\n".join(parts)
    if len(snapshot) > GUIDANCE_MAX_BYTES:
        raise CodingPolicyError(f"guidance snapshot exceeds {GUIDANCE_MAX_BYTES} bytes")
    return snapshot


def normalize_network_allowlist(values: object) -> list[str]:
    if not isinstance(values, list) or any(type(item) is not str for item in values):
        raise CodingPolicyError("network allowlist must be a list of host names")
    hosts: list[str] = []
    for raw in values:
        host = raw.strip().lower()
        if not host:
            raise CodingPolicyError("network allowlist contains an empty entry")
        if any(character in host for character in "/:*@?#[]\\ "):
            raise CodingPolicyError(
                f"{raw!r} must be a bare host name without scheme, port, "
                "path, or wildcard"
            )
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise CodingPolicyError(f"{raw!r} is an IP literal; list host names")
        if host == "localhost" or host.endswith(".localhost"):
            raise CodingPolicyError(f"{raw!r} names this machine")
        if any(host.endswith(suffix) for suffix in LOCAL_ONLY_SUFFIXES):
            raise CodingPolicyError(f"{raw!r} is a local-network name")
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or labels[-1].isdigit()
            or not all(_LABEL.fullmatch(label) for label in labels)
        ):
            raise CodingPolicyError(f"{raw!r} is not a valid host name")
        if host in hosts:
            raise CodingPolicyError(f"{raw!r} is listed twice")
        hosts.append(host)
    if len(hosts) > MAX_ALLOWLIST_HOSTS:
        raise CodingPolicyError(
            f"network allowlist must contain at most {MAX_ALLOWLIST_HOSTS} hosts"
        )
    return hosts


def _system_resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def private_resolution_warnings(
    hosts: Iterable[str],
    *,
    resolve: Callable[[str], list[str]] = _system_resolve,
) -> list[str]:
    """Warn, never reject: the user decided a listed host is trusted (D§1).

    A failed lookup is not a warning; the build turn simply cannot connect.
    """

    warnings: list[str] = []
    for host in hosts:
        try:
            addresses = resolve(host)
        except OSError:
            continue
        private = sorted(
            {
                address
                for address in addresses
                if (parsed := ipaddress.ip_address(address.split("%", 1)[0])).is_private
                or parsed.is_loopback
                or parsed.is_link_local
            }
        )
        if private:
            warnings.append(
                f"{host} resolves to a private address ({', '.join(private)})"
            )
    return warnings


@dataclass
class CodingEnvState:
    status: str = "absent"
    tool: str | None = None
    python: str | None = None
    error: str | None = None
    created_by_delibra: bool = False
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CodingEnvState":
        if not isinstance(data, dict):
            raise CodingPolicyError("coding environment is invalid")
        for key in ("tool", "python", "error", "updated_at"):
            value = data.get(key)
            if value is not None and type(value) is not str:
                raise CodingPolicyError(f"coding environment {key} is invalid")
        if type(data.get("created_by_delibra", False)) is not bool:
            raise CodingPolicyError("coding environment ownership is invalid")
        state = cls(
            status=str(data.get("status", "absent")),
            tool=data.get("tool"),
            python=data.get("python"),
            error=data.get("error"),
            created_by_delibra=data.get("created_by_delibra", False),
            updated_at=data.get("updated_at"),
        )
        if type(data.get("status", "absent")) is not str or state.status not in ENV_STATUSES:
            raise CodingPolicyError("coding environment status is invalid")
        if state.tool is not None and state.tool not in ENV_TOOLS:
            raise CodingPolicyError("coding environment tool is invalid")
        return state

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DockerPolicy:
    """A project's Docker backend for build runs (design §1)."""

    enabled: bool = False
    backend: str | None = None
    status: str = "absent"
    error: str | None = None
    lima_instance: str | None = None
    lima_version: str | None = None
    host_loopback_closed: bool | None = None
    local_endpoint: str | None = None
    local_acknowledged_at: str | None = None
    docker_version: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> "DockerPolicy":
        if not isinstance(data, dict) or type(data.get("enabled", False)) is not bool:
            raise CodingPolicyError("docker settings are invalid")
        for key in (
            "backend", "error", "lima_instance", "lima_version", "local_endpoint",
            "local_acknowledged_at", "docker_version", "updated_at",
        ):
            value = data.get(key)
            if value is not None and type(value) is not str:
                raise CodingPolicyError(f"docker {key} is invalid")
        closed = data.get("host_loopback_closed")
        if closed is not None and type(closed) is not bool:
            raise CodingPolicyError("docker host_loopback_closed is invalid")
        status = data.get("status", "absent")
        if type(status) is not str or status not in DOCKER_STATUSES:
            raise CodingPolicyError("docker status is invalid")
        backend = data.get("backend")
        if backend is not None and backend not in DOCKER_BACKENDS:
            raise CodingPolicyError("docker backend is invalid")
        # The instance name reaches limactl argv: only Delibra's own shape.
        instance = data.get("lima_instance")
        if instance is not None and not LIMA_INSTANCE.fullmatch(instance):
            raise CodingPolicyError("docker lima_instance is invalid")
        endpoint = data.get("local_endpoint")
        if endpoint is not None and not Path(endpoint).is_absolute():
            raise CodingPolicyError("docker local_endpoint is invalid")
        if status == "ready" and (
            backend is None
            or (backend == "lima" and instance is None)
            or (backend == "local" and endpoint is None)
        ):
            raise CodingPolicyError("docker ready state is incomplete")
        if status == "deleting" and instance is None:
            raise CodingPolicyError("docker deleting state names no VM")
        return cls(
            enabled=data.get("enabled", False),
            backend=backend,
            status=status,
            error=data.get("error"),
            lima_instance=instance,
            lima_version=data.get("lima_version"),
            host_loopback_closed=closed,
            local_endpoint=endpoint,
            local_acknowledged_at=data.get("local_acknowledged_at"),
            docker_version=data.get("docker_version"),
            updated_at=data.get("updated_at"),
        )

    @classmethod
    def failed(
        cls, message: str, *, instance: str | None, updated_at: str, enabled: bool = True
    ) -> "DockerPolicy":
        """A project whose Docker preparation or deletion failed; the VM name
        stays so Delete Docker VM can still reach its disk."""

        return cls(enabled=enabled, status="error", error=message, lima_instance=instance, updated_at=updated_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CodingPolicy:
    enabled: bool = False
    network_allowlist: list[str] = field(
        default_factory=lambda: list(DEFAULT_NETWORK_ALLOWLIST)
    )
    env: CodingEnvState = field(default_factory=CodingEnvState)
    network_warnings: list[str] = field(default_factory=list)
    guidance_files: list[str] = field(default_factory=list)
    # None follows Delibra's default list (app/build_git_exclude.txt).
    git_exclude: list[str] | None = None
    docker: DockerPolicy = field(default_factory=DockerPolicy)

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> "CodingPolicy":
        data = manifest.get("coding")
        if data is None:
            return cls()
        if not isinstance(data, dict) or type(data.get("enabled", False)) is not bool:
            raise CodingPolicyError("coding settings are invalid")
        if not isinstance(data.get("env", {}), dict):
            raise CodingPolicyError("coding environment is invalid")
        warnings = data.get("network_warnings", [])
        if not isinstance(warnings, list) or any(type(item) is not str for item in warnings):
            raise CodingPolicyError("coding network warnings are invalid")
        guidance = data.get("guidance_files", [])
        # Shape only: a guidance file deleted later must not make the manifest
        # unloadable; the snapshot taken at run creation re-validates.
        if not isinstance(guidance, list) or any(
            type(item) is not str or not Path(item).is_absolute() for item in guidance
        ):
            raise CodingPolicyError("coding guidance files are invalid")
        return cls(
            enabled=data.get("enabled", False),
            network_allowlist=normalize_network_allowlist(
                data.get("network_allowlist", list(DEFAULT_NETWORK_ALLOWLIST))
            ),
            env=CodingEnvState.from_dict(data.get("env", {})),
            network_warnings=list(warnings),
            guidance_files=list(guidance),
            git_exclude=(
                None
                if data.get("git_exclude") is None
                else normalize_git_exclude(data["git_exclude"])
            ),
            docker=DockerPolicy.from_dict(data.get("docker", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "network_allowlist": list(self.network_allowlist),
            "env": self.env.to_dict(),
            "network_warnings": list(self.network_warnings),
            "guidance_files": list(self.guidance_files),
            "git_exclude": None if self.git_exclude is None else list(self.git_exclude),
            "docker": self.docker.to_dict(),
        }


# A version number standing alone: no pre-release or build suffix attached.
_CLI_VERSION = re.compile(r"(?<![\w.-])(\d+)(?:\.(\d+))?(?:\.(\d+))?(?![\w.-])")


def parse_cli_version(text: str) -> tuple[int, int, int] | None:
    match = _CLI_VERSION.search(text)
    if match is None:
        return None
    major, minor, patch = (int(part or 0) for part in match.groups())
    return major, minor, patch


def cli_version_compatible(verified: str | None, probed: str | None) -> bool:
    """Same major as the spike-verified version, and not lower.

    An exact match always passes; otherwise both must parse, so a
    pre-release or an unrecognised string needs an exact match.
    """

    if verified is None or probed is None:
        return False
    if probed == verified:
        return True
    floor, found = parse_cli_version(verified), parse_cli_version(probed)
    return floor is not None and found is not None and found[0] == floor[0] and found >= floor


def version_at_least(verified: str, probed: str | None) -> bool:
    """A stable release no lower than the verified one, of any major.

    For components whose upgrades must not block runs (guest Docker,
    user decision 2026-10-21); a pre-release or unparseable string
    needs an exact match.
    """

    if probed is None:
        return False
    if probed == verified:
        return True
    floor, found = parse_cli_version(verified), parse_cli_version(probed)
    return floor is not None and found is not None and found >= floor


def build_capable(agent: str, probed_version: str | None) -> bool:
    return cli_version_compatible(BUILD_VERIFIED_VERSIONS.get(agent), probed_version)


def cli_newer_than_verified(
    agent: str,
    probed_version: str | None,
    verified_versions: dict[str, str] | None = None,
) -> str | None:
    """A note for an accepted CLI that the spikes never ran against."""

    table = BUILD_VERIFIED_VERSIONS if verified_versions is None else verified_versions
    verified = table.get(agent)
    if (
        verified is None
        or probed_version is None
        or probed_version == verified
        or not cli_version_compatible(verified, probed_version)
    ):
        return None
    return f"{agent} CLI {probed_version} is newer than the spike-verified {verified}"


def docker_capable(agent: str, probed_version: str | None) -> bool:
    """The build gate's rule against the S3/S4-verified versions."""

    return cli_version_compatible(DOCKER_VERIFIED_VERSIONS.get(agent), probed_version)

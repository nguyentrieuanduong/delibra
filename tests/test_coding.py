import pytest

from app.coding import (
    BUILD_VERIFIED_VERSIONS,
    DEFAULT_NETWORK_ALLOWLIST,
    CodingPolicy,
    CodingPolicyError,
    build_capable,
    normalize_network_allowlist,
    private_resolution_warnings,
)


def test_default_policy_is_disabled_with_package_indexes() -> None:
    policy = CodingPolicy.from_manifest({})
    assert policy.enabled is False
    assert policy.network_allowlist == list(DEFAULT_NETWORK_ALLOWLIST)
    assert policy.env.status == "absent"


def test_policy_round_trips() -> None:
    policy = CodingPolicy.from_manifest({})
    policy.enabled = True
    again = CodingPolicy.from_manifest({"coding": policy.to_dict()})
    assert again == policy


def test_hosts_are_lowercased_and_kept_in_order() -> None:
    assert normalize_network_allowlist([" PyPI.org ", "github.com"]) == [
        "pypi.org",
        "github.com",
    ]


def test_empty_list_means_no_network() -> None:
    assert normalize_network_allowlist([]) == []


@pytest.mark.parametrize(
    "bad",
    [
        "", "https://pypi.org", "pypi.org/simple", "pypi.org:443",
        "*.pypi.org", "*", "127.0.0.1", "[::1]", "localhost",
        "api.localhost", "pypi", "bad_host.org", "-x.org", "1.2.3",
        "printer.local", "nas.lan", "svc.internal", "box.home.arpa",
        "host.localdomain",
    ],
)
def test_invalid_hosts_are_rejected(bad: str) -> None:
    with pytest.raises(CodingPolicyError):
        normalize_network_allowlist([bad])


def test_duplicates_are_rejected() -> None:
    with pytest.raises(CodingPolicyError):
        normalize_network_allowlist(["pypi.org", "PYPI.org"])


def test_private_resolutions_warn_but_do_not_reject() -> None:
    table = {
        "pypi.org": ["151.101.0.223"],
        "intranet.example.com": ["10.1.2.3", "151.101.0.1"],
        "rebind.example.com": ["127.0.0.1"],
    }

    def resolve(host: str) -> list[str]:
        if host == "down.example.com":
            raise OSError("no such host")
        return table[host]

    warnings = private_resolution_warnings(
        ["pypi.org", "intranet.example.com", "rebind.example.com", "down.example.com"],
        resolve=resolve,
    )
    assert warnings == [
        "intranet.example.com resolves to a private address (10.1.2.3)",
        "rebind.example.com resolves to a private address (127.0.0.1)",
    ]


def test_policy_keeps_network_warnings() -> None:
    policy = CodingPolicy.from_manifest({})
    policy.network_warnings = ["x.example.com resolves to a private address (10.0.0.1)"]
    assert CodingPolicy.from_manifest({"coding": policy.to_dict()}) == policy


def test_only_the_spike_verified_cli_versions_are_build_capable() -> None:
    # S1/S2 on 2026-10-16 (spike/FINDINGS.md); strings as app/health.py reports them.
    assert build_capable("claude", "2.1.296 (Claude Code)") is True
    assert build_capable("codex", "codex-cli 0.162.1") is True
    assert build_capable("claude", "2.1.1") is False
    assert build_capable("claude", "2.1.297 (Claude Code)") is False
    assert build_capable("codex", "codex-cli 0.162.2") is False
    assert build_capable("claude", None) is False
    assert build_capable("fake", "1") is False


def test_version_match_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BUILD_VERIFIED_VERSIONS, "codex", "codex-cli 0.162.1")
    assert build_capable("codex", "codex-cli 0.162.1") is True
    assert build_capable("codex", "codex-cli 0.162.10") is False

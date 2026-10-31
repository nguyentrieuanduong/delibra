import os
import random
from pathlib import Path

import pytest

from app.coding import (
    BUILD_VERIFIED_VERSIONS,
    DOCKER_VERIFIED_VERSIONS,
    CODE_CRAFT_SUGGESTED_GUIDANCE,
    GUIDANCE_MAX_BYTES,
    GUIDANCE_MAX_FILES,
    GuidancePresetError,
    DEFAULT_NETWORK_ALLOWLIST,
    CodingPolicy,
    CodingPolicyError,
    DockerPolicy,
    build_capable,
    cli_newer_than_verified,
    cli_version_compatible,
    docker_capable,
    expand_guidance_preset,
    guidance_snapshot_size,
    normalize_git_exclude,
    normalize_guidance_paths,
    normalize_network_allowlist,
    parse_cli_version,
    private_resolution_warnings,
    read_guidance,
    version_at_least,
)
from app.config import GuidancePreset
from app.storage import ProjectStore, RegistryStore


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


def test_build_runs_accept_the_same_major_and_not_lower(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BUILD_VERIFIED_VERSIONS, "claude", "2.1.296 (Claude Code)")
    monkeypatch.setitem(BUILD_VERIFIED_VERSIONS, "codex", "codex-cli 0.162.1")
    assert build_capable("claude", "2.1.296 (Claude Code)") is True
    assert build_capable("claude", "2.1.297 (Claude Code)") is True
    assert build_capable("claude", "2.4.0 (Claude Code)") is True
    assert build_capable("claude", "2.1.295 (Claude Code)") is False
    assert build_capable("claude", "3.0.0 (Claude Code)") is False
    assert build_capable("codex", "codex-cli 0.163.0") is True
    assert build_capable("codex", "codex-cli 0.162.10") is True
    assert build_capable("codex", "codex-cli 0.162.0") is False
    assert build_capable("codex", "codex-cli 1.0.0") is False
    assert build_capable("codex", "codex-cli 0.164.0-alpha.1") is False
    assert build_capable("claude", "garbage") is False
    assert build_capable("claude", None) is False
    assert build_capable("unknown-agent", "1") is False


def test_unparseable_verified_versions_need_an_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BUILD_VERIFIED_VERSIONS, "claude", "spike-verified")
    assert build_capable("claude", "spike-verified") is True
    assert build_capable("claude", "spike-verified-2") is False


def test_cli_versions_parse_from_both_providers_output() -> None:
    assert parse_cli_version("2.1.296 (Claude Code)") == (2, 1, 296)
    assert parse_cli_version("codex-cli 0.163.0") == (0, 163, 0)
    assert parse_cli_version("1") == (1, 0, 0)
    assert parse_cli_version("codex-cli 0.164.0-alpha.1") is None
    assert parse_cli_version("garbage") is None


def test_cli_version_parsing_survives_arbitrary_text() -> None:
    # Generated inputs (TDD rule 3: the parser reads external CLI output).
    rng = random.Random(20261021)
    alphabet = "0123456789.-+ abcxyz()\t\n"
    for _ in range(5_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        parsed = parse_cli_version(text)
        assert parsed is None or (len(parsed) == 3 and all(part >= 0 for part in parsed))
        # An exact match always passes; anything else never crosses a major.
        assert cli_version_compatible(text, text) is True
        if parsed is not None:
            assert cli_version_compatible("9999.0.0", text) is (parsed[0] == 9999)


def test_a_newer_accepted_cli_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BUILD_VERIFIED_VERSIONS, "codex", "codex-cli 0.162.1")
    assert cli_newer_than_verified("codex", "codex-cli 0.162.1") is None
    assert cli_newer_than_verified("codex", "codex-cli 0.163.0") == (
        "codex CLI codex-cli 0.163.0 is newer than the spike-verified codex-cli 0.162.1"
    )
    assert cli_newer_than_verified("codex", "codex-cli 1.0.0") is None
    assert cli_newer_than_verified("codex", None) is None


def test_guidance_paths_must_be_absolute_markdown_regular_files(tmp_path) -> None:
    good = tmp_path / "tdd.md"
    good.write_text("# TDD\n")
    link = tmp_path / "link.md"
    link.symlink_to(good)
    assert normalize_guidance_paths([str(good)]) == [str(good)]
    for bad in ["relative.md", str(tmp_path / "missing.md"), str(tmp_path / "x.txt"), str(link)]:
        if bad.endswith("x.txt"):
            (tmp_path / "x.txt").write_text("x")
        with pytest.raises(CodingPolicyError):
            normalize_guidance_paths([bad])
    with pytest.raises(CodingPolicyError):
        normalize_guidance_paths([str(good), str(good)])


def test_guidance_total_size_is_bounded(tmp_path) -> None:
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (GUIDANCE_MAX_BYTES + 1))
    with pytest.raises(CodingPolicyError):
        normalize_guidance_paths([str(big)])


def test_read_guidance_labels_each_file(tmp_path) -> None:
    first = tmp_path / "a.md"
    first.write_text("Alpha rules\n")
    second = tmp_path / "b.md"
    second.write_text("Beta rules\n")
    text = read_guidance([str(first), str(second)]).decode()
    assert text.index(f"### {first}") < text.index("Alpha rules") < text.index(f"### {second}")


def test_a_list_accepted_on_save_is_accepted_at_run_start(tmp_path) -> None:
    guide = tmp_path / "large.md"
    header = len(f"### {guide}\n\n".encode())
    guide.write_bytes(b"x" * (GUIDANCE_MAX_BYTES - header - 1))

    assert guidance_snapshot_size([(str(guide), guide.stat().st_size)]) == GUIDANCE_MAX_BYTES
    assert normalize_guidance_paths([str(guide)]) == [str(guide)]
    assert len(read_guidance([str(guide)])) == GUIDANCE_MAX_BYTES


def test_a_list_over_the_snapshot_size_is_refused_on_save(tmp_path) -> None:
    guide = tmp_path / "large.md"
    guide.write_bytes(b"x" * GUIDANCE_MAX_BYTES)

    with pytest.raises(CodingPolicyError, match="including file headers"):
        normalize_guidance_paths([str(guide)])


def test_a_symlinked_parent_directory_is_refused_everywhere(tmp_path) -> None:
    real = tmp_path / "real" / "skills"
    real.mkdir(parents=True)
    (real / "a.md").write_text("# A\n")
    os.symlink(tmp_path / "real", tmp_path / "link")
    # The link is a parent, not the last component: plain O_NOFOLLOW misses it.
    through_link = str(tmp_path / "link" / "skills" / "a.md")

    with pytest.raises(CodingPolicyError, match="without symlinks"):
        normalize_guidance_paths([through_link])
    with pytest.raises(CodingPolicyError):
        read_guidance([through_link])
    with pytest.raises(GuidancePresetError, match="None of the s guidance files"):
        expand_guidance_preset(GuidancePreset("s", (str(tmp_path / "link" / "skills"),), True))


def test_a_parent_reference_is_refused(tmp_path) -> None:
    (tmp_path / "a.md").write_text("# A\n")

    with pytest.raises(CodingPolicyError, match="without symlinks"):
        normalize_guidance_paths([f"{tmp_path}/x/../a.md"])


def test_code_craft_suggestions_skip_approval_gated_skills() -> None:
    names = {path.split("/skills/")[1].split("/")[0] for path in CODE_CRAFT_SUGGESTED_GUIDANCE}
    assert names == {
        "test-driven-development", "coding-standards", "security-baseline",
        "systematic-debugging", "verification-before-completion",
    }


def test_git_exclude_defaults_to_delibras_list_and_round_trips() -> None:
    assert CodingPolicy().git_exclude is None
    assert CodingPolicy.from_manifest({"coding": {"enabled": True}}).git_exclude is None
    policy = CodingPolicy(git_exclude=["build/", "!keep/", ""])
    assert CodingPolicy.from_manifest({"coding": policy.to_dict()}).git_exclude == ["build/", "!keep/", ""]


@pytest.mark.parametrize(
    "bad",
    ["build/", [1], ["a\x00b"], ["a\rb"], ["# >>> Delibra coding mode"], ["# <<< Delibra coding mode"]],
)
def test_invalid_git_exclude_is_rejected(bad) -> None:
    with pytest.raises(CodingPolicyError):
        normalize_git_exclude(bad)
    with pytest.raises(CodingPolicyError):
        CodingPolicy.from_manifest({"coding": {"git_exclude": bad}})


def test_git_exclude_is_bounded() -> None:
    assert normalize_git_exclude(["\tindented", "x"]) == ["\tindented", "x"]
    with pytest.raises(CodingPolicyError):
        normalize_git_exclude(["x"] * 2_001)
    with pytest.raises(CodingPolicyError):
        normalize_git_exclude(["x" * 70_000])


def test_docker_defaults_off_and_round_trips() -> None:
    policy = CodingPolicy.from_manifest({})
    assert policy.docker == DockerPolicy()
    policy.docker = DockerPolicy(
        enabled=True, backend="lima", status="ready",
        lima_instance="delibra-0123456789", lima_version="2.2.1",
        host_loopback_closed=False, docker_version="29.0.0",
        updated_at="2026-10-21T00:00:00Z",
    )
    assert CodingPolicy.from_manifest({"coding": policy.to_dict()}) == policy


def test_a_coding_manifest_from_before_docker_loads_with_docker_off() -> None:
    legacy = CodingPolicy.from_manifest({}).to_dict()
    del legacy["docker"]
    assert CodingPolicy.from_manifest({"coding": legacy}).docker == DockerPolicy()


@pytest.mark.parametrize(
    "bad",
    [
        {"enabled": "yes"},
        {"status": "running"},
        {"backend": "desktop"},
        {"lima_instance": "--force"},
        {"local_endpoint": "relative.sock"},
        {"host_loopback_closed": "no"},
        {"status": "ready"},
        {"status": "ready", "backend": "lima"},
        {"status": "ready", "backend": "local"},
        {"status": "deleting"},
    ],
)
def test_invalid_docker_settings_are_rejected(bad: dict) -> None:
    with pytest.raises(CodingPolicyError):
        DockerPolicy.from_dict(bad)


def test_docker_capability_follows_the_same_major_rule(monkeypatch) -> None:
    monkeypatch.delitem(DOCKER_VERIFIED_VERSIONS, "claude", raising=False)
    assert not docker_capable("claude", "2.1.296 (Claude Code)")
    monkeypatch.setitem(DOCKER_VERIFIED_VERSIONS, "claude", "2.1.296 (Claude Code)")
    assert docker_capable("claude", "2.1.296 (Claude Code)")
    assert docker_capable("claude", "2.1.297 (Claude Code)")
    assert not docker_capable("claude", "2.1.295 (Claude Code)")
    assert not docker_capable("claude", "3.0.0 (Claude Code)")
    assert not docker_capable("claude", None)


def test_store_saves_docker_state(tmp_path) -> None:
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    store = ProjectStore(RegistryStore(tmp_path / "home").register("P", project_dir))
    state = DockerPolicy(enabled=True, status="preparing", updated_at="t")
    assert store.set_coding_docker(state).docker == state
    assert store.coding_policy().docker == state


def test_docker_verified_versions_match_the_s3_s4_findings() -> None:
    findings = (Path(__file__).resolve().parents[1] / "spike" / "FINDINGS.md").read_text()
    assert DOCKER_VERIFIED_VERSIONS
    for agent, version in DOCKER_VERIFIED_VERSIONS.items():
        assert f"## Docker spike: {agent}" in findings
        assert f"- {agent}: `{version}`" in findings


def test_version_at_least_accepts_newer_majors_but_not_downgrades_or_prereleases() -> None:
    assert version_at_least("29.9.2", "29.9.2")
    assert version_at_least("29.9.2", "29.10.0")
    assert version_at_least("29.9.2", "30.1.0")
    assert not version_at_least("29.9.2", "29.9.1")
    assert not version_at_least("29.9.2", "30.0.0-rc.1")
    assert not version_at_least("29.9.2", None)


def _skill_tree(root: Path) -> Path:
    (root / "sub" / "deep").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "a.md").write_text("# A\n")
    (root / "b.txt").write_text("b\n")
    (root / ".hidden.md").write_text("# H\n")
    (root / "sub" / "c.md").write_text("# C\n")
    (root / "sub" / "deep" / "d.MD").write_text("# D\n")
    (root / ".git" / "e.md").write_text("# E\n")
    os.symlink(root / "a.md", root / "link.md")
    os.symlink(root / "sub", root / "linkdir")
    return root


def test_a_directory_preset_takes_top_level_markdown_only(tmp_path) -> None:
    root = _skill_tree(tmp_path / "skills")

    assert expand_guidance_preset(GuidancePreset("s", (str(root),))) == [str(root / "a.md")]


def test_a_recursive_preset_walks_subdirectories_but_not_links_or_dot_names(tmp_path) -> None:
    root = _skill_tree(tmp_path / "skills")

    assert expand_guidance_preset(GuidancePreset("s", (str(root),), True)) == [
        str(root / "a.md"),
        str(root / "sub" / "c.md"),
        str(root / "sub" / "deep" / "d.MD"),
    ]


def test_preset_entries_keep_their_order_and_drop_duplicates(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _skill_tree(tmp_path / "skills")
    preset = GuidancePreset("s", ("~/skills/sub/c.md", "~/skills", "/missing/x.md"), True)

    assert expand_guidance_preset(preset) == [
        str(root / "sub" / "c.md"),
        str(root / "a.md"),
        str(root / "sub" / "deep" / "d.MD"),
    ]


def test_a_preset_with_nothing_found_is_refused(tmp_path) -> None:
    preset = GuidancePreset("s", (str(tmp_path / "gone"), str(tmp_path / "x.md")))

    with pytest.raises(GuidancePresetError) as caught:
        expand_guidance_preset(preset)

    assert str(caught.value) == (
        f"None of the s guidance files exist (looked in {tmp_path / 'gone'} and 1 more); "
        "guidance was not changed"
    )


def test_a_preset_over_the_file_limit_is_refused_with_counts(tmp_path) -> None:
    for index in range(GUIDANCE_MAX_FILES + 1):
        (tmp_path / f"{index:02}.md").write_text("x")

    with pytest.raises(
        GuidancePresetError,
        match=(
            r"^s expands to 33 files and \d+ bytes including file headers; the limit is "
            r"32 files and 262144 bytes; guidance was not changed$"
        ),
    ):
        expand_guidance_preset(GuidancePreset("s", (str(tmp_path),)))


def test_a_preset_over_the_byte_limit_is_refused(tmp_path) -> None:
    (tmp_path / "big.md").write_bytes(b"x" * GUIDANCE_MAX_BYTES)

    with pytest.raises(GuidancePresetError, match="expands to 1 files and"):
        expand_guidance_preset(GuidancePreset("s", (str(tmp_path),)))


def test_a_preset_scan_stops_at_the_entry_budget_before_listing_more(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("app.coding.GUIDANCE_SCAN_MAX_ENTRIES", 3)
    for index in range(50):
        (tmp_path / f"{index}.txt").write_text("x")
    seen: list[str] = []
    real_scandir = os.scandir

    class Counting:
        def __init__(self, target):
            self.inner = real_scandir(target)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.inner.close()

        def __iter__(self):
            for entry in self.inner:
                seen.append(entry.name)
                yield entry

    monkeypatch.setattr("app.coding.os.scandir", Counting)

    with pytest.raises(GuidancePresetError) as caught:
        expand_guidance_preset(GuidancePreset("s", (str(tmp_path),)))

    assert str(caught.value) == (
        "s has more than 3 directory entries to scan; point it at a smaller directory; "
        "guidance was not changed"
    )
    assert len(seen) == 4


def test_a_preset_scan_stops_at_the_depth_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.coding.GUIDANCE_SCAN_MAX_DEPTH", 2)
    (tmp_path / "1" / "2" / "3").mkdir(parents=True)

    with pytest.raises(GuidancePresetError) as caught:
        expand_guidance_preset(GuidancePreset("s", (str(tmp_path),), True))

    assert str(caught.value) == (
        "s nests deeper than 2 directories; point it at a smaller directory; "
        "guidance was not changed"
    )


def test_guidance_limits_allow_32_files_and_256_kib() -> None:
    assert (GUIDANCE_MAX_FILES, GUIDANCE_MAX_BYTES) == (32, 262_144)

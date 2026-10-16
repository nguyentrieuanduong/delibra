import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from app.build_git import _git, commit_turn, ensure_local_excludes, git_preflight


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The filter check reads every config scope. Isolate it, or a machine
    # with Git LFS installed globally would disable commits in every test.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Tester")
    git(root, "config", "user.email", "tester@example.invalid")
    (root / "README.md").write_text("hello\n")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "initial")
    return root


def history(root: Path) -> list[str]:
    return git(root, "log", "--format=%H").splitlines()


async def ready(root: Path):
    state = await git_preflight(root)
    assert state.enabled, state.reason
    return state


@pytest.mark.asyncio
async def test_preflight_on_a_clean_repo(tmp_path) -> None:
    root = make_repo(tmp_path)
    state = await git_preflight(root)
    assert (state.enabled, state.branch, state.head) == (True, "main", history(root)[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        (lambda root: (root / "dirty.py").write_text("x"), "working tree has uncommitted changes"),
        (lambda root: git(root, "checkout", "-q", "--detach"), "HEAD is detached"),
        (lambda root: (root / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n"), "merge, rebase"),
        (lambda root: (root / ".git" / "REVERT_HEAD").write_text("0" * 40 + "\n"), "revert"),
        (lambda root: (root / ".git" / "sequencer").mkdir(), "revert"),
        (lambda root: git(root, "config", "--unset", "user.email"), "git user.email is not configured"),
        (
            lambda root: ((root / ".delibra").mkdir(), (root / ".delibra" / "x").write_text("x"),
                          git(root, "add", "-f", ".delibra/x")),
            "Git index has staged changes",
        ),
    ],
)
async def test_preflight_disables_commits_with_a_reason(tmp_path, prepare, reason) -> None:
    root = make_repo(tmp_path)
    prepare(root)
    state = await git_preflight(root)
    assert state.enabled is False
    assert reason in (state.reason or "")


@pytest.mark.asyncio
async def test_preflight_outside_a_repo_or_below_its_root(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert (await git_preflight(plain)).reason == "project is not a git repository"
    root = make_repo(tmp_path)
    (root / "sub").mkdir()
    assert (await git_preflight(root / "sub")).reason == "project is not the Git worktree root"


@pytest.mark.asyncio
async def test_missing_git_disables_commits_without_aborting_build(tmp_path, monkeypatch) -> None:
    async def missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_git)
    state = await git_preflight(tmp_path)
    assert state.enabled is False
    assert "Git executable is unavailable" in (state.reason or "")


@pytest.mark.asyncio
async def test_changes_only_in_envs_or_delibra_keep_commits_on(tmp_path) -> None:
    root = make_repo(tmp_path)
    for private in ("envs/bin/python", ".delibra/manifest.json"):
        (root / private).parent.mkdir(parents=True, exist_ok=True)
        (root / private).write_text("x")
    assert (await git_preflight(root)).enabled is True


@pytest.mark.asyncio
async def test_a_configured_filter_disables_commits_without_running(tmp_path) -> None:
    root = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    git(root, "config", "filter.evil.clean", f"touch {marker}; cat")
    (root / ".gitattributes").write_text("* filter=evil\n")
    state = await git_preflight(root)
    assert state.enabled is False
    assert "filter" in (state.reason or "")
    assert not marker.exists()


@pytest.mark.asyncio
async def test_commit_turn_appends_exactly_one_commit(tmp_path) -> None:
    root = make_repo(tmp_path)
    state = await ready(root)
    before = history(root)
    (root / "a.py").write_text("print('a')\n")
    (root / "envs").mkdir()
    (root / "envs" / "big.bin").write_text("x")
    (root / ".delibra").mkdir()
    (root / ".delibra" / "x").write_text("x")

    state, warning = await commit_turn(root, state, "Auto build 1 · cycle 1 · Alpha", "Did a", "Delibra-Auto-Run: abc")

    after = history(root)
    assert warning is None
    assert after[1:] == before  # strict prefix: nothing rewritten
    assert state.head == after[0]
    assert git(root, "show", "--name-only", "--format=", "HEAD").splitlines() == ["a.py"]
    message = git(root, "log", "-1", "--format=%B")
    assert message.startswith("Auto build 1 · cycle 1 · Alpha\n\nDid a\n\nDelibra-Auto-Run: abc")


@pytest.mark.asyncio
async def test_no_changes_means_no_commit(tmp_path) -> None:
    root = make_repo(tmp_path)
    state = await ready(root)
    before = history(root)
    new_state, warning = await commit_turn(root, state, "s", "b", "t")
    assert (new_state, warning) == (state, None)
    assert history(root) == before


@pytest.mark.asyncio
async def test_hooks_never_run(tmp_path) -> None:
    root = make_repo(tmp_path)
    state = await ready(root)
    marker = tmp_path / "hook-ran"
    for hook in ("pre-commit", "commit-msg", "post-commit"):
        path = root / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        path.chmod(0o755)
    (root / "b.py").write_text("b\n")

    _state, warning = await commit_turn(root, state, "s", "b", "t")

    assert warning is None
    assert not marker.exists()


@pytest.mark.asyncio
async def test_head_moved_by_the_user_disables_commits(tmp_path) -> None:
    root = make_repo(tmp_path)
    state = await ready(root)
    git(root, "commit", "-q", "--allow-empty", "-m", "user commit")
    (root / "c.py").write_text("c\n")
    user_history = history(root)

    new_state, warning = await commit_turn(root, state, "s", "b", "t")

    assert new_state.enabled is False
    assert warning == "branch or HEAD changed outside the run; commits disabled"
    assert history(root) == user_history


@pytest.mark.asyncio
async def test_user_staged_paths_disable_commits_and_stay_staged(tmp_path) -> None:
    root = make_repo(tmp_path)
    state = await ready(root)
    (root / ".delibra").mkdir()
    (root / ".delibra" / "x").write_text("x")
    git(root, "add", "-f", ".delibra/x")
    (root / "d.py").write_text("d\n")

    new_state, warning = await commit_turn(root, state, "s", "b", "t")

    assert new_state.enabled is False
    assert warning == "Git index has staged changes; commits disabled"
    assert git(root, "diff", "--cached", "--name-only") == ".delibra/x"


@pytest.mark.asyncio
async def test_a_filter_added_mid_run_disables_commits_before_add(tmp_path) -> None:
    root = make_repo(tmp_path)
    state = await ready(root)
    marker = tmp_path / "filter-ran"
    git(root, "config", "filter.evil.clean", f"touch {marker}; cat")
    (root / ".gitattributes").write_text("* filter=evil\n")

    new_state, warning = await commit_turn(root, state, "s", "b", "t")

    assert new_state.enabled is False
    assert "filter" in (warning or "")
    assert not marker.exists()


@pytest.mark.asyncio
async def test_a_commit_that_overruns_its_budget_disables_commits(tmp_path, monkeypatch) -> None:
    root = make_repo(tmp_path)
    state = await git_preflight(root)
    (root / "new.txt").write_text("x\n")

    async def stalled(_root):
        await asyncio.sleep(5)

    monkeypatch.setattr("app.build_git._unsafe_filter_reason", stalled)
    monkeypatch.setattr("app.build_git.COMMIT_TIMEOUT_SECONDS", 0.1)
    new_state, warning = await commit_turn(root, state, "subject", "body", "trailer")

    assert new_state.enabled is False
    assert "did not finish within" in (warning or "")
    assert len(history(root)) == 1


@pytest.mark.asyncio
async def test_a_cancelled_git_command_does_not_outlive_its_task(tmp_path, monkeypatch) -> None:
    spawned: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def sleeping(*_argv, **kwargs):
        process = await original("sleep", "30", **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", sleeping)
    task = asyncio.create_task(_git(tmp_path, "status"))
    while not spawned:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawned[0].returncode is not None


@pytest.mark.asyncio
async def test_cancelled_git_reaps_a_child_holding_its_output_pipe(
    tmp_path, monkeypatch
) -> None:
    spawned: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec
    marker = tmp_path / "child-spawned"

    async def with_child(*_argv, **kwargs):
        process = await original(
            sys.executable,
            "-c",
            "import pathlib, subprocess, sys, time; "
            "subprocess.Popen(['sleep', '2']); "
            "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(2)",
            str(marker),
            **kwargs,
        )
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", with_child)
    task = asyncio.create_task(_git(tmp_path, "status"))
    async with asyncio.timeout(1):
        while not marker.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert spawned[0].returncode is not None


@pytest.mark.asyncio
async def test_commit_turn_works_when_gitignore_already_ignores_the_excluded_dirs(tmp_path) -> None:
    root = make_repo(tmp_path)
    (root / ".gitignore").write_text(".delibra/\nenvs/\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore private dirs")
    state = await git_preflight(root)
    for path in (".delibra/run.json", "envs/bin/python", "src/new.py"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x\n")
    (root / "README.md").unlink()

    new_state, warning = await commit_turn(root, state, "subject", "body", "trailer")

    assert warning is None
    assert new_state.enabled is True
    assert git(root, "show", "--name-status", "--format=", "HEAD").splitlines() == [
        "D\tREADME.md", "A\tsrc/new.py",
    ]
    assert git(root, "status", "--porcelain") == ""


BLOCK_START = "# >>> Delibra coding mode"


@pytest.mark.asyncio
async def test_local_excludes_add_the_default_ignore_list_once(tmp_path) -> None:
    root = make_repo(tmp_path)
    for path in (".delibra/run.json", "envs/bin/python", "__pycache__/m.cpython-312.pyc",
                 "node_modules/a/index.js", ".DS_Store", ".vscode/extensions.json",
                 ".vscode/settings.json", "src/app.py"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x\n")

    first = await ensure_local_excludes(root)
    second = await ensure_local_excludes(root)

    exclude = (root / ".git" / "info" / "exclude").read_text()
    assert exclude.count(BLOCK_START) == 1
    assert first == second and "/.delibra/" in first and "__pycache__/" in first
    assert sorted(git(root, "status", "--porcelain", "--untracked-files=all").splitlines()) == [
        "?? .vscode/extensions.json", "?? src/app.py",
    ]


@pytest.mark.asyncio
async def test_local_excludes_replace_only_their_own_block(tmp_path) -> None:
    root = make_repo(tmp_path)
    exclude = root / ".git" / "info" / "exclude"
    exclude.write_text(
        "mine-before\n# >>> Delibra coding mode\nstale-pattern\n# <<< Delibra coding mode\nmine-after\n"
    )

    await ensure_local_excludes(root)

    text = exclude.read_text()
    assert text.startswith("mine-before\n# >>> Delibra coding mode:")
    assert text.endswith("# <<< Delibra coding mode\nmine-after\n")
    assert "stale-pattern" not in text and "__pycache__/" in text


@pytest.mark.asyncio
async def test_local_excludes_are_confined_to_a_project_below_the_repo_root(tmp_path) -> None:
    repo = make_repo(tmp_path)
    root = repo / "apps" / "site"
    for path in ("apps/site/__pycache__/m.pyc", "apps/site/src/__pycache__/n.pyc",
                 "apps/site/.vscode/extensions.json", "apps/other/__pycache__/o.pyc"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("x\n")

    lines = await ensure_local_excludes(root)

    assert "/apps/site/.delibra/" in lines
    assert "/apps/site/**/__pycache__/" in lines
    assert "!/apps/site/.vscode/extensions.json" in lines
    assert sorted(git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()) == [
        "?? apps/other/__pycache__/o.pyc", "?? apps/site/.vscode/extensions.json",
    ]


def test_the_default_ignore_list_is_delibras_own_copy() -> None:
    from app.build_git import DEFAULT_EXCLUDE_TEMPLATE
    assert DEFAULT_EXCLUDE_TEMPLATE.name == "build_git_exclude.txt"
    assert DEFAULT_EXCLUDE_TEMPLATE.parent.name == "app"
    assert "__pycache__/" in DEFAULT_EXCLUDE_TEMPLATE.read_text(encoding="utf-8").splitlines()



@pytest.mark.asyncio
async def test_local_excludes_skip_a_project_outside_git(tmp_path) -> None:
    root = tmp_path / "plain"
    root.mkdir()

    assert await ensure_local_excludes(root) == []
    assert not (root / ".git").exists()


@pytest.mark.asyncio
async def test_local_excludes_write_a_projects_own_list(tmp_path) -> None:
    root = make_repo(tmp_path)

    lines = await ensure_local_excludes(root, ["only-this/", "*.tmp"])

    assert lines == ["/.delibra/", "/envs/", "only-this/", "*.tmp"]
    assert "__pycache__/" not in (root / ".git" / "info" / "exclude").read_text()


@pytest.mark.asyncio
async def test_a_cancelled_git_command_stays_cancelled_when_cleanup_is_slow(tmp_path, monkeypatch) -> None:
    import app.build_git as build_git

    class Stuck:
        pid = 999_999_999
        returncode = None

        async def communicate(self):
            await asyncio.sleep(60)

        async def wait(self):
            await asyncio.sleep(60)

    async def spawn(*_args, **_kwargs):
        return Stuck()

    monkeypatch.setattr(build_git.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(build_git.os, "killpg", lambda *_args: None)
    task = asyncio.create_task(build_git._git(tmp_path, "status"))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

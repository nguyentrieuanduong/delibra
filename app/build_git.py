"""Append-only commits made by Delibra after build turns.

Agents never write .git: a sandbox cannot tell commit from reset. Delibra
commits from outside the sandbox with hooks and fsmonitor disabled, and
refuses to commit once HEAD moves under it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import os
from pathlib import Path
import signal
import tempfile

from app.models import AutoBuildGit

PATHSPEC = ["--", ".", ":(exclude)envs", ":(exclude).delibra"]
SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false"]
COMMAND_TIMEOUT_SECONDS = 30
# The commit runs under the participant's session lock, which Stop also
# needs, so the whole sequence is bounded, not only each command.
COMMIT_TIMEOUT_SECONDS = 60


async def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(root), *SAFE, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return 127, f"Git executable is unavailable: {exc}"
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=COMMAND_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.CancelledError) as exc:
        # Git helpers can inherit stdout. Kill the entire process group,
        # including a child that outlives its parent.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.communicate(), timeout=2)
        except TimeoutError:
            await asyncio.wait_for(process.wait(), timeout=1)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 124, "git command timed out"
    return process.returncode or 0, out.decode("utf-8", "replace").strip()


def _off(reason: str) -> AutoBuildGit:
    return AutoBuildGit(enabled=False, reason=reason, branch=None, head=None)


async def _unsafe_filter_reason(root: Path) -> str | None:
    code, configured = await _git(
        root, "config", "--includes", "--get-regexp",
        r"^filter\..*\.(clean|process)$",
    )
    if code == 0 and configured:
        return "executable Git clean/process filter is configured"
    if code == 1:
        return None  # no matching configuration
    return "could not inspect Git filters"


async def git_preflight(root: Path) -> AutoBuildGit:
    code, top = await _git(root, "rev-parse", "--show-toplevel")
    if code != 0:
        return _off(
            top if code == 127 else "project is not a git repository"
        )
    if Path(top).resolve() != root.resolve():
        return _off("project is not the Git worktree root")
    if reason := await _unsafe_filter_reason(root):
        return _off(reason)
    code, branch = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if code != 0:
        return _off("HEAD is detached")
    code, git_dir = await _git(root, "rev-parse", "--absolute-git-dir")
    if code != 0:
        return _off("could not locate the Git directory")
    busy = (
        "MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
        "BISECT_LOG", "rebase-merge", "rebase-apply", "sequencer",
    )
    if any((Path(git_dir) / name).exists() for name in busy):
        return _off("a merge, rebase, cherry-pick, revert or bisect is in progress")
    code, status = await _git(root, "status", "--porcelain", *PATHSPEC)
    if code != 0 or status:
        return _off("working tree has uncommitted changes")
    code, _ = await _git(root, "diff", "--cached", "--quiet")
    if code != 0:
        return _off("Git index has staged changes or could not be inspected")
    for key in ("user.name", "user.email"):
        code, value = await _git(root, "config", key)
        if code != 0 or not value:
            return _off(f"git {key} is not configured")
    code, head = await _git(root, "rev-parse", "HEAD")
    if code != 0:
        return _off("repository has no commits yet")
    return AutoBuildGit(enabled=True, reason=None, branch=branch, head=head)


LOCAL_EXCLUDES = (".delibra/", "envs/")
# Delibra's own copy of the user's default ignore template (taken from the
# repository's .gitignore at f9c1946). Never read a project's .gitignore.
DEFAULT_EXCLUDE_TEMPLATE = Path(__file__).with_name("build_git_exclude.txt")
_BLOCK_START = "# >>> Delibra coding mode: default ignores, local to this clone (managed)"
_BLOCK_END = "# <<< Delibra coding mode"


def default_git_exclude() -> list[str]:
    """Delibra's default ignore list, one pattern (or comment) per line."""

    return DEFAULT_EXCLUDE_TEMPLATE.read_text(encoding="utf-8").splitlines()


def _scoped(pattern: str, prefix: str) -> str:
    """Confine one template pattern to the project at `prefix` ("" at the repo root)."""

    if not prefix or not pattern.strip() or pattern.startswith("#"):
        return pattern
    negated = pattern.startswith("!")
    body = pattern[1:] if negated else pattern
    if body.startswith("/"):
        scoped = f"/{prefix}{body[1:]}"
    elif body.startswith("**/") or "/" in body.rstrip("/"):
        scoped = f"/{prefix}{body}"
    else:
        scoped = f"/{prefix}**/{body}"
    return ("!" if negated else "") + scoped


async def ensure_local_excludes(
    root: Path, patterns: Sequence[str] | None = None
) -> list[str]:
    """Write Delibra's managed ignore block into this clone's `.git/info/exclude`.

    Never `.gitignore`: editing a tracked file would dirty the tree and turn
    build commits off. The block holds Delibra's own directories, anchored
    to the project root, then the default ignore template, confined to the
    project. It replaces the previous block, so template changes reach
    existing projects; lines outside it are the user's. Returns the block's
    patterns; none when the project is not in a Git repository. `patterns`
    is the project's own list; None means Delibra's default.
    """

    code, top = await _git(root, "rev-parse", "--show-toplevel")
    if code != 0:
        return []
    code, exclude = await _git(
        root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"
    )
    if code != 0:
        return []
    try:
        relative = root.resolve().relative_to(Path(top).resolve()).as_posix()
    except ValueError:
        return []
    prefix = "" if relative == "." else f"{relative}/"
    if any(character in prefix for character in "*?[]\\!#"):
        return []  # would be read as a pattern, not a path
    template = list(patterns) if patterns is not None else default_git_exclude()
    body = [f"/{prefix}{name}" for name in LOCAL_EXCLUDES] + [
        _scoped(line.rstrip(), prefix) for line in template
    ]
    block = [_BLOCK_START, *body, _BLOCK_END]
    path = Path(exclude)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    starts = [i for i, line in enumerate(lines) if line.startswith("# >>> Delibra coding mode")]
    ends = [i for i, line in enumerate(lines) if line == _BLOCK_END]
    if starts and ends and ends[0] > starts[0]:
        lines[starts[0] : ends[0] + 1] = block
    else:
        lines.extend(block)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [line for line in body if line.strip() and not line.startswith("#")]



async def commit_turn(
    root: Path, git: AutoBuildGit, subject: str, body: str, trailer: str
) -> tuple[AutoBuildGit, str | None]:
    if not git.enabled:
        return git, None
    try:
        return await asyncio.wait_for(
            _commit_turn(root, git, subject, body, trailer), COMMIT_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return _off("commit timed out"), (
            f"commit did not finish within {COMMIT_TIMEOUT_SECONDS}s; commits "
            "disabled. Check `git status` for staged changes"
        )


async def _commit_turn(
    root: Path, git: AutoBuildGit, subject: str, body: str, trailer: str
) -> tuple[AutoBuildGit, str | None]:
    if reason := await _unsafe_filter_reason(root):
        return _off(reason), f"{reason}; commits disabled"
    _code, branch = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    _code, head = await _git(root, "rev-parse", "HEAD")
    if branch != git.branch or head != git.head:
        warning = "branch or HEAD changed outside the run; commits disabled"
        return _off(warning), warning
    code, _ = await _git(root, "diff", "--cached", "--quiet")
    if code != 0:
        warning = "Git index has staged changes; commits disabled"
        return _off(warning), warning
    # `git add` with an exclude pathspec naming an ignored path exits 1
    # (Git 2.39) after staging everything else; `ls-files` accepts the
    # same excludes. So list the in-scope changes, then add exactly those.
    code, listed = await _git(
        root, "ls-files", "-z", "--modified", "--deleted", "--others",
        "--exclude-standard", *PATHSPEC,
    )
    if code != 0:
        return _off("git ls-files failed"), f"git ls-files failed: {listed[-500:]}"
    paths = sorted({path for path in listed.split("\0") if path})
    if not paths:
        return git, None
    with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
        handle.write(b"\0".join(path.encode("utf-8") for path in paths))
    try:
        code, out = await _git(
            root, "--literal-pathspecs", "add", "-A",
            f"--pathspec-from-file={handle.name}", "--pathspec-file-nul",
        )
    finally:
        os.unlink(handle.name)
    if code != 0:
        return _off("git add failed"), f"git add failed: {out[-500:]}"
    code, _ = await _git(root, "diff", "--cached", "--quiet")
    if code == 0:
        return git, None
    if code != 1:
        return _off("git diff failed"), "git diff failed; commits disabled"
    code, out = await _git(
        root, "commit", "--no-verify", "--only", "-m", subject, "-m", body,
        "-m", trailer, *PATHSPEC
    )
    if code != 0:
        return _off("git commit failed"), f"git commit failed: {out[-500:]}"
    _code, new_head = await _git(root, "rev-parse", "HEAD")
    return AutoBuildGit(enabled=True, reason=None, branch=git.branch, head=new_head), None

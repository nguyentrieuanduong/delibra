"""Append-only commits made by Delibra after build turns.

Agents never write .git: a sandbox cannot tell commit from reset. Delibra
commits from outside the sandbox with hooks and fsmonitor disabled, and
refuses to commit once HEAD moves under it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.models import AutoBuildGit

PATHSPEC = ["--", ".", ":(exclude)envs", ":(exclude).delibra"]
SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false"]


async def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(root), *SAFE, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"Git executable is unavailable: {exc}"
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.communicate()
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
    busy = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply")
    if any((Path(git_dir) / name).exists() for name in busy):
        return _off("a merge, rebase, cherry-pick or bisect is in progress")
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


async def commit_turn(
    root: Path, git: AutoBuildGit, subject: str, body: str, trailer: str
) -> tuple[AutoBuildGit, str | None]:
    if not git.enabled:
        return git, None
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
    code, out = await _git(root, "add", "-A", *PATHSPEC)
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

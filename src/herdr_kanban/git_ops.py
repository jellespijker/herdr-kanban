"""Isolated Git and worktree operations over the `git` CLI (ADR-0002, ADR-0006)."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GitRunner = Callable[[list[str], Path | None], subprocess.CompletedProcess[str]]

REMOTE_RE = re.compile(r"(?:github\.com[:/])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class GitError(RuntimeError):
    """Raised when a required git operation fails."""


@dataclass(slots=True)
class RebaseResult:
    ok: bool
    conflicted_files: list[str]
    output: str


def default_runner(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("git")
    if not exe:
        raise GitError("git executable not found in PATH")
    return subprocess.run(
        [exe, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=120,
    )


class GitClient:
    def __init__(self, runner: GitRunner = default_runner) -> None:
        self._run = runner

    def _git(
        self, *args: str, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        proc = self._run(list(args), cwd)
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise GitError(f"git {' '.join(args)} failed: {detail}")
        return proc

    def worktree_root(self, cwd: Path) -> Path | None:
        proc = self._git("rev-parse", "--show-toplevel", cwd=cwd, check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return Path(proc.stdout.strip()).resolve()

    def main_repo_root(self, cwd: Path) -> Path | None:
        """Return the main checkout root even when `cwd` is inside a linked git worktree."""
        proc = self._git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd, check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return self.worktree_root(cwd)
        common_dir = Path(proc.stdout.strip()).resolve()
        if common_dir.name == ".git":
            return common_dir.parent
        return self.worktree_root(cwd)

    def current_branch(self, cwd: Path) -> str:
        proc = self._git("branch", "--show-current", cwd=cwd, check=False)
        name = proc.stdout.strip()
        if name:
            return name
        rev = self._git("rev-parse", "--short", "HEAD", cwd=cwd, check=False)
        return rev.stdout.strip() or "main"

    def head_sha(self, cwd: Path) -> str:
        proc = self._git("rev-parse", "HEAD", cwd=cwd, check=False)
        return proc.stdout.strip()

    def parse_github_remote(self, cwd: Path, remote: str = "origin") -> tuple[str, str] | None:
        proc = self._git("remote", "get-url", remote, cwd=cwd, check=False)
        if proc.returncode != 0:
            return None
        return parse_remote_url(proc.stdout.strip())

    def fetch_branch(self, cwd: Path, remote: str, branch: str) -> None:
        self._git("fetch", "--quiet", remote, branch, cwd=cwd)

    def ensure_remote_branch(self, cwd: Path, remote: str, branch: str) -> None:
        """Fetch target branch; if it does not exist on remote yet, push HEAD to create it."""
        proc = self._git("fetch", "--quiet", remote, branch, cwd=cwd, check=False)
        if proc.returncode == 0:
            return
        self._git("push", remote, f"HEAD:refs/heads/{branch}", cwd=cwd)
        self._git("fetch", "--quiet", remote, branch, cwd=cwd)

    def create_ticket_worktree(
        self,
        main_repo: Path,
        worktree_path: Path,
        remote: str,
        target_branch: str,
        issue_number: int,
    ) -> tuple[Path, str]:
        """Create or reuse an isolated worktree branched from `<remote>/<target_branch>`."""
        local_branch = f"kanban/issue-{issue_number}"
        self._git("worktree", "prune", cwd=main_repo, check=False)
        self.fetch_branch(main_repo, remote, target_branch)

        if worktree_path.is_dir() and (worktree_path / ".git").exists():
            return worktree_path.resolve(), local_branch

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            "worktree",
            "add",
            "-B",
            local_branch,
            str(worktree_path),
            f"{remote}/{target_branch}",
            cwd=main_repo,
        )
        return worktree_path.resolve(), local_branch

    def remove_ticket_worktree(
        self,
        main_repo: Path,
        worktree_path: Path,
        local_branch: str | None = None,
        *,
        force: bool = False,
    ) -> None:
        if worktree_path.exists():
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(worktree_path))
            self._git(*args, cwd=main_repo, check=False)
        self._git("worktree", "prune", cwd=main_repo, check=False)
        if local_branch:
            self._git("branch", "-D", local_branch, cwd=main_repo, check=False)

    def is_clean(self, worktree_path: Path) -> bool:
        proc = self._git("status", "--porcelain", cwd=worktree_path, check=False)
        return proc.returncode == 0 and proc.stdout.strip() == ""

    def is_rebase_in_progress(self, worktree_path: Path) -> bool:
        for marker in ("rebase-merge", "rebase-apply"):
            proc = self._git("rev-parse", "--git-path", marker, cwd=worktree_path, check=False)
            git_path = proc.stdout.strip()
            if git_path:
                resolved = Path(git_path) if Path(git_path).is_absolute() else (worktree_path / git_path)
                if resolved.exists():
                    return True
        return False

    def commit_count_ahead(self, worktree_path: Path, base_ref: str) -> int:
        proc = self._git("rev-list", "--count", f"{base_ref}..HEAD", cwd=worktree_path, check=False)
        if proc.returncode != 0:
            return 0
        try:
            return int(proc.stdout.strip())
        except ValueError:
            return 0

    def touched_files(self, worktree_path: Path, base_ref: str) -> list[str]:
        """Files modified relative to `base_ref` (both committed and uncommitted)."""
        files: set[str] = set()
        committed = self._git("diff", "--name-only", f"{base_ref}...HEAD", cwd=worktree_path, check=False)
        if committed.returncode == 0:
            for line in committed.stdout.splitlines():
                if line.strip():
                    files.add(line.strip())
        dirty = self._git("status", "--porcelain", cwd=worktree_path, check=False)
        if dirty.returncode == 0:
            for line in dirty.stdout.splitlines():
                if len(line) > 3:
                    raw_path = line[3:].strip()
                    if " -> " in raw_path:
                        raw_path = raw_path.split(" -> ", 1)[1]
                    if raw_path:
                        files.add(raw_path.strip('"'))
        return sorted(files)

    def rebase_onto_target(self, worktree_path: Path, remote: str, target_branch: str) -> RebaseResult:
        self.fetch_branch(worktree_path, remote, target_branch)
        proc = self._git("rebase", f"{remote}/{target_branch}", cwd=worktree_path, check=False)
        if proc.returncode == 0:
            return RebaseResult(ok=True, conflicted_files=[], output=proc.stdout.strip())
        conflicts_proc = self._git("diff", "--name-only", "--diff-filter=U", cwd=worktree_path, check=False)
        conflicts = [line.strip() for line in conflicts_proc.stdout.splitlines() if line.strip()]
        combined = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
        return RebaseResult(ok=False, conflicted_files=conflicts, output=combined)

    def fast_forward_push(self, worktree_path: Path, remote: str, target_branch: str) -> tuple[bool, str]:
        """Push HEAD to `refs/heads/<target_branch>` without `--force`."""
        proc = self._git(
            "push",
            remote,
            f"HEAD:refs/heads/{target_branch}",
            cwd=worktree_path,
            check=False,
        )
        output = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
        return proc.returncode == 0, output


def parse_remote_url(url: str) -> tuple[str, str] | None:
    match = REMOTE_RE.search(url.strip())
    if not match:
        return None
    return match.group(1), match.group(2)

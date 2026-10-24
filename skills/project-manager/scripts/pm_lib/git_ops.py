"""Git facts: process invocation, status parsing, ancestry, surface matching.

Every function here computes a fact from git or normalizes a path string; none
of them judge anything. Surface matching uses segment-aware glob matching via
``PurePosixPath.full_match`` (Python 3.13+), not ``fnmatch``, so a single ``*``
never silently crosses a ``/``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

from . import PmError

FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def git(repo: Path, *args: str) -> str:
    """Run git, stripped stdout on success; raise PmError (with stderr) on failure."""
    returncode, stdout, stderr = git_result(repo, *args)
    if returncode != 0:
        message = stderr.strip() or stdout.strip() or "git command failed"
        raise PmError(message)
    return stdout.strip()


def git_result(repo: Path, *args: str) -> tuple[int, str, str]:
    """Run git, never raising. Returns (returncode, stdout, stderr) unstripped."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode, result.stdout, result.stderr


def git_head(repo: Path) -> str | None:
    returncode, stdout, _stderr = git_result(repo, "rev-parse", "--verify", "HEAD")
    return stdout.strip() if returncode == 0 else None


def current_branch(repo: Path) -> str | None:
    """The current branch name, or None when detached or the repo is unborn."""
    returncode, stdout, _stderr = git_result(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if returncode != 0:
        return None
    branch = stdout.strip()
    # Detached HEAD reports the literal string "HEAD" rather than a branch name.
    if not branch or branch == "HEAD":
        return None
    return branch


def git_bytes(repo: Path, *args: str) -> bytes | None:
    """Run git and return its raw stdout bytes, or None when it fails: for
    content that is hashed, where text decoding would alter the bytes."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return result.stdout if result.returncode == 0 else None


def git_status_text(repo: Path) -> str:
    # Deliberately not the git() helper above: that strips stdout, and
    # `status --short` output is positional — a leading space on the first
    # line (" M file", an unstaged modify) is part of the status code.
    # Stripping it would shift that line's path parse by one character.
    # `--no-renames` reports a dirty rename as a delete plus an add, so both
    # sides reach the surface check, as they do for a committed rename.
    returncode, stdout, stderr = git_result(repo, "status", "--short", "--untracked-files=all", "--no-renames")
    if returncode != 0:
        raise PmError(stderr.strip() or stdout.strip() or "git status failed")
    return stdout


def status_path(line: str) -> str:
    """Extract the path from one `git status --short` line.

    `git_status_text` asks for `--no-renames`, so a rename line ("R  a -> b")
    should not occur; one handed in from elsewhere takes the target (b). Surrounding quotes
    (git quotes paths containing unusual characters) are stripped, but the
    quoted content is not C-unescaped — an unusual path simply won't
    string-match an authorized entry, which fails closed rather than open.
    """
    path = line[3:] if len(line) > 3 else line
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    return path.strip().strip('"')


def meaningful_status_lines(status_text: str) -> list[str]:
    """Status lines with `.pm` and `.pm/`-prefixed paths filtered out."""
    lines: list[str] = []
    for line in status_text.splitlines():
        path = status_path(line)
        if path == ".pm" or path.startswith(".pm/"):
            continue
        lines.append(line)
    return lines


def require_clean_worktree(repo: Path) -> None:
    dirty = meaningful_status_lines(git_status_text(repo))
    if dirty:
        raise PmError("starting git state is dirty outside .pm/: " + "; ".join(dirty))


def status_changed_files(status_text: str) -> set[str]:
    return {status_path(line) for line in meaningful_status_lines(status_text)}


def changed_files_between(repo: Path, before_head: str | None, after_head: str | None, after_status: str) -> set[str]:
    """Union of committed changes (before_head..after_head) and dirty-tree changes."""
    files: set[str] = set()
    if before_head and after_head and before_head != after_head:
        files.update(git(repo, "diff", "--no-renames", "--name-only", before_head, after_head).splitlines())
    elif after_head and before_head is None:
        files.update(git(repo, "show", "--no-renames", "--name-only", "--format=", after_head).splitlines())
    files.update(status_changed_files(after_status))
    return {path for path in files if path}


def write_git_diff(repo: Path, before_head: str | None, after_head: str | None, destination: Path) -> None:
    """Write a binary-safe diff patch to destination.

    On git failure, destination is still written as an empty (but valid,
    zero-change) diff, and the error is recorded in a sidecar file next to
    it — so the artifact is never a diff-shaped file that is actually a git
    error message.
    """
    if before_head and after_head and before_head != after_head:
        returncode, stdout, stderr = git_result(repo, "diff", "--binary", before_head, after_head)
    elif after_head and before_head is None:
        # A run initialized on an unborn branch has no before_head to diff
        # against; `git show` covers the committed slice exactly as
        # `changed_files_between` does for the same case, so the evidence
        # artifact is not silently empty.
        returncode, stdout, stderr = git_result(repo, "show", "--binary", "--format=", after_head)
    else:
        returncode, stdout, stderr = git_result(repo, "diff", "--binary")
    if returncode == 0:
        destination.write_text(stdout, encoding="utf-8")
    else:
        destination.write_text("", encoding="utf-8")
        (destination.parent / "git-diff-error.txt").write_text(stderr, encoding="utf-8")


def is_full_commit_hash(value: str | None) -> bool:
    return bool(value and FULL_COMMIT_RE.fullmatch(value))


def commit_is_descendant(repo: Path, before_head: str | None, after_head: str | None) -> bool:
    if not after_head:
        return False
    if not before_head:
        return True
    returncode, _stdout, _stderr = git_result(repo, "merge-base", "--is-ancestor", before_head, after_head)
    return returncode == 0


def normalize_authorized_entry(entry: str) -> str:
    """Normalize one raw "Files allowed to change" line into a path string.

    Plan authors commonly write `` `path/to/file.py` (new file) `` — an
    inline-code span followed by a trailing annotation. str.strip("`") only
    trims from the very ends of the string, so it cannot remove a closing
    backtick that isn't the last character. Extract the inline-code span
    explicitly when present; only fall back to raw stripping for plain
    (non-backtick-wrapped) entries.
    """
    stripped = entry.strip()
    match = re.match(r"`([^`]+)`", stripped)
    if match:
        return match.group(1).strip().rstrip(".")
    return stripped.strip("`").rstrip(".")


def has_glob(entry: str) -> bool:
    """True when `entry` carries a glob marker, so `is_authorized_path` reads it as a pattern."""
    return any(marker in entry for marker in ("*", "?", "["))


def is_authorized_path(path: str, authorized_entries: list[str]) -> bool:
    for raw_entry in authorized_entries:
        entry = normalize_authorized_entry(raw_entry)
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif has_glob(entry):
            # PurePosixPath.full_match is path-segment aware: a single "*"
            # does not cross "/", so "*.md" matches only top-level markdown,
            # not "deep/nested/anything.md". Authors who want a recursive
            # match write "**/*.md" explicitly. fnmatch would silently widen
            # the one gate PM computes itself.
            if PurePosixPath(path).full_match(entry):
                return True
        elif path == entry:
            return True
    return False


def is_protected_path(path: str, protected: list[str]) -> bool:
    """True when `path` falls under a protected entry.

    Protection fails closed, so a plain entry naming a directory
    ("instruments", no trailing slash) covers its whole subtree as well as the
    path itself. `is_authorized_path` must not do the same: it decides what a
    slice may change, and a surface that widened from `src` to `src/**` would
    grant more than the plan froze. Slash, glob and exact entries behave as in
    `is_authorized_path`.
    """
    if is_authorized_path(path, protected):
        return True
    for raw_entry in protected:
        entry = normalize_authorized_entry(raw_entry)
        if entry and not entry.endswith("/") and not has_glob(entry):
            if path == entry or path.startswith(entry + "/"):
                return True
    return False


def unauthorized_files(changed_files: set[str], authorized_entries: list[str]) -> list[str]:
    return sorted(path for path in changed_files if not is_authorized_path(path, authorized_entries))


def resolve_repo(path: Path) -> Path:
    repo = path.expanduser().resolve()
    if not repo.exists():
        raise PmError(f"repo path does not exist: {repo}")
    root = git(repo, "rev-parse", "--show-toplevel")
    return Path(root).resolve()


def resolve_plan(path: Path) -> Path:
    plan = path.expanduser().resolve()
    if not plan.is_file():
        raise PmError(f"plan file does not exist: {plan}")
    return plan


def worktree_git_dir(repo: Path) -> Path:
    """The worktree-specific git directory (`rev-parse --absolute-git-dir`, resolved).

    Linked worktrees get their own git directory under the common git dir's
    `worktrees/<name>/`, so PM state rooted here is distinct per worktree.
    """
    return Path(git(repo, "rev-parse", "--absolute-git-dir")).resolve()


def git_common_dir_name(repo: Path) -> str:
    """The main repository's name: the basename of the directory holding the git common dir.

    Unlike `repo.name`, this names the main checkout even when `repo` is a
    linked worktree, whose own top level is usually named after the worktree.
    """
    common_dir = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common_dir).resolve().parent.name

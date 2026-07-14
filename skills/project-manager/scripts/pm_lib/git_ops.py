from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

from .constants import FULL_COMMIT_RE
from .models import CommandResult, PmError
from .process import run_command


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise PmError(message)
    return result.stdout.strip()


def git_result(repo: Path, *args: str) -> CommandResult:
    return run_command(["git", "-C", str(repo), *args], allow_failure=True)


def git_head(repo: Path) -> str | None:
    result = git_result(repo, "rev-parse", "--verify", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def git_status_text(repo: Path) -> str:
    # Deliberately not the generic git() helper: that strips stdout, and
    # `status --short` output is positional — a leading space on the first
    # line (" M file", an unstaged modify) is part of the status code, and
    # stripping it shifted that line's path parse by one character
    # ("EADME.md"), misreporting the file at every consumer of status text.
    result = git_result(repo, "status", "--short", "--untracked-files=all")
    if result.returncode != 0:
        raise PmError(result.stderr.strip() or result.stdout.strip() or "git status failed")
    return result.stdout


def status_path(line: str) -> str:
    # Note: git quotes paths with unusual characters (non-ASCII, embedded
    # quotes) and C-style escapes them. PM strips the surrounding quotes but
    # does not unescape, so such a path won't string-match the authorized entry
    # and the slice fails closed at the gate — the safe direction, if slightly
    # opaque, for the rare repos that hit it.
    path = line[3:] if len(line) > 3 else line
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    return path.strip().strip('"')


def meaningful_status_lines(status_text: str) -> list[str]:
    lines: list[str] = []
    for line in status_text.splitlines():
        path = status_path(line)
        if path == ".ai-pm" or path.startswith(".ai-pm/"):
            continue
        lines.append(line)
    return lines


def require_clean_worktree(repo: Path) -> None:
    dirty = meaningful_status_lines(git_status_text(repo))
    if dirty:
        raise PmError("starting git state is dirty outside .ai-pm/: " + "; ".join(dirty))


def status_changed_files(status_text: str) -> set[str]:
    return {status_path(line) for line in meaningful_status_lines(status_text)}


def changed_files_between(repo: Path, before_head: str | None, after_head: str | None, after_status: str) -> set[str]:
    files: set[str] = set()
    if before_head and after_head and before_head != after_head:
        files.update(git(repo, "diff", "--name-only", before_head, after_head).splitlines())
    elif after_head and before_head is None:
        files.update(git(repo, "show", "--name-only", "--format=", after_head).splitlines())
    files.update(status_changed_files(after_status))
    return {path for path in files if path}


def write_git_diff(repo: Path, before_head: str | None, after_head: str | None, destination: Path) -> None:
    if before_head and after_head and before_head != after_head:
        result = git_result(repo, "diff", "--binary", before_head, after_head)
    else:
        result = git_result(repo, "diff", "--binary")
    if result.returncode == 0:
        destination.write_text(result.stdout, encoding="utf-8")
    else:
        # Keep the patch file a valid (empty) diff and record the failure
        # separately, so the artifact is never a diff-shaped file that is
        # actually a git error message.
        destination.write_text("", encoding="utf-8")
        (destination.parent / "git-diff-error.txt").write_text(result.stderr, encoding="utf-8")


def is_full_commit_hash(value: str | None) -> bool:
    return bool(value and FULL_COMMIT_RE.fullmatch(value))


def commit_is_descendant(repo: Path, before_head: str | None, after_head: str | None) -> bool:
    if not after_head:
        return False
    if not before_head:
        return True
    result = git_result(repo, "merge-base", "--is-ancestor", before_head, after_head)
    return result.returncode == 0


def normalize_authorized_entry(entry: str) -> str:
    # Plan authors commonly write `` `path/to/file.py` (new file) `` — an
    # inline-code span followed by a trailing annotation. str.strip("`")
    # only trims from the very ends of the string, so it cannot remove a
    # closing backtick that isn't the last character. Extract the inline
    # code span explicitly when present; only fall back to raw stripping
    # for plain (non-backtick-wrapped) entries.
    stripped = entry.strip()
    match = re.match(r"`([^`]+)`", stripped)
    if match:
        return match.group(1).strip().rstrip(".")
    return stripped.strip("`").rstrip(".")


def is_authorized_path(path: str, authorized_entries: list[str]) -> bool:
    for raw_entry in authorized_entries:
        entry = normalize_authorized_entry(raw_entry)
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif any(marker in entry for marker in ("*", "?", "[")):
            # PurePosixPath.full_match is path-segment aware: a single "*" does
            # not cross "/", so an authorized entry of "*.md" matches only
            # top-level markdown, not "deep/nested/anything.md". Authors who
            # want a recursive match write "**/*.md" explicitly. fnmatch would
            # silently widen the one gate PM computes itself.
            if PurePosixPath(path).full_match(entry):
                return True
        elif path == entry:
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


def git_access_path(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--absolute-git-dir")).resolve()

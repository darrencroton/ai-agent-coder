"""Git facts: process invocation, status parsing, ancestry, surface matching.

Every function here computes a fact from git or normalizes a path string; none
of them judge anything. The two host-side commands that recurse into
submodules, `git status` and the worktree-mode `git diff`, refuse a repository
whose index holds a gitlink (see `refuse_gitlinks`). Surface matching uses segment-aware glob matching via
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


def gitlinks(repo: Path) -> list[str]:
    """Paths of the index's gitlinks (mode-160000 entries, i.e. submodules).

    Read from `git ls-files --stage -z`, which lists the index and executes no
    repository configuration, unlike `git status` and `git diff`, which
    recurse into a populated submodule and run its configuration (for example
    `core.fsmonitor`) on the host. `-z` keeps unusual path names unquoted.
    Raises PmError when git fails, so a caller never reads a failed listing
    as "no gitlinks".
    """
    returncode, stdout, stderr = git_result(repo, "ls-files", "--stage", "-z")
    if returncode != 0:
        raise PmError(stderr.strip() or stdout.strip() or "git ls-files failed")
    paths: list[str] = []
    for entry in stdout.split("\0"):
        # Each entry is "<mode> <object> <stage>\t<path>"; the metadata holds
        # no tab, so the first tab ends it even when the path has spaces.
        metadata, _tab, path = entry.partition("\t")
        if path and metadata.split(" ", 1)[0] == "160000":
            paths.append(path)
    return paths


def refuse_gitlinks(repo: Path) -> None:
    """Fail closed, before a host-side recursing git command runs, when the index holds a gitlink.

    `git status` and the worktree-mode `git diff` recurse into a populated
    submodule and execute its configuration on the host, and a gitlink planted
    by a sandboxed process can point at a repository whose configuration it
    controls. Repositories with submodules are therefore not supported, sandboxed
    or not. Residuals: the window between this check and the command that
    follows it (a detached sandboxed process could add a gitlink in between),
    and the PM and Campaign Manager agents' own git commands, which this
    check does not reach.
    """
    paths = gitlinks(repo)
    if paths:
        raise PmError(
            "repositories with submodules are not supported: the index holds "
            + ", ".join(paths)
            + ", and the host's `git status` would execute the submodule's configuration"
        )


def git_status_text(repo: Path) -> str:
    """The repository's `git status --short` text, unstripped.

    This is the one host-side `git status` in the toolkit, so every status
    read goes through it. It refuses (PmError) a repository whose index holds a
    gitlink, before `status` runs, because `status` would recurse into the
    submodule and execute its configuration on the host; see `refuse_gitlinks`.

    Deliberately not the git() helper above: that strips stdout, and
    `status --short` output is positional — a leading space on the first
    line (" M file", an unstaged modify) is part of the status code.
    Stripping it would shift that line's path parse by one character.
    `--no-renames` reports a dirty rename as a delete plus an add, so both
    sides reach the surface check, as they do for a committed rename.
    """
    refuse_gitlinks(repo)
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


def git_diff_text(repo: Path, before_head: str | None, after_head: str | None) -> tuple[str, str | None]:
    """A binary-safe diff patch as `(diff text, error text)`; the caller writes it.

    On git failure the diff text is empty (a valid, zero-change diff) and the
    error text is git's stderr, so a caller records the error beside the
    artifact and the artifact is never a diff-shaped file that is actually a
    git error message; on success the error text is None. This function
    writes nothing: the callers own the destination, and finalize's destination
    is under the sandbox-writable `.pm/` tree, which host code may write only
    through `pm_paths.write_pm_text`.

    The worktree-mode diff (no refs) recurses into submodules like `git
    status`, so it refuses (PmError) a repository whose index holds a gitlink
    before running; see `refuse_gitlinks`. Tree-to-tree diffs and `git show`
    spawn no submodule process and are not checked.
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
        refuse_gitlinks(repo)
        returncode, stdout, stderr = git_result(repo, "diff", "--binary")
    if returncode == 0:
        return stdout, None
    return "", stderr


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

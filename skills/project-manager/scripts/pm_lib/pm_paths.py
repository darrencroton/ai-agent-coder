"""Guarded access to the sandbox-writable `.pm/` tree.

`<worktree>/.pm/` is writable by the sandboxed Developer (and, in a campaign,
the child PM), and the host reads and writes files there. Path.read_text and
write_text follow symlinks, so a link planted by the sandbox would turn a host
write of Developer-chosen text into a write of that text anywhere the operator
can write, and a host read into a read of a file the sandbox is denied (the
host then copies it where the sandbox can read it). RULE: host code never
opens a path under `<repo>/.pm/` except through the helpers below (`open_pm_file`, `read_pm_text`,
`read_pm_bytes`, `write_pm_text`, `write_pm_bytes`, `pm_file_present`,
`ensure_pm_dir`).

The walk starts at the trusted repo, opens `.pm` itself and every component
beneath it with O_NOFOLLOW relative to the previous directory fd, and opens
the leaf with O_NOFOLLOW, so the check and the use are one operation (an
lstat-then-open walk leaves a swap window). O_NOFOLLOW does not refuse a hard
link, so the leaf is also fstat-ed and must be a regular file with a single
link before a write truncates it (a linked leaf would carry the truncation to
the other name). Mechanical: a refusal is a PmError, never a silent skip.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

from . import PmError

# errno values that mean "this component is a symlink (or not what we need)":
# ELOOP and EMLINK are how O_NOFOLLOW reports a symlink on Linux and macOS or
# FreeBSD, ENOTDIR is a non-directory (or a symlink under O_DIRECTORY) where a
# directory was needed, ENXIO is a write-open of a FIFO with no reader, EISDIR a
# directory where a file was needed.
_PM_REFUSED_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR, errno.ENXIO, errno.EISDIR})
_PM_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _pm_refusal(repo: Path, names: list[str], reason: str) -> PmError:
    shown = repo.joinpath(*names)
    return PmError(
        f"refused {shown}: {reason}; the path is in the sandbox-writable .pm/ tree, "
        "where a symlink or hard link planted by the sandbox would redirect a host read or write"
    )


def _pm_components(repo: Path, path: Path) -> tuple[Path, list[str]]:
    """The resolved `repo` and `path`'s components below it, `.pm` first.

    `path` may be spelled from `repo` as given or from its resolved form
    (state records absolute paths). Anything else, or a `..` component, is a
    PmError: the helpers only ever touch the `.pm/` tree.
    """
    root = Path(repo).resolve()
    target = Path(path)
    for base in (Path(repo), root):
        try:
            parts = list(target.relative_to(base).parts)
            break
        except ValueError:
            continue
    else:
        raise PmError(f"refused {target}: not under {root / '.pm'}")
    if not parts or parts[0] != ".pm" or ".." in parts:
        raise PmError(f"refused {target}: not under {root / '.pm'}")
    return root, parts


def _open_pm_dir_chain(root: Path, names: list[str], *, create: bool) -> int:
    """Open `root`, then each of `names` beneath it, as directories; return the last fd.

    Every step is `O_NOFOLLOW | O_DIRECTORY` relative to the previous fd, so
    no component, `.pm` included, is followed if it is a symlink. With
    `create` a missing directory is made with `mkdir(dir_fd=...)` and then
    opened the same way. The caller closes the returned fd; every
    intermediate fd is closed here, on success and on failure.
    """
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    walked: list[str] = []
    try:
        for name in names:
            walked.append(name)
            try:
                try:
                    child = os.open(name, _PM_DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise FileNotFoundError(
                            errno.ENOENT, os.strerror(errno.ENOENT), str(root.joinpath(*walked))
                        ) from None
                    try:
                        os.mkdir(name, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(name, _PM_DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in _PM_REFUSED_ERRNOS:
                    raise _pm_refusal(root, walked, "a symlink or non-directory where a directory is needed") from exc
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_pm_file(repo: Path, path: Path, *, write: bool, exclusive: bool = False) -> int:
    """Open `path` (under `<repo>/.pm/`) without following any symlink; return the fd.

    The one way host code opens a file in the sandbox-writable `.pm/` tree
    (see the module docstring). `repo` is trusted and resolved once;
    `.pm` and every directory below it are opened `O_NOFOLLOW | O_DIRECTORY`
    relative to the previous one, and the leaf `O_NOFOLLOW`. A symlink
    anywhere on the path, a leaf that is not a regular file, or a leaf with
    more than one hard link raises `PmError` naming the component, because
    the path is in the sandbox-writable tree. A missing path raises
    `FileNotFoundError` on a read.

    `write=True` creates missing directories and the leaf, then truncates the
    leaf only after the checks pass (never `O_TRUNC` on the open, which
    would truncate a hard-linked file before the check); `exclusive` creates
    the leaf with `O_EXCL` and raises `FileExistsError` when it exists. The
    leaf is opened non-blocking, so a planted FIFO is refused, not waited on.
    The caller owns the returned fd.
    """
    root, parts = _pm_components(repo, path)
    parent_fd = _open_pm_dir_chain(root, parts[:-1], create=write)
    try:
        flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if write:
            flags |= os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else 0)
        else:
            flags |= os.O_RDONLY
        try:
            fd = os.open(parts[-1], flags, 0o666, dir_fd=parent_fd)
        except FileExistsError:
            raise
        except FileNotFoundError:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(root.joinpath(*parts))) from None
        except OSError as exc:
            if exc.errno in _PM_REFUSED_ERRNOS:
                raise _pm_refusal(root, parts, "a symlink, FIFO or directory where a file is needed") from exc
            raise
    finally:
        os.close(parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _pm_refusal(root, parts, "not a regular file")
        if info.st_nlink != 1:
            raise _pm_refusal(root, parts, f"a file with {info.st_nlink} hard links")
        if write and not exclusive:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _fdopen(fd: int, mode: str, **kwargs: Any) -> Any:
    """`os.fdopen` that closes `fd` itself when the wrapping fails."""
    try:
        return os.fdopen(fd, mode, **kwargs)
    except BaseException:
        os.close(fd)
        raise


def read_pm_text(repo: Path, path: Path, *, errors: str = "strict") -> str:
    """`path` (under `<repo>/.pm/`) read as UTF-8 through `open_pm_file`.

    Raises `FileNotFoundError` when it is absent and `PmError` when the path
    is refused; a caller that tolerates absence catches only the former.
    """
    with _fdopen(open_pm_file(repo, path, write=False), "r", encoding="utf-8", errors=errors) as handle:
        return handle.read()


def read_pm_bytes(repo: Path, path: Path, limit: int | None = None) -> bytes:
    """`path` (under `<repo>/.pm/`) read through `open_pm_file`; at most `limit` bytes when given."""
    with _fdopen(open_pm_file(repo, path, write=False), "rb") as handle:
        return handle.read() if limit is None else handle.read(limit)


def write_pm_text(repo: Path, path: Path, text: str, *, exclusive: bool = False) -> None:
    """Write `text` to `path` (under `<repo>/.pm/`) as UTF-8 through `open_pm_file`,
    creating missing directories. `exclusive` raises `FileExistsError` instead of replacing."""
    with _fdopen(open_pm_file(repo, path, write=True, exclusive=exclusive), "w", encoding="utf-8") as handle:
        handle.write(text)


def write_pm_bytes(repo: Path, path: Path, data: bytes) -> None:
    """Write `data` to `path` (under `<repo>/.pm/`) through `open_pm_file`, creating missing directories."""
    with _fdopen(open_pm_file(repo, path, write=True), "wb") as handle:
        handle.write(data)


def pm_file_present(repo: Path, path: Path) -> bool:
    """Whether `path` (under `<repo>/.pm/`) is an existing regular file, by the
    same walk as `open_pm_file`: False when absent, `PmError` when refused (a
    planted link is not a file that is merely missing). Replaces the
    symlink-following `Path.is_file()` and `exists()` probes."""
    try:
        os.close(open_pm_file(repo, path, write=False))
    except FileNotFoundError:
        return False
    return True


def ensure_pm_dir(repo: Path, path: Path) -> None:
    """Create `path` (under `<repo>/.pm/`) and any missing parents as real
    directories, refusing a symlink on the way like `open_pm_file`. Replaces
    `Path.mkdir(parents=True)`, which follows a symlinked `.pm` or parent and
    creates directories under its target."""
    root, parts = _pm_components(repo, path)
    os.close(_open_pm_dir_chain(root, parts, create=True))

"""Archive a stopped campaign child before its node worktree is removed.

`git worktree remove` deletes the worktree's git-ignored files and its git
directory, and with them everything a Campaign Manager judges a node by and
everything a later reader audits it from: the child PM's HANDOFF.md, the
`.pm/` tree (the campaign channel files, the run artifacts), the state root
under the worktree's git directory, any uncommitted work a child that died
mid-slice left behind, and the product's git-ignored output. `archive_child`
captures all of it, in the order campaign-mode.md *Cleanup* gives, into the
dossier's `validation/<node>/` and a results root the CM names, checks that
the copies are complete, and applies the contract's removal rule; it never
removes anything itself. `stop-child --archive` calls it after the child's
server is confirmed gone and before the sandbox teardown (which would
remove the sandbox temp dir it copies), and removes the worktree only when
it returned. Every step overwrites what an earlier, partial run wrote, so a
re-run after a failure completes the archive.

What it decides is mechanical: a copy is complete when every regular file
of its source exists at the destination with the same size, and the
worktree may go when `diff.patch` is non-empty whenever `status.txt` lists a
change and no nested repository outside the brief's output directories was
recorded as a bare gitlink (whose content no patch holds). Whether the
archived run is any good stays the CM's judgement.

The manifest naming the dossier is child-writable, so its `node_dir` is
checked for the dossier shape (`<dossier>/nodes/<node>` with `brief.md` at
the dossier root) rather than trusted; a child that rewrote it can at most
redirect the archive into another dossier's `validation/`, which the
printed destination shows.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import PmError
from . import git_ops
from . import sandbox as sandbox_mod

# Where each capture lands under `validation/<node>/`, as campaign-mode.md
# *Cleanup* and dossier-template.md's `validation/` layout name them.
HANDOFF_NAME = "HANDOFF.md"
STAGED_PATCH = "staged.patch"
STATUS_TEXT = "status.txt"
DIFF_PATCH = "diff.patch"
SANDBOX_TMP_NAME = "sandbox-tmp"
# A failed or incomplete copy names at most this many files; the rest are counted.
_NAMED_MAX = 5
# Diff options that keep both patches applicable and keep git from running
# anything a nested repository's configuration names: no external diff
# driver, no textconv filter, and a submodule shown as its commit line only
# (`diff.submodule=log` would run `git log` inside it).
_PATCH_OPTIONS = ("--binary", "--no-ext-diff", "--no-textconv", "--submodule=short")


@dataclass
class ArchiveOutcome:
    """What `archive_child` wrote and where, as facts and printable lines."""

    node: str
    # `<dossier>/validation/<node>/` and `<results root>/<node>/`.
    validation_dir: Path
    results_dir: Path
    # One line per step, in the order the steps ran, for the CLI to print.
    lines: list[str] = field(default_factory=list)


class ArchiveIncomplete(PmError):
    """An archive that stopped part-way: the message says what could not be
    captured or copied, and `outcome` holds the steps that completed first,
    so the caller can print both and keep the worktree."""

    def __init__(self, message: str, outcome: ArchiveOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


def archive_child(
    worktree: Path, manifest: dict[str, Any], results_root: Path, sandbox_tmpdir: Path | None
) -> ArchiveOutcome:
    """Archive the stopped child in `worktree` into its dossier's
    `validation/<node>/` and into `<results_root>/<node>/`; return what was
    written. ArchiveIncomplete, carrying the steps that did complete, when
    anything could not be captured, copied or checked, or the removal rule
    does not hold: the caller then keeps the worktree, and the sandbox temp
    dir, for a re-run.

    The dossier is found from the manifest's `node_dir` (`_dossier_of`), and
    the brief there names the git-ignored output directories
    (`sandbox.brief_output_directories`). Under `validation/<node>/`, in
    order: HANDOFF.md, `campaign/` and `runs/` from `.pm/`, `state/` from the
    state root `<worktree git dir>/pm/`, then the uncommitted work
    (`_capture_uncommitted`). Then each output directory present in the
    worktree, and `sandbox_tmpdir` when given and present, go to the results
    root; then the removal rule is checked (`_check_removal_rule`).
    """
    worktree = worktree.expanduser().resolve()
    dossier, node, brief = _dossier_of(manifest)
    try:
        output_dirs = sandbox_mod.brief_output_directories(brief.read_text(encoding="utf-8")) or []
    except (OSError, UnicodeDecodeError) as exc:
        raise PmError(f"the dossier brief {brief} could not be read ({exc}); nothing was archived") from exc
    results_root = results_root.expanduser().resolve()
    outcome = ArchiveOutcome(node=node, validation_dir=dossier / "validation" / node, results_dir=results_root / node)
    # A destination that overlaps the worktree or the sandbox temp dir would
    # copy a tree onto itself, or be removed with it once the archive
    # reported itself complete: refused before anything is written.
    for scheduled, label in ((worktree, "the worktree"), (sandbox_tmpdir, "the sandbox temp dir")):
        if scheduled is None:
            continue
        real = Path(os.path.realpath(scheduled))
        for destination, what in (
            (outcome.results_dir, "results directory"),
            (outcome.validation_dir, "validation directory"),
        ):
            # Compared as real paths: a link at either destination must not lead back inside.
            destination = Path(os.path.realpath(destination))
            if destination.is_relative_to(real) or real.is_relative_to(destination):
                raise PmError(
                    f"the {what} {destination} overlaps {label} ({scheduled}), which is removed after the archive;"
                    " name a results root outside it"
                )
    try:
        _capture_run(worktree, outcome)
        status_text, diff = _capture_uncommitted(worktree, output_dirs, outcome)
        _copy_outputs(worktree, output_dirs, sandbox_tmpdir, results_root, outcome)
        _check_removal_rule(status_text, diff, outcome)
    except (PmError, OSError) as exc:
        raise ArchiveIncomplete(f"{exc}; the worktree is kept", outcome) from exc
    return outcome


def _dossier_of(manifest: dict[str, Any]) -> tuple[Path, str, Path]:
    """(dossier root, node id, brief) from the manifest's `node_dir`.

    The node directory is `<dossier>/nodes/<node>`, where the dossier root is
    the git toplevel of that directory (`sandbox._git_toplevel`, the reading
    `launch-child` gave it) and holds `brief.md`. A manifest whose `node_dir`
    is null (a launcher in no repository), or that is not of that shape, is a
    PmError: there is no dossier to archive into, and a guessed one would put
    the run where no reconcile looks.
    """
    text = manifest.get("node_dir")
    if text is None:
        raise PmError(
            "the child's manifest records no node directory (node_dir is null: its launcher lay in no dossier"
            " repository), so there is no dossier to archive into"
        )
    if not isinstance(text, str) or not text or not Path(text).is_absolute():
        raise PmError(f"the child's manifest node_dir is not an absolute path: {text!r}; no dossier to archive into")
    node_dir = Path(text)
    if not node_dir.is_dir():
        raise PmError(f"the child's node directory {node_dir} does not exist; no dossier to archive into")
    root = sandbox_mod._git_toplevel(node_dir)
    real = Path(os.path.realpath(node_dir))
    if root is None or real != root / "nodes" / real.name:
        raise PmError(
            f"the child's node directory {node_dir} is not <dossier>/nodes/<node> in a dossier repository"
            f" (its git toplevel is {root or 'none'}); no dossier to archive into"
        )
    brief = root / sandbox_mod.BRIEF_NAME
    if not brief.is_file():
        raise PmError(f"the dossier {root} holds no {sandbox_mod.BRIEF_NAME}; no dossier to archive into")
    return root, real.name, brief


# --- The run: HANDOFF.md, .pm/campaign, .pm/runs, the state root --------------------


def _capture_run(worktree: Path, outcome: ArchiveOutcome) -> None:
    """HANDOFF.md, `.pm/campaign/`, `.pm/runs/` and the state root into
    `validation/<node>/`, in that order.

    The worktree's `.pm/` tree is child-writable, so each source directory
    beneath it must be a real directory reached through no symbolic link
    (`_real_directory`), and the copy keeps every link inside it as a link,
    never following one: the host must not read a file the child named
    through a link it planted (pm_paths' module docstring gives the rule
    for every host access under `.pm/`).
    """
    validation = outcome.validation_dir
    validation.mkdir(parents=True, exist_ok=True)
    handoff = worktree / HANDOFF_NAME
    if os.path.lexists(handoff):
        _copy_entry(handoff, validation / HANDOFF_NAME)
        outcome.lines.append(f"archived {handoff} -> {validation / HANDOFF_NAME}")
    else:
        outcome.lines.append(f"no {HANDOFF_NAME} in the worktree")
    for name in ("campaign", "runs"):
        source = worktree / ".pm" / name
        if not _real_directory(worktree, ".pm", name):
            outcome.lines.append(f"no {source}/ to archive")
            continue
        outcome.lines.extend(_copy_tree(source, validation / name, refuse_hard_links=True))
        outcome.lines.append(f"archived {source}/ -> {validation / name}/")
    state_root = git_ops.worktree_git_dir(worktree) / "pm"
    if state_root.is_dir():
        outcome.lines.extend(_copy_tree(state_root, validation / "state"))
        outcome.lines.append(f"archived state root {state_root}/ -> {validation / 'state'}/")
    else:
        outcome.lines.append(f"no state root {state_root}/ to archive")


def _real_directory(base: Path, *names: str) -> bool:
    """Whether `base/names...` is a directory reached through no symbolic
    link below `base`: False when a component is absent, PmError when one
    is a link or the last is not a directory."""
    path = base
    for name in names:
        path = path / name
        if not os.path.lexists(path):
            return False
        if os.path.islink(path):
            raise PmError(f"refused {path}: a symbolic link, which the archive never follows")
    if not path.is_dir():
        raise PmError(f"refused {path}: not a directory")
    return True


# --- The uncommitted work ----------------------------------------------------------


def _capture_uncommitted(worktree: Path, output_dirs: list[str], outcome: ArchiveOutcome) -> tuple[str, bytes]:
    """`staged.patch`, `status.txt`, `git add -A`, then `diff.patch`, in that
    order; returns the status text and the diff for the removal rule.

    `staged.patch` comes first, before anything is staged, so a partly staged
    file keeps its staged version. A re-run after a failure past `add -A`
    would find everything staged by that run, so when `staged.patch` and
    `status.txt` both exist already they are kept as the pre-staging capture
    they are (nothing changes the worktree of a stopped child in between,
    unless the CM does so by hand, and then it removes both first).

    These are raw git calls rather than `git_ops.git_status_text`, whose
    gitlink refusal would fire once `add -A` has recorded a nested
    repository: such a gitlink is acceptable beneath an output directory
    (its files are copied from there) and refused anywhere else
    (`_refuse_gitlinks`), before the diff runs. The status read ignores
    submodules, so it never recurses into one and runs its configuration.
    """
    validation = outcome.validation_dir
    staged_path, status_path = validation / STAGED_PATCH, validation / STATUS_TEXT
    if staged_path.is_file() and status_path.is_file():
        status_text = status_path.read_text(encoding="utf-8")
        outcome.lines.append(f"kept {staged_path} and {status_path} from an earlier run (captured before its add -A)")
    else:
        staged_path.write_bytes(_git_bytes(worktree, "diff", "--cached", *_PATCH_OPTIONS))
        outcome.lines.append(f"wrote {staged_path} ({staged_path.stat().st_size} bytes)")
        code, status_text, err = git_ops.git_result(worktree, "status", "--porcelain", "--ignore-submodules=all")
        if code != 0:
            raise PmError(f"git status in {worktree} failed: {err.strip() or 'no output'}")
        status_path.write_text(status_text, encoding="utf-8")
        outcome.lines.append(f"wrote {status_path} ({len(_changes(status_text))} changes listed)")
    code, _out, err = git_ops.git_result(worktree, "add", "-A")
    if code != 0:
        raise PmError(f"git add -A in {worktree} failed: {err.strip() or 'no output'}")
    outcome.lines.append(f"staged the worktree's uncommitted work (git add -A in {worktree})")
    _refuse_gitlinks(worktree, output_dirs)
    diff_path = validation / DIFF_PATCH
    diff = _git_bytes(worktree, "diff", "--cached", *_PATCH_OPTIONS, "HEAD")
    diff_path.write_bytes(diff)
    outcome.lines.append(f"wrote {diff_path} ({len(diff)} bytes)")
    return status_text, diff


def _git_bytes(worktree: Path, *args: str) -> bytes:
    """git's raw stdout (a patch must not be decoded, which would alter or
    reject non-UTF-8 content); PmError naming the command when git fails."""
    output = git_ops.git_bytes(worktree, *args)
    if output is None:
        raise PmError(f"git {' '.join(args)} in {worktree} failed")
    return output


def _refuse_gitlinks(worktree: Path, output_dirs: list[str]) -> None:
    """PmError naming each gitlink in the index outside the brief's output
    directories: `add -A` records a nested repository as a bare commit id,
    so `diff.patch` holds none of its content, and removing the worktree
    would lose it. One beneath an output directory is copied as files with
    that directory, so it is accepted. The index is read by
    `git_ops.gitlinks` (mode-160000 entries of `ls-files --stage`), which
    runs no repository configuration."""
    stray = [
        path
        for path in git_ops.gitlinks(worktree)
        if not any(path == directory or path.startswith(f"{directory}/") for directory in output_dirs)
    ]
    if stray:
        raise PmError(
            f"the index of {worktree} holds a nested repository outside the brief's git-ignored output directories"
            f" ({', '.join(stray)}): git add -A recorded it as a bare gitlink, so no patch holds its content; copy it"
            " by hand into the node's validation directory before the worktree is removed"
        )


def _changes(status_text: str) -> list[str]:
    return [line for line in status_text.splitlines() if line.strip()]


# --- The output: the brief's output directories and the sandbox temp dir -----------


def _copy_outputs(
    worktree: Path,
    output_dirs: list[str],
    sandbox_tmpdir: Path | None,
    results_root: Path,
    outcome: ArchiveOutcome,
) -> None:
    """Each output directory present in the worktree to
    `<results root>/<node>/<dir>/`, and the sandbox temp dir, when given and
    present, to `<results root>/<node>/sandbox-tmp/`; the results root is
    created when absent.

    An output directory reached through a symbolic link (a setup script's
    link to a shared volume, say) is not copied: `git worktree remove`
    deletes only the link, never its target, and the copy never follows a
    link the child could have planted.
    """
    results_root.mkdir(parents=True, exist_ok=True)
    for directory in output_dirs:
        source = worktree / directory
        if not os.path.lexists(source):
            outcome.lines.append(f"output directory {directory}/: not in the worktree")
            continue
        linked = _first_link(worktree, directory)
        if linked is not None:
            outcome.lines.append(
                f"output directory {directory}/: not copied, {linked} is a symbolic link (to"
                f" {os.readlink(linked)}); removing the worktree deletes only the link"
            )
            continue
        if not source.is_dir():
            raise PmError(f"output directory {source} is not a directory")
        target = outcome.results_dir / directory
        outcome.lines.extend(_copy_tree(source, target))
        outcome.lines.append(f"copied {source}/ -> {target}/")
    if sandbox_tmpdir is not None and os.path.lexists(sandbox_tmpdir):
        if os.path.islink(sandbox_tmpdir) or not sandbox_tmpdir.is_dir():
            raise PmError(f"refused sandbox temp dir {sandbox_tmpdir}: not a directory")
        target = outcome.results_dir / SANDBOX_TMP_NAME
        outcome.lines.extend(_copy_tree(sandbox_tmpdir, target))
        outcome.lines.append(f"copied sandbox temp dir {sandbox_tmpdir}/ -> {target}/")


def _first_link(base: Path, relative: str) -> Path | None:
    """The first component of `base/relative` that is a symbolic link, or None."""
    path = base
    for name in relative.split("/"):
        path = path / name
        if os.path.islink(path):
            return path
    return None


# --- Copying -------------------------------------------------------------------------


def _copy_entry(source: Path, target: Path) -> None:
    """One child-writable file, a symbolic link copied as the link itself,
    replacing what `target` held; PmError for anything else (a directory
    where HANDOFF.md should be, say), for a hard link (`_refuse_hard_link`),
    and when the copy is short."""
    if os.path.lexists(target) and not target.is_dir():
        target.unlink()
    if os.path.islink(source):
        os.symlink(os.readlink(source), target)
        return
    source_stat = os.lstat(source)
    if not stat.S_ISREG(source_stat.st_mode):
        raise PmError(f"refused {source}: not a regular file or a symbolic link")
    _refuse_hard_link(source, source_stat)
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise PmError(f"could not copy {source} to {target}: {exc}") from exc
    if os.lstat(target).st_size != os.lstat(source).st_size:
        raise PmError(f"the copy of {source} to {target} is incomplete")


def _refuse_hard_link(source: str | Path, source_stat: os.stat_result) -> None:
    """The rule every host read under `.pm/` follows (`pm_paths`): a file
    with more than one link may be another file the child could not
    otherwise hand the host, so it is refused, not copied."""
    if source_stat.st_nlink > 1:
        raise PmError(f"refused {source}: a hard link ({source_stat.st_nlink} links), which the archive never copies")


def _copy_tree(source: Path, target: Path, *, refuse_hard_links: bool = False) -> list[str]:
    """Copy the directory `source` into `target`, merging with and
    overwriting what an earlier run left there; returns a line for each
    entry skipped as neither a regular file, a directory nor a link (a
    socket or a FIFO, which cannot be copied and holds no output).

    `refuse_hard_links` applies `_refuse_hard_link` to every file: on for
    the child-writable `.pm/` trees, off for an output directory and the
    sandbox temp dir, where a clone made from a local path hard-links its
    git objects and the destination is the owner's results root.

    `symlinks=True` copies every link as a link and never follows it (the
    source may be child-writable; see `_capture_run`), and `dirs_exist_ok`
    merges into a partial earlier copy. Two things `copytree` alone would
    trip on in such a merge are handled here: a link an earlier copy made
    is removed before it is made again (`os.symlink` refuses an existing
    name, and a directory copied over a link would write through it), and a
    file an earlier copy made is unlinked before it is copied again (a
    read-only one, such as a nested repository's objects, refuses an
    overwrite). PmError naming the source and the target when the copy
    fails or is incomplete (`_missing`).
    """
    skipped: list[str] = []

    def copy_file(src: str, dst: str) -> str:
        source_stat = os.lstat(src)
        if not stat.S_ISREG(source_stat.st_mode):
            skipped.append(f"skipped {src}: not a regular file, a directory or a symbolic link")
            return dst
        if refuse_hard_links:
            _refuse_hard_link(src, source_stat)
        if os.path.lexists(dst) and not os.path.isdir(dst):
            os.unlink(dst)
        return shutil.copy2(src, dst)

    _clear_links(source, target)
    try:
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True, copy_function=copy_file)
    except (shutil.Error, OSError) as exc:
        raise PmError(f"could not copy {source} to {target}: {_copy_failure(exc)}") from exc
    missing = _missing(source, target)
    if missing:
        raise PmError(f"the copy of {source} to {target} is incomplete: {_named(missing)}")
    return skipped


def _clear_links(source: Path, target: Path) -> None:
    """Remove each link in `target` whose name an entry of `source` also
    has, so a merge never writes through, or collides with, a link an
    earlier copy made."""
    for directory, dirnames, filenames in os.walk(source):
        relative = Path(directory).relative_to(source)
        for name in (*dirnames, *filenames):
            existing = target / relative / name
            if os.path.islink(existing):
                existing.unlink()


def _missing(source: Path, target: Path) -> list[str]:
    """Every regular file under `source` that is absent from `target` or has
    another size there, relative to `source`: the completeness check."""
    missing: list[str] = []
    for directory, _dirnames, filenames in os.walk(source):
        relative = Path(directory).relative_to(source)
        for name in filenames:
            original = os.lstat(Path(directory) / name)
            if not stat.S_ISREG(original.st_mode):
                continue
            try:
                copy = os.lstat(target / relative / name)
            except OSError:
                missing.append(str(relative / name))
                continue
            if not stat.S_ISREG(copy.st_mode) or copy.st_size != original.st_size:
                missing.append(str(relative / name))
    return missing


def _copy_failure(exc: BaseException) -> str:
    """A failed copy's reason: `shutil.Error` carries one (source, target,
    why) per failed entry, any other OSError is its own message."""
    if isinstance(exc, shutil.Error) and exc.args and isinstance(exc.args[0], list):
        return _named([f"{src}: {why}" for src, _dst, why in exc.args[0]])
    return str(exc)


def _named(items: list[str]) -> str:
    shown = "; ".join(items[:_NAMED_MAX])
    return shown if len(items) <= _NAMED_MAX else f"{shown}; and {len(items) - _NAMED_MAX} more"


# --- The removal rule ---------------------------------------------------------------


def _check_removal_rule(status_text: str, diff: bytes, outcome: ArchiveOutcome) -> None:
    """campaign-mode.md *Cleanup*'s rule for `git worktree remove --force`,
    which discards modified and untracked files: allowed only when
    `diff.patch` is non-empty whenever `status.txt` lists any change, so no
    change the status saw is lost with the worktree. PmError otherwise."""
    changes = _changes(status_text)
    if changes and not diff.strip():
        raise PmError(
            f"{STATUS_TEXT} lists {len(changes)} changes but {DIFF_PATCH} is empty, so removing the worktree would"
            " lose them"
        )
    if changes:
        outcome.lines.append(f"removal rule holds: {len(changes)} changes listed, {DIFF_PATCH} holds them")
    else:
        outcome.lines.append("removal rule holds: no uncommitted change")

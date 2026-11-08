"""Campaign-mode child PM control: launch, watch, answer and stop.

A Campaign Manager (CM) runs one ordinary child PM per node plan, in the
node's own git worktree and in a tmux session on a private socket. The four
commands here (`launch-child`, `wait-child`, `answer-child`, `stop-child`)
are that loop's mechanics and nothing more: none takes a run token, none
writes PM run state, and each decides nothing — whether a child's run is
good is the CM's reading of its report and the node diff.

Everything a command needs to find the child again lives in the launch
manifest, `<worktree>/.pm/campaign/child.json`, which `launch_child` writes
the moment the session exists. The child's own channels sit beside it: the
CM question file and its numbered answers, the completion file, the pane
captures and the launcher copy.

Each command points `PM_TMUX_SOCKET` at the child's socket for its own
`sessions` calls (which read it through `sessions.tmux_argv`) and restores
the caller's value before returning, so an in-process caller keeps its own
server. The tmux calls that name their socket directly are
`sessions.list_sessions_on`, `sessions.kill_server` and the server
environment pair `sessions.set_server_environment`/`server_environment`.

The manifest is child-writable, so the server it names is not trusted to be
the child's: `launch-child` marks the server at launch and `stop-child` kills
only a server carrying this worktree's mark (see `stop_child`).

With `--sandbox`, the child's server runs under `srt` (see `sandbox`), so the
child PM and everything it starts are confined to the policy derived for its
worktree. The host-only control directory `<common git dir>/pm-sandbox/<socket>/`
holds that policy, the operator's attach policy and the server's record;
`stop-child` finds it from the worktree and the socket, never from a path the
manifest names, and tears it down with the server.

A child's `run.json` files are read here without a token, so what they say
is unverified: `wait-child` and `stop-child` print it labelled as such, as a
cross-check on the child's own completion file, never as a fact.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import PmError
from . import git_ops
from . import pm_paths
from . import profiles
from . import prompts
from . import sandbox as sandbox_mod
from . import sessions
from . import slice_ops
from . import state as state_mod

_WAIT_POLL_SECONDS = 2.0
_TAIL_LINES = 40

_SOCKET_ENV_VAR = "PM_TMUX_SOCKET"
# Required in every manifest, in this order. A sandboxed child's manifest
# also carries a `sandbox` block after these (policy, policy_sha256, socket,
# control_dir), for the reader's convenience only: nothing here trusts a path
# it names.
_MANIFEST_FIELDS = ("socket", "session", "branch", "base", "worktree", "launcher", "launched_at")
# The files `launch-child --sandbox` writes in the control directory.
_POLICY_FILE = "policy.json"
_ATTACH_POLICY_FILE = "attach-policy.json"
_SRT_LOG_FILE = "srt.log"
# tmux rejects `.` and `:` in a session name, and a `-t` target containing
# `.` parses as window.pane, so a branch is reduced to this set for names.
_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
# tmux puts a `-L` socket at `/private/tmp/tmux-<uid>/<name>` on macOS, where
# a Unix socket path is limited to 104 bytes: an 84-character name fails with
# "File name too long". The branch component is cut so the default socket
# (`pm-child-<b>-<6 hex>`, 48 characters at most) fits, and an explicit name
# is capped with room to spare for a long uid or temporary directory.
_NAME_COMPONENT_MAX = 32
_SOCKET_NAME_MAX = 64
# What a child's server may host: its own `campaign-*` session and the
# `pm-*` Developer sessions the child launches. Anything else is a bystander.
_OWNED_SESSION_RE = re.compile(r"^(campaign|pm)-")
# Matches `cm-answer-<n>.md`; `answer-child --redeliver` re-sends the highest `<n>`.
_ANSWER_FILE_RE = re.compile(r"^cm-answer-(\d+)\.md$")
# The server-environment mark `launch-child` sets on a child's server: the
# worktree it was launched for. `stop-child` kills no server without it.
_OWNER_ENV_VAR = "PM_CHILD_WORKTREE"


# --- Paths and names -----------------------------------------------------------


def campaign_dir(worktree: Path) -> Path:
    """`<worktree>/.pm/campaign/`: the manifest and every child channel file.

    The directory is in the sandbox-writable `.pm/` tree, so every host read
    and write of a file in it goes through `pm_paths`' guarded helpers
    (`read_pm_text`, `write_pm_text`, `pm_file_present`, ...), which refuse a
    planted symlink or hard link; see that module's docstring.
    """
    return slice_ops.pm_dir(worktree) / "campaign"


def manifest_path(worktree: Path) -> Path:
    return campaign_dir(worktree) / "child.json"


def name_component(branch: str) -> str:
    """`branch` with every character outside `[A-Za-z0-9_-]` replaced by `-`."""
    return _NAME_UNSAFE_RE.sub("-", branch)


def child_names(branch: str, suffix: str, socket: str | None = None) -> tuple[str, str]:
    """(session, socket) for a child on `branch` with hex `suffix`.

    The session is `campaign-<b>-<suffix>`, deliberately outside `pm-*`, the
    shape the unscoped `stop --scavenge` sweep kills. The socket is `socket`
    when given, else `pm-child-<b>-<suffix>`: one server per child, so two
    nodes or two repositories never share one. `<b>` is `name_component`
    cut to 32 characters (see `_NAME_COMPONENT_MAX`); the random suffix
    still tells apart two branches that share a long prefix.
    """
    component = name_component(branch)[:_NAME_COMPONENT_MAX]
    return f"campaign-{component}-{suffix}", socket or f"pm-child-{component}-{suffix}"


def check_socket_name(socket: str, *, what: str) -> None:
    """Refuse a socket name that is blank, contains whitespace or `/`, is
    `default`, or is longer than 64 characters.

    A blank name would leave `PM_TMUX_SOCKET` effectively unset, and
    `default` names the unscoped default server outright: either puts the
    child on the operator's own server, which `stop-child`'s `kill-server`
    would then destroy. A longer name risks the socket path limit (see
    `_SOCKET_NAME_MAX`).
    """
    if not socket.strip():
        raise PmError(f"{what} must be non-blank")
    if any(character.isspace() for character in socket):
        raise PmError(f"{what} must not contain whitespace: {socket!r}")
    if "/" in socket:
        raise PmError(f"{what} must not contain '/': {socket!r}")
    if socket == "default":
        raise PmError(f"{what} must not be 'default', the unscoped tmux server the operator's own sessions use")
    if len(socket) > _SOCKET_NAME_MAX:
        raise PmError(
            f"{what} must be at most {_SOCKET_NAME_MAX} characters (a tmux socket path is length-limited), "
            f"got {len(socket)}: {socket!r}"
        )


def _tmux_socket(socket: str) -> AbstractContextManager[None]:
    """Point `PM_TMUX_SOCKET` at `socket` for the block (see `sessions.scoped_environ`)."""
    return sessions.scoped_environ(_SOCKET_ENV_VAR, socket)


def load_manifest(worktree: Path) -> dict[str, Any]:
    """The launch manifest; PmError naming the file when it is missing,
    unreadable, malformed, or carries a socket `launch-child` would refuse."""
    path = manifest_path(worktree)
    try:
        data = json.loads(pm_paths.read_pm_text(worktree, path))
    except (OSError, PmError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PmError(f"child manifest could not be read: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise PmError(f"child manifest is not a JSON object: {path}")
    missing = [key for key in _MANIFEST_FIELDS if not isinstance(data.get(key), str) or not data[key]]
    if missing:
        raise PmError(f"child manifest lacks a non-empty string for {', '.join(missing)}: {path}")
    check_socket_name(data["socket"], what=f"child manifest socket in {path}")
    return data


# --- Tokenless run reads --------------------------------------------------------


@dataclass
class ChildRun:
    """One `run.json` under the child worktree's state root, read without a
    token: `status` is `unreadable` when the file cannot be read or parsed."""

    run_id: str
    status: str
    stop_reason: str | None = None
    reviewer_pgids: list[int] = field(default_factory=list)


def read_child_runs(worktree: Path) -> list[ChildRun]:
    """Every run under the worktree's state root, by run directory name.

    PmError only when the state root itself cannot be located: the worktree
    is gone, or is not the root of a git worktree — git would otherwise find
    an enclosing repository and this would read that repository's runs. An
    individual unreadable file is a `ChildRun` with status `unreadable`,
    never an error.
    """
    toplevel = git_ops.resolve_repo(worktree)
    if toplevel != worktree.resolve():
        raise PmError(f"{worktree} is not the root of a git worktree (git resolves it to {toplevel})")
    root = state_mod.state_root(worktree)
    runs: list[ChildRun] = []
    for run_json in sorted(root.glob("*/run.json")):
        run_id = run_json.parent.name
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            runs.append(ChildRun(run_id=run_id, status="unreadable"))
            continue
        if not isinstance(data, dict):
            runs.append(ChildRun(run_id=run_id, status="unreadable"))
            continue
        current = data.get("current_slice")
        pgids = current.get("reviewer_pids") if isinstance(current, dict) else None
        runs.append(
            ChildRun(
                run_id=run_id,
                status=str(data.get("status")),
                stop_reason=data.get("stop_reason") if isinstance(data.get("stop_reason"), str) else None,
                reviewer_pgids=[pgid for pgid in pgids if isinstance(pgid, int)] if isinstance(pgids, list) else [],
            )
        )
    return runs


def _runs_or_error(worktree: Path) -> tuple[list[ChildRun], str | None]:
    """`read_child_runs`, or no runs and why: a report on a child never fails
    because the child's state root cannot be read."""
    try:
        return read_child_runs(worktree), None
    except (PmError, OSError) as exc:
        return [], str(exc)


# --- launch-child ---------------------------------------------------------------


@dataclass
class LaunchChildOutcome:
    session: str
    socket: str
    worktree: Path
    launcher: Path
    branch: str
    base: str
    # The command the operator watches the child with: a plain `tmux attach`,
    # or for a sandboxed child the same attach run under the attach policy.
    watch: str
    # Set for a sandboxed child: the host-only control directory.
    control_dir: Path | None = None
    # What `sandbox.load_pinned_policy` warned about, for the caller to print.
    sandbox_warnings: list[str] = field(default_factory=list)
    # The new worktree's own git directory (where its run state lives), and the
    # launcher's node directory with the dossier HEAD it was clean at (None
    # when the launcher lies in no repository).
    git_dir: Path | None = None
    node_dir: Path | None = None
    dossier_commit: str | None = None


@dataclass
class _ChildLaunch:
    """What `launch_child`'s preconditions resolved, for the steps that create things."""

    repo: Path
    branch: str
    base_commit: str
    worktree: Path
    prompt_bytes: bytes
    command: str
    executable: str
    expected_model_display: str | None
    session: str
    socket: str
    setup: Path | None
    # Set only for a sandboxed launch (`--sandbox`): the base policy's real
    # path, its parsed content and SHA-256, its warnings, the `srt` version,
    # the control directory, and the launcher's directory (a derived read).
    sandbox: Path | None = None
    base_policy: dict[str, Any] | None = None
    policy_sha256: str | None = None
    sandbox_warnings: list[str] = field(default_factory=list)
    srt_version: str | None = None
    control_dir: Path | None = None
    launcher_dir: Path | None = None
    # The launcher's directory and the dossier HEAD, when it lies in a repository.
    node_dir: Path | None = None
    dossier_commit: str | None = None


# A worktree setup script that has not finished in this long is hung, and a
# hung setup would otherwise hang the Campaign Manager's launch forever.
SETUP_TIMEOUT_SECONDS = 600


def _left_behind(branch: str, worktree: Path) -> str:
    return (
        f"branch {branch} and worktree {worktree} are left behind; remove them by hand "
        f"(git worktree remove {worktree}; git branch -D {branch})"
    )


def launch_child(
    *,
    repo: Path,
    base: str,
    branch: str,
    worktree: Path,
    prompt_file: Path,
    harness: str,
    model: str | None = None,
    effort: str | None = None,
    socket: str | None = None,
    harness_command: str | None = None,
    setup: Path | None = None,
    sandbox: Path | None = None,
) -> LaunchChildOutcome:
    """Create the node branch and worktree, then launch a child PM in tmux.

    Every precondition is checked before anything is created, among them
    that no tmux server on the chosen socket hosts a session. Then
    `git worktree add -b <branch> <worktree> <base commit>`, `/HANDOFF.md` in
    the git exclude file the worktree reads (verified with `git
    check-ignore`), the self-ignoring `.pm/.gitignore`, `.pm/campaign/` and
    a byte copy of the prompt file as `launcher.md`; then `setup`, when
    given, runs as `sh <setup>` inside the new worktree with its output kept
    in `.pm/campaign/setup.log`, so a product's git-ignored scaffolding (data
    links, a virtualenv link) is in place before any session starts there,
    and a non-zero exit or a hang fails the launch; then the session starts
    as `start_slice` starts a Developer's, its server is marked as this
    child's (`PM_CHILD_WORKTREE`), the manifest is written, readiness is
    awaited, and the one-line child launch pointer is injected.

    With `sandbox`, an `srt` base policy, the server is sandboxed: after
    `setup` and before the session, the control directory and the per-run
    temp directory are created, the effective policy (`pm_inside`, the
    launcher's directory readable) and the attach policy are derived and
    written there, the server is started under `srt` and verified by its
    probes, and its record is written; the session is then created through
    that server, the manifest gains a `sandbox` block, and `watch` runs the
    attach under the attach policy. When the launcher's dossier brief pins a
    policy (`## Sandbox`), `sandbox` is required and must match the pin.

    The launcher's directory (the node directory of the dossier) must be
    committed: when it lies in a git repository, uncommitted or untracked files
    there refuse the launch, naming them, and the manifest records that
    directory and the dossier HEAD (`node_dir`, `dossier_commit`). A launcher
    in no repository records both as null and is not refused.

    There is no rollback: a failure after the worktree exists names the
    branch and worktree left behind for the CM to remove by hand, and a
    failure after the session started also captures the pane to `pane.txt`
    when it can, kills the child's private server (which may already host
    Developers the child launched), reports whether that kill was confirmed,
    and keeps any manifest written, so `stop-child` can be re-run against it.
    A sandboxed server is torn down instead (`sandbox.stop_server`: the
    server, its temp and its control directories) wherever a failure after
    it started would kill it; a failure before it started removes only the
    directories this launch created (see `_start_child_session`).
    """
    launch = _check_launch_preconditions(
        repo=repo,
        base=base,
        branch=branch,
        worktree=worktree,
        prompt_file=prompt_file,
        harness=harness,
        model=model,
        effort=effort,
        socket=socket,
        harness_command=harness_command,
        setup=setup,
        sandbox=sandbox,
    )
    launcher = _create_node_worktree(launch)
    _run_worktree_setup(launch)
    _start_child_session(launch, launcher)
    # `-t` is the point: a bare `attach` lands on the most recent session,
    # usually a Developer rather than the child PM. A sandboxed server is
    # attached to only from inside the attach policy, where a command a pane
    # makes the client run (`detach-client -E`) can write nothing.
    watch = f"tmux -L {shlex.quote(launch.socket)} attach -t {shlex.quote(launch.session)}"
    if launch.control_dir is not None:
        watch = f"srt -s {shlex.quote(str(launch.control_dir / _ATTACH_POLICY_FILE))} {watch}"
    return LaunchChildOutcome(
        git_dir=git_ops.worktree_git_dir(launch.worktree),
        node_dir=launch.node_dir,
        dossier_commit=launch.dossier_commit,
        session=launch.session,
        socket=launch.socket,
        worktree=launch.worktree,
        launcher=launcher,
        branch=launch.branch,
        base=launch.base_commit,
        watch=watch,
        control_dir=launch.control_dir,
        sandbox_warnings=launch.sandbox_warnings,
    )


def _check_launch_preconditions(
    *,
    repo: Path,
    base: str,
    branch: str,
    worktree: Path,
    prompt_file: Path,
    harness: str,
    model: str | None,
    effort: str | None,
    socket: str | None,
    harness_command: str | None,
    setup: Path | None = None,
    sandbox: Path | None = None,
) -> _ChildLaunch:
    """Every refusal `launch_child` makes before anything is created: the
    inputs first (the dossier brief's sandbox pin and the base policy among
    them), then the environment (tmux, the harness executable, the socket's
    server, for a sandboxed launch `srt` and the platform, the socket's
    sandbox control directory for every launch, and for a sandboxed launch
    its temp directory)."""
    repo = git_ops.resolve_repo(repo)
    returncode, stdout, stderr = git_ops.git_result(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if returncode != 0 or not stdout.strip():
        detail = stderr.strip()
        raise PmError(f"--base {base!r} does not resolve to a commit in {repo}" + (f": {detail}" if detail else ""))
    base_commit = stdout.strip()
    # A tracked HANDOFF.md cannot be hidden by an exclude line, so the child
    # PM's own handoff would be a modification in its node diff.
    if git_ops.git_result(repo, "cat-file", "-e", f"{base_commit}:HANDOFF.md")[0] == 0:
        raise PmError("the base commit tracks HANDOFF.md; a child PM's handoff must be an untracked file")
    returncode, _stdout, _stderr = git_ops.git_result(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if returncode == 0:
        raise PmError(f"branch {branch!r} already exists in {repo}; a child launches on a new branch")
    # Checked before resolving: a dangling symlink resolves to its missing
    # target, and both tests would then pass on a path that exists.
    expanded = worktree.expanduser()
    if expanded.exists() or expanded.is_symlink():
        raise PmError(f"worktree path already exists: {expanded}")
    worktree = expanded.resolve()
    try:
        prompt_bytes = prompt_file.read_bytes()
        prompt_text = prompt_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PmError(f"prompt file could not be read: {prompt_file} ({exc})") from exc
    if not prompt_text.strip():
        raise PmError(f"prompt file is blank: {prompt_file}")
    if setup is not None:
        setup = setup.expanduser()
        try:
            setup_text = setup.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PmError(f"setup script could not be read: {setup} ({exc})") from exc
        if not setup_text.strip():
            raise PmError(f"setup script is blank: {setup}")
        setup = setup.resolve()
    # The launcher's dossier brief: when it pins a sandbox policy, the launch
    # must be sandboxed under exactly that file, so a CM cannot forget it.
    brief = sandbox_mod.find_brief(prompt_file)
    pin: str | None = None
    if brief is not None:
        try:
            pin = sandbox_mod.brief_sandbox_pin(brief.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise PmError(f"the launcher's dossier brief could not be read: {brief} ({exc})") from exc
        except PmError as exc:
            raise PmError(f"{exc} (in {brief})") from exc
    node_dir, dossier_commit = _committed_node_dir(prompt_file)
    if pin is not None and sandbox is None:
        raise PmError(
            f"the launcher's dossier brief {brief} pins a sandbox policy, SHA-256 `{pin}`; launch with --sandbox "
            "naming that file"
        )
    base_policy: dict[str, Any] | None = None
    policy_sha256: str | None = None
    sandbox_warnings: list[str] = []
    if sandbox is not None:
        sandbox = sandbox.expanduser()
        # The policy the server will run under is the one hashed (and pinned): one read.
        base_policy, sandbox_warnings, policy_sha256 = sandbox_mod.load_pinned_policy(sandbox)
        if pin is not None and policy_sha256 != pin:
            raise PmError(
                f"--sandbox {sandbox} has SHA-256 {policy_sha256}, but the launcher's dossier brief {brief} pins "
                f"`{pin}`; launch with the pinned policy"
            )
        sandbox = sandbox.resolve()
    # `is not None`, not truthiness: an explicit blank override is refused as
    # a blank command below rather than silently replaced by the composed one.
    if harness_command is None and harness not in profiles.SUPPORTED_HARNESSES:
        supported = ", ".join(profiles.SUPPORTED_HARNESSES)
        raise PmError(f"--harness {harness!r} is not supported (one of {supported}), and no --harness-command is given")
    if socket is not None:
        check_socket_name(socket, what="--socket")

    if shutil.which("tmux") is None:
        raise PmError("tmux is required to launch a child; install it")
    command, expected_model_display = profiles.launch_command(
        harness, override=harness_command, model=model, effort=effort, sandboxed=sandbox is not None
    )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise PmError(f"harness command could not be parsed: {command!r} ({exc})") from exc
    if not argv:
        raise PmError("harness command is blank")
    executable = argv[0]
    if shutil.which(executable) is None:
        raise PmError(f"harness executable not found on PATH: {executable}")

    session, socket = child_names(branch, secrets.token_hex(3), socket)
    # The server must be private to this child: stop-child and the launch
    # failure guard kill it whole, so joining a live one — the caller's own,
    # another child's — would destroy sessions this child does not own. A
    # server that cannot be listed (an over-long socket path, say) raises
    # here: a pass would be a guess.
    existing = sessions.list_sessions_on(socket)
    if existing:
        raise PmError(
            f"tmux server {socket} already hosts {len(existing)} session(s); a child needs a private server "
            "(omit --socket, or name one with no server)"
        )
    srt_version: str | None = None
    if base_policy is not None:
        # `check_binaries` refuses first when srt is absent.
        sandbox_mod.check_binaries(base_policy)
        srt_version = sandbox_mod.srt_version()
        if platform.system() != "Darwin":
            raise PmError("sandboxing is implemented for macOS only")
        # An empty server passes the session check above, but srt cannot
        # start a sandboxed one beside it.
        if sessions.server_running(socket, timeout=sandbox_mod.START_DEADLINE_SECONDS):
            raise PmError(f"a tmux server already answers on socket {socket!r}; a sandboxed child needs a new one")
    # A control directory for the socket, left by an earlier sandboxed child,
    # is refused for an unsandboxed launch too: stop-child would take this
    # child for a sandboxed one and check it against that record. A socket
    # name the sandbox refuses can have none, and only a sandboxed launch
    # needs one.
    common = Path(git_ops.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    control_dir: Path | None
    try:
        control_dir = sandbox_mod.control_dir(common, socket)
    except PmError:
        if base_policy is not None:
            raise
        control_dir = None
    if control_dir is not None and os.path.lexists(control_dir):
        raise PmError(
            f"sandbox control dir already exists: {control_dir}; stop-child of the child on socket {socket} "
            "removes it, or remove it by hand once no server runs there"
        )
    if base_policy is None:
        control_dir = None
    else:
        tmpdir = sandbox_mod.run_tmpdir(socket)
        if os.path.lexists(tmpdir):
            raise PmError(f"sandbox temp dir already exists: {tmpdir}; another child may own socket {socket}")
    return _ChildLaunch(
        repo=repo,
        branch=branch,
        base_commit=base_commit,
        worktree=worktree,
        prompt_bytes=prompt_bytes,
        command=command,
        executable=executable,
        expected_model_display=expected_model_display,
        session=session,
        socket=socket,
        setup=setup,
        sandbox=sandbox,
        base_policy=base_policy,
        policy_sha256=policy_sha256,
        sandbox_warnings=sandbox_warnings,
        srt_version=srt_version,
        control_dir=control_dir,
        launcher_dir=prompt_file.expanduser().resolve().parent,
        node_dir=node_dir,
        dossier_commit=dossier_commit,
    )


def _committed_node_dir(prompt_file: Path) -> tuple[Path | None, str | None]:
    """`(node dir, dossier HEAD)` for a launcher whose directory is committed.

    The node directory is the launcher's own directory. `(None, None)` when it
    lies in no git repository (git's exit 128, "not a git repository"); every
    other git failure raises, because a broken repository must not read as none.
    PmError naming the files when the directory holds uncommitted or untracked
    ones: the plan the child is given must be the plan on record.
    """
    node_dir = prompt_file.expanduser().resolve().parent
    # `LC_ALL=C`: the "not a git repository" text below must not be localised.
    code, _top, err = git_ops.git_result(node_dir, "rev-parse", "--show-toplevel", env={"LC_ALL": "C"})
    if code == 128 and "not a git repository" in err:
        return None, None
    if code != 0:
        raise PmError(
            f"git could not say which repository {node_dir} lies in (exit {code}): {err.strip() or 'no output'}; "
            "refusing to read it as no repository"
        )
    git_ops.refuse_gitlinks(node_dir)
    code, status, err = git_ops.git_result(
        node_dir, "status", "--short", "--untracked-files=all", "--no-renames", "--", "."
    )
    if code != 0:
        raise PmError(f"git status of {node_dir} failed: {err.strip() or status.strip() or 'no output'}")
    dirty = [line for line in status.splitlines() if line.strip()]
    if dirty:
        raise PmError(
            f"the launcher's node directory {node_dir} has uncommitted or untracked files; commit them before "
            "launching, so the child's plan is the recorded one: " + "; ".join(dirty)
        )
    # `status` never lists an ignored file, so the launcher itself must be tracked.
    code, _out, err = git_ops.git_result(node_dir, "ls-files", "--error-unmatch", "--", prompt_file.name)
    if code != 0:
        raise PmError(
            f"the launcher {prompt_file} is not tracked in its repository (ignored, or never added); commit it "
            f"before launching, so the child's plan is the recorded one: {err.strip() or 'no output'}"
        )
    return node_dir, git_ops.git_head(node_dir)


def _create_node_worktree(launch: _ChildLaunch) -> Path:
    """The node worktree and its campaign files; returns the launcher path.
    From here every failure names what is left behind."""
    branch, worktree = launch.branch, launch.worktree
    try:
        git_ops.git(launch.repo, "worktree", "add", "-b", branch, str(worktree), launch.base_commit)
    except PmError as exc:
        # git normally leaves nothing behind on failure; say so when it did.
        leftovers = []
        if git_ops.git_result(launch.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")[0] == 0:
            leftovers.append(f"branch {branch} exists (git branch -D {branch})")
        if worktree.exists():
            leftovers.append(f"path {worktree} exists (git worktree remove {worktree}, or remove it by hand)")
        partial = f"; left behind: {'; '.join(leftovers)}" if leftovers else ""
        raise PmError(f"git worktree add failed: {exc}{partial}") from exc
    directory = campaign_dir(worktree)
    launcher = directory / "launcher.md"
    try:
        _exclude_handoff(worktree)
        slice_ops.write_pm_gitignore(worktree)
        pm_paths.write_pm_bytes(worktree, launcher, launch.prompt_bytes)
    except (OSError, PmError) as exc:
        raise PmError(f"child setup failed ({exc}); {_left_behind(branch, worktree)}") from exc
    return launcher


def _run_worktree_setup(launch: _ChildLaunch) -> None:
    """Run the launch's setup script, if any, inside the new worktree.

    `sh <script>` with the worktree as its working directory, the caller's
    environment less `sessions.SEAT_ENV_DROPPED`, and its own process group; its stdout
    and stderr go straight to `.pm/campaign/setup.log`, so the wait depends
    on the shell exiting and never on a pipe closing, and a log that cannot
    be opened fails the launch before the script runs, because the log is
    the only evidence of what ran. On a timeout (`SETUP_TIMEOUT_SECONDS`),
    or any interruption of the wait, everything still in the script's
    process group is killed before the error propagates; a process that left
    the group is outside this reach, and the message says so. A non-zero
    exit, a timeout, a failure to run at all, or a worktree the script left
    dirty outside `.pm/` (an untracked or modified file a child's own
    clean-worktree check would refuse) fails the launch before any session
    exists, naming the branch and worktree left behind. A job the script
    leaves running in the background after exiting 0 is not killed and is
    outside the dirty check, so a setup script must wait for everything it
    starts.
    """
    if launch.setup is None:
        return
    run_setup_script(
        launch.setup,
        launch.worktree,
        log=campaign_dir(launch.worktree) / "setup.log",
        left_behind=_left_behind(launch.branch, launch.worktree),
    )


def run_setup_script(script: Path, worktree: Path, *, log: Path, left_behind: str) -> None:
    """Run `sh <script>` inside `worktree` as `_run_worktree_setup` describes:
    output to `log`, own process group, no `sessions.SEAT_ENV_DROPPED`, killed on timeout
    or interruption, and a failure, a timeout or a dirty worktree afterwards
    raises `PmError` ending in `left_behind`. `campaign-preflight` runs the
    same code in a throwaway worktree, so what it checks is what a launch
    will do."""
    env = sessions.seat_environ()
    try:
        handle = log.open("w", encoding="utf-8")
    except OSError as exc:
        raise PmError(f"worktree setup log could not be opened at {log} ({exc}); {left_behind}") from exc
    with handle:
        try:
            process = subprocess.Popen(
                ["sh", str(script)],
                cwd=worktree,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise PmError(f"worktree setup {script} could not run ({exc}); {left_behind}") from exc
        try:
            returncode = _wait_for_setup(process)
        except subprocess.TimeoutExpired:
            _kill_process_group(process.pid)
            process.wait()
            raise PmError(
                f"worktree setup {script} did not finish in {SETUP_TIMEOUT_SECONDS}s; everything still in its process "
                f"group was killed, and a process that left the group may still be running (output in {log}); "
                f"{left_behind}"
            ) from None
        except BaseException:
            # An interrupt or any other failure of the wait: never leave the
            # script's group running behind a launcher that has gone.
            _kill_process_group(process.pid)
            process.wait()
            raise
    if returncode != 0:
        tail = log_tail(log)
        detail = f": {' | '.join(tail)}" if tail else ""
        raise PmError(f"worktree setup {script} exited {returncode}{detail} (full output in {log}); {left_behind}")
    try:
        dirty = git_ops.meaningful_status_lines(git_ops.git_status_text(worktree))
    except PmError as exc:
        raise PmError(f"worktree setup ran but git status failed afterwards ({exc}); {left_behind}") from exc
    if dirty:
        raise PmError(
            f"worktree setup {script} left the worktree dirty outside .pm/ ({'; '.join(dirty)}); a child PM refuses to "
            f"start on a dirty worktree, so setup must create only git-ignored files; {left_behind}"
        )


def _wait_for_setup(process: subprocess.Popen) -> int:
    """Wait for the setup shell to exit; separate so a test can interrupt the wait."""
    return process.wait(timeout=SETUP_TIMEOUT_SECONDS)


def _kill_process_group(pid: int) -> None:
    """Kill the process group `pid` leads; a group already gone is not an error."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def log_tail(log: Path, lines: int = 3) -> list[str]:
    try:
        return log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:]
    except OSError:
        return []


def _exclude_handoff(worktree: Path) -> None:
    """Add `/HANDOFF.md` to the git exclude file `worktree` reads, unless a
    `HANDOFF.md` or `/HANDOFF.md` line is already there, then verify with
    `git check-ignore` that the worktree's root HANDOFF.md is ignored.

    The child PM keeps a HANDOFF.md in its worktree root; untracked and not
    ignored, it is a `??` status line, which fails floor fact 7 at every
    `finalize`. In a linked worktree `.git` is a file, so the path comes from
    git: there it is the main repository's `info/exclude`, shared by every
    worktree and kept after the campaign. Hence the anchored line: an
    unanchored one would hide a `HANDOFF.md` at any depth from the floor, in
    every worktree, for good. A `.gitignore` negation (`!HANDOFF.md`)
    outranks the exclude file, so the line alone proves nothing; the check
    does.
    """
    path = Path(git_ops.git(worktree, "rev-parse", "--git-path", "info/exclude"))
    if not path.is_absolute():
        path = worktree / path
    try:
        existing = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        existing = ""
    lines = existing.splitlines()
    if "HANDOFF.md" not in lines and "/HANDOFF.md" not in lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        separator = "\n" if existing and not existing.endswith("\n") else ""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{separator}/HANDOFF.md\n")
    if git_ops.git_result(worktree, "check-ignore", "-q", "HANDOFF.md")[0] != 0:
        raise PmError(
            f"HANDOFF.md is not ignored in {worktree} even after the exclude line (an ignore rule overrides it); "
            "a child PM's handoff would fail floor fact 7"
        )


def _start_child_session(launch: _ChildLaunch, launcher: Path) -> None:
    """Start the child's session, mark its server as this child's, write the
    manifest, await readiness and inject the launch pointer.

    For a sandboxed launch the server comes first: the control and temp
    directories, the effective and attach policies written to the control
    directory, the server started under `srt` and verified, the record
    written; the session is then created through that server, so it can
    exist nowhere else. A control directory that appeared after the
    preconditions checked for it belongs to a rival launch on the same
    socket: the launch is refused and nothing of it is touched. Once this
    launch has created the control directory it owns the socket (a rival
    fails at its own creation), and from then on every failure tears down
    what this launch created before it propagates: `sandbox.stop_server`
    once the server has started (it is this launch's own, by
    `start_server`'s refusal of a socket already answering); before that,
    the directories it created and, when `start_server` was interrupted
    before returning (it raises PmError only after killing what it
    started), any server answering on the socket.
    """
    branch, worktree, session, socket = launch.branch, launch.worktree, launch.session, launch.socket
    directory = campaign_dir(worktree)
    env = {
        "PM_TMUX_SOCKET": socket,
        "PM_CM_QUESTION_PATH": str(directory / "cm-question.md"),
        "PM_CHILD_RESULT_PATH": str(directory / "child-result.json"),
    }
    manifest = {
        "socket": socket,
        "session": session,
        "branch": branch,
        "base": launch.base_commit,
        "worktree": str(worktree),
        "launcher": str(launcher),
    }
    control = launch.control_dir
    # What this launch created, and so may remove: the control directory
    # once its `mkdir` succeeded, the temp dir once `make_tmpdir` returned.
    control_created = False
    tmpdir: Path | None = None
    # True while `start_server` may have started `srt` without returning its pid.
    server_pending = False
    srt_pid: int | None = None

    def sandbox_teardown() -> str:
        """Undo the sandbox's server and directories, as far as this launch
        created them; return what happened, for the failure message. Never
        raises."""
        if control is None:
            return ""
        try:
            if srt_pid is not None:
                record = {"srt_pid": srt_pid, "tmpdir": str(tmpdir), "control": str(control)}
                outcome = sandbox_mod.stop_server(socket=socket, record=record)
                remaining = outcome.remaining
                killed = "was killed" if remaining == [] else f"still lists sessions: {', '.join(remaining)}"
                return f"; the sandboxed tmux server {socket} {killed}; {'; '.join(outcome.notes)}"
            notes = []
            if server_pending:
                # The socket is this launch's, so a server answering there is
                # the one the interrupted `srt` started.
                if sessions.server_running(socket, timeout=sandbox_mod.START_DEADLINE_SECONDS):
                    remaining = sessions.kill_server(socket)
                    killed = "was killed" if remaining == [] else f"still lists sessions: {', '.join(remaining)}"
                    notes.append(f"the tmux server the interrupted start left on socket {socket} {killed}")
                else:
                    notes.append(
                        f"no tmux server answered on socket {socket} after the interrupted start; check by hand that "
                        f"no srt is still starting one (pgrep -f {shlex.quote(f'tmux -L {socket}')})"
                    )
            if tmpdir is not None and sandbox_mod.remove_tmpdir(tmpdir):
                notes.append(f"removed sandbox temp dir {tmpdir}")
            if control_created and os.path.lexists(control):
                shutil.rmtree(control)
                notes.append(f"removed sandbox control dir {control}")
            return f"; {'; '.join(notes)}" if notes else ""
        except BaseException as exc:
            # Cleanup must never replace the failure it is cleaning up after.
            return (
                f"; the sandbox teardown failed ({exc}): check tmux -L {socket}, and remove "
                f"{tmpdir or 'the sandbox temp dir'} and {control} by hand"
            )

    if control is not None and launch.base_policy is not None:
        # The control directory is the host's alone: every derived policy
        # denies its root, so nothing inside the sandbox can write a policy a
        # later host-launched srt would read.
        try:
            control.mkdir(parents=True)
        except FileExistsError:
            raise PmError(
                f"sandbox control dir already exists: {control}; another launch on socket {socket} created it after "
                f"this launch's checks, and it was left untouched; {_left_behind(branch, worktree)}"
            ) from None
        except OSError as exc:
            raise PmError(
                f"child sandbox did not start (could not create {control}: {exc}); {_left_behind(branch, worktree)}"
            ) from exc
        control_created = True
        try:
            tmpdir = sandbox_mod.make_tmpdir(socket)
            control_root = control.parent  # <common git dir>/pm-sandbox, which every policy denies
            policy = sandbox_mod.derive_policy(
                launch.base_policy,
                worktree=worktree,
                branch=branch,
                socket=socket,
                tmpdir=tmpdir,
                control_root=control_root,
                pm_inside=True,
                extra_read=[launch.launcher_dir] if launch.launcher_dir is not None else [],
            )
            sandbox_mod.check_binaries(policy)
            attach = sandbox_mod.derive_attach_policy(launch.base_policy, socket=socket, control_root=control_root)
            policy_path = control / _POLICY_FILE
            policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
            (control / _ATTACH_POLICY_FILE).write_text(json.dumps(attach, indent=2) + "\n", encoding="utf-8")
            started_at = state_mod.utc_now_iso()
            server_pending = True
            try:
                srt_pid, server_pid = sandbox_mod.start_server(
                    socket=socket,
                    policy_path=policy_path,
                    worktree=worktree,
                    tmpdir=tmpdir,
                    log_path=control / _SRT_LOG_FILE,
                )
            except PmError:
                # `start_server` kills what it started before raising one.
                server_pending = False
                raise
            server_pending = False
            sandbox_mod.verify_server(socket=socket, worktree=worktree)
            sandbox_mod.write_record(
                control,
                worktree=str(worktree),
                branch=branch,
                socket=socket,
                tmpdir=str(tmpdir),
                policy=str(launch.sandbox),
                policy_sha256=launch.policy_sha256,
                srt_version=launch.srt_version,
                srt_pid=srt_pid,
                server_pid=server_pid,
                started_at=started_at,
            )
        except BaseException as exc:
            teardown = sandbox_teardown()
            if not isinstance(exc, Exception):
                # An interrupt propagates as itself, carrying what was torn down.
                exc.add_note(f"child sandbox did not start{teardown}; {_left_behind(branch, worktree)}")
                raise
            raise PmError(f"child sandbox did not start ({exc}){teardown}; {_left_behind(branch, worktree)}") from exc

    with _tmux_socket(socket):
        try:
            if control is None:
                sessions.start_session(session, worktree, launch.command, env)
            else:
                sessions.start_session_via_server(socket, session, worktree, launch.command, env)
        except PmError as exc:
            teardown = sandbox_teardown()
            raise PmError(f"child session did not start ({exc}){teardown}; {_left_behind(branch, worktree)}") from exc
        except BaseException as exc:
            # An interrupt or an unexpected error still takes a sandbox down.
            teardown = sandbox_teardown()
            if teardown:
                exc.add_note(f"child session did not start{teardown}; {_left_behind(branch, worktree)}")
            raise
        # The session is live from here. The server is marked as this child's
        # before anything else (stop-child kills only a server carrying the
        # mark), then the manifest is written; any failure after this kills
        # the whole private server, keeping any manifest for stop-child.
        try:
            sessions.set_server_environment(socket, _OWNER_ENV_VAR, str(worktree))
            manifest["launched_at"] = state_mod.utc_now_iso()
            manifest["dossier_commit"] = launch.dossier_commit
            manifest["node_dir"] = str(launch.node_dir) if launch.node_dir is not None else None
            if control is not None:
                manifest["sandbox"] = {
                    "policy": str(launch.sandbox),
                    "policy_sha256": launch.policy_sha256,
                    "socket": socket,
                    "control_dir": str(control),
                }
            pm_paths.write_pm_text(worktree, manifest_path(worktree), json.dumps(manifest, indent=2) + "\n")
            sessions.wait_until_ready(session, launch.executable, expected_model_display=launch.expected_model_display)
            sessions.send_prompt(session, prompts.render_child_launch_pointer(launcher))
        except BaseException as exc:
            # Best-effort cleanup that must never replace the failure it is
            # cleaning up after. The pane is captured first: it holds the
            # dialog or error that made the launch fail.
            pane_path = directory / "pane.txt"
            try:
                pm_paths.write_pm_text(worktree, pane_path, sessions.capture_text(session))
                pane_note = f"its pane was captured to {pane_path}"
            except BaseException as capture_exc:
                pane_note = f"its pane could not be captured to {pane_path} ({capture_exc})"
            if control is not None:
                # A sandboxed server goes with its temp and control directories.
                teardown = sandbox_teardown()
                if not isinstance(exc, Exception):
                    exc.add_note(
                        f"child launch failed after its session started; {pane_note}{teardown}; "
                        f"{_left_behind(branch, worktree)}"
                    )
                    raise
                raise PmError(
                    f"child launch failed after its session started ({exc}); {pane_note}{teardown}; "
                    f"{_left_behind(branch, worktree)}"
                ) from exc
            try:
                remaining: list[str] | None = sessions.kill_server(socket)
            except BaseException:
                remaining = None
            if not isinstance(exc, Exception):
                raise
            killed = "was killed" if remaining == [] else "could not be confirmed killed"
            raise PmError(
                f"child launch failed after its session started ({exc}); {pane_note}; the tmux server {socket} "
                f"{killed}; {_left_behind(branch, worktree)}"
            ) from exc


# --- wait-child -----------------------------------------------------------------


@dataclass
class WaitChildOutcome:
    session: str
    running: bool
    result_present: bool
    result_status: str | None
    question_present: bool
    question_text: str | None
    dialog_markers: dict[str, Any]
    tail: str
    elapsed_seconds: float
    # True when a requested wait elapsed with no session-death, result,
    # question, or dialog-marker signal.
    no_signal: bool
    runs: list[ChildRun]
    # Why the run list is empty when it could not be read at all.
    runs_error: str | None = None
    pane_live_error: str | None = None


def _result_status(worktree: Path, path: Path) -> str | None:
    try:
        data = json.loads(pm_paths.read_pm_text(worktree, path))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    status = data.get("status") if isinstance(data, dict) else None
    return status if isinstance(status, str) else None


def wait_child(worktree: Path, *, wait: float | None = None, seen_markers: Sequence[str] = ()) -> WaitChildOutcome:
    """`observe`'s loop for a child, without run state: evidence, never a decision.

    Polls every two seconds, writing the visible pane to `pane-live.txt` on
    each poll, and returns early only on session death, `child-result.json`
    or `cm-question.md` appearing, or a dialog marker on the visible pane —
    never on a pane change, which a working TUI produces constantly. A marker
    whose matched literal equals one of `seen_markers` does not end the wait
    (any other marker does) but is still reported on return. Without `wait` it
    polls once. Once the manifest has loaded nothing here raises
    for an ordinary failure: a read that fails is reported in the outcome
    instead. The one exception is a symlink, hard link or non-regular file
    planted under `.pm/` at `child-result.json` or `cm-question.md`: the
    guarded probe (`pm_paths.pm_file_present`) raises PmError naming it,
    because a path the sandbox can write must not be read through a link.
    """
    worktree = worktree.expanduser().resolve()
    manifest = load_manifest(worktree)
    session = manifest["session"]
    directory = campaign_dir(worktree)
    pane_live_path = directory / "pane-live.txt"
    result_path = directory / "child-result.json"
    question_path = directory / "cm-question.md"
    try:
        capture = pm_paths.read_pm_text(worktree, pane_live_path, errors="replace")
    except (OSError, PmError):
        capture = ""
    pane_live_error: str | None = None

    with _tmux_socket(manifest["socket"]):
        start = time.monotonic()
        deadline = start + wait if wait else None
        while True:
            observation = sessions.scan_visible_pane(session)
            if observation.capture is not None:
                # A dead session or failed capture provides no new screen;
                # the last good capture stays as the evidence.
                capture = observation.capture
                try:
                    pm_paths.write_pm_text(worktree, pane_live_path, capture)
                    pane_live_error = None
                except (OSError, PmError) as exc:
                    pane_live_error = str(exc)
            dialog_markers = observation.dialog_markers
            running = sessions.session_exists(session)
            if (
                sessions.unseen_markers(dialog_markers, seen_markers)
                or not running
                or pm_paths.pm_file_present(worktree, result_path)
                or pm_paths.pm_file_present(worktree, question_path)
            ):
                break
            remaining = deadline - time.monotonic() if deadline is not None else 0.0
            if remaining <= 0:
                break
            time.sleep(min(_WAIT_POLL_SECONDS, remaining))
        elapsed_seconds = time.monotonic() - start

    result_present = pm_paths.pm_file_present(worktree, result_path)
    question_present = pm_paths.pm_file_present(worktree, question_path)
    no_signal = (
        bool(wait)
        and running
        and not result_present
        and not question_present
        and not sessions.unseen_markers(dialog_markers, seen_markers)
    )
    runs, runs_error = _runs_or_error(worktree)
    return WaitChildOutcome(
        session=session,
        running=running,
        result_present=result_present,
        result_status=_result_status(worktree, result_path) if result_present else None,
        question_present=question_present,
        question_text=slice_ops.read_question_head(worktree, question_path) if question_present else None,
        dialog_markers=dialog_markers,
        tail="\n".join(capture.splitlines()[-_TAIL_LINES:]),
        elapsed_seconds=elapsed_seconds,
        no_signal=no_signal,
        runs=runs,
        runs_error=runs_error,
        pane_live_error=pane_live_error,
    )


# --- answer-child ---------------------------------------------------------------


@dataclass
class AnswerChildOutcome:
    answer_path: Path
    question_archived_path: Path
    answer_number: int
    # False when the pane did not change after the pointer's Enter (see `sessions.Delivery`).
    delivered: bool = True


def answer_child(worktree: Path, *, text: str | None = None, redeliver: bool = False) -> AnswerChildOutcome:
    """Deliver the CM's answer to the child's open `cm-question.md`, or re-send the last one.

    With `text`: refused, before any file is touched, with no live session, no
    open question, a completion file already present, or blank text. Delivery
    is `slice_ops.deliver_answer` with the `cm-` prefix — the helper behind
    `pm answer`, with its numbering and its failure handling.

    With `redeliver` (and no `text`): re-sends the pointer for the
    highest-numbered `cm-answer-<n>.md` through `sessions.send_line` and writes
    nothing. Refused with no live session, an open `cm-question.md` (a newer
    question the answer does not address), a completion file, or no answer file.
    """
    if redeliver == (text is not None):
        raise PmError("answer-child takes exactly one of --text (answer the open question) and --redeliver")
    worktree = worktree.expanduser().resolve()
    manifest = load_manifest(worktree)
    session = manifest["session"]
    directory = campaign_dir(worktree)
    question_path = directory / "cm-question.md"
    result_path = directory / "child-result.json"
    with _tmux_socket(manifest["socket"]):
        if not sessions.session_exists(session):
            raise PmError(f"no live child session {session} on tmux server {manifest['socket']}")
        if redeliver:
            return _redeliver_answer(worktree, directory, session)
        assert text is not None
        if not pm_paths.pm_file_present(worktree, question_path):
            raise PmError(f"no open child question: {question_path} does not exist")
        _refuse_if_child_finished(worktree, result_path)
        if not text.strip():
            raise PmError("--text answer must be non-empty (there is nothing to deliver)")
        answer_path, archived_path, number, delivery = slice_ops.deliver_answer(
            worktree, directory, "cm-", session, text
        )
    return AnswerChildOutcome(
        answer_path=answer_path,
        question_archived_path=archived_path,
        answer_number=number,
        delivered=bool(delivery.confirmed),
    )


def _refuse_if_child_finished(worktree: Path, result_path: Path) -> None:
    if pm_paths.pm_file_present(worktree, result_path):
        raise PmError(
            f"the child has written its completion file ({result_path}); an answer cannot reach a finished run"
        )


def _redeliver_answer(worktree: Path, directory: Path, session: str) -> AnswerChildOutcome:
    """Re-send the pointer for the highest-numbered answer; `answer_child` has checked the session."""
    question_path = directory / "cm-question.md"
    if pm_paths.pm_file_present(worktree, question_path):
        raise PmError(
            f"{question_path} is open: a newer question is waiting for an answer, not a re-send of the last one"
        )
    _refuse_if_child_finished(worktree, directory / "child-result.json")
    matches = (_ANSWER_FILE_RE.match(path.name) for path in directory.glob("cm-answer-*.md"))
    numbers = [int(match.group(1)) for match in matches if match]
    if not numbers:
        raise PmError(f"no answer to re-send: {directory} holds no cm-answer-<n>.md")
    number = max(numbers)
    answer_path = directory / f"cm-answer-{number}.md"
    if not pm_paths.pm_file_present(worktree, answer_path):
        raise PmError(f"no answer to re-send: {answer_path} is not a regular file")
    delivery = sessions.send_line(session, prompts.render_answer_pointer(answer_path))
    return AnswerChildOutcome(
        answer_path=answer_path,
        question_archived_path=directory / f"cm-question-{number}.md",
        answer_number=number,
        delivered=bool(delivery.confirmed),
    )


# --- stop-child -----------------------------------------------------------------


@dataclass
class StopChildOutcome:
    session: str
    socket: str
    was_running: bool
    pane_path: Path
    # True when the session was gone and an earlier `pane.txt` was kept.
    pane_kept: bool
    # Set when the pane capture itself could not be written.
    pane_error: str | None
    reviewer_pgids: list[int]
    # True when the re-probe after the kill found no server on the socket;
    # otherwise `remaining` holds the sessions still listed there.
    server_gone: bool
    remaining: list[str]
    runs_error: str | None = None
    # Set when the post-kill probe failed: the kill is then unconfirmed, and
    # `server_gone` is False with `remaining` empty.
    kill_error: str | None = None
    # For a sandboxed child: what the teardown found and did (`sandbox.tear_down`),
    # and why it did not run or failed, when it did not complete.
    sandbox_teardown: sandbox_mod.TeardownOutcome | None = None
    sandbox_error: str | None = None

    @property
    def sandbox_notes(self) -> list[str]:
        """One line per teardown step (`TeardownOutcome.notes`), for printing."""
        return self.sandbox_teardown.notes if self.sandbox_teardown is not None else []

    @property
    def sandbox_stopped(self) -> bool:
        """False when a sandbox teardown was skipped or failed, or left
        anything up (`TeardownOutcome.left`: a session, `srt`, the control
        directory). A temp dir left under another `TMPDIR` is not among them,
        as in a Mode B run end: a retry under this `TMPDIR` cannot remove it,
        so its note names it for the operator."""
        teardown = self.sandbox_teardown
        if self.sandbox_error is not None:
            return False
        return teardown is None or not teardown.left


def stop_child(worktree: Path) -> StopChildOutcome:
    """Capture the child's full pane, report its recorded reviewers, kill its
    private tmux server and tear down its sandbox, if it has one; a sandbox
    left up makes `stop-child` exit non-zero. Safe to re-run: with the
    session already gone, an existing `pane.txt` is kept rather than
    overwritten.

    The manifest sits in the child's own worktree, so the server it names is
    checked rather than trusted, before anything is captured or killed.
    Ownership is proved by the launch-time server mark: a live server whose
    `PM_CHILD_WORKTREE` is not this worktree (or is unset) is refused, which
    covers a reused socket now hosting a different child's server. The mark
    is `launch-child`'s `PM_CHILD_WORKTREE=<worktree>` in the server's global
    environment (mechanical), so it belongs to the server, not to the child's
    session: it outlives that session, and a dead child's orphaned `pm-*`
    Developers can still be cleaned up. It guards against stale manifests and
    reused sockets, not against a hostile child: every session on the server
    can read and rewrite it (it is a worktree path, not a secret). A server with no sessions and no
    mark is already gone, and the kill below is a no-op. After that, a
    server hosting any session not named `campaign-*` or `pm-*` — an
    operator's own, say — is refused too, and so is one that cannot be
    listed or whose mark cannot be read.

    Reviewers the child commissioned run in their own process groups,
    outside its tmux server, so `kill-server` does not reach them; their
    group ids are read tokenlessly from the child's `run.json` files and
    returned for a by-hand check. Nothing here signals them.

    A sandboxed child is one whose manifest has a `sandbox` block, or whose
    control directory exists. That directory is derived, never read from the
    manifest: `<common git dir of the worktree>/pm-sandbox/<socket>`. A child
    with no `sandbox` block whose socket name the sandbox refuses (a control
    character, say) can never have been sandboxed, and is stopped as an
    unsandboxed one. The record in the control directory is host-written
    (nothing inside the sandbox can write under the control root), so one
    naming another worktree or socket, which is what a manifest pointed at
    another child's socket meets, is refused before anything is captured or
    killed. After a confirmed kill the sandbox goes through `sandbox.tear_down`,
    the teardown a Mode B run end shares: the recorded `srt` waited for, the
    record's temp directory and the control directory removed, and the
    control root once nothing else is left in it. With no record (an earlier
    stop removed it, or a launch never wrote it) the sandbox is torn down
    from what `launch-child` created, the temp dir `make_tmpdir` names and
    the control directory, each reported gone when it already is; such a
    control directory is checked by the server mark alone. The control
    directory and its record are kept while a session is listed or `srt`
    still runs, so a re-run sees what is left and exits non-zero until it
    has gone. A record's temp dir under another `TMPDIR` is left in place
    with a note and does not make the stop exit non-zero, as in a Mode B
    run end: the control directory goes, so a re-run under the record's
    `TMPDIR` removes the temp dir, while one under this `TMPDIR` finds no
    record and cannot see it; the note asks the operator. A control
    directory whose record cannot be read is checked by the server mark
    alone, the server captured and killed as usual, and the directory left
    in place, with the command that removes it by hand.
    """
    worktree = worktree.expanduser().resolve()
    manifest = load_manifest(worktree)
    session = manifest["session"]
    socket = manifest["socket"]
    try:
        common: Path | None = Path(
            git_ops.git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        common_error = None
    except PmError as exc:
        common, common_error = None, str(exc)
    control: Path | None = None
    if common is not None:
        try:
            control = sandbox_mod.control_dir(common, socket)
        except PmError:
            # A socket name the sandbox refuses can never have been sandboxed.
            if "sandbox" in manifest:
                raise
    sandboxed = "sandbox" in manifest or (control is not None and os.path.lexists(control))
    record: dict[str, Any] | None = None
    # Set when the control directory exists but holds no readable record.
    record_error: str | None = None
    if sandboxed:
        if control is None:
            raise PmError(
                f"the child's manifest records a sandbox, but its control directory cannot be derived from {worktree} "
                f"({common_error}); refusing to kill the server"
            )
        if os.path.lexists(control / sandbox_mod.RECORD_NAME):
            try:
                record = sandbox_mod.read_record(control)
            except PmError as exc:
                # Nothing to check here, so the server mark below is the
                # ownership proof, and nothing is removed.
                record_error = str(exc)
            if record is not None and (record.get("worktree") != str(worktree) or record.get("socket") != socket):
                remedy = ""
                if "sandbox" not in manifest:
                    remedy = (
                        f"; this child's manifest records no sandbox, so {control} is left by an earlier sandboxed "
                        f"child on socket {socket}: once no server of that child runs there, remove it by hand "
                        f"(rm -rf {shlex.quote(str(control))}) and re-run stop-child"
                    )
                raise PmError(
                    f"the sandbox record in {control} names worktree {record.get('worktree')!r} on socket "
                    f"{record.get('socket')!r}, not {worktree} on {socket}; refusing to kill the server{remedy}"
                )
    listed = sessions.list_sessions_on(socket)
    owner = sessions.server_environment(socket, _OWNER_ENV_VAR, timeout=sandbox_mod.PROBE_TIMEOUT_SECONDS)
    if (listed or owner is not None) and owner != str(worktree):
        raise PmError(
            f"tmux server {socket} was not launched for {worktree} (it carries "
            f"{_OWNER_ENV_VAR}={owner if owner is not None else 'nothing'}); refusing to kill the server"
        )
    bystanders = [name for name in listed if not _OWNED_SESSION_RE.match(name)]
    if bystanders:
        raise PmError(
            f"tmux server {socket} hosts sessions this child does not own: {', '.join(bystanders)}; "
            "refusing to kill the server"
        )
    pane_path = campaign_dir(worktree) / "pane.txt"
    pane_error: str | None = None
    # Bound before the try: a refused pane.txt raises from the presence check.
    pane_kept = False
    with _tmux_socket(socket):
        was_running = sessions.session_exists(session)
        # A re-run against a server already gone would overwrite the first
        # stop's capture with capture_text's placeholder; keep the evidence.
        # A refused (planted-link) pane.txt is reported as the capture's
        # error, never allowed to abort the stop before the server is killed.
        try:
            pane_kept = not was_running and pm_paths.pm_file_present(worktree, pane_path)
            if not pane_kept:
                pm_paths.write_pm_text(worktree, pane_path, sessions.capture_text(session))
        except (OSError, PmError) as exc:
            pane_error = str(exc)
    runs, runs_error = _runs_or_error(worktree)
    pgids = sorted({pgid for run in runs for pgid in run.reviewer_pgids})
    kill_error: str | None = None
    try:
        remaining = sessions.kill_server(socket)
    except PmError as exc:
        # The reviewer groups above are already read; do not lose them.
        kill_error = str(exc)
        remaining = []
    server_gone = kill_error is None and not remaining
    teardown: sandbox_mod.TeardownOutcome | None = None
    sandbox_error: str | None = None
    if sandboxed and control is not None:
        if not server_gone:
            sandbox_error = (
                f"the server was not confirmed gone, so its sandbox temp dir and {control} were left in place; "
                "re-run stop-child"
            )
        elif record_error is not None:
            sandbox_error = (
                f"no readable sandbox record in {control} ({record_error}), so nothing of the sandbox was removed; "
                f"once no server runs on socket {socket}, remove it by hand: rm -rf {shlex.quote(str(control))}"
            )
        else:
            try:
                teardown = sandbox_mod.tear_down(socket=socket, control=control, record=record)
            except (PmError, OSError) as exc:
                sandbox_error = str(exc)
    return StopChildOutcome(
        session=session,
        socket=socket,
        was_running=was_running,
        pane_path=pane_path,
        pane_kept=pane_kept,
        pane_error=pane_error,
        reviewer_pgids=pgids,
        server_gone=server_gone,
        remaining=remaining,
        runs_error=runs_error,
        kill_error=kill_error,
        sandbox_teardown=teardown,
        sandbox_error=sandbox_error,
    )

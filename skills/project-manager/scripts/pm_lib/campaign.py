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
server. The one tmux call that names its socket directly is
`sessions.kill_server`.

A child's `run.json` files are read here without a token, so what they say
is unverified: `wait-child` and `stop-child` print it labelled as such, as a
cross-check on the child's own completion file, never as a fact.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import PmError
from . import git_ops
from . import profiles
from . import prompts
from . import sessions
from . import slice_ops
from . import state as state_mod

_WAIT_POLL_SECONDS = 2.0
_TAIL_LINES = 40
_QUESTION_LINES = 40

_SOCKET_ENV_VAR = "PM_TMUX_SOCKET"
_MANIFEST_FIELDS = ("socket", "session", "branch", "base", "worktree", "launcher", "launched_at")
# tmux rejects `.` and `:` in a session name, and a `-t` target containing
# `.` parses as window.pane, so a branch is reduced to this set for names.
_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


# --- Paths and names -----------------------------------------------------------


def campaign_dir(worktree: Path) -> Path:
    """`<worktree>/.pm/campaign/`: the manifest and every child channel file."""
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
    nodes or two repositories never share one.
    """
    component = name_component(branch)
    return f"campaign-{component}-{suffix}", socket or f"pm-child-{component}-{suffix}"


def check_socket_name(socket: str, *, what: str) -> None:
    """Refuse a socket name that is blank, contains whitespace, or contains `/`.

    A blank name would leave `PM_TMUX_SOCKET` effectively unset, putting the
    child on the caller's default server — which `stop-child`'s
    `kill-server` would then destroy.
    """
    if not socket.strip():
        raise PmError(f"{what} must be non-blank")
    if any(character.isspace() for character in socket):
        raise PmError(f"{what} must not contain whitespace: {socket!r}")
    if "/" in socket:
        raise PmError(f"{what} must not contain '/': {socket!r}")


@contextmanager
def _tmux_socket(socket: str) -> Iterator[None]:
    """Point `PM_TMUX_SOCKET` at `socket` for the block; restore the caller's
    value (or its absence) on the way out, whatever the block raised."""
    previous = os.environ.get(_SOCKET_ENV_VAR)
    os.environ[_SOCKET_ENV_VAR] = socket
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_SOCKET_ENV_VAR, None)
        else:
            os.environ[_SOCKET_ENV_VAR] = previous


def load_manifest(worktree: Path) -> dict[str, Any]:
    """The launch manifest; PmError naming the file when it is missing,
    unreadable, malformed, or carries a socket `launch-child` would refuse."""
    path = manifest_path(worktree)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
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


# --- launch-child ---------------------------------------------------------------


@dataclass
class LaunchChildOutcome:
    session: str
    socket: str
    worktree: Path
    launcher: Path
    branch: str
    base: str


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
) -> LaunchChildOutcome:
    """Create the node branch and worktree, then launch a child PM in tmux.

    Every precondition is checked before anything is created, the last
    being that no tmux server on the chosen socket hosts a session. Then
    `git worktree add -b <branch> <worktree> <base commit>`, the self-ignoring
    `.pm/.gitignore`, `.pm/campaign/` and a byte copy of the prompt file as
    `launcher.md`; then the session starts as `start_slice` starts a
    Developer's, the manifest is written as soon as it exists, readiness is
    awaited, and the one-line child launch pointer is injected.

    There is no rollback: a failure after the worktree exists names the
    branch and worktree left behind for the CM to remove by hand, and a
    failure after the session started also kills the child's private server
    (which may already host Developers the child launched) and keeps the
    manifest, so `stop-child` can be re-run against it.
    """
    # --- Preconditions: nothing is created until every one has passed.
    repo = git_ops.resolve_repo(repo)
    returncode, stdout, stderr = git_ops.git_result(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if returncode != 0 or not stdout.strip():
        detail = stderr.strip()
        raise PmError(f"--base {base!r} does not resolve to a commit in {repo}" + (f": {detail}" if detail else ""))
    base_commit = stdout.strip()
    returncode, _stdout, _stderr = git_ops.git_result(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if returncode == 0:
        raise PmError(f"branch {branch!r} already exists in {repo}; a child launches on a new branch")
    # Checked before resolving: a dangling symlink resolves to its missing
    # target, and both tests would then pass on a path that exists.
    expanded = worktree.expanduser()
    if expanded.exists() or expanded.is_symlink():
        raise PmError(f"worktree path already exists: {expanded}")
    worktree = expanded.resolve()
    if worktree.exists():
        raise PmError(f"worktree path already exists: {worktree}")
    try:
        prompt_bytes = prompt_file.read_bytes()
        prompt_text = prompt_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PmError(f"prompt file could not be read: {prompt_file} ({exc})") from exc
    if not prompt_text.strip():
        raise PmError(f"prompt file is blank: {prompt_file}")
    if not harness_command and harness not in profiles.SUPPORTED_HARNESSES:
        supported = ", ".join(profiles.SUPPORTED_HARNESSES)
        raise PmError(f"--harness {harness!r} is not supported (one of {supported}), and no --harness-command is given")
    if socket is not None:
        check_socket_name(socket, what="--socket")

    expected_model_display: str | None = None
    if harness_command:
        command = harness_command
    else:
        session_id = str(uuid.uuid4()) if harness == "claude" else None
        if harness == "opencode" and model:
            identity = profiles.query_model_identity(harness, model)
            if identity:
                expected_model_display = identity["display_name"]
        command = profiles.compose_command(harness, model=model, effort=effort, session_id=session_id)
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
    # another child's, `default` — would destroy sessions this child does
    # not own.
    with _tmux_socket(socket):
        existing = sessions.sessions_with_prefix("")
    if existing:
        raise PmError(
            f"tmux server {socket} already hosts {len(existing)} session(s); a child needs a private server "
            "(omit --socket, or name one with no server)"
        )

    # --- The worktree. From here every failure names what is left behind.
    try:
        git_ops.git(repo, "worktree", "add", "-b", branch, str(worktree), base_commit)
    except PmError as exc:
        # git normally leaves nothing behind on failure; say so when it did.
        leftovers = []
        if git_ops.git_result(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")[0] == 0:
            leftovers.append(f"branch {branch} exists (git branch -D {branch})")
        if worktree.exists():
            leftovers.append(f"path {worktree} exists (git worktree remove {worktree}, or remove it by hand)")
        partial = f"; left behind: {'; '.join(leftovers)}" if leftovers else ""
        raise PmError(f"git worktree add failed: {exc}{partial}") from exc
    directory = campaign_dir(worktree)
    launcher = directory / "launcher.md"
    try:
        slice_ops.write_pm_gitignore(worktree)
        directory.mkdir(parents=True, exist_ok=True)
        launcher.write_bytes(prompt_bytes)
    except OSError as exc:
        raise PmError(f"child setup failed ({exc}); {_left_behind(branch, worktree)}") from exc

    env = {
        "PM_TMUX_SOCKET": socket,
        "PM_CM_QUESTION_PATH": str(directory / "cm-question.md"),
        "PM_CHILD_RESULT_PATH": str(directory / "child-result.json"),
    }
    manifest = {
        "socket": socket,
        "session": session,
        "branch": branch,
        "base": base_commit,
        "worktree": str(worktree),
        "launcher": str(launcher),
    }
    with _tmux_socket(socket):
        try:
            sessions.start_session(session, worktree, command, env)
        except PmError as exc:
            raise PmError(f"child session did not start ({exc}); {_left_behind(branch, worktree)}") from exc
        # The session is live from here. The manifest is written first, so a
        # manifest exists whenever a session does; any failure after this
        # kills the whole private server, keeping the manifest for stop-child.
        try:
            manifest["launched_at"] = state_mod.utc_now_iso()
            manifest_path(worktree).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            sessions.wait_until_ready(session, executable, expected_model_display=expected_model_display)
            sessions.send_prompt(session, prompts.render_child_launch_pointer(launcher))
        except BaseException as exc:
            # Best-effort cleanup that must never replace the failure it is
            # cleaning up after.
            try:
                sessions.kill_server(socket)
            except BaseException:
                pass
            if not isinstance(exc, Exception):
                raise
            raise PmError(
                f"child launch failed after its session started ({exc}); the tmux server {socket} was killed; "
                f"{_left_behind(branch, worktree)}"
            ) from exc

    return LaunchChildOutcome(
        session=session, socket=socket, worktree=worktree, launcher=launcher, branch=branch, base=base_commit
    )


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


def _result_status(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    status = data.get("status") if isinstance(data, dict) else None
    return status if isinstance(status, str) else None


def _question_text(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return "\n".join(lines[:_QUESTION_LINES])


def wait_child(worktree: Path, *, wait: float | None = None) -> WaitChildOutcome:
    """`observe`'s loop for a child, without run state: evidence, never a decision.

    Polls every two seconds, writing the visible pane to `pane-live.txt` on
    each poll, and returns early only on session death, `child-result.json`
    or `cm-question.md` appearing, or a dialog marker on the visible pane —
    never on a pane change, which a working TUI produces constantly. Without
    `wait` it polls once. Once the manifest has loaded nothing here raises:
    a read that fails is reported in the outcome instead.
    """
    worktree = worktree.expanduser().resolve()
    manifest = load_manifest(worktree)
    session = manifest["session"]
    directory = campaign_dir(worktree)
    pane_live_path = directory / "pane-live.txt"
    result_path = directory / "child-result.json"
    question_path = directory / "cm-question.md"
    try:
        capture = pane_live_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
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
                    pane_live_path.write_text(capture, encoding="utf-8")
                    pane_live_error = None
                except OSError as exc:
                    pane_live_error = str(exc)
            dialog_markers = observation.dialog_markers
            running = sessions.session_exists(session)
            if dialog_markers["present"] or not running or result_path.is_file() or question_path.is_file():
                break
            remaining = deadline - time.monotonic() if deadline is not None else 0.0
            if remaining <= 0:
                break
            time.sleep(min(_WAIT_POLL_SECONDS, remaining))
        elapsed_seconds = time.monotonic() - start

    result_present = result_path.is_file()
    question_present = question_path.is_file()
    no_signal = bool(wait) and running and not result_present and not question_present and not dialog_markers["present"]
    runs_error: str | None = None
    try:
        runs = read_child_runs(worktree)
    except (PmError, OSError) as exc:
        runs, runs_error = [], str(exc)
    return WaitChildOutcome(
        session=session,
        running=running,
        result_present=result_present,
        result_status=_result_status(result_path) if result_present else None,
        question_present=question_present,
        question_text=_question_text(question_path) if question_present else None,
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


def answer_child(worktree: Path, *, text: str) -> AnswerChildOutcome:
    """Deliver the CM's answer to the child's open `cm-question.md`.

    Refused, before any file is touched, with no live session, no open
    question, a completion file already present, or blank text. Delivery is
    `slice_ops.deliver_answer` with the `cm-` prefix — the helper behind
    `pm answer`, with its numbering and its failure handling.
    """
    worktree = worktree.expanduser().resolve()
    manifest = load_manifest(worktree)
    session = manifest["session"]
    directory = campaign_dir(worktree)
    question_path = directory / "cm-question.md"
    with _tmux_socket(manifest["socket"]):
        if not sessions.session_exists(session):
            raise PmError(f"no live child session {session} on tmux server {manifest['socket']}")
        if not question_path.is_file():
            raise PmError(f"no open child question: {question_path} does not exist")
        if (directory / "child-result.json").exists():
            raise PmError(
                f"the child has written its completion file ({directory / 'child-result.json'}); "
                "an answer cannot reach a finished run"
            )
        if not text.strip():
            raise PmError("--text answer must be non-empty (there is nothing to deliver)")
        answer_path, archived_path, number = slice_ops.deliver_answer(directory, "cm-", session, text)
    return AnswerChildOutcome(answer_path=answer_path, question_archived_path=archived_path, answer_number=number)


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
    runs_error: str | None = None


def stop_child(worktree: Path) -> StopChildOutcome:
    """Capture the child's full pane, report its recorded reviewers, and kill
    its private tmux server. Safe to re-run: with the session already gone,
    an existing `pane.txt` is kept rather than overwritten.

    Reviewers the child commissioned run in their own process groups,
    outside its tmux server, so `kill-server` does not reach them; their
    group ids are read tokenlessly from the child's `run.json` files and
    returned for a by-hand check. Nothing here signals them.
    """
    worktree = worktree.expanduser().resolve()
    manifest = load_manifest(worktree)
    session = manifest["session"]
    socket = manifest["socket"]
    pane_path = campaign_dir(worktree) / "pane.txt"
    pane_error: str | None = None
    with _tmux_socket(socket):
        was_running = sessions.session_exists(session)
        # A re-run against a server already gone would overwrite the first
        # stop's capture with capture_to's placeholder; keep the evidence.
        pane_kept = not was_running and pane_path.is_file()
        if not pane_kept:
            try:
                sessions.capture_to(session, pane_path)
            except OSError as exc:
                pane_error = str(exc)
        runs_error: str | None = None
        try:
            runs = read_child_runs(worktree)
        except (PmError, OSError) as exc:
            runs, runs_error = [], str(exc)
        pgids = sorted({pgid for run in runs for pgid in run.reviewer_pgids})
        sessions.kill_server(socket)
    return StopChildOutcome(
        session=session,
        socket=socket,
        was_running=was_running,
        pane_path=pane_path,
        pane_kept=pane_kept,
        pane_error=pane_error,
        reviewer_pgids=pgids,
        runs_error=runs_error,
    )

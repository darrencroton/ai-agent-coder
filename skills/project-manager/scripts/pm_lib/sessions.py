"""Developer session lifecycle: headless process contact and the legacy tmux path.

This module owns *all* harness-process and tmux contact.  The headless runner
is additive until the lifecycle cutover; the existing tmux functions remain
unchanged.  It never judges anything beyond the hard-stop marker scan
(`scan_hard_stop`), which is pure text parsing shared by `send_line`,
`observe`, and `floor.py`'s fact 8.

The recorded readiness banners and hard-stop marker/phrasing strings preserve
field observations of external tools; the code around them is independent.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import PmError

# --- Hard-stop marker floor (target-design §11) -----------------------------

# Directory-trust / folder-trust dialogs from the interactive TUIs. If any of
# these is on screen when PM is about to submit, a blind Enter would confirm
# it (for example auto-trusting a directory) — exactly the kind of side
# effect PM must not cause.
TRUST_PROMPT_MARKERS: tuple[str, ...] = (
    "Do you trust the contents of this directory",
    "Do you trust the files in this folder",
    "Do you trust the files in this directory",
)

_LITERAL_MARKERS: dict[str, tuple[str, ...]] = {
    "trust_prompt": TRUST_PROMPT_MARKERS,
    "approval_prompt": (
        "Do you want to proceed?",
        "Approve this action",
        "Allow this command",
        "requires approval",
        "requires manual approval",
        "approval required",
    ),
    "credential_prompt": (
        "Enter API key",
        "Enter password",
        "Enter your password",
        "Login required",
        "Please log in",
        "Sign in to continue",
        "MFA",
        "two-factor",
    ),
    "permission_prompt": (
        "Permission denied",
        "Grant permission",
        "requires permission",
        "allow access",
    ),
}

# Detects "push / create PR / deploy / install dependency / license change"
# prompts — carried verbatim from old-evidence (constants.EXTERNAL_SIDE_EFFECT_PROMPT_RE).
EXTERNAL_SIDE_EFFECT_PROMPT_RE = re.compile(
    r"\b(?:do you want to|approve|confirm|allow|permission to|shall i|should i|ready to)\b"
    r"[^.\n?]{0,120}\b(?:push(?: to remote)?|create (?:a )?(?:pull request|pr)|open (?:a )?(?:pull request|pr)|"
    r"deploy|release|publish|install (?:a )?dependenc(?:y|ies)|change (?:the )?license|license change)\b"
    r"|"
    r"\b(?:push to remote|create (?:a )?(?:pull request|pr)|open (?:a )?(?:pull request|pr)|deploy|release|publish|"
    r"install (?:a )?dependenc(?:y|ies)|license change)\b[^.\n]{0,60}(?:\?|yes/no|\[y/n\]|approve|confirm)",
    re.IGNORECASE,
)

# usage_limit_hard_stop: weekly/monthly-window and account/billing/subscription/
# credit phrasings, plus the generic "usage/session/rate/quota/limit/cap ...
# reached/exceeded/exhausted" pattern. The weekly/monthly/account patterns are
# suppressed when the same text carries the informational sub-100% usage
# warning or the conditional "if you hit your limit" phrasing — both are
# explicitly non-stopping (design §16; carried from old-evidence hints.py).
_WEEKLY_LIMIT_RE = re.compile(r"\bweekly\b[^.\n]{0,80}\b(?:limit|quota|cap)\b|\b(?:limit|quota|cap)\b[^.\n]{0,80}\bweekly\b")
_MONTHLY_LIMIT_RE = re.compile(
    r"\bmonthly\b[^.\n]{0,80}\b(?:limit|quota|cap)\b|\b(?:limit|quota|cap)\b[^.\n]{0,80}\bmonthly\b"
)
_ACCOUNT_BILLING_LIMIT_RE = re.compile(
    r"\b(?:account|billing|subscription|plan|credit|credits)\b[^.\n]{0,100}\b(?:limit|quota|cap|exhausted|upgrade|billing)\b"
)
_GENERIC_LIMIT_RE = re.compile(r"\b(?:usage|session|rate|quota|limit|cap)\b[^.\n]{0,80}\b(?:reached|exceeded|exhausted)\b")
_INFORMATIONAL_USAGE_RE = re.compile(
    # Deliberately no \b right after the '%': '%' is punctuation, so a \b
    # there never matches when followed by whitespace (both sides are
    # non-word characters) — a latent bug in the old-evidence pattern this
    # is re-specified from, fixed here so the informational fixture ("used
    # 80% of your weekly limit") is actually recognized as non-stopping.
    r"\b(?:you(?:'ve| have)\s+used|used)\s+(\d{1,3})%[^.\n]{0,120}"
    r"\b(?:hourly|daily|weekly|monthly|5[- ]?hour|five[- ]?hour)?\s*(?:usage\s*)?(?:limit|quota|cap)\b"
)
_CONDITIONAL_LIMIT_RE = re.compile(r"\bif you hit your limit\b")


def scan_hard_stop(text: str) -> dict[str, Any]:
    """The hard-stop marker floor, shared by send_line, observe, and floor fact 8.

    Whitespace-normalizes (a prompt wrapped across terminal rows must still
    match) and lowercases for keyword matching, exactly as the old-evidence
    tmux adapter did. No confidence grades, no subtypes beyond the kind
    labels, no reset-time parsing — the PM agent reads the pane itself.
    """
    normalized = re.sub(r"\s+", " ", text or "")
    lowered = normalized.lower()
    matches: dict[str, Any] = {"present": False, "kinds": [], "markers": []}

    def _add(kind: str, marker_text: str) -> None:
        matches["present"] = True
        if kind not in matches["kinds"]:
            matches["kinds"].append(kind)
        matches["markers"].append(marker_text)

    for kind, markers in _LITERAL_MARKERS.items():
        for marker in markers:
            if marker.lower() in lowered:
                _add(kind, marker)

    external_match = EXTERNAL_SIDE_EFFECT_PROMPT_RE.search(normalized)
    if external_match:
        _add("external_side_effect_request", external_match.group(0).strip())

    informational_match = _INFORMATIONAL_USAGE_RE.search(lowered)
    informational = bool(informational_match and int(informational_match.group(1)) < 100)
    conditional = bool(_CONDITIONAL_LIMIT_RE.search(lowered))

    if not informational and not conditional:
        for pattern in (_WEEKLY_LIMIT_RE, _MONTHLY_LIMIT_RE, _ACCOUNT_BILLING_LIMIT_RE):
            match = pattern.search(lowered)
            if match:
                _add("usage_limit_hard_stop", match.group(0).strip())

    generic_match = _GENERIC_LIMIT_RE.search(lowered)
    if generic_match:
        _add("usage_limit_hard_stop", generic_match.group(0).strip())

    return matches


# --- Headless process plumbing ----------------------------------------------

DEVELOPER_PID_SIDECAR = "developer.pid"
SESSION_OUTFILE = "session-output.txt"
_HEADLESS_CHILDREN: dict[int, subprocess.Popen] = {}


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_state(pid: int) -> str | None:
    """Return the portable ``ps`` state code, or ``None`` when unavailable."""
    if not _process_exists(pid):
        return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        check=False,
        capture_output=True,
        text=True,
    )
    state = result.stdout.strip()
    return state if result.returncode == 0 and state else None


def process_identity(pid: int) -> str | None:
    """Capture a process start-time identity suitable for rejecting PID reuse."""
    if not _process_exists(pid):
        return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        check=False,
        capture_output=True,
        text=True,
    )
    identity = result.stdout.strip()
    return identity if result.returncode == 0 and identity else None


def headless_process_alive(pid: int, identity: str) -> bool:
    """Return true only while ``pid`` is live and still has its launch identity."""
    if not identity or process_identity(pid) != identity:
        return False
    state = _process_state(pid)
    if state and "Z" in state:
        return False
    return True


def launch_headless(
    command: str,
    repo: Path,
    env: dict[str, str],
    artifact_dir: Path,
    *,
    outfile_name: str = SESSION_OUTFILE,
) -> dict[str, Any]:
    """Launch a detached command and return its tracked process coordinates.

    The command is a shell string because PM's command composers produce
    fully quoted shell commands.  The Developer receives the controller's
    environment minus ``PM_RUN_TOKEN``, overlaid with the explicit ``env``.
    stdout and stderr share one outfile so that every harness exposes the
    same progress signal.
    """
    if "PM_RUN_TOKEN" in env:
        raise PmError("session environment must never contain PM_RUN_TOKEN")

    artifact_dir.mkdir(parents=True, exist_ok=True)
    outfile = artifact_dir / outfile_name
    process_env = {key: value for key, value in os.environ.items() if key != "PM_RUN_TOKEN"}
    process_env.update(env)

    with outfile.open("wb") as output_handle:
        process = subprocess.Popen(
            command,
            shell=True,
            executable="/bin/sh",
            cwd=str(repo),
            env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=output_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )

    # start_new_session makes the child the process-group leader.  Capture
    # identity immediately; a missing identity makes later signalling unsafe.
    pid = process.pid
    identity = process_identity(pid)
    if not identity:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired as exc:
            raise PmError(f"could not reap unidentified headless process group {pid}") from exc
        raise PmError(f"could not capture launch identity for headless process {pid}")
    _HEADLESS_CHILDREN[pid] = process
    return {
        "pid": pid,
        "pgid": pid,
        "identity": identity,
        "outfile": str(outfile),
    }


def read_output_tail(outfile: Path, *, max_bytes: int = 128 * 1024) -> str:
    """Read at most the final ``max_bytes`` of a captured session outfile."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        with outfile.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _process_group_exists(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _process_group_has_live_members(pgid: int) -> bool:
    """Return true when a process group contains a non-zombie member."""
    if not _process_group_exists(pgid):
        return False
    result = subprocess.run(
        ["ps", "-axo", "pgid=,stat="],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Fail closed: if ps cannot classify the group, assume it is active.
        return True
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            member_pgid = int(parts[0])
        except ValueError:
            continue
        if member_pgid == pgid and "Z" not in parts[1]:
            return True
    return False


def _reap_child(pid: int, *, wait_timeout: float = 0.0) -> None:
    """Reap ``pid`` when it is our child; another controller may own it."""
    process = _HEADLESS_CHILDREN.get(pid)
    if process is not None:
        if wait_timeout > 0:
            try:
                process.wait(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                return
        else:
            process.poll()
        if process.returncode is not None:
            _HEADLESS_CHILDREN.pop(pid, None)
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def terminate_headless(
    pid: int,
    pgid: int,
    identity: str,
    *,
    term_timeout: float = 5.0,
    kill_timeout: float = 5.0,
) -> bool:
    """Terminate and reap an identity-checked detached process group.

    ``False`` means neither the recorded leader nor an orphaned launch group
    remains, or the leader PID has been reused, so no signal was sent.  A
    group that survives SIGKILL is an explicit failure rather than a silently
    quiesced session.

    POSIX reserves a process-group id while any member remains in that group.
    Therefore, when the original leader PID is absent but this PGID still has
    live members, the group is the launch's orphaned group rather than a newly
    created unrelated group.  A reused PID that is also a new leader is
    detected by the identity/getpgid checks before either escalation signal.
    """
    current_identity = process_identity(pid)
    group_exists = _process_group_has_live_members(pgid)
    if current_identity != identity:
        # When the leader is gone but its group still has descendants, the
        # group cannot have been reused: a new group with this PGID would
        # require a new leader whose PID is pgid, making an identity visible.
        # This lets a fresh controller reap an orphaned launch group safely.
        if not group_exists:
            _reap_child(pid)
            return False
        if current_identity is not None:
            try:
                if os.getpgid(pid) == pgid:
                    return False
            except ProcessLookupError:
                pass
    else:
        try:
            if os.getpgid(pid) != pgid:
                return False
        except ProcessLookupError:
            _reap_child(pid)
            # The matching leader can exit between identity capture and
            # getpgid.  Its still-live group is then the same safe orphan
            # case handled above.
            if not _process_group_has_live_members(pgid):
                return False

    if not group_exists:
        _reap_child(pid, wait_timeout=kill_timeout)
        return False

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        _reap_child(pid, wait_timeout=kill_timeout)
        return True

    deadline = time.monotonic() + max(0.0, term_timeout)
    while _process_group_has_live_members(pgid) and time.monotonic() < deadline:
        _reap_child(pid)
        time.sleep(0.05)

    if _process_group_has_live_members(pgid):
        current_identity = process_identity(pid)
        if current_identity is not None and current_identity != identity:
            try:
                pid_reused_as_leader = os.getpgid(pid) == pgid
            except ProcessLookupError:
                pid_reused_as_leader = False
            if pid_reused_as_leader:
                raise PmError(
                    f"refusing SIGKILL for process group {pgid}: leader PID {pid} was reused"
                )
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + max(0.0, kill_timeout)
    while _process_group_has_live_members(pgid) and time.monotonic() < deadline:
        _reap_child(pid)
        time.sleep(0.05)
    _reap_child(pid, wait_timeout=kill_timeout)

    if _process_group_has_live_members(pgid):
        raise PmError(f"headless process group {pgid} survived SIGKILL")
    return True


def quiesce_headless(
    pid: int,
    pgid: int,
    identity: str,
    *,
    term_timeout: float = 5.0,
    kill_timeout: float = 5.0,
) -> None:
    """Ensure the tracked process is dead before a resumable follow-up turn."""
    terminate_headless(
        pid,
        pgid,
        identity,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )
    deadline = time.monotonic() + max(0.0, kill_timeout)
    while _process_group_has_live_members(pgid) and time.monotonic() < deadline:
        _reap_child(pid)
        time.sleep(0.05)
    if headless_process_alive(pid, identity) or _process_group_has_live_members(pgid):
        raise PmError(f"headless process group {pgid} did not quiesce")


def write_developer_sidecar(
    path: Path,
    *,
    pid: int,
    pgid: int,
    identity: str,
    run_id: str,
    slice_id: str,
) -> None:
    """Atomically persist process coordinates and run/slice ownership."""
    if pid <= 0 or pgid <= 0 or not identity or not run_id or not slice_id:
        raise PmError("developer sidecar requires pid, pgid, identity, run_id, and slice_id")
    payload = {
        "version": 1,
        "pid": pid,
        "pgid": pgid,
        "identity": identity,
        "run_id": run_id,
        "slice_id": slice_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def read_developer_sidecar(path: Path) -> dict[str, Any] | None:
    """Read and validate a ``developer.pid`` sidecar; return ``None`` if absent."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise PmError(f"could not read developer sidecar {path}: {exc}") from exc

    required = {
        "version": int,
        "pid": int,
        "pgid": int,
        "identity": str,
        "run_id": str,
        "slice_id": str,
    }
    if not isinstance(raw, dict) or any(type(raw.get(key)) is not expected for key, expected in required.items()):
        raise PmError(f"developer sidecar {path} has invalid fields")
    if raw["version"] != 1 or raw["pid"] <= 0 or raw["pgid"] <= 0:
        raise PmError(f"developer sidecar {path} has invalid values")
    if not raw["identity"] or not raw["run_id"] or not raw["slice_id"]:
        raise PmError(f"developer sidecar {path} has empty ownership fields")
    return raw


# --- tmux process plumbing --------------------------------------------------


def _run_tmux(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *args], input=input_text, check=False, text=True, capture_output=True)
    except OSError:
        return subprocess.CompletedProcess(args=["tmux", *args], returncode=127, stdout="", stderr="tmux not found")


def _tmux_or_raise(args: list[str], error_prefix: str, *, input_text: str | None = None) -> subprocess.CompletedProcess:
    result = _run_tmux(*args, input_text=input_text)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PmError(f"{error_prefix}: {detail}" if detail else error_prefix)
    return result


def session_name(run_id: str, slice_number: int, attempt: int) -> str:
    """`pm-<run_id>-s<NN>a<N>` — the scavenge path sweeps the `pm-<run_id>` prefix."""
    return f"pm-{run_id}-s{slice_number:02d}a{attempt}"


def start_session(session: str, repo: Path, command: str, env: dict[str, str]) -> None:
    """`tmux new-session -d -s <session> -c <repo> "unset PM_RUN_TOKEN; <env-prefix> <command>"`.

    Env values are shell-quoted. The Developer session's environment must
    never carry the PM run capability token (target-design §8) — asserted
    defensively for the explicit map here, AND stripped from the inherited
    environment: a tmux session inherits the server's (ultimately the
    controller's) environment, so an exported PM_RUN_TOKEN would otherwise
    be visible inside every Developer session. The `unset` runs in the
    session's own shell before anything else.
    """
    if "PM_RUN_TOKEN" in env:
        raise PmError("session environment must never contain PM_RUN_TOKEN")
    if not shutil.which("tmux"):
        raise PmError("tmux is required for runtime execution")
    env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    payload = f"{env_prefix} {command}".strip() if env_prefix else command
    shell_command = f"unset PM_RUN_TOKEN; {payload}"
    _tmux_or_raise(["new-session", "-d", "-s", session, "-c", str(repo), shell_command], "tmux start failed")


def pane_text(session: str) -> str:
    """`capture-pane -p -S -32768`; empty string on any failure."""
    result = _run_tmux("capture-pane", "-p", "-S", "-32768", "-t", session)
    return result.stdout if result.returncode == 0 else ""


def capture_to(session: str, destination: Path) -> None:
    """Write pane text to `destination`; an explanatory placeholder when unavailable."""
    if not shutil.which("tmux"):
        destination.write_text("tmux was unavailable during capture\n", encoding="utf-8")
        return
    result = _run_tmux("capture-pane", "-p", "-S", "-32768", "-t", session)
    if result.returncode == 0:
        destination.write_text(result.stdout, encoding="utf-8")
    else:
        destination.write_text("tmux pane was unavailable during capture\n", encoding="utf-8")


def session_exists(session: str) -> bool:
    if not shutil.which("tmux"):
        return False
    return _run_tmux("has-session", "-t", session).returncode == 0


def sessions_with_prefix(prefix: str) -> list[str]:
    if not shutil.which("tmux"):
        return []
    result = _run_tmux("list-sessions", "-F", "#{session_name}")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip().startswith(prefix)]


def detect_activity(session: str, previous_capture: str) -> dict[str, Any]:
    if not session_exists(session):
        return {"running": False, "active": False, "capture": ""}
    capture = pane_text(session)
    return {"running": True, "active": capture != previous_capture, "capture": capture}


def request_stop(session: str) -> None:
    if session_exists(session):
        _run_tmux("send-keys", "-t", session, "C-c")


def force_stop(session: str) -> None:
    if session_exists(session):
        _run_tmux("kill-session", "-t", session)


# --- Readiness -------------------------------------------------------------


def _raise_on_trust_prompt(executable: str, capture: str) -> None:
    result = scan_hard_stop(capture)
    if "trust_prompt" in result["kinds"]:
        raise PmError(f"{executable} directory trust prompt blocked unattended launch; trust the repo before running PM")


def _wait_stable_pane_ready(session: str, executable: str, deadline: float) -> None:
    """Readiness inferred from the TUI finishing its draw: a non-empty pane unchanged
    across a short window. Reaching the deadline is non-fatal: send_prompt's
    settle-and-double-submit discipline is the backstop."""
    previous = ""
    stable_since: float | None = None
    while time.monotonic() < deadline:
        if not session_exists(session):
            raise PmError(f"{executable} session exited before the prompt could be sent")
        capture = pane_text(session)
        _raise_on_trust_prompt(executable, capture)
        if capture.strip() and capture == previous:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 1.5:
                time.sleep(0.5)
                return
        else:
            stable_since = None
        previous = capture
        time.sleep(0.25)


def _wait_banner_ready(session: str, executable: str, is_ready: Callable[[str], bool], banner_deadline: float) -> None:
    """Banner-keyed readiness with a stable-pane fallback (banner strings are
    version-fragile; a reworded banner must not turn every launch into a hard
    failure)."""
    while time.monotonic() < banner_deadline:
        if not session_exists(session):
            raise PmError(f"{executable} session exited before the prompt could be sent")
        capture = pane_text(session)
        _raise_on_trust_prompt(executable, capture)
        if is_ready(capture):
            time.sleep(0.5)
            return
        time.sleep(0.25)
    _wait_stable_pane_ready(session, executable, time.monotonic() + 10.0)


def _verify_opencode_model_display(session: str, expected_model_display: str) -> None:
    """Fail closed when OpenCode's resolved TUI model differs from inventory metadata."""
    capture = pane_text(session)
    expected = re.sub(r"\s+", " ", expected_model_display or "").strip().lower()
    observed = re.sub(r"\s+", " ", capture).lower()
    if expected and expected not in observed:
        raise PmError(
            "opencode did not display the requested model identity before prompt injection: "
            f"expected {expected_model_display!r}; refusing possible silent fallback"
        )


def wait_until_ready(
    session: str,
    harness_executable: str,
    *,
    expected_model_display: str | None = None,
    deadline_seconds: float = 60.0,
) -> None:
    """Dispatch readiness detection on the harness executable's basename.

    codex: banner "OpenAI Codex" + "›", with stable-pane fallback.
    opencode: banner "Ask anything", with stable-pane fallback, then (when
    `expected_model_display` is given) a whitespace-normalized
    case-insensitive containment check of the display name in the pane —
    PmError on absence ("refusing possible silent fallback").
    claude/copilot/anything else: stable-pane heuristic only.
    Every readiness poll checks the trust-prompt markers and fails closed on
    them; a session that exits during the wait raises PmError.
    """
    executable = Path(harness_executable).name if harness_executable else ""
    banner_deadline = time.monotonic() + deadline_seconds
    if executable == "codex":
        _wait_banner_ready(session, "codex", lambda capture: "OpenAI Codex" in capture and "›" in capture, banner_deadline)
    elif executable == "opencode":
        _wait_banner_ready(session, "opencode", lambda capture: "Ask anything" in capture, banner_deadline)
        if expected_model_display:
            _verify_opencode_model_display(session, expected_model_display)
    else:
        _wait_stable_pane_ready(session, executable or "harness", time.monotonic() + deadline_seconds)


# --- Injection ---------------------------------------------------------------


def send_prompt(session: str, pointer: str) -> None:
    """Deliver the one-line launch pointer, then settle-and-double-C-m.

    `pointer` is the short "read your contract at <path>" line rendered by
    `prompts.render_launch_pointer`; the full multi-KB contract lives in the
    `prompt.md` file it names, not in this message. Earlier this function
    pasted the whole contract via `tmux load-buffer`/`paste-buffer`, but PM
    Test 20 (Finding 1) found some harness TUIs silently truncate a paste at
    a fixed input-buffer size (~3 KB), leaving the Developer without its
    validation plan, workflow, or hard rules. A single-line pointer is far
    below any such limit and is sent as literal keystrokes, not a paste, so
    the delivery path can no longer drop contract content.

    Refuses outright when any hard-stop marker is visible in the pane — the
    initial injection is a send like any other (target-design §3.2's
    "hard-prompt refusal on any send"), and submitting anything blind into a
    credential/approval/side-effect dialog would answer it. Refuses a
    newline: the pointer must stay a single `send-keys -l` line.

    A single C-m right after the send can be consumed finalizing the line
    instead of submitting it, so a second is sent after the TUI settles — but
    only after re-scanning the pane, so a credential/approval/side-effect
    prompt the first C-m may have surfaced is never blindly answered by the
    second (target-design §3.2's "hard-prompt refusal on any send"; when one
    C-m already submitted, withholding the second is harmless). Both C-m
    sends tolerate a session that has already exited — a fast-finishing
    harness can exit before either fires, a normal completion path, not a
    send_prompt failure.
    """
    if "\n" in pointer or "\r" in pointer:
        raise PmError("launch pointer must be a single line; the contract itself goes in the prompt.md file it names")
    hard_stop = scan_hard_stop(pane_text(session))
    if hard_stop["present"]:
        raise PmError(
            "refusing to inject the slice launch pointer into a visible hard prompt: " + ", ".join(hard_stop["kinds"])
        )
    _tmux_or_raise(["send-keys", "-t", session, "-l", "--", pointer], "tmux launch pointer send failed")
    time.sleep(1.0)
    _run_tmux("send-keys", "-t", session, "C-m")
    time.sleep(1.0)
    if session_exists(session) and not scan_hard_stop(pane_text(session))["present"]:
        _run_tmux("send-keys", "-t", session, "C-m")


def send_line(session: str, text: str) -> None:
    """A single steering line: refuses newlines, a dead session, and a visible
    hard-stop prompt; otherwise `send-keys -l -- <text>` then double-C-m."""
    if "\n" in text or "\r" in text:
        raise PmError("send text must be a single line; write multi-line content to a file and send a one-line pointer")
    if not session_exists(session):
        raise PmError(f"tmux session is not running: {session}")
    capture = pane_text(session)
    hard_stop = scan_hard_stop(capture)
    if hard_stop["present"]:
        raise PmError("refusing to send into hard prompt on screen: " + ", ".join(hard_stop["kinds"]))
    _tmux_or_raise(["send-keys", "-t", session, "-l", "--", text], "tmux literal send failed")
    time.sleep(1.0)
    _run_tmux("send-keys", "-t", session, "C-m")
    time.sleep(1.0)
    _run_tmux("send-keys", "-t", session, "C-m")


def send_correction(session: str, text: str) -> None:
    """A multi-line `finalize --steer` correction, delivered straight into
    the live pane: refuses a dead session and a visible hard-stop prompt
    exactly like `send_line`, then loads `text` into a tmux paste buffer via
    stdin (no temp file, no persistent artifact) and submits it with the
    same settle-and-double-C-m discipline as `send_prompt`.
    """
    if not session_exists(session):
        raise PmError(f"tmux session is not running: {session}")
    hard_stop = scan_hard_stop(pane_text(session))
    if hard_stop["present"]:
        raise PmError(
            "refusing to inject correction into hard prompt on screen: " + ", ".join(hard_stop["kinds"])
        )
    buffer_name = f"{session}_steer"
    _tmux_or_raise(["load-buffer", "-b", buffer_name, "-"], "tmux correction buffer load failed", input_text=text)
    try:
        _tmux_or_raise(["paste-buffer", "-b", buffer_name, "-t", session], "tmux correction paste failed")
    finally:
        # Guaranteed cleanup: a paste failure (e.g. the session exits between
        # load and paste) must not leave the correction sitting in a named
        # tmux server buffer indefinitely.
        _run_tmux("delete-buffer", "-b", buffer_name)
    time.sleep(1.0)
    _run_tmux("send-keys", "-t", session, "C-m")
    time.sleep(1.0)
    _run_tmux("send-keys", "-t", session, "C-m")

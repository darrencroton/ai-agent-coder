"""tmux session lifecycle: launch, readiness, injection, capture, liveness.

This module owns *all* tmux and harness-process contact — no other module in
this package shells out to tmux. It never judges anything beyond the
interactive-dialog marker scan (`scan_hard_stop`), which is pure text parsing
guarding PM's own keystrokes in `send_prompt`/`send_line` and reported as a
signal by `observe`. It is not a floor fact: whether a visible message means
the run must stop is the PM agent's reading, not this module's.

No injection path here sends more than one line into a pane: both `send_prompt`
and `send_line` refuse a newline, so multi-line content only ever reaches a
session as a file the pointer names. Neither bounds a line's length: the
newline refusal defeats paste splitting outright, while truncation is only
defeated by pointers staying far below any observed input limit — a guarantee
of practice, not of code. See `send_prompt` for both failure modes.

The readiness banners and dialog-marker strings are field observations of
external tools; the code around them is independent.
"""

from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import PmError
from . import TypedNotSubmitted

# --- Hard-stop markers ------------------------------------------------------

# Directory-trust / folder-trust dialogs from the interactive TUIs. If any of
# these is on screen when PM is about to submit, a blind Enter would confirm
# it (for example auto-trusting a directory) — exactly the kind of side
# effect PM must not cause.
TRUST_PROMPT_MARKERS: tuple[str, ...] = (
    "Do you trust the contents of this directory",
    "Do you trust the files in this folder",
    "Do you trust the files in this directory",
    # Qwen's phrasing, and its dialog defaults to "Trust folder" — an
    # unrecognized one would be confirmed by the launch injection's Enter.
    "Do you trust this folder",
)

_LITERAL_MARKERS: dict[str, tuple[str, ...]] = {
    "trust_prompt": TRUST_PROMPT_MARKERS,
    "approval_prompt": (
        "Do you want to proceed?",
        "Approve this action",
        "Allow this command",
        "Allow execution of",
        "Waiting for user confirmation",
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
    # Prompt-shaped phrases only: an outcome like "Permission denied" is not a
    # prompt and fires on a slice's own test evidence.
    "permission_prompt": (
        "Grant permission",
        "requires permission",
        "allow access",
    ),
}


@functools.lru_cache(maxsize=None)
def _literal_marker_re(marker: str) -> re.Pattern[str]:
    """A marker that is one bare word matches on boundaries; all others stay
    substrings.

    "MFA" as a substring matched inside unrelated words — any pane showing a
    path like `/tmp/tmpq8mfa2z1/` scanned as a credential prompt, and a false
    marker refuses every send and ends `observe --wait` early. Everything else keeps substring matching so a real prompt
    a pane renders flush against other characters ("xxxEnter API key", the
    mid-token wrap case) still matches; the accepted cost is that negated or
    quoted prose can match too ("no approval required", "disallow access"),
    which fails toward stopping and is the safer direction here.

    The test is a pure word token, not the absence of spaces: "two-factor" is
    a hyphenated phrase, and bounding it would lose "xxxTwo-factor".
    """
    body = re.escape(marker.lower())
    if not marker.isalnum():
        return re.compile(body)
    return re.compile(rf"\b{body}\b")


def scan_hard_stop(text: str) -> dict[str, Any]:
    """The interactive-dialog marker scan that guards PM's own keystrokes.

    Whitespace-normalizes and lowercases, then looks for the literal dialog
    strings above. A prompt wrapped across terminal rows still matches because
    capture rejoins wrapped lines first (`pane_text`); this normalization alone
    would not rescue a marker split mid-token.

    Deliberately narrow, and deliberately not a stop-condition oracle. Two
    keyword families used to live here and were removed: usage-limit regexes
    that stopped a run on Claude Code's informational "You've reached 85% of
    your weekly limit", and an external-side-effect regex that fired on
    ordinary domain prose ("Update the release notes for this change?").
    Both were trying to reach a semantic conclusion — that a human is being
    asked something — from keyword matching on a rendered TUI. That is the PM
    agent's reading of the pane, recorded in its assessment. What is left are
    the literal dialog strings the harnesses actually render, which is what a
    blind Enter could answer.
    """
    normalized = re.sub(r"\s+", " ", text or "")
    lowered = normalized.lower()
    matches: dict[str, Any] = {"present": False, "kinds": [], "markers": []}

    for kind, markers in _LITERAL_MARKERS.items():
        for marker in markers:
            if _literal_marker_re(marker).search(lowered):
                matches["present"] = True
                if kind not in matches["kinds"]:
                    matches["kinds"].append(kind)
                matches["markers"].append(marker)

    return matches


def marker_detail(hard_stop: dict[str, Any]) -> str:
    """Kinds plus the matched strings — a refusal that names only its kinds
    leaves PM guessing which line of the pane it objected to, and so unable to
    tell a real dialog from a false match."""
    kinds = ", ".join(hard_stop["kinds"])
    markers = "; ".join(repr(marker) for marker in hard_stop["markers"])
    return f"{kinds} (matched {markers})" if markers else kinds


# --- tmux process plumbing --------------------------------------------------


def tmux_argv(*args: str) -> list[str]:
    """The `tmux` argv to run, honouring the `PM_TMUX_SOCKET` server override.

    Unset (the default), PM uses the caller's default tmux server, so an
    operator can `tmux attach` to a Developer session in the usual way. Set,
    every PM tmux call is confined to that named server via `-L`.

    `-L` is the reliable spelling: `TMUX_TMPDIR` is ignored whenever `$TMUX`
    is already set, which is exactly the case when PM itself runs inside
    tmux — so a run that believed it was isolated would silently operate on
    the caller's real server, where a session sweep can destroy the
    operator's own windows.
    """
    socket = os.environ.get("PM_TMUX_SOCKET", "").strip()
    return ["tmux", *(("-L", socket) if socket else ()), *args]


def _run_tmux(*args: str) -> subprocess.CompletedProcess:
    argv = tmux_argv(*args)
    try:
        return subprocess.run(argv, check=False, text=True, capture_output=True)
    except OSError:
        return subprocess.CompletedProcess(args=argv, returncode=127, stdout="", stderr="tmux not found")


def _tmux_or_raise(args: list[str], error_prefix: str) -> subprocess.CompletedProcess:
    result = _run_tmux(*args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PmError(f"{error_prefix}: {detail}" if detail else error_prefix)
    return result


def session_name(run_id: str, slice_number: int, attempt: int) -> str:
    """`pm-<run_id>-s<NN>a<N>` — `sessions_for_run` recovers a run's sessions."""
    return f"pm-{run_id}-s{slice_number:02d}a{attempt}"


def sessions_for_run(run_id: str) -> list[str]:
    """Live sessions belonging to exactly `run_id`.

    Matched against the full `session_name` shape rather than by string
    prefix. A `pm-<run_id>` prefix test is not equivalent: run `X` would
    also match run `X-2`'s sessions, and `-2` is precisely the suffix
    `state.new_run_id` appends on a local id collision — so reaping one run
    could kill a different, live run's Developer session.
    """
    pattern = re.compile(rf"^pm-{re.escape(run_id)}-s\d+a\d+$")
    return [name for name in _session_names() if pattern.match(name)]


def start_session(session: str, repo: Path, command: str, env: dict[str, str]) -> None:
    """`tmux new-session -d -s <session> -c <repo> "unset PM_RUN_TOKEN; <env-prefix> <command>"`.

    Env values are shell-quoted. The Developer session's environment must
    never carry the PM run capability token — asserted defensively for the
    explicit map here, AND stripped from the inherited environment: a tmux
    session inherits the server's (ultimately the controller's) environment,
    so an exported PM_RUN_TOKEN would otherwise be visible inside every
    Developer session. The `unset` runs in the session's own shell first.
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
    """`capture-pane -p -J -S -32768`; empty string on any failure.

    `-J` rejoins lines tmux hard-wrapped at the pane width. Without it a
    marker split mid-token ("Ente" / "r API key to continue") survives
    `scan_hard_stop`'s whitespace normalization as "Ente r API key" and
    matches nothing, making marker detection depend on pane width.
    """
    result = _run_tmux("capture-pane", "-p", "-J", "-S", "-32768", "-t", session)
    return result.stdout if result.returncode == 0 else ""


def visible_pane_text(session: str) -> str:
    """`capture-pane -p -J` — the visible pane only, no scrollback.

    What the keystroke guard needs to know is whether a dialog is on screen
    *now*, so history is not merely unnecessary here but wrong: a trust
    dialog answered an hour ago still sits inside `pane_text`'s 32k lines,
    and scanning those refused every later send for the life of the session.
    Full scrollback stays the right capture for evidence (`capture_to`).
    """
    result = _run_tmux("capture-pane", "-p", "-J", "-t", session)
    return result.stdout if result.returncode == 0 else ""


def scan_live_hard_stop(session: str) -> dict[str, Any]:
    """`scan_hard_stop` over the visible pane of a live session."""
    return scan_hard_stop(visible_pane_text(session))


def capture_to(session: str, destination: Path) -> None:
    """Write pane text to `destination`; an explanatory placeholder when unavailable."""
    if not shutil.which("tmux"):
        destination.write_text("tmux was unavailable during capture\n", encoding="utf-8")
        return
    result = _run_tmux("capture-pane", "-p", "-J", "-S", "-32768", "-t", session)
    if result.returncode == 0:
        destination.write_text(result.stdout, encoding="utf-8")
    else:
        destination.write_text("tmux pane was unavailable during capture\n", encoding="utf-8")


def session_exists(session: str) -> bool:
    if not shutil.which("tmux"):
        return False
    return _run_tmux("has-session", "-t", session).returncode == 0


def _session_names() -> list[str]:
    if not shutil.which("tmux"):
        return []
    result = _run_tmux("list-sessions", "-F", "#{session_name}")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def sessions_with_prefix(prefix: str) -> list[str]:
    """Prefix sweep, for the state-independent `stop --scavenge` bare `pm-`
    case only. Prefer `sessions_for_run` whenever a run id is known: a
    prefix cannot tell run `X` from run `X-2`."""
    return [name for name in _session_names() if name.startswith(prefix)]


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
    `prompt.md` file it names, not in this message. Pasting the whole contract
    instead is unsafe twice over: some harness TUIs silently truncate a paste
    at a fixed input-buffer size (~3 KB), leaving the Developer without its
    validation plan, workflow, or hard rules; and opencode's TUI loses
    bracketed-paste state when a paste spans more than one OS read, taking
    embedded newlines as Enter presses and submitting one message as several
    fragments. A single line sent as literal keystrokes has nothing to
    misread, and the short pointer stays far below any observed input
    limit — a guarantee of practice, not of code.

    Refuses outright when any dialog marker is visible in the pane, both
    before typing and again immediately before the Enter — the initial
    injection is a send like any other, and submitting anything blind into a
    credential/approval/trust dialog would answer it. Refuses a
    newline: the pointer must stay a single `send-keys -l` line.

    A single C-m right after the send can be consumed finalizing the line
    instead of submitting it, so a second is sent after the TUI settles — but
    only after re-scanning the pane, so a credential/approval/side-effect
    prompt the first C-m may have surfaced is never blindly answered by the
    second (when one C-m already submitted, withholding the second is
    harmless). Both C-m sends tolerate a session that has already exited — a
    fast-finishing harness can exit before either fires, a normal completion
    path, not a send_prompt failure.
    """
    if "\n" in pointer or "\r" in pointer:
        raise PmError("launch pointer must be a single line; the contract itself goes in the prompt.md file it names")
    hard_stop = scan_live_hard_stop(session)
    if hard_stop["present"]:
        raise PmError(
            "refusing to inject the slice launch pointer into a visible dialog: "
            + marker_detail(hard_stop)
            + "; trust/authenticate the harness before launching, then start the slice again"
        )
    _tmux_or_raise(["send-keys", "-t", session, "-l", "--", pointer], "tmux launch pointer send failed")
    time.sleep(1.0)
    # Same settle window as `send_line`'s: a trust or credential dialog can
    # draw itself during this second, and the Enter below would confirm it.
    pre_submit = scan_live_hard_stop(session)
    if pre_submit["present"]:
        raise TypedNotSubmitted(
            "a dialog appeared while the launch pointer was being typed; refusing to submit into it: "
            + marker_detail(pre_submit)
            + " (the pointer is typed but unsubmitted; read the pane before retrying)"
        )
    _run_tmux("send-keys", "-t", session, "C-m")
    time.sleep(1.0)
    if session_exists(session) and not scan_live_hard_stop(session)["present"]:
        _run_tmux("send-keys", "-t", session, "C-m")


def send_line(session: str, text: str) -> None:
    """A single steering line — a free `send` nudge or a `finalize --steer`
    correction pointer: refuses newlines, a dead session, and a visible
    dialog marker; otherwise `send-keys -l -- <text>` then double-C-m.

    The dialog scan is not overridable. A marker literal is not a dialog
    identity — `Enter API key` in a test log and a real credential dialog scan
    identically — so no comparison may authorize a keystroke this function is
    about to send blind. Nothing is walled off by refusing: the scan reads the
    *visible* pane, so a false match from ordinary output clears as the
    Developer prints its next line and PM re-issues after the next `observe`,
    while a marker persisting on a static screen is a session that is not
    progressing — a relaunch or a stop.

    The first C-m is checked, unlike `send_prompt`'s: callers treat a return
    as delivery, and a pane exiting before it leaves the text typed but never
    submitted. The second is withheld when the pane has died or shows any
    marker — the first C-m can itself surface a credential/approval/trust
    prompt, and a blind second would answer it. Withholding is safe: if the
    first already submitted, the second was redundant.
    """
    if "\n" in text or "\r" in text:
        raise PmError("send text must be a single line; write multi-line content to a file and send a one-line pointer")
    if not session_exists(session):
        raise PmError(f"tmux session is not running: {session}")
    hard_stop = scan_live_hard_stop(session)
    if hard_stop["present"]:
        raise PmError(
            "refusing to send into a dialog on screen: "
            + marker_detail(hard_stop)
            + "; if this is ordinary output rather than a dialog it clears as the pane scrolls, so"
            " re-issue after the next observe — a marker that persists on a static screen is a"
            " relaunch or a stop"
        )
    _tmux_or_raise(["send-keys", "-t", session, "-l", "--", text], "tmux literal send failed")
    time.sleep(1.0)
    # Re-scanned immediately before the Enter, not only before the typing: the
    # settle above is a full second in which an asynchronous dialog can draw
    # itself over a pane that was clear when this call began, and it is the
    # Enter — never the typing — that would answer it.
    pre_submit = scan_live_hard_stop(session)
    if pre_submit["present"]:
        raise TypedNotSubmitted(
            "a dialog appeared while the line was being typed; refusing to submit into it: "
            + marker_detail(pre_submit)
            + " (the text is typed but unsubmitted; read the pane before retrying)"
        )
    _tmux_or_raise(["send-keys", "-t", session, "C-m"], "tmux submit failed; text typed but not sent")
    time.sleep(1.0)
    if session_exists(session) and not scan_live_hard_stop(session)["present"]:
        _run_tmux("send-keys", "-t", session, "C-m")

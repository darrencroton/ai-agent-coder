"""tmux session lifecycle: launch, readiness, injection, capture, liveness.

This module owns *all* tmux and harness-process contact — no other module
in this package shells out to tmux (implementation-blueprint.md §4). It
never judges anything beyond the hard-stop marker scan (`scan_hard_stop`,
target-design §11's marker floor), which is pure text parsing shared by
`send_line`, Stage 3's `observe`, and `floor.py`'s fact 8.

Behaviour is re-specified from the current implementation's field-proven
tmux adapter (see the Stage 2 brief's old-evidence pointer). Per
``docs/mode-b-lite/replacement-ledger.md`` §9.1, the recorded readiness
banners and hard-stop marker/phrasing strings are the sanctioned data
carry-over — observations of external tools, not architecture; the code
around them is written fresh.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
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


# --- tmux process plumbing --------------------------------------------------


def _run_tmux(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *args], check=False, text=True, capture_output=True)
    except OSError:
        return subprocess.CompletedProcess(args=["tmux", *args], returncode=127, stdout="", stderr="tmux not found")


def _tmux_or_raise(args: list[str], error_prefix: str) -> subprocess.CompletedProcess:
    result = _run_tmux(*args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PmError(f"{error_prefix}: {detail}" if detail else error_prefix)
    return result


def session_name(run_id: str, slice_number: int, attempt: int) -> str:
    """`pm-<run_id>-s<NN>a<N>` — the scavenge path sweeps the `pm-<run_id>` prefix."""
    return f"pm-{run_id}-s{slice_number:02d}a{attempt}"


def start_session(session: str, repo: Path, command: str, env: dict[str, str]) -> None:
    """`tmux new-session -d -s <session> -c <repo> "<env-prefix> <command>"`.

    Env values are shell-quoted. The Developer session's environment must
    never carry the PM run capability token (target-design §8) — this is
    asserted defensively here, not just upheld by callers.
    """
    if "PM_RUN_TOKEN" in env:
        raise PmError("session environment must never contain PM_RUN_TOKEN")
    if not shutil.which("tmux"):
        raise PmError("tmux is required for runtime execution")
    env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    shell_command = f"{env_prefix} {command}".strip() if env_prefix else command
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
    deadline_seconds: float = 20.0,
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


def send_prompt(session: str, prompt_path: Path) -> None:
    """load-buffer/paste-buffer/delete-buffer, then settle-and-double-C-m.

    A single C-m sent right after paste-buffer can be consumed finalizing the
    pasted multi-line block instead of submitting it (confirmed by
    reproduction in the old-evidence adapter); a second C-m after the TUI
    settles reliably submits it. Both sends tolerate a session that has
    already exited — a fast-finishing harness can exit before either fires,
    which is a normal completion path, not a send_prompt failure.
    """
    buffer_name = f"{session}_prompt"
    _tmux_or_raise(["load-buffer", "-b", buffer_name, str(prompt_path)], "tmux prompt load failed")
    _tmux_or_raise(["paste-buffer", "-b", buffer_name, "-t", session], "tmux prompt paste failed")
    _run_tmux("delete-buffer", "-b", buffer_name)
    time.sleep(1.0)
    _run_tmux("send-keys", "-t", session, "C-m")
    time.sleep(1.0)
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

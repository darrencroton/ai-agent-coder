#!/usr/bin/env python3
"""PreToolUse guard: refuse a poll that can return nothing the harness won't tell you.

A PM session backgrounds a wait (Developer session, reviewer, test run) and
then looks to see if it finished — redundant, since Claude Code re-invokes
the agent on exit, and expensive, since each look resends the whole
conversation. Measured on one 1391-turn session: 346 task-output re-reads
(273 byte-identical) plus 32 backgrounded hand-rolled waiters, ~26% of turns.
These are harness calls, not PM commands, so the toolkit itself cannot see or
bound them.

Denies two shapes:

`Read` — a re-read byte-identical to the file's previous read. First read and
any read returning new bytes still pass.

`Bash` — a BACKGROUNDED command that waits then inspects a PM artifact
(`until [ -s .../tasks/<id>.output ]; do sleep 30; done`, and similarly
against a review report or result.json). Keyed on the wait plus the target,
never the inspector — the 32 measured polls used 7 different ones (grep,
cat, head, git log, test, tail, ls), so an inspector allowlist would miss
most.

Scope: both require `.pm/` in cwd. `Read` additionally requires Claude
Code's own scratchpad task-output path. `Bash` additionally requires
`run_in_background: true` plus a wait; `pm.py review`/`observe --wait` are
exempt as the toolkit's own legitimate waiters. Foreground is always
allowed — no second wake, and the escape hatch when no notification is
coming.

Fails open on any unexpected input or error — this bounds spend, and must
never be why a run gets stuck.

Text-matched, not shell-parsed, by design: quote-aware parsing would be more
machinery than the guard it serves. Known gaps, none seen in the 290 calls
this was built against: exemption via a quoted `pm.py review`, a directory
name that shell-concatenates into the mirror path, dynamically built paths,
`read -t`, non-shell sleeps, and a non-standard `$GIT_DIR` layout. All fail
toward allow, and the foreground form is always the fix.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

# Claude Code's scratchpad task-output layout. `$`-anchored vets a Read's
# file_path; unanchored searches inside a Bash command string.
_TASK_OUTPUT_BODY = r"/claude-\d+/[^/]+/[0-9a-f-]{36}/tasks/(?P<task_id>[A-Za-z0-9_-]+)\.output"
_TASK_OUTPUT_RE = re.compile(_TASK_OUTPUT_BODY + r"$")
_TASK_OUTPUT_IN_COMMAND_RE = re.compile(_TASK_OUTPUT_BODY)

# A PM run id: `<UTC stamp>-<3-byte nonce>`, plus the `-2`, `-3`, ... collision
# suffix (pm_lib/state.py new_run_id).
_RUN_ID = r"\d{8}T\d{6}Z-[0-9a-f]{6}(?:-\d+)?"
# Authoritative state dir and its in-repo mirror (pm_lib/state.py
# worktree_git_dir) — matched by directory layout, not just the run-id shape,
# so an unrelated path shaped like a run id is never denied. The mirror lags
# the original, so a still-running review only shows up under `.git/pm/`.
# `_BOUNDARY`: match must start the string or follow a separator —
# `archive-.pm/runs/...` previously slipped through on the `-`.
_BOUNDARY = r"""(?<![^\s;&|()<>"'/])"""
_PM_ARTIFACT_RE = re.compile(
    rf"{_BOUNDARY}\.git/(?:worktrees/[^/\s]+/)?pm/{_RUN_ID}/"
    rf"|{_BOUNDARY}\.pm/runs/{_RUN_ID}/"
)

# Toolkit's own waiters — `observe --wait`, `review` — exempt so a compound
# command like `pm.py review …; sleep 5; cat <report>` still works.
_PM_COMMAND_RE = re.compile(r"pm\.py\s+(?:review|observe)\b")

# Stripped before matching: an appended `# pm.py review` would otherwise
# satisfy the exemption above and disable the guard. Quote-unaware by
# design — the only cost of stripping a `#` inside a quoted string is a
# missed deny, the safe direction.
_COMMENT_RE = re.compile(r"(?:^|(?<=[\s;&|(]))#[^\n]*")

# Every hand-rolled waiter measured used one of these; `until`/`while` loops
# all carry `do sleep N; done`, so the loop keyword itself needs no pattern.
_WAIT_RE = re.compile(r"\bsleep\s+\d|\btail\s+-[fF]\b")

_STAMP_DIR = Path.home() / ".claude" / "hooks" / ".pm-poll-guard"

_DENY_REASON = (
    "Blocked: this background task has produced no new output since your last read, "
    "so this call cannot tell you anything you do not already know — and it costs a "
    "full context re-read to learn that. You do not need to poll: Claude Code "
    "re-invokes you automatically when a background command exits. End the turn and "
    "wait for that. If you believe the task is genuinely stuck, act on it (send a "
    "nudge, steer, relaunch, or stop it) rather than reading again."
)

_BASH_DENY_REASON = (
    "Blocked: this backgrounds a command that waits and then inspects a PM artifact — "
    "a second observer of something that already has a completion notification coming. "
    "It costs two turns (this launch, plus the wake when its own sleep finishes) to "
    "learn what the existing notification would have told you for free. End the turn "
    "and wait for that. If you need to look right now, run the same command in the "
    "FOREGROUND (drop run_in_background) — that is always allowed, and is the right "
    "tool when no notification is coming. To wait on a Developer session, use "
    "`pm.py observe --wait N` with a single long wait."
)


def _allow() -> None:
    sys.exit(0)


def _deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def _in_pm_run(payload: dict) -> bool:
    cwd = payload.get("cwd")
    return bool(cwd) and (Path(cwd) / ".pm").is_dir()


def _check_bash(payload: dict) -> None:
    """Deny a backgrounded wait-then-inspect on a PM artifact. Never returns a value.

    Ordered cheapest-first, so an ordinary command is allowed on string
    operations alone and never pays for the `_in_pm_run` stat.
    """
    tool_input = payload.get("tool_input") or {}
    # Foreground is always fine: it blocks the turn but creates no second wake.
    if tool_input.get("run_in_background") is not True:
        _allow()

    raw = tool_input.get("command")
    if not isinstance(raw, str) or not raw:
        _allow()

    command = _COMMENT_RE.sub(" ", raw)
    if _PM_COMMAND_RE.search(command):
        _allow()
    if not _WAIT_RE.search(command):
        _allow()
    if not (_TASK_OUTPUT_IN_COMMAND_RE.search(command) or _PM_ARTIFACT_RE.search(command)):
        _allow()
    if not _in_pm_run(payload):
        _allow()

    _deny(_BASH_DENY_REASON)


def _check_read(payload: dict) -> None:
    """Deny a re-read returning bytes identical to the previous one. Never returns a value."""
    file_path = (payload.get("tool_input") or {}).get("file_path") or ""

    match = _TASK_OUTPUT_RE.search(file_path)
    if not match:
        _allow()
    # Checked only once the path already looks like a task output, so an
    # unrelated Read never triggers a filesystem call it did not before.
    if not _in_pm_run(payload):
        _allow()

    target = Path(file_path)
    content = target.read_bytes() if target.is_file() else b""
    digest = hashlib.sha256(content).hexdigest()

    session = str(payload.get("session_id") or "nosession")
    # Session-scoped: two runs polling the same task id must not collide, and a
    # stale stamp from an old session must never deny a fresh read.
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{session}-{match.group('task_id')}")
    stamp = _STAMP_DIR / safe

    previous = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
    if previous == digest:
        _deny(_DENY_REASON)

    _STAMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest, encoding="utf-8")
    _allow()


def main() -> None:
    payload = json.load(sys.stdin)

    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool is None:
        # No tool_name: fall back to the input's shape, but only when exactly
        # one discriminator is present. The matcher in settings.json already
        # selects the tool, so this only covers a payload that omits the field;
        # guessing between two candidate shapes could deny a call the hook
        # cannot even identify.
        candidates = [name for name in ("command", "file_path") if name in tool_input]
        tool = {"command": "Bash", "file_path": "Read"}.get(
            candidates[0] if len(candidates) == 1 else ""
        )

    # Each branch applies the PM-run gate itself, once it knows the call is a
    # candidate — an unrelated call must not pay for a filesystem stat.
    if tool == "Bash":
        _check_bash(payload)
    elif tool == "Read":
        _check_read(payload)
    _allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        _allow()  # Fail open, always.

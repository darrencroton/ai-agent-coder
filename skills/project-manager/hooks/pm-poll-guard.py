#!/usr/bin/env python3
"""PreToolUse guard: refuse a poll that can return nothing the harness won't tell you.

Why this exists
---------------
A supervising PM session backgrounds long commands (a Developer wait, a
commissioned review, a test run) and then goes looking to see whether they
finished. Claude Code already re-invokes the agent when a background command
exits, so that looking is redundant — but each look costs a full model
round-trip that resends the whole conversation.

Measured on one 1391-turn Claude Code session supervising two PM runs:
346 task-output Reads (273 of them byte-identical repeats, with same-file
streaks of 64 and 77) plus 32 backgrounded hand-rolled waiters — together
about 26% of the session's turns.

The toolkit cannot see any of this (these are harness calls, not PM
commands), so the guard has to live here.

What it denies
--------------
Two shapes, one per tool.

`Read` — a re-read whose content is byte-identical to what the previous read
of that same task-output file returned. Provably no new information. The
first read of a file always passes, and so does any read that would return
new bytes: a notification answers "finished?", not "progressing or hung?",
and one interim look before nudging is legitimate.

`Bash` — a BACKGROUNDED command that waits and then inspects a PM artifact.
Backgrounding such a command is itself the waste: it schedules a second
completion notification for a target that already has one coming, so the
agent wakes twice and learns nothing the first wake would not have carried.
The measured forms are hand-rolled waiters:

    until [ -s .../tasks/<id>.output ]; do sleep 30; done; ...
    until grep -qi "^## Verdict" .git/pm/<run>/slices/slice-002/review-4-*.md; do sleep 60; done
    until [ -f .pm/runs/<run>/slices/slice-003/result.json ]; do sleep 60; done

Keyed on the WAIT and the TARGET, never on the inspector. Across the 32
measured polls the inspectors were grep (17), cat (13), head (8), git log
(7), test -s (5), test -f (5), tail (3) and ls (2) — an allowlist of
"reading" commands would have missed most of them.

Scope
-----
Gates that keep this out of everything else:
  * The session's working directory contains `.pm/` — i.e. this really is a
    PM run. Required for both tools.
  * `Read`: the path is Claude Code's own scratchpad task-output layout
    (.../claude-<uid>/<project>/<session-uuid>/tasks/<id>.output). A
    repository with its own `tasks/` directory cannot match.
  * `Bash`: `run_in_background` is true, AND the command waits, AND it names
    a Claude task output or a path under a PM run state dir or its mirror.
    Commands that invoke pm.py are exempt — `observe --wait` and `review`
    are the toolkit's own legitimate waiters.

A FOREGROUND wait is always allowed. It blocks the turn but spawns no extra
wake, and it is the escape hatch when no notification is genuinely coming
(after a session resume, say). The deny message says so.

Fails open. Any unexpected input, missing field, or error allows the call:
this bounds spend, and must never be the reason a run gets stuck.

How precise this is, honestly
-----------------------------
The Bash branch matches text; it does not parse shell, and deliberately does
not try to. A quote-aware scanner would be more machinery than the thing it
guards: this bounds spend on a seat that is trusted by construction, and the
threat model here is a PM cutting corners, not one evading a cost guard.

So the following are known and accepted, all of them absent from the 290 real
Bash calls this was built against:

  * A poll can be exempted by mentioning `pm.py review` in a quoted string
    (`echo 'pm.py review'; sleep 60; cat <artifact>`). That is evasion, not an
    accident, and evasion is out of scope.
  * A directory contrived to look like the mirror after a non-boundary
    character — `archive".pm/runs/<run-id>/"`, which the shell concatenates —
    is denied. Recovery is one edit: drop `run_in_background`.
  * Dynamically built paths (`d=...; sleep 900; tail "$d/report"`), `read -t`,
    and non-shell sleeps are not recognised.

Both directions are bounded. A missed poll costs the turns this exists to
save; a wrong deny costs one denied call that the foreground form runs
unchanged. Neither can stop a run.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

# Claude Code's scratchpad task-output layout, anchored tightly enough that a
# repository's own tasks/ directory cannot match. The `$`-anchored form vets a
# Read's file_path; the unanchored form searches inside a Bash command string.
_TASK_OUTPUT_BODY = r"/claude-\d+/[^/]+/[0-9a-f-]{36}/tasks/(?P<task_id>[A-Za-z0-9_-]+)\.output"
_TASK_OUTPUT_RE = re.compile(_TASK_OUTPUT_BODY + r"$")
_TASK_OUTPUT_IN_COMMAND_RE = re.compile(_TASK_OUTPUT_BODY)

# A PM run id: `<UTC stamp>-<3-byte nonce>`, plus the `-2`, `-3`, ... suffix
# new_run_id appends on collision (pm_lib/state.py).
_RUN_ID = r"\d{8}T\d{6}Z-[0-9a-f]{6}(?:-\d+)?"
# Authoritative state dir and its in-repo mirror. The live reviewer report is
# the ORIGINAL under the state dir — the mirror is only written after the
# reviewer exits cleanly — so matching just `.pm/` would miss every poll of a
# review still running. `worktree_git_dir` puts the state dir at
# `<repo>/.git/pm/<run-id>/`, or `<main>/.git/worktrees/<name>/pm/<run-id>/` for
# a linked worktree.
#
# The run-id shape alone is NOT enough to call a path a PM artifact: an
# unrelated `/srv/data/pm/<something shaped like a run id>/` would match, and
# denying that is a wrong answer under fail-open. So the match is anchored to
# the directory layout the toolkit actually creates. The lookbehind rejects a
# bare suffix — `archive.pm/runs/...` is somebody else's directory, not the
# mirror. A repository with a non-standard `$GIT_DIR` simply will not match, and
# allowing is the right failure direction.
#
# `_BOUNDARY` is a single-character negative lookbehind: the match must start the
# string or follow a separator. Excluding only word characters and dots was not
# enough — `archive-.pm/runs/...` is somebody else's directory and slipped
# through on the `-`.
_BOUNDARY = r"""(?<![^\s;&|()<>"'/])"""
_PM_ARTIFACT_RE = re.compile(
    rf"{_BOUNDARY}\.git/(?:worktrees/[^/\s]+/)?pm/{_RUN_ID}/"
    rf"|{_BOUNDARY}\.pm/runs/{_RUN_ID}/"
)

# The toolkit's own waiters, which must never be denied: `observe --wait` blocks
# on the Developer session and `review` blocks on the reviewer it just launched.
# Neither matches the wait-plus-artifact shape on its own, so this exists only to
# keep a compound form like `pm.py review …; sleep 5; cat <report>` working.
_PM_COMMAND_RE = re.compile(r"pm\.py\s+(?:review|observe)\b")

# Shell comments, stripped before anything is matched. Without this, appending
# `# pm.py review` to any poll satisfied the exemption above and disabled the
# guard entirely — and a commented-out artifact path could equally have caused a
# deny. Quote tracking is deliberately not attempted: the only cost of stripping
# a `#` that was really inside a quoted string is a missed deny, which is the
# safe direction.
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

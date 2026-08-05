#!/usr/bin/env python3
"""PreToolUse(Read) guard: refuse a background-task poll that can return nothing new.

Why this exists
---------------
A supervising PM session backgrounds long commands (a Developer wait, a
commissioned review, a test run) and then re-reads the task's output file to
see whether it finished. Claude Code already re-invokes the agent when a
background command exits, so those reads are redundant — but each one costs a
full model round-trip that resends the whole conversation. Measured on one
687-turn PM run: 346 of 687 turns were exactly this, ~$83 of a $161 bill,
slightly more than every productive turn combined.

The toolkit cannot see these calls (they are harness Reads, not PM commands),
so the guard has to live here.

What it denies
--------------
Only a re-read whose content is byte-identical to what the previous read of
that same file already returned — provably no new information. The first read
of a file always passes, and any read that would return new bytes passes.

Scope
-----
Both gates must hold, so nothing outside a PM run is ever affected:
  1. The path is Claude Code's own scratchpad task-output layout
     (.../claude-<uid>/<project>/<session-uuid>/tasks/<id>.output).
     A repository with its own `tasks/` directory cannot match.
  2. The session's working directory contains `.pm/` — i.e. this really is a
     PM run. Deliberately keyed on the session, not on which command was
     backgrounded: PM hand-rolls its own `until ...; do sleep; done` waiters
     as well as running pm.py, and polling either is equally wasteful.

Fails open. Any unexpected input, missing field, or error allows the read:
this bounds spend, and must never be the reason a run gets stuck.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

# Claude Code's scratchpad task-output layout, anchored tightly enough that a
# repository's own tasks/ directory cannot match.
_TASK_OUTPUT_RE = re.compile(
    r"/claude-\d+/[^/]+/[0-9a-f-]{36}/tasks/(?P<task_id>[A-Za-z0-9_-]+)\.output$"
)

_STAMP_DIR = Path.home() / ".claude" / "hooks" / ".pm-poll-guard"

_DENY_REASON = (
    "Blocked: this background task has produced no new output since your last read, "
    "so this call cannot tell you anything you do not already know — and it costs a "
    "full context re-read to learn that. You do not need to poll: Claude Code "
    "re-invokes you automatically when a background command exits. End the turn and "
    "wait for that. If you believe the task is genuinely stuck, act on it (send a "
    "nudge, steer, relaunch, or stop it) rather than reading again."
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


def main() -> None:
    payload = json.load(sys.stdin)
    file_path = (payload.get("tool_input") or {}).get("file_path") or ""

    match = _TASK_OUTPUT_RE.search(file_path)
    if not match:
        _allow()

    # Gate 2: only inside a PM run.
    cwd = payload.get("cwd")
    if not cwd or not (Path(cwd) / ".pm").is_dir():
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


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        _allow()  # Fail open, always.

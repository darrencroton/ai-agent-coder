"""Command orchestration for the slice lifecycle (target-design §3/§12).

This module wires the pieces other `pm_lib` modules already provide —
`state`, `plan`, `git_ops`, `sessions`, `profiles`, `prompts` — into the
per-command sequences described in target-design. Most of it still decides
nothing semantic: `init`/`status`/`approve`/`start-slice`/`observe` and bare
`finalize` only mutate state through the token-authenticated `state` module,
drive the headless Developer process through `sessions`, or read
git/filesystem facts through `git_ops` — `floor.py` computes the facts, never
a verdict.

The one place semantic judgement enters this module is `finalize_accept` /
`finalize_steer` / `finalize_stop`: each is an explicit, recorded act the PM
agent takes through the CLI (never inferred from evidence alone), gated by
the floor (never waivable) and, on elevated slices, by review freshness
(design §5). Assessment text assembles facts around the PM's own reasoning
text; it never invents that reasoning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import IntegrityError, PmError
from . import git_ops
from . import plan as plan_mod
from . import profiles
from . import prompts
from . import sessions
from . import state as state_mod
from .floor import FloorReport, evaluate_floor

_SLICE_ID_RE = re.compile(r"^Slice\s+(?P<number>\d+)$")

# Artifact rotation / observe polling.
_OBSERVE_POLL_SECONDS = 2.0
_OBSERVE_TAIL_LINES = 40
# Per-slice record of the outfile size seen by the previous `observe`, so
# "output changed" means growth since the last observation.
_OBSERVE_CURSOR_FILE = "observe-cursor.txt"

# Controller-owned notes.md tripwire (target-design §10): a hard cap kept as
# a non-fatal warning, since a runaway notes file silently degrades every
# later Developer prompt.
_NOTES_SIZE_CAP_BYTES = 512 * 1024
# Branches a run must never land on by *implicit* default (an explicit
# --branch main is still honoured); per-slice commits piling onto a shared
# default branch is the PM Test 20 branch-default footgun.
_PROTECTED_DEFAULT_BRANCHES = frozenset({"main", "master"})

# The stop_reason recorded on attempt-budget exhaustion. Load-bearing: the
# exhaustion guard below matches on it, which is what makes the budget a
# genuine terminal stop (design §11 "mandatory stop") rather than a status
# note — after exhaustion, only `finalize --stop` (record the story) and
# `stop` remain available for the slice.
_BUDGET_EXHAUSTED_REASON = "attempt budget exhausted"

# Launch-bound session-id correlation. PM never queries a bare "newest
# session": an id is bound to *this* launch either by construction (a
# launch-set uuid for claude/copilot) or by matching this launch's exact
# emitted id / harness-store record (its own pointer + repo cwd + start-time
# window). Anything ambiguous, missing, or unverifiable stays None so
# `finalize --steer` fails closed.
#
# A `--harness-command` override prints its own launch id on a dedicated,
# exact line; PM captures that (never synthesizes one). An override that emits
# no such line has no resumable id and steer blocks.
_OVERRIDE_SESSION_ID_RE = re.compile(r"^\s*PM_DEVELOPER_SESSION_ID\s*[:=]\s*(\S+)\s*$", re.MULTILINE)
# codex prints `session id: <uuid>` near the top of its exec output.
_CODEX_STDOUT_ID_RE = re.compile(r"^\s*session id:\s*([0-9a-f-]+)\s*$", re.MULTILINE | re.IGNORECASE)
# A canonical vendor session-id shape used when reading a store filename.
_SESSION_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
# Bounded slack (seconds) around this launch's start time for store matching.
_CORRELATION_WINDOW_SLACK_S = 5.0
# `finalize --steer` gathers this launch's own session id from the still-live
# prior turn with a bounded wait before quiescing it (a harness prints/records
# its id shortly after launch, not synchronously with Popen). The wait is
# fail-closed: it never synthesizes an id or accepts a newest session, and it
# stops early once the id is found or the output already shows a hard prompt
# that will be refused.
_STEER_CAPTURE_TIMEOUT_S = 5.0
_STEER_CAPTURE_POLL_S = 0.1


def _refuse_if_budget_exhausted(state: dict[str, Any]) -> None:
    if state.get("status") == "needs-human" and state.get("stop_reason") == _BUDGET_EXHAUSTED_REASON:
        raise PmError(
            "attempt budget exhausted for the current slice; record the outcome with "
            "finalize --stop (or stop the run) — steering and acceptance are closed"
        )


# --- Path helpers ------------------------------------------------------------


def pm_dir(repo: Path) -> Path:
    return repo / ".pm"


def runs_root(repo: Path) -> Path:
    return pm_dir(repo) / "runs"


def run_artifact_dir(repo: Path, run_id: str) -> Path:
    return runs_root(repo) / run_id


def notes_path(repo: Path, run_id: str) -> Path:
    return run_artifact_dir(repo, run_id) / "notes.md"


def slice_number(slice_id: str) -> int:
    match = _SLICE_ID_RE.match(slice_id)
    if not match:
        raise PmError(f"slice id {slice_id!r} is not in the expected 'Slice <N>' shape")
    return int(match.group("number"))


def slice_artifact_dir(repo: Path, run_id: str, slice_id: str) -> Path:
    return run_artifact_dir(repo, run_id) / "slices" / f"slice-{slice_number(slice_id):03d}"


def write_pm_gitignore(repo: Path) -> None:
    """A self-ignoring `.pm/.gitignore` (a bare `*`) so the artifact tree never
    needs individual entries in the repository's own `.gitignore`."""
    directory = pm_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    gitignore.write_text("*\n", encoding="utf-8")


# --- Controller-owned originals + mirrors (target-design §8 item 3, §9) ------
#
# PM-authored artifacts (notes.md, run-report.md, assessment.md, review
# reports) have their AUTHORITATIVE ORIGINAL under the run's state dir
# (outside the worktree, alongside run.json) and are MIRRORED into `.pm/`
# for human reading. Nothing is ever read back from the mirror for control
# decisions — only these write helpers touch the mirror side.


def notes_original_path(run_dir: Path) -> Path:
    return run_dir / "notes.md"


def slice_state_dir(run_dir: Path, slice_id: str) -> Path:
    return run_dir / "slices" / f"slice-{slice_number(slice_id):03d}"


def mirror_artifact(repo: Path, run_dir: Path, run_id: str, relative_path: str) -> Path:
    """Copy an already-written ORIGINAL (under `run_dir`) to its `.pm/`
    mirror location, creating any missing mirror directories (this is what
    lets the report/mirror tree regenerate correctly after `.pm/` has been
    deleted entirely). Returns the original path."""
    original = run_dir / relative_path
    mirror = run_artifact_dir(repo, run_id) / relative_path
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_bytes(original.read_bytes())
    return original


def write_controller_artifact(repo: Path, run_dir: Path, run_id: str, relative_path: str, content: str) -> Path:
    """Write a PM-authored controller-owned artifact: the ORIGINAL under
    `run_dir`, then its `.pm/` mirror. Returns the original path."""
    original = run_dir / relative_path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text(content, encoding="utf-8")
    return mirror_artifact(repo, run_dir, run_id, relative_path)


def write_notes(repo: Path, run_dir: Path, run_id: str, *, text: str, mode: str) -> tuple[Path, str | None]:
    """Update the run notes safely: write the AUTHORITATIVE original under the
    run state dir, then re-mirror into `.pm/` (`write_controller_artifact`).

    This is the only sanctioned writer of `notes.md`. The `.pm/` mirror is
    regenerate-only, so a direct hand-edit to it is silently clobbered by the
    next `start-slice` re-mirror (PM Test 20 secondary finding); routing every
    notes update through here removes that footgun. `mode` is "append" (add
    `text` as a new trailing block, separated by a blank line) or "set"
    (replace the whole file). Returns the original path and an optional
    over-cap warning.
    """
    if not text.strip():
        raise PmError("notes text must be non-empty (nothing to append or set)")
    original = notes_original_path(run_dir)
    if mode == "append":
        existing = original.read_text(encoding="utf-8") if original.exists() else ""
        if existing.strip():
            if not existing.endswith("\n"):
                existing += "\n"
            content = f"{existing}\n{text.rstrip()}\n"
        else:
            content = f"{text.rstrip()}\n"
    elif mode == "set":
        content = f"{text.rstrip()}\n"
    else:
        raise PmError(f"unknown notes mode: {mode!r}")
    write_controller_artifact(repo, run_dir, run_id, "notes.md", content)
    size = original.stat().st_size
    warning: str | None = None
    if size > _NOTES_SIZE_CAP_BYTES:
        warning = (
            f"notes.md is {size} bytes, over the {_NOTES_SIZE_CAP_BYTES}-byte (512 KiB) cap; "
            "a runaway notes file silently degrades every later Developer prompt — curate it down"
        )
    return original, warning


def regenerate_report(repo: Path, run_dir: Path, state: dict[str, Any]) -> Path:
    """Regenerate `run-report.md` from controller-owned data alone and
    write its original + mirror. Needs no token: this writes a plain file,
    never `run.json`."""
    events = state_mod.read_events(run_dir)
    text = state_mod.render_run_report(state, events, run_dir)
    return write_controller_artifact(repo, run_dir, state["run_id"], "run-report.md", text)


# --- Shared state access ------------------------------------------------------


def repo_from_cwd(cwd: Path) -> Path:
    return git_ops.resolve_repo(cwd)


def load_writable_state(run_dir: Path, token: str) -> dict[str, Any]:
    """Load + MAC-verify state for a mutating command.

    An integrity failure is terminal by construction: the unauthenticated
    run.json is deliberately NOT rewritten or re-signed — re-signing would
    turn attacker-controlled bytes (say, a Developer marking its own slice
    accepted) into MAC-valid state. Left unsigned, every future mutating
    command keeps failing closed on the same IntegrityError, the tampered
    file survives as evidence, and recovery is the operator's decision
    (start a new run). Only the append-only event log records the
    detection, since events carry no authority.
    """
    try:
        return state_mod.load_state(run_dir, token)
    except IntegrityError as exc:
        try:
            state_mod.append_event(run_dir, "stop", note=f"state integrity check failed: {exc}")
        except PmError:
            pass
        raise


def slice_entry(state: dict[str, Any], slice_id: str) -> dict[str, Any] | None:
    for entry in state.get("slices", []):
        if isinstance(entry, dict) and entry.get("id") == slice_id:
            return entry
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- headless Developer process helpers (target-design §3/§12) ----------------


def developer_sidecar_path(repo: Path, run_id: str) -> Path:
    """`developer.pid` sidecar location: per-run, under the `.pm/` mirror.

    Placed under the run's artifact dir (not the state dir) so it survives a
    deleted state directory and is discoverable by `stop --scavenge --run
    <id>` from the repo and run id alone. `stop_scavenge_sweep` documents what
    happens when this sidecar is gone too.
    """
    return run_artifact_dir(repo, run_id) / sessions.DEVELOPER_PID_SIDECAR


def _developer_env(
    *, artifact_dir: Path, plan_path: Path, slice_id: str, notes_path: Path, result_path: Path
) -> dict[str, str]:
    """The PM_* environment every Developer turn (launch or resume) receives.

    Never carries PM_RUN_TOKEN — `sessions.launch_headless` asserts its
    absence and strips it from the inherited environment as a second guard.
    """
    tmp_dir = artifact_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return {
        "PM_SLICE_ARTIFACT_DIR": str(artifact_dir),
        "PM_PLAN_PATH": str(plan_path),
        "PM_SLICE_ID": slice_id,
        "PM_NOTES_PATH": str(notes_path),
        "PM_RESULT_PATH": str(result_path),
        "TMPDIR": str(tmp_dir),
    }


RESUME_SESSION_ENV_VAR = "PM_DEVELOPER_RESUME_SESSION_ID"


def _launch_developer(command: str, repo: Path, env: dict[str, str], artifact_dir: Path) -> dict[str, Any]:
    """Launch a Developer turn with the resume env var under PM's sole control.

    ``sessions.launch_headless`` copies the controller's environment (minus
    PM_RUN_TOKEN) and overlays ``env``; an inherited PM_DEVELOPER_RESUME_SESSION_ID
    would otherwise leak into an *initial* launch and be honoured as a resume.
    So the var is stripped from the controller environment for the duration of
    the launch and restored afterwards; the child then sees exactly what ``env``
    provides — nothing on a launch, the captured id on an override resume.
    """
    prior = os.environ.pop(RESUME_SESSION_ENV_VAR, None)
    try:
        return sessions.launch_headless(command, repo, env, artifact_dir)
    finally:
        if prior is not None:
            os.environ[RESUME_SESSION_ENV_VAR] = prior


def _outfile_size(outfile: Path) -> int:
    """Captured-output size in bytes; an absent/unreadable outfile counts as
    zero bytes observed, so it compares equal to "nothing seen yet"."""
    try:
        return outfile.stat().st_size
    except OSError:
        return 0


def _read_observe_cursor(cursor_path: Path) -> int:
    """Bytes of captured output the previous `observe` saw; zero if none."""
    try:
        return int(cursor_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_observe_cursor(cursor_path: Path, size: int) -> None:
    """Record this observation's outfile size. Best-effort: a cursor that
    cannot be written only costs the next `observe` its growth signal, so it
    must never fail an otherwise successful read-only observation."""
    try:
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(f"{size}\n", encoding="utf-8")
    except OSError:
        pass


def _terminate_current(current: dict[str, Any]) -> bool:
    """Terminate the identity-checked process group recorded in `current`.

    Returns True when a live group was terminated, False when there was
    nothing to signal (leader gone, or the PID was reused — the PID-reuse-safe
    "nothing of ours to kill" case). A ``PmError`` from ``terminate_headless``
    (a group that survived SIGKILL, or a reused leader that still owns the
    group) is deliberately NOT swallowed: a caller must never claim success or
    clear the slice's authority when the tracked process could not be killed.
    """
    pid = current.get("pid")
    pgid = current.get("pgid")
    identity = current.get("identity")
    if not (pid and pgid and identity):
        return False
    return sessions.terminate_headless(int(pid), int(pgid), str(identity))


def _abort_launch(launch: dict[str, Any]) -> None:
    """Tear down a just-launched turn whose post-launch bookkeeping failed.

    Between ``Popen`` and the authenticated state write there is a window in
    which the sidecar or state write can fail (a held lock, a full disk). The
    headless model has no global process list, so a launch left behind by that
    window would be an autonomous Developer editing the repo with *no* durable
    handle. Terminating here keeps the failure closed. Best-effort by design:
    the bookkeeping error is the one worth reporting, so a termination failure
    must not mask it.
    """
    try:
        sessions.terminate_headless(int(launch["pid"]), int(launch["pgid"]), str(launch["identity"]))
    except (PmError, OSError, KeyError, TypeError, ValueError):
        pass


# --- launch-bound session-id correlation (PM-owned; shares no orchestrator code)
#
# The per-vendor store layouts and matching rules below are a thin PM copy of
# behaviour also implemented in the orchestrator's delegate_sessions.py; per the
# plan they must stay factually consistent but share no code and never import
# it. Store roots are module functions so tests can redirect them to a temp
# tree without touching the real user home.


def _codex_sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _opencode_session_db() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _qwen_chats_root(cwd: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return Path.home() / ".qwen" / "projects" / slug / "chats"


def _read_output_head(outfile: Path, *, max_bytes: int = 8192) -> str:
    try:
        with outfile.open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _override_stdout_session_id(outfile: Path) -> str | None:
    """The launch id a `--harness-command` override printed on its own exact
    line, read only from THIS launch's captured output. None when absent —
    PM never synthesizes an override id."""
    head = _read_output_head(outfile)
    match = _OVERRIDE_SESSION_ID_RE.search(head) or _OVERRIDE_SESSION_ID_RE.search(
        sessions.read_output_tail(outfile)
    )
    return match.group(1) if match else None


def _codex_stdout_session_id(outfile: Path) -> str | None:
    match = _CODEX_STDOUT_ID_RE.search(_read_output_head(outfile))
    return match.group(1) if match else None


def _resolve_path(value: str | Path) -> Path | None:
    try:
        return Path(value).expanduser().resolve()
    except OSError:
        return None


def _codex_candidate_cwd_matches(path: Path, cwd: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(3):
                line = handle.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "session_meta":
                    continue
                session_cwd = row.get("payload", {}).get("cwd")
                if not isinstance(session_cwd, str):
                    return False
                resolved = _resolve_path(session_cwd)
                return resolved == cwd if resolved is not None else session_cwd == str(cwd)
    except OSError:
        return False
    return False


def _codex_candidate_prompt_matches(path: Path, prompt: str, *, max_lines: int = 40) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(max_lines):
                line = handle.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row_type = row.get("type")
                payload = row.get("payload", {})
                if row_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                    for item in payload.get("content", []):
                        if item.get("type") == "input_text" and item.get("text") == prompt:
                            return True
                if row_type == "event_msg" and payload.get("type") == "user_message" and payload.get("message") == prompt:
                    return True
    except OSError:
        return False
    return False


def _unique(matches: list[str]) -> str | None:
    """A launch-bound id only when exactly one distinct candidate matched;
    zero or ambiguous (>1) matches fail closed to None."""
    distinct = sorted(set(matches))
    return distinct[0] if len(distinct) == 1 else None


def _codex_store_session_id(*, cwd: Path, prompt: str, started_at: float, latest: float) -> str | None:
    root = _codex_sessions_root()
    if not prompt or not root.exists():
        return None
    matches: list[str] = []
    for candidate in root.rglob("*.jsonl"):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime < started_at - _CORRELATION_WINDOW_SLACK_S or mtime > latest + _CORRELATION_WINDOW_SLACK_S:
            continue
        if not _codex_candidate_cwd_matches(candidate, cwd):
            continue
        if not _codex_candidate_prompt_matches(candidate, prompt):
            continue
        found = _SESSION_UUID_RE.search(candidate.name)
        if found:
            matches.append(found.group(0))
    return _unique(matches)


def _opencode_part_matches_prompt(data: str, prompt: str) -> bool:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("type") == "text" and payload.get("text") == prompt


def _opencode_store_session_id(*, cwd: Path, prompt: str, started_at: float, latest: float) -> str | None:
    database = _opencode_session_db()
    if not prompt or not database.exists():
        return None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error, ValueError):
        return None
    try:
        rows = connection.execute(
            "SELECT id FROM session WHERE directory = ? AND time_created BETWEEN ? AND ? "
            "ORDER BY time_created DESC",
            (
                str(cwd),
                int((started_at - _CORRELATION_WINDOW_SLACK_S) * 1000),
                int((latest + _CORRELATION_WINDOW_SLACK_S) * 1000),
            ),
        ).fetchall()
        matches: list[str] = []
        for (session_id,) in rows:
            part_rows = connection.execute(
                "SELECT data FROM part WHERE session_id = ? ORDER BY time_created, id",
                (session_id,),
            ).fetchall()
            if any(_opencode_part_matches_prompt(data, prompt) for (data,) in part_rows):
                matches.append(str(session_id))
    except (OSError, sqlite3.Error, ValueError):
        return None
    finally:
        connection.close()
    return _unique(matches)


def _qwen_candidate_session_id(path: Path, prompt: str, cwd: Path, started_at: float, latest: float) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "user" or row.get("cwd") != str(cwd):
                    continue
                timestamp = _parse_epoch(row.get("timestamp"))
                if timestamp is None or not (
                    started_at - _CORRELATION_WINDOW_SLACK_S <= timestamp <= latest + _CORRELATION_WINDOW_SLACK_S
                ):
                    continue
                parts = row.get("message", {}).get("parts", [])
                if not any(isinstance(part, dict) and part.get("text") == prompt for part in parts):
                    continue
                session_id = row.get("sessionId")
                if isinstance(session_id, str) and session_id and path.stem == session_id:
                    return session_id
    except OSError:
        return None
    return None


def _qwen_store_session_id(*, cwd: Path, prompt: str, started_at: float, latest: float) -> str | None:
    root = _qwen_chats_root(cwd)
    if not prompt or not root.exists():
        return None
    matches: list[str] = []
    for candidate in root.glob("*.jsonl"):
        session_id = _qwen_candidate_session_id(candidate, prompt, cwd, started_at, latest)
        if session_id:
            matches.append(session_id)
    return _unique(matches)


def _parse_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _correlate_store_session_id(
    harness_name: str | None, *, outfile: Path, cwd: Path, prompt: str, started_at: float, latest: float
) -> str | None:
    """Match one launch to its harness-store record (codex/opencode/qwen).

    codex is tried by its exact stdout id first, then by a unique store record;
    opencode and qwen are store-only. Every path requires an exact
    pointer + repo-cwd + start-time-window match and a *single* candidate.
    """
    # Compare resolved cwds throughout: a store may record a symlink-resolved
    # path (e.g. /private/var vs /var on macOS) that a raw string compare misses.
    cwd = _resolve_path(cwd) or cwd
    if harness_name == "codex":
        stdout_id = _codex_stdout_session_id(outfile)
        if stdout_id:
            return stdout_id
        return _codex_store_session_id(cwd=cwd, prompt=prompt, started_at=started_at, latest=latest)
    if harness_name == "opencode":
        return _opencode_store_session_id(cwd=cwd, prompt=prompt, started_at=started_at, latest=latest)
    if harness_name == "qwen":
        return _qwen_store_session_id(cwd=cwd, prompt=prompt, started_at=started_at, latest=latest)
    return None


def _capture_launch_session_id(
    *,
    harness_name: str | None,
    effective_override: str | None,
    launch_id: str,
    outfile: Path,
    prompt: str,
    cwd: Path,
    started_at: float,
) -> str | None:
    """Best-effort launch-bound id at launch time (may be None; re-tried later).

    claude/copilot bind the launch-set ``launch_id`` by construction. An
    override captures its own printed id from this launch's output (never
    synthesized). codex/opencode/qwen correlate to this launch's own store
    record. A store or output may not exist immediately after ``Popen``; a
    None here is re-correlated in ``finalize_steer`` from the completed turn.
    """
    if effective_override:
        return _override_stdout_session_id(outfile)
    if harness_name in ("claude", "copilot"):
        return launch_id
    return _correlate_store_session_id(
        harness_name, outfile=outfile, cwd=cwd, prompt=prompt, started_at=started_at, latest=time.time()
    )


def _recorrelate_session_id(
    *, harness_name: str | None, effective_override: str | None, outfile: Path, prompt: str, cwd: Path, started_at: float
) -> str | None:
    """Re-run launch-bound correlation after a turn has completed and quiesced.

    Used by ``finalize_steer`` when no id was bound at launch: the override's
    own printed id and the codex/opencode/qwen store records are all present
    once the turn has finished. claude/copilot ids are launch-set and need no
    re-correlation. Still never a bare newest-session query.
    """
    if effective_override:
        return _override_stdout_session_id(outfile)
    if harness_name in ("claude", "copilot"):
        return None
    return _correlate_store_session_id(
        harness_name, outfile=outfile, cwd=cwd, prompt=prompt, started_at=started_at, latest=time.time()
    )


def _await_launch_session_id(
    *,
    harness_name: str | None,
    effective_override: str | None,
    outfile: Path,
    prompt: str,
    cwd: Path,
    started_at: float,
    timeout: float = _STEER_CAPTURE_TIMEOUT_S,
    poll: float = _STEER_CAPTURE_POLL_S,
) -> str | None:
    """Bounded, launch-provenance-safe wait for this launch's own session id.

    Polls the same launch-bound correlation as ``_recorrelate_session_id`` (the
    override's own printed id, or the codex/opencode/qwen store record matched to
    this launch) against the still-live prior turn, so an immediately-requested
    steer can bind an id the harness emits shortly after launch rather than
    racing it. Returns None — never a synthesized or newest-session id — when no
    launch-owned id appears within ``timeout``. Stops early once the id is found
    or the captured output already shows a hard-stop marker that the caller will
    refuse, so a hard-prompt or no-id turn is not made to wait needlessly long.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        session_id = _recorrelate_session_id(
            harness_name=harness_name,
            effective_override=effective_override,
            outfile=outfile,
            prompt=prompt,
            cwd=cwd,
            started_at=started_at,
        )
        if session_id:
            return session_id
        if sessions.scan_hard_stop(sessions.read_output_tail(outfile))["present"]:
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


# --- Risk ratchet (target-design §4) ------------------------------------------


def apply_risk_ratchet(entry: dict[str, Any], current: dict[str, Any] | None, *, risk_flag: str | None) -> bool:
    """Raise a slice entry's (and, when given, the live current_slice's)
    `risk` to "elevated". `plan_risk` is never touched anywhere — it is the
    plan parser's immutable fact; the ratchet only ever raises the
    separate, mutable `risk` field, and a plan-elevated slice already
    satisfies "stays elevated" without any action here.

    Returns True iff `risk_flag` was supplied and valid, so the caller
    knows to log a `risk-raise` event. Raises `PmError` for any value other
    than "elevated" — the ratchet only ever raises, never lowers.
    """
    if risk_flag is None:
        return False
    if risk_flag != "elevated":
        raise PmError("risk can only be raised: pass --risk elevated (or omit --risk); it can never be lowered")
    entry["risk"] = "elevated"
    if current is not None:
        current["risk"] = "elevated"
    return True


# --- init ---------------------------------------------------------------------


@dataclass
class InitResult:
    run_id: str
    run_dir: Path
    token: str
    state: dict[str, Any]
    slices: list[plan_mod.PlanSlice]
    branch: str


def init_run(
    repo: Path,
    plan_path: Path,
    *,
    harness: str,
    model: str | None,
    effort: str | None,
    branch: str | None,
    create_branch: str | None,
    attest: str | None,
    max_attempts: int | None,
    reviewer_tools: str | None,
    reviewer_model: str | None,
    reviewer_effort: str | None,
    harness_command: str | None,
) -> InitResult:
    """Preflight, branch setup, and state/artifact creation for `init`.

    Callers are expected to have already run `plan.plan_check_report` and
    stopped on errors (this function assumes the plan is clean); it does
    not re-check the plan.
    """
    if harness_command is None and harness not in profiles.SUPPORTED_HARNESSES:
        supported = ", ".join(profiles.SUPPORTED_HARNESSES)
        raise PmError(
            f"no PM harness profile is defined for {harness!r} and no --harness-command override was "
            f"given; supported harnesses: {supported}"
        )

    if harness_command:
        candidate_executable = shlex.split(harness_command)[0] if harness_command.strip() else ""
    else:
        candidate_executable = profiles.HARNESS_PROFILES[harness]["executable"]
    if not candidate_executable or not _executable_exists(candidate_executable):
        raise PmError(f"harness executable not found on PATH: {candidate_executable!r}")

    resolved_branch = _resolve_init_branch(repo, branch=branch, create_branch=create_branch)
    # Required in every case, including after a branch switch and on the
    # "use the current branch" path, which has no earlier clean check.
    git_ops.require_clean_worktree(repo)

    slices = plan_mod.parse_plan(plan_path)
    known_ids = {plan_slice.slice_id for plan_slice in slices}
    attested_ids: set[str] = set()
    if attest:
        attested_ids = {piece.strip() for piece in attest.split(",") if piece.strip()}
        unknown = attested_ids - known_ids
        if unknown:
            raise PmError(f"--attest names unknown slice id(s): {', '.join(sorted(unknown))}")

    entries = [
        {
            "id": plan_slice.slice_id,
            "title": plan_slice.title,
            "status": "attested" if plan_slice.slice_id in attested_ids else None,
            "risk": plan_slice.plan_risk,
            "plan_risk": plan_slice.plan_risk,
            "commit": None,
            "attempts": 0,
        }
        for plan_slice in slices
    ]

    harness_block = {"name": harness, "model": model, "effort": effort, "command_override": harness_command}
    reviewer_block = {
        "tools": list(profiles.parse_reviewer_tools(reviewer_tools)),
        "model": reviewer_model,
        "effort": reviewer_effort,
    }
    policy_block = {"max_attempts": max_attempts if max_attempts is not None else 3, "commit_required": True}

    state, token, run_dir = state_mod.create_run(
        repo,
        plan_path=plan_path,
        plan_sha256=plan_mod.plan_digest(plan_path),
        slice_count=len(slices),
        branch=resolved_branch,
        harness=harness_block,
        reviewer=reviewer_block,
        policy=policy_block,
        slices=entries,
    )

    write_pm_gitignore(repo)
    (run_artifact_dir(repo, state["run_id"]) / "slices").mkdir(parents=True, exist_ok=True)
    state_mod.append_event(
        run_dir, "init", note=f"harness={harness} branch={resolved_branch} slices={len(slices)}"
    )

    return InitResult(run_id=state["run_id"], run_dir=run_dir, token=token, state=state, slices=slices, branch=resolved_branch)


def _executable_exists(executable: str) -> bool:
    import shutil

    return shutil.which(executable) is not None


def _resolve_init_branch(repo: Path, *, branch: str | None, create_branch: str | None) -> str:
    if create_branch:
        git_ops.require_clean_worktree(repo)
        git_ops.git(repo, "checkout", "-b", create_branch)
        return create_branch
    if branch:
        git_ops.require_clean_worktree(repo)
        returncode, _stdout, _stderr = git_ops.git_result(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
        if returncode != 0:
            raise PmError(f"branch {branch!r} does not exist; create it first or pass --create-branch")
        git_ops.git(repo, "checkout", branch)
        return branch
    current = git_ops.current_branch(repo)
    if current is None:
        raise PmError(
            "current HEAD is detached or the repository is unborn; pass --branch <existing> or "
            "--create-branch <new> so PM has a named branch to operate on"
        )
    if current in _PROTECTED_DEFAULT_BRANCHES:
        raise PmError(
            f"refusing to run PM on the default branch {current!r} by implicit default: every slice "
            f"commit would land directly on it. Pass --create-branch <new> for a dedicated run branch, "
            f"or --branch {current} to operate on it deliberately."
        )
    return current


# --- status ---------------------------------------------------------------


@dataclass
class StatusResult:
    state: dict[str, Any]
    slices: list[plan_mod.PlanSlice] | None
    plan_error: str | None
    next_slice_id: str | None
    next_slice_eligible: bool | None
    next_slice_reasons: list[str]
    current_session_alive: bool | None


def status(repo: Path, run_dir: Path, token: str | None = None) -> StatusResult:
    # Opportunistically MAC-verified when the controller's token is
    # available: the PM agent acts on status output between mutating
    # commands, so a tampered state should surface here, not one command
    # later. Tokenless (human) reads stay unverified, as documented.
    state = state_mod.load_state(run_dir, token)
    plan_error: str | None = None
    slices: list[plan_mod.PlanSlice] | None = None
    try:
        slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    except OSError as exc:
        plan_error = str(exc)

    next_slice_id = None
    next_eligible: bool | None = None
    next_reasons: list[str] = []
    if slices is not None:
        approved_ids = frozenset((state.get("approvals") or {}).keys())
        next_plan_slice = plan_mod.next_slice(slices, state)
        if next_plan_slice is not None:
            next_slice_id = next_plan_slice.slice_id
            next_eligible, next_reasons = plan_mod.eligibility(next_plan_slice, approved_ids)

    current = state.get("current_slice")
    current_alive: bool | None = None
    if current and current.get("pid") and current.get("identity"):
        current_alive = sessions.headless_process_alive(int(current["pid"]), str(current["identity"]))

    return StatusResult(
        state=state,
        slices=slices,
        plan_error=plan_error,
        next_slice_id=next_slice_id,
        next_slice_eligible=next_eligible,
        next_slice_reasons=next_reasons,
        current_session_alive=current_alive,
    )


# --- approve ----------------------------------------------------------------


def approve(repo: Path, run_dir: Path, token: str, *, slice_id: str, reason: str) -> dict[str, Any]:
    state = load_writable_state(run_dir, token)
    slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    plan_slice = plan_mod.plan_slice_by_id(slices, slice_id)
    if plan_slice is None:
        raise PmError(f"{slice_id} was not found in the plan")
    if plan_slice.approval_needed is not True:
        raise PmError(
            f"{slice_id} is not approval-gated (its Risk Flags 'Approval needed before implementation:' "
            f"line is {plan_slice.approval_needed!r}, not an explicit 'yes'); an unclear or absent flag "
            "is a planning defect that approval cannot clear"
        )
    approvals = dict(state.get("approvals") or {})
    approvals[slice_id] = {"at": _utc_now_iso(), "reason": reason}
    state["approvals"] = approvals
    state_mod.save_state(run_dir, state, token)
    state_mod.append_event(run_dir, "approve", slice_id=slice_id, note=reason)
    return state


# --- start-slice --------------------------------------------------------------


@dataclass
class StartSliceOutcome:
    kind: str  # all_complete | blocked | plan_changed | attempts_exhausted | launched | relaunched
    slice_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    attempt: int | None = None
    # Named exactly as `current_slice` and `status` name them: `session` is
    # PM's own per-attempt label, `session_id` the harness's launch-bound
    # resume handle (None when none could be bound to this launch).
    session: str | None = None
    session_id: str | None = None
    pid: int | None = None
    reaped: list[str] = field(default_factory=list)
    message: str = ""
    notes_warning: str | None = None


def _rotate_prior_attempt(artifact_dir: Path, superseded_attempt: int) -> None:
    # The next turn starts with a truncated outfile, so the previous attempt's
    # observe cursor would misreport growth against it; drop it rather than
    # rotate it (it is a transient progress marker, not slice evidence).
    (artifact_dir / _OBSERVE_CURSOR_FILE).unlink(missing_ok=True)
    names = ("result.json", sessions.SESSION_OUTFILE)
    present = [name for name in names if (artifact_dir / name).exists()]
    if not present:
        return
    destination = artifact_dir / f"attempt-{superseded_attempt}"
    destination.mkdir(parents=True, exist_ok=True)
    for name in present:
        (artifact_dir / name).rename(destination / name)


def start_slice(
    repo: Path,
    run_dir: Path,
    token: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    reviewer_tools: str | None = None,
    harness_command: str | None = None,
    risk: str | None = None,
) -> StartSliceOutcome:
    state = load_writable_state(run_dir, token)
    run_id = state["run_id"]
    plan_path = Path(state["plan"]["path"])

    try:
        plan_mod.verify_plan_unchanged(state, plan_path)
    except PmError as exc:
        state["status"] = "needs-human"
        state["stop_reason"] = "plan file changed mid-run"
        state_mod.save_state(run_dir, state, token)
        state_mod.append_event(run_dir, "plan-changed", note=str(exc))
        return StartSliceOutcome(kind="plan_changed", message=str(exc))

    slices = plan_mod.parse_plan(plan_path)

    current = state.get("current_slice")
    relaunch = False
    plan_slice: plan_mod.PlanSlice | None = None
    if current and current.get("id"):
        entry = slice_entry(state, current["id"])
        if entry is not None and entry.get("status") not in ("accepted", "attested"):
            plan_slice = plan_mod.plan_slice_by_id(slices, current["id"])
            relaunch = plan_slice is not None

    if not relaunch:
        plan_slice = plan_mod.next_slice(slices, state)
        if plan_slice is None:
            # Design §3.4 (Finish): the run ends honestly with a final state
            # write and report regeneration — an all-attested run reaches
            # completion here rather than idling as "active" forever.
            if state.get("status") != "complete":
                state["status"] = "complete"
                state["stop_reason"] = None
                state_mod.save_state(run_dir, state, token)
                state_mod.append_event(run_dir, "complete", note="all slices accepted or attested")
                regenerate_report(repo, run_dir, state)
            return StartSliceOutcome(kind="all_complete")

    assert plan_slice is not None
    approved_ids = frozenset((state.get("approvals") or {}).keys())
    eligible, reasons = plan_mod.eligibility(plan_slice, approved_ids)
    if not eligible:
        return StartSliceOutcome(kind="blocked", slice_id=plan_slice.slice_id, reasons=reasons)

    current_branch = git_ops.current_branch(repo)
    if current_branch != state.get("branch"):
        raise PmError(
            f"current branch {current_branch!r} does not match the run's recorded branch "
            f"{state.get('branch')!r}; switch back before starting a slice"
        )
    if not relaunch:
        git_ops.require_clean_worktree(repo)

    entry = slice_entry(state, plan_slice.slice_id)
    if entry is None:
        raise PmError(f"{plan_slice.slice_id} is not present in the run's slice entries")

    if risk is not None:
        # `current` here (if any) belongs to whichever slice was previously
        # in flight, not necessarily this one; new_current is always built
        # fresh from entry["risk"] below, so mutating only the entry is
        # sufficient — nothing reads a stale `current["risk"]` afterward.
        apply_risk_ratchet(entry, None, risk_flag=risk)
        state_mod.append_event(
            run_dir, "risk-raise", slice_id=plan_slice.slice_id, note="operator-raised via start-slice --risk elevated"
        )

    policy = state.get("policy") or {}
    max_attempts = int(policy.get("max_attempts", 3))

    if relaunch:
        attempts = int(entry.get("attempts", 0)) + 1
        if attempts > max_attempts:
            # Exhaustion is a mandatory stop (design §11): terminate the
            # tracked Developer process group so nothing keeps working past
            # the budget; the slice stays current so finalize --stop can
            # record the full story.
            if current:
                _terminate_current(current)
            state["status"] = "needs-human"
            state["stop_reason"] = _BUDGET_EXHAUSTED_REASON
            state_mod.save_state(run_dir, state, token)
            state_mod.append_event(
                run_dir, "stop", slice_id=plan_slice.slice_id, note=_BUDGET_EXHAUSTED_REASON
            )
            return StartSliceOutcome(kind="attempts_exhausted", slice_id=plan_slice.slice_id)
    else:
        attempts = 0

    artifact_dir = slice_artifact_dir(repo, run_id, plan_slice.slice_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Terminate any process still tracked by the outgoing current_slice before
    # relaunching (a relaunch supersedes a dead-or-hung prior attempt). The
    # headless model has no global process sweep — the tracked pgid and the
    # sidecar are the only handles — so termination is by recorded identity,
    # not a prefix scan. Done BEFORE rotation so the outfile is no longer being
    # written when it is moved aside.
    reaped: list[str] = []
    if current and current.get("pid"):
        if _terminate_current(current):
            reaped.append(f"developer pid {current['pid']}")

    if relaunch:
        _rotate_prior_attempt(artifact_dir, attempts - 1)

    if relaunch:
        before_head = current.get("before_head") if current else None
    else:
        before_head = git_ops.git_head(repo)
        (artifact_dir / "status-before.txt").write_text(git_ops.git_status_text(repo), encoding="utf-8")

    # Controller-owned notes.md: the PM agent curates the ORIGINAL (under
    # run_dir) via the `notes` command (write_notes) — start-slice's job here
    # is only to create it if absent, mirror it into `.pm/` (PM_NOTES_PATH
    # points at the mirror, since the Developer reads .pm/, never the state
    # dir) on every launch, and tripwire-warn (non-fatal) when the original
    # has grown past the cap.
    original_notes = notes_original_path(run_dir)
    if not original_notes.exists():
        original_notes.parent.mkdir(parents=True, exist_ok=True)
        original_notes.write_text("", encoding="utf-8")
    notes_size = original_notes.stat().st_size
    notes_warning: str | None = None
    if notes_size > _NOTES_SIZE_CAP_BYTES:
        notes_warning = (
            f"notes.md is {notes_size} bytes, over the {_NOTES_SIZE_CAP_BYTES}-byte (512 KiB) cap; "
            "a runaway notes file silently degrades every later Developer prompt — curate it down"
        )
    slice_notes_path = notes_path(repo, run_id)
    slice_notes_path.parent.mkdir(parents=True, exist_ok=True)
    slice_notes_path.write_bytes(original_notes.read_bytes())

    result_path = artifact_dir / "result.json"
    prompt_text = prompts.render_developer_prompt(
        plan_slice,
        plan_path=plan_path,
        artifact_dir=artifact_dir,
        notes_path=slice_notes_path,
        result_path=result_path,
    )
    prompt_path = artifact_dir / "prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    env = _developer_env(
        artifact_dir=artifact_dir,
        plan_path=plan_path,
        slice_id=plan_slice.slice_id,
        notes_path=slice_notes_path,
        result_path=result_path,
    )

    harness_block = state.get("harness") or {}
    harness_name = harness_block.get("name")
    effective_override = harness_command or harness_block.get("command_override")
    launch_model = model or harness_block.get("model")
    launch_effort = effort or harness_block.get("effort")

    # A launch-set id used ONLY by claude/copilot (composed into the launch
    # command and bound by construction). An override prints and PM captures
    # its own id; codex/opencode/qwen are correlated to this launch's store
    # record — neither uses this uuid.
    launch_id = str(uuid.uuid4())
    pointer = prompts.render_launch_pointer(prompt_path)

    if effective_override:
        # The override runs with the one-line launch pointer as its final
        # argument; PM_DEVELOPER_RESUME_SESSION_ID is unset on launch (only a
        # resume sets it), matching the frozen override protocol.
        command = f"{effective_override} {shlex.quote(pointer)}"
    else:
        git_access_dir = None
        launch_session_id: str | None = None
        if harness_name == "codex" and bool(policy.get("commit_required", True)):
            git_access_dir = git_ops.worktree_git_dir(repo)
        if harness_name in ("claude", "copilot"):
            launch_session_id = launch_id
        if harness_name == "opencode" and launch_model:
            # Fail-closed inventory validation: a model absent from the harness
            # inventory raises here rather than letting the harness silently
            # fall back to a different model.
            profiles.query_model_identity(harness_name, launch_model)
        command = shlex.join(
            profiles.compose_headless_command(
                harness_name,
                pointer,
                mode="developer",
                repo=repo,
                model=launch_model,
                effort=launch_effort,
                session_id=launch_session_id,
                git_access_dir=git_access_dir,
            )
        )

    session_label = sessions.session_name(run_id, slice_number(plan_slice.slice_id), attempts)
    launch_cwd = str(repo)
    launch_started_at = time.time()
    launch = _launch_developer(command, repo, env, artifact_dir)
    # Everything from here to the authenticated state write is bookkeeping for
    # a process that is ALREADY running: if any of it fails, the launch must be
    # torn down rather than left untracked (see _abort_launch). The guard ends
    # at save_state — once state records the process, a later event-log failure
    # must not kill a legitimately tracked Developer.
    try:
        # Best-effort at launch; a store/output that is not yet written yields
        # None and is re-correlated from the completed turn in finalize_steer.
        session_id = _capture_launch_session_id(
            harness_name=harness_name,
            effective_override=effective_override,
            launch_id=launch_id,
            outfile=Path(launch["outfile"]),
            prompt=pointer,
            cwd=Path(launch_cwd),
            started_at=launch_started_at,
        )
        sessions.write_developer_sidecar(
            developer_sidecar_path(repo, run_id),
            pid=launch["pid"],
            pgid=launch["pgid"],
            identity=launch["identity"],
            run_id=run_id,
            slice_id=plan_slice.slice_id,
        )

        now = _utc_now_iso()
        new_current: dict[str, Any] = {
            "id": plan_slice.slice_id,
            "artifact_dir": str(artifact_dir),
            "session": session_label,
            "session_id": session_id,
            "pid": launch["pid"],
            "pgid": launch["pgid"],
            "identity": launch["identity"],
            "outfile": launch["outfile"],
            "command_override": effective_override,
            # Launch-correlation metadata for a safe delayed re-correlation at
            # finalize --steer (the harness store/output may be empty right
            # after Popen): this launch's exact pointer, cwd, and start time.
            "launch_pointer": pointer,
            "launch_cwd": launch_cwd,
            "launch_started_at": launch_started_at,
            "before_head": before_head,
            "started_at": (current.get("started_at") if relaunch and current and current.get("started_at") else now),
            "attempts": attempts,
            "risk": entry.get("risk", plan_slice.plan_risk),
            "plan_risk": plan_slice.plan_risk,
            "wake_at": None,
            "reviewer_pids": [],
        }
        launch_overrides: dict[str, Any] = {
            key: value for key, value in (("model", model), ("effort", effort)) if value
        }
        if reviewer_tools:
            # Recorded per slice (design §8); review._resolve_tool prefers it
            # over the run-level reviewer configuration.
            launch_overrides["reviewer_tools"] = list(profiles.parse_reviewer_tools(reviewer_tools))
        if launch_overrides:
            new_current["launch"] = launch_overrides

        state["current_slice"] = new_current
        entry["attempts"] = attempts
        # A successful launch reactivates a run that a human resumed after a
        # stop/needs-human pause; tampered state can never reach here (the MAC
        # check above fails closed before any launch).
        state["status"] = "active"
        state_mod.save_state(run_dir, state, token)
    except Exception:
        _abort_launch(launch)
        raise

    note = f"attempt {attempts}"
    if reaped:
        note += f"; reaped stale developer process(es): {', '.join(reaped)}"
    state_mod.append_event(
        run_dir,
        "relaunch" if relaunch else "launch",
        slice_id=plan_slice.slice_id,
        note=note,
        evidence=str(prompt_path),
    )

    return StartSliceOutcome(
        kind="relaunched" if relaunch else "launched",
        slice_id=plan_slice.slice_id,
        attempt=attempts,
        session=session_label,
        session_id=session_id,
        pid=launch["pid"],
        reaped=reaped,
        notes_warning=notes_warning,
    )


# --- observe ------------------------------------------------------------------


@dataclass
class ObserveOutcome:
    has_current_slice: bool
    running: bool = False
    output_changed: bool = False
    result_present: bool = False
    result_status: str | None = None
    hard_stop: dict[str, Any] = field(default_factory=lambda: {"present": False, "kinds": [], "markers": []})
    tail: str = ""
    slice_id: str | None = None
    elapsed_seconds: float = 0.0


def observe(repo: Path, run_dir: Path, *, wait: float | None = None, token: str | None = None) -> ObserveOutcome:
    # Same opportunistic verification as status(): see the comment there.
    state = state_mod.load_state(run_dir, token)
    current = state.get("current_slice")
    if not current or not current.get("outfile"):
        return ObserveOutcome(has_current_slice=False)

    outfile = Path(current["outfile"])
    artifact_dir = Path(current["artifact_dir"])
    pid = current.get("pid")
    identity = current.get("identity")
    result_path = artifact_dir / "result.json"
    # "Changed" means grown since the PREVIOUS observation, not since the top
    # of this call: comparing two reads within one call would make a no-wait
    # `observe` (PM's normal polling shape) always report "no change" and drop
    # the progress record from the event log.
    cursor_path = artifact_dir / _OBSERVE_CURSOR_FILE
    previous_size = _read_observe_cursor(cursor_path)

    def _alive() -> bool:
        return bool(pid and identity and sessions.headless_process_alive(int(pid), str(identity)))

    initial_running = _alive()
    result_existed_before = result_path.is_file()

    running = initial_running
    latest_tail = ""
    deadline = time.monotonic() + wait if wait else None
    wait_start = time.monotonic()
    # Wait exits early ONLY on a meaningful signal — the Developer process
    # dying, result.json appearing, or a hard-stop marker in the captured
    # session output — never on mere output growth (a streaming harness churns
    # its outfile constantly and would otherwise defeat the wait almost
    # immediately; target-design §12, Amended post-implementation).
    while deadline is not None and time.monotonic() < deadline:
        running = _alive()
        latest_tail = sessions.read_output_tail(outfile)
        if not running or result_path.is_file() or sessions.scan_hard_stop(latest_tail)["present"]:
            break
        time.sleep(_OBSERVE_POLL_SECONDS)
    else:
        # No wait, or the wait ran to full duration: refresh once so a
        # zero-wait observe still reports current liveness and output.
        running = _alive()
        latest_tail = sessions.read_output_tail(outfile)
    elapsed_seconds = time.monotonic() - wait_start

    current_size = _outfile_size(outfile)
    output_changed = current_size != previous_size
    if output_changed:
        _write_observe_cursor(cursor_path, current_size)

    result_present = result_path.is_file()
    result_status: str | None = None
    if result_present:
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                result_status = data.get("status")
        except (OSError, json.JSONDecodeError):
            result_status = None

    hard_stop = sessions.scan_hard_stop(latest_tail)
    tail_lines = latest_tail.splitlines()[-_OBSERVE_TAIL_LINES:]
    tail = "\n".join(tail_lines)

    liveness_changed = running != initial_running
    result_newly_appeared = result_present and not result_existed_before
    if output_changed or liveness_changed or result_newly_appeared:
        state_mod.append_event(
            run_dir,
            "observe",
            slice_id=current.get("id"),
            note=(
                f"output_changed={output_changed} liveness_changed={liveness_changed} "
                f"running={running} result_present={result_present} "
                f"elapsed={elapsed_seconds:.1f}s"
            ),
            evidence=str(outfile) if output_changed else None,
        )

    return ObserveOutcome(
        has_current_slice=True,
        running=running,
        output_changed=output_changed,
        result_present=result_present,
        result_status=result_status,
        hard_stop=hard_stop,
        tail=tail,
        slice_id=current.get("id"),
        elapsed_seconds=elapsed_seconds,
    )


# --- finalize -------------------------------------------------------------
#
# Bare `finalize` (this section's first function) keeps the Stage 3
# floor-and-collect behaviour. The three decision paths below —
# `finalize_accept` / `finalize_steer` / `finalize_stop` — are where
# acceptance first exists in this toolkit (target-design §3.3/§5): the
# floor is mechanical and non-waivable, but accept/steer/stop are PM's own
# recorded acts, never inferred from evidence alone.


@dataclass
class FinalizeOutcome:
    report: FloorReport
    artifact_dir: Path
    session_output_path: Path
    status_before_path: Path
    status_after_path: Path
    diff_path: Path
    result_path: Path
    slice_id: str


_ACCEPT_REASONING_MIN_CHARS = 40
_REQUIRED_ELEVATED_REVIEW_SKILLS = ("code-review", "drift-audit")


def _collect_finalize_evidence(repo: Path, state: dict[str, Any], current: dict[str, Any]) -> tuple[FloorReport, Path]:
    """Shared by bare `finalize` and every decision path: read the captured
    session output + write status-after + diff evidence under the slice's
    artifact dir, then evaluate the eight-fact floor. Never mutates or saves
    state.

    The Developer's captured stdout already lives at
    ``<artifact_dir>/session-output.txt`` (the launch outfile), so fact 8
    reads it directly rather than snapshotting a live session."""
    slice_id = current["id"]
    artifact_dir = Path(current["artifact_dir"])
    outfile = Path(current["outfile"]) if current.get("outfile") else artifact_dir / sessions.SESSION_OUTFILE
    session_output = sessions.read_output_tail(outfile)

    (artifact_dir / "status-after.txt").write_text(git_ops.git_status_text(repo), encoding="utf-8")

    diff_path = artifact_dir / "diff.patch"
    after_head = git_ops.git_head(repo)
    git_ops.write_git_diff(repo, current.get("before_head"), after_head, diff_path)

    slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    report = evaluate_floor(repo, state, slices, slice_id, artifact_dir=artifact_dir, session_output=session_output)
    return report, artifact_dir


def _persist_risk_ratchet(run_dir: Path, state: dict[str, Any], token: str) -> None:
    """Durably record a just-applied risk ratchet, immediately.

    A decision path applies the ratchet, appends a `risk-raise` event, and then
    does a lot of fallible work — evidence collection touches the filesystem,
    termination can raise, review artifacts are re-hashed from disk — before it
    would otherwise save state. Any exception in that stretch would leave the
    durable event log claiming an elevated slice while authenticated state still
    said `standard`, and a later acceptance would then skip the very elevated
    review requirement the event announced.

    Saving here rather than guarding each fallible step is what makes that
    impossible for *every* failure mode rather than the handful anyone thought
    to catch. It is safe precisely because it happens while the ratchet's risk
    fields are the only mutation on `state`: no decision has been made yet, so
    there is no half-written decision to persist. The later save rewrites the
    same fields.
    """
    state_mod.save_state(run_dir, state, token)


def finalize(repo: Path, run_dir: Path, token: str, *, risk: str | None = None) -> FinalizeOutcome:
    state = load_writable_state(run_dir, token)
    current = state.get("current_slice")
    if not current:
        raise PmError("no current slice to finalize")
    slice_id = current["id"]

    if risk is not None:
        entry = slice_entry(state, slice_id)
        if entry is None:
            raise PmError(f"{slice_id} is not present in the run's slice entries")
        if apply_risk_ratchet(entry, current, risk_flag=risk):
            state_mod.append_event(
                run_dir, "risk-raise", slice_id=slice_id, note="risk raised via bare finalize --risk elevated"
            )

    report, artifact_dir = _collect_finalize_evidence(repo, state, current)

    note = "8/8 passed" if report.passed else "failed: " + ", ".join(
        fact.name for fact in report.facts if not fact.passed
    )
    state_mod.append_event(run_dir, "floor", slice_id=slice_id, note=note, evidence=str(artifact_dir))
    # updated_at bump (and, when --risk was given, the ratchet) only — no
    # other semantic field changes in bare finalize.
    state_mod.save_state(run_dir, state, token)

    return FinalizeOutcome(
        report=report,
        artifact_dir=artifact_dir,
        session_output_path=artifact_dir / sessions.SESSION_OUTFILE,
        status_before_path=artifact_dir / "status-before.txt",
        status_after_path=artifact_dir / "status-after.txt",
        diff_path=artifact_dir / "diff.patch",
        result_path=artifact_dir / "result.json",
        slice_id=slice_id,
    )


# --- finalize decision paths: assessment rendering helpers --------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_review_fresh(review: dict[str, Any], head: str | None) -> bool:
    """A review is fresh for `head` iff it was recorded against exactly
    this HEAD and its artifact still exists with a matching sha256 (design
    §5: any tree change after a mandatory review invalidates it)."""
    if not isinstance(review, dict) or head is None or review.get("head") != head:
        return False
    artifact = review.get("artifact")
    if not artifact or not Path(artifact).is_file():
        return False
    return review.get("sha256") == _sha256_file(Path(artifact))


def _fresh_reviews_for_head(reviews: list[dict[str, Any]], head: str | None) -> dict[str, dict[str, Any]]:
    fresh: dict[str, dict[str, Any]] = {}
    for review in reviews:
        if _is_review_fresh(review, head):
            skill = review.get("skill")
            if skill:
                fresh[skill] = review
    return fresh


def _reviews_consulted_text(reviews: list[dict[str, Any]], head: str | None, effective_risk: str) -> str:
    if not reviews:
        return "PM assessment only (standard risk)" if effective_risk != "elevated" else "(no reviews recorded)"
    lines: list[str] = []
    for review in reviews:
        stale = "" if _is_review_fresh(review, head) else " [SUPERSEDED - stale for current HEAD]"
        lines.append(
            f"- {review.get('skill')}/{review.get('tool')} @ {review.get('head')} -> "
            f"{review.get('artifact')}{stale}"
        )
    return "\n".join(lines)


def _attempts_summary(run_dir: Path, slice_id: str, attempts: int) -> str:
    events = state_mod.read_events(run_dir)
    steer_events = [event for event in events if event.get("kind") == "steer" and event.get("slice") == slice_id]
    lines = [f"Attempts: {attempts}"]
    if steer_events:
        lines.append(f"Steer interventions: {len(steer_events)}")
        for event in steer_events:
            lines.append(f"  - {event.get('ts')}:")
            note_lines = str(event.get("note") or "").splitlines() or [""]
            for note_line in note_lines:
                lines.append(f"      {note_line}")
    return "\n".join(lines)


def _render_assessment(
    entry: dict[str, Any],
    report: FloorReport,
    *,
    reasoning: str,
    decision: str,
    head: str | None,
    reviews_text: str,
    attempts_summary: str,
) -> str:
    lines = [
        f"# Assessment: {entry.get('id')} - {entry.get('title')}",
        "",
        f"Decision: {decision}",
        f"Timestamp: {_utc_now_iso()}",
        f"Commit: {head}",
        "",
        "## Floor",
        "",
    ]
    for fact in report.facts:
        status = "PASS" if fact.passed else "FAIL"
        lines.append(f"{fact.number}. {fact.name}: {status} - {fact.detail}")
    risk = entry.get("risk")
    plan_risk = entry.get("plan_risk")
    source = "plan-declared" if risk == plan_risk else "PM-raised (ratchet)"
    lines += [
        "",
        "## Risk",
        f"Level: {risk} (source: {source}; plan_risk={plan_risk})",
        "",
        "## Reviews consulted",
        reviews_text,
        "",
        "## Attempts / interventions",
        attempts_summary,
        "",
        "## PM reasoning",
        reasoning,
        "",
    ]
    return "\n".join(lines)


# --- finalize --accept ---------------------------------------------------


@dataclass
class AcceptOutcome:
    kind: str  # accepted | floor_failed | reviews_stale | raced
    slice_id: str
    report: FloorReport | None = None
    assessment_path: Path | None = None
    message: str = ""


def finalize_accept(repo: Path, run_dir: Path, token: str, *, reasoning: str, risk: str | None = None) -> AcceptOutcome:
    """`finalize --accept "reasoning"` (target-design §3.3/§5/§8 item 3).

    The floor is re-run in full and is never waivable. On a passing floor,
    an elevated slice additionally requires both a drift-audit and a
    code-review entry recorded fresh against the current HEAD (design §5's
    review-freshness rule) before acceptance is recorded.

    Both of those gates necessarily run while the Developer is still live, so
    they describe a tree it could still be changing. Acceptance is therefore
    re-checked once the Developer is provably dead — the floor, HEAD, and (for
    an elevated slice) the mandatory reports' freshness — and it is that
    post-quiesce evidence that gets recorded: PM never records an ACCEPTED
    assessment against a repository a tracked process could still mutate.
    """
    stripped_reasoning = reasoning.strip()
    if len(stripped_reasoning) < _ACCEPT_REASONING_MIN_CHARS:
        raise PmError(
            f"--accept reasoning must be at least {_ACCEPT_REASONING_MIN_CHARS} characters after "
            "stripping whitespace; the assessment is the accountability record, not a rubber stamp"
        )

    state = load_writable_state(run_dir, token)
    _refuse_if_budget_exhausted(state)
    current = state.get("current_slice")
    if not current:
        raise PmError("no current slice to finalize")
    slice_id = current["id"]
    entry = slice_entry(state, slice_id)
    if entry is None:
        raise PmError(f"{slice_id} is not present in the run's slice entries")

    ratcheted = apply_risk_ratchet(entry, current, risk_flag=risk)
    if ratcheted:
        state_mod.append_event(run_dir, "risk-raise", slice_id=slice_id, note=stripped_reasoning.splitlines()[0][:120])
        _persist_risk_ratchet(run_dir, state, token)

    report, artifact_dir = _collect_finalize_evidence(repo, state, current)
    floor_note = "8/8 passed" if report.passed else "failed: " + ", ".join(
        fact.name for fact in report.facts if not fact.passed
    )
    state_mod.append_event(run_dir, "floor", slice_id=slice_id, note=floor_note, evidence=str(artifact_dir))

    if not report.passed:
        state_mod.save_state(run_dir, state, token)
        failed_names = ", ".join(fact.name for fact in report.facts if not fact.passed)
        return AcceptOutcome(
            kind="floor_failed", slice_id=slice_id, report=report,
            message=f"floor failed for {slice_id}: {failed_names}; nothing accepted",
        )

    effective_risk = entry.get("risk") or "standard"
    reviews = list(entry.get("reviews") or [])
    head = git_ops.git_head(repo)

    if effective_risk == "elevated":
        fresh = _fresh_reviews_for_head(reviews, head)
        missing = sorted(set(_REQUIRED_ELEVATED_REVIEW_SKILLS) - set(fresh.keys()))
        if missing:
            state_mod.save_state(run_dir, state, token)
            return AcceptOutcome(
                kind="reviews_stale", slice_id=slice_id, report=report,
                message=(
                    f"acceptance refused: missing or stale review(s) for {', '.join(missing)} "
                    f"against HEAD {head}; re-run review --skill <name> against the current HEAD"
                ),
            )

    # Terminate the Developer BEFORE the ACCEPTED assessment is written. A
    # termination failure raises and refuses the acceptance outright, and doing
    # it first means no assessment.md is ever left on disk announcing an
    # acceptance the state never recorded. Deliberately after the review-
    # freshness gate: a refused acceptance must leave the Developer alive so
    # the operator can still steer it.
    _terminate_current(current)

    # Everything gated above was measured while the Developer was still live,
    # so it describes a tree the Developer could still have been changing: a
    # harness that committed in that window would otherwise earn an ACCEPTED
    # assessment describing a tree that is no longer current, carrying a commit
    # that never faced the surface-authorization fact. Now that the process is
    # provably dead, take the evidence again. This second pair is the only one
    # measured against a repository no tracked process can still mutate, so it
    # is the authoritative one and it is what the assessment records.
    #
    # Re-running the whole floor rather than only re-reading HEAD is deliberate:
    # a Developer that *edited* without committing leaves HEAD identical but
    # trips the clean-worktree fact, and one that wrote a hard-stop marker on
    # its way out trips fact 8.
    #
    # This needs no new machinery (VISION principle 9): _collect_finalize_evidence
    # is idempotent and saves no state, and no floor fact reads either of the two
    # files it rewrites (status-after.txt, diff.patch), so its own writes cannot
    # flip a fact.
    #
    quiesced_report, quiesced_artifact_dir = _collect_finalize_evidence(repo, state, current)
    quiesced_head = git_ops.git_head(repo)

    reasons = []
    if quiesced_head != head:
        reasons.append(f"HEAD moved from {head} to {quiesced_head}")
    if not quiesced_report.passed:
        failed_names = ", ".join(fact.name for fact in quiesced_report.facts if not fact.passed)
        reasons.append(f"the floor no longer passes: {failed_names}")
    if effective_risk == "elevated":
        # The mandatory reviews are acceptance evidence too, and _is_review_fresh
        # re-hashes each report from disk. The pre-termination gate above ran
        # while the Developer was live, and a codex Developer is handed the
        # worktree git dir as a writable root — which is exactly where review
        # originals live. So a report rewritten on the way out would otherwise
        # sail through: it moves neither HEAD nor the worktree, and no floor
        # fact reads it. Without this check the ACCEPTED assessment could even
        # render its own mandatory review as "[SUPERSEDED - stale]".
        still_fresh = _fresh_reviews_for_head(reviews, quiesced_head)
        lost = sorted(set(_REQUIRED_ELEVATED_REVIEW_SKILLS) - set(still_fresh.keys()))
        if lost:
            reasons.append(f"required review(s) no longer fresh: {', '.join(lost)}")
    if reasons:
        detail = "; ".join(reasons)
        state_mod.append_event(
            run_dir, "floor", slice_id=slice_id,
            note=f"post-quiesce re-check refused acceptance: {detail}",
            evidence=str(quiesced_artifact_dir),
        )
        state_mod.save_state(run_dir, state, token)
        return AcceptOutcome(
            kind="raced", slice_id=slice_id, report=quiesced_report,
            message=(
                f"acceptance refused for {slice_id}: the Developer acted while the acceptance was "
                f"being decided ({detail}), so the evidence the decision rested on is stale and "
                "nothing was accepted. The Developer has been terminated; re-run `finalize` to see "
                "the current evidence, then accept or steer afresh."
            ),
        )
    # quiesced_head == head is guaranteed above, so `head` below is already the
    # post-quiesce value; only the report needs swapping for the authoritative one.
    report = quiesced_report

    reviews_text = _reviews_consulted_text(reviews, head, effective_risk)
    attempts_summary = _attempts_summary(run_dir, slice_id, current.get("attempts", entry.get("attempts", 0)))
    assessment_text = _render_assessment(
        entry, report, reasoning=stripped_reasoning, decision="ACCEPTED", head=head,
        reviews_text=reviews_text, attempts_summary=attempts_summary,
    )
    assessment_relative = f"slices/slice-{slice_number(slice_id):03d}/assessment.md"
    assessment_original = write_controller_artifact(repo, run_dir, state["run_id"], assessment_relative, assessment_text)

    first_line = stripped_reasoning.splitlines()[0][:120]
    entry["status"] = "accepted"
    entry["commit"] = head
    entry["decision"] = first_line
    entry["assessment"] = str(assessment_original)
    entry["summary"] = first_line

    state["current_slice"] = None

    # Accepting the final undecided slice finishes the run (design §3.4):
    # the state write below is the final one and the report regeneration is
    # the closing act.
    slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    run_complete = plan_mod.next_slice(slices, state) is None
    if run_complete:
        state["status"] = "complete"
        state["stop_reason"] = None

    state_mod.save_state(run_dir, state, token)
    state_mod.append_event(run_dir, "accept", slice_id=slice_id, note=first_line, evidence=str(assessment_original))
    if run_complete:
        state_mod.append_event(run_dir, "complete", note="all slices accepted or attested")
    regenerate_report(repo, run_dir, state)

    return AcceptOutcome(
        kind="accepted", slice_id=slice_id, report=report, assessment_path=assessment_original,
        message=f"{slice_id} accepted",
    )


# --- finalize --steer ------------------------------------------------------


@dataclass
class SteerOutcome:
    kind: str  # steered | budget_exhausted
    slice_id: str
    attempts: int | None = None
    message: str = ""


def finalize_steer(repo: Path, run_dir: Path, token: str, *, correction: str, risk: str | None = None) -> SteerOutcome:
    """`finalize --steer "correction"`: resume the slice as a new budgeted
    turn (target-design headless model).

    Steering is turn-based: the prior `-p`/`exec` turn has run to completion
    and exited, so a dead prior process is the *normal* precondition, not an
    error. The prior process is quiesced (confirmed dead, its identity-checked
    group reaped if it lingers), then a captured launch-bound session id is
    required (blocking with a clear error if none), then a detached resume
    turn is launched — counted against the same attempt budget as a relaunch.
    """
    state = load_writable_state(run_dir, token)
    _refuse_if_budget_exhausted(state)
    run_id = state["run_id"]
    current = state.get("current_slice")
    if not current:
        raise PmError("no current slice to steer")
    slice_id = current["id"]
    entry = slice_entry(state, slice_id)
    if entry is None:
        raise PmError(f"{slice_id} is not present in the run's slice entries")

    # Stripped copy used only to decide "is this blank" and to summarize the
    # risk-raise event's own note; the correction delivered to the resume turn
    # and recorded on the steer event below stays exactly as given — a
    # verbatim correction can legitimately start or end with meaningful
    # whitespace (e.g. an indented code block).
    stripped_correction = correction.strip()
    ratcheted = apply_risk_ratchet(entry, current, risk_flag=risk)
    if ratcheted:
        note = stripped_correction.splitlines()[0][:120] if stripped_correction else "risk raised via finalize --steer"
        state_mod.append_event(run_dir, "risk-raise", slice_id=slice_id, note=note)
        _persist_risk_ratchet(run_dir, state, token)

    # Increment FIRST, then decide: a candidate attempt count over budget
    # is never persisted, matching start_slice's relaunch-exhaustion path.
    attempts = int(current.get("attempts", 0)) + 1
    policy = state.get("policy") or {}
    max_attempts = int(policy.get("max_attempts", 3))
    if attempts > max_attempts:
        # Mandatory stop, as in start_slice's exhaustion path: the tracked
        # process group is terminated, not left running past the budget.
        _terminate_current(current)
        state["status"] = "needs-human"
        state["stop_reason"] = _BUDGET_EXHAUSTED_REASON
        state_mod.save_state(run_dir, state, token)
        state_mod.append_event(run_dir, "stop", slice_id=slice_id, note=_BUDGET_EXHAUSTED_REASON)
        return SteerOutcome(
            kind="budget_exhausted", slice_id=slice_id, message="attempt budget exhausted; steer refused"
        )

    harness_name = (state.get("harness") or {}).get("name")
    override = current.get("command_override")
    outfile = Path(current["outfile"]) if current.get("outfile") else None
    launch_pointer = current.get("launch_pointer") or ""
    launch_cwd = Path(current.get("launch_cwd") or repo)
    launch_started_at = float(current.get("launch_started_at") or 0.0)

    # The refusal gates below (quiesce, hard-stop, id-required) raise instead of
    # returning, so they bypass every state write. The risk ratchet applied
    # above is a durable one-way escalation whose `risk-raise` event is already
    # in the log, so a refusal here must still persist it — otherwise the event
    # log claims an elevation the state never recorded and a later accept would
    # fail open on standard-risk review requirements. Only the ratchet (and any
    # session id correlated below) is persisted: the attempt increment is not
    # applied to state until the resume actually launches.
    try:
        # Gather this launch's own session id BEFORE quiescing (evidence
        # gathering, distinct from *requiring* the id below). A harness —
        # including an override, which prints its exact id line then idles —
        # emits its id shortly after Popen, not synchronously with it, so an
        # immediately-requested steer must read the launch-owned evidence from
        # the still-live turn with a bounded, fail-closed wait. Quiescing first
        # could kill the process before it emits its id. This never synthesizes
        # an id and never accepts a newest session.
        session_id = current.get("session_id")
        if not session_id and outfile is not None:
            session_id = _await_launch_session_id(
                harness_name=harness_name,
                effective_override=override,
                outfile=outfile,
                prompt=launch_pointer,
                cwd=launch_cwd,
                started_at=launch_started_at,
            )
            if session_id:
                current["session_id"] = session_id

        # Quiesce the prior turn before requiring the id or launching the
        # resume: result.json appearing does not prove the harness process
        # exited, so a resume must never race a still-flushing or still-acting
        # prior turn. quiesce_headless raises if the tracked group will not die.
        pid = current.get("pid")
        pgid = current.get("pgid")
        identity = current.get("identity")
        if pid and pgid and identity:
            sessions.quiesce_headless(int(pid), int(pgid), str(identity))

        # Hard-stop refusal on corrections (pre-cutover rule preserved): once
        # the prior turn has quiesced, scan its captured output; a credential /
        # approval / usage-limit / external-side-effect marker means PM must not
        # blindly resume. Refuse BEFORE any attempt increment, rotation, or
        # steer event.
        if outfile is not None:
            hard_stop = sessions.scan_hard_stop(sessions.read_output_tail(outfile))
            if hard_stop["present"]:
                raise PmError(
                    "refusing to resume into a hard prompt visible in the captured session output: "
                    + ", ".join(hard_stop["kinds"])
                )

        # A launch-owned id that only finalized as the turn exited (e.g. a store
        # record flushed at completion) can still be bound now that it has
        # quiesced. Still the same launch-bound correlation — never a bare
        # newest session.
        if not session_id and outfile is not None:
            session_id = _recorrelate_session_id(
                harness_name=harness_name,
                effective_override=override,
                outfile=outfile,
                prompt=launch_pointer,
                cwd=launch_cwd,
                started_at=launch_started_at,
            )
            if session_id:
                current["session_id"] = session_id

        # Require a launch-bound session id. Without one, PM will not guess "the
        # last session" — it fails closed and the operator relaunches with
        # start-slice (a fresh session) instead.
        if not session_id:
            raise PmError(
                f"no launch-bound session id could be correlated for {slice_id}; cannot resume this "
                "harness headlessly — relaunch with start-slice"
            )
    except PmError:
        # The ratchet itself is already durable (persisted immediately after its
        # event above). This save is still load-bearing for a *different* field:
        # the gates above may have correlated and stored `session_id` on
        # `current`, and a refusal should not throw that away.
        if ratcheted:
            state_mod.save_state(run_dir, state, token)
        raise

    current["attempts"] = attempts
    entry["attempts"] = attempts

    # Rotate the prior attempt's completion signal + captured output into
    # attempt-<n>/ before the resume launch (quiesce → rotate → launch): a
    # steered attempt must never be mistaken for complete on the pre-steer
    # result.json (observe --wait breaks the instant one exists), and the
    # pre-steer output must be preserved before the resume truncates the
    # outfile.
    artifact_dir = Path(current["artifact_dir"])
    _rotate_prior_attempt(artifact_dir, attempts - 1)

    # Compose and launch the resume turn. The correction is framed by the
    # reference-sourced steer wrapper; the harness continues its prior session
    # (profile harnesses via the resume composer's flags; a custom override via
    # PM_DEVELOPER_RESUME_SESSION_ID).
    message = prompts.render_steer_message(correction)
    env = _developer_env(
        artifact_dir=artifact_dir,
        plan_path=Path(state["plan"]["path"]),
        slice_id=slice_id,
        notes_path=notes_path(repo, run_id),
        result_path=artifact_dir / "result.json",
    )
    if override:
        command = f"{override} {shlex.quote(message)}"
        env[RESUME_SESSION_ENV_VAR] = session_id
    else:
        git_access_dir = None
        if harness_name == "codex" and bool(policy.get("commit_required", True)):
            git_access_dir = git_ops.worktree_git_dir(repo)
        command = shlex.join(
            profiles.compose_resume_command(
                harness_name, message, session_id=session_id, repo=repo, git_access_dir=git_access_dir
            )
        )

    launch = _launch_developer(command, repo, env, artifact_dir)
    # As in start_slice: the resume turn is already running, so its bookkeeping
    # is guarded up to (and including) the state write — a failure here tears
    # the new turn down instead of orphaning it untracked.
    try:
        # The resume is a new budgeted turn: advance the per-attempt session
        # label while keeping session_id as the stable harness resume handle.
        current["session"] = sessions.session_name(run_id, slice_number(slice_id), attempts)
        current["pid"] = launch["pid"]
        current["pgid"] = launch["pgid"]
        current["identity"] = launch["identity"]
        current["outfile"] = launch["outfile"]
        sessions.write_developer_sidecar(
            developer_sidecar_path(repo, run_id),
            pid=launch["pid"],
            pgid=launch["pgid"],
            identity=launch["identity"],
            run_id=run_id,
            slice_id=slice_id,
        )
        state_mod.save_state(run_dir, state, token)
    except Exception:
        _abort_launch(launch)
        raise

    # The complete, verbatim correction lives in the event's note (no
    # truncation, no stripping, no evidence path) — it is the only durable
    # record of what was said, now that no steer file exists to point to.
    state_mod.append_event(run_dir, "steer", slice_id=slice_id, note=correction)

    return SteerOutcome(
        kind="steered", slice_id=slice_id, attempts=attempts,
        message=f"steered {slice_id} (attempt {attempts})",
    )


# --- finalize --stop --------------------------------------------------------


@dataclass
class StopDecisionOutcome:
    slice_id: str
    assessment_path: Path
    report: FloorReport


def finalize_stop(repo: Path, run_dir: Path, token: str, *, reason: str, risk: str | None = None) -> StopDecisionOutcome:
    """`finalize --stop "reason"`: records exactly what happened, floor
    passing or not — that is the point of a stop record."""
    state = load_writable_state(run_dir, token)
    current = state.get("current_slice")
    if not current:
        raise PmError("no current slice to stop")
    slice_id = current["id"]
    entry = slice_entry(state, slice_id)
    if entry is None:
        raise PmError(f"{slice_id} is not present in the run's slice entries")

    stripped_reason = reason.strip()
    ratcheted = apply_risk_ratchet(entry, current, risk_flag=risk)
    if ratcheted:
        note = stripped_reason.splitlines()[0][:120] if stripped_reason else "risk raised via finalize --stop"
        state_mod.append_event(run_dir, "risk-raise", slice_id=slice_id, note=note)
        _persist_risk_ratchet(run_dir, state, token)

    report, artifact_dir = _collect_finalize_evidence(repo, state, current)
    floor_note = "8/8 passed" if report.passed else "failed: " + ", ".join(
        fact.name for fact in report.facts if not fact.passed
    )
    state_mod.append_event(run_dir, "floor", slice_id=slice_id, note=floor_note, evidence=str(artifact_dir))

    # Terminate before publishing the STOPPED assessment, for the same reason
    # as finalize_accept: a termination failure raises, and an assessment
    # written first would be left on disk announcing a stop the state never
    # recorded. The floor evidence above is deliberately collected first — a
    # stop record exists to say what happened, floor passing or not, and that
    # event is the record of what the Developer left behind.
    _terminate_current(current)
    for pgid in list(current.get("reviewer_pids") or []):
        _kill_reviewer_pgid(pgid)

    # Re-collect once the Developer is provably dead so the assessment's floor
    # report and HEAD are one *consistent* pair. HEAD was always read after
    # termination, so pairing it with the pre-termination report could describe
    # a tree that never existed at any single instant. Unlike finalize_accept
    # this can never refuse: a stop records what happened, floor passing or not.
    report, artifact_dir = _collect_finalize_evidence(repo, state, current)
    quiesced_floor_note = "8/8 passed" if report.passed else "failed: " + ", ".join(
        fact.name for fact in report.facts if not fact.passed
    )
    if quiesced_floor_note != floor_note:
        state_mod.append_event(
            run_dir, "floor", slice_id=slice_id,
            note=f"post-quiesce re-read (recorded in the assessment): {quiesced_floor_note}",
            evidence=str(artifact_dir),
        )

    head = git_ops.git_head(repo)
    reviews = list(entry.get("reviews") or [])
    reviews_text = _reviews_consulted_text(reviews, head, entry.get("risk") or "standard")
    attempts_summary = _attempts_summary(run_dir, slice_id, current.get("attempts", entry.get("attempts", 0)))
    assessment_text = _render_assessment(
        entry, report, reasoning=stripped_reason, decision="STOPPED", head=head,
        reviews_text=reviews_text, attempts_summary=attempts_summary,
    )
    assessment_relative = f"slices/slice-{slice_number(slice_id):03d}/assessment.md"
    assessment_original = write_controller_artifact(repo, run_dir, state["run_id"], assessment_relative, assessment_text)

    first_line = stripped_reason.splitlines()[0][:120] if stripped_reason else ""
    entry["status"] = "stopped"
    entry["decision"] = first_line
    entry["assessment"] = str(assessment_original)
    entry["summary"] = first_line

    state["status"] = "needs-human"
    state["stop_reason"] = reason
    state["current_slice"] = None

    state_mod.save_state(run_dir, state, token)
    state_mod.append_event(run_dir, "slice-stop", slice_id=slice_id, note=first_line, evidence=str(assessment_original))
    regenerate_report(repo, run_dir, state)

    return StopDecisionOutcome(slice_id=slice_id, assessment_path=assessment_original, report=report)


# --- stop -----------------------------------------------------------------


def _kill_reviewer_pgid(pgid: int) -> None:
    """killpg, tolerating a process group that is already gone (ESRCH) or
    unreachable (EPERM) — a hung reviewer that stop reaps."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


@dataclass
class StopOutcome:
    run_id: str
    killed: list[str]


def stop(
    repo: Path,
    run_dir: Path,
    token: str,
    *,
    reason: str,
    slice_status: str | None = None,
) -> StopOutcome:
    state = load_writable_state(run_dir, token)
    run_id = state["run_id"]
    current = state.get("current_slice")

    # The Developer's captured stdout already persists at
    # <artifact_dir>/session-output.txt (the launch outfile), so no separate
    # snapshot is taken at stop time.

    # Reap any recorded reviewer process groups (a hung `review` subprocess)
    # — ESRCH/EPERM tolerated. Applies whenever state is readable, including
    # the --scavenge-with-readable-state path (cli.py already routes that
    # through this same function).
    if current and current.get("reviewer_pids"):
        for pgid in list(current["reviewer_pids"]):
            _kill_reviewer_pgid(pgid)
        current["reviewer_pids"] = []

    killed: list[str] = []
    if current and current.get("pid"):
        if _terminate_current(current):
            killed.append(f"developer pid {current['pid']}")

    if state.get("status") != "complete":
        state["status"] = "stopped"
    state["stop_reason"] = reason
    if slice_status and current and current.get("id"):
        entry = slice_entry(state, current["id"])
        if entry is not None:
            entry["status"] = slice_status

    state_mod.save_state(run_dir, state, token)
    state_mod.append_event(
        run_dir,
        "stop",
        slice_id=current.get("id") if current else None,
        note=reason,
        evidence=", ".join(killed) if killed else None,
    )
    regenerate_report(repo, run_dir, state)
    return StopOutcome(run_id=run_id, killed=killed)


def stop_scavenge_sweep(repo: Path, *, run_id: str | None) -> list[str]:
    """State-independent scavenge for `stop --scavenge` when run state cannot
    be trusted or read at all. Reads the per-run `developer.pid` sidecar (under
    `.pm/`), validates its recorded identity, and terminates the tracked
    process group by that identity — never a blind signal.

    A run id is required: the headless model keeps no global process list, so
    with neither readable state nor a run id (and thus no sidecar path) there
    is nothing to discover. If both the run state and the sidecar are gone,
    global discovery is impossible."""
    if not run_id:
        return []
    try:
        record = sessions.read_developer_sidecar(developer_sidecar_path(repo, run_id))
    except PmError:
        # A corrupt/unverifiable sidecar fails closed: PM never signals a
        # process group it cannot validate.
        return []
    if record is None or record.get("run_id") != run_id:
        return []
    # A corrupt/unreadable sidecar already failed closed above. A termination
    # failure (a live group that survives SIGKILL, or a reused leader that
    # still owns the group) is NOT swallowed: scavenge must not report success
    # when the tracked process could not be killed. A PID-reuse-safe False
    # (nothing of ours to signal) simply reports nothing terminated.
    if sessions.terminate_headless(int(record["pid"]), int(record["pgid"]), str(record["identity"])):
        return [f"developer pid {record['pid']}"]
    return []

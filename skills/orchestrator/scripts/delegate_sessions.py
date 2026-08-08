"""Per-vendor session discovery heuristics for tracked delegates.

This module binds harness-owned session records to a tracked launch using an
explicit launch-time id where the harness supports one, or exact prompt, cwd,
and start-time correlation where it does not. It also owns transcript activity
and extraction. ``delegate_jobs.py`` imports from here so vendor store details
stay separate from launcher and lifecycle logic.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any, Iterator

SESSION_ID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
CODEX_SESSION_ID_RE = re.compile(r"^session id:\s*([0-9a-f-]+)\s*$", re.MULTILINE)


def parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None


def read_tail_text(path: Path, *, max_bytes: int = 32768) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


def infer_tool_name(command: list[str]) -> str:
    return Path(command[0]).name if command else ""


def has_flag(command: list[str], flags: set[str]) -> bool:
    return any(arg in flags for arg in command)


def option_values(command: list[str], flags: set[str]) -> list[str]:
    values: list[str] = []
    idx = 0
    while idx < len(command):
        arg = command[idx]
        if arg in flags:
            if idx + 1 < len(command):
                values.append(command[idx + 1])
            idx += 2
            continue
        for flag in flags:
            prefix = f"{flag}="
            if arg.startswith(prefix):
                values.append(arg[len(prefix) :])
                break
        idx += 1
    return values


def codex_prompt_from_command(command: list[str]) -> str | None:
    """The prompt in a codex command composed by ``compose_delegate_command``.

    Deliberately parses only the two shapes that composer emits —
    ``["codex", "exec", <prompt>, ...]`` and its resume form — and returns None
    for anything else. Correlation only ever sees composed commands, and a
    resume launch short-circuits on the recorded parent session id long before
    the prompt is consulted, so a general codex-argv parser would be
    speculative machinery for callers that do not exist.
    """
    if len(command) < 3 or command[1] != "exec":
        return None
    if command[2] == "resume":
        return command[4] if len(command) >= 5 else None
    return command[2]


def prompt_from_command(command: list[str]) -> str | None:
    tool = infer_tool_name(command)
    if tool == "codex":
        return codex_prompt_from_command(command)
    if tool == "opencode" and len(command) >= 3 and command[1] == "run":
        return command[2]
    values = option_values(command, {"-p", "--print", "--prompt"})
    return values[-1] if values else None


def infer_project_dirs(command: list[str], tool: str) -> list[Path]:
    raw_values: list[str] = []
    if tool == "claude":
        raw_values.extend(option_values(command, {"--add-dir"}))
    elif tool == "codex":
        raw_values.extend(option_values(command, {"-C", "--cd", "--add-dir"}))
    elif tool == "copilot":
        raw_values.extend(option_values(command, {"--add-dir"}))
    elif tool == "opencode":
        raw_values.extend(option_values(command, {"--dir"}))

    project_dirs: list[Path] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        try:
            project_dir = Path(raw_value).expanduser().resolve()
        except OSError:
            continue
        key = str(project_dir)
        if key in seen:
            continue
        seen.add(key)
        project_dirs.append(project_dir)
    return project_dirs


def claude_project_root(project_dir: Path) -> Path:
    # Claude Code's own project-slug algorithm replaces every
    # non-alphanumeric character (not just the path separator) with "-", e.g.
    # "/Users/x/Documents/AI Tools/pm-test" -> "-Users-x-Documents-AI-Tools-
    # pm-test". Verified against a real session's recorded cwd; replacing
    # only os.sep silently missed any project path containing a space, dot,
    # or other separator and made session lookups fail for such paths.
    normalized = re.sub(r"[^A-Za-z0-9]", "-", str(project_dir))
    return Path.home() / ".claude" / "projects" / normalized


def codex_session_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def copilot_session_root() -> Path:
    return Path.home() / ".copilot" / "session-state"


def opencode_session_db() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def qwen_project_root(project_dir: Path) -> Path:
    normalized = re.sub(r"[^A-Za-z0-9]", "-", str(project_dir))
    return Path.home() / ".qwen" / "projects" / normalized


def session_id_from_text(text: str) -> str | None:
    match = SESSION_ID_RE.search(text)
    return match.group(0) if match else None


def session_id_from_path(path: Path) -> str | None:
    return session_id_from_text(path.name)


def codex_session_id_from_output(path: Path, *, max_bytes: int = 8192) -> str | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_bytes)
    except OSError:
        return None
    match = CODEX_SESSION_ID_RE.search(text)
    return match.group(1) if match else None


def configured_session_id(command: list[str]) -> str | None:
    values = option_values(command, {"--session-id"})
    return values[-1] if values and values[-1] else None


def jsonl_head_rows(path: Path, max_lines: int) -> Iterator[Any]:
    """The first `max_lines` decoded JSON values in a session transcript.

    Undecodable lines are skipped rather than fatal: a transcript being
    written concurrently can end in a partial line. An unreadable file yields
    nothing, so every caller degrades to "no match" instead of raising.

    Rows are yielded as decoded, without a mapping check, because that is what
    the hand-written loops this replaced each did. A line that is valid JSON but not
    an object therefore still reaches `row.get(...)` and raises, exactly as
    before. `pm_lib.state.read_events` guards that case; adding the same guard
    here would be a behaviour change, so it is left as a separate decision.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(max_lines):
                line = handle.readline()
                if not line:
                    return
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield row
    except OSError:
        return


def codex_candidate_prompt_matches(path: Path, prompt: str | None, *, max_lines: int = 40) -> bool:
    if not prompt:
        return False
    for row in jsonl_head_rows(path, max_lines):
        row_type = row.get("type")
        payload = row.get("payload", {})
        if row_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            for item in payload.get("content", []):
                if item.get("type") == "input_text" and item.get("text") == prompt:
                    return True
        if row_type == "event_msg" and payload.get("type") == "user_message" and payload.get("message") == prompt:
            return True
    return False


def codex_candidate_cwd_matches(path: Path, cwd: Path | None) -> bool:
    if cwd is None:
        return False
    for row in jsonl_head_rows(path, 3):
        if row.get("type") != "session_meta":
            continue
        session_cwd = row.get("payload", {}).get("cwd")
        if not isinstance(session_cwd, str):
            return False
        try:
            return Path(session_cwd).expanduser().resolve() == cwd
        except OSError:
            return session_cwd == str(cwd)
    return False


def codex_workdir_from_command(command: list[str]) -> Path | None:
    values = option_values(command, {"-C", "--cd"})
    if not values:
        return None
    try:
        return Path(values[-1]).expanduser().resolve()
    except OSError:
        return None


def entry_workdir(entry: dict[str, Any]) -> Path | None:
    raw_cwd = entry.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        return None
    try:
        return Path(raw_cwd).expanduser().resolve()
    except OSError:
        return None


def recorded_session_path(entry: dict[str, Any]) -> Path | None:
    """A session path already recorded on `entry` and still present on disk.

    Every per-harness resolver below starts here: once a session has been
    correlated, re-deriving it risks re-running discovery against a store
    that has since rotated.
    """
    existing = entry.get("session_path")
    if isinstance(existing, str) and existing:
        path = Path(existing)
        if path.exists():
            return path
    return None


def resolve_claude_session_path(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> Path | None:
    recorded = recorded_session_path(entry)
    if recorded is not None:
        return recorded

    command = entry.get("command", [])
    if not isinstance(command, list):
        return None

    # A Claude transcript is always addressable by id: a first launch is
    # composed with --session-id, and a continuation inherits the recorded
    # parent id. Without one there is nothing launch-bound left to correlate
    # against, so ownership fails closed to null rather than guessing.
    session_id = entry.get("session_id") or configured_session_id(command)
    if not isinstance(session_id, str) or not session_id:
        return None

    deadline = time.time() + max(wait_seconds, 0.0)
    while True:
        for project_dir in infer_project_dirs(command, "claude"):
            exact_path = claude_project_root(project_dir) / f"{session_id}.jsonl"
            if exact_path.exists():
                return exact_path
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def resolve_codex_session_path(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> Path | None:
    recorded = recorded_session_path(entry)
    if recorded is not None:
        return recorded

    command = entry.get("command", [])
    if not isinstance(command, list) or has_flag(command, {"--ephemeral"}):
        return None

    session_id = entry.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        outfile = Path(entry["outfile"])
        session_id = codex_session_id_from_output(outfile)

    session_root = codex_session_root()
    if session_id and session_root.exists():
        exact_matches = sorted(session_root.rglob(f"*{session_id}.jsonl"))
        if exact_matches:
            return exact_matches[-1]

    prompt = prompt_from_command(command)
    workdir = codex_workdir_from_command(command)
    started_at = parse_iso(entry.get("started_at"))
    if not prompt or workdir is None or started_at is None:
        return None
    deadline = time.time() + max(wait_seconds, 0.0)

    while True:
        matches: list[tuple[float, Path]] = []
        if session_root.exists():
            candidates: list[tuple[float, Path]] = []
            for candidate in session_root.rglob("*.jsonl"):
                try:
                    candidates.append((candidate.stat().st_mtime, candidate))
                except OSError:
                    continue
            for _, candidate in sorted(candidates, reverse=True):
                stat = candidate.stat()
                if stat.st_mtime < (started_at - 5):
                    continue
                if not codex_candidate_cwd_matches(candidate, workdir):
                    continue
                if not codex_candidate_prompt_matches(candidate, prompt):
                    continue
                matches.append((stat.st_mtime, candidate))

        if len(matches) == 1:
            return matches[0][1]
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def resolve_copilot_session_path(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> Path | None:
    recorded = recorded_session_path(entry)
    if recorded is not None:
        return recorded

    command = entry.get("command", [])
    if not isinstance(command, list):
        return None
    session_id = entry.get("session_id") or configured_session_id(command)
    if not isinstance(session_id, str) or not session_id:
        return None

    path = copilot_session_root() / session_id / "events.jsonl"
    deadline = time.time() + max(wait_seconds, 0.0)
    while True:
        if path.exists():
            return path
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _opencode_part_matches_prompt(data: str, prompt: str) -> bool:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("type") == "text" and payload.get("text") == prompt


def _opencode_matching_session_ids(entry: dict[str, Any], *, wait_seconds: float) -> list[str]:
    command = entry.get("command", [])
    if not isinstance(command, list):
        return []
    prompt = prompt_from_command(command)
    workdirs = infer_project_dirs(command, "opencode")
    started_at = parse_iso(entry.get("started_at"))
    database = opencode_session_db()
    if not prompt or len(workdirs) != 1 or started_at is None or not database.exists():
        return []

    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            candidates = connection.execute(
                "SELECT id FROM session WHERE directory = ? AND time_created BETWEEN ? AND ? "
                "ORDER BY time_created DESC",
                (
                    str(workdirs[0]),
                    int((started_at - 5) * 1000),
                    int((started_at + max(wait_seconds, 0.0) + 5) * 1000),
                ),
            ).fetchall()
            matches: list[str] = []
            for (session_id,) in candidates:
                part_rows = connection.execute(
                    "SELECT data FROM part WHERE session_id = ? ORDER BY time_created, id",
                    (session_id,),
                ).fetchall()
                if any(_opencode_part_matches_prompt(data, prompt) for (data,) in part_rows):
                    matches.append(str(session_id))
            return matches
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return []


def resolve_opencode_session_id(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> str | None:
    deadline = time.time() + max(wait_seconds, 0.0)
    while True:
        matches = _opencode_matching_session_ids(entry, wait_seconds=wait_seconds)
        if len(matches) == 1:
            return matches[0]
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def resolve_opencode_session_path(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> Path | None:
    """Return the OpenCode store only when it contains the captured session."""
    session_id = entry.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    database = opencode_session_db()
    deadline = time.time() + max(wait_seconds, 0.0)
    while True:
        if database.exists():
            try:
                connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
                try:
                    found = connection.execute("SELECT 1 FROM session WHERE id = ?", (session_id,)).fetchone()
                finally:
                    connection.close()
                if found is not None:
                    return database
            except (OSError, sqlite3.Error, ValueError):
                pass
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _qwen_candidate_session(
    path: Path,
    prompt: str,
    workdir: Path,
    started_at: float,
    latest_start: float,
) -> tuple[str, float] | None:
    for row in jsonl_head_rows(path, 40):
        if row.get("type") != "user" or row.get("cwd") != str(workdir):
            continue
        timestamp = parse_iso(row.get("timestamp"))
        if timestamp is None or not (started_at - 5 <= timestamp <= latest_start):
            continue
        parts = row.get("message", {}).get("parts", [])
        if not any(isinstance(part, dict) and part.get("text") == prompt for part in parts):
            continue
        session_id = row.get("sessionId")
        if isinstance(session_id, str) and session_id and path.stem == session_id:
            return session_id, timestamp
    return None


def resolve_qwen_session_path(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> Path | None:
    recorded = recorded_session_path(entry)
    if recorded is not None:
        return recorded

    command = entry.get("command", [])
    if not isinstance(command, list):
        return None
    prompt = prompt_from_command(command)
    workdir = entry_workdir(entry)
    started_at = parse_iso(entry.get("started_at"))
    if not prompt or workdir is None or started_at is None:
        return None

    chats_root = qwen_project_root(workdir) / "chats"
    deadline = time.time() + max(wait_seconds, 0.0)
    latest_start = started_at + max(wait_seconds, 0.0) + 5
    while True:
        matches: list[tuple[float, Path]] = []
        if chats_root.exists():
            for candidate in chats_root.glob("*.jsonl"):
                match = _qwen_candidate_session(candidate, prompt, workdir, started_at, latest_start)
                if match is not None:
                    matches.append((match[1], candidate))
        if len(matches) == 1:
            return matches[0][1]
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def summarize_session_row(row: dict[str, Any]) -> tuple[str, str | None]:
    row_type = row.get("type", "unknown")
    if row_type == "assistant":
        content = row.get("message", {}).get("content")
        if isinstance(content, list):
            for item in content:
                item_type = item.get("type")
                if item_type == "tool_use":
                    return ("assistant.tool_use", item.get("name"))
                if item_type == "text":
                    return ("assistant.text", "text")
                if item_type == "thinking":
                    return ("assistant.thinking", "thinking")
    if row_type == "user":
        content = row.get("message", {}).get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            if content[0].get("type") == "tool_result":
                return ("user.tool_result", None)
        return ("user", None)
    if row_type == "queue-operation":
        return ("queue-operation", row.get("operation"))
    return (row_type, None)


def summarize_codex_row(row: dict[str, Any]) -> tuple[str, str | None]:
    row_type = row.get("type", "unknown")
    payload = row.get("payload", {})
    if row_type == "response_item":
        payload_type = payload.get("type")
        if payload_type == "message":
            role = payload.get("role")
            if role == "assistant":
                return ("assistant.text", "text")
            if role == "user":
                return ("user", None)
            return (f"message.{role or 'unknown'}", None)
        if payload_type == "function_call":
            return ("assistant.function_call", payload.get("name"))
        if payload_type == "function_call_output":
            return ("tool.output", None)
        if payload_type == "reasoning":
            return ("assistant.reasoning", "reasoning")
        return (f"response_item.{payload_type or 'unknown'}", None)
    if row_type == "event_msg":
        event_type = payload.get("type")
        if event_type == "agent_message":
            return ("assistant.text", "text")
        return (f"event.{event_type or 'unknown'}", None)
    return (row_type, None)


def summarize_copilot_row(row: dict[str, Any]) -> tuple[str, str | None]:
    row_type = str(row.get("type", "unknown"))
    data = row.get("data", {})
    if row_type == "assistant.message":
        return (row_type, "text")
    if row_type.startswith("assistant."):
        return (row_type, None)
    if row_type.startswith("tool."):
        detail = data.get("toolName") if isinstance(data, dict) else None
        return (row_type, detail)
    return (row_type, None)


def summarize_qwen_row(row: dict[str, Any]) -> tuple[str, str | None]:
    row_type = str(row.get("type", "unknown"))
    if row_type != "assistant":
        return (row_type, None)

    message = row.get("message")
    parts = message.get("parts", []) if isinstance(message, dict) else []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                return ("assistant.function_call", function_call.get("name"))
            if isinstance(part.get("text"), str):
                return ("assistant.text", "text")
    return ("assistant", None)


def _session_activity_payload(
    path: Path,
    summarize_row: Callable[[dict[str, Any]], tuple[str, str | None]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared activity payload; the vendor difference is only the row summarizer.

    A row counts as assistant activity when its summarized type starts with
    "assistant" — for Claude rows that is exactly `type == "assistant"`
    (summarize_session_row returns "assistant"/"assistant.*" for those rows and
    nothing else), and for Codex it covers assistant messages, function calls,
    and reasoning.
    """
    stat = path.stat()
    now = time.time()
    tail_rows: list[dict[str, Any]] = []
    for line in read_tail_text(path).splitlines():
        try:
            tail_rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    last_row_with_timestamp = next((row for row in reversed(tail_rows) if row.get("timestamp")), None)
    last_assistant_row = next(
        (
            row
            for row in reversed(tail_rows)
            if row.get("timestamp") and summarize_row(row)[0].startswith("assistant")
        ),
        None,
    )

    payload: dict[str, Any] = {
        "activity_source": "session_transcript",
        "last_activity_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "last_activity_age_s": max(0, int(now - stat.st_mtime)),
        "session_path": str(path),
        "session_mtime_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_mtime_age_s": max(0, int(now - stat.st_mtime)),
        "session_size": stat.st_size,
    }
    if extra:
        payload.update(extra)

    if last_row_with_timestamp is not None:
        row_type, detail = summarize_row(last_row_with_timestamp)
        payload["last_event_at"] = last_row_with_timestamp.get("timestamp")
        payload["last_event_type"] = row_type
        if detail:
            payload["last_event_detail"] = detail
        event_ts = parse_iso(last_row_with_timestamp.get("timestamp"))
        if event_ts is not None:
            payload["last_event_age_s"] = max(0, int(now - event_ts))

    if last_assistant_row is not None:
        row_type, detail = summarize_row(last_assistant_row)
        payload["last_assistant_at"] = last_assistant_row.get("timestamp")
        payload["last_assistant_type"] = row_type
        if detail:
            payload["last_assistant_detail"] = detail
        assistant_ts = parse_iso(last_assistant_row.get("timestamp"))
        if assistant_ts is not None:
            payload["last_assistant_age_s"] = max(0, int(now - assistant_ts))

    return payload


def _opencode_session_activity(path: Path, session_id: str) -> dict[str, Any]:
    """Read activity for one captured OpenCode session from its SQLite store."""
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            session_row = connection.execute(
                "SELECT time_updated FROM session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                return {}
            part_rows = connection.execute(
                "SELECT p.time_updated, p.data, m.data "
                "FROM part AS p LEFT JOIN message AS m ON m.id = p.message_id "
                "WHERE p.session_id = ? ORDER BY p.time_updated DESC, p.id DESC LIMIT 100",
                (session_id,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return {}

    now = time.time()
    try:
        session_updated_at = float(session_row[0]) / 1000
        latest_part_at = float(part_rows[0][0]) / 1000 if part_rows else session_updated_at
    except (TypeError, ValueError):
        return {}
    updated_at = max(session_updated_at, latest_part_at)
    payload: dict[str, Any] = {
        "activity_source": "session_store",
        "last_activity_at": datetime.fromtimestamp(updated_at, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "last_activity_age_s": max(0, int(now - updated_at)),
        "session_path": str(path),
        "session_id": session_id,
    }

    latest_event: tuple[float, str, str | None] | None = None
    latest_assistant: tuple[float, str, str | None] | None = None
    for raw_timestamp, raw_part, raw_message in part_rows:
        try:
            part = json.loads(raw_part)
            message = json.loads(raw_message) if raw_message else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(part, dict) or not isinstance(message, dict):
            continue
        timestamp = float(raw_timestamp) / 1000
        part_type = str(part.get("type", "unknown"))
        role = message.get("role")
        event_type = f"{role}.{part_type}" if role else f"part.{part_type}"
        detail = part.get("tool") if part_type == "tool" else None
        if latest_event is None:
            latest_event = (timestamp, event_type, detail)
        if role == "assistant" and latest_assistant is None:
            latest_assistant = (timestamp, event_type, detail)
        if latest_event is not None and latest_assistant is not None:
            break

    for prefix, event in (("last_event", latest_event), ("last_assistant", latest_assistant)):
        if event is None:
            continue
        timestamp, event_type, detail = event
        payload[f"{prefix}_at"] = datetime.fromtimestamp(timestamp, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        payload[f"{prefix}_age_s"] = max(0, int(now - timestamp))
        payload[f"{prefix}_type"] = event_type
        if detail:
            payload[f"{prefix}_detail"] = detail
    return payload


def extract_claude_session_text(path: Path) -> str | None:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None

    best_plan: str | None = None
    for row in reversed(rows):
        if row.get("type") != "assistant":
            continue
        content = row.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "").strip()
                if text:
                    return text
            if item_type == "tool_use" and item.get("name") == "ExitPlanMode":
                plan = item.get("input", {}).get("plan", "").strip()
                if plan and best_plan is None:
                    best_plan = plan
    return best_plan


def extract_codex_session_text(path: Path) -> str | None:
    best_agent_message: str | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            rows: list[dict[str, Any]] = []
            for line in handle:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None

    for row in reversed(rows):
        row_type = row.get("type")
        payload = row.get("payload", {})
        if row_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            parts = [
                item.get("text", "").strip()
                for item in payload.get("content", [])
                if item.get("type") == "output_text" and item.get("text", "").strip()
            ]
            if parts:
                return "\n".join(parts).strip()
        if row_type == "event_msg" and payload.get("type") == "agent_message":
            message = str(payload.get("message", "")).strip()
            if message and best_agent_message is None:
                best_agent_message = message
    return best_agent_message


def resolve_session_path(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> Path | None:
    tool = entry.get("tool")
    if tool == "claude":
        return resolve_claude_session_path(entry, wait_seconds=wait_seconds)
    if tool == "codex":
        return resolve_codex_session_path(entry, wait_seconds=wait_seconds)
    if tool == "copilot":
        return resolve_copilot_session_path(entry, wait_seconds=wait_seconds)
    if tool == "opencode":
        return resolve_opencode_session_path(entry, wait_seconds=wait_seconds)
    if tool == "qwen":
        return resolve_qwen_session_path(entry, wait_seconds=wait_seconds)
    return None


def resolve_launch_session(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> tuple[str | None, Path | None]:
    """Return only session metadata whose ownership is bound to ``entry``."""
    command = entry.get("command", [])
    if not isinstance(command, list):
        return None, None

    # A validated continuation inherits the exact launch-bound parent session
    # identity. Preserve it rather than trying to correlate the resumed prompt
    # as though it were a new vendor session.
    parent_session_id = entry.get("parent_session_id")
    if isinstance(parent_session_id, str) and parent_session_id:
        return parent_session_id, resolve_session_path(entry, wait_seconds=wait_seconds)

    tool = entry.get("tool")
    if tool in {"claude", "copilot"}:
        session_id = entry.get("session_id") or configured_session_id(command)
        if not isinstance(session_id, str) or not session_id:
            return None, None
        session_path = resolve_session_path(entry, wait_seconds=wait_seconds)
        return session_id, session_path

    if tool == "codex":
        outfile = entry.get("outfile")
        session_id = None
        if isinstance(outfile, str) and outfile:
            session_id = codex_session_id_from_output(Path(outfile))
        session_path = resolve_codex_session_path(entry, wait_seconds=wait_seconds)
        if session_id is None and session_path is not None:
            session_id = session_id_from_path(session_path)
        return session_id, session_path

    if tool == "opencode":
        return resolve_opencode_session_id(entry, wait_seconds=wait_seconds), None

    if tool == "qwen":
        session_path = resolve_qwen_session_path(entry, wait_seconds=wait_seconds)
        return (session_id_from_path(session_path), session_path) if session_path is not None else (None, None)

    return None, None


def session_activity(tool: str, path: Path, *, session_id: str | None = None) -> dict[str, Any]:
    if tool == "claude":
        return _session_activity_payload(path, summarize_session_row)
    if tool == "codex":
        return _session_activity_payload(path, summarize_codex_row, extra={"session_id": session_id_from_path(path)})
    if tool == "copilot":
        return _session_activity_payload(
            path,
            summarize_copilot_row,
            extra={"session_id": session_id_from_path(path.parent)},
        )
    if tool == "opencode" and session_id:
        return _opencode_session_activity(path, session_id)
    if tool == "qwen":
        return _session_activity_payload(path, summarize_qwen_row, extra={"session_id": session_id_from_path(path)})
    return {}


def extract_session_text(tool: str, path: Path) -> str | None:
    if tool == "claude":
        return extract_claude_session_text(path)
    if tool == "codex":
        return extract_codex_session_text(path)
    return None


def looks_like_codex_exec_transcript(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("OpenAI Codex v"):
        return True
    return "session id:" in text and "tokens used" in text

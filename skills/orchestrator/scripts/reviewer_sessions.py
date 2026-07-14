"""Per-vendor session-transcript heuristics for tracked reviewers.

Claude Code and Codex CLI write their own on-disk session transcripts
(~/.claude/projects/..., ~/.codex/sessions/...). This module owns everything
that locates a reviewer's session file from its recorded launch command, reads
lightweight activity signals from it, and extracts the final answer when the
outfile alone is not enough. reviewer_jobs.py imports from here; the split keeps
the launcher/lifecycle logic and the vendor transcript knowledge in separate
files. Project Manager reuses `claude_project_root` through reviewer_jobs.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

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
    if len(command) < 2 or command[1] != "exec":
        return None
    idx = 2

    exec_skip_value_flags = {
        "--enable",
        "--disable",
        "-c",
        "--config",
        "-i",
        "--image",
        "-m",
        "--model",
        "--local-provider",
        "-s",
        "--sandbox",
        "-p",
        "--profile",
        "-C",
        "--cd",
        "--add-dir",
        "--output-schema",
        "--color",
        "-o",
        "--output-last-message",
    }
    exec_standalone_flags = {
        "--oss",
        "--full-auto",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--ephemeral",
        "--progress-cursor",
        "--json",
    }

    while idx < len(command):
        arg = command[idx]
        if arg in exec_skip_value_flags:
            idx += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in exec_skip_value_flags):
            idx += 1
            continue
        if arg in exec_standalone_flags or any(arg.startswith(f"{flag}=") for flag in exec_standalone_flags):
            idx += 1
            continue
        if arg.startswith("-"):
            idx += 1
            continue
        if arg == "review":
            idx += 1
            break
        if arg in {"resume", "help"}:
            return None
        return arg

    review_skip_value_flags = {
        "--enable",
        "--disable",
        "-c",
        "--config",
        "--base",
        "--commit",
        "-m",
        "--model",
        "--title",
        "-o",
        "--output-last-message",
    }
    review_standalone_flags = {
        "--uncommitted",
        "--full-auto",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
    }

    while idx < len(command):
        arg = command[idx]
        if arg in review_skip_value_flags:
            idx += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in review_skip_value_flags):
            idx += 1
            continue
        if arg in review_standalone_flags or any(arg.startswith(f"{flag}=") for flag in review_standalone_flags):
            idx += 1
            continue
        if arg.startswith("-"):
            idx += 1
            continue
        return arg
    return None


def prompt_from_command(command: list[str]) -> str | None:
    tool = infer_tool_name(command)
    if tool == "codex":
        return codex_prompt_from_command(command)
    values = option_values(command, {"-p", "--print", "--prompt"})
    return values[-1] if values else None


def prompt_marker(prompt: str | None) -> str | None:
    if not prompt:
        return None
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("TASK:") or stripped.startswith("REVIEW THIS") or stripped.startswith("RESEARCH:"):
            return stripped[:200]
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("RETURN:") or stripped.startswith("FILES:"):
            return stripped[:200]
    collapsed = prompt.strip().replace("\n", " ")
    return collapsed[:200] if collapsed else None


def infer_project_dirs(command: list[str], tool: str) -> list[Path]:
    raw_values: list[str] = []
    if tool == "claude":
        raw_values.extend(option_values(command, {"--add-dir"}))
    elif tool == "codex":
        raw_values.extend(option_values(command, {"-C", "--cd", "--add-dir"}))
    elif tool == "copilot":
        raw_values.extend(option_values(command, {"--add-dir"}))

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


def file_head_contains(path: Path, marker: str | None, *, max_bytes: int = 131072) -> bool:
    if not marker:
        return True
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return marker in handle.read(max_bytes)
    except OSError:
        return False


def candidate_prompt_matches(path: Path, prompt: str | None, *, max_lines: int = 6) -> bool:
    if not prompt:
        return False
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
                if row.get("type") == "queue-operation" and row.get("content") == prompt:
                    return True
                if row.get("type") == "user":
                    message_content = row.get("message", {}).get("content")
                    if isinstance(message_content, str) and message_content == prompt:
                        return True
    except OSError:
        return False
    return False


def codex_candidate_prompt_matches(path: Path, prompt: str | None, *, max_lines: int = 40) -> bool:
    if not prompt:
        return False
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


def codex_candidate_cwd_matches(path: Path, cwd: Path | None) -> bool:
    if cwd is None:
        return False
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
                try:
                    return Path(session_cwd).expanduser().resolve() == cwd
                except OSError:
                    return session_cwd == str(cwd)
    except OSError:
        return False
    return False


def codex_workdir_from_command(command: list[str]) -> Path | None:
    values = option_values(command, {"-C", "--cd"})
    if not values:
        return None
    try:
        return Path(values[-1]).expanduser().resolve()
    except OSError:
        return None


def resolve_claude_session_path(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> Path | None:
    existing = entry.get("session_path")
    if isinstance(existing, str) and existing:
        path = Path(existing)
        if path.exists():
            return path

    command = entry.get("command", [])
    if not isinstance(command, list):
        return None

    prompt = prompt_from_command(command)
    marker = prompt_marker(prompt)
    started_at = parse_iso(entry.get("started_at"))
    deadline = time.time() + max(wait_seconds, 0.0)
    best_match: tuple[int, float, Path] | None = None

    while True:
        for project_dir in infer_project_dirs(command, "claude"):
            session_root = claude_project_root(project_dir)
            if not session_root.exists():
                continue
            candidates: list[tuple[float, Path]] = []
            for candidate in session_root.glob("*.jsonl"):
                try:
                    candidates.append((candidate.stat().st_mtime, candidate))
                except OSError:
                    continue
            for _, candidate in sorted(candidates, reverse=True):
                stat = candidate.stat()
                if started_at is not None and stat.st_mtime < (started_at - 300):
                    continue
                score = 0
                if started_at is not None and stat.st_mtime >= (started_at - 1):
                    score += 2
                if candidate_prompt_matches(candidate, prompt):
                    score += 10
                if file_head_contains(candidate, marker):
                    score += 4
                if best_match is None or (score, stat.st_mtime) > (best_match[0], best_match[1]):
                    best_match = (score, stat.st_mtime, candidate)

        threshold = 10 if prompt else 4
        if best_match and best_match[0] >= threshold:
            return best_match[2]
        if time.time() >= deadline:
            fallback_threshold = 6 if prompt else 2
            return best_match[2] if best_match and best_match[0] >= fallback_threshold else None
        time.sleep(0.5)


def resolve_codex_session_path(entry: dict[str, Any], *, wait_seconds: float = 0.0) -> Path | None:
    existing = entry.get("session_path")
    if isinstance(existing, str) and existing:
        path = Path(existing)
        if path.exists():
            return path

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
    deadline = time.time() + max(wait_seconds, 0.0)
    best_match: tuple[int, float, Path] | None = None

    while True:
        if session_root.exists():
            candidates: list[tuple[float, Path]] = []
            for candidate in session_root.rglob("*.jsonl"):
                try:
                    candidates.append((candidate.stat().st_mtime, candidate))
                except OSError:
                    continue
            for _, candidate in sorted(candidates, reverse=True):
                stat = candidate.stat()
                if started_at is not None and stat.st_mtime < (started_at - 300):
                    continue
                score = 0
                if started_at is not None and stat.st_mtime >= (started_at - 1):
                    score += 2
                if workdir is not None and codex_candidate_cwd_matches(candidate, workdir):
                    score += 6
                if codex_candidate_prompt_matches(candidate, prompt):
                    score += 10
                if best_match is None or (score, stat.st_mtime) > (best_match[0], best_match[1]):
                    best_match = (score, stat.st_mtime, candidate)

        threshold = 12 if prompt and workdir else 10 if prompt else 6 if workdir else 2
        if best_match and best_match[0] >= threshold:
            return best_match[2]
        if time.time() >= deadline:
            fallback_threshold = 8 if prompt and workdir else 6 if prompt else 4 if workdir else 2
            return best_match[2] if best_match and best_match[0] >= fallback_threshold else None
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
    return None


def session_activity(tool: str, path: Path) -> dict[str, Any]:
    if tool == "claude":
        return _session_activity_payload(path, summarize_session_row)
    if tool == "codex":
        return _session_activity_payload(path, summarize_codex_row, extra={"session_id": session_id_from_path(path)})
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

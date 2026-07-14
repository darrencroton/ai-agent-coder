"""Shared observation machinery for both PM execution paths.

The model-supervised command wrappers (observe/send/wait/pause-until in
commands.py) and the deterministic batch driver (execute_slice in runner.py)
consume the same observation snapshots and the same bounded observing wait.
This lives in its own module because commands.py imports runner.py: the batch
driver could not import the machinery from commands.py without a cycle.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git_ops import git_status_text, meaningful_status_lines
from .models import PmError
from .profiles import current_allow_unattended_default, parse_reviewer_tools, resolve_current_harness_command
from .runtime import extract_operational_hints, relative_artifact_path
from .state import append_operational_event, load_run, operational_events_file
from .tmux_adapter import TmuxHarnessAdapter
from .utils import parse_iso_datetime, utc_now


def _slice_artifact_dir(repo: Path, current: dict[str, Any]) -> Path:
    value = current.get("artifact_dir")
    if not value:
        raise PmError("current slice has no artifact_dir")
    path = Path(str(value))
    return path if path.is_absolute() else repo / path


def _current_adapter(args: argparse.Namespace, repo: Path, state: dict[str, Any]) -> TmuxHarnessAdapter:
    current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else {}
    session_id = current.get("developer_session_id") if isinstance(current, dict) else None
    return TmuxHarnessAdapter(
        state["harness"]["name"],
        resolve_current_harness_command(args, repo, state, str(session_id) if session_id else None),
        current_allow_unattended_default(args, state),
        parse_reviewer_tools(getattr(args, "reviewer_tools", None)),
        expected_model_display=str(state.get("harness", {}).get("model_identity", {}).get("display_name") or "") or None,
    )


def _result_status(result_path: Path) -> dict[str, Any]:
    if not result_path.exists():
        return {"exists": False, "parse_status": "absent", "path": str(result_path)}
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"exists": True, "parse_status": "invalid", "path": str(result_path), "error": str(exc)}
    return {
        "exists": True,
        "parse_status": "valid" if isinstance(data, dict) else "invalid",
        "path": str(result_path),
        "status": data.get("status") if isinstance(data, dict) else None,
        "slice_id": data.get("slice_id") if isinstance(data, dict) else None,
    }


def _read_tail(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - (limit * 4)))
        text = handle.read().decode("utf-8", errors="replace")
    return text[-limit:]


def _hard_stop_hint_kinds(snapshot: dict[str, Any]) -> list[str]:
    hints = snapshot.get("operational_hints")
    if not isinstance(hints, list):
        return []
    kinds: list[str] = []
    for hint in hints:
        if not isinstance(hint, dict) or not hint.get("hard_stop"):
            continue
        kind = str(hint.get("kind") or "unknown")
        subtype = hint.get("subtype")
        label = f"{kind}:{subtype}" if subtype else kind
        if label not in kinds:
            kinds.append(label)
    return kinds


def _raise_on_hard_stop_hints(snapshot: dict[str, Any], action: str) -> None:
    kinds = _hard_stop_hint_kinds(snapshot)
    if kinds:
        raise PmError(f"refusing to {action} while hard-stop operational hint is present: " + ", ".join(kinds))


def build_observation(args: argparse.Namespace, repo: Path, run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    now_text = utc_now()
    snapshot: dict[str, Any] = {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "repo_path": state.get("repo_path"),
        "branch": state.get("branch"),
        "current_time": {
            "utc": now_text,
            "local": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        },
        "harness": state.get("harness", {}),
        "current_slice": None,
        "process": {"running": False},
        "prompt_on_screen": {"present": False, "kinds": [], "markers": []},
        "operational_hints": [],
        "artifacts": {"run_dir": str(run_dir), "operational_events": str(operational_events_file(repo, state))},
        "result": {"exists": False, "parse_status": "no-current-slice"},
        "git": {"status_lines": meaningful_status_lines(git_status_text(repo))},
    }
    current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
    if not current:
        return snapshot

    artifact_dir = _slice_artifact_dir(repo, current)
    attempt = int(current.get("attempt") or 1)
    session_name = str(current.get("tmux_session") or "")
    started_at = str(current.get("started_at") or "")
    elapsed_seconds: int | None = None
    if started_at:
        try:
            elapsed_seconds = int((datetime.now(timezone.utc) - parse_iso_datetime(started_at)).total_seconds())
        except ValueError:
            elapsed_seconds = None

    adapter = _current_adapter(args, repo, state)
    previous_path = artifact_dir / "pane-capture-live-latest.txt"
    previous_capture = previous_path.read_text(encoding="utf-8") if previous_path.exists() else ""
    activity = adapter.detect_activity(session_name, previous_capture)
    capture = str(activity.get("capture") or "")
    live_capture_path = artifact_dir / f"pane-capture-live-attempt-{attempt}.txt"
    if capture:
        live_capture_path.parent.mkdir(parents=True, exist_ok=True)
        live_capture_path.write_text(capture, encoding="utf-8")
        previous_path.write_text(capture, encoding="utf-8")
    hard_prompt = adapter.detect_hard_prompt(capture)
    result_path = artifact_dir / "developer-result.json"
    transcript_path = artifact_dir / "developer-transcript.jsonl"
    result = _result_status(result_path)
    transcript_tail = _read_tail(transcript_path)
    hints = extract_operational_hints(
        capture,
        transcript_text=transcript_tail,
        process_running=bool(activity.get("running")),
        process_active=bool(activity.get("active")),
        result_exists=bool(result.get("exists")),
        max_single_pause_seconds=int(state.get("supervision", {}).get("max_single_pause_seconds", 21600)),
    )
    snapshot.update(
        {
            "current_slice": {
                "slice_id": current.get("slice_id"),
                "title": current.get("title"),
                "attempt": attempt,
                "started_at": started_at,
                "elapsed_seconds": elapsed_seconds,
                "before_head": current.get("before_head"),
                "tmux_session": session_name,
                "artifact_dir": relative_artifact_path(repo, artifact_dir),
                "pause": current.get("pause"),
            },
            "process": {"running": bool(activity.get("running")), "active": bool(activity.get("active"))},
            "prompt_on_screen": hard_prompt,
            "pane": {
                "capture_path": relative_artifact_path(repo, previous_path),
                "tail": capture[-4000:],
                "tail_truncated": len(capture) > 4000,
            },
            "transcript": {
                "path": relative_artifact_path(repo, transcript_path) if transcript_path.exists() else None,
                "tail": transcript_tail,
                "tail_truncated": transcript_path.exists() and len(transcript_tail) >= 4000,
            },
            "artifacts": {
                "run_dir": str(run_dir),
                "artifact_dir": relative_artifact_path(repo, artifact_dir),
                "pane_capture_latest": relative_artifact_path(repo, previous_path),
                "transcript_path": relative_artifact_path(repo, transcript_path) if transcript_path.exists() else None,
                "operational_events": str(operational_events_file(repo, state)),
                "observation_latest": relative_artifact_path(repo, artifact_dir / "observation-latest.json"),
            },
            "result": result,
            "operational_hints": hints,
        }
    )
    return snapshot


def record_observation(repo: Path, state: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
    event = append_operational_event(
        repo,
        state,
        {
            "kind": "observation",
            "status": "recorded",
            "slice_id": current.get("slice_id") if current else None,
            "attempt": current.get("attempt") if current else None,
            "evidence_path": snapshot.get("pane", {}).get("capture_path") if isinstance(snapshot.get("pane"), dict) else "",
            "process_running": snapshot.get("process", {}).get("running"),
            "result_exists": snapshot.get("result", {}).get("exists"),
            "hard_prompt": snapshot.get("prompt_on_screen", {}),
            "hard_stop_hints": _hard_stop_hint_kinds(snapshot),
            "operational_hint_kinds": [
                str(hint.get("kind"))
                for hint in snapshot.get("operational_hints", [])
                if isinstance(hint, dict) and hint.get("kind")
            ],
        },
    )
    snapshot["operational_event_id"] = event["event_id"]
    current_snapshot = snapshot.get("current_slice") if isinstance(snapshot.get("current_slice"), dict) else None
    if current_snapshot:
        artifact_dir = _slice_artifact_dir(repo, current_snapshot)
        (artifact_dir / "observation-latest.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return snapshot


def idle_stall_due(repo: Path, state: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Return true only after repeated idle windows span the configured ceiling."""
    current = snapshot.get("current_slice") if isinstance(snapshot.get("current_slice"), dict) else None
    if not current:
        return False
    event_path = operational_events_file(repo, state)
    if not event_path.is_file():
        return False
    observations: list[dict[str, Any]] = []
    for line in reversed(event_path.read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("slice_id") != current.get("slice_id") or event.get("attempt") != current.get("attempt"):
            continue
        if event.get("kind") in {"repair", "send"}:
            break
        if event.get("kind") != "observation":
            continue
        if "idle_no_progress" not in (event.get("operational_hint_kinds") or []):
            break
        observations.append(event)
    supervision = state.get("supervision", {})
    minimum = int(supervision.get("min_idle_observation_windows", 3))
    if len(observations) < minimum:
        return False
    try:
        newest = parse_iso_datetime(str(observations[0]["detected_at"]))
        oldest = parse_iso_datetime(str(observations[-1]["detected_at"]))
    except (KeyError, ValueError):
        return False
    return (newest - oldest).total_seconds() >= int(supervision.get("max_observe_staleness_seconds", 600))


def _observation_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    """Compact fingerprint of the decision-relevant observation state."""
    hard_prompt = snapshot.get("prompt_on_screen", {})
    return (
        bool(snapshot.get("process", {}).get("running")),
        bool(snapshot.get("result", {}).get("exists")),
        tuple(hard_prompt.get("kinds", []) if isinstance(hard_prompt, dict) else ()),
        tuple(_hard_stop_hint_kinds(snapshot)),
    )


# Floor between recorded observation events while nothing changes. Polling
# still happens at --poll-seconds for responsiveness; recording every poll
# flooded operational-events.jsonl (a 6-hour pause at a 2s cadence is ~10k
# near-identical "observation" events drowning the real signals).
_OBSERVATION_EVENT_FLOOR_SECONDS = 60.0


def wait_observing(
    args: argparse.Namespace,
    repo: Path,
    run_dir: Path,
    seconds: float,
    *,
    activity_log: Path | None = None,
    stop_on_hard_signals: bool = True,
    stop_on_idle_stall: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Observe the live slice until a decision-relevant condition or timeout.

    Both execution paths wait through this one loop; the keyword parameters
    are the per-driver policy:

    - activity_log: when set (the batch driver), append one compact
      {checked_at, running, active} line per poll — every poll, deliberately
      not subject to the observation-event floor, because the activity log is
      the batch path's per-poll audit trail and its first line must exist even
      when the result has already landed before the first check.
    - stop_on_hard_signals: when False (the batch driver), a visible hard
      prompt or hard-stop hint does not interrupt the wait — the deterministic
      policy has no judgment to apply to one, detection markers are broad
      substring matches that routinely occur in harness output, and the
      unconditional safety boundary is send_literal's refusal to type into a
      session showing a hard prompt. The signals are still observed and
      recorded either way.
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    final_snapshot: dict[str, Any] = {}
    reason = "timeout"
    last_recorded_signature: tuple[Any, ...] | None = None
    last_recorded_at = float("-inf")
    while True:
        state = load_run(run_dir)
        final_snapshot = build_observation(args, repo, run_dir, state)
        if activity_log is not None:
            with activity_log.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "checked_at": utc_now(),
                            "running": bool(final_snapshot.get("process", {}).get("running")),
                            "active": bool(final_snapshot.get("process", {}).get("active")),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        signature = _observation_signature(final_snapshot)
        hard_prompt = final_snapshot.get("prompt_on_screen", {})
        hard_prompt_present = isinstance(hard_prompt, dict) and hard_prompt.get("present")
        hard_stop_kinds = _hard_stop_hint_kinds(final_snapshot)
        breaking = (
            final_snapshot.get("result", {}).get("exists")
            or not final_snapshot.get("process", {}).get("running")
            or (stop_on_hard_signals and (bool(hard_prompt_present) or bool(hard_stop_kinds)))
            or time.monotonic() >= deadline
        )
        # Record on change, on the cadence floor, and always on the final
        # snapshot so the wait's evidence pointer refers to a recorded event.
        if breaking or signature != last_recorded_signature or time.monotonic() - last_recorded_at >= _OBSERVATION_EVENT_FLOOR_SECONDS:
            final_snapshot = record_observation(repo, state, final_snapshot)
            last_recorded_signature = signature
            last_recorded_at = time.monotonic()
        if final_snapshot.get("result", {}).get("exists"):
            reason = "result-ready"
            break
        if stop_on_idle_stall and final_snapshot.get("operational_event_id") and idle_stall_due(repo, state, final_snapshot):
            reason = "idle-stall"
            break
        if not final_snapshot.get("process", {}).get("running"):
            reason = "process-exited"
            break
        if stop_on_hard_signals and hard_prompt_present:
            reason = "hard-prompt"
            break
        if stop_on_hard_signals and hard_stop_kinds:
            reason = "hard-stop-hint"
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(min(float(args.poll_seconds), max(0.0, deadline - time.monotonic())))
    return reason, final_snapshot

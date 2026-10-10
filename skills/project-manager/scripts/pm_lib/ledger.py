"""Ledger rows: how each Developer and Reviewer performed, per slice window.

`run_rows` turns one run's signed state and event log into rows, one per
slice window, and is the ledger's whole semantics. It is pure: no git, no
plan file, no clock. Everything it reports is already recorded elsewhere
(judgments and reviews on the signed slice entries, identities, causes and
timestamps on events), so a row is a projection, never a second copy.

`write_run_file` regenerates the per-run ledger file,
``<ledger_dir>/<host>/<run_id>.json``, from MAC-verified state under the
run's existing lock. `cli.main` calls it after every token-bearing command;
the file is derived output and is never edited by hand. `leaderboard` reads
these files back into one markdown leaderboard.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from . import judgments
from . import state as state_mod

_LEDGER_DIR_ENV_VAR = "PM_LEDGER_DIR"

# Events that open a Developer submission.
_ORIGIN_KINDS = {"launch", "relaunch", "steer"}
# Events, for the same slice, that end the Developer's working time on a
# submission: the floor check, the next submission, a stop, or a decision.
_DEVELOPER_END_KINDS = {
    "floor",
    "launch",
    "relaunch",
    "steer",
    "stop",
    "slice-stop",
    "accept",
}


def ledger_dir() -> Path:
    """`PM_LEDGER_DIR` when set and non-blank, else the skill's own `ledger/`.

    The default is resolved from this file's real path, so every harness's
    symlinked copy of the skill reaches the same directory.
    """
    override = os.environ.get(_LEDGER_DIR_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "ledger"


def host_name() -> str:
    """Short, lowercased host name: the per-host folder under `ledger_dir()`.

    `unknown-host` when the name's first label is empty (a host name of ""
    or ".local"), so the file always lands one folder below `ledger_dir()`.
    """
    return socket.gethostname().split(".")[0].lower() or "unknown-host"


def _seconds_between(start_raw: Any, end_raw: Any) -> int | None:
    start = state_mod.parse_event_ts(start_raw)
    end = state_mod.parse_event_ts(end_raw)
    if start is None or end is None:
        return None
    return int(round((end - start).total_seconds()))


def _data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _identity(event: dict[str, Any]) -> dict[str, Any] | None:
    developer = _data(event).get("developer")
    if not isinstance(developer, dict):
        return None
    return {
        "tool": developer.get("tool"),
        "model": developer.get("model"),
        "effort": developer.get("effort"),
        "model_tag": developer.get("model_tag"),
    }


def _developer_judgment(entry: dict[str, Any], origin: int) -> dict[str, Any] | None:
    record = judgments.active_developer_judgment(entry, origin)
    if record is None:
        return None
    if record.get("status") == "unavailable":
        return {"status": "unavailable"}
    if "score" in record:
        return {
            "score": record.get("score"),
            "criteria_met": record.get("criteria_met"),
            "defects": record.get("defects"),
        }
    return None


def _developer_seconds(events: list[dict[str, Any]], slice_id: str, origin: int) -> int | None:
    for event in events[origin + 1 :]:
        kind, owner = event.get("kind"), event.get("slice")
        own_end = owner == slice_id and kind in _DEVELOPER_END_KINDS
        run_wide_stop = kind == "stop" and owner is None
        if own_end or run_wide_stop:
            return _seconds_between(events[origin].get("ts"), event.get("ts"))
    return None


def _submissions(
    entry: dict[str, Any],
    events: list[dict[str, Any]],
    slice_id: str,
    window: list[int],
) -> list[dict[str, Any]]:
    submissions: list[dict[str, Any]] = []
    developer: dict[str, Any] | None = None
    for index in window:
        kind = events[index].get("kind")
        if kind in {"launch", "relaunch"}:
            developer = _identity(events[index])
        if kind not in _ORIGIN_KINDS:
            continue
        submissions.append(
            {
                "origin": index,
                "kind": kind,
                "developer": developer,
                "judgment": _developer_judgment(entry, index),
                "developer_s": _developer_seconds(events, slice_id, index),
            }
        )
    return submissions


def _rating(active: list[dict[str, Any]], skill: Any, review_id: Any) -> dict[str, Any] | None:
    for record in active:
        if record.get("assessment") != "rating" or record.get("skill") != skill:
            continue
        if review_id not in judgments.referenced_ids(record):
            continue
        if record.get("status") == "unavailable":
            return {"status": "unavailable"}
        return {"score": record.get("score")}
    return None


def _review_origin(review: dict[str, Any]) -> int | None:
    origin = review.get("origin_event")
    index = origin.get("index") if isinstance(origin, dict) else None
    return index if type(index) is int else None


def _reviews(
    entry: dict[str, Any],
    active: list[dict[str, Any]],
    events: list[dict[str, Any]],
    window: list[int],
) -> tuple[list[dict[str, Any]], dict[tuple[Any, Any], int]]:
    """The window's review entries, and its recorded reviews keyed by (skill, id).

    A review belongs to the window whose own events include its origin, so a
    review can never be credited to another slice's or another window's row.
    """
    members = set(window)
    rows: list[dict[str, Any]] = []
    recorded: dict[tuple[Any, Any], int] = {}
    for review in entry.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        origin = _review_origin(review)
        if origin not in members:
            continue
        skill, review_id = review.get("skill"), review.get("review_id")
        recorded[(skill, review_id)] = origin
        rows.append(
            {
                "review_id": review_id,
                "skill": skill,
                "tool": review.get("tool"),
                "model": review.get("model"),
                "effort": review.get("effort"),
                "command_override": review.get("command_override"),
                "model_tag": review.get("model_tag"),
                "origin": origin,
                "rating": _rating(active, skill, review_id),
            }
        )
    for index in window:
        if events[index].get("kind") != "review-failed":
            continue
        data = _data(events[index])
        rows.append(
            {
                "review_id": None,
                "failed": True,
                "skill": data.get("skill"),
                "tool": data.get("tool"),
                "model": data.get("model"),
                "effort": data.get("effort"),
                "command_override": data.get("command_override"),
                "model_tag": data.get("model_tag"),
                "origin": data.get("origin_event_index"),
                "reason": data.get("reason"),
            }
        )
    return rows, recorded


def _comparisons(active: list[dict[str, Any]], recorded: dict[tuple[Any, Any], int]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for record in active:
        if record.get("assessment") != "comparison":
            continue
        skill = record.get("skill")
        if record.get("status") == "unavailable":
            ids = [review_id for review_id in record.get("review_ids") or [] if (skill, review_id) in recorded]
            if ids:
                comparisons.append({"status": "unavailable", "review_ids": ids})
            continue
        order = record.get("order") or []
        if order and all((skill, review_id) in recorded for review_id in order):
            comparisons.append(
                {
                    "origin": recorded[(skill, order[0])],
                    "order": order,
                    "close": record.get("close") or [],
                }
            )
    return comparisons


def _outcome(events: list[dict[str, Any]], window: list[int]) -> tuple[str, str | None, str | None]:
    """`(outcome, cause, decided_at)` by precedence accepted, exhausted, stopped, open.

    A window ended by `pm stop --slice-status stopped` has no `slice-stop`:
    it is stopped, with no cause, at the last `stop` event carrying that
    status, unless the slice was relaunched after it (the marker is then void).
    """

    def first(predicate) -> dict[str, Any] | None:
        return next((events[index] for index in window if predicate(events[index])), None)

    accept = first(lambda event: event.get("kind") == "accept")
    if accept is not None:
        return "accepted", None, accept.get("ts")
    budget_stop = first(lambda event: event.get("kind") == "stop" and _data(event).get("cause") == "developer")
    if budget_stop is not None:
        return "exhausted", "developer", budget_stop.get("ts")
    slice_stop = first(lambda event: event.get("kind") == "slice-stop")
    if slice_stop is not None:
        return "stopped", _data(slice_stop).get("cause"), slice_stop.get("ts")
    marked = max(
        (
            index
            for index in window
            if events[index].get("kind") == "stop" and _data(events[index]).get("slice_status") == "stopped"
        ),
        default=None,
    )
    if marked is not None and not any(events[index].get("kind") == "relaunch" for index in window if index > marked):
        return "stopped", None, events[marked].get("ts")
    return "open", None, None


def _slice_rows(state: dict[str, Any], entry: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slice_id = entry.get("id")
    own = [index for index, event in enumerate(events) if event.get("slice") == slice_id]
    opens = [index for index in own if events[index].get("kind") == "launch"]
    active = judgments.active_judgments(entry)
    rows: list[dict[str, Any]] = []
    for position, window_open in enumerate(opens):
        window_end = opens[position + 1] if position + 1 < len(opens) else len(events)
        window = [index for index in own if window_open <= index < window_end]
        outcome, cause, decided_at = _outcome(events, window)
        counts = {kind: 0 for kind in ("steer", "relaunch", "send")}
        for index in window:
            kind = events[index].get("kind")
            if kind in counts:
                counts[kind] += 1
        reviews, recorded = _reviews(entry, active, events, window)
        rows.append(
            {
                "repo_name": state.get("repo_name"),
                "run_id": state.get("run_id"),
                "slice_id": slice_id,
                "window_open": window_open,
                "difficulty": entry.get("difficulty"),
                "criteria_total": entry.get("criteria_total"),
                "outcome": outcome,
                "cause": cause,
                "decided_at": decided_at,
                "elapsed_s": (
                    None if decided_at is None else _seconds_between(events[window_open].get("ts"), decided_at)
                ),
                "steers": counts["steer"],
                "relaunches": counts["relaunch"],
                "nudges": counts["send"],
                "code": entry.get("code") if outcome == "accepted" else None,
                "submissions": _submissions(entry, events, slice_id, window),
                "reviews": reviews,
                "comparisons": _comparisons(active, recorded),
            }
        )
    return rows


def run_rows(state: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per slice window of this run, in slice order then window order.

    A window runs from a slice's `launch` event to its next `launch` (a fresh
    launch after `finalize --stop`) or the end of the log; relaunches and
    steers stay inside it. Every event-derived field reads only the row's own
    slice's events, except that a run-wide `stop` also ends `developer_s`.
    Attested slices and slices never launched have no row. Pure: no git, no
    plan file, no clock.
    """
    rows: list[dict[str, Any]] = []
    for entry in state.get("slices") or []:
        if not isinstance(entry, dict) or entry.get("status") == "attested":
            continue
        rows.extend(_slice_rows(state, entry, events))
    return rows


def write_run_file(run_dir: Path, token: str) -> Path:
    """Regenerate this run's ledger file from MAC-verified state; return its path.

    Load, read and write all happen under the run's own lock, so two
    overlapping commands can never leave the older snapshot in place.
    """
    with state_mod._advisory_lock(run_dir / ".lock"):
        state = state_mod._load_state_unlocked(run_dir, token)
        events = state_mod.read_events(run_dir)
        run_id = state["run_id"]
        payload = {
            "run_id": run_id,
            "run_tag": state.get("run_tag"),
            "events": len(events),
            "rows": run_rows(state, events),
        }
        path = ledger_dir() / host_name() / f"{run_id}.json"
        state_mod._atomic_write_bytes(path, (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return path

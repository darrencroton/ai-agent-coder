"""Ledger rows: how each Developer and Reviewer performed, per slice window.

`run_rows` turns one run's signed state and event log into rows, one per
slice window, and is the ledger's whole semantics. It is pure: no git, no
plan file, no clock. Everything it reports is already recorded elsewhere
(judgments and reviews on the signed slice entries, identities, causes and
timestamps on events), so a row is a projection, never a second copy.

`write_run_file` regenerates the per-run ledger file,
``<ledger_dir>/<host>/<run_id>.json``, from MAC-verified state under the
run's existing lock. `cli.main` calls it after every token-bearing command;
the file is derived output and is never edited by hand.

`load_run_files` and `render_leaderboard` turn every host's per-run files
into one markdown leaderboard (`pm ledger render`). The leaderboard is a pure
function of the files' names and bytes: duplicates across hosts collapse,
and nothing in it reads the clock.
"""

from __future__ import annotations

import json
import math
import os
import socket
import urllib.parse
from fractions import Fraction
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
    """Short, lowercased host name: the per-host folder under `ledger_dir()`."""
    return socket.gethostname().split(".")[0].lower()


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
    }


def _developer_judgment(entry: dict[str, Any], origin: int) -> dict[str, Any] | None:
    for record in judgments._active_developer_judgments(entry):
        if judgments._developer_origin_index(record) != origin:
            continue
        if record.get("status") == "unavailable":
            return {"status": "unavailable"}
        if "score" in record:
            return {
                "score": record.get("score"),
                "criteria_met": record.get("criteria_met"),
                "defects": record.get("defects"),
            }
    return None


def _developer_seconds(
    events: list[dict[str, Any]], slice_id: str, origin: int
) -> int | None:
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


def _rating(
    active: list[dict[str, Any]], skill: Any, review_id: Any
) -> dict[str, Any] | None:
    for record in active:
        if record.get("assessment") != "rating" or record.get("skill") != skill:
            continue
        if review_id not in judgments._referenced_ids(record):
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
                "origin": data.get("origin_event_index"),
                "reason": data.get("reason"),
            }
        )
    return rows, recorded


def _comparisons(
    active: list[dict[str, Any]], recorded: dict[tuple[Any, Any], int]
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for record in active:
        if record.get("assessment") != "comparison":
            continue
        skill = record.get("skill")
        if record.get("status") == "unavailable":
            ids = [
                review_id
                for review_id in record.get("review_ids") or []
                if (skill, review_id) in recorded
            ]
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


def _outcome(
    events: list[dict[str, Any]], window: list[int]
) -> tuple[str, str | None, str | None]:
    """`(outcome, cause, decided_at)` by precedence accepted, exhausted, stopped, open."""

    def first(predicate) -> dict[str, Any] | None:
        return next(
            (events[index] for index in window if predicate(events[index])), None
        )

    accept = first(lambda event: event.get("kind") == "accept")
    if accept is not None:
        return "accepted", None, accept.get("ts")
    budget_stop = first(
        lambda event: (
            event.get("kind") == "stop" and _data(event).get("cause") == "developer"
        )
    )
    if budget_stop is not None:
        return "exhausted", "developer", budget_stop.get("ts")
    slice_stop = first(lambda event: event.get("kind") == "slice-stop")
    if slice_stop is not None:
        return "stopped", _data(slice_stop).get("cause"), slice_stop.get("ts")
    return "open", None, None


def _slice_rows(
    state: dict[str, Any], entry: dict[str, Any], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    slice_id = entry.get("id")
    own = [
        index for index, event in enumerate(events) if event.get("slice") == slice_id
    ]
    opens = [index for index in own if events[index].get("kind") == "launch"]
    active = judgments._active_judgments(entry)
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
                    None
                    if decided_at is None
                    else _seconds_between(events[window_open].get("ts"), decided_at)
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


def run_rows(
    state: dict[str, Any], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
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
            "events": len(events),
            "rows": run_rows(state, events),
        }
        path = ledger_dir() / host_name() / f"{run_id}.json"
        state_mod._atomic_write_bytes(
            path, (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
        )
    return path


# --- the leaderboard -----------------------------------------------------------

# Groups with fewer windows (Developers) or commissions (Reviewers) than this
# are listed under "Insufficient data", unranked.
MIN_WINDOWS = 3
MIN_COMMISSIONS = 3

# The frozen pre-ledger table lives beside the skill's own ledger data, never
# under `PM_LEDGER_DIR`, so an override cannot hide it.
_HISTORY_PATH = (
    Path(__file__).resolve().parents[2] / "ledger" / "historical-pre-ledger.md"
)

_DIFFICULTIES = ("easy", "moderate", "hard")
# code-health's six file categories and three line kinds, in display order.
_CODE_CATEGORIES = (
    "production",
    "test",
    "documentation",
    "configuration",
    "data",
    "other",
)
_LINE_KINDS = ("code", "comment", "blank")


def load_run_files(root: Path) -> list[tuple[str, bytes | None]]:
    """Every ``<root>/*/*.json`` as ``(host/name, bytes)``, in sorted path order.

    An unreadable file carries None in place of its bytes, so the renderer can
    list it under Errors instead of failing the whole render.
    """
    files: list[tuple[str, bytes | None]] = []
    for path in sorted(root.glob("*/*.json")):
        if not path.is_file():
            continue
        label = path.relative_to(root).as_posix()
        try:
            files.append((label, path.read_bytes()))
        except OSError:
            files.append((label, None))
    return files


def _parse_run_file(
    label: str, raw: bytes | None
) -> tuple[dict[str, Any] | None, str | None]:
    """`(payload, None)` for a well-formed per-run file, else `(None, error)`."""
    if raw is None:
        return None, f"{label}: unreadable"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        # Also covers bad UTF-8, an integer too long to convert, and nesting
        # too deep for the parser.
        return None, f"{label}: not valid JSON ({type(exc).__name__}: {exc})"
    if not (
        isinstance(payload, dict)
        and isinstance(payload.get("run_id"), str)
        and type(payload.get("events")) is int
        and isinstance(payload.get("rows"), list)
        and all(isinstance(row, dict) for row in payload["rows"])
    ):
        return None, f"{label}: missing or invalid run_id, events or rows"
    for index, row in enumerate(payload["rows"]):
        problem = _row_shape_error(row)
        if problem is not None:
            return None, f"{label}: malformed row {index}: {problem}"
    return payload, None


def _is_scalar(value: Any) -> bool:
    """A value usable as a group-key field or an id: str, int, bool or None."""
    return value is None or isinstance(value, (str, int, bool))


def _dict_or_none(value: Any) -> bool:
    return value is None or isinstance(value, dict)


def _list_or_none(value: Any) -> bool:
    return value is None or isinstance(value, list)


def _row_shape_error(row: dict[str, Any]) -> str | None:
    """What in one row the renderer cannot consume, or None.

    Every field the renderer hashes, compares or iterates is checked here, so
    a file whose interior is malformed is excluded with a named reason instead
    of failing the whole render.
    """
    for field in ("outcome", "cause", "difficulty"):
        if not _is_scalar(row.get(field)):
            return f"{field} is not a scalar"
    if not _dict_or_none(row.get("code")):
        return "code is not an object"
    for field in ("submissions", "reviews", "comparisons"):
        items = row.get(field)
        if not _list_or_none(items) or not all(
            isinstance(item, dict) for item in items or []
        ):
            return f"{field} is not a list of objects"
    for submission in row.get("submissions") or []:
        developer = submission.get("developer")
        if not _dict_or_none(developer) or not _dict_or_none(
            submission.get("judgment")
        ):
            return "a submission's developer or judgment is not an object"
        if developer is not None and not all(
            _is_scalar(developer.get(key)) for key in ("tool", "model", "effort")
        ):
            return "a submission's developer identity is not scalar"
    for review in row.get("reviews") or []:
        keys = ("review_id", "skill", "tool", "model", "effort", "command_override")
        if not all(_is_scalar(review.get(key)) for key in keys):
            return "a review's id, skill or identity is not scalar"
        if not _dict_or_none(review.get("rating")):
            return "a review's rating is not an object"
    for comparison in row.get("comparisons") or []:
        for field in ("order", "close"):
            members = comparison.get(field)
            if not _list_or_none(members) or not all(
                _is_scalar(member) for member in members or []
            ):
                return f"a comparison's {field} is not a list of scalar ids"
    return None


def _select_runs(
    files: list[tuple[str, bytes | None]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """One copy per run (the most events wins; identical copies collapse).

    Copies of a run that tie on the largest `events` but differ in bytes are
    all excluded and named, as is every malformed file. Independent of the
    order the files arrive in.
    """
    errors: list[tuple[str, str]] = []
    copies: dict[str, list[tuple[str, bytes, dict[str, Any]]]] = {}
    for label, raw in sorted(files, key=lambda item: item[0]):
        payload, error = _parse_run_file(label, raw)
        if payload is None:
            errors.append((label, error or label))
            continue
        copies.setdefault(payload["run_id"], []).append((label, raw or b"", payload))
    runs: list[dict[str, Any]] = []
    for run_id in sorted(copies):
        most = max(payload["events"] for _label, _raw, payload in copies[run_id])
        best = [copy for copy in copies[run_id] if copy[2]["events"] == most]
        if len({raw for _label, raw, _payload in best}) == 1:
            runs.append(best[0][2])
            continue
        for label, _raw, _payload in best:
            errors.append(
                (
                    label,
                    f"{label}: run {run_id} has another copy with {most} events and different contents",
                )
            )
    return runs, [message for _label, message in sorted(errors)]


# --- statistics and cells --------------------------------------------------------


def _number(value: Fraction) -> str:
    """An exact value as an integer, else to one decimal (medians end in .5)."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{float(value):.1f}"


def _mean(values: list[int]) -> Fraction | None:
    return Fraction(sum(values), len(values)) if values else None


def _median(values: list[int]) -> Fraction | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[middle])
    return Fraction(ordered[middle - 1] + ordered[middle], 2)


def _share(hits: int, total: int) -> Fraction | None:
    return Fraction(hits, total) if total else None


def _two_decimals(value: Fraction) -> str:
    """An exact value rounded half-up (away from zero) to two decimals."""
    hundredths = math.floor(abs(value) * 100 + Fraction(1, 2))
    sign = "-" if value < 0 and hundredths else ""
    return f"{sign}{hundredths // 100}.{hundredths % 100:02d}"


def _mean_cell(values: list[int]) -> str:
    mean = _mean(values)
    return "n/a" if mean is None else f"{_two_decimals(mean)} (n={len(values)})"


def _median_cell(values: list[int]) -> str:
    median = _median(values)
    return "n/a" if median is None else f"{_number(median)} (n={len(values)})"


def _share_cell(hits: int, total: int) -> str:
    if not total:
        return "n/a"
    percent = Fraction(100 * hits, total)
    return f"{hits}/{total} ({int(percent + Fraction(1, 2))}%)"


def _ratio_cell(met: int, total: int, count: int) -> str:
    return "n/a" if not count else f"{met}/{total} (n={count})"


def _key_part(value: Any) -> tuple[int, str]:
    """Sortable form of one group-key field: None sorts after every value."""
    return (1, "") if value is None else (0, str(value))


def _group_sort_key(group: tuple[Any, ...]) -> tuple[tuple[int, str], ...]:
    return tuple(_key_part(value) for value in group)


def _descending(value: Fraction | None) -> tuple[int, Fraction]:
    """Sort key for a descending statistic with None last."""
    return (1, Fraction(0)) if value is None else (0, -value)


def _label(value: Any) -> str:
    return "unknown" if value is None else str(value)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """A markdown table; pipes inside cells are escaped."""

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"

    return [line(headers), line(["---"] * len(headers)), *(line(row) for row in rows)]


def _ranked_sections(
    headers: list[str],
    ranked: list[list[str]],
    insufficient: list[list[str]],
    threshold: str,
) -> list[str]:
    lines: list[str] = []
    if ranked:
        lines += _table(
            ["Rank", *headers],
            [[str(rank), *row] for rank, row in enumerate(ranked, 1)],
        )
    else:
        lines.append("No group has enough data to rank.")
    if insufficient:
        lines += ["", f"Insufficient data ({threshold}), unranked:", ""]
        lines += _table(headers, insufficient)
    return lines


# --- Developer tables -------------------------------------------------------------


def _counts_for_developer(row: dict[str, Any]) -> bool:
    outcome = row.get("outcome")
    return outcome in {"accepted", "exhausted"} or (
        outcome == "stopped" and row.get("cause") == "developer"
    )


def _identity_key(developer: Any) -> tuple[Any, Any, Any]:
    if not isinstance(developer, dict):
        return (None, None, None)
    return (developer.get("tool"), developer.get("model"), developer.get("effort"))


def _scored(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        submission
        for submission in row.get("submissions") or []
        if isinstance(submission, dict)
        and isinstance(submission.get("judgment"), dict)
        and "score" in submission["judgment"]
    ]


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _code_lines(row: dict[str, Any], category: str) -> tuple[int, int, int] | None:
    code = row.get("code")
    if (
        not isinstance(code, dict)
        or "error" in code
        or not isinstance(code.get("lines"), dict)
    ):
        return None
    counts = code["lines"].get(category)
    if not isinstance(counts, dict):
        return None
    code_n, comment_n, blank_n = (_int(counts.get(kind)) for kind in _LINE_KINDS)
    if code_n is None or comment_n is None or blank_n is None:
        return None
    return code_n, comment_n, blank_n


def _complexity_delta(row: dict[str, Any]) -> int | None:
    code = row.get("code")
    if (
        not isinstance(code, dict)
        or "error" in code
        or not isinstance(code.get("complexity"), dict)
    ):
        return None
    before, after = code["complexity"].get("before"), code["complexity"].get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    before_sum, after_sum = _int(before.get("sum")), _int(after.get("sum"))
    if before_sum is None or after_sum is None:
        return None
    return after_sum - before_sum


def _new_developer_group() -> dict[str, Any]:
    return {
        "scores": [],
        "criteria": [],
        "major": [],
        "minor": [],
        "developer_s": [],
        "windows": 0,
        "accepted": 0,
        "first_accepted": 0,
        "submissions": [],
        "steers_nudges": [],
        "elapsed_s": [],
        "lines": {category: [] for category in _CODE_CATEGORIES},
        "complexity": [],
    }


def _developer_groups(
    rows: list[dict[str, Any]],
) -> tuple[dict[tuple[Any, ...], dict[str, Any]], int, int]:
    """Per-identity figures over the Developer-counted windows of `rows`.

    Returns the groups and the counts of windows left out of window-level
    columns: mixed identities, and no scored submission.
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    mixed = unscored = 0
    for row in rows:
        if not _counts_for_developer(row):
            continue
        scored = _scored(row)
        for submission in scored:
            group = groups.setdefault(
                _identity_key(submission.get("developer")), _new_developer_group()
            )
            score = _int(submission["judgment"].get("score"))
            if score is not None:
                group["scores"].append(score)
            seconds = _int(submission.get("developer_s"))
            if seconds is not None:
                group["developer_s"].append(seconds)
        if scored:
            first = scored[0]
            group = groups[_identity_key(first.get("developer"))]
            judgment = first["judgment"]
            met, total = (
                _int(judgment.get("criteria_met")),
                _int(row.get("criteria_total")),
            )
            if met is not None and total is not None:
                group["criteria"].append((met, total))
            defects = judgment.get("defects")
            if isinstance(defects, dict):
                levels = [
                    _int(defects.get(level)) for level in ("P0", "P1", "P2", "P3")
                ]
                if None not in levels:
                    group["major"].append(levels[0] + levels[1])
                    group["minor"].append(levels[2] + levels[3])
        identities = {
            _identity_key(submission.get("developer")) for submission in scored
        }
        if not identities:
            unscored += 1
            continue
        if len(identities) > 1:
            mixed += 1
            continue
        group = groups[identities.pop()]
        group["windows"] += 1
        if row.get("outcome") == "accepted":
            group["accepted"] += 1
            if len(scored) == 1:
                group["first_accepted"] += 1
        group["submissions"].append(len(scored))
        steers, nudges = _int(row.get("steers")), _int(row.get("nudges"))
        if steers is not None and nudges is not None:
            group["steers_nudges"].append(steers + nudges)
        elapsed = _int(row.get("elapsed_s"))
        if elapsed is not None:
            group["elapsed_s"].append(elapsed)
        for category in _CODE_CATEGORIES:
            lines = _code_lines(row, category)
            if lines is not None:
                group["lines"][category].append(lines)
        delta = _complexity_delta(row)
        if delta is not None:
            group["complexity"].append(delta)
    return groups, mixed, unscored


_DEVELOPER_HEADERS = [
    "Developer (tool / model / effort)",
    "Score (PM), mean 0–2",
    "First-submission criteria met (PM), Σk/Σn",
    "First-submission P0+P1 defects (PM), mean",
    "First-submission P2+P3 defects (PM), mean",
    "Developer time per submission, median s",
    "Windows, n",
    "Accepted, share",
    "Accepted on first submission, share",
    "Scored submissions per window, mean",
    "Steers+nudges per window, mean",
    "Wall time per window, median s",
    *(
        f"Δ {category} lines code/comment/blank, median"
        for category in _CODE_CATEGORIES
    ),
    "Δ complexity sum, median",
]


def _lines_cell(values: list[tuple[int, int, int]]) -> str:
    if not values:
        return "n/a"
    medians = []
    for kind in range(len(_LINE_KINDS)):
        median = _median([value[kind] for value in values])
        medians.append("n/a" if median is None else _number(median))
    return f"{'/'.join(medians)} (n={len(values)})"


def _developer_cells(key: tuple[Any, ...], group: dict[str, Any]) -> list[str]:
    criteria = group["criteria"]
    return [
        " / ".join(_label(value) for value in key),
        _mean_cell(group["scores"]),
        _ratio_cell(
            sum(met for met, _ in criteria),
            sum(total for _, total in criteria),
            len(criteria),
        ),
        _mean_cell(group["major"]),
        _mean_cell(group["minor"]),
        _median_cell(group["developer_s"]),
        str(group["windows"]),
        _share_cell(group["accepted"], group["windows"]),
        _share_cell(group["first_accepted"], group["windows"]),
        _mean_cell(group["submissions"]),
        _mean_cell(group["steers_nudges"]),
        _median_cell(group["elapsed_s"]),
        *(_lines_cell(group["lines"][category]) for category in _CODE_CATEGORIES),
        _median_cell(group["complexity"]),
    ]


def _developer_table(rows: list[dict[str, Any]]) -> list[str]:
    groups, mixed, unscored = _developer_groups(rows)
    if not groups and not mixed and not unscored:
        return ["No Developer-counted windows."]
    keys = sorted(groups, key=_group_sort_key)
    ranked = [key for key in keys if groups[key]["windows"] >= MIN_WINDOWS]
    ranked.sort(
        key=lambda key: (
            _descending(_mean(groups[key]["scores"])),
            _descending(_share(groups[key]["first_accepted"], groups[key]["windows"])),
            _group_sort_key(key),
        )
    )
    insufficient = [key for key in keys if groups[key]["windows"] < MIN_WINDOWS]
    lines = _ranked_sections(
        _DEVELOPER_HEADERS,
        [_developer_cells(key, groups[key]) for key in ranked],
        [_developer_cells(key, groups[key]) for key in insufficient],
        f"fewer than {MIN_WINDOWS} windows",
    )
    lines += [
        "",
        f"Window-level columns leave out {mixed} window(s) with mixed Developer identities"
        f" and {unscored} window(s) with no scored submission.",
    ]
    return lines


# --- Reviewer tables --------------------------------------------------------------


def _new_reviewer_group() -> dict[str, Any]:
    return {
        "commissions": 0,
        "rated": 0,
        "unusable": 0,
        "scores": [],
        "panels": 0,
        "wins": 0,
        "losses": 0,
        "close_wins": 0,
        "close_losses": 0,
    }


def _reviewer_groups(
    runs: list[dict[str, Any]],
) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    """Per-skill, per-identity reviewer figures over every window, any outcome."""
    by_skill: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    for run in runs:
        for row in run["rows"]:
            members: dict[Any, tuple[str, tuple[Any, ...]]] = {}
            for review in row.get("reviews") or []:
                if not isinstance(review, dict):
                    continue
                skill = _label(review.get("skill"))
                key = (
                    review.get("tool"),
                    review.get("model"),
                    review.get("effort"),
                    review.get("command_override"),
                )
                group = by_skill.setdefault(skill, {}).setdefault(
                    key, _new_reviewer_group()
                )
                group["commissions"] += 1
                rating = review.get("rating")
                if review.get("failed"):
                    group["unusable"] += 1
                    continue
                members[review.get("review_id")] = (skill, key)
                if isinstance(rating, dict) and "score" in rating:
                    group["rated"] += 1
                    score = _int(rating.get("score"))
                    if score is not None:
                        group["scores"].append(score)
                elif isinstance(rating, dict) and rating.get("status") == "unavailable":
                    group["unusable"] += 1
            for comparison in row.get("comparisons") or []:
                if isinstance(comparison, dict) and isinstance(
                    comparison.get("order"), list
                ):
                    _credit_order(by_skill, members, comparison)
    return by_skill


def _credit_order(
    by_skill: dict[str, dict[tuple[Any, ...], dict[str, Any]]],
    members: dict[Any, tuple[str, tuple[Any, ...]]],
    comparison: dict[str, Any],
) -> None:
    """Credit one panel order: panels, every implied pair, and close calls."""
    ids = comparison["order"]
    close = set(comparison.get("close") or [])
    known = [members.get(review_id) for review_id in ids]
    groups = [
        None if member is None else by_skill[member[0]][member[1]] for member in known
    ]
    for skill, key in {member for member in known if member is not None}:
        by_skill[skill][key]["panels"] += 1
    for position, upper in enumerate(groups):
        for lower in groups[position + 1 :]:
            if upper is not None:
                upper["wins"] += 1
            if lower is not None:
                lower["losses"] += 1
    for position in range(1, len(ids)):
        if ids[position] not in close:
            continue
        upper, lower = groups[position - 1], groups[position]
        if upper is not None:
            upper["close_wins"] += 1
        if lower is not None:
            lower["close_losses"] += 1


_REVIEWER_HEADERS = [
    "Reviewer (tool / model / effort / override)",
    "Commissions, n",
    "Usable (PM), share rated/(rated+unavailable+failed)",
    "Score (PM), mean 0–2",
    "Panels (PM), n",
    "Pairwise wins (PM), share",
    "Close-call wins (PM), share",
]


def _reviewer_cells(key: tuple[Any, ...], group: dict[str, Any]) -> list[str]:
    tool, model, effort, override = key
    override_label = (
        "unknown" if override is None else ("override" if override else "no override")
    )
    return [
        " / ".join([_label(tool), _label(model), _label(effort), override_label]),
        str(group["commissions"]),
        _share_cell(group["rated"], group["rated"] + group["unusable"]),
        _mean_cell(group["scores"]),
        str(group["panels"]),
        _share_cell(group["wins"], group["wins"] + group["losses"]),
        _share_cell(group["close_wins"], group["close_wins"] + group["close_losses"]),
    ]


def _reviewer_table(
    skill: str, groups: dict[tuple[Any, ...], dict[str, Any]]
) -> list[str]:
    def statistic(group: dict[str, Any]) -> Fraction | None:
        if skill == "code-review":
            return _share(group["wins"], group["wins"] + group["losses"])
        return _mean(group["scores"])

    keys = sorted(groups, key=_group_sort_key)
    ranked = [key for key in keys if groups[key]["commissions"] >= MIN_COMMISSIONS]
    ranked.sort(
        key=lambda key: (_descending(statistic(groups[key])), _group_sort_key(key))
    )
    insufficient = [key for key in keys if groups[key]["commissions"] < MIN_COMMISSIONS]
    return _ranked_sections(
        _REVIEWER_HEADERS,
        [_reviewer_cells(key, groups[key]) for key in ranked],
        [_reviewer_cells(key, groups[key]) for key in insufficient],
        f"fewer than {MIN_COMMISSIONS} commissions",
    )


# --- the document -----------------------------------------------------------------


def _as_of(rows: list[dict[str, Any]]) -> str:
    latest: tuple[Any, str] | None = None
    for row in rows:
        parsed = state_mod.parse_event_ts(row.get("decided_at"))
        if parsed is not None and (latest is None or parsed > latest[0]):
            latest = (parsed, row["decided_at"])
    return "unknown" if latest is None else latest[1]


def render_leaderboard(
    runs: list[tuple[str, bytes | None]], out_dir: Path | None = None
) -> tuple[str, list[str]]:
    """The leaderboard as markdown, and the files excluded from it.

    `runs` is `load_run_files` output in any order; the result depends only
    on the files' contents and names, never on their order or the clock. A
    History link to the frozen pre-ledger table is written relative to
    `out_dir` when that table exists.
    """
    kept, errors = _select_runs(runs)
    rows = [row for run in kept for row in run["rows"]]
    lines = ["# PM model leaderboard", "", f"As of: {_as_of(rows)}", ""]
    if _HISTORY_PATH.is_file():
        target = (
            os.path.relpath(_HISTORY_PATH, out_dir)
            if out_dir is not None
            else str(_HISTORY_PATH)
        )
        # Percent-encoded so spaces, '#' and parentheses keep the link valid.
        link = urllib.parse.quote(Path(target).as_posix(), safe="/")
        lines += [f"History: [{_HISTORY_PATH.name}]({link})", ""]
    lines += [
        f"Runs: {len(kept)}; slice windows: {len(rows)}.",
        "",
        "Every cell states its statistic and its own n; `n/a` means no values. Columns marked (PM) are"
        " PM judgment, never blended into a measured figure. There is no composite score.",
        "",
        "## Developers",
        "",
        "Counted windows: `accepted`, `exhausted`, and `stopped` with cause `developer`. Only scored"
        " submissions count. Submission-level columns credit each submission's own identity;"
        " window-level columns (from Windows on) credit a window only when all its scored submissions"
        " share one identity.",
        "",
        "### All difficulties",
        "",
        *_developer_table(rows),
    ]
    bands = sorted(
        {row.get("difficulty") for row in rows} - set(_DIFFICULTIES),
        key=_key_part,
    )
    for band in [*_DIFFICULTIES, *bands]:
        lines += ["", f"### Difficulty: {_label(band)}", ""]
        lines += _developer_table(
            [row for row in rows if row.get("difficulty") == band]
        )
    lines += ["", "## Reviewers"]
    reviewer_groups = _reviewer_groups(kept)
    if not reviewer_groups:
        lines += ["", "No reviews recorded."]
    for skill in sorted(reviewer_groups):
        order = "pairwise wins" if skill == "code-review" else "mean score"
        lines += ["", f"### {skill}", "", f"Ranked by {order}.", ""]
        lines += _reviewer_table(skill, reviewer_groups[skill])
    lines += ["", "## Errors", ""]
    lines += [f"- {error}" for error in errors] if errors else ["None."]
    return "\n".join(lines) + "\n", errors

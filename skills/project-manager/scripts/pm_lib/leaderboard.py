"""The cross-run leaderboard: every host's per-run ledger files, one markdown.

`load_run_files` reads every ``<ledger_dir>/<host>/*.json`` and
`render_leaderboard` turns them into the leaderboard `pm ledger render`
writes: Developer tables overall and per difficulty, Reviewer tables per
skill. The leaderboard is a pure function of the files' names and bytes:
duplicates across hosts collapse, every statistic is exact (`Fraction`, then
half-up `Decimal` rounding for display), and nothing in it reads the clock.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import PmError
from . import state as state_mod

# Groups with fewer windows (Developers) or commissions (Reviewers) than this
# are listed under "Insufficient data", unranked.
MIN_WINDOWS = 3
MIN_COMMISSIONS = 3

# The frozen pre-ledger table lives beside the skill's own ledger data, never
# under `PM_LEDGER_DIR`, so an override cannot hide it.
_HISTORY_PATH = Path(__file__).resolve().parents[2] / "ledger" / "historical-pre-ledger.md"

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
# The identity fields a Developer submission and a review both carry; a
# Reviewer's group key also holds its `command_override`.
_IDENTITY_FIELDS = ("tool", "model", "effort", "model_tag")


def load_run_files(root: Path) -> list[tuple[str, bytes | None]]:
    """Every ``<root>/<host>/*.json`` as ``(host/name, bytes)``, in sorted path order.

    Exactly one level of host folders: files directly under `root`, and
    anything below a host folder, are ignored. A file that cannot be read (a
    dangling symlink, say) carries None in place of its bytes, and so does a
    host folder that cannot be listed, labelled ``host/``, so the renderer
    lists either under Errors instead of losing it or failing the whole
    render. A missing `root` has no files.
    """
    try:
        hosts = sorted(root.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PmError(f"cannot list the ledger directory {root}: {exc}") from exc
    files: list[tuple[str, bytes | None]] = []
    for host in hosts:
        if not host.is_dir():
            continue
        try:
            entries = sorted(host.iterdir())
        except OSError:
            files.append((f"{host.name}/", None))
            continue
        for path in entries:
            if not path.name.endswith(".json") or path.is_dir():
                continue
            label = f"{host.name}/{path.name}"
            try:
                files.append((label, path.read_bytes() if path.is_file() else None))
            except OSError:
                files.append((label, None))
    return files


def _parse_run_file(label: str, raw: bytes | None) -> tuple[dict[str, Any] | None, str | None]:
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
        and _str_or_none(payload.get("run_tag"))
        and type(payload.get("events")) is int
        and isinstance(payload.get("rows"), list)
        and all(isinstance(row, dict) for row in payload["rows"])
    ):
        return None, f"{label}: missing or invalid run_id, run_tag, events or rows"
    for index, row in enumerate(payload["rows"]):
        problem = _row_shape_error(row)
        if problem is not None:
            return None, f"{label}: malformed row {index}: {problem}"
    return payload, None


def _str_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


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
        if not _str_or_none(row.get(field)):
            return f"{field} is not a string or null"
    if not _dict_or_none(row.get("code")):
        return "code is not an object"
    for field in ("submissions", "reviews", "comparisons"):
        items = row.get(field)
        if not _list_or_none(items) or not all(isinstance(item, dict) for item in items or []):
            return f"{field} is not a list of objects"
    for submission in row.get("submissions") or []:
        developer = submission.get("developer")
        if not _dict_or_none(developer) or not _dict_or_none(submission.get("judgment")):
            return "a submission's developer or judgment is not an object"
        if developer is not None and not all(_str_or_none(developer.get(key)) for key in _IDENTITY_FIELDS):
            return "a submission's developer identity is not strings or null"
    for review in row.get("reviews") or []:
        keys = ("review_id", "skill", *_IDENTITY_FIELDS)
        if not all(_str_or_none(review.get(key)) for key in keys):
            return "a review's id, skill or identity is not strings or null"
        override = review.get("command_override")
        if not (override is None or isinstance(override, bool)):
            return "a review's command_override is not a boolean or null"
        if not _dict_or_none(review.get("rating")):
            return "a review's rating is not an object"
    for comparison in row.get("comparisons") or []:
        for field in ("order", "close"):
            members = comparison.get(field)
            if not _list_or_none(members) or not all(isinstance(member, str) for member in members or []):
                return f"a comparison's {field} is not a list of review ids"
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


def _rounded(value: Fraction, places: int) -> str:
    """`value` rounded half-up (away from zero) to `places` decimals, exactly.

    The division carries enough digits that its own rounding can never move
    the value across a half-way point, so only the final quantize rounds.
    """
    with localcontext() as context:
        context.prec = len(str(value.numerator)) + len(str(value.denominator)) + places + 3
        exact = Decimal(value.numerator) / Decimal(value.denominator)
        rounded = exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    # A value that rounds to zero prints unsigned.
    return str(abs(rounded) if rounded.is_zero() else rounded)


def _number(value: Fraction) -> str:
    """A median of ints, exactly: an integer, or an integer and a half."""
    if value.denominator == 1:
        return str(value.numerator)
    return _rounded(value, 1)


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


def _mean_cell(values: list[int]) -> str:
    mean = _mean(values)
    return "n/a" if mean is None else f"{_rounded(mean, 2)} (n={len(values)})"


def _median_cell(values: list[int]) -> str:
    median = _median(values)
    return "n/a" if median is None else f"{_number(median)} (n={len(values)})"


def _share_cell(hits: int, total: int) -> str:
    if not total:
        return "n/a"
    return f"{hits}/{total} ({_rounded(Fraction(100 * hits, total), 0)}%)"


def _ratio_cell(met: int, total: int, count: int) -> str:
    return "n/a" if not count else f"{met}/{total} (n={count})"


def _key_part(value: Any) -> tuple[int, Any]:
    """Sortable form of one group-key field: None sorts after every value.

    `_row_shape_error` keeps each field to one type (str, or bool for
    `command_override`) besides None, so values always compare.
    """
    return (1, "") if value is None else (0, value)


def _group_sort_key(group: tuple[Any, ...]) -> tuple[tuple[int, Any], ...]:
    return tuple(_key_part(value) for value in group)


def _descending(value: Fraction | None) -> tuple[int, Fraction]:
    """Sort key for a descending statistic with None last."""
    return (1, Fraction(0)) if value is None else (0, -value)


def _label(value: Any) -> str:
    return "unknown" if value is None else str(value)


def _identity_label(tool: Any, model: Any, effort: Any, *rest: str, model_tag: Any) -> str:
    """`tool / model / effort / …rest / tag`: an unset tag is normal, so it reads `untagged`."""
    tag = state_mod.UNTAGGED if model_tag is None else model_tag
    return " / ".join([_label(tool), _label(model), _label(effort), *rest, tag])


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
    return outcome in {"accepted", "exhausted"} or (outcome == "stopped" and row.get("cause") == "developer")


def _identity_key(developer: Any) -> tuple[Any, ...]:
    """`(tool, model, effort, model_tag)`; all None for a missing identity."""
    if not isinstance(developer, dict):
        return (None,) * len(_IDENTITY_FIELDS)
    return tuple(developer.get(field) for field in _IDENTITY_FIELDS)


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
    if not isinstance(code, dict) or "error" in code or not isinstance(code.get("lines"), dict):
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
    if not isinstance(code, dict) or "error" in code or not isinstance(code.get("complexity"), dict):
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


def _credit_submissions(
    groups: dict[tuple[Any, ...], dict[str, Any]],
    row: dict[str, Any],
    scored: list[dict[str, Any]],
) -> None:
    """Submission-level figures, each credited to its own submission's identity.

    Every scored submission adds its score and Developer time; the window's
    first scored submission also adds its criteria met and defect counts.
    """
    for submission in scored:
        group = groups.setdefault(_identity_key(submission.get("developer")), _new_developer_group())
        score = _int(submission["judgment"].get("score"))
        if score is not None:
            group["scores"].append(score)
        seconds = _int(submission.get("developer_s"))
        if seconds is not None:
            group["developer_s"].append(seconds)
    if not scored:
        return
    first = scored[0]
    group = groups[_identity_key(first.get("developer"))]
    judgment = first["judgment"]
    met, total = _int(judgment.get("criteria_met")), _int(row.get("criteria_total"))
    if met is not None and total is not None:
        group["criteria"].append((met, total))
    defects = judgment.get("defects")
    if isinstance(defects, dict):
        levels = [_int(defects.get(level)) for level in ("P0", "P1", "P2", "P3")]
        if None not in levels:
            group["major"].append(levels[0] + levels[1])
            group["minor"].append(levels[2] + levels[3])


def _credit_window(group: dict[str, Any], row: dict[str, Any], scored: int) -> None:
    """Window-level figures, for a window whose `scored` submissions share `group`'s identity."""
    group["windows"] += 1
    if row.get("outcome") == "accepted":
        group["accepted"] += 1
        if scored == 1:
            group["first_accepted"] += 1
    group["submissions"].append(scored)
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
        _credit_submissions(groups, row, scored)
        identities = {_identity_key(submission.get("developer")) for submission in scored}
        if not identities:
            unscored += 1
        elif len(identities) > 1:
            mixed += 1
        else:
            _credit_window(groups[identities.pop()], row, len(scored))
    return groups, mixed, unscored


_DEVELOPER_HEADERS = [
    "Developer (tool / model / effort / tag)",
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
    *(f"Δ {category} lines code/comment/blank, median" for category in _CODE_CATEGORIES),
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
        _identity_label(*key[:3], model_tag=key[3]),
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
                    review.get("model_tag"),
                )
                group = by_skill.setdefault(skill, {}).setdefault(key, _new_reviewer_group())
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
                if isinstance(comparison, dict) and isinstance(comparison.get("order"), list):
                    _credit_order(by_skill, members, comparison)
    return by_skill


def _credit_order(
    by_skill: dict[str, dict[tuple[Any, ...], dict[str, Any]]],
    members: dict[Any, tuple[str, tuple[Any, ...]]],
    comparison: dict[str, Any],
) -> None:
    """Credit one panel order: panels, every implied pair, and close calls.

    A pair whose two members share one group (the same configuration
    commissioned twice) is skipped: it would credit that group a win and a
    loss at once.
    """
    ids = comparison["order"]
    close = set(comparison.get("close") or [])
    known = [members.get(review_id) for review_id in ids]
    groups = [None if member is None else by_skill[member[0]][member[1]] for member in known]
    for skill, key in {member for member in known if member is not None}:
        by_skill[skill][key]["panels"] += 1
    for position, upper in enumerate(groups):
        for lower in groups[position + 1 :]:
            if upper is lower:
                continue
            if upper is not None:
                upper["wins"] += 1
            if lower is not None:
                lower["losses"] += 1
    for position in range(1, len(ids)):
        if ids[position] not in close:
            continue
        upper, lower = groups[position - 1], groups[position]
        if upper is lower:
            continue
        if upper is not None:
            upper["close_wins"] += 1
        if lower is not None:
            lower["close_losses"] += 1


_REVIEWER_HEADERS = [
    "Reviewer (tool / model / effort / override / tag)",
    "Commissions, n",
    "Usable (PM), share rated/(rated+unavailable+failed)",
    "Score (PM), mean 0–2",
    "Panels (PM), n",
    "Pairwise wins (PM), share",
    "Close-call wins (PM), share",
]


def _reviewer_cells(key: tuple[Any, ...], group: dict[str, Any]) -> list[str]:
    tool, model, effort, override, model_tag = key
    override_label = "unknown" if override is None else ("override" if override else "no override")
    return [
        _identity_label(tool, model, effort, override_label, model_tag=model_tag),
        str(group["commissions"]),
        _share_cell(group["rated"], group["rated"] + group["unusable"]),
        _mean_cell(group["scores"]),
        str(group["panels"]),
        _share_cell(group["wins"], group["wins"] + group["losses"]),
        _share_cell(group["close_wins"], group["close_wins"] + group["close_losses"]),
    ]


def _reviewer_table(skill: str, groups: dict[tuple[Any, ...], dict[str, Any]]) -> list[str]:
    def statistic(group: dict[str, Any]) -> Fraction | None:
        if skill == "code-review":
            return _share(group["wins"], group["wins"] + group["losses"])
        return _mean(group["scores"])

    keys = sorted(groups, key=_group_sort_key)
    ranked = [key for key in keys if groups[key]["commissions"] >= MIN_COMMISSIONS]
    ranked.sort(key=lambda key: (_descending(statistic(groups[key])), _group_sort_key(key)))
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
    runs: list[tuple[str, bytes | None]],
    out_dir: Path | None = None,
    run_tags: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """The leaderboard as markdown, and the files excluded from it.

    `runs` is `load_run_files` output in any order; the result depends only
    on the files' contents and names, never on their order or the clock. A
    History link to the frozen pre-ledger table is written relative to
    `out_dir` when that table exists. A non-empty `run_tags` keeps only the
    runs whose `run_tag` is in it, after duplicate copies are resolved, and
    raises `PmError` when no run is left (naming how many files were excluded
    as malformed, since one of them may carry the tag).
    """
    kept, errors = _select_runs(runs)
    lines = ["# PM model leaderboard", ""]
    if run_tags:
        kept = [run for run in kept if run.get("run_tag") in run_tags]
        if not kept:
            excluded = f" ({len(errors)} malformed or conflicting file(s) excluded)" if errors else ""
            raise PmError(f"no per-run ledger file has run tag {', '.join(sorted(run_tags))}{excluded}")
    rows = [row for run in kept for row in run["rows"]]
    lines += [f"As of: {_as_of(rows)}", ""]
    if run_tags:
        lines += [f"Run tags: {', '.join(sorted(run_tags))}", ""]
    if _HISTORY_PATH.is_file():
        target = os.path.relpath(_HISTORY_PATH, out_dir) if out_dir is not None else str(_HISTORY_PATH)
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
        lines += _developer_table([row for row in rows if row.get("difficulty") == band])
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

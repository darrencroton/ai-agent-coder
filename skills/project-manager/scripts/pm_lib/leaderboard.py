"""The cross-run leaderboard: every host's per-run ledger files, one markdown.

`load_run_files` reads every ``<ledger_dir>/<host>/*.json`` and
`render_leaderboard` turns them into the leaderboard `pm ledger render`
writes: one Developer table ranked within difficulty bands, and one Reviewer
table per skill, code review's followed by a head-to-head table. The
leaderboard is a pure function of the files' names and bytes: duplicates
across hosts collapse, every statistic is exact (`Fraction`, then half-up
`Decimal` rounding for display), and nothing in it reads the clock.
`retag_run_files` is the one writer here: it edits a finished run's tags in
the per-run files themselves.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import PmError
from . import review as review_mod
from . import state as state_mod

# Groups with fewer windows (Developers) or commissions (Reviewers) than this
# are listed unranked, after the ranked rows.
MIN_WINDOWS = 3
MIN_COMMISSIONS = 3

# The rank cell of an unranked group.
_UNRANKED = "–"
# Developer difficulty bands in display order; any other label follows them.
_DIFFICULTIES = ("hard", "moderate", "easy")
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


def retag_run_files(root: Path, run_id: str, *, run_tag: str | None, model_tags: dict[str, str]) -> list[Path]:
    """Retag every host copy of one run's per-run file in place; return the rewritten paths, sorted.

    `run_tag` replaces the file's `run_tag` (None leaves it). `model_tags`
    maps OLD to NEW model tags, applied in one pass to every submission's
    Developer and every review (recorded or failed), so a swap is well
    defined; OLD may be `state.UNTAGGED` to select identities with no tag.
    Every copy is rewritten with `write_run_file`'s serialisation, so copies
    that were identical stay identical.

    Raises `PmError` before anything is written for an invalid tag, an OLD
    that matches no identity in any copy, no copy of `run_id` under `root`,
    a malformed copy, or a host folder that cannot be listed (a copy there
    would later conflict with the retagged ones); a file belongs to the run
    by its parsed `run_id`, or by its name `<run_id>.json` when it cannot be
    parsed. Copies are replaced one at a time; if one write fails, the copies
    already rewritten are restored from their original bytes, so a failure
    never leaves the run's copies disagreeing, and the error says so.
    """
    new_run_tag = state_mod.tag_value(run_tag, flag="--run-tag")
    mapping = {old: state_mod.tag_value(new, flag="--model-tag") for old, new in model_tags.items()}

    copies: list[tuple[Path, bytes, dict[str, Any]]] = []
    for label, raw in load_run_files(root):
        payload, error = _parse_run_file(label, raw)
        if payload is None:
            if label.endswith("/"):
                raise PmError(f"cannot retag while the host folder {root / label} cannot be listed")
            if Path(label).stem == run_id:
                raise PmError(f"cannot retag {root / label}: {error}")
        elif payload["run_id"] == run_id:
            copies.append((root / label, raw or b"", payload))
    if not copies:
        raise PmError(f"no per-run ledger file for run {run_id} under {root}")

    matched = {old: 0 for old in mapping}
    for _path, _raw, payload in copies:
        for identity in _identities(payload):
            key = state_mod.UNTAGGED if identity.get("model_tag") is None else identity.get("model_tag")
            if key in mapping:
                identity["model_tag"] = mapping[key]
                matched[key] += 1
    for old, count in matched.items():
        if not count:
            raise PmError(f"--model-tag {old}: no identity in run {run_id} carries that tag under {root}")

    rewritten: list[tuple[Path, bytes]] = []
    for path, raw, payload in copies:
        if new_run_tag is not None:
            payload["run_tag"] = new_run_tag
        data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
        try:
            state_mod._atomic_write_bytes(path, data)
        except OSError as exc:
            unrestored = []
            for done, original in rewritten:
                try:
                    state_mod._atomic_write_bytes(done, original)
                except OSError:
                    unrestored.append(str(done))
            outcome = (
                "the copies already rewritten were restored, so nothing changed"
                if not unrestored
                else f"and these rewritten copies could not be restored: {', '.join(unrestored)}"
            )
            raise PmError(f"cannot rewrite {path}: {exc}; {outcome}") from exc
        rewritten.append((path, raw))
    return sorted(path for path, _original in rewritten)


def _identities(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every dict in a parsed per-run file that carries a `model_tag`: Developers and reviews."""
    found: list[dict[str, Any]] = []
    for row in payload["rows"]:
        found += [s["developer"] for s in row.get("submissions") or [] if s.get("developer") is not None]
        found += row.get("reviews") or []
    return found


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
        and _str_or_none(payload.get("pm_model"))
        and type(payload.get("events")) is int
        and isinstance(payload.get("rows"), list)
        and all(isinstance(row, dict) for row in payload["rows"])
    ):
        return None, f"{label}: missing or invalid run_id, run_tag, pm_model, events or rows"
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
        order = comparison.get("order")
        if not _list_or_none(order) or not all(isinstance(member, str) for member in order or []):
            return "a comparison's order is not a list of review ids"
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
    """The mean to two decimals."""
    mean = _mean(values)
    return "n/a" if mean is None else _rounded(mean, 2)


def _minutes_cell(seconds: list[int]) -> str:
    """The exact median of `seconds`, in minutes to one decimal."""
    median = _median(seconds)
    return "n/a" if median is None else _rounded(median / 60, 1)


def _signed_cell(values: list[int]) -> str:
    """The exact median, a positive one signed: `+490.5`, `0`, `-12`."""
    median = _median(values)
    if median is None:
        return "n/a"
    return f"+{_number(median)}" if median > 0 else _number(median)


def _share_cell(hits: int, total: int) -> str:
    if not total:
        return "n/a"
    return f"{hits}/{total} ({_rounded(Fraction(100 * hits, total), 0)}%)"


def _pair_cell(first: str, second: str) -> str:
    """Two statistics in one cell, `a / b`; `n/a` when neither has values."""
    return "n/a" if first == second == "n/a" else f"{first} / {second}"


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


def _ranked_keys(
    groups: dict[tuple[Any, ...], dict[str, Any]],
    size: Callable[[dict[str, Any]], int],
    minimum: int,
    statistics: Callable[[dict[str, Any]], tuple[Any, ...]],
) -> list[tuple[str, tuple[Any, ...]]]:
    """`(rank cell, group key)` in table order.

    Groups whose `size` reaches `minimum` come first, numbered from 1, ordered
    by `statistics` (a descending sort key) and then group key; the rest
    follow in group-key order with rank `–`.
    """
    keys = sorted(groups, key=_group_sort_key)
    ranked = sorted(
        (key for key in keys if size(groups[key]) >= minimum),
        key=lambda key: (statistics(groups[key]), _group_sort_key(key)),
    )
    unranked = [key for key in keys if size(groups[key]) < minimum]
    return [*((str(rank), key) for rank, key in enumerate(ranked, 1)), *((_UNRANKED, key) for key in unranked)]


def _label(value: Any) -> str:
    return "unknown" if value is None else str(value)


def _identity_label(tool: Any, model: Any, effort: Any, *notes: str | None) -> str:
    """`tool / model / effort`, then each note that is set (an override, a model tag)."""
    return " / ".join([_label(tool), _label(model), _label(effort), *(note for note in notes if note is not None)])


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """A markdown table; pipes inside cells are escaped."""

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"

    return [line(headers), line(["---"] * len(headers)), *(line(row) for row in rows)]


# --- Developer table --------------------------------------------------------------


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


def _code_field(row: dict[str, Any], field: str) -> dict[str, Any] | None:
    """One object from a row's code-health block, or None when the block failed or lacks it."""
    code = row.get("code")
    if not isinstance(code, dict) or "error" in code or not isinstance(code.get(field), dict):
        return None
    return code[field]


def _code_lines(row: dict[str, Any], category: str) -> int | None:
    """The change in one file category's code lines (comments and blanks left out)."""
    counts = (_code_field(row, "lines") or {}).get(category)
    return _int(counts.get("code")) if isinstance(counts, dict) else None


def _complexity_delta(row: dict[str, Any]) -> int | None:
    complexity = _code_field(row, "complexity") or {}
    before, after = complexity.get("before"), complexity.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    before_sum, after_sum = _int(before.get("sum")), _int(after.get("sum"))
    if before_sum is None or after_sum is None:
        return None
    return after_sum - before_sum


def _new_developer_group() -> dict[str, Any]:
    return {
        "first_scores": [],
        "criteria": [],
        "major": [],
        "minor": [],
        "developer_s": [],
        "windows": 0,
        "accepted": 0,
        "first_accepted": 0,
        "interventions": [],
        "elapsed_s": [],
        "production_lines": [],
        "test_lines": [],
        "complexity": [],
    }


def _credit_submissions(
    groups: dict[tuple[Any, ...], dict[str, Any]],
    row: dict[str, Any],
    scored: list[dict[str, Any]],
) -> None:
    """Submission-level figures, each credited to its own submission's identity.

    Every scored submission adds its Developer time; the window's first scored
    submission also adds its score, criteria met and defect counts.
    """
    for submission in scored:
        group = groups.setdefault(_identity_key(submission.get("developer")), _new_developer_group())
        seconds = _int(submission.get("developer_s"))
        if seconds is not None:
            group["developer_s"].append(seconds)
    if not scored:
        return
    first = scored[0]
    group = groups[_identity_key(first.get("developer"))]
    judgment = first["judgment"]
    score = _int(judgment.get("score"))
    if score is not None:
        group["first_scores"].append(score)
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
    interventions = [_int(row.get(field)) for field in ("steers", "relaunches", "nudges")]
    if None not in interventions:
        group["interventions"].append(sum(interventions))
    measured = {
        "elapsed_s": _int(row.get("elapsed_s")),
        "production_lines": _code_lines(row, "production"),
        "test_lines": _code_lines(row, "test"),
        "complexity": _complexity_delta(row),
    }
    for field, value in measured.items():
        if value is not None:
            group[field].append(value)


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
    "Difficulty",
    "Rank",
    "Developer (tool / model / effort)",
    "Accepted",
    "First-pass accept",
    "First score (PM)",
    "Criteria met, first (PM)",
    "Defects, first: P0–1 / P2–3 (PM)",
    "Interventions / window",
    "Dev time / submission",
    "Wall / window",
    "Δ code lines, prod / test",
    "Δ complexity (Python)",
]


def _developer_cells(key: tuple[Any, ...], group: dict[str, Any]) -> list[str]:
    criteria = group["criteria"]
    return [
        _identity_label(*key),
        _share_cell(group["accepted"], group["windows"]),
        _share_cell(group["first_accepted"], group["windows"]),
        _mean_cell(group["first_scores"]),
        _share_cell(sum(met for met, _ in criteria), sum(total for _, total in criteria)),
        _pair_cell(_mean_cell(group["major"]), _mean_cell(group["minor"])),
        _mean_cell(group["interventions"]),
        _minutes_cell(group["developer_s"]),
        _minutes_cell(group["elapsed_s"]),
        _pair_cell(_signed_cell(group["production_lines"]), _signed_cell(group["test_lines"])),
        _signed_cell(group["complexity"]),
    ]


def _developer_table(rows: list[dict[str, Any]]) -> list[str]:
    """One table for every difficulty band, ranked within each band."""
    bands = sorted({row.get("difficulty") for row in rows} - set(_DIFFICULTIES), key=_key_part)
    table: list[list[str]] = []
    mixed = unscored = 0
    for band in (*_DIFFICULTIES, *bands):
        groups, band_mixed, band_unscored = _developer_groups([row for row in rows if row.get("difficulty") == band])
        mixed, unscored = mixed + band_mixed, unscored + band_unscored
        ordered = _ranked_keys(
            groups,
            lambda group: group["windows"],
            MIN_WINDOWS,
            lambda group: (
                _descending(_mean(group["first_scores"])),
                _descending(_share(group["first_accepted"], group["windows"])),
            ),
        )
        for index, (rank, key) in enumerate(ordered):
            table.append([_label(band) if index == 0 else "", rank, *_developer_cells(key, groups[key])])
    if not table and not mixed and not unscored:
        return ["No Developer-counted windows."]
    lines = _table(_DEVELOPER_HEADERS, table) if table else []
    if mixed or unscored:
        lines += [
            *([""] if lines else []),
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
        "confirmed": [],
        "elapsed_s": [],
        "wins": 0,
        "losses": 0,
        # (skill, group key) of each reviewer this group placed above -> pairs.
        "beat": {},
        "failed_by_class": {},
    }


def _failure_class(reason: Any) -> str:
    """A `review-failed` reason as the leaderboard counts it: one of the
    review module's classes, else `unclassified` (an `exit <N>` nothing
    recognised, or a reason recorded before the classes existed)."""
    return reason if reason in review_mod.FAILURE_CLASSES else "unclassified"


def _failed_by_class_cell(counts: dict[str, int]) -> str:
    """`limit 14 · capacity 11 · unclassified 2` in the classes' fixed order,
    zero classes left out, and `0` when nothing failed."""
    parts = [f"{name} {counts[name]}" for name in (*review_mod.FAILURE_CLASSES, "unclassified") if counts.get(name)]
    return " · ".join(parts) or "0"


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
                    failure_class = _failure_class(review.get("reason"))
                    group["failed_by_class"][failure_class] = group["failed_by_class"].get(failure_class, 0) + 1
                    continue
                members[review.get("review_id")] = (skill, key)
                elapsed = _int(review.get("elapsed_s"))
                if elapsed is not None:
                    group["elapsed_s"].append(elapsed)
                if isinstance(rating, dict) and "score" in rating:
                    group["rated"] += 1
                    for field, value in (("scores", rating.get("score")), ("confirmed", rating.get("confirmed"))):
                        if _int(value) is not None:
                            group[field].append(value)
                elif isinstance(rating, dict) and rating.get("status") == "unavailable":
                    group["unusable"] += 1
            for comparison in row.get("comparisons") or []:
                if isinstance(comparison, dict) and isinstance(comparison.get("order"), list):
                    _credit_order(by_skill, members, comparison["order"])
    return by_skill


def _credit_order(
    by_skill: dict[str, dict[tuple[Any, ...], dict[str, Any]]],
    members: dict[Any, tuple[str, tuple[Any, ...]]],
    order: list[str],
) -> None:
    """Credit every pair one panel order implies: the upper member wins.

    A review id that names no recorded review still costs or credits its
    partner, but is no group. A pair whose two members share one group (the
    same configuration commissioned twice) is skipped: it would credit that
    group a win and a loss at once.
    """
    known = [members.get(review_id) for review_id in order]
    for position, upper in enumerate(known):
        for lower in known[position + 1 :]:
            if upper == lower:
                continue
            if upper is not None:
                group = by_skill[upper[0]][upper[1]]
                group["wins"] += 1
                if lower is not None:
                    group["beat"][lower] = group["beat"].get(lower, 0) + 1
            if lower is not None:
                by_skill[lower[0]][lower[1]]["losses"] += 1


def _reviewer_label(key: tuple[Any, ...]) -> str:
    tool, model, effort, override, model_tag = key
    override_note = {True: "override", None: "override unknown"}.get(override)
    return _identity_label(tool, model, effort, override_note, model_tag)


def _reviewer_cells(key: tuple[Any, ...], group: dict[str, Any], pairwise: bool) -> list[str]:
    wins = [_share_cell(group["wins"], group["wins"] + group["losses"])] if pairwise else []
    return [
        _reviewer_label(key),
        str(group["commissions"]),
        _share_cell(group["rated"], group["rated"] + group["unusable"]),
        _failed_by_class_cell(group["failed_by_class"]),
        _mean_cell(group["scores"]),
        _mean_cell(group["confirmed"]),
        *wins,
        _minutes_cell(group["elapsed_s"]),
    ]


def _head_to_head(
    skill: str, groups: dict[tuple[Any, ...], dict[str, Any]], ranked: list[tuple[Any, ...]]
) -> list[str]:
    """Each ranked reviewer's wins–losses against each other, or nothing when no two shared a panel."""

    def record(row_key: tuple[Any, ...], column_key: tuple[Any, ...]) -> tuple[int, int]:
        return (
            groups[row_key]["beat"].get((skill, column_key), 0),
            groups[column_key]["beat"].get((skill, row_key), 0),
        )

    rows = []
    for rank, row_key in enumerate(ranked, 1):
        cells = []
        for column_key in ranked:
            wins, losses = record(row_key, column_key)
            cells.append("·" if column_key == row_key else f"{wins}–{losses}" if wins or losses else "")
        rows.append([f"{rank} · {_reviewer_label(row_key)}", *cells])
    if all(cell in {"·", ""} for row in rows for cell in row[1:]):
        return []
    return [
        "",
        "Head-to-head (PM): each row's wins–losses against each column, by rank above;"
        " blank where two reviewers never shared a panel.",
        "",
        *_table(["Reviewer", *(str(rank) for rank in range(1, len(ranked) + 1))], rows),
    ]


def _reviewer_table(skill: str, groups: dict[tuple[Any, ...], dict[str, Any]]) -> list[str]:
    """One skill's Reviewer table; code review ranks by pairwise wins and adds the head-to-head."""
    pairwise = skill == "code-review"

    def statistics(group: dict[str, Any]) -> tuple[Any, ...]:
        if pairwise:
            return (_descending(_share(group["wins"], group["wins"] + group["losses"])),)
        return (_descending(_mean(group["scores"])),)

    ordered = _ranked_keys(groups, lambda group: group["commissions"], MIN_COMMISSIONS, statistics)
    headers = [
        "Rank",
        "Reviewer (tool / model / effort)",
        "Commissions",
        "Usable (PM)",
        "Failed by class",
        "Score (PM)",
        "Confirmed defects / review (PM)",
        *(["Pairwise wins (PM)"] if pairwise else []),
        "Review time",
    ]
    lines = _table(headers, [[rank, *_reviewer_cells(key, groups[key], pairwise)] for rank, key in ordered])
    if pairwise:
        lines += _head_to_head(skill, groups, [key for rank, key in ordered if rank != _UNRANKED])
    return lines


# --- the document -----------------------------------------------------------------


def _as_of(rows: list[dict[str, Any]]) -> str:
    latest: tuple[Any, str] | None = None
    for row in rows:
        parsed = state_mod.parse_event_ts(row.get("decided_at"))
        if parsed is not None and (latest is None or parsed > latest[0]):
            latest = (parsed, row["decided_at"])
    return "unknown" if latest is None else latest[1]


def _pm_models(runs: list[dict[str, Any]]) -> str:
    """Who sat as PM: most runs first, then by name, with `unrecorded` (no `pm_model`) last."""
    counts = Counter(run.get("pm_model") for run in runs)
    ordered = sorted(counts.items(), key=lambda item: (item[0] is None, -item[1], item[0] or ""))
    parts = [f"{'unrecorded' if model is None else model} ({n} run{'' if n == 1 else 's'})" for model, n in ordered]
    return " · ".join(parts) or "n/a"


def render_leaderboard(
    runs: list[tuple[str, bytes | None]],
    run_tags: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """The leaderboard as markdown, and the files excluded from it.

    `runs` is `load_run_files` output in any order; the result depends only
    on the files' contents and names, never on their order or the clock. A
    non-empty `run_tags` keeps only the runs whose `run_tag` is in it, after
    duplicate copies are resolved, and raises `PmError` when no run is left
    (naming how many files were excluded as malformed, since one of them may
    carry the tag).
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
    lines += [
        f"Runs: {len(kept)}; slice windows: {len(rows)}.",
        "",
        f"PM model: {_pm_models(kept)}",
        "",
        "Columns marked (PM) are PM judgment, never blended into a measured figure. There is no composite"
        " score. Times are medians in minutes; `n/a` means no values. Groups below"
        f" {MIN_WINDOWS} windows (Developers) or {MIN_COMMISSIONS} commissions (Reviewers) are listed"
        f" unranked, rank `{_UNRANKED}`.",
        "",
        "## Developers",
        "",
        "Counted windows: `accepted`, `exhausted`, and `stopped` with cause `developer`; only scored"
        " submissions count. Ranked within each difficulty band by first-submission score, then"
        " first-pass acceptance. First = a window's first scored submission."
        " Interventions = steers + relaunches + nudges. First-submission columns and Dev time credit"
        " each submission's own identity; the other columns credit a window only when all its scored"
        " submissions share one identity. Δ columns measure the accepted diff, so compare them only"
        " between Developers given the same slices.",
        "",
        *_developer_table(rows),
        "",
        "## Reviewers",
    ]
    reviewer_groups = _reviewer_groups(kept)
    if not reviewer_groups:
        lines += ["", "No reviews recorded."]
    for skill in sorted(reviewer_groups):
        intro = (
            "Ranked by pairwise wins across panel orders. A share depends on who a reviewer was panelled"
            " with, so read it with the head-to-head table."
            if skill == "code-review"
            else "Ranked by mean score."
        )
        lines += ["", f"### {skill}", "", intro, ""]
        lines += _reviewer_table(skill, reviewer_groups[skill])
    lines += ["", "## Errors", ""]
    lines += [f"- {error}" for error in errors] if errors else ["None."]
    return "\n".join(lines) + "\n", errors

"""Authenticated PM judgments of developer and reviewer usefulness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PmError, git_ops, slice_ops
from . import plan as plan_mod
from . import state as state_mod

_SKILLS = {"drift-audit", "code-review"}
_VERSION = 1
_RATING = "rating"
_COMPARISON = "comparison"
_DEFECT_KEYS = ("P0", "P1", "P2", "P3")


@dataclass(frozen=True)
class JudgmentOutcome:
    judgment_id: str
    slice_id: str
    created: bool


def load_input(path: Path) -> dict[str, Any]:
    """Read one JSON object from the caller-supplied, non-authoritative file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise PmError(f"could not read judgment input {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PmError(f"judgment input is not valid JSON: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise PmError("judgment input must be one JSON object")
    return data


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PmError(f"judgment {name} must be a non-empty string")
    return value.strip()


def _review_ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PmError(f"judgment {name} must be a non-empty list of review IDs")
    ids = [_nonempty_string(item, name) for item in value]
    if len(ids) != len(set(ids)):
        raise PmError(f"judgment {name} contains a duplicate review ID")
    return ids


def _parse_input(data: dict[str, Any]) -> dict[str, Any]:
    version = data.get("schema_version")
    if type(version) is not int or version != _VERSION:
        raise PmError("judgment schema_version must be integer 1")
    slice_id = _nonempty_string(data.get("slice"), "slice")
    skill = data.get("skill")
    if not isinstance(skill, str) or skill not in _SKILLS:
        raise PmError("judgment skill must be 'drift-audit' or 'code-review'")
    reason = _nonempty_string(data.get("reason"), "reason")
    supersedes = data.get("supersedes")
    if supersedes is not None:
        supersedes = _nonempty_string(supersedes, "supersedes")

    common = {"schema_version", "slice", "skill", "reason", "supersedes", "assessment"}
    assessment = data.get("assessment")
    if assessment is not None and (not isinstance(assessment, str) or assessment not in {_RATING, _COMPARISON}):
        raise PmError("judgment assessment must be 'rating' or 'comparison'")
    status = data.get("status")
    if status is not None:
        if status != "unavailable":
            raise PmError("judgment status must be 'unavailable' when supplied")
        allowed = common | {"status", "review_ids"}
        unknown = set(data) - allowed
        if unknown:
            raise PmError(f"unavailable judgment has unsupported fields: {', '.join(sorted(unknown))}")
        ids = _review_ids(data.get("review_ids"), "review_ids")
        if skill == "drift-audit" and len(ids) != 1:
            raise PmError("an unavailable drift-audit judgment must reference exactly one review ID")
        if assessment is None:
            raise PmError("an unavailable judgment must state assessment 'rating' or 'comparison'")
        if skill == "drift-audit" and assessment != _RATING:
            raise PmError("drift-audit judgments support ratings only")
        if assessment == _RATING and len(ids) != 1:
            raise PmError("an unavailable rating must reference exactly one review ID")
        if assessment == _COMPARISON and len(ids) < 2:
            raise PmError("an unavailable comparison must reference at least two review IDs")
        return {
            "schema_version": _VERSION,
            "slice": slice_id,
            "skill": skill,
            "reason": reason,
            "status": "unavailable",
            "review_ids": ids,
            "assessment": assessment,
            "supersedes": supersedes,
        }

    if skill == "drift-audit" or "score" in data or "review_id" in data:
        allowed = common | {"review_id", "score"}
        unknown = set(data) - allowed
        if unknown:
            label = "drift judgment" if skill == "drift-audit" else "code rating"
            raise PmError(f"{label} has unsupported fields: {', '.join(sorted(unknown))}")
        score = data.get("score")
        if type(score) is not int or score not in (0, 1, 2):
            label = "drift judgment" if skill == "drift-audit" else "code rating"
            raise PmError(f"{label} score must be integer 0, 1, or 2")
        if skill == "drift-audit" and assessment not in (None, _RATING):
            raise PmError("drift-audit judgments support ratings only")
        if skill == "code-review" and assessment not in (None, _RATING):
            raise PmError("code-review score judgments must use assessment 'rating'")
        return {
            "schema_version": _VERSION,
            "slice": slice_id,
            "skill": skill,
            "reason": reason,
            "review_id": _nonempty_string(data.get("review_id"), "review_id"),
            "score": score,
            "assessment": _RATING,
            "supersedes": supersedes,
        }

    if "rank_groups" in data:
        raise PmError("code-review rank_groups is no longer supported; supply order and close")
    allowed = common | {"order", "close"}
    unknown = set(data) - allowed
    if unknown:
        raise PmError(f"code-review judgment has unsupported fields: {', '.join(sorted(unknown))}")
    order = _review_ids(data.get("order"), "order")
    if len(order) < 2:
        raise PmError("code-review order must name at least two review IDs")
    close = data.get("close")
    if not isinstance(close, list):
        raise PmError("code-review close must be a list of review IDs (possibly empty)")
    close_ids = [_nonempty_string(item, "close") for item in close]
    if len(close_ids) != len(set(close_ids)):
        raise PmError("judgment close contains a duplicate review ID")
    for review_id in close_ids:
        if review_id not in order:
            raise PmError(f"code-review close member {review_id} is not in order")
        if order.index(review_id) == 0:
            raise PmError(f"code-review close member {review_id} is first in order and has no member above it")
    if assessment not in (None, _COMPARISON):
        raise PmError("code-review order must use assessment 'comparison'")
    return {
        "schema_version": _VERSION,
        "slice": slice_id,
        "skill": skill,
        "reason": reason,
        "order": order,
        "close": [review_id for review_id in order if review_id in close_ids],
        "assessment": _COMPARISON,
        "supersedes": supersedes,
    }


def _artifact_is_intact(review: dict[str, Any]) -> bool:
    artifact = review.get("artifact")
    if not isinstance(artifact, str) or not artifact:
        return False
    path = Path(artifact)
    try:
        return path.is_file() and review.get("sha256") == slice_ops.sha256_file(path)
    except OSError:
        return False


def _review_map(entry: dict[str, Any], skill: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for review in entry.get("reviews") or []:
        if not isinstance(review, dict) or review.get("skill") != skill:
            continue
        review_id = review.get("review_id")
        if isinstance(review_id, str) and review_id:
            if review_id in records:
                raise PmError(f"duplicate {skill} review ID in signed state: {review_id}")
            records[review_id] = review
    return records


def referenced_ids(judgment: dict[str, Any]) -> set[str]:
    """The review IDs one reviewer judgment (or parsed input) covers."""
    if judgment.get("status") == "unavailable":
        return {item for item in judgment.get("review_ids") or [] if isinstance(item, str)}
    if judgment.get("skill") == "drift-audit" or "review_id" in judgment:
        review_id = judgment.get("review_id")
        return {review_id} if isinstance(review_id, str) else set()
    return {item for item in judgment.get("order") or [] if isinstance(item, str)}


def _active_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude records superseded by a later immutable correction."""
    superseded = {item.get("supersedes") for item in records if isinstance(item.get("supersedes"), str)}
    return [item for item in records if item.get("judgment_id") not in superseded]


def active_judgments(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """A slice entry's reviewer judgments that no later record supersedes."""
    return _active_records([item for item in entry.get("review_judgments") or [] if isinstance(item, dict)])


def assessment_of(record: dict[str, Any]) -> str:
    """Return a stored reviewer judgment's explicit kind; raise if it has none."""
    value = record.get("assessment")
    if not isinstance(value, str) or value not in {_RATING, _COMPARISON}:
        raise PmError(
            f"review judgment {record.get('judgment_id')!r} has no valid assessment "
            f"(expected {_RATING!r} or {_COMPARISON!r})"
        )
    return value


def _input_matches(record: dict[str, Any], parsed: dict[str, Any]) -> bool:
    keys = {
        "schema_version",
        "slice",
        "skill",
        "reason",
        "status",
        "review_ids",
        "review_id",
        "score",
        "order",
        "close",
        "assessment",
        "supersedes",
    }
    wanted = {
        key: value
        for key, value in parsed.items()
        if key in keys and key not in {"slice", "assessment"} and value is not None
    }
    return {key: record.get(key) for key in wanted} == wanted and assessment_of(record) == parsed["assessment"]


def _next_judgment_id(records: list[dict[str, Any]], prefix: str) -> str:
    highest = 0
    for record in records:
        value = record.get("judgment_id")
        if isinstance(value, str) and value.startswith(prefix):
            try:
                highest = max(highest, int(value.removeprefix(prefix)))
            except ValueError:
                pass
    return f"{prefix}{highest + 1}"


def _validate_reviews(parsed: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, Any]]:
    skill = parsed["skill"]
    ids = referenced_ids(parsed)
    records = _review_map(entry, skill)
    resolved: list[dict[str, Any]] = []
    for review_id in ids:
        review = records.get(review_id)
        if review is None:
            raise PmError(f"unknown {skill} review ID: {review_id}")
        if not _artifact_is_intact(review):
            raise PmError(f"review artifact is missing or altered for {review_id}")
        resolved.append(review)
    return resolved


def _context_key(review: dict[str, Any]) -> tuple[Any, ...]:
    origin = review.get("origin_event")
    context = review.get("review_context")
    if not isinstance(origin, dict) or not isinstance(context, dict):
        raise PmError(f"review {review.get('review_id')!r} lacks commission-time context")
    return (
        origin.get("index"),
        origin.get("kind"),
        origin.get("slice"),
        review.get("head"),
        review.get("before_head"),
        review.get("grants_seen"),
        json.dumps(context, sort_keys=True, separators=(",", ":")),
    )


def _validate_comparable_panel(parsed: dict[str, Any], reviews: list[dict[str, Any]]) -> None:
    if parsed.get("status") == "unavailable" or len(reviews) <= 1:
        return
    keys = {_context_key(review) for review in reviews}
    if len(keys) != 1:
        raise PmError("code-review panel mixes commission contexts; record it as unavailable instead")


def record_judgment(run_dir: Path, token: str, data: dict[str, Any]) -> JudgmentOutcome:
    """Validate and persist a judgment against the latest locked run state."""
    parsed = _parse_input(data)
    with state_mod.locked_update(run_dir, token) as state:
        entry = slice_ops.slice_entry(state, parsed["slice"])
        if entry is None:
            raise PmError(f"{parsed['slice']} is not present in this run")
        records = [item for item in entry.get("review_judgments") or [] if isinstance(item, dict)]
        reviews = _validate_reviews(parsed, entry)
        _validate_comparable_panel(parsed, reviews)
        for record in records:
            if _input_matches(record, parsed):
                return JudgmentOutcome(str(record.get("judgment_id")), parsed["slice"], created=False)

        active = active_judgments(entry)
        supersedes = parsed.get("supersedes")
        target = next((item for item in active if item.get("judgment_id") == supersedes), None)
        if supersedes is not None:
            if target is None:
                raise PmError(f"supersedes must name an active judgment in this slice: {supersedes}")
            if target.get("skill") != parsed["skill"] or assessment_of(target) != parsed["assessment"]:
                raise PmError("supersedes must name a judgment for the same review skill and assessment")

        new_ids = referenced_ids(parsed)
        for earlier in active:
            if earlier is target:
                continue
            if assessment_of(earlier) != parsed["assessment"]:
                continue
            overlap = new_ids & referenced_ids(earlier)
            if overlap:
                raise PmError("review IDs already have an active judgment: " + ", ".join(sorted(overlap)))

        judgment = {key: value for key, value in parsed.items() if value is not None and key != "slice"}
        judgment["judgment_id"] = _next_judgment_id(records, "judgment-")
        judgment["at"] = state_mod.utc_now_iso()
        entry["review_judgments"] = [*records, judgment]
        return JudgmentOutcome(judgment["judgment_id"], parsed["slice"], created=True)


def _review_ids_without_coverage(state: dict[str, Any], *, assessment: str) -> list[tuple[str, str]]:
    """Return successful review IDs missing one independent assessment kind."""
    missing: list[tuple[str, str]] = []
    for entry in state.get("slices") or []:
        if not isinstance(entry, dict):
            continue
        covered = set().union(
            *(referenced_ids(item) for item in active_judgments(entry) if assessment_of(item) == assessment)
        )
        for review in entry.get("reviews") or []:
            if not isinstance(review, dict):
                continue
            review_id = review.get("review_id")
            if isinstance(review_id, str) and review_id and review_id not in covered:
                missing.append((str(entry.get("id")), review_id))
    return missing


def unjudged_review_ids(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Stable successful commissions without active individual rating coverage."""
    return _review_ids_without_coverage(state, assessment=_RATING)


def panel_groups(entry: dict[str, Any], origin_index: int) -> list[list[str]]:
    """Return the comparable code-review panels of one Developer submission.

    A panel is two or more intact code-review reports of the origin that share
    one commission context and have no active unavailable rating. Groups and
    their members keep the order of ``entry["reviews"]``. A review of the origin
    that lacks commission-time context raises ``PmError`` (see ``_context_key``).
    """
    unavailable = {
        review_id
        for judgment in active_judgments(entry)
        if judgment.get("status") == "unavailable" and assessment_of(judgment) == _RATING
        for review_id in referenced_ids(judgment)
    }
    groups: dict[tuple[Any, ...], list[str]] = {}
    for review in entry.get("reviews") or []:
        if not isinstance(review, dict) or review.get("skill") != "code-review":
            continue
        origin = review.get("origin_event")
        review_id = review.get("review_id")
        if (
            not isinstance(origin, dict)
            or origin.get("index") != origin_index
            or not isinstance(review_id, str)
            or not review_id
            or review_id in unavailable
            or not _artifact_is_intact(review)
        ):
            continue
        groups.setdefault(_context_key(review), []).append(review_id)
    return [group for group in groups.values() if len(group) >= 2]


def panel_is_covered(group: list[str], entry: dict[str, Any]) -> bool:
    """Whether an active comparison-kind record of `entry` covers the whole panel.

    The record is an ``order`` or an unavailable comparison; it covers the
    panel when its referenced IDs include every member of `group`. Under
    Decision 14 the record may also name reviews outside the panel, so this
    is an "includes" test, not equality.
    """
    return any(
        set(group) <= referenced_ids(item) for item in active_judgments(entry) if assessment_of(item) == _COMPARISON
    )


def unranked_code_review_ids(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Members of code panels no active order or unavailable comparison covers."""
    missing: list[tuple[str, str]] = []
    for entry in state.get("slices") or []:
        if not isinstance(entry, dict):
            continue
        origins = sorted(
            {
                review["origin_event"]["index"]
                for review in entry.get("reviews") or []
                if isinstance(review, dict)
                and review.get("skill") == "code-review"
                and isinstance(review.get("origin_event"), dict)
                and type(review["origin_event"].get("index")) is int
            }
        )
        for origin_index in origins:
            for group in panel_groups(entry, origin_index):
                if not panel_is_covered(group, entry):
                    missing.extend((str(entry.get("id")), review_id) for review_id in group)
    return missing


def _parse_developer_input(data: dict[str, Any]) -> dict[str, Any]:
    version = data.get("schema_version")
    if type(version) is not int or version != _VERSION:
        raise PmError("developer judgment schema_version must be integer 1")
    slice_id = _nonempty_string(data.get("slice"), "slice")
    reason = _nonempty_string(data.get("reason"), "reason")
    origin_event_index = data.get("origin_event_index")
    if type(origin_event_index) is not int or origin_event_index < 0:
        raise PmError("developer judgment origin_event_index must be a non-negative integer")
    supersedes = data.get("supersedes")
    if supersedes is not None:
        supersedes = _nonempty_string(supersedes, "supersedes")

    common = {"schema_version", "slice", "reason", "origin_event_index", "supersedes"}
    if data.get("status") is not None:
        if data.get("status") != "unavailable":
            raise PmError("developer judgment status must be 'unavailable' when supplied")
        unknown = set(data) - (common | {"status"})
        if unknown:
            raise PmError(f"unavailable developer judgment has unsupported fields: {', '.join(sorted(unknown))}")
        return {
            "schema_version": _VERSION,
            "slice": slice_id,
            "reason": reason,
            "origin_event_index": origin_event_index,
            "status": "unavailable",
            "supersedes": supersedes,
        }

    unknown = set(data) - (common | {"score", "criteria_met", "defects"})
    if unknown:
        raise PmError(f"developer judgment has unsupported fields: {', '.join(sorted(unknown))}")
    score = data.get("score")
    if type(score) is not int or score not in (0, 1, 2):
        raise PmError("developer judgment score must be integer 0, 1, or 2")
    criteria_met = data.get("criteria_met")
    if type(criteria_met) is not int or criteria_met < 0:
        raise PmError("developer judgment criteria_met must be a non-negative integer")
    defects = data.get("defects")
    if not isinstance(defects, dict):
        raise PmError("developer judgment defects must be an object with the keys " + ", ".join(_DEFECT_KEYS))
    if set(defects) != set(_DEFECT_KEYS):
        raise PmError(
            f"developer judgment defects must have exactly the keys {', '.join(_DEFECT_KEYS)} "
            f"(got {', '.join(sorted(map(str, defects))) or 'none'})"
        )
    for key in _DEFECT_KEYS:
        if type(defects[key]) is not int or defects[key] < 0:
            raise PmError(f"developer judgment defects {key} must be a non-negative integer")
    return {
        "schema_version": _VERSION,
        "slice": slice_id,
        "reason": reason,
        "origin_event_index": origin_event_index,
        "score": score,
        "criteria_met": criteria_met,
        "defects": {key: defects[key] for key in _DEFECT_KEYS},
        "supersedes": supersedes,
    }


def active_developer_judgments(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """A slice entry's Developer judgments that no later record supersedes."""
    return _active_records([item for item in entry.get("developer_judgments") or [] if isinstance(item, dict)])


def developer_origin_index(record: dict[str, Any]) -> int | None:
    """The origin event index a Developer judgment assesses, or None if it has none."""
    origin = (record.get("submission") or {}).get("origin_event")
    index = origin.get("index") if isinstance(origin, dict) else None
    return index if type(index) is int else None


def active_developer_judgment(entry: dict[str, Any], origin_index: int) -> dict[str, Any] | None:
    """The active Developer judgment of the submission opened at `origin_index`, or None."""
    return next(
        (
            judgment
            for judgment in active_developer_judgments(entry)
            if developer_origin_index(judgment) == origin_index
        ),
        None,
    )


def _developer_input_matches(record: dict[str, Any], parsed: dict[str, Any]) -> bool:
    wanted = {key: value for key, value in parsed.items() if key not in {"slice", "supersedes"} and value is not None}
    wanted["origin_event_index"] = parsed["origin_event_index"]
    actual = {key: record.get(key) for key in wanted if key != "origin_event_index"}
    return (
        actual == {key: value for key, value in wanted.items() if key != "origin_event_index"}
        and (developer_origin_index(record) == wanted["origin_event_index"])
        and record.get("supersedes") == parsed.get("supersedes")
    )


def read_events_or_raise(run_dir: Path) -> list[dict[str, Any]]:
    """Read the run's event log, turning an unreadable or malformed log into ``PmError``."""
    try:
        return state_mod.read_events(run_dir)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise PmError(f"could not read PM event log {run_dir / 'events.jsonl'}: {exc}") from exc


def _scan_latest_developer_origin(events: list[dict[str, Any]], slice_id: str) -> dict[str, Any] | None:
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.get("slice") == slice_id and event.get("kind") in {"launch", "relaunch", "steer"}:
            return {"index": index, "kind": event["kind"], "slice": slice_id}
    return None


def _latest_developer_origin(run_dir: Path, slice_id: str) -> dict[str, Any]:
    origin = _scan_latest_developer_origin(read_events_or_raise(run_dir), slice_id)
    if origin is None:
        raise PmError(f"current developer submission for {slice_id} has no launch, relaunch, or steer event")
    return origin


def _origin_from_current(
    state: dict[str, Any], parsed: dict[str, Any], run_dir: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = state.get("current_slice")
    if not isinstance(current, dict) or current.get("id") != parsed["slice"]:
        raise PmError(
            "developer judgment is not an exact retry or correction; it must assess the current slice submission"
        )
    developer = current.get("developer")
    if not isinstance(developer, dict):
        raise PmError("current developer submission lacks a stable launch identity")
    origin = _latest_developer_origin(run_dir, parsed["slice"])
    if origin["index"] != parsed["origin_event_index"]:
        raise PmError("developer judgment origin_event_index does not match the current developer submission")
    submission = {
        "origin_event": origin,
        "head": git_ops.git_head(Path(str(state.get("repo") or ""))),
        "before_head": current.get("before_head"),
        "grants_seen": len(plan_mod.slice_grants(state, parsed["slice"])),
    }
    identity = {
        "tool": developer.get("tool"),
        "model": developer.get("model"),
        "effort": developer.get("effort"),
        "model_tag": developer.get("model_tag"),
    }
    return submission, identity


def record_developer_judgment(run_dir: Path, token: str, data: dict[str, Any]) -> JudgmentOutcome:
    """Persist one PM developer assessment with immutable launch provenance."""
    parsed = _parse_developer_input(data)
    with state_mod.locked_update(run_dir, token) as state:
        entry = slice_ops.slice_entry(state, parsed["slice"])
        if entry is None:
            raise PmError(f"{parsed['slice']} is not present in this run")
        if "criteria_met" in parsed:
            criteria_total = entry.get("criteria_total")
            if type(criteria_total) is not int or criteria_total < 0:
                raise PmError(
                    f"{parsed['slice']} has no valid criteria_total in signed state; criteria_met cannot be checked"
                )
            if parsed["criteria_met"] > criteria_total:
                raise PmError(
                    f"developer judgment criteria_met {parsed['criteria_met']} exceeds "
                    f"{parsed['slice']} criteria_total {criteria_total}"
                )
        records = [item for item in entry.get("developer_judgments") or [] if isinstance(item, dict)]
        current_submission: dict[str, Any] | None = None
        current_identity: dict[str, Any] | None = None
        current = state.get("current_slice")
        if isinstance(current, dict) and current.get("id") == parsed["slice"]:
            origin = _scan_latest_developer_origin(read_events_or_raise(run_dir), parsed["slice"])
            if origin is not None and origin["index"] == parsed["origin_event_index"]:
                current_submission, current_identity = _origin_from_current(state, parsed, run_dir)
        for record in records:
            if _developer_input_matches(record, parsed):
                if current_submission is not None and record.get("submission") != current_submission:
                    raise PmError("developer submission changed since this judgment; use supersedes to correct it")
                return JudgmentOutcome(str(record.get("judgment_id")), parsed["slice"], created=False)

        active = active_developer_judgments(entry)
        supersedes = parsed.get("supersedes")
        target = next((item for item in active if item.get("judgment_id") == supersedes), None)
        if supersedes is not None:
            if target is None:
                raise PmError(f"supersedes must name an active developer judgment in this slice: {supersedes}")
            if developer_origin_index(target) != parsed["origin_event_index"]:
                raise PmError("supersedes must name the same developer submission origin")
            if current_submission is not None:
                submission, identity = current_submission, current_identity or {}
            else:
                submission = dict(target.get("submission") or {})
                identity = dict(target.get("developer") or {})
        else:
            if current_submission is not None:
                submission, identity = current_submission, current_identity or {}
            else:
                submission, identity = _origin_from_current(state, parsed, run_dir)

        for earlier in active:
            if earlier is target:
                continue
            if developer_origin_index(earlier) == parsed["origin_event_index"]:
                raise PmError("developer submission already has an active judgment; use supersedes to correct it")

        judgment = {
            key: value
            for key, value in parsed.items()
            if value is not None and key not in {"slice", "origin_event_index"}
        }
        judgment["judgment_id"] = _next_judgment_id(records, "developer-judgment-")
        judgment["at"] = state_mod.utc_now_iso()
        judgment["submission"] = submission
        judgment["developer"] = identity
        entry["developer_judgments"] = [*records, judgment]
        return JudgmentOutcome(judgment["judgment_id"], parsed["slice"], created=True)


def unjudged_developer_origins(state: dict[str, Any], events: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    """Launch, relaunch, and steer origins without active developer coverage."""
    covered = {
        (str(entry.get("id")), developer_origin_index(judgment))
        for entry in state.get("slices") or []
        if isinstance(entry, dict)
        for judgment in active_developer_judgments(entry)
        if developer_origin_index(judgment) is not None
    }
    return [
        (str(event.get("slice")), index, str(event.get("kind")))
        for index, event in enumerate(events)
        if event.get("kind") in {"launch", "relaunch", "steer"}
        and isinstance(event.get("slice"), str)
        and (str(event["slice"]), index) not in covered
    ]


def current_submission_gaps(
    state: dict[str, Any],
    events: list[dict[str, Any]],
    repo: Path,
    slice_id: str,
) -> list[str]:
    """Name every judgment the current submission of `slice_id` still lacks.

    The current submission is the latest launch, relaunch or steer event for
    the slice; gaps on earlier submissions are never reported. An empty list
    means the submission is fully judged: an active Developer judgment taken
    at the current head and grant count, a rating-kind record for every
    intact review of the submission, and an order or unavailable comparison
    covering every panel (``panel_groups``). Reads git HEAD once; otherwise
    pure.
    """
    origin = _scan_latest_developer_origin(events, slice_id)
    if origin is None:
        return [f"no launch, relaunch or steer event for {slice_id}"]
    index = origin["index"]
    entry = slice_ops.slice_entry(state, slice_id) or {}
    gaps: list[str] = []

    developer = active_developer_judgment(entry, index)
    if developer is None:
        gaps.append(f"Developer judgment for event {index} (judge-developer)")
    else:
        submission = developer.get("submission") or {}
        if submission.get("head") != git_ops.git_head(repo) or submission.get("grants_seen") != len(
            plan_mod.slice_grants(state, slice_id)
        ):
            gaps.append(
                f"Developer judgment for event {index} is stale (head or grants changed); record a superseding judgment"
            )

    active = active_judgments(entry)
    rated = set().union(*(referenced_ids(item) for item in active if assessment_of(item) == _RATING))
    for review in entry.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        origin_event = review.get("origin_event")
        if not isinstance(origin_event, dict) or origin_event.get("index") != index:
            continue
        if not _artifact_is_intact(review):
            continue
        review_id = review.get("review_id")
        if review_id not in rated:
            gaps.append(f"rating for {review_id} ({review.get('skill')})")

    for group in panel_groups(entry, index):
        if not panel_is_covered(group, entry):
            gaps.append(
                f"panel order for {', '.join(group)} (judge-reviews order, or one "
                "unavailable comparison covering all of them)"
            )
    return gaps


def publish_event(run_dir: Path, token: str, judgment_id: str, slice_id: str, *, kind: str = "review-judgment") -> None:
    """Publish one judgment event, atomically checking for an exact retry first."""
    with state_mod._advisory_lock(run_dir / ".lock"):
        state_mod._load_state_unlocked(run_dir, token)
        try:
            events = state_mod.read_events(run_dir)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PmError(f"could not read PM event log {run_dir / 'events.jsonl'}: {exc}") from exc
        if any(
            event.get("kind") == kind and event.get("note") == judgment_id and event.get("slice") == slice_id
            for event in events
        ):
            return
        state_mod._append_event_unlocked(run_dir, kind, slice_id=slice_id, note=judgment_id)

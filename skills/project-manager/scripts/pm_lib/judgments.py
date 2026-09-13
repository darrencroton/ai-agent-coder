"""Authenticated PM judgments of completed reviewer usefulness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PmError, slice_ops
from . import state as state_mod

_SKILLS = {"drift-audit", "code-review"}
_VERSION = 1


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

    common = {"schema_version", "slice", "skill", "reason", "supersedes"}
    status = data.get("status")
    if status is not None:
        if status != "unavailable":
            raise PmError("judgment status must be 'unavailable' when supplied")
        allowed = common | {"status", "review_ids"}
        unknown = set(data) - allowed
        if unknown:
            raise PmError(
                f"unavailable judgment has unsupported fields: {', '.join(sorted(unknown))}"
            )
        ids = _review_ids(data.get("review_ids"), "review_ids")
        if skill == "drift-audit" and len(ids) != 1:
            raise PmError(
                "an unavailable drift-audit judgment must reference exactly one review ID"
            )
        return {
            "schema_version": _VERSION,
            "slice": slice_id,
            "skill": skill,
            "reason": reason,
            "status": "unavailable",
            "review_ids": ids,
            "supersedes": supersedes,
        }

    if skill == "drift-audit":
        allowed = common | {"review_id", "score"}
        unknown = set(data) - allowed
        if unknown:
            raise PmError(
                f"drift judgment has unsupported fields: {', '.join(sorted(unknown))}"
            )
        score = data.get("score")
        if type(score) is not int or score not in (0, 1, 2):
            raise PmError("drift judgment score must be integer 0, 1, or 2")
        return {
            "schema_version": _VERSION,
            "slice": slice_id,
            "skill": skill,
            "reason": reason,
            "review_id": _nonempty_string(data.get("review_id"), "review_id"),
            "score": score,
            "supersedes": supersedes,
        }

    allowed = common | {"rank_groups"}
    unknown = set(data) - allowed
    if unknown:
        raise PmError(
            f"code-review judgment has unsupported fields: {', '.join(sorted(unknown))}"
        )
    groups = data.get("rank_groups")
    if not isinstance(groups, list) or not groups:
        raise PmError(
            "code-review rank_groups must be a non-empty list of non-empty tie groups"
        )
    parsed_groups = [_review_ids(group, "rank_groups") for group in groups]
    flat = [review_id for group in parsed_groups for review_id in group]
    if len(flat) != len(set(flat)):
        raise PmError("code-review rank_groups contains a review ID more than once")
    return {
        "schema_version": _VERSION,
        "slice": slice_id,
        "skill": skill,
        "reason": reason,
        "rank_groups": parsed_groups,
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
                raise PmError(
                    f"duplicate {skill} review ID in signed state: {review_id}"
                )
            records[review_id] = review
    return records


def _referenced_ids(judgment: dict[str, Any]) -> set[str]:
    if judgment.get("status") == "unavailable":
        return {
            item for item in judgment.get("review_ids") or [] if isinstance(item, str)
        }
    if judgment.get("skill") == "drift-audit":
        review_id = judgment.get("review_id")
        return {review_id} if isinstance(review_id, str) else set()
    return {
        item
        for group in judgment.get("rank_groups") or []
        for item in group
        if isinstance(item, str)
    }


def _active_judgments(entry: dict[str, Any]) -> list[dict[str, Any]]:
    records = [
        item for item in entry.get("review_judgments") or [] if isinstance(item, dict)
    ]
    superseded = {
        item.get("supersedes")
        for item in records
        if isinstance(item.get("supersedes"), str)
    }
    return [item for item in records if item.get("judgment_id") not in superseded]


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
        "rank_groups",
        "supersedes",
    }
    wanted = {
        key: value
        for key, value in parsed.items()
        if key in keys and key != "slice" and value is not None
    }
    return {key: record.get(key) for key in wanted} == wanted


def _next_judgment_id(records: list[dict[str, Any]]) -> str:
    highest = 0
    for record in records:
        value = record.get("judgment_id")
        if isinstance(value, str) and value.startswith("judgment-"):
            try:
                highest = max(highest, int(value.removeprefix("judgment-")))
            except ValueError:
                pass
    return f"judgment-{highest + 1}"


def _validate_reviews(
    parsed: dict[str, Any], entry: dict[str, Any]
) -> list[dict[str, Any]]:
    skill = parsed["skill"]
    ids = _referenced_ids(parsed)
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
        raise PmError(
            f"review {review.get('review_id')!r} lacks commission-time context; it remains historical/unjudged"
        )
    return (
        origin.get("index"),
        origin.get("kind"),
        origin.get("slice"),
        review.get("head"),
        review.get("before_head"),
        review.get("grants_seen"),
        json.dumps(context, sort_keys=True, separators=(",", ":")),
    )


def _validate_comparable_panel(
    parsed: dict[str, Any], reviews: list[dict[str, Any]]
) -> None:
    if parsed.get("status") == "unavailable" or len(reviews) <= 1:
        return
    keys = {_context_key(review) for review in reviews}
    if len(keys) != 1:
        raise PmError(
            "code-review panel mixes commission contexts; record it as unavailable instead"
        )


def record_judgment(run_dir: Path, token: str, data: dict[str, Any]) -> JudgmentOutcome:
    """Validate and persist a judgment against the latest locked run state."""
    parsed = _parse_input(data)
    with state_mod.locked_update(run_dir, token) as state:
        entry = slice_ops.slice_entry(state, parsed["slice"])
        if entry is None:
            raise PmError(f"{parsed['slice']} is not present in this run")
        records = [
            item
            for item in entry.get("review_judgments") or []
            if isinstance(item, dict)
        ]
        reviews = _validate_reviews(parsed, entry)
        _validate_comparable_panel(parsed, reviews)
        for record in records:
            if _input_matches(record, parsed):
                return JudgmentOutcome(
                    str(record.get("judgment_id")), parsed["slice"], created=False
                )

        active = _active_judgments(entry)
        supersedes = parsed.get("supersedes")
        target = next(
            (item for item in active if item.get("judgment_id") == supersedes), None
        )
        if supersedes is not None:
            if target is None:
                raise PmError(
                    f"supersedes must name an active judgment in this slice: {supersedes}"
                )
            if target.get("skill") != parsed["skill"]:
                raise PmError(
                    "supersedes must name a judgment for the same review skill"
                )

        new_ids = _referenced_ids(parsed)
        for earlier in active:
            if earlier is target:
                continue
            overlap = new_ids & _referenced_ids(earlier)
            if overlap:
                raise PmError(
                    "review IDs already have an active judgment: "
                    + ", ".join(sorted(overlap))
                )

        judgment = {
            key: value
            for key, value in parsed.items()
            if value is not None and key != "slice"
        }
        judgment["judgment_id"] = _next_judgment_id(records)
        judgment["at"] = state_mod.utc_now_iso()
        entry["review_judgments"] = [*records, judgment]
        return JudgmentOutcome(judgment["judgment_id"], parsed["slice"], created=True)


def unjudged_review_ids(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Stable successful commissions without active judgment coverage."""
    missing: list[tuple[str, str]] = []
    for entry in state.get("slices") or []:
        if not isinstance(entry, dict):
            continue
        covered = set().union(
            *(_referenced_ids(item) for item in _active_judgments(entry))
        )
        for review in entry.get("reviews") or []:
            review_id = review.get("review_id") if isinstance(review, dict) else None
            if isinstance(review_id, str) and review_id and review_id not in covered:
                missing.append((str(entry.get("id")), review_id))
    return missing


def historical_review_count(state: dict[str, Any]) -> int:
    """Completed records predating stable review IDs, kept as unjudged evidence."""
    return sum(
        1
        for entry in state.get("slices") or []
        for review in entry.get("reviews") or []
        if isinstance(review, dict) and not isinstance(review.get("review_id"), str)
    )


def publish_event(run_dir: Path, token: str, judgment_id: str, slice_id: str) -> None:
    """Publish one judgment event, atomically checking for an exact retry first."""
    with state_mod._advisory_lock(run_dir / ".lock"):
        state_mod._load_state_unlocked(run_dir, token)
        try:
            events = state_mod.read_events(run_dir)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PmError(
                f"could not read PM event log {run_dir / 'events.jsonl'}: {exc}"
            ) from exc
        if any(
            event.get("kind") == "review-judgment"
            and event.get("note") == judgment_id
            and event.get("slice") == slice_id
            for event in events
        ):
            return
        state_mod._append_event_unlocked(
            run_dir, "review-judgment", slice_id=slice_id, note=judgment_id
        )

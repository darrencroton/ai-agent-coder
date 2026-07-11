from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .models import McError, PlanSlice
from .constants import (
    BROAD_SURFACE_ENTRIES,
    COMPLETED_SLICE_STATUSES,
    DEPENDENCY_SURFACE_BASENAMES,
    DEPENDENCY_SURFACE_PREFIXES,
    DEPENDENCY_SURFACE_SUFFIXES,
    LICENSE_SURFACE_PREFIXES,
)
from .git_ops import normalize_authorized_entry


def plan_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def duplicate_slice_numbers(slices: list[PlanSlice]) -> list[int]:
    counts: dict[int, int] = {}
    for plan_slice in slices:
        counts[plan_slice.number] = counts.get(plan_slice.number, 0) + 1
    return sorted(number for number, count in counts.items() if count > 1)


def verify_plan_unchanged(state: dict[str, Any], plan_path: Path) -> None:
    """Fail closed if the plan file changed since the run was initialized.

    A "frozen" contract that is silently re-read on every slice is not frozen:
    editing the plan mid-run (renumbering slices, widening an authorized
    surface, flipping an approval flag) would otherwise be honored on the next
    slice. Runs created before digests were recorded have no baseline and are
    skipped for backward compatibility.
    """
    recorded = state.get("plan", {}).get("sha256")
    if not recorded:
        return
    current = plan_digest(plan_path)
    if current != recorded:
        raise McError(
            "plan file changed since this run was initialized "
            f"(recorded sha256 {recorded[:12]}…, current {current[:12]}…); "
            "start a new MC run for a revised plan"
        )


def parse_plan(path: Path) -> list[PlanSlice]:
    text = path.read_text(encoding="utf-8")
    headers = list(re.finditer(r"^## Slice\s+(?P<number>\d+):\s*(?P<title>.+?)\s*$", text, flags=re.MULTILINE))
    slices: list[PlanSlice] = []
    for index, header in enumerate(headers):
        start = header.end()
        end_candidates = []
        if index + 1 < len(headers):
            end_candidates.append(headers[index + 1].start())
        next_non_slice_heading = re.search(r"^## (?!Slice\s+\d+:).+$", text[start:], flags=re.MULTILINE)
        if next_non_slice_heading:
            end_candidates.append(start + next_non_slice_heading.start())
        end = min(end_candidates) if end_candidates else len(text)
        body = text[start:end].strip()
        sections = parse_sections(body)
        slices.append(
            PlanSlice(
                number=int(header.group("number")),
                title=header.group("title").strip(),
                body=body,
                sections=sections,
            )
        )
    return slices


def parse_sections(slice_body: str) -> dict[str, str]:
    matches = list(re.finditer(r"^### (?P<name>.+?)\s*$", slice_body, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(slice_body)
        sections[match.group("name").strip()] = slice_body[start:end].strip()
    return sections


def completed_slice_ids(state: dict[str, Any]) -> set[str]:
    complete: set[str] = set()
    for entry in state.get("slices", []):
        if str(entry.get("status", "")).lower() in COMPLETED_SLICE_STATUSES:
            slice_id = entry.get("slice_id")
            if slice_id:
                complete.add(str(slice_id))
    return complete


def next_slice(slices: list[PlanSlice], state: dict[str, Any]) -> PlanSlice | None:
    complete = completed_slice_ids(state)
    for plan_slice in sorted(slices, key=lambda item: item.number):
        if plan_slice.slice_id not in complete:
            return plan_slice
    return None


def eligibility(plan_slice: PlanSlice, approved_slice_ids: frozenset[str] | set[str] = frozenset()) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    missing = plan_slice.missing_sections
    if missing:
        reasons.append(f"missing required sections: {', '.join(missing)}")
    if not plan_slice.authorized_files:
        reasons.append("authorized surface has no files allowed to change")
    approval = plan_slice.approval_needed
    if approval is True and plan_slice.slice_id not in approved_slice_ids:
        reasons.append("slice is approval-needed (record operator approval with the approve command to run it)")
    elif approval is None:
        # A recorded approval clears only an explicit `yes`. An unclear flag is
        # a planning defect, not an approval question, so it stays blocking.
        reasons.append("approval-needed risk flag is missing or unclear")
    return not reasons, reasons


def plan_slice_by_id(slices: list[PlanSlice], slice_id: str) -> PlanSlice | None:
    for plan_slice in slices:
        if plan_slice.slice_id == slice_id:
            return plan_slice
    return None


def surface_lint(entry: str) -> str | None:
    normalized = normalize_authorized_entry(entry)
    if not normalized:
        return None
    if normalized in BROAD_SURFACE_ENTRIES:
        return f"entry {normalized!r} authorizes the entire repository, which defeats a frozen surface"
    basename = normalized.rstrip("/").rsplit("/", 1)[-1].lower()
    if (
        basename in DEPENDENCY_SURFACE_BASENAMES
        or basename.startswith(DEPENDENCY_SURFACE_PREFIXES)
        or basename.endswith(DEPENDENCY_SURFACE_SUFFIXES)
    ):
        return (
            f"entry {normalized!r} looks dependency-shaped; MC's dependency stop is heuristic "
            "(pane markers, prompt prohibitions), not diff inspection — approval-gate this slice "
            "or keep dependency manifests out of unattended surfaces"
        )
    if basename.startswith(LICENSE_SURFACE_PREFIXES):
        return (
            f"entry {normalized!r} looks license-shaped; license changes are a stop condition MC "
            "cannot mechanically inspect — approval-gate this slice or narrow the surface"
        )
    return None


def plan_check_report(path: Path) -> dict[str, Any]:
    """Whole-plan pre-run sanity check.

    Validates every slice contract up front so a plan defect surfaces before
    any harness launches, instead of mid-run at whichever slice carries it.
    Errors are conditions that would block a slice or hide work (missing
    sections, empty authorized surface, unclear approval flags, duplicate
    numbers): init fails closed on them. Warnings are lint for conditions MC
    cannot mechanically guard against mid-run (heuristic stop surfaces,
    mode-dependent plan features): the operator should resolve them or
    consciously accept them before starting.
    """
    text = path.read_text(encoding="utf-8")
    slices = parse_plan(path)
    errors: list[str] = []
    warnings: list[str] = []
    approval_gated: list[str] = []

    if not slices:
        errors.append("plan contains no slices (no '## Slice <N>: <name>' headings found)")
    duplicates = duplicate_slice_numbers(slices)
    if duplicates:
        errors.append(
            "plan has duplicate slice numbers: "
            + ", ".join(str(number) for number in duplicates)
            + " (each slice number must be unique so completion tracking cannot silently skip work)"
        )

    for plan_slice in sorted(slices, key=lambda item: item.number):
        prefix = f"{plan_slice.slice_id} ({plan_slice.title})"
        missing = plan_slice.missing_sections
        if missing:
            errors.append(f"{prefix}: missing required sections: {', '.join(missing)}")
        if not plan_slice.authorized_files:
            errors.append(f"{prefix}: authorized surface has no files allowed to change")
        approval = plan_slice.approval_needed
        if approval is None:
            errors.append(
                f"{prefix}: 'Approval needed before implementation:' must be exactly 'yes' or 'no' "
                "(an unclear value stops the run and cannot be approved away at runtime)"
            )
        elif approval:
            approval_gated.append(plan_slice.slice_id)
        for raw_entry in plan_slice.authorized_files:
            lint = surface_lint(raw_entry)
            if lint:
                warnings.append(f"{prefix}: {lint}")

    if re.search(r"^#{2,3}\s*Slice Batches\b", text, flags=re.MULTILINE) or re.search(
        r"^\s*-\s*Batch\s+\S+\s*:\s*Slices?\b", text, flags=re.MULTILINE
    ):
        warnings.append(
            "plan defines slice batches: batches apply to Mode A/B sessions only — MC runs atomic "
            "slices in plan order and ignores batch groupings"
        )

    return {
        "plan_path": str(path),
        "slice_count": len(slices),
        "errors": errors,
        "warnings": warnings,
        "approval_gated": approval_gated,
    }

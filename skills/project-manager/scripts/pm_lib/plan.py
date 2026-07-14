from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .models import PmError, PlanSlice
from .constants import (
    BROAD_SURFACE_ENTRIES,
    COMPLETED_SLICE_STATUSES,
    DEPENDENCY_SURFACE_BASENAMES,
    DEPENDENCY_SURFACE_PREFIXES,
    DEPENDENCY_SURFACE_SUFFIXES,
    LICENSE_SURFACE_PREFIXES,
    TOP_LEVEL_ONLY_SURFACE_ENTRIES,
)
from .git_ops import normalize_authorized_entry


SLICE_HEADING_RE = re.compile(r"^## Slice\s+(?P<number>\d+):\s*(?P<title>.+?)\s*$", flags=re.MULTILINE)
SLICE_LIKE_HEADING_RE = re.compile(
    r"^[ ]{0,3}#{1,6}\s+Slice(?:\s|:|\d|$)[^\n]*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
SLICE_BATCH_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}\s+Slice Batches\b", flags=re.IGNORECASE | re.MULTILINE)
BATCH_BULLET_RE = re.compile(r"^\s*-\s*Batch\s+\S+\s*:\s*Slices?\b", flags=re.MULTILINE)


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
    slice. Schema-v3 runs must always carry this baseline.
    """
    recorded = state.get("plan", {}).get("sha256")
    if not recorded:
        raise PmError("run state has no frozen plan digest; initialize a new PM run")
    current = plan_digest(plan_path)
    if current != recorded:
        raise PmError(
            "plan file changed since this run was initialized "
            f"(recorded sha256 {recorded[:12]}…, current {current[:12]}…); "
            "start a new PM run for a revised plan"
        )


def parse_plan(path: Path) -> list[PlanSlice]:
    text = path.read_text(encoding="utf-8")
    headers = list(SLICE_HEADING_RE.finditer(text))
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
    authorized_files = plan_slice.authorized_files
    if not authorized_files:
        reasons.append("authorized surface has no files allowed to change")
    else:
        invalid_entries = [error for entry in authorized_files if (error := authorized_entry_error(entry))]
        if invalid_entries:
            reasons.append("invalid authorized surface: " + "; ".join(invalid_entries))
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


def authorized_entry_error(entry: str) -> str | None:
    normalized = normalize_authorized_entry(entry)
    if not normalized:
        return f"entry {entry!r} is empty after normalization"
    if normalized.startswith("/"):
        return f"entry {normalized!r} is absolute; authorized paths must be repository-relative"
    if normalized.startswith("./"):
        return f"entry {normalized!r} has a redundant './' prefix that matches no git path"
    if "\\" in normalized:
        return f"entry {normalized!r} uses a backslash; authorized git paths must use '/' separators"
    if "//" in normalized:
        return f"entry {normalized!r} contains an empty path segment"
    # A backtick-wrapped entry has already had its path extracted, so internal
    # whitespace there is deliberate. Unwrapped whitespace is almost always a
    # trailing annotation ("README.md (new file)") that would silently match
    # no changed path at runtime.
    if any(ch.isspace() for ch in normalized) and not re.match(r"`[^`]+`", entry.strip()):
        return (
            f"entry {normalized!r} contains unwrapped whitespace, which matches no changed path; "
            "backtick-wrap the path itself (`path/to/file` (note)) so annotations are kept out of the match"
        )
    parts = normalized.rstrip("/").split("/")
    if any(part in {".", ".."} for part in parts):
        return f"entry {normalized!r} contains a '.' or '..' path segment that matches no authorized git path"
    return None


def directory_surface_lint(entry: str, repo: Path) -> str | None:
    normalized = normalize_authorized_entry(entry)
    if not normalized or normalized.endswith("/"):
        return None
    if any(marker in normalized for marker in ("*", "?", "[")):
        return None
    if (repo / normalized).is_dir():
        return (
            f"entry {normalized!r} names an existing directory; a plain path matches only an identical "
            f"changed path, so it authorizes nothing beneath the directory — write {normalized + '/'!r} "
            "to authorize the subtree"
        )
    return None


def mask_fenced_blocks(text: str) -> tuple[str, bool]:
    """Blank out fenced code blocks, preserving offsets and line structure.

    Returns the masked text and whether a fence was left unclosed. Heading
    scans compare against the masked text so a '## Slice ...' example inside a
    fence is reported as ambiguous instead of being read as (or hidden from)
    a machine-consumed heading.
    """
    masked_lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        marker: tuple[str, int, str] | None = None
        if indent <= 3 and stripped[:3] in ("```", "~~~"):
            char = stripped[0]
            run = len(stripped) - len(stripped.lstrip(char))
            marker = (char, run, stripped[run:].strip())
        if fence is None:
            if marker:
                fence = (marker[0], marker[1])
                masked_lines.append(" " * len(line))
            else:
                masked_lines.append(line)
        else:
            masked_lines.append(" " * len(line))
            if marker and marker[0] == fence[0] and marker[1] >= fence[1] and not marker[2]:
                fence = None
    return "\n".join(masked_lines), fence is not None


def surface_lint(entry: str) -> str | None:
    normalized = normalize_authorized_entry(entry)
    if not normalized:
        return None
    if normalized in BROAD_SURFACE_ENTRIES:
        return f"entry {normalized!r} authorizes the entire repository, which defeats a frozen surface"
    if normalized in TOP_LEVEL_ONLY_SURFACE_ENTRIES:
        return f"entry {normalized!r} matches top-level paths only; use '**/*' if recursive authorization is intended"
    basename = normalized.rstrip("/").rsplit("/", 1)[-1].lower()
    if (
        basename in DEPENDENCY_SURFACE_BASENAMES
        or basename.startswith(DEPENDENCY_SURFACE_PREFIXES)
        or basename.endswith(DEPENDENCY_SURFACE_SUFFIXES)
    ):
        return (
            f"entry {normalized!r} looks dependency-shaped; PM's dependency stop is heuristic "
            "(pane markers, prompt prohibitions), not diff inspection — approval-gate this slice "
            "or keep dependency manifests out of unattended surfaces"
        )
    if basename.startswith(LICENSE_SURFACE_PREFIXES):
        return (
            f"entry {normalized!r} looks license-shaped; license changes are a stop condition PM "
            "cannot mechanically inspect — approval-gate this slice or narrow the surface"
        )
    return None


def plan_check_report(path: Path, repo: Path | None = None) -> dict[str, Any]:
    """Whole-plan pre-run sanity check.

    Validates every slice contract up front so a plan defect surfaces before
    any harness launches, instead of mid-run at whichever slice carries it.
    Errors are conditions that would block a slice or hide work (missing
    sections, empty authorized surface, unclear approval flags, duplicate
    numbers, fenced or malformed slice headings): init fails closed on them.
    Warnings are lint for conditions PM cannot mechanically guard against
    mid-run (heuristic stop surfaces, mode-dependent plan features): the
    operator should resolve them or consciously accept them before starting.
    When ``repo`` is given, authorized entries are additionally linted against
    the worktree (e.g. a plain entry that names an existing directory).
    """
    text = path.read_text(encoding="utf-8")
    slices = parse_plan(path)
    errors: list[str] = []
    warnings: list[str] = []
    approval_gated: list[str] = []

    masked_text, unclosed_fence = mask_fenced_blocks(text)
    if unclosed_fence:
        errors.append(
            "plan contains an unclosed code fence, which makes heading detection ambiguous; "
            "close the fenced block before running"
        )
    for match in SLICE_LIKE_HEADING_RE.finditer(text):
        heading = match.group(0)
        if SLICE_BATCH_HEADING_RE.match(heading):
            continue
        if not masked_text[match.start():match.end()].strip():
            errors.append(
                f"slice-like heading {heading!r} sits inside a fenced code block; PM's parser reads "
                "headings literally, so fenced examples can silently change the slice set — move or reword it"
            )
            continue
        if not SLICE_HEADING_RE.fullmatch(heading):
            errors.append(
                f"malformed slice heading {heading!r}; slice headings must be exactly "
                "'## Slice <N>: <name>' so planned work cannot be silently skipped"
            )
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
        authorized_files = plan_slice.authorized_files
        if not authorized_files:
            errors.append(f"{prefix}: authorized surface has no files allowed to change")
        else:
            for raw_entry in authorized_files:
                entry_error = authorized_entry_error(raw_entry)
                if entry_error:
                    errors.append(f"{prefix}: invalid authorized surface: {entry_error}")
                    continue
                lint = surface_lint(raw_entry)
                if lint:
                    warnings.append(f"{prefix}: {lint}")
                if repo is not None:
                    dir_lint = directory_surface_lint(raw_entry, repo)
                    if dir_lint:
                        warnings.append(f"{prefix}: {dir_lint}")
        approval = plan_slice.approval_needed
        if approval is None:
            errors.append(
                f"{prefix}: 'Approval needed before implementation:' must be exactly 'yes' or 'no' "
                "(an unclear value stops the run and cannot be approved away at runtime)"
            )
        elif approval:
            approval_gated.append(plan_slice.slice_id)
    if SLICE_BATCH_HEADING_RE.search(masked_text) or BATCH_BULLET_RE.search(masked_text):
        warnings.append(
            "plan defines slice batches: batches bind in Mode A sessions only — PM (Mode B) runs "
            "atomic slices in plan order and ignores batch groupings"
        )

    return {
        "plan_path": str(path),
        "slice_count": len(slices),
        "errors": errors,
        "warnings": warnings,
        "approval_gated": approval_gated,
    }

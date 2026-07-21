"""Plan parsing, check-plan, eligibility, and mechanical risk derivation.

The parser preserves the proven plan-format behaviour from the predecessor
while keeping its implementation independent.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PmError
from .git_ops import normalize_authorized_entry

REQUIRED_SECTIONS: tuple[str, ...] = (
    "Intended Change",
    "Acceptance Criteria",
    "Authorized Surface",
    "Explicit Non-Goals",
    "Risk Flags",
    "Validation Plan",
    "Rollback Path",
)

# Slice statuses (lite-1) that mark a slice as no longer eligible to run.
_COMPLETED_SLICE_STATUSES = {"accepted", "attested"}

# check-plan lint vocabulary. PM's dependency/license/side-effect stop
# conditions are heuristic (pane markers, prompt prohibitions), not diff
# inspection: a silent dependency edit inside an authorized surface would
# pass the file-authorization floor. The compensating control is plan-level
# — keep these files out of unattended authorized surfaces or approval-gate
# the slice — so check-plan warns when an authorized entry looks dependency-
# or license-shaped. Basenames are matched case-insensitively.
_DEPENDENCY_SURFACE_BASENAMES = {
    "package.json",
    "pipfile",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "cargo.toml",
    "go.mod",
    "go.sum",
    "gemfile",
    "composer.json",
    "environment.yml",
    "environment.yaml",
    "flake.nix",
}
_DEPENDENCY_SURFACE_PREFIXES = ("requirements",)
_DEPENDENCY_SURFACE_SUFFIXES = (".lock", "-lock.json", "-lock.yaml", "-lock.yml")
_LICENSE_SURFACE_PREFIXES = ("license", "copying", "notice", "patents")
# These recursive globs match every repository-relative path under the
# segment-aware authorization matcher.
_BROAD_SURFACE_ENTRIES = {"**", "**/*"}
_TOP_LEVEL_ONLY_SURFACE_ENTRIES = {"*"}

_SLICE_HEADING_RE = re.compile(r"^## Slice\s+(?P<number>\d+):\s*(?P<title>.+?)\s*$", flags=re.MULTILINE)
_SLICE_LIKE_HEADING_RE = re.compile(
    r"^[ ]{0,3}#{1,6}\s+Slice(?:\s|:|\d|$)[^\n]*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_SLICE_BATCH_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}\s+Slice Batches\b", flags=re.IGNORECASE | re.MULTILINE)
_BATCH_BULLET_RE = re.compile(r"^\s*-\s*Batch\s+\S+\s*:\s*Slices?\b", flags=re.MULTILINE)


def _bullet_values(text: str) -> list[str]:
    values: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            value = stripped[2:].strip()
            if value and value.lower() not in ("none", "none."):
                values.append(value)
    return values


@dataclass(frozen=True)
class PlanSlice:
    number: int
    title: str
    body: str
    sections: dict[str, str]

    @property
    def slice_id(self) -> str:
        return f"Slice {self.number}"

    @property
    def missing_sections(self) -> list[str]:
        return [section for section in REQUIRED_SECTIONS if not self.sections.get(section, "").strip()]

    @property
    def authorized_files(self) -> list[str]:
        section = self.sections.get("Authorized Surface", "")
        # Capture the lines under "Files allowed to change:", stopping at the
        # next column-0 bullet (a sibling such as "Functions/... allowed to
        # change: none.", or any stray top-level bullet). Sub-bullets (the
        # actual file entries) are indented and so pass the negative
        # lookahead; a stray sibling bullet at column 0 stops the capture
        # before it can be mistaken for an authorized file.
        match = re.search(
            r"Files allowed to change:[^\n]*\n(?P<body>(?:(?!-)[^\n]*\n?)*)",
            section,
        )
        if not match:
            return []
        return _bullet_values(match.group("body"))

    @property
    def approval_needed(self) -> bool | None:
        section = self.sections.get("Risk Flags", "")
        match = re.search(
            r"Approval needed before implementation:\s*(?P<value>[^\n]+)", section, flags=re.IGNORECASE
        )
        if not match:
            return None
        # Match the answer exactly. A prefix test (startswith("no")) fails
        # open: "not yet decided", "none", "not required — ask first" all
        # begin with "no" and would silently be treated as "no approval
        # required". Anything that is not an unambiguous yes/no returns
        # None, which callers treat as blocking, never as approvable.
        value = match.group("value").strip().lower().rstrip(".")
        if value == "no":
            return False
        if value == "yes":
            return True
        return None

    @property
    def independent_audit_required(self) -> bool:
        section = self.sections.get("Risk Flags", "")
        match = re.search(r"Independent audit required:\s*(?P<value>[^\n]+)", section, flags=re.IGNORECASE)
        if not match:
            return False
        # Unlike approval_needed, this fails closed to *off*: absent, blank,
        # or anything not an exact "yes" leaves the opt-in gate unarmed.
        return match.group("value").strip().lower().rstrip(".") == "yes"

    @property
    def risky_surfaces_clear(self) -> bool:
        section = self.sections.get("Risk Flags", "")
        match = re.search(r"Risky surfaces touched:\s*(?P<value>[^\n]+)", section, flags=re.IGNORECASE)
        if not match:
            return False
        return match.group("value").strip().lower().rstrip(".") == "none"

    @property
    def plan_risk(self) -> str:
        """Mechanical, immutable risk derivation (target-design §4).

        Elevated iff approval is explicitly required, independent audit is
        required, or any risky surface is declared touched (including when
        the line is missing or unclear — silence is not "none"). This
        derivation happens once, at parse time; later stages record it in
        state and may only raise it, never lower it.
        """
        if self.approval_needed is True or self.independent_audit_required or not self.risky_surfaces_clear:
            return "elevated"
        return "standard"


def plan_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def duplicate_slice_numbers(slices: list[PlanSlice]) -> list[int]:
    counts: dict[int, int] = {}
    for plan_slice in slices:
        counts[plan_slice.number] = counts.get(plan_slice.number, 0) + 1
    return sorted(number for number, count in counts.items() if count > 1)


def verify_plan_unchanged(state: dict[str, Any], plan_path: Path) -> None:
    """Fail closed if the plan file changed since the run was initialized.

    A "frozen" contract that is silently re-read on every slice is not
    frozen: editing the plan mid-run (renumbering slices, widening a
    surface, flipping an approval flag) would otherwise be honored on the
    next slice.
    """
    recorded = state.get("plan", {}).get("sha256")
    if not recorded:
        raise PmError("run state has no frozen plan digest; start a new run")
    current = plan_digest(plan_path)
    if current != recorded:
        raise PmError(
            "plan file changed since this run was initialized "
            f"(recorded sha256 {recorded[:12]}…, current {current[:12]}…); "
            "start a new run for a revised plan"
        )


def parse_plan(path: Path) -> list[PlanSlice]:
    text = path.read_text(encoding="utf-8")
    headers = list(_SLICE_HEADING_RE.finditer(text))
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


def parse_sections(body: str) -> dict[str, str]:
    matches = list(re.finditer(r"^### (?P<name>.+?)\s*$", body, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group("name").strip()] = body[start:end].strip()
    return sections


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
    # A backtick-wrapped entry has already had its path extracted, so
    # internal whitespace there is deliberate. Unwrapped whitespace is
    # almost always a trailing annotation ("README.md (new file)") that
    # would silently match no changed path at runtime.
    if any(ch.isspace() for ch in normalized) and not re.match(r"`[^`]+`", entry.strip()):
        return (
            f"entry {normalized!r} contains unwrapped whitespace, which matches no changed path; "
            "backtick-wrap the path itself (`path/to/file` (note)) so annotations are kept out of the match"
        )
    parts = normalized.rstrip("/").split("/")
    if any(part in {".", ".."} for part in parts):
        return f"entry {normalized!r} contains a '.' or '..' path segment that matches no authorized git path"
    return None


def surface_lint(entry: str) -> str | None:
    normalized = normalize_authorized_entry(entry)
    if not normalized:
        return None
    if normalized in _BROAD_SURFACE_ENTRIES:
        return f"entry {normalized!r} authorizes the entire repository, which defeats a frozen surface"
    if normalized in _TOP_LEVEL_ONLY_SURFACE_ENTRIES:
        return f"entry {normalized!r} matches top-level paths only; use '**/*' if recursive authorization is intended"
    basename = normalized.rstrip("/").rsplit("/", 1)[-1].lower()
    if (
        basename in _DEPENDENCY_SURFACE_BASENAMES
        or basename.startswith(_DEPENDENCY_SURFACE_PREFIXES)
        or basename.endswith(_DEPENDENCY_SURFACE_SUFFIXES)
    ):
        return (
            f"entry {normalized!r} looks dependency-shaped; PM's dependency stop is heuristic "
            "(pane markers, prompt prohibitions), not diff inspection — approval-gate this slice "
            "or keep dependency manifests out of unattended surfaces"
        )
    if basename.startswith(_LICENSE_SURFACE_PREFIXES):
        return (
            f"entry {normalized!r} looks license-shaped; license changes are a stop condition PM "
            "cannot mechanically inspect — approval-gate this slice or narrow the surface"
        )
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
    scans compare against the masked text so a '## Slice ...' example inside
    a fence is reported as ambiguous instead of being read as (or hidden
    from) a machine-consumed heading.
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


def plan_check_report(path: Path, repo: Path | None = None) -> dict[str, Any]:
    """Whole-plan pre-run sanity check.

    Validates every slice contract up front so a plan defect surfaces before
    any harness launches, instead of mid-run at whichever slice carries it.
    Errors are conditions that would block a slice or hide work (missing
    sections, empty authorized surface, unclear approval flags, duplicate
    numbers, fenced or malformed slice headings). Warnings are lint for
    conditions PM cannot mechanically guard against mid-run (heuristic stop
    surfaces, Mode-A-only plan features). When `repo` is given, authorized
    entries are additionally linted against the worktree.
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
    for match in _SLICE_LIKE_HEADING_RE.finditer(text):
        heading = match.group(0)
        if _SLICE_BATCH_HEADING_RE.match(heading):
            continue
        if not masked_text[match.start() : match.end()].strip():
            errors.append(
                f"slice-like heading {heading!r} sits inside a fenced code block; PM's parser reads "
                "headings literally, so fenced examples can silently change the slice set — move or reword it"
            )
            continue
        if not _SLICE_HEADING_RE.fullmatch(heading):
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

    if _SLICE_BATCH_HEADING_RE.search(masked_text) or _BATCH_BULLET_RE.search(masked_text):
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


def next_slice(slices: list[PlanSlice], state: dict[str, Any]) -> PlanSlice | None:
    """First slice, in plan order, not already accepted or attested in state."""
    completed: set[str] = set()
    for entry in state.get("slices", []):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if str(entry.get("status", "")).lower() in _COMPLETED_SLICE_STATUSES:
            completed.add(str(entry["id"]))
    for plan_slice in sorted(slices, key=lambda item: item.number):
        if plan_slice.slice_id not in completed:
            return plan_slice
    return None


def eligibility(
    plan_slice: PlanSlice, approved_slice_ids: frozenset[str] | set[str] = frozenset()
) -> tuple[bool, list[str]]:
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
        # A recorded approval clears only an explicit `yes`. An unclear flag
        # is a planning defect, not an approval question, so it stays
        # blocking regardless of any recorded approval.
        reasons.append("approval-needed risk flag is missing or unclear")
    return not reasons, reasons


def plan_slice_by_id(slices: list[PlanSlice], slice_id: str) -> PlanSlice | None:
    for plan_slice in slices:
        if plan_slice.slice_id == slice_id:
            return plan_slice
    return None

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .constants import REQUIRED_SECTIONS


class McError(Exception):
    """User-facing MC error."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _bullet_values(text: str) -> list[str]:
    values: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            value = stripped[2:].strip()
            if value and value.lower() != "none." and value.lower() != "none":
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
        # Capture the lines under the "Files allowed to change:" line, stopping
        # at the next column-0 bullet (a sibling such as "Functions.../Tests...",
        # or any stray top-level bullet). Blank and indented lines are kept and
        # then filtered by _bullet_values, so a stray sibling bullet cannot be
        # mistaken for an authorized file while lenient spacing still parses.
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
        match = re.search(r"Approval needed before implementation:\s*(?P<value>[^\n]+)", section, flags=re.IGNORECASE)
        if not match:
            return None
        # Match the answer exactly. A prefix test (startswith("no")) fails open:
        # "not yet decided", "none", and "not required — ask first" all begin
        # with "no" and would silently be treated as "no approval required",
        # running unattended a slice a human explicitly left undecided. Anything
        # that is not an unambiguous yes/no returns None, which eligibility()
        # treats as blocking.
        value = match.group("value").strip().lower().rstrip(".")
        if value == "no":
            return False
        if value == "yes":
            return True
        return None

    @property
    def independent_audit_required(self) -> bool:
        """Whether this slice opts in to MC's mechanical independent-audit gate.

        Independence (delegating drift-audit and code-review to a separate
        model) is a degradable *preference* expressed in the orchestrator
        prompt, not a mechanical requirement: a slice audited locally by a
        single model is a valid accepted outcome. A plan may nonetheless opt a
        high-stakes slice into mechanical enforcement by adding
        ``Independent audit required: yes`` to its Risk Flags, which re-arms
        MC's worker-launch verification as a blocking gate for that slice.

        Unlike ``approval_needed`` (whose absence blocks), this fails *closed to
        off*: absent, blank, or anything that is not an exact ``yes`` means the
        opt-in gate is not armed, matching the default degradable-preference
        posture. Only an unambiguous ``yes`` arms it.
        """
        section = self.sections.get("Risk Flags", "")
        match = re.search(r"Independent audit required:\s*(?P<value>[^\n]+)", section, flags=re.IGNORECASE)
        if not match:
            return False
        return match.group("value").strip().lower().rstrip(".") == "yes"


@dataclass(frozen=True)
class GateDecision:
    status: str
    reason: str
    result: dict[str, Any] | None = None
    actual_changed_files: tuple[str, ...] = ()
    # Coarse, stable failure-category label (e.g. "validation", "drift",
    # "integrity-head"). Drives the repair circuit breaker and repair-prompt
    # stanza selection; empty for a pass and for statuses with no category.
    signature: str = ""

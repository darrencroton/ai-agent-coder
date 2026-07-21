"""Template rendering for the Developer and Reviewer prompts.

The only prompt-shaping logic in the package lives here. Every other module
that needs rendered prompt text calls into this one — no inline prompt
fragments in `slice_ops.py`, `review.py`, or anywhere else. Templates are not
owned by this module: they live in ``references/developer-prompt.md`` and
``references/reviewer-prompt.md`` as single fenced ```md blocks, so wording
can be edited without touching code.

`compile_skill_bundle` independently implements the transitive skill-bundle
embedding behaviour also exercised by
``skills/orchestrator/scripts/delegate_contract.py``
(`compile_skill_bundle` / `compose_delegate_command`) — this module shares
no code with it and never imports from ``skills/orchestrator/``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import PmError
from .plan import PlanSlice

# skills/project-manager/scripts/pm_lib/prompts.py -> parents[2] is
# skills/project-manager/, so references/ sits alongside scripts/.
_DEFAULT_REFERENCE_PATH = Path(__file__).resolve().parents[2] / "references" / "developer-prompt.md"
_DEFAULT_REVIEWER_REFERENCE_PATH = Path(__file__).resolve().parents[2] / "references" / "reviewer-prompt.md"
# ... -> parents[3] is skills/, the root every skill package lives under.
_DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parents[3]

_MD_FENCE_RE = re.compile(r"```md\n(?P<body>.*?)\n```", re.DOTALL)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((?P<target>[^)]+)\)")
_HEADING_RE = re.compile(r"^#{1,6} .*$", re.MULTILINE)

_STEER_MESSAGE_HEADING = "## Steer Message Template"
_LAUNCH_POINTER_HEADING = "## Launch Pointer"


def _section_text(text: str, heading: str | None) -> str:
    """The slice of `text` scoped to `heading`.

    `heading` is None for the file's leading section (from the top through
    the line just before the second heading, or the whole file when there
    is at most one heading) — this is what keeps `load_template`'s existing
    single-block callers (the developer and reviewer prompt templates)
    working unchanged as more sections are added to the same file. A named
    `heading` (matched verbatim, e.g. "## Steer Message Template") returns
    the text between that heading line and the next heading or end of file;
    PmError if no heading matches exactly.
    """
    headings = list(_HEADING_RE.finditer(text))
    if heading is None:
        if len(headings) <= 1:
            return text
        return text[: headings[1].start()]
    for index, match in enumerate(headings):
        if match.group(0).strip() == heading:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[match.end() : end]
    raise PmError(f"heading {heading!r} not found")


def load_template(reference_path: Path | None = None, *, heading: str | None = None) -> str:
    """Extract the single ```md fenced block from `reference_path`, scoped
    to `heading` (the file's leading section when `heading` is None).

    PmError, naming the file, when the block is absent or there is more
    than one within that scope — a reference file's section is expected to
    carry exactly one rendered prompt, not a menu of alternatives.
    """
    path = reference_path or _DEFAULT_REFERENCE_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PmError(f"developer prompt template could not be read: {path} ({exc})") from exc

    section = _section_text(text, heading)
    matches = _MD_FENCE_RE.findall(section)
    scope = f"heading {heading!r} of" if heading else "developer prompt template"
    if not matches:
        raise PmError(f"no fenced ```md block found in {scope}: {path}")
    if len(matches) > 1:
        raise PmError(f"more than one fenced ```md block found in {scope}: {path}")
    return matches[0]


def render_developer_prompt(
    plan_slice: PlanSlice,
    *,
    plan_path: Path,
    artifact_dir: Path,
    notes_path: Path,
    result_path: Path,
    reference_path: Path | None = None,
) -> str:
    """Render the Developer prompt for one slice launch.

    Section texts (`intended_change`, `acceptance_criteria`, …) come from
    `plan_slice.sections` verbatim, with only trailing whitespace stripped —
    the plan author's wording and formatting is not otherwise touched.
    """
    template = load_template(reference_path)
    sections = plan_slice.sections
    fields: dict[str, Any] = {
        "plan_path": str(plan_path),
        "slice_id": plan_slice.slice_id,
        "slice_title": plan_slice.title,
        "artifact_dir": str(artifact_dir),
        "notes_path": str(notes_path),
        "result_path": str(result_path),
        "intended_change": sections.get("Intended Change", "").rstrip(),
        "acceptance_criteria": sections.get("Acceptance Criteria", "").rstrip(),
        "authorized_surface": sections.get("Authorized Surface", "").rstrip(),
        "explicit_non_goals": sections.get("Explicit Non-Goals", "").rstrip(),
        "risk_flags": sections.get("Risk Flags", "").rstrip(),
        "validation_plan": sections.get("Validation Plan", "").rstrip(),
        "rollback_path": sections.get("Rollback Path", "").rstrip(),
    }
    path_for_errors = reference_path or _DEFAULT_REFERENCE_PATH
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise PmError(
            f"developer prompt template {path_for_errors} has an unresolved or stray brace "
            f"({exc}); escape literal '{{' / '}}' as '{{{{' / '}}}}' per the template's editing note"
        ) from exc


def render_launch_pointer(prompt_path: Path, *, reference_path: Path | None = None) -> str:
    """Render the one-line launch pointer that tells a fresh Developer
    session to read its full contract from `prompt_path`.

    The complete frozen contract is written to `prompt_path` in the slice
    artifact directory; only this short pointer is injected into the
    session, so delivery never depends on a harness TUI accepting a
    multi-KB paste (PM Test 20, Finding 1). Sourced from the "## Launch
    Pointer" section of `references/developer-prompt.md` so the wording
    lives in exactly one place. The result must be a single line — a
    newline would be rejected by `sessions.send_prompt`.
    """
    path = reference_path or _DEFAULT_REFERENCE_PATH
    template = load_template(path, heading=_LAUNCH_POINTER_HEADING)
    try:
        return template.format(prompt_path=str(prompt_path))
    except (KeyError, IndexError, ValueError) as exc:
        raise PmError(
            f"launch pointer template {path} has an unresolved or stray brace "
            f"({exc}); escape literal '{{' / '}}' as '{{{{' / '}}}}' per the template's editing note"
        ) from exc


def render_steer_message(correction: str, *, reference_path: Path | None = None) -> str:
    """Render a `finalize --steer` correction for direct live-session
    injection without an artifact file. Sourced from the "## Steer Message
    Template" section of `references/developer-prompt.md` so the fixed wrapper
    wording lives in exactly one place, not inline here.
    """
    path = reference_path or _DEFAULT_REFERENCE_PATH
    template = load_template(path, heading=_STEER_MESSAGE_HEADING)
    try:
        return template.format(correction=correction)
    except (KeyError, IndexError, ValueError) as exc:
        raise PmError(
            f"steer message template {path} has an unresolved or stray brace "
            f"({exc}); escape literal '{{' / '}}' as '{{{{' / '}}}}' per the template's editing note"
        ) from exc


# --- Transitive skill-bundle embedding --------------------------------------


def compile_skill_bundle(skill_name: str, *, skills_root: Path | None = None) -> str:
    """Embed `skills/<skill_name>/SKILL.md` plus every locally-linked
    Markdown resource it transitively references, breadth-first.

    A `[text](target)` link is followed when: it is not a bare URL
    (contains no ``://``), it is not a pure in-page anchor (empty once its
    ``#fragment`` is stripped), and the resolved path's suffix is `.md`.
    Every followed file must resolve inside the skill's own directory — a
    link that escapes it (e.g. `../outside.md`) raises `PmError` naming the
    escaping path, checked before existence so an escape is never masked as
    a missing-file error. A missing linked file also raises `PmError`.
    Files are deduplicated (a diamond of links embeds its target once).

    Each file is rendered as a delimited block:
    ``BEGIN EMBEDDED SKILL FILE: <path>`` / content / ``END EMBEDDED SKILL
    FILE: <path>``, joined with a blank line between files.
    """
    root = skills_root or _DEFAULT_SKILLS_ROOT
    skill_dir = (root / skill_name).resolve()
    entry = (skill_dir / "SKILL.md").resolve()
    if not entry.is_file():
        raise PmError(f"skill {skill_name!r} has no SKILL.md at {entry}")

    pending: list[Path] = [entry]
    seen: set[Path] = set()
    rendered: list[str] = []
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            path.relative_to(skill_dir)
        except ValueError:
            raise PmError(
                f"skill resource referenced by {skill_name!r} escapes its own skill directory "
                f"{skill_dir}: {path}"
            ) from None
        if not path.is_file():
            raise PmError(f"skill resource referenced by {skill_name!r} is missing: {path}")
        text = path.read_text(encoding="utf-8")
        rendered.append(f"BEGIN EMBEDDED SKILL FILE: {path}\n{text.rstrip()}\nEND EMBEDDED SKILL FILE: {path}")
        for match in _MARKDOWN_LINK_RE.finditer(text):
            target = match.group("target").split("#", 1)[0].strip()
            if not target or "://" in target:
                continue
            candidate = (path.parent / target).resolve()
            if candidate.suffix.lower() != ".md":
                continue
            pending.append(candidate)
    return "\n\n".join(rendered)


def render_reviewer_prompt(
    *,
    skill_name: str,
    repo: str,
    slice_id: str,
    slice_title: str,
    before_head: str | None,
    reviewed_head: str,
    diff_path: str,
    changed_files: list[str],
    intended_change: str,
    acceptance_criteria: str,
    authorized_surface: str,
    explicit_non_goals: str,
    risk_flags: str,
    reference_path: Path | None = None,
    skills_root: Path | None = None,
) -> str:
    """Render the Reviewer prompt for one commissioned review.

    Embeds the named skill's complete transitive bundle
    (`compile_skill_bundle`) so the review contract survives a harness with
    no skill loader of its own.
    """
    template = load_template(reference_path or _DEFAULT_REVIEWER_REFERENCE_PATH)
    skill_bundle = compile_skill_bundle(skill_name, skills_root=skills_root)
    changed_files_text = "\n".join(f"  - {path}" for path in changed_files) if changed_files else "  - (none)"
    fields: dict[str, Any] = {
        "skill_name": skill_name,
        "repo": repo,
        "slice_id": slice_id,
        "slice_title": slice_title,
        "before_head": before_head or "(none — first commit of the run)",
        "reviewed_head": reviewed_head,
        "diff_path": diff_path,
        "changed_files": changed_files_text,
        "intended_change": intended_change,
        "acceptance_criteria": acceptance_criteria,
        "authorized_surface": authorized_surface,
        "explicit_non_goals": explicit_non_goals,
        "risk_flags": risk_flags,
        "skill_bundle": skill_bundle,
    }
    path_for_errors = reference_path or _DEFAULT_REVIEWER_REFERENCE_PATH
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise PmError(
            f"reviewer prompt template {path_for_errors} has an unresolved or stray brace "
            f"({exc}); escape literal '{{' / '}}' as '{{{{' / '}}}}' per the template's editing note"
        ) from exc

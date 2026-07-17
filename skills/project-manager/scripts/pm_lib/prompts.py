"""Developer and repair prompt rendering: the templates a slice harness actually receives.

Composes the frozen slice contract, prior-slice context, and reviewer/policy
paths into the prompt text sent to a fresh Developer session, and the
targeted in-session repair prompts for a bounded, unrelaxed gate retry. Never
re-derives or relaxes a gate; only renders what the gate decision and frozen
contract already established.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any

from .git_ops import meaningful_status_lines, normalize_authorized_entry, unauthorized_files
from .models import GateDecision, PmError, PlanSlice
from .runtime import reviewer_jobs_path, result_schema_path, skill_root, slice_paths


def orchestrator_embedded_instructions() -> str:
    # A slice needs the PM-specific semantic delegation contract, not the full
    # general-purpose orchestrator bundle and every linked harness reference.
    # The validated launcher owns harness flags, while this compact reference is
    # the single source for the instructions a skill-less slice harness needs.
    path = skill_root().parent / "orchestrator" / "references" / "pm-slice-contract.md"
    return path.read_text(encoding="utf-8").rstrip()


def reviewer_auth_policy_text(reviewer_tools: tuple[str, ...]) -> str:
    if not reviewer_tools:
        return "No reviewer tool is configured for this run."
    policies: list[str] = []
    if "copilot" in reviewer_tools:
        policies.append(
            "Copilot gets an isolated per-slice COPILOT_HOME for writable session state when Copilot is a reviewer "
            "and not the developer."
        )
    if "codex" in reviewer_tools:
        policies.append("Codex gets an isolated per-slice CODEX_HOME seeded with auth.json when Codex is a reviewer and not the developer.")
    if "claude" in reviewer_tools:
        policies.append(
            "Claude reviewers use the operator's normal Claude Code auth/config; PM does not set CLAUDE_CONFIG_DIR because "
            "copying .credentials.json into an isolated config dir is not a valid portable login. For non-interactive "
            "isolated auth, provide ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN in the environment."
        )
    unknown = [tool for tool in reviewer_tools if tool not in {"copilot", "codex", "claude"}]
    for tool in unknown:
        policies.append(f"{tool} uses its configured profile; no credential isolation policy is defined by PM.")
    return " ".join(policies)


def load_prompt_template() -> str:
    # The extracted template is rendered with str.format in
    # render_developer_prompt, so any literal `{`/`}` added to the template
    # block in references/developer-prompt.md (a JSON example, a shell
    # `${var}`) would raise at runtime. Keep placeholders as the only braces in
    # that block, or escape literals as `{{`/`}}`. The template file carries the
    # same warning for editors.
    path = skill_root() / "references" / "developer-prompt.md"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```md\n(?P<template>.*?)\n```", text, flags=re.DOTALL)
    if not match:
        raise PmError(f"developer prompt template not found in {path}")
    return match.group("template")


def render_developer_prompt(
    state: dict[str, Any],
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    run_json: Path,
    reviewer_tools: tuple[str, ...] = (),
    reviewer_model: str | None = None,
    reviewer_effort: str | None = None,
) -> str:
    del run_json  # The rendered prompt must not disclose controller state.
    template = load_prompt_template()
    paths = slice_paths(slice_artifact_dir)
    prior_context_path = slice_artifact_dir / "prior-slice-context.md"
    prior_context_sha256 = hashlib.sha256(prior_context_path.read_bytes()).hexdigest() if prior_context_path.is_file() else "not-generated"
    example_tool = reviewer_tools[0] if len(reviewer_tools) == 1 else "<one required tool; create one request per tool>"
    request_example = {
        "schema_version": 2,
        "label": "01-<tool>-<subtask>",
        "slice_id": plan_slice.slice_id,
        "plan_sha256": str(state.get("plan", {}).get("sha256") or ""),
        "tool": example_tool,
        "model": reviewer_model or "default",
        "effort": reviewer_effort or "default",
        "task": "<bounded reviewer task>",
        "context": "<task-specific context>",
        "required_skills": [],
        "files": [normalize_authorized_entry(entry) for entry in plan_slice.authorized_files],
        "constraints": ["<task-specific constraint>"],
        "expected_output": "<exact output contract>",
    }
    values = {
        "plan_path": state["plan_path"],
        "prior_context_path": str(prior_context_path),
        "prior_context_sha256": prior_context_sha256,
        "slice_artifact_dir": str(slice_artifact_dir),
        "result_schema_path": str(result_schema_path()),
        "reviewer_jobs_path": str(reviewer_jobs_path()),
        "reviewer_artifact_root": str(paths["reviewer_artifact_root"]),
        "slice_tmp_dir": str(paths["tmp_dir"]),
        "tool_home_root": str(paths["tool_home_root"]),
        "copilot_home": str(paths["copilot_home"]),
        "codex_home": str(paths["codex_home"]),
        "claude_config_dir": str(paths["claude_config_dir"]),
        "reviewer_auth_policy": reviewer_auth_policy_text(reviewer_tools),
        "reviewer_policy_path": str(slice_artifact_dir / "reviewer-policy.json"),
        "reviewer_request_example": json.dumps(request_example, indent=2, sort_keys=True),
        "audit_skill_reminder": (
            "This slice is opt-in (`Independent audit required: yes`): the drift-audit and code-review requests "
            "must each set `required_skills` to exactly `[\"drift-audit\"]` or exactly `[\"code-review\"]` — never "
            "`[]` and never both skills in one request. A mismatched or mixed value is rejected before launch."
            if plan_slice.independent_audit_required
            else ""
        ),
        "orchestrator_embedded_instructions": orchestrator_embedded_instructions(),
        "reviewer_tools": ", ".join(reviewer_tools) if reviewer_tools else "none available for this run",
        "reviewer_model": reviewer_model or "default",
        "reviewer_effort": reviewer_effort or "default",
        "slice_id": plan_slice.slice_id,
        "slice_title": plan_slice.title,
        "intended_change": plan_slice.sections.get("Intended Change", ""),
        "acceptance_criteria": plan_slice.sections.get("Acceptance Criteria", ""),
        "authorized_surface": plan_slice.sections.get("Authorized Surface", ""),
        "explicit_non_goals": plan_slice.sections.get("Explicit Non-Goals", ""),
        "risk_flags": plan_slice.sections.get("Risk Flags", ""),
        "validation_plan": plan_slice.sections.get("Validation Plan", ""),
        "rollback_path": plan_slice.sections.get("Rollback Path", ""),
    }
    return template.format(**values).rstrip() + "\n"


def load_repair_template() -> str:
    # Same str.format constraint as load_prompt_template: only the documented
    # placeholders may appear as braces in the repair block.
    path = skill_root() / "references" / "developer-prompt.md"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"## Repair Template\n.*?```md\n(?P<template>.*?)\n```", text, flags=re.DOTALL)
    if not match:
        raise PmError(f"repair prompt template not found in {path}")
    return match.group("template")


# Gate signatures whose repair keeps the implementation and commit untouched
# and fixes only the named evidence/quality gap.
_EVIDENCE_GATE_LABELS = {
    "validation": "validation",
    "drift": "drift audit",
    "review": "code review",
    "reviewer-evidence": "reviewer evidence",
}


def _repair_stanza(
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    gate: GateDecision,
    before_head: str | None,
) -> str:
    signature = gate.signature
    if signature in _EVIDENCE_GATE_LABELS:
        label = _EVIDENCE_GATE_LABELS[signature]
        return (
            "Your code changes and any commit you already created are present and correct as far as PM verified; "
            f"do NOT re-implement the slice and do NOT redo work that already passed. Fix only the {label} gap "
            f"quoted above: re-run that gate properly, write its evidence artifact under the slice artifact "
            "directory, and record the passing outcome in `developer-result.json`."
        )
    if signature == "unauthorized-files":
        offending = unauthorized_files(set(gate.actual_changed_files), plan_slice.authorized_files)
        start = before_head or "<the slice starting commit recorded by PM>"
        if offending:
            # shlex-quoted so a path with spaces or metacharacters stays one
            # argument when the developer copies the command literally.
            restore_command = shlex.join(["git", "checkout", start, "--", *offending])
        else:
            restore_command = f"git checkout {start} -- <the files named in the gate reason>"
        return (
            "These files are OUTSIDE your authorized surface: "
            + (", ".join(offending) if offending else "(see the gate reason above)")
            + ". This repair is restore-only: restore those exact paths to their pre-slice committed content with\n\n"
            + f"    {restore_command}\n\n"
            + "and touch nothing else. Do not otherwise edit, fix, or improve anything outside your authorized files."
        )
    if signature == "changed-files-mismatch":
        actual = ", ".join(gate.actual_changed_files) if gate.actual_changed_files else "(no changed files)"
        return (
            "Your self-reported `changed_files` does not match git evidence. No file edits are needed: correct the "
            f"`changed_files` list in `developer-result.json` to exactly match the actual diff: {actual}."
        )
    if signature == "commit-missing":
        return (
            "Your gates passed but the required commit was never created. use the commit skill for this slice's "
            "work only, then record the commit in `developer-result.json`."
        )
    if signature == "dirty-worktree":
        status_path = slice_artifact_dir / "git-status-after.txt"
        status_lines = meaningful_status_lines(status_path.read_text(encoding="utf-8")) if status_path.is_file() else []
        listing = "\n".join(status_lines) if status_lines else "(see the gate reason above)"
        return (
            "The worktree has uncommitted changes outside `.ai-pm/` after your commit:\n\n"
            + listing
            + "\n\nResolve them within your authorized surface — commit authorized slice work or restore stray "
            "edits to their committed content — so the worktree ends clean."
        )
    if signature == "result-malformed":
        return (
            "Your `developer-result.json` is unreadable or invalid (see the gate reason above). Your file edits "
            "may be fine; rewrite `developer-result.json` so it is valid JSON matching the required schema, "
            "reporting this same slice honestly."
        )
    if signature == "developer-repairable":
        return (
            "You reported status `repairable` yourself. Resume this same slice: complete the remaining work inside "
            "the frozen contract, re-run validation, the drift-audit skill, and the code-review skill, and write a "
            "fresh `developer-result.json`."
        )
    if signature == "transient-service-unavailable":
        return (
            "PM found a current-attempt, high-confidence transient service-unavailable signal alongside your "
            "terminal self-report. Retry the interrupted operation within this same frozen slice, then complete "
            "the normal validation, audit, review, commit, and structured-result contract. Do not relax any gate."
        )
    if signature == "residual-ledger-mismatch":
        return (
            "An audit or review artifact visibly contains non-empty findings or observations while your structured "
            "`residual_findings` ledger is empty. Reconcile the reporting artifacts: fix any material slice-caused "
            "defect, and copy every legitimate non-blocking post-plan consideration into the ledger with its "
            "disposition, rationale, and suggested follow-up. Then rewrite the result without weakening a verdict."
        )
    if signature == "context-budget":
        return (
            "Your implementation and quality gates passed, but the reporting in `developer-result.json` would make "
            "the cumulative context too large for the next planned slice. Do not change code or drop material "
            "knowledge. Condense repetition and verbosity in the summary, validation notes, blockers, continuation "
            "notes, and residual findings while preserving every distinct decision, lesson, outcome, risk, and "
            "follow-up; then rewrite the same honest result."
        )
    if signature == "idle-no-progress":
        return (
            "PM observed no pane progress across the configured observation ceiling. Re-establish your current "
            "slice state from the frozen prompt and repository evidence, continue the interrupted work, and write "
            "the normal structured result only after every unchanged gate has completed."
        )
    if signature == "ledger-retention":
        return (
            "The named archived ledger item (quoted in the gate reason above) is missing from your fresh "
            "`residual_findings` or `continuation_notes` in `developer-result.json`. Do not re-implement the slice: "
            "restore that exact item verbatim by merging it back into the ledger alongside anything you added this "
            "round — do not erase it and do not weaken any verdict to make room for it. Retention is verified "
            "mechanically, not semantically, so deletion is indistinguishable from silent knowledge loss: if the "
            "issue an item names was since resolved, keep the item and record the resolution in an additional note "
            "or in the summary instead of removing it. Then rewrite `developer-result.json` reporting this same "
            "slice honestly."
        )
    raise PmError(f"no repair stanza defined for gate signature: {signature!r}")


def render_repair_prompt(
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    gate: GateDecision,
    before_head: str | None = None,
) -> str:
    """Render a targeted in-session correction for a repairable gate failure.

    Composes only from data already on hand (the gate decision, the frozen
    slice contract, and evidence files in the slice artifact directory); it
    never re-derives or relaxes the gate.
    """
    template = load_repair_template()
    authorized = "\n".join(f"- {entry}" for entry in plan_slice.authorized_files) or "- (none parsed from the plan)"
    values = {
        "slice_id": plan_slice.slice_id,
        "slice_title": plan_slice.title,
        "gate_reason": gate.reason,
        "gate_signature": gate.signature,
        "category_stanza": _repair_stanza(plan_slice, slice_artifact_dir, gate, before_head),
        "authorized_files": authorized,
        "delegation_posture": (
            "This slice is opt-in (`Independent audit required: yes`): use separate validated read-only reviewer requests for "
            "`drift-audit` and `code-review`, wait for drift `PASS` before launching review, and retain both successful contracts."
            if plan_slice.independent_audit_required
            else "This slice uses the default posture: independent delegation remains preferred when a reviewer is available, "
            "but a local audit is accepted. In either case, drift must return `PASS` before code review starts."
        ),
        "slice_artifact_dir": str(slice_artifact_dir),
        "result_schema_path": str(result_schema_path()),
    }
    return template.format(**values).rstrip() + "\n"

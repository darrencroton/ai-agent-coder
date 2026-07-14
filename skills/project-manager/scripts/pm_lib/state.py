from __future__ import annotations

import copy
import fcntl
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .constants import (
    CONTINUATION_NOTE_CATEGORIES,
    DEFAULT_SUPERVISION,
    FULL_COMMIT_RE,
    MAX_CONTINUATION_FIELD_CHARS,
    MAX_CONTINUATION_NOTES,
    PARSER_NAME,
    RUN_ACTIVE_STATUSES,
    RUN_STOP_STATUSES,
    SCHEMA_VERSION,
)
from .models import GateDecision, PmError, PlanSlice
from .gates import _continuation_notes_status, _residual_findings_status, reviewer_audit_provenance
from .plan import completed_slice_ids
from .runtime import relative_artifact_path
from .utils import utc_now


def normalize_stop_status(gate_status: str) -> str:
    """Map a non-passing gate status onto an allowed run stop status."""
    status_value = "failed" if gate_status == "fail" else gate_status
    return status_value if status_value in RUN_STOP_STATUSES else "blocked"


def controller_state_path(run_path: Path) -> Path | None:
    """Return the controller-owned state path outside the target worktree.

    Slice harnesses need writable artifacts under ``.ai-pm`` but never need
    mutable controller state. The protected copy lives in Git metadata, which
    is outside normal worktree sandboxes (including linked worktrees).
    """
    path = run_json_path(run_path)
    run_dir = path.parent
    if run_dir.parent.name != "runs" or run_dir.parent.parent.name != ".ai-pm":
        return None
    repo = run_dir.parent.parent.parent
    git_marker = repo / ".git"
    if git_marker.is_dir():
        git_dir = git_marker.resolve()
    elif git_marker.is_file():
        marker = git_marker.read_text(encoding="utf-8", errors="replace").strip()
        if not marker.startswith("gitdir:"):
            return None
        configured = Path(marker.split(":", 1)[1].strip())
        git_dir = (repo / configured).resolve() if not configured.is_absolute() else configured.resolve()
    else:
        return None
    return git_dir / "ai-pm-control" / run_dir.name / "run.json"


def _read_run_file(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PmError(f"run.json not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PmError(f"invalid run.json: {path}: {exc}") from exc
    return validate_run_state(state, path)


def load_run(run_path: Path) -> dict[str, Any]:
    path = run_json_path(run_path)
    state = _read_run_file(path)
    controller_path = controller_state_path(path)
    controller_required = isinstance(state.get("harness"), dict) and state["harness"].get("launch_config") is not None
    if controller_path is None:
        if controller_required:
            raise PmError(f"controller-owned run state cannot be located for active run mirror: {path}")
        return state
    if not controller_path.exists():
        if controller_required:
            raise PmError(
                f"controller-owned run state is missing for active run mirror: {path}; "
                "use stop-with-evidence to halt safely"
            )
    else:
        controller_state = _read_run_file(controller_path)
        if state != controller_state:
            raise PmError(
                f"worktree run-state mirror differs from controller-owned state: {path}; "
                "use stop-with-evidence to halt safely and preserve tamper evidence"
            )
    return state


def load_controller_run(run_path: Path) -> dict[str, Any]:
    """Load the isolated controller copy without trusting the worktree mirror."""
    controller_path = controller_state_path(run_path)
    if controller_path is None or not controller_path.exists():
        raise PmError(f"controller-owned run state not found for {run_json_path(run_path)}")
    return _read_run_file(controller_path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_run(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    validate_run_state(state, path)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    controller_path = controller_state_path(path)
    controller_required = isinstance(state.get("harness"), dict) and state["harness"].get("launch_config") is not None
    if controller_required and (controller_path is None or not controller_path.exists()):
        raise PmError(f"controller-owned run state is missing for active run mirror: {path}")
    if controller_path is not None and controller_path.exists():
        _atomic_write_text(controller_path, payload)
    _atomic_write_text(path, payload)
    _atomic_write_text(path.parent / "run-report.md", render_run_report(state))


def activate_controller_state(path: Path, state: dict[str, Any]) -> None:
    """Create the protected state copy immediately before a harness launch."""
    controller_path = controller_state_path(path)
    if controller_path is None:
        raise PmError(f"cannot isolate controller state for run path: {path}")
    state["updated_at"] = utc_now()
    validate_run_state(state, path)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(controller_path, payload)
    _atomic_write_text(path, payload)
    _atomic_write_text(path.parent / "run-report.md", render_run_report(state))


def _residual_lines(findings: Any) -> list[str]:
    if not isinstance(findings, list) or not findings:
        return ["- none"]
    lines: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        location = f" at `{finding.get('location')}`" if finding.get("location") else ""
        lines.append(
            f"- **{finding.get('severity', 'unspecified')} / {finding.get('source', 'other')}**{location}: "
            f"{finding.get('summary', '')} _(disposition: {finding.get('disposition', 'unspecified')})_"
        )
        lines.append(f"  - Rationale: {finding.get('rationale', '')}")
        lines.append(f"  - Suggested follow-up: {finding.get('suggested_follow_up', '')}")
    return lines or ["- none"]


def _continuation_note_lines(notes: Any) -> list[str]:
    if not isinstance(notes, list) or not notes:
        return ["- none"]
    lines: list[str] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        location = f" at `{note.get('location')}`" if note.get("location") else ""
        lines.append(
            f"- **{note.get('category', 'unspecified')}**{location}: {note.get('summary', '')} "
            f"_(applies to: {note.get('applies_to', '')})_"
        )
        lines.append(f"  - Rationale: {note.get('rationale', '')}")
    return lines or ["- none"]


def _audit_provenance_text(entry: dict[str, Any], audit: str) -> str:
    provenance = entry.get("audit_provenance") if isinstance(entry.get("audit_provenance"), dict) else {}
    record = provenance.get(audit) if isinstance(provenance.get(audit), dict) else {}
    performed_by = record.get("performed_by")
    if performed_by == "reviewer":
        return f"reviewer ({record.get('reviewer_tool')}/{record.get('reviewer_label')})"
    if performed_by == "developer-self-audit":
        context = record.get("fallback_context") or "no validated Reviewer evidence"
        return f"developer-self-audit — {context}"
    return f"not-observed — {record.get('fallback_context') or 'audit provenance was not observed by PM'}"


def render_slice_summary(entry: dict[str, Any]) -> str:
    validation = entry.get("validation") if isinstance(entry.get("validation"), list) else []
    validation_lines = [
        f"- `{item.get('command', '')}`: {item.get('result', 'unknown')} — {item.get('notes', '')}"
        for item in validation
        if isinstance(item, dict)
    ] or ["- none recorded"]
    commit = entry.get("commit") if isinstance(entry.get("commit"), dict) else {}
    blockers = entry.get("blockers") if isinstance(entry.get("blockers"), list) else []
    return "\n".join(
        [
            f"# {entry.get('slice_id', 'Unknown slice')} — {entry.get('title', '')}",
            "",
            f"- Status: {entry.get('status', 'unknown')}",
            f"- Summary: {entry.get('summary', '') or 'none recorded'}",
            f"- Gate reason: {entry.get('gate_reason', '')}",
            f"- Drift audit: {(entry.get('drift_audit') or {}).get('verdict') if isinstance(entry.get('drift_audit'), dict) else None}",
            f"- Drift audit performed by: {_audit_provenance_text(entry, 'drift-audit')}",
            f"- Code review: {(entry.get('code_review') or {}).get('verdict') if isinstance(entry.get('code_review'), dict) else None}",
            f"- Code review performed by: {_audit_provenance_text(entry, 'code-review')}",
            f"- Commit: {commit.get('hash') or 'none'}",
            "",
            "## Validation",
            "",
            *validation_lines,
            "",
            "## Residual Findings / Post-Plan Considerations",
            "",
            *_residual_lines(entry.get("residual_findings")),
            "",
            "## Continuation Notes for Later Slices",
            "",
            *_continuation_note_lines(entry.get("continuation_notes")),
            "",
            "## Blockers",
            "",
            *([f"- {blocker}" for blocker in blockers] or ["- none"]),
            "",
            "## Next Action",
            "",
            f"- {entry.get('next_action') or 'none'}",
            "",
        ]
    )


def render_run_report(state: dict[str, Any]) -> str:
    slices = state.get("slices") if isinstance(state.get("slices"), list) else []
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    group_index: dict[str, int] = {}
    for entry in slices:
        if not isinstance(entry, dict):
            continue
        slice_id = str(entry.get("slice_id") or "Unknown")
        if slice_id not in group_index:
            group_index[slice_id] = len(groups)
            groups.append((slice_id, []))
        groups[group_index[slice_id]][1].append(entry)
    authoritative = [entries[-1] for _, entries in groups if entries]
    authoritative_completed = sum(
        1 for entry in authoritative if str(entry.get("status") or "").lower() in {"pass", "assumed-complete"}
    )
    all_residuals = [
        (entry, finding)
        for entry in authoritative
        for finding in (entry.get("residual_findings") if isinstance(entry.get("residual_findings"), list) else [])
        if isinstance(finding, dict)
    ]
    all_continuation_notes = [
        (entry, note)
        for entry in authoritative
        for note in (entry.get("continuation_notes") if isinstance(entry.get("continuation_notes"), list) else [])
        if isinstance(note, dict)
    ]
    lines = [
        "# Project Manager Run Report",
        "",
        f"- Run: `{state.get('run_id', 'unknown')}`",
        f"- Status: {state.get('status', 'unknown')}",
        f"- Branch: `{state.get('branch', 'unknown')}`",
        f"- Plan: `{state.get('plan_path', '')}`",
        f"- Completed slices: {authoritative_completed}/{(state.get('plan') or {}).get('slice_count', 0)}",
        f"- Stop reason: {state.get('stop_reason') or 'none'}",
        "",
        "## Slice Results",
        "",
    ]
    if not slices:
        lines.append("- No slices have run.")
    for slice_id, entries in groups:
        if not entries:
            continue
        lines.extend([f"### {slice_id} — {entries[-1].get('title', '')}", ""])
        for outcome_number, entry in enumerate(entries, start=1):
            authoritative_label = "authoritative" if outcome_number == len(entries) else "superseded"
            commit = entry.get("commit") if isinstance(entry.get("commit"), dict) else {}
            validation = entry.get("validation") if isinstance(entry.get("validation"), list) else []
            validation_summary = "; ".join(
                f"{item.get('command', '')}: {item.get('result', 'unknown')}"
                for item in validation
                if isinstance(item, dict)
            ) or "none recorded"
            artifact = entry.get("artifact_dir") or "none"
            lines.extend(
                [
                    f"#### Recorded outcome {outcome_number} — {authoritative_label}",
                    "",
                    f"- Status: {entry.get('status', 'unknown')}",
                    f"- Summary: {entry.get('summary', '') or 'none recorded'}",
                    f"- Validation: {validation_summary}",
                    f"- Drift audit: {(entry.get('drift_audit') or {}).get('verdict') if isinstance(entry.get('drift_audit'), dict) else None}",
                    f"- Drift audit performed by: {_audit_provenance_text(entry, 'drift-audit')}",
                    f"- Code review: {(entry.get('code_review') or {}).get('verdict') if isinstance(entry.get('code_review'), dict) else None}",
                    f"- Code review performed by: {_audit_provenance_text(entry, 'code-review')}",
                    f"- Commit: {commit.get('hash') or 'none'}",
                    f"- Artifacts: `{artifact}`",
                    f"- Slice summary: `{entry.get('slice_summary') or 'not generated'}`",
                    "",
                ]
            )
    lines.extend(["## Residual Findings / Post-Plan Considerations", ""])
    if not all_residuals:
        lines.append("- none")
    else:
        for entry, finding in all_residuals:
            location = f" at `{finding.get('location')}`" if finding.get("location") else ""
            lines.append(
                f"- **{entry.get('slice_id')} — {finding.get('severity')} / {finding.get('source')}**{location}: "
                f"{finding.get('summary')} _(disposition: {finding.get('disposition')})_"
            )
            lines.append(f"  - Rationale: {finding.get('rationale')}")
            lines.append(f"  - Suggested follow-up: {finding.get('suggested_follow_up')}")
    lines.extend(["", "## Continuation Notes for Later Slices", ""])
    if not all_continuation_notes:
        lines.append("- none")
    else:
        for entry, note in all_continuation_notes:
            location = f" at `{note.get('location')}`" if note.get("location") else ""
            lines.append(
                f"- {entry.get('slice_id')} — **{note.get('category', 'unspecified')}**{location}: "
                f"{note.get('summary', '')} _(applies to: {note.get('applies_to', '')})_"
            )
            lines.append(f"  - Rationale: {note.get('rationale', '')}")
    lines.extend(["", "## Run Blockers and Next Actions", ""])
    any_actions = False
    for entry in authoritative:
        blockers = entry.get("blockers") if isinstance(entry.get("blockers"), list) else []
        next_action = str(entry.get("next_action") or "")
        for blocker in blockers:
            lines.append(f"- {entry.get('slice_id')}: blocker — {blocker}")
            any_actions = True
        if next_action:
            lines.append(f"- {entry.get('slice_id')}: next — {next_action}")
            any_actions = True
    if not any_actions:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def run_json_path(run_path: Path) -> Path:
    path = run_path.expanduser().resolve()
    return path / "run.json" if path.is_dir() else path


_RUN_FIELDS = {
    "schema_version",
    "run_id",
    "created_at",
    "updated_at",
    "status",
    "repo_path",
    "plan_path",
    "worktree_root",
    "branch",
    "harness",
    "policy",
    "plan",
    "current_slice",
    "supervision",
    "operational_events_path",
    "approvals",
    "slices",
    "stop_reason",
}
_CURRENT_SLICE_FIELDS = {
    "slice_id",
    "title",
    "artifact_dir",
    "tmux_session",
    "attempt",
    "started_at",
    "before_head",
    "pause",
    "reviewer_tools",
    "repair",
    "reviewer_policy",
    "prior_slice_context",
}
_CURRENT_SLICE_OPTIONAL_FIELDS = {"developer_session_id", "launch_config"}
_SLICE_ENTRY_FIELDS = {
    "slice_id",
    "title",
    "status",
    "started_at",
    "completed_at",
    "artifact_dir",
    "before_head",
    "changed_files",
    "summary",
    "validation",
    "drift_audit",
    "code_review",
    "audit_provenance",
    "commit",
    "next_action",
    "blockers",
    "residual_findings",
    "continuation_notes",
    "gate_reason",
    "reviewer_tools",
    "repair",
}
_SLICE_ENTRY_OPTIONAL_FIELDS = {"reviewer_policy", "slice_summary"}
_REPAIR_FIELDS = {"round", "last_signature", "signature_streak", "session_generation"}
_HARNESS_FIELDS = {"name", "adapter", "preflight"}
_HARNESS_OPTIONAL_FIELDS = {"launch_config", "model_identity", "model_requested", "effort_requested"}
_PREFLIGHT_FIELDS = {"platform", "python", "python_version", "git", "tmux"}
_POLICY_FIELDS = {"dirty_state", "approval_gated_slices", "max_repair_attempts", "commit_required"}
_PLAN_FIELDS = {"slice_count", "parser", "sha256"}
_REVIEWER_POLICY_FIELDS = {"sha256", "policy"}
_PRIOR_SLICE_CONTEXT_FIELDS = {"path", "sha256"}
_AUDIT_PROVENANCE_FIELDS = {"drift-audit", "code-review"}
_AUDIT_PROVENANCE_RECORD_FIELDS = {"performed_by", "reviewer_tool", "reviewer_label", "fallback_context"}
_LAUNCH_STRING_FIELDS = {"harness_command", "harness_model", "harness_effort", "reviewer_model", "reviewer_effort"}
_LAUNCH_BOOLEAN_FIELDS = {"allow_profile_command", "allow_unattended_default"}
_LAUNCH_CONFIG_FIELDS = _LAUNCH_STRING_FIELDS | _LAUNCH_BOOLEAN_FIELDS | {"reviewer_tools"}
_MODEL_IDENTITY_FIELDS = {"requested", "resolved_id", "display_name", "catalog_verified", "checked_at", "slice_id"}
_MODEL_IDENTITY_OPTIONAL_FIELDS = {"inventory_command"}
_PAUSE_FIELDS = {"paused_until", "reason", "evidence_event_id"}
_APPROVAL_FIELDS = {"approved_at", "reason", "approved_by"}
_RUN_STATUSES = RUN_ACTIVE_STATUSES | RUN_STOP_STATUSES | {"complete"}
_SLICE_STATUSES = {"pass", "assumed-complete", "needs-human", "blocked", "fail", "failed", "cancelled"}


def _require_fields(value: dict[str, Any], required: set[str], label: str, path: Path) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise PmError(f"invalid schema-v4 run state at {path}: {label} missing required field(s): {', '.join(missing)}")


def _reject_unknown_fields(value: dict[str, Any], allowed: set[str], label: str, path: Path) -> None:
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise PmError(f"invalid schema-v4 run state at {path}: {label} contains unsupported field(s): {', '.join(unknown)}")


def _require_mapping_shape(value: Any, template: dict[str, Any], label: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be an object")
    _require_fields(value, set(template), label, path)
    _reject_unknown_fields(value, set(template), label, path)
    for key, expected in template.items():
        if isinstance(expected, dict):
            _require_mapping_shape(value[key], expected, f"{label}.{key}", path)


def _require_string(value: Any, label: str, path: Path, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be {qualifier}")


def _require_integer(value: Any, label: str, path: Path, *, minimum: int = 0, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        requirement = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be an integer {requirement}")


def _require_string_list(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be a list of strings")


def _validate_digest(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be a 64-character lowercase hex digest")


def _validate_commit_hash(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, str) or not FULL_COMMIT_RE.fullmatch(value):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be a full lowercase Git commit hash")


def _validate_repair(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be an object")
    _require_fields(value, _REPAIR_FIELDS, label, path)
    _reject_unknown_fields(value, _REPAIR_FIELDS, label, path)
    _require_integer(value["round"], f"{label}.round", path)
    _require_string(value["last_signature"], f"{label}.last_signature", path, allow_empty=True)
    _require_integer(value["signature_streak"], f"{label}.signature_streak", path, maximum=2)
    _require_integer(value["session_generation"], f"{label}.session_generation", path, minimum=1)


def _validate_continuation_notes(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, list):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be a list")
    if len(value) > MAX_CONTINUATION_NOTES:
        raise PmError(
            f"invalid schema-v4 run state at {path}: {label} exceeds the maximum of {MAX_CONTINUATION_NOTES} entries"
        )
    required = {"category", "summary", "rationale", "applies_to"}
    for index, note in enumerate(value):
        note_label = f"{label}[{index}]"
        if not isinstance(note, dict):
            raise PmError(f"invalid schema-v4 run state at {path}: {note_label} must be an object")
        _require_fields(note, required, note_label, path)
        _reject_unknown_fields(note, required | {"location"}, note_label, path)
        for field in required:
            _require_string(note[field], f"{note_label}.{field}", path)
            if len(note[field]) > MAX_CONTINUATION_FIELD_CHARS:
                raise PmError(
                    f"invalid schema-v4 run state at {path}: {note_label}.{field} exceeds the maximum of "
                    f"{MAX_CONTINUATION_FIELD_CHARS} characters"
                )
        if note["category"] not in CONTINUATION_NOTE_CATEGORIES:
            raise PmError(f"invalid schema-v4 run state at {path}: unsupported {note_label}.category {note['category']!r}")
        if "location" in note:
            _require_string(note["location"], f"{note_label}.location", path, allow_empty=True)
            if len(note["location"]) > MAX_CONTINUATION_FIELD_CHARS:
                raise PmError(
                    f"invalid schema-v4 run state at {path}: {note_label}.location exceeds the maximum of "
                    f"{MAX_CONTINUATION_FIELD_CHARS} characters"
                )


def _validate_reviewer_policy(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be an object")
    _require_fields(value, _REVIEWER_POLICY_FIELDS, label, path)
    _reject_unknown_fields(value, _REVIEWER_POLICY_FIELDS, label, path)
    _validate_digest(value["sha256"], f"{label}.sha256", path)
    if not isinstance(value["policy"], dict):
        raise PmError(f"invalid schema-v4 run state at {path}: {label}.policy must be an object")


def _validate_audit_provenance(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be an object")
    _require_fields(value, _AUDIT_PROVENANCE_FIELDS, label, path)
    _reject_unknown_fields(value, _AUDIT_PROVENANCE_FIELDS, label, path)
    for audit in _AUDIT_PROVENANCE_FIELDS:
        record = value[audit]
        if not isinstance(record, dict):
            raise PmError(f"invalid schema-v4 run state at {path}: {label}.{audit} must be an object")
        _require_fields(record, _AUDIT_PROVENANCE_RECORD_FIELDS, f"{label}.{audit}", path)
        _reject_unknown_fields(record, _AUDIT_PROVENANCE_RECORD_FIELDS, f"{label}.{audit}", path)
        performed_by = record["performed_by"]
        if performed_by not in {"reviewer", "developer-self-audit", "not-observed"}:
            raise PmError(f"invalid schema-v4 run state at {path}: {label}.{audit}.performed_by is unsupported")
        if performed_by == "reviewer":
            _require_string(record["reviewer_tool"], f"{label}.{audit}.reviewer_tool", path)
            _require_string(record["reviewer_label"], f"{label}.{audit}.reviewer_label", path)
            if record["fallback_context"] is not None:
                raise PmError(f"invalid schema-v4 run state at {path}: {label}.{audit}.fallback_context must be null for Reviewer provenance")
        else:
            if record["reviewer_tool"] is not None or record["reviewer_label"] is not None:
                raise PmError(f"invalid schema-v4 run state at {path}: {label}.{audit} cannot name Reviewer evidence for {performed_by}")
            _require_string(record["fallback_context"], f"{label}.{audit}.fallback_context", path)


def _validate_launch_config(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be an object")
    _require_fields(value, _LAUNCH_CONFIG_FIELDS, label, path)
    _reject_unknown_fields(value, _LAUNCH_CONFIG_FIELDS, label, path)
    for field in _LAUNCH_BOOLEAN_FIELDS:
        if not isinstance(value.get(field), bool):
            raise PmError(f"invalid schema-v4 run state at {path}: {label}.{field} must be a boolean")
    for field in _LAUNCH_STRING_FIELDS:
        if value.get(field) is not None:
            _require_string(value[field], f"{label}.{field}", path)
    _require_string_list(value.get("reviewer_tools"), f"{label}.reviewer_tools", path)


def _validate_model_identity(value: Any, label: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: {label} must be an object")
    _require_fields(value, _MODEL_IDENTITY_FIELDS, label, path)
    _reject_unknown_fields(value, _MODEL_IDENTITY_FIELDS | _MODEL_IDENTITY_OPTIONAL_FIELDS, label, path)
    for field in ("requested", "resolved_id"):
        if value[field] is not None:
            _require_string(value[field], f"{label}.{field}", path)
    _require_string(value["display_name"], f"{label}.display_name", path, allow_empty=True)
    if not isinstance(value["catalog_verified"], bool):
        raise PmError(f"invalid schema-v4 run state at {path}: {label}.catalog_verified must be a boolean")
    _require_string(value["checked_at"], f"{label}.checked_at", path)
    _require_string(value["slice_id"], f"{label}.slice_id", path)
    if value.get("inventory_command") is not None:
        _require_string(value["inventory_command"], f"{label}.inventory_command", path)


def validate_run_state(state: dict[str, Any], path: Path) -> dict[str, Any]:
    """Validate the one supported durable run-state shape without migration."""
    if not isinstance(state, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: run must be an object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise PmError(
            f"unsupported run-state schema at {path}: expected {SCHEMA_VERSION}, found {state.get('schema_version')!r}; "
            "initialize a new PM run"
        )
    _require_fields(state, _RUN_FIELDS, "run", path)
    _reject_unknown_fields(state, _RUN_FIELDS, "run", path)
    for field in ("run_id", "created_at", "updated_at", "repo_path", "plan_path", "branch"):
        _require_string(state[field], field, path)
    if state["worktree_root"] is not None:
        _require_string(state["worktree_root"], "worktree_root", path)
    if state["stop_reason"] is not None:
        _require_string(state["stop_reason"], "stop_reason", path)
    if not isinstance(state["status"], str) or state["status"] not in _RUN_STATUSES:
        raise PmError(f"invalid schema-v4 run state at {path}: unsupported run status {state['status']!r}")

    harness = state["harness"]
    if not isinstance(harness, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: harness must be an object")
    _require_fields(harness, _HARNESS_FIELDS, "harness", path)
    _reject_unknown_fields(harness, _HARNESS_FIELDS | _HARNESS_OPTIONAL_FIELDS, "harness", path)
    _require_string(harness["name"], "harness.name", path)
    if harness["adapter"] is not None:
        _require_string(harness["adapter"], "harness.adapter", path)
    if not isinstance(harness["preflight"], dict):
        raise PmError(f"invalid schema-v4 run state at {path}: harness.preflight must be an object")
    _require_fields(harness["preflight"], _PREFLIGHT_FIELDS, "harness.preflight", path)
    _reject_unknown_fields(harness["preflight"], _PREFLIGHT_FIELDS, "harness.preflight", path)
    for field in _PREFLIGHT_FIELDS:
        value = harness["preflight"][field]
        if value is not None:
            _require_string(value, f"harness.preflight.{field}", path)
    if harness.get("launch_config") is not None:
        _validate_launch_config(harness["launch_config"], "harness.launch_config", path)
    if harness.get("model_identity") is not None:
        _validate_model_identity(harness["model_identity"], "harness.model_identity", path)
    for field in ("model_requested", "effort_requested"):
        if harness.get(field) is not None:
            _require_string(harness[field], f"harness.{field}", path)

    policy = state["policy"]
    if not isinstance(policy, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: policy must be an object")
    _require_fields(policy, _POLICY_FIELDS, "policy", path)
    _reject_unknown_fields(policy, _POLICY_FIELDS, "policy", path)
    if policy["dirty_state"] != "clean-required" or policy["approval_gated_slices"] != "stop":
        raise PmError(f"invalid schema-v4 run state at {path}: policy contains unsupported enforcement values")
    _require_integer(policy["max_repair_attempts"], "policy.max_repair_attempts", path)
    if not isinstance(policy["commit_required"], bool):
        raise PmError(f"invalid schema-v4 run state at {path}: policy.commit_required must be a boolean")

    plan = state["plan"]
    if not isinstance(plan, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: plan must be an object")
    _require_fields(plan, _PLAN_FIELDS, "plan", path)
    _reject_unknown_fields(plan, _PLAN_FIELDS, "plan", path)
    _require_integer(plan["slice_count"], "plan.slice_count", path, minimum=1)
    if plan["parser"] != PARSER_NAME:
        raise PmError(f"invalid schema-v4 run state at {path}: plan.parser must be {PARSER_NAME!r}")
    _validate_digest(plan["sha256"], "plan.sha256", path)

    approvals = state["approvals"]
    if not isinstance(approvals, dict):
        raise PmError(f"invalid schema-v4 run state at {path}: approvals must be an object")
    for slice_id, approval in approvals.items():
        _require_string(slice_id, "approvals key", path)
        if not isinstance(approval, dict):
            raise PmError(f"invalid schema-v4 run state at {path}: approvals[{slice_id!r}] must be an object")
        _require_fields(approval, _APPROVAL_FIELDS, f"approvals[{slice_id!r}]", path)
        _reject_unknown_fields(approval, _APPROVAL_FIELDS, f"approvals[{slice_id!r}]", path)
        for field in _APPROVAL_FIELDS:
            _require_string(approval[field], f"approvals[{slice_id!r}].{field}", path)

    _require_mapping_shape(state["supervision"], DEFAULT_SUPERVISION, "supervision", path)
    if not isinstance(state["supervision"]["mode"], str) or state["supervision"]["mode"] not in {
        "deterministic-batch",
        "model-supervised",
    }:
        raise PmError(f"invalid schema-v4 run state at {path}: supervision.mode is unsupported")
    _require_string(state["supervision"]["default_resume_prompt"], "supervision.default_resume_prompt", path)
    if state["supervision"]["pause_policy"] != DEFAULT_SUPERVISION["pause_policy"]:
        raise PmError(f"invalid schema-v4 run state at {path}: supervision.pause_policy is unsupported")
    for field in (
        "default_reset_buffer_seconds",
        "max_single_pause_seconds",
        "max_consecutive_pauses_per_slice",
        "max_cumulative_pause_seconds_per_run",
        "max_transient_retries_per_slice",
        "max_observe_staleness_seconds",
        "min_idle_observation_windows",
    ):
        _require_integer(state["supervision"][field], f"supervision.{field}", path)
    for field in DEFAULT_SUPERVISION["pause_counters"]:
        _require_integer(state["supervision"]["pause_counters"][field], f"supervision.pause_counters.{field}", path)
    if not isinstance(state["operational_events_path"], str) or not state["operational_events_path"]:
        raise PmError(f"invalid schema-v4 run state at {path}: operational_events_path must be a non-empty string")
    if not isinstance(state["slices"], list):
        raise PmError(f"invalid schema-v4 run state at {path}: slices must be a list")
    for index, entry in enumerate(state["slices"]):
        if not isinstance(entry, dict):
            raise PmError(f"invalid schema-v4 run state at {path}: slices[{index}] must be an object")
        _require_fields(entry, _SLICE_ENTRY_FIELDS, f"slices[{index}]", path)
        _reject_unknown_fields(
            entry,
            _SLICE_ENTRY_FIELDS | _SLICE_ENTRY_OPTIONAL_FIELDS,
            f"slices[{index}]",
            path,
        )
        if not isinstance(entry["status"], str) or entry["status"] not in _SLICE_STATUSES:
            raise PmError(f"invalid schema-v4 run state at {path}: unsupported slices[{index}] status {entry['status']!r}")
        _validate_repair(entry["repair"], f"slices[{index}].repair", path)
        _validate_audit_provenance(entry["audit_provenance"], f"slices[{index}].audit_provenance", path)
        if entry["repair"]["round"] > policy["max_repair_attempts"]:
            raise PmError(f"invalid schema-v4 run state at {path}: slices[{index}].repair.round exceeds policy budget")
        _require_string_list(entry["reviewer_tools"], f"slices[{index}].reviewer_tools", path)
        _validate_continuation_notes(entry["continuation_notes"], f"slices[{index}].continuation_notes", path)
        if entry["status"] == "assumed-complete":
            if entry["before_head"] is not None or entry["artifact_dir"] is not None:
                raise PmError(f"invalid schema-v4 run state at {path}: assumed-complete slice boundaries must be null")
        else:
            _validate_commit_hash(entry["before_head"], f"slices[{index}].before_head", path)
            _require_string(entry["artifact_dir"], f"slices[{index}].artifact_dir", path)
            _require_string(entry.get("slice_summary"), f"slices[{index}].slice_summary", path)
            _validate_reviewer_policy(entry.get("reviewer_policy"), f"slices[{index}].reviewer_policy", path)
    current = state["current_slice"]
    if current is not None:
        if not isinstance(current, dict):
            raise PmError(f"invalid schema-v4 run state at {path}: current_slice must be an object or null")
        _require_fields(current, _CURRENT_SLICE_FIELDS, "current_slice", path)
        _reject_unknown_fields(
            current,
            _CURRENT_SLICE_FIELDS | _CURRENT_SLICE_OPTIONAL_FIELDS,
            "current_slice",
            path,
        )
        _validate_repair(current["repair"], "current_slice.repair", path)
        if current["repair"]["round"] > policy["max_repair_attempts"]:
            raise PmError(f"invalid schema-v4 run state at {path}: current_slice.repair.round exceeds policy budget")
        _validate_commit_hash(current["before_head"], "current_slice.before_head", path)
        _require_string_list(current["reviewer_tools"], "current_slice.reviewer_tools", path)
        _validate_reviewer_policy(current["reviewer_policy"], "current_slice.reviewer_policy", path)
        prior_context = current["prior_slice_context"]
        if not isinstance(prior_context, dict):
            raise PmError(f"invalid schema-v4 run state at {path}: current_slice.prior_slice_context must be an object")
        _require_fields(prior_context, _PRIOR_SLICE_CONTEXT_FIELDS, "current_slice.prior_slice_context", path)
        _reject_unknown_fields(prior_context, _PRIOR_SLICE_CONTEXT_FIELDS, "current_slice.prior_slice_context", path)
        _require_string(prior_context["path"], "current_slice.prior_slice_context.path", path)
        _validate_digest(prior_context["sha256"], "current_slice.prior_slice_context.sha256", path)
        launch_config = current.get("launch_config")
        if launch_config is not None:
            _validate_launch_config(launch_config, "current_slice.launch_config", path)
        _require_integer(current["attempt"], "current_slice.attempt", path, minimum=1)
        for field in ("slice_id", "title", "artifact_dir", "tmux_session", "started_at"):
            _require_string(current[field], f"current_slice.{field}", path)
        if current["pause"] is not None:
            if not isinstance(current["pause"], dict):
                raise PmError(f"invalid schema-v4 run state at {path}: current_slice.pause must be an object or null")
            _require_fields(current["pause"], _PAUSE_FIELDS, "current_slice.pause", path)
            _reject_unknown_fields(current["pause"], _PAUSE_FIELDS, "current_slice.pause", path)
            for field in _PAUSE_FIELDS:
                _require_string(current["pause"][field], f"current_slice.pause.{field}", path)
    return state


def update_run_locked(run_json: Path, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Update run.json under a per-run advisory lock."""
    path = run_json_path(run_json)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_run(path)
        mutate(state)
        write_run(path, state)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return state


def operational_events_file(repo: Path, state: dict[str, Any]) -> Path:
    path = Path(state["operational_events_path"])
    return path if path.is_absolute() else repo / path


def _next_event_number(event_path: Path) -> int:
    """Next event number from a sidecar counter, not by re-counting lines.

    Counting lines on every append is O(n) per event and O(n^2) over a run —
    a multi-hour pause at a 2s poll cadence produces thousands of events. The
    counter file lives beside the log and is read/written under the same lock.
    If the counter sidecar is lost, it is reconstructed by counting once.
    """
    counter_path = event_path.with_name(event_path.name + ".counter")
    if counter_path.exists():
        try:
            current = int(counter_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            current = 0
    elif event_path.exists():
        with event_path.open(encoding="utf-8") as handle:
            current = sum(1 for _ in handle)
    else:
        current = 0
    counter_path.write_text(f"{current + 1}\n", encoding="utf-8")
    return current + 1


def append_operational_event(repo: Path, state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Append one operational event without rewriting run.json."""
    event_path = operational_events_file(repo, state)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = event_path.with_suffix(event_path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        record = dict(event)
        if "event_id" not in record:
            record["event_id"] = f"op-{_next_event_number(event_path):04d}"
        record.setdefault("detected_at", utc_now())
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return record


def resolve_run_path(repo: Path, value: str) -> Path:
    if value == "current":
        return repo / ".ai-pm" / "current"
    return Path(value).expanduser().resolve()


def resolve_run_dir(repo: Path, value: str) -> Path:
    path = resolve_run_path(repo, value).resolve()
    return path.parent if path.is_file() else path


def update_state_for_stop(run_json: Path, state: dict[str, Any], status_value: str, reason: str) -> None:
    state["status"] = status_value
    state["stop_reason"] = reason
    state["current_slice"] = None
    write_run(run_json, state)


def idle_status_after_pass(state: dict[str, Any]) -> str:
    return "complete" if len(completed_slice_ids(state)) >= state["plan"]["slice_count"] else "partial"


def approved_slice_ids(state: dict[str, Any]) -> set[str]:
    """Slice ids the operator has explicitly approved with the approve command."""
    approvals = state.get("approvals")
    if not isinstance(approvals, dict):
        return set()
    return {str(slice_id) for slice_id in approvals}


def reset_slice_pause_counters(state: dict[str, Any]) -> None:
    """Zero the per-slice pause counter when a new slice attempt starts.

    Without this reset the counter named "consecutive pauses per slice" is
    actually a per-run cap: two pauses anywhere in the run would block every
    later slice's first pause. The cumulative per-run counter is untouched.
    """
    counters = state.setdefault("supervision", {}).setdefault("pause_counters", {})
    counters["consecutive_pauses_current_slice"] = 0


def default_repair_state() -> dict[str, Any]:
    return {"round": 0, "last_signature": "", "signature_streak": 0, "session_generation": 1}


def repair_state(current: dict[str, Any] | None) -> dict[str, Any]:
    """Read the required schema-v4 repair state."""
    if not isinstance(current, dict) or not isinstance(current.get("repair"), dict):
        raise PmError("schema-v4 current slice is missing required repair state")
    repair = current["repair"]
    return {
        "round": int(repair["round"]),
        "last_signature": str(repair["last_signature"]),
        "signature_streak": int(repair["signature_streak"]),
        "session_generation": int(repair["session_generation"]),
    }


def current_slice_state(
    repo: Path,
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    session_name: str,
    attempt: int,
    started_at: str,
    before_head: str | None,
    developer_session_id: str | None = None,
    reviewer_tools: tuple[str, ...] = (),
    repair: dict[str, Any] | None = None,
    reviewer_policy: dict[str, Any] | None = None,
    prior_slice_context: dict[str, str] | None = None,
    launch_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not before_head:
        raise PmError("cannot start a schema-v4 slice without a recorded before_head")
    if reviewer_policy is None:
        raise PmError("cannot start a schema-v4 slice without a reviewer-policy snapshot")
    if prior_slice_context is None:
        raise PmError("cannot start a schema-v4 slice without protected prior-slice context metadata")
    state = {
        "slice_id": plan_slice.slice_id,
        "title": plan_slice.title,
        "artifact_dir": relative_artifact_path(repo, slice_artifact_dir),
        "tmux_session": session_name,
        "attempt": attempt,
        "started_at": started_at,
        "before_head": before_head,
        "pause": None,
        # Persisted so a later, separate invocation (finalize-slice,
        # stop-with-evidence) can recover the reviewer-tool requirement for
        # this slice attempt without depending on that invocation's own
        # --reviewer-tools flag, which may not be re-supplied.
        "reviewer_tools": list(reviewer_tools),
        # Repair-loop progress for this slice: {round, last_signature,
        # signature_streak, session_generation}. Budget and circuit-breaker
        # decisions are driven from this persisted state, not from counting
        # appended slice entries (in-session repairs append none).
        "repair": dict(repair) if repair is not None else default_repair_state(),
        "reviewer_policy": copy.deepcopy(reviewer_policy),
        "prior_slice_context": copy.deepcopy(prior_slice_context),
    }
    if developer_session_id:
        state["developer_session_id"] = developer_session_id
    if launch_config is not None:
        state["launch_config"] = copy.deepcopy(launch_config)
    return state


def slice_entry_from_gate(
    repo: Path,
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    started_at: str,
    gate: GateDecision,
    before_head: str | None = None,
    reviewer_tools: tuple[str, ...] = (),
    repair: dict[str, Any] | None = None,
    reviewer_policy: dict[str, Any] | None = None,
    *,
    write_summary: bool = True,
) -> dict[str, Any]:
    result = gate.result or {}
    residual_findings = result.get("residual_findings", [])
    if _residual_findings_status(residual_findings) is not None:
        residual_findings = []
    continuation_notes = result.get("continuation_notes", [])
    if _continuation_notes_status(continuation_notes) is not None:
        continuation_notes = []
    entry = {
        "slice_id": plan_slice.slice_id,
        "title": plan_slice.title,
        "status": gate.status,
        "started_at": started_at,
        "completed_at": utc_now(),
        "artifact_dir": relative_artifact_path(repo, slice_artifact_dir),
        # The commit HEAD immediately before this slice's work started. reconcile
        # uses it to recompute changed files against the exact slice boundary
        # instead of guessing HEAD^ (which misses a slice's earlier commits).
        "before_head": before_head,
        "changed_files": list(gate.actual_changed_files or tuple(result.get("changed_files") or ())),
        "summary": result.get("summary", ""),
        "validation": result.get("validation", []),
        "drift_audit": result.get("drift_audit", {"verdict": None, "path": ""}),
        "code_review": result.get("code_review", {"verdict": None, "path": ""}),
        "commit": result.get("commit", {"requested": False, "created": False, "hash": None}),
        "next_action": result.get("next_action", ""),
        "blockers": result.get("blockers", []),
        "residual_findings": copy.deepcopy(residual_findings),
        "continuation_notes": copy.deepcopy(continuation_notes),
        "gate_reason": gate.reason,
        # Preserved (not just read) so reconcile can recover the reviewer-tool
        # requirement for this attempt without a fresh --reviewer-tools flag.
        "reviewer_tools": list(reviewer_tools),
        "audit_provenance": reviewer_audit_provenance(
            slice_artifact_dir,
            reviewer_tools,
            reviewer_policy,
            developer_result=result if gate.result is not None else None,
            repo=repo,
        ),
    }
    entry["repair"] = dict(repair) if repair is not None else default_repair_state()
    if reviewer_policy is not None:
        entry["reviewer_policy"] = copy.deepcopy(reviewer_policy)
    slice_summary_path = slice_artifact_dir / "slice-summary.md"
    slice_summary_path.parent.mkdir(parents=True, exist_ok=True)
    entry["slice_summary"] = relative_artifact_path(repo, slice_summary_path)
    if write_summary:
        slice_summary_path.write_text(render_slice_summary(entry), encoding="utf-8")
    return entry

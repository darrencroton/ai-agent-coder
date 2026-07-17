from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from .constants import (
    CONTINUATION_NOTE_CATEGORIES,
    DEVELOPER_STATUSES,
    MAX_CONTINUATION_FIELD_CHARS,
    MAX_CONTINUATION_NOTES,
    REQUIRED_AUDIT_SKILLS,
    SCHEMA_VERSION,
)
from .git_ops import (
    changed_files_between,
    commit_is_descendant,
    is_full_commit_hash,
    meaningful_status_lines,
    unauthorized_files,
)
from .models import GateDecision, PmError, PlanSlice
from .utils import utc_now


# Failure-signature taxonomy: every non-pass classification PM makes itself
# carries one of these coarse, stable labels. The repair loop keys its circuit
# breaker on them and the repair-prompt renderer selects its correction stanza
# by them, so they are the single source of truth for which violations are
# repairable (steer the live developer session) versus terminal
# (integrity/trust breach — do not steer from a false-belief context; stop for
# a human). Re-verification after a repair re-runs the *identical* gate, so a
# repairable classification can never let a bad slice through — it only grants
# another chance to satisfy the same gate.
REPAIRABLE_SIGNATURES = frozenset(
    {
        "validation",
        "drift",
        "review",
        "reviewer-evidence",
        "unauthorized-files",
        "changed-files-mismatch",
        "result-malformed",
        "residual-ledger-mismatch",
        "commit-missing",
        "dirty-worktree",
        "developer-repairable",
        "context-budget",
        # A high-confidence, current-attempt transient-outage signal alongside
        # a terminal self-report: retrying the same slice in the same session
        # is the fix, so this is steerable rather than a trust breach.
        "transient-service-unavailable",
        # No visible pane progress across the observation ceiling is a stall,
        # not evidence the developer session did anything wrong — the fix is
        # a nudge to continue, so it is steerable like any other repair.
        "idle-no-progress",
        # A fresh result silently dropping a previously archived ledger item
        # is a bookkeeping gap the developer can restore without redoing any
        # already-accepted work.
        "ledger-retention",
    }
)
TERMINAL_SIGNATURES = frozenset(
    {
        "integrity-head",
        "slice-id-mismatch",
        # An opt-in slice ("Independent audit required: yes") with no reviewer made
        # available cannot be satisfied by steering the developer — it is an
        # operator/plan configuration mismatch — so it stops for a human at once
        # rather than burning the repair budget.
        "reviewer-unavailable",
        # The protected prior-slice context digest no longer matches what was
        # embedded at launch: reality and the record disagree, so continuing
        # to steer from that session would build on a false belief rather than
        # repair a fixable gap.
        "prior-context-integrity",
    }
)


def gate_failure(
    signature: str,
    reason: str,
    result: dict[str, Any] | None = None,
    actual_changed_files: tuple[str, ...] = (),
    *,
    repair_payload: dict[str, Any] | None = None,
) -> GateDecision:
    """Build a non-pass GateDecision whose status follows the signature taxonomy."""
    if signature in TERMINAL_SIGNATURES:
        status = "needs-human"
    elif signature in REPAIRABLE_SIGNATURES:
        status = "repairable"
    else:
        raise PmError(f"unknown gate failure signature: {signature}")
    return GateDecision(status, reason, result, actual_changed_files, signature, repair_payload)


def write_reconciliation_artifact(
    slice_artifact_dir: Path,
    *,
    field: str,
    reported_value: str,
    corrected_value: str,
    reason: str,
) -> None:
    payload = {
        "field": field,
        "reported_value": reported_value,
        "corrected_value": corrected_value,
        "reason": reason,
        "reconciled_at": utc_now(),
    }
    (slice_artifact_dir / "pm-reconciliation.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (slice_artifact_dir / "pm-reconciliation.md").write_text(
        "# PM Reconciliation\n\n"
        f"- Field: `{field}`\n"
        f"- Reported value: `{reported_value}`\n"
        f"- Corrected value: `{corrected_value}`\n"
        f"- Reason: {reason}\n",
        encoding="utf-8",
    )


def write_developer_result(path: Path, result: dict[str, Any]) -> None:
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_developer_result(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PmError(f"developer result missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PmError(f"invalid developer result: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PmError(f"developer result is not an object: {path}")
    return data


def _within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def artifact_exists(repo: Path, slice_artifact_dir: Path, result: dict[str, Any], field: str, default_name: str) -> bool:
    """Return True only for a real, non-empty evidence file inside the run.

    A verdict string in developer-result.json is not enough on its own: PM
    also requires the named artifact to exist as a non-empty file that resolves
    under the slice artifact directory. That stops a result from
    satisfying the gate by pointing `path` at an arbitrary existing file (for
    example `/etc/hosts` or `README.md`) or at an empty placeholder.
    """
    configured = result.get(field, {}).get("path") if isinstance(result.get(field), dict) else None
    if not configured:
        candidates = [slice_artifact_dir / default_name]
    else:
        candidate = Path(configured)
        candidates = [candidate] if candidate.is_absolute() else [slice_artifact_dir / candidate, repo / candidate]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            non_empty = candidate.stat().st_size > 0
        except OSError:
            continue
        if non_empty and _within(candidate, slice_artifact_dir):
            return True
    return False


def object_field(result: dict[str, Any], field: str) -> dict[str, Any]:
    value = result.get(field)
    return value if isinstance(value, dict) else {}


def _normalized_reviewer_contract(
    entry: dict[str, Any],
    *,
    required_tools: set[str],
    policy: dict[str, Any],
    policy_sha256: str,
    reviewer_artifact_root: Path | None,
) -> tuple[str, str | None] | None:
    """Return the normalized tool/audit pair for one exact Reviewer contract.

    Role and access are launcher-owned constants in the v2 Reviewer contract.
    PM validates those normalized evidence values directly; policy allow-lists
    no longer exist and cannot be widened by a Developer session.
    """
    entry_tool = str(entry.get("tool", "")).lower()
    contract = entry.get("launch_contract")
    if entry_tool not in required_tools or not isinstance(contract, dict):
        return None
    if contract.get("status") != "pass" or contract.get("policy_sha256") != policy_sha256:
        return None
    if contract.get("slice_id") != policy.get("slice_id") or contract.get("plan_sha256") != policy.get("plan_sha256"):
        return None
    contract_tool = str(contract.get("tool", "")).lower()
    if contract_tool != entry_tool or contract_tool not in required_tools:
        return None
    if contract.get("model") != str(policy.get("required_model") or "default"):
        return None
    if contract.get("effort") != str(policy.get("required_effort") or "default"):
        return None
    if contract.get("role") != "reviewer" or contract.get("access") != "read-only":
        return None
    if contract.get("repo_path") != policy.get("repo_path") or contract.get("cwd") != policy.get("repo_path"):
        return None
    if not isinstance(entry.get("pid"), int) or entry.get("pid", 0) <= 0:
        return None
    outfile_text, errfile_text = entry.get("outfile"), entry.get("errfile")
    if not isinstance(outfile_text, str) or not isinstance(errfile_text, str):
        return None
    outfile, errfile = Path(outfile_text), Path(errfile_text)
    if not outfile.is_file() or not errfile.is_file():
        return None
    if reviewer_artifact_root is not None and not (
        _within(outfile, reviewer_artifact_root) and _within(errfile, reviewer_artifact_root)
    ):
        return None
    skills = contract.get("required_skills")
    audit = (
        skills[0]
        if isinstance(skills, list)
        and len(skills) == 1
        and isinstance(skills[0], str)
        and skills[0] in REQUIRED_AUDIT_SKILLS
        else None
    )
    return contract_tool, audit


class ReviewerCompletion(NamedTuple):
    """One label's validated, successfully-completed reviewer run.

    Shared by reviewer_audit_provenance and reviewer_evidence_failure: both
    require the identical exact-match launch contract and the identical
    completed/returncode==0 status bar before trusting a record. `finished_at`
    is pre-normalized to None for anything but a non-empty string; the two
    consumers deliberately disagree on what a None finished_at means for
    latest-verdict selection (see _iter_reviewer_completions), so that
    decision stays with each caller rather than living here.
    """

    tool: str
    label: str
    audit: str | None
    verdict: str | None
    finished_at: str | None


def _normalized_run_reviewers(
    run: dict[str, Any],
    *,
    required: set[str],
    policy: dict[str, Any],
    policy_sha256: str,
    reviewer_artifact_root: Path | None,
) -> dict[str, tuple[str, str | None]]:
    """Normalize one run's manifest reviewer entries into {label: (tool, audit)}.

    Includes every label whose tool is in `required` and whose launch contract
    validates against the immutable policy snapshot — a plain reviewer entry
    (audit is None) and an audit-bearing entry are both included here; a
    caller that only cares about audits filters on `audit is not None` itself.
    """
    manifest = run.get("manifest") if isinstance(run.get("manifest"), dict) else {}
    manifest_reviewers = object_field(manifest, "reviewers")
    normalized_by_label: dict[str, tuple[str, str | None]] = {}
    for label, entry in (manifest_reviewers.items() if isinstance(manifest_reviewers, dict) else ()):
        if not isinstance(entry, dict) or str(entry.get("tool", "")).lower() not in required:
            continue
        normalized = _normalized_reviewer_contract(
            entry,
            required_tools=required,
            policy=policy,
            policy_sha256=policy_sha256,
            reviewer_artifact_root=reviewer_artifact_root,
        )
        if normalized is not None:
            normalized_by_label[str(label)] = normalized
    return normalized_by_label


def _iter_reviewer_completions(
    run: dict[str, Any], normalized_by_label: dict[str, tuple[str, str | None]]
) -> Iterator[ReviewerCompletion]:
    """Yield one ReviewerCompletion per normalized label with a successful status.

    Launch alone is not enough: a required reviewer that crashed on start (or
    is still running at finalize) proves nothing was delegated. The status
    payloads come from reviewer_jobs.py's own *-status.json files, so like the
    tool name they are mechanically derived, not narrated.
    """
    for status in run.get("reviewers") or ():
        if not isinstance(status, dict):
            continue
        normalized = normalized_by_label.get(str(status.get("label", "")))
        if normalized is None:
            continue
        if str(status.get("state", "")).lower() != "completed" or status.get("returncode") != 0:
            continue
        tool, audit = normalized
        verdict = None
        if audit is not None:
            verdicts = status.get("skill_verdicts") if isinstance(status.get("skill_verdicts"), dict) else {}
            raw_verdict = verdicts.get(audit)
            verdict = str(raw_verdict).upper() if isinstance(raw_verdict, str) else None
        finished_at = status.get("finished_at")
        yield ReviewerCompletion(
            tool=tool,
            label=str(status.get("label", "")),
            audit=audit,
            verdict=verdict,
            finished_at=finished_at if isinstance(finished_at, str) and finished_at else None,
        )


def reviewer_audit_provenance(
    slice_artifact_dir: Path,
    reviewer_tools: tuple[str, ...],
    expected_snapshot: dict[str, Any] | None,
    *,
    developer_result: dict[str, Any] | None = None,
    repo: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Classify each audit from independently verifiable execution evidence.

    Reviewer attribution requires exact validated launch evidence. Developer
    self-audit attribution requires both a Developer result entry for the audit
    and its non-empty artifact inside the slice directory. A terminal outcome
    with neither proof remains ``not-observed`` rather than claiming an audit
    happened merely because no Reviewer was configured.
    """
    audit_names = tuple(REQUIRED_AUDIT_SKILLS)
    provenance = {
        audit: {
            "performed_by": "not-observed",
            "reviewer_tool": None,
            "reviewer_label": None,
            "fallback_context": "no validated Reviewer evidence or proven Developer self-audit artifact exists for this audit",
        }
        for audit in audit_names
    }

    audit_fields = {
        "drift-audit": ("drift_audit", "drift-audit.md"),
        "code-review": ("code_review", "code-review.md"),
    }

    def proven_developer_artifact(candidate: Path) -> bool:
        try:
            return candidate.is_file() and candidate.stat().st_size > 0 and _within(candidate, slice_artifact_dir)
        except OSError:
            return False

    if isinstance(developer_result, dict):
        for audit, (field, default_name) in audit_fields.items():
            result_record = developer_result.get(field)
            if not isinstance(result_record, dict):
                continue
            configured = result_record.get("path")
            candidates: list[Path]
            if isinstance(configured, str) and configured:
                configured_path = Path(configured)
                if configured_path.is_absolute():
                    candidates = [configured_path]
                else:
                    candidates = [slice_artifact_dir / configured_path]
                    if repo is not None:
                        candidates.append(repo / configured_path)
            else:
                candidates = [slice_artifact_dir / default_name]
            if not any(proven_developer_artifact(candidate) for candidate in candidates):
                continue
            fallback = "no Reviewer was configured; Developer performed the audit"
            if reviewer_tools:
                fallback = "no successful validated Reviewer PASS evidence exists for this audit; Developer performed the audit"
            provenance[audit] = {
                "performed_by": "developer-self-audit",
                "reviewer_tool": None,
                "reviewer_label": None,
                "fallback_context": fallback,
            }

    policy_path = slice_artifact_dir / "reviewer-policy.json"
    summary_path = slice_artifact_dir / "reviewer-runs-summary.json"
    if not reviewer_tools or not policy_path.is_file() or not summary_path.is_file() or not isinstance(expected_snapshot, dict):
        return provenance
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return provenance
    if not isinstance(policy, dict) or policy.get("schema_version") != 2:
        return provenance
    policy_sha256 = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    if policy_sha256 != expected_snapshot.get("sha256") or policy != expected_snapshot.get("policy"):
        return provenance
    required = {tool.lower() for tool in reviewer_tools}
    root_text = str(policy.get("reviewer_artifact_root") or "")
    reviewer_root = Path(root_text).resolve() if root_text else None
    records: dict[str, list[tuple[str, int, str, str, str | None]]] = {audit: [] for audit in audit_names}
    sequence = 0
    runs = summary.get("runs") if isinstance(summary, dict) else []
    for run in runs if isinstance(runs, list) else ():
        if not isinstance(run, dict):
            continue
        normalized_by_label = {
            label: normalized
            for label, normalized in _normalized_run_reviewers(
                run, required=required, policy=policy, policy_sha256=policy_sha256, reviewer_artifact_root=reviewer_root
            ).items()
            if normalized[1] is not None
        }
        for completion in _iter_reviewer_completions(run, normalized_by_label):
            # A completed reviewer with no recorded finished_at cannot be
            # ordered against other candidates for this audit; skipping just
            # this record (rather than disqualifying the whole audit, as
            # reviewer_evidence_failure does below) still lets a well-formed
            # record for the same audit win the latest-verdict selection.
            if completion.finished_at is None:
                continue
            sequence += 1
            records[completion.audit].append(
                (completion.finished_at, sequence, completion.tool, completion.label, completion.verdict)
            )
    for audit, candidates in records.items():
        if not candidates:
            continue
        _, _, tool, label, verdict = max(candidates, key=lambda item: (item[0], item[1]))
        if verdict != "PASS":
            continue
        provenance[audit] = {
            "performed_by": "reviewer",
            "reviewer_tool": tool,
            "reviewer_label": label,
            "fallback_context": None,
        }
    return provenance


def reviewer_evidence_failure(
    slice_artifact_dir: Path,
    reviewer_tools: tuple[str, ...],
    expected_snapshot: dict[str, Any] | None,
) -> str | None:
    """Return a gate-failure reason if a required reviewer tool has no mechanical launch evidence.

    Narration and executable-name matching are insufficient. Require the
    plan-mandated reviewer-evidence.md, PM's authoritative reviewer-policy.json, a
    manifest launch contract whose policy digest and normalized semantic
    fields match that policy, and successful helper-owned process status. This
    prevents both mislabeled substitutes and correctly named harnesses launched
    with unvalidated model/access/role flags.
    """
    if not reviewer_tools:
        return None
    tools_label = ", ".join(reviewer_tools)
    required = {tool.lower() for tool in reviewer_tools}
    evidence_path = slice_artifact_dir / "reviewer-evidence.md"
    if not evidence_path.is_file() or evidence_path.stat().st_size == 0:
        return f"required reviewer tool(s) ({tools_label}) have no reviewer-evidence.md for this slice"
    policy_path = slice_artifact_dir / "reviewer-policy.json"
    if not policy_path.is_file():
        return (
            "required reviewer launch policy is missing; PM cannot verify that reviewer tool, model, access, and slice identity "
            "were validated before launch"
        )
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "reviewer-policy.json is not valid JSON"
    if not isinstance(policy, dict):
        return "reviewer-policy.json must contain a JSON object"
    if policy.get("schema_version") != 2:
        return "reviewer-policy.json schema_version is missing or unsupported"
    policy_sha256 = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    if not isinstance(expected_snapshot, dict):
        return "PM run state has no immutable reviewer-policy snapshot for this slice"
    if policy_sha256 != expected_snapshot.get("sha256") or policy != expected_snapshot.get("policy"):
        return (
            "reviewer-policy.json changed after PM created the slice policy. Restore the exact PM-generated policy; "
            "do not alter tools, model, effort, repository, artifact root, audit skill sets, or any other policy field from the Developer session"
        )

    summary_path = slice_artifact_dir / "reviewer-runs-summary.json"
    if not summary_path.is_file():
        return (
            f"required reviewer tool(s) ({tools_label}) were never launched: reviewer-runs-summary.json is missing "
            "(no reviewer_jobs.py run directory was created)"
        )
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "reviewer-runs-summary.json is not valid JSON"
    runs = summary.get("runs") if isinstance(summary, dict) else []
    if not isinstance(runs, list):
        runs = []
    reviewer_artifact_root_text = str(policy.get("reviewer_artifact_root") or "")
    reviewer_artifact_root = Path(reviewer_artifact_root_text).resolve() if reviewer_artifact_root_text else None
    any_reviewer = False
    matched_tools: set[str] = set()
    contracted_tools: set[str] = set()
    successful_tools: set[str] = set()
    required_audits = set(REQUIRED_AUDIT_SKILLS)
    contracted_audits: set[str] = set()
    successful_audits: set[str] = set()
    audit_records: dict[str, list[tuple[str | None, int, str | None]]] = {skill: [] for skill in required_audits}
    audit_sequence = 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("reviewers"):
            any_reviewer = True
        manifest_reviewers = object_field(run.get("manifest") or {}, "reviewers") if isinstance(run.get("manifest"), dict) else {}
        for label, entry in (manifest_reviewers.items() if isinstance(manifest_reviewers, dict) else ()):
            if isinstance(entry, dict) and str(entry.get("tool", "")).lower() in required:
                matched_tools.add(str(entry.get("tool", "")).lower())
        # A manifest/status pair alone is just JSON on disk — the developer
        # has full write access to its own reviewer-runs tree and could
        # hand-author a matching manifest entry, launch contract, and
        # completed status without ever invoking reviewer_jobs.py or a real
        # harness process. start_tracked_reviewer always records a positive
        # subprocess pid and always creates outfile/errfile via `.open("wb")`
        # before the child process starts, regardless of whether the tool
        # produces output, and always writes them inside the policy's
        # reviewer_artifact_root. _normalized_run_reviewers requiring that
        # real filesystem footprint (via _normalized_reviewer_contract) closes
        # the zero-effort forgery (three hand-written JSON files, no other
        # artifacts) without depending on content that could itself be
        # fabricated.
        normalized_by_label = _normalized_run_reviewers(
            run,
            required=required,
            policy=policy,
            policy_sha256=policy_sha256,
            reviewer_artifact_root=reviewer_artifact_root,
        )
        for contract_tool, audit in normalized_by_label.values():
            contracted_tools.add(contract_tool)
            if audit is not None:
                contracted_audits.add(audit)
        # Launch alone is not enough: a required reviewer that crashed on start
        # (or is still running at finalize) proves nothing was delegated. The
        # status payloads come from reviewer_jobs.py's own *-status.json files,
        # so like the tool name they are mechanically derived, not narrated.
        for completion in _iter_reviewer_completions(run, normalized_by_label):
            successful_tools.add(completion.tool)
            if completion.audit is not None:
                audit_sequence += 1
                audit_records[completion.audit].append((completion.finished_at, audit_sequence, completion.verdict))
    if not any_reviewer:
        return (
            f"required reviewer tool(s) ({tools_label}) were never launched: a reviewer_jobs.py run directory was "
            "created but no reviewer was started in it (reviewer-runs-summary.json has no reviewer entries)"
        )
    missing_tools = sorted(required - matched_tools)
    if missing_tools:
        return (
            f"required reviewer tool(s) were never actually invoked: {', '.join(missing_tools)}. reviewer_jobs.py recorded "
            "reviewer run(s), but none used matching executable(s) — check for labels that hide a different command"
        )
    missing_contracts = sorted(required - contracted_tools)
    if missing_contracts:
        return (
            f"required reviewer executable(s) ran without matching validated launch contracts: {', '.join(missing_contracts)}. "
            "No such reviewer has a passing deterministic launch contract matching the immutable PM policy and current "
            "reviewer-policy.json, backed by a real subprocess pid and outfile/errfile actually present under the policy "
            "reviewer_artifact_root. A hand-authored manifest/status entry does not satisfy the gate. Do not invoke the "
            "harness directly or use a raw reviewer command. Read any <label>-request-feedback.md artifact, correct the "
            "semantic reviewer request, and launch it through reviewer_jobs.py launch --policy <reviewer-policy.json> "
            "--request <reviewer-request.json>"
        )
    missing_success = sorted(required - successful_tools)
    if missing_success:
        return (
            f"required reviewer tool(s) never completed successfully: {', '.join(missing_success)}. Matching reviewer run(s) "
            "did not finish with state 'completed' and returncode 0 — crashed, cancelled, or running reviewers do not satisfy the gate"
        )
    missing_audit_contracts = sorted(required_audits - contracted_audits)
    if missing_audit_contracts:
        return (
            "opt-in independent audit is missing separate validated launch contract(s) for: "
            + ", ".join(missing_audit_contracts)
            + ". Launch one read-only request with required_skills ['drift-audit'], wait for and read its PASS verdict, "
            "then launch a separate read-only request with required_skills ['code-review']; one generic reviewer run does not prove both audits"
        )
    latest_audit_verdicts: dict[str, str | None] = {}
    for audit, records in audit_records.items():
        if not records or any(finished_at is None for finished_at, _, _ in records):
            latest_audit_verdicts[audit] = None
            continue
        latest_audit_verdicts[audit] = max(records, key=lambda record: (record[0], record[1]))[2]
        if latest_audit_verdicts[audit] == "PASS":
            successful_audits.add(audit)
    missing_audit_success = sorted(required_audits - successful_audits)
    if missing_audit_success:
        details = ", ".join(
            f"{audit}={latest_audit_verdicts.get(audit) or 'missing'}"
            for audit in missing_audit_success
        )
        return (
            "opt-in independent audit reviewer verdict is not PASS for: "
            + details
            + ". Each distinct audit reviewer must complete successfully and emit its helper-recorded "
            "PM_AUDIT_VERDICT: PASS; fix and re-run the audit or stop for human judgment"
        )
    return None


def _validation_status(validation: Any) -> str | None:
    """Return a gate-failure reason for the validation block, or None if it passes."""
    if not isinstance(validation, list) or not validation:
        return "validation evidence is missing"
    if not all(isinstance(entry, dict) for entry in validation):
        return "validation entries are malformed (expected objects)"
    if any(str(entry.get("result", "")).lower() != "pass" for entry in validation):
        return "validation did not pass"
    return None


_RESIDUAL_SOURCES = {"implementation", "validation", "drift-audit", "code-review", "reviewer", "other"}
_RESIDUAL_DISPOSITIONS = {
    "deferred-inconsequential",
    "pre-existing",
    "unrelated-out-of-scope",
    "needs-follow-up",
}


def residual_findings_status(findings: Any) -> str | None:
    """Validate the reporting-only post-plan consideration ledger."""
    if not isinstance(findings, list):
        return "residual_findings is missing or malformed (expected a list, using [] when none)"
    required_fields = ("source", "severity", "summary", "disposition", "rationale", "suggested_follow_up")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            return f"residual_findings[{index}] is malformed (expected an object)"
        for field in required_fields:
            if not isinstance(finding.get(field), str) or not str(finding[field]).strip():
                return f"residual_findings[{index}].{field} must be a non-empty string"
        if finding["source"] not in _RESIDUAL_SOURCES:
            return (
                f"residual_findings[{index}].source is invalid: {finding['source']!r}; "
                f"expected one of {', '.join(sorted(_RESIDUAL_SOURCES))}"
            )
        if finding["disposition"] not in _RESIDUAL_DISPOSITIONS:
            return (
                f"residual_findings[{index}].disposition is invalid: {finding['disposition']!r}; "
                f"expected one of {', '.join(sorted(_RESIDUAL_DISPOSITIONS))}"
            )
        if "location" in finding and not isinstance(finding["location"], str):
            return f"residual_findings[{index}].location must be a string when present"
    return None


def continuation_notes_status(notes: Any) -> str | None:
    """Validate knowledge intentionally passed to later planned slices."""
    if not isinstance(notes, list):
        return "continuation_notes is missing or malformed (expected a list, using [] when none)"
    if len(notes) > MAX_CONTINUATION_NOTES:
        return f"continuation_notes exceeds the maximum of {MAX_CONTINUATION_NOTES} entries"
    required_fields = ("category", "summary", "rationale", "applies_to")
    for index, note in enumerate(notes):
        if not isinstance(note, dict):
            return f"continuation_notes[{index}] is malformed (expected an object)"
        unknown = set(note) - {*required_fields, "location"}
        if unknown:
            return f"continuation_notes[{index}] contains unsupported field(s): {', '.join(sorted(unknown))}"
        for field in required_fields:
            if not isinstance(note.get(field), str) or not str(note[field]).strip():
                return f"continuation_notes[{index}].{field} must be a non-empty string"
            if len(note[field]) > MAX_CONTINUATION_FIELD_CHARS:
                return (
                    f"continuation_notes[{index}].{field} exceeds the maximum of "
                    f"{MAX_CONTINUATION_FIELD_CHARS} characters"
                )
        if note["category"] not in CONTINUATION_NOTE_CATEGORIES:
            return (
                f"continuation_notes[{index}].category is invalid: {note['category']!r}; "
                f"expected one of {', '.join(sorted(CONTINUATION_NOTE_CATEGORIES))}"
            )
        if "location" in note:
            if not isinstance(note["location"], str):
                return f"continuation_notes[{index}].location must be a string when present"
            if len(note["location"]) > MAX_CONTINUATION_FIELD_CHARS:
                return (
                    f"continuation_notes[{index}].location exceeds the maximum of "
                    f"{MAX_CONTINUATION_FIELD_CHARS} characters"
                )
    return None


def _artifact_has_unledgered_finding_shape(path: Path) -> bool:
    """Detect narrow Markdown shapes that visibly claim non-empty findings.

    This is deliberately syntactic. It does not decide whether an observation
    is material or re-review the diff; it only catches the contradictory shape
    "artifact lists observations, structured ledger says none".
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    heading_re = re.compile(
        r"(?im)^#{1,6}\s+.*(?:findings|observations|non[- ]blocking recommendations|remaining accepted risk).*$"
    )
    headings = list(heading_re.finditer(text))
    for heading in headings:
        next_heading = re.search(r"(?m)^#{1,6}\s+", text[heading.end() :])
        end = heading.end() + next_heading.start() if next_heading else len(text)
        body = text[heading.end() : end]
        for raw_line in body.splitlines():
            line = raw_line.strip()
            lowered = line.lower()
            if not line or re.fullmatch(r"[-|:\s]+", line):
                continue
            if lowered in {"- none", "none", "- no findings", "no findings", "- none.", "none."}:
                continue
            if any(marker in lowered for marker in ("resolved", "fixed in", "already fixed", "addressed", "no longer applicable", "n/a after")):
                continue
            if re.match(r"^(?:\d+\.|[-*])\s+", line) and (
                re.search(r"\[p[0-3]\]", lowered)
                or "non-blocking" in lowered
                or "observation" in lowered
            ):
                return True
    return False


def _apply_commit_hash_reconciliation(
    result: dict[str, Any],
    result_path: Path,
    slice_artifact_dir: Path,
    *,
    reported_hash: str,
    corrected_hash: str,
    record_reason: str,
    decision_reason: str,
    actual_changed: tuple[str, ...],
) -> GateDecision:
    """Correct the reported commit hash to the proven HEAD and record why.

    Only ever called after every other gate has passed and PM has proven the
    corrected hash is the current HEAD descended from the slice start, so the
    correction cannot mask unauthorized files, missing validation, failed
    audits/reviews, a dirty worktree, or a missing commit.
    """
    result.setdefault("reconciliations", []).append(
        {
            "field": "commit.hash",
            "reported_value": reported_hash,
            "corrected_value": corrected_hash,
            "reason": record_reason,
            "reconciled_at": utc_now(),
        }
    )
    result["commit"]["hash"] = corrected_hash
    write_developer_result(result_path, result)
    write_reconciliation_artifact(
        slice_artifact_dir,
        field="commit.hash",
        reported_value=reported_hash,
        corrected_value=corrected_hash,
        reason=record_reason,
    )
    return GateDecision("pass", decision_reason, result, actual_changed)


_LEDGER_RETENTION_FIELDS = ("residual_findings", "continuation_notes")


def _archived_ledger_items(slice_artifact_dir: Path, field: str) -> list[tuple[Path, dict[str, Any]]]:
    """Recover every well-formed archived item for one ledger field across all repair rounds.

    Tolerance mirrors runner.py's _fresh_session_repair_prompt, which recovers
    the identical items and re-injects them into the developer's next prompt
    as an instruction: an unreadable archive or a non-dict item was never a
    retrievable instruction there either, so this mechanical check must not
    demand retention of more than the prompt actually asked for.
    """
    items: list[tuple[Path, dict[str, Any]]] = []
    for archived_path in sorted(slice_artifact_dir.glob("developer-result-repair-*.json")):
        try:
            archived = json.loads(archived_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(archived, dict):
            continue
        entries = archived.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                items.append((archived_path, entry))
    return items


def _truncate_for_reason(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _covers(container: dict[str, Any], covered: dict[str, Any]) -> bool:
    """True if `container` has every key/value pair `covered` has (a field-superset)."""
    return all(key in container and container[key] == value for key, value in covered.items())


def ledger_item_retained(archived_item: dict[str, Any], candidates: list[Any]) -> bool:
    """True if some candidate item preserves every key/value pair of the archived item.

    Retention requires every key the archived item had to still be present with
    an unchanged value in some candidate — but tolerates a candidate carrying
    *additional* keys the archived item didn't have. A Developer legitimately
    adding a new field (for example a `location` key while rewriting a result
    to satisfy an unrelated schema-compliance gate) is a strict superset of the
    archived item, not a loss, and must not itself trip retention. A dropped,
    changed, or reworded value on any key the archived item did carry still
    fails, since that is exactly the silent-knowledge-loss this check exists
    to catch.
    """
    return any(isinstance(candidate, dict) and _covers(candidate, archived_item) for candidate in candidates)


def merge_maximal_ledger_item(items: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    """Insert `candidate` into `items`, keeping only field-maximal items.

    Two archived items can describe the same underlying finding at different
    completeness (for example a base item and a later round's version with an
    added `location` field) without either being a verbatim duplicate. Plain
    "not already present" dedup would keep both, and a single not-retained
    check applied item-by-item can under-collect: given a base item followed
    by a fuller version and no fresh coverage, both would independently look
    "not yet covered" unless earlier insertions are folded in as they arrive.
    This keeps exactly one, maximal entry per comparable chain: if an existing
    item already covers `candidate`'s fields, do nothing; otherwise drop every
    existing item `candidate` covers and append `candidate` once. Items that
    are incomparable (neither a superset of the other, e.g. two unrelated
    findings, or two different additive fields on the same base) are both
    kept.
    """
    for existing in items:
        if _covers(existing, candidate):
            return
    items[:] = [existing for existing in items if not _covers(candidate, existing)]
    items.append(candidate)


def _missing_ledger_items(slice_artifact_dir: Path, result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return every archived ledger item, grouped by field, missing from the fresh result.

    Archived repair-round ledgers (`developer-result-repair-<n>.json`) are
    re-injected into a relaunched session's prompt as *instructions*
    (`_fresh_session_repair_prompt`), never as anything a gate previously
    verified — so a repair round could silently drop knowledge the ledger
    already recorded and nothing would catch it. This makes retention
    mechanical: every archived `residual_findings`/`continuation_notes` item's
    fields must still appear, unchanged, in some item in the corresponding
    fresh ledger (see `ledger_item_retained`) — merging in new items, or new
    fields on a retained item, is fine; silently losing or altering an old
    field is not.

    Collects every gap across both fields in one pass — not just the first —
    so a repair round with multiple omissions gets one complete correction
    instead of consuming one repair round per item. Gaps are folded together
    with `merge_maximal_ledger_item` (not raw append) so that, across several
    archived rounds, only the fullest version of a repeatedly-refined item is
    reported missing rather than every intermediate version.
    """
    missing: dict[str, list[dict[str, Any]]] = {}
    for field in _LEDGER_RETENTION_FIELDS:
        archived_items = _archived_ledger_items(slice_artifact_dir, field)
        if not archived_items:
            continue
        fresh_value = result.get(field)
        fresh_list = fresh_value if isinstance(fresh_value, list) else []
        field_gaps: list[dict[str, Any]] = []
        for _archived_path, item in archived_items:
            if ledger_item_retained(item, fresh_list) or ledger_item_retained(item, field_gaps):
                continue
            merge_maximal_ledger_item(field_gaps, item)
        if field_gaps:
            missing[field] = field_gaps
    return missing


def _ledger_retention_failure(slice_artifact_dir: Path, result: dict[str, Any]) -> tuple[str, dict[str, list[dict[str, Any]]]] | None:
    """Return (reason, missing-items-by-field) if the fresh result drops an archived ledger item, else None.

    `reason` stays a short, human-readable summary (first gap only, truncated)
    for logs and the mechanical gate-decision record; the full missing-items
    payload — every gap, in full, across both fields — is returned separately
    so the repair-prompt renderer can embed complete JSON objects a Developer
    can actually copy verbatim, rather than the truncated summary alone.
    """
    missing = _missing_ledger_items(slice_artifact_dir, result)
    if not missing:
        return None
    field, items = next(iter(missing.items()))
    summary = _truncate_for_reason(items[0].get("summary"))
    total = sum(len(v) for v in missing.values())
    plural = "" if total == 1 else "s"
    reason = (
        f"{field} dropped an item archived in a prior repair round: \"{summary}\" no longer appears in the fresh "
        f"result's {field} ledger ({total} item{plural} missing across residual_findings/continuation_notes total); "
        "restore every missing item verbatim (merging, not erasing) — see the repair prompt for the complete list. "
        "Retention is checked mechanically, not semantically: if the underlying issue was since resolved, keep the "
        "item and record the resolution alongside it rather than deleting history"
    )
    return reason, missing


def verify_gate(
    repo: Path,
    state: dict[str, Any],
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    before_head: str | None,
    after_head: str | None,
    after_status: str,
    reviewer_tools: tuple[str, ...] = (),
    *,
    last_repair_signature: str | None = None,
) -> GateDecision:
    result_path = slice_artifact_dir / "developer-result.json"
    if not result_path.is_file():
        # Absence is not a content defect: the session may have died or timed
        # out without answering, which the runner handles as its own condition
        # (relaunch/stop), not as a steerable in-session repair.
        return GateDecision("blocked", f"developer result missing: {result_path}")
    try:
        result = load_developer_result(result_path)
    except PmError as exc:
        return gate_failure("result-malformed", str(exc))

    if result.get("schema_version") != SCHEMA_VERSION:
        return gate_failure("result-malformed", "developer result schema_version is missing or unsupported", result)
    if result.get("slice_id") != plan_slice.slice_id:
        # The developer worked (or reported on) the wrong slice: continuing
        # to steer from that context would build on a false belief about which
        # contract is in force.
        return gate_failure("slice-id-mismatch", "developer result slice_id does not match selected slice", result)

    status_value = str(result.get("status", "")).lower()
    if status_value not in DEVELOPER_STATUSES:
        return gate_failure("result-malformed", f"developer result status is invalid: {result.get('status')}", result)
    residual_failure = residual_findings_status(result.get("residual_findings"))
    if residual_failure:
        return gate_failure("result-malformed", residual_failure, result)
    continuation_failure = continuation_notes_status(result.get("continuation_notes"))
    if continuation_failure:
        return gate_failure("result-malformed", continuation_failure, result)
    if status_value != "pass":
        # The developer's own self-report is respected as-is: its
        # `repairable` earns a repair round, and its considered stops
        # (needs-human/fail/blocked) stay terminal with no PM signature.
        signature = "developer-repairable" if status_value == "repairable" else ""
        return GateDecision(status_value, f"developer reported {status_value}", result, signature=signature)

    actual_changed = changed_files_between(repo, before_head, after_head, after_status)
    changed_evidence = tuple(sorted(actual_changed))
    unauthorized = unauthorized_files(actual_changed, plan_slice.authorized_files)
    if unauthorized:
        # Repairable, but restore-only: the repair prompt for this signature is
        # bounded to restoring these exact paths to their pre-slice content,
        # never to editing outside the authorized surface.
        return gate_failure("unauthorized-files", "unauthorized changed files: " + ", ".join(unauthorized), result, changed_evidence)

    reported_files = result.get("changed_files")
    if not isinstance(reported_files, list) or not all(isinstance(item, str) for item in reported_files):
        # Same signature as the mismatch below: both defects have the identical
        # bookkeeping fix (rewrite changed_files to match the actual diff).
        return gate_failure("changed-files-mismatch", "developer changed_files is malformed (expected a list of paths)", result, changed_evidence)
    if actual_changed != set(reported_files):
        return gate_failure("changed-files-mismatch", "developer changed_files does not match git evidence", result, changed_evidence)

    validation_failure = _validation_status(result.get("validation"))
    if validation_failure:
        return gate_failure("validation", validation_failure, result, changed_evidence)
    # Same bar as the drift/review artifacts: a real, non-empty file inside the
    # run. A bare .exists() check let an empty placeholder satisfy this gate.
    if not artifact_exists(repo, slice_artifact_dir, result, "validation", "validation-summary.md"):
        return gate_failure("validation", "validation-summary.md is missing or empty", result, changed_evidence)

    drift_verdict = str(object_field(result, "drift_audit").get("verdict", "")).upper()
    if drift_verdict != "PASS":
        return gate_failure("drift", f"drift audit verdict is not PASS: {drift_verdict or 'missing'}", result, changed_evidence)
    if not artifact_exists(repo, slice_artifact_dir, result, "drift_audit", "drift-audit.md"):
        return gate_failure("drift", "drift audit artifact is missing", result, changed_evidence)

    review_verdict = str(object_field(result, "code_review").get("verdict", "")).upper()
    if review_verdict != "PASS":
        return gate_failure("review", f"code review verdict is not PASS: {review_verdict or 'missing'}", result, changed_evidence)
    if not artifact_exists(repo, slice_artifact_dir, result, "code_review", "code-review.md"):
        return gate_failure("review", "code review artifact is missing", result, changed_evidence)

    if not result.get("residual_findings") and (
        _artifact_has_unledgered_finding_shape(slice_artifact_dir / "drift-audit.md")
        or _artifact_has_unledgered_finding_shape(slice_artifact_dir / "code-review.md")
    ):
        return gate_failure(
            "residual-ledger-mismatch",
            "audit/review artifact contains a non-empty findings or observations shape but residual_findings is empty",
            result,
            changed_evidence,
        )

    # EXEMPTION: a context-budget repair explicitly instructs the developer to
    # condense and reword ledger items so the cumulative context fits the next
    # planned slice's launch budget (see prompts.py's context-budget repair
    # stanza); mechanical retention would wedge that required rewrite even
    # under field-superset matching (rewording changes existing field values,
    # not just adding new ones), so this check does not apply to the round
    # that follows one.
    if last_repair_signature != "context-budget":
        retention_failure = _ledger_retention_failure(slice_artifact_dir, result)
        if retention_failure:
            reason, missing_items = retention_failure
            return gate_failure(
                "ledger-retention",
                reason,
                result,
                changed_evidence,
                repair_payload={"missing_ledger_items": missing_items},
            )

    # Independence (delegating drift-audit and code-review to a separate model)
    # is a degradable *preference* by default: a slice audited locally by a
    # single model is a valid accepted outcome, so reviewer delegation is reported
    # for visibility (see `summarize`) but does not gate acceptance. A slice may
    # opt in to mechanical enforcement with `Independent audit required: yes` in
    # its Risk Flags, which re-arms the reviewer-launch verification below as a
    # blocking gate for that slice only.
    if plan_slice.independent_audit_required:
        if not reviewer_tools:
            # The plan demands mechanical proof of an independent audit, but the
            # operator made no reviewer available this run, so PM cannot verify
            # one. This is a config mismatch the developer cannot repair, so
            # stop terminally rather than spending the repair budget first.
            return gate_failure(
                "reviewer-unavailable",
                "slice marks 'Independent audit required: yes' but no reviewer tool was made available for this run "
                "(configure --reviewer-tools); PM cannot verify an independent audit",
                result,
                changed_evidence,
            )
        current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
        expected_snapshot = current.get("reviewer_policy") if current and current.get("slice_id") == plan_slice.slice_id else None
        if not isinstance(expected_snapshot, dict):
            for entry in reversed(state.get("slices", [])):
                if isinstance(entry, dict) and entry.get("slice_id") == plan_slice.slice_id and isinstance(entry.get("reviewer_policy"), dict):
                    expected_snapshot = entry["reviewer_policy"]
                    break
        reviewer_failure = reviewer_evidence_failure(slice_artifact_dir, reviewer_tools, expected_snapshot)
        if reviewer_failure:
            return gate_failure("reviewer-evidence", reviewer_failure, result, changed_evidence)

    commit = result.get("commit") if isinstance(result.get("commit"), dict) else {}
    if state.get("policy", {}).get("commit_required", True):
        if not commit.get("requested") or not commit.get("created") or not commit.get("hash"):
            return gate_failure("commit-missing", "required commit was not created", result, changed_evidence)
        if meaningful_status_lines(after_status):
            return gate_failure("dirty-worktree", "post-commit worktree is dirty outside .ai-pm/", result, changed_evidence)
        # Integrity gates run on git evidence alone, before any comparison with
        # the self-reported hash: a truthful report of a reset-to-unrelated
        # HEAD must fail here, not pass because the strings happen to match.
        if not after_head or after_head == before_head:
            return gate_failure("integrity-head", "required commit did not advance HEAD", result, changed_evidence)
        if not commit_is_descendant(repo, before_head, after_head):
            return gate_failure("integrity-head", "current HEAD is not descended from the slice starting commit", result, changed_evidence)
        reported_hash = str(commit["hash"])
        if reported_hash != after_head:
            # An abbreviated-but-correct hash and an outright-wrong hash both
            # differ from the proven full HEAD as strings and land here; the
            # only difference is the message we record.
            if is_full_commit_hash(reported_hash):
                record_reason = (
                    "developer reported an incorrect commit hash, but PM proved the slice commit from local git "
                    "evidence and corrected developer-result.json"
                )
                decision_reason = "all gates passed; corrected reported commit hash to current HEAD"
            else:
                record_reason = "developer reported an abbreviated commit hash; PM corrected it to the full current HEAD"
                decision_reason = "all gates passed; expanded reported commit hash to full current HEAD"
            return _apply_commit_hash_reconciliation(
                result,
                result_path,
                slice_artifact_dir,
                reported_hash=reported_hash,
                corrected_hash=after_head,
                record_reason=record_reason,
                decision_reason=decision_reason,
                actual_changed=changed_evidence,
            )

    return GateDecision("pass", "all gates passed", result, changed_evidence)

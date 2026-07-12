from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .constants import ORCHESTRATOR_STATUSES, SCHEMA_VERSION
from .git_ops import (
    changed_files_between,
    commit_is_descendant,
    is_full_commit_hash,
    meaningful_status_lines,
    unauthorized_files,
)
from .models import GateDecision, McError, PlanSlice
from .utils import utc_now


# Failure-signature taxonomy: every non-pass classification MC makes itself
# carries one of these coarse, stable labels. The repair loop keys its circuit
# breaker on them and the repair-prompt renderer selects its correction stanza
# by them, so they are the single source of truth for which violations are
# repairable (steer the live orchestrator session) versus terminal
# (integrity/trust breach — do not steer from a false-belief context; stop for
# a human). Re-verification after a repair re-runs the *identical* gate, so a
# repairable classification can never let a bad slice through — it only grants
# another chance to satisfy the same gate.
REPAIRABLE_SIGNATURES = frozenset(
    {
        "validation",
        "drift",
        "review",
        "worker-evidence",
        "unauthorized-files",
        "changed-files-mismatch",
        "result-malformed",
        "commit-missing",
        "dirty-worktree",
        "orchestrator-repairable",
    }
)
TERMINAL_SIGNATURES = frozenset(
    {
        "integrity-head",
        "slice-id-mismatch",
        # An opt-in slice ("Independent audit required: yes") with no worker made
        # available cannot be satisfied by steering the orchestrator — it is an
        # operator/plan configuration mismatch — so it stops for a human at once
        # rather than burning the repair budget.
        "worker-unavailable",
    }
)


def gate_failure(
    signature: str,
    reason: str,
    result: dict[str, Any] | None = None,
    actual_changed_files: tuple[str, ...] = (),
) -> GateDecision:
    """Build a non-pass GateDecision whose status follows the signature taxonomy."""
    if signature in TERMINAL_SIGNATURES:
        status = "needs-human"
    elif signature in REPAIRABLE_SIGNATURES:
        status = "repairable"
    else:
        raise McError(f"unknown gate failure signature: {signature}")
    return GateDecision(status, reason, result, actual_changed_files, signature)


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
    (slice_artifact_dir / "mc-reconciliation.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (slice_artifact_dir / "mc-reconciliation.md").write_text(
        "# MC Reconciliation\n\n"
        f"- Field: `{field}`\n"
        f"- Reported value: `{reported_value}`\n"
        f"- Corrected value: `{corrected_value}`\n"
        f"- Reason: {reason}\n",
        encoding="utf-8",
    )


def write_orchestrator_result(path: Path, result: dict[str, Any]) -> None:
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_orchestrator_result(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise McError(f"orchestrator result missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise McError(f"invalid orchestrator result: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise McError(f"orchestrator result is not an object: {path}")
    return data


def _within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def artifact_exists(repo: Path, slice_artifact_dir: Path, result: dict[str, Any], field: str, default_name: str) -> bool:
    """Return True only for a real, non-empty evidence file inside the run.

    A verdict string in orchestrator-result.json is not enough on its own: MC
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


def worker_evidence_failure(
    slice_artifact_dir: Path,
    worker_tools: tuple[str, ...],
    expected_snapshot: dict[str, Any] | None,
) -> str | None:
    """Return a gate-failure reason if a required worker tool has no mechanical launch evidence.

    Narration and executable-name matching are insufficient. Require the
    plan-mandated worker-evidence.md, MC's authoritative worker-policy.json, a
    manifest launch contract whose policy digest and normalized semantic
    fields match that policy, and successful helper-owned process status. This
    prevents both mislabeled substitutes and correctly named harnesses launched
    with unvalidated model/access/role flags.
    """
    if not worker_tools:
        return None
    tools_label = ", ".join(worker_tools)
    required = {tool.lower() for tool in worker_tools}
    evidence_path = slice_artifact_dir / "worker-evidence.md"
    if not evidence_path.is_file() or evidence_path.stat().st_size == 0:
        return f"required worker tool(s) ({tools_label}) have no worker-evidence.md for this slice"
    policy_path = slice_artifact_dir / "worker-policy.json"
    if not policy_path.is_file():
        return (
            "required worker launch policy is missing; MC cannot verify that worker tool, model, access, and slice identity "
            "were validated before launch"
        )
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "worker-policy.json is not valid JSON"
    if not isinstance(policy, dict):
        return "worker-policy.json must contain a JSON object"
    policy_sha256 = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    if not isinstance(expected_snapshot, dict):
        return "MC run state has no immutable worker-policy snapshot for this slice"
    if policy_sha256 != expected_snapshot.get("sha256") or policy != expected_snapshot.get("policy"):
        return (
            "worker-policy.json changed after MC created the slice policy. Restore the exact MC-generated policy; "
            "do not widen tools, model, effort, role, access, repository, or authorized files from the orchestrator session"
        )

    summary_path = slice_artifact_dir / "worker-runs-summary.json"
    if not summary_path.is_file():
        return (
            f"required worker tool(s) ({tools_label}) were never launched: worker-runs-summary.json is missing "
            "(no worker_jobs.py run directory was created)"
        )
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "worker-runs-summary.json is not valid JSON"
    runs = summary.get("runs") if isinstance(summary, dict) else []
    if not isinstance(runs, list):
        runs = []
    worker_artifact_root_text = str(policy.get("worker_artifact_root") or "")
    worker_artifact_root = Path(worker_artifact_root_text).resolve() if worker_artifact_root_text else None
    any_worker = False
    matched_tools: set[str] = set()
    contracted_tools: set[str] = set()
    successful_tools: set[str] = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("workers"):
            any_worker = True
        manifest_workers = object_field(run.get("manifest") or {}, "workers") if isinstance(run.get("manifest"), dict) else {}
        tool_by_label: dict[str, str] = {}
        for label, entry in (manifest_workers.items() if isinstance(manifest_workers, dict) else ()):
            if not isinstance(entry, dict) or str(entry.get("tool", "")).lower() not in required:
                continue
            entry_tool = str(entry.get("tool", "")).lower()
            matched_tools.add(entry_tool)
            contract = entry.get("launch_contract")
            if not isinstance(contract, dict):
                continue
            if contract.get("status") != "pass" or contract.get("policy_sha256") != policy_sha256:
                continue
            if contract.get("slice_id") != policy.get("slice_id") or contract.get("plan_sha256") != policy.get("plan_sha256"):
                continue
            contract_tool = str(contract.get("tool", "")).lower()
            if contract_tool != entry_tool or contract_tool not in required:
                continue
            required_model = str(policy.get("required_model") or "default")
            required_effort = str(policy.get("required_effort") or "default")
            if contract.get("model") != required_model:
                continue
            if contract.get("effort") != required_effort:
                continue
            if contract.get("access") not in policy.get("allowed_access", []):
                continue
            if contract.get("role") not in policy.get("allowed_roles", []):
                continue
            if contract.get("repo_path") != policy.get("repo_path") or contract.get("cwd") != policy.get("repo_path"):
                continue
            # A manifest/status pair alone is just JSON on disk — the
            # orchestrator has full write access to its own worker-runs tree
            # and could hand-author a matching manifest entry, launch
            # contract, and completed status without ever invoking
            # worker_jobs.py or a real harness process. start_tracked_worker
            # always records a positive subprocess pid and always creates
            # outfile/errfile via `.open("wb")` before the child process
            # starts, regardless of whether the tool produces output, and
            # always writes them inside the policy's worker_artifact_root.
            # Requiring that real filesystem footprint closes the
            # zero-effort forgery (three hand-written JSON files, no other
            # artifacts) without depending on content that could itself be
            # fabricated.
            if not isinstance(entry.get("pid"), int) or entry.get("pid", 0) <= 0:
                continue
            outfile_text, errfile_text = entry.get("outfile"), entry.get("errfile")
            if not isinstance(outfile_text, str) or not isinstance(errfile_text, str):
                continue
            outfile, errfile = Path(outfile_text), Path(errfile_text)
            if not outfile.is_file() or not errfile.is_file():
                continue
            if worker_artifact_root is not None and not (
                _within(outfile, worker_artifact_root) and _within(errfile, worker_artifact_root)
            ):
                continue
            contracted_tools.add(contract_tool)
            tool_by_label[str(label)] = contract_tool
        # Launch alone is not enough: a required worker that crashed on start
        # (or is still running at finalize) proves nothing was delegated. The
        # status payloads come from worker_jobs.py's own *-status.json files,
        # so like the tool name they are mechanically derived, not narrated.
        for status in run.get("workers") or ():
            if not isinstance(status, dict):
                continue
            if str(status.get("label", "")) not in tool_by_label:
                continue
            if str(status.get("state", "")).lower() == "completed" and status.get("returncode") == 0:
                successful_tools.add(tool_by_label[str(status.get("label", ""))])
    if not any_worker:
        return (
            f"required worker tool(s) ({tools_label}) were never launched: a worker_jobs.py run directory was "
            "created but no worker was started in it (worker-runs-summary.json has no worker entries)"
        )
    missing_tools = sorted(required - matched_tools)
    if missing_tools:
        return (
            f"required worker tool(s) were never actually invoked: {', '.join(missing_tools)}. worker_jobs.py recorded "
            "worker run(s), but none used matching executable(s) — check for labels that hide a different command"
        )
    missing_contracts = sorted(required - contracted_tools)
    if missing_contracts:
        return (
            f"required worker executable(s) ran without matching validated launch contracts: {', '.join(missing_contracts)}. "
            "No such worker has a passing deterministic launch contract matching the immutable MC policy and current "
            "worker-policy.json, backed by a real subprocess pid and outfile/errfile actually present under the policy "
            "worker_artifact_root. A hand-authored manifest/status entry does not satisfy the gate. Do not invoke the "
            "harness directly or use a raw worker command. Read any <label>-request-feedback.md artifact, correct the "
            "semantic worker request, and launch it through worker_jobs.py launch --policy <worker-policy.json> "
            "--request <worker-request.json>"
        )
    missing_success = sorted(required - successful_tools)
    if missing_success:
        return (
            f"required worker tool(s) never completed successfully: {', '.join(missing_success)}. Matching worker run(s) "
            "did not finish with state 'completed' and returncode 0 — crashed, cancelled, or running workers do not satisfy the gate"
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

    Only ever called after every other gate has passed and MC has proven the
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
    write_orchestrator_result(result_path, result)
    write_reconciliation_artifact(
        slice_artifact_dir,
        field="commit.hash",
        reported_value=reported_hash,
        corrected_value=corrected_hash,
        reason=record_reason,
    )
    return GateDecision("pass", decision_reason, result, actual_changed)


def verify_gate(
    repo: Path,
    state: dict[str, Any],
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    before_head: str | None,
    after_head: str | None,
    after_status: str,
    worker_tools: tuple[str, ...] = (),
) -> GateDecision:
    result_path = slice_artifact_dir / "orchestrator-result.json"
    if not result_path.is_file():
        # Absence is not a content defect: the session may have died or timed
        # out without answering, which the runner handles as its own condition
        # (relaunch/stop), not as a steerable in-session repair.
        return GateDecision("blocked", f"orchestrator result missing: {result_path}")
    try:
        result = load_orchestrator_result(result_path)
    except McError as exc:
        return gate_failure("result-malformed", str(exc))

    if result.get("schema_version") != SCHEMA_VERSION:
        return gate_failure("result-malformed", "orchestrator result schema_version is missing or unsupported", result)
    if result.get("slice_id") != plan_slice.slice_id:
        # The orchestrator worked (or reported on) the wrong slice: continuing
        # to steer from that context would build on a false belief about which
        # contract is in force.
        return gate_failure("slice-id-mismatch", "orchestrator result slice_id does not match selected slice", result)

    status_value = str(result.get("status", "")).lower()
    if status_value not in ORCHESTRATOR_STATUSES:
        return gate_failure("result-malformed", f"orchestrator result status is invalid: {result.get('status')}", result)
    if status_value != "pass":
        # The orchestrator's own self-report is respected as-is: its
        # `repairable` earns a repair round, and its considered stops
        # (needs-human/fail/blocked) stay terminal with no MC signature.
        signature = "orchestrator-repairable" if status_value == "repairable" else ""
        return GateDecision(status_value, f"orchestrator reported {status_value}", result, signature=signature)

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
        return gate_failure("changed-files-mismatch", "orchestrator changed_files is malformed (expected a list of paths)", result, changed_evidence)
    if actual_changed != set(reported_files):
        return gate_failure("changed-files-mismatch", "orchestrator changed_files does not match git evidence", result, changed_evidence)

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

    # Independence (delegating drift-audit and code-review to a separate model)
    # is a degradable *preference* by default: a slice audited locally by a
    # single model is a valid accepted outcome, so worker delegation is reported
    # for visibility (see `summarize`) but does not gate acceptance. A slice may
    # opt in to mechanical enforcement with `Independent audit required: yes` in
    # its Risk Flags, which re-arms the worker-launch verification below as a
    # blocking gate for that slice only.
    if plan_slice.independent_audit_required:
        if not worker_tools:
            # The plan demands mechanical proof of an independent audit, but the
            # operator made no worker available this run, so MC cannot verify
            # one. This is a config mismatch the orchestrator cannot repair, so
            # stop terminally rather than spending the repair budget first.
            return gate_failure(
                "worker-unavailable",
                "slice marks 'Independent audit required: yes' but no worker tool was made available for this run "
                "(configure --worker-tools); MC cannot verify an independent audit",
                result,
                changed_evidence,
            )
        current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
        expected_snapshot = current.get("worker_policy") if current and current.get("slice_id") == plan_slice.slice_id else None
        if not isinstance(expected_snapshot, dict):
            for entry in reversed(state.get("slices", [])):
                if isinstance(entry, dict) and entry.get("slice_id") == plan_slice.slice_id and isinstance(entry.get("worker_policy"), dict):
                    expected_snapshot = entry["worker_policy"]
                    break
        worker_failure = worker_evidence_failure(slice_artifact_dir, worker_tools, expected_snapshot)
        if worker_failure:
            return gate_failure("worker-evidence", worker_failure, result, changed_evidence)

    commit = result.get("commit") if isinstance(result.get("commit"), dict) else {}
    if state.get("policy", {}).get("commit_required", True):
        if not commit.get("requested") or not commit.get("created") or not commit.get("hash"):
            return gate_failure("commit-missing", "required commit was not created", result, changed_evidence)
        if meaningful_status_lines(after_status):
            return gate_failure("dirty-worktree", "post-commit worktree is dirty outside .ai-mc/", result, changed_evidence)
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
                    "orchestrator reported an incorrect commit hash, but MC proved the slice commit from local git "
                    "evidence and corrected orchestrator-result.json"
                )
                decision_reason = "all gates passed; corrected reported commit hash to current HEAD"
            else:
                record_reason = "orchestrator reported an abbreviated commit hash; MC corrected it to the full current HEAD"
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

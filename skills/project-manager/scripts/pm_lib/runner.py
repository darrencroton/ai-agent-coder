from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .constants import DEFAULT_MAX_REPAIR_ATTEMPTS, KNOWN_UNATTENDED_HARNESS_COMMANDS, RUN_STOP_STATUSES
from .gates import verify_gate
from .git_ops import git, git_head, git_status_text, require_clean_worktree, write_git_diff
from .models import GateDecision, PmError, PlanSlice
from .observation import _current_adapter, _slice_artifact_dir, wait_observing
from .plan import eligibility
from .profiles import (
    current_allow_unattended_default,
    freeze_run_launch_config,
    parse_reviewer_tools,
    query_profile_model_identity,
    resolve_current_harness_command,
    resolve_harness_command,
)
from .runtime import (
    cancel_run_reviewers,
    capture_developer_transcript,
    capture_reviewer_runs_summary,
    ensure_slice_runtime_dirs,
    extract_operational_hints,
    render_developer_prompt,
    render_repair_prompt,
    prior_slice_context_integrity_failure,
    projected_prior_slice_context_budget_failure,
    slice_dir_name,
    tmux_session_name,
    write_reviewer_policy,
    reviewer_policy_snapshot,
    write_prior_slice_context,
)
from .state import (
    activate_controller_state,
    append_operational_event,
    approved_slice_ids,
    current_slice_state,
    default_repair_state,
    idle_status_after_pass,
    load_run,
    normalize_stop_status,
    relative_artifact_path,
    repair_state,
    reset_slice_pause_counters,
    slice_entry_from_gate,
    update_state_for_stop,
    write_run,
)
from .tmux_adapter import TmuxHarnessAdapter
from .utils import utc_now


def _capture_git_evidence(repo: Path, slice_artifact_dir: Path, attempt: int, before_head: str | None) -> tuple[str | None, str]:
    after_head = git_head(repo)
    after_status = git_status_text(repo)
    (slice_artifact_dir / f"git-status-after-attempt-{attempt}.txt").write_text(after_status, encoding="utf-8")
    (slice_artifact_dir / "git-status-after.txt").write_text(after_status, encoding="utf-8")
    write_git_diff(repo, before_head, after_head, slice_artifact_dir / "git-diff.patch")
    return after_head, after_status


def _capture_failure_evidence(
    adapter: TmuxHarnessAdapter,
    *,
    session_name: str,
    harness_name: str,
    repo: Path,
    developer_session_id: str | None,
    slice_artifact_dir: Path,
    attempt: int,
    before_head: str | None,
) -> None:
    """Best-effort evidence capture for paths already handling a failure."""
    for capture_step in (
        lambda: adapter.capture(session_name, slice_artifact_dir / "pane-capture.txt"),
        lambda: capture_developer_transcript(harness_name, repo, developer_session_id, slice_artifact_dir),
        lambda: capture_reviewer_runs_summary(slice_artifact_dir),
        lambda: _capture_git_evidence(repo, slice_artifact_dir, attempt, before_head),
    ):
        try:
            capture_step()
        except Exception:
            continue


def _check_runtime_start_preconditions(repo: Path, state: dict[str, Any], plan_slice: PlanSlice, run_json: Path) -> bool:
    runnable, reasons = eligibility(plan_slice, approved_slice_ids(state))
    if not runnable:
        update_state_for_stop(run_json, state, "needs-human", "; ".join(reasons))
        print(f"Next slice: {plan_slice.slice_id} - {plan_slice.title}")
        print("Eligibility: blocked")
        for reason in reasons:
            print(f"- {reason}")
        return False

    try:
        require_clean_worktree(repo)
    except PmError as exc:
        update_state_for_stop(run_json, state, "needs-human", str(exc))
        print(f"{plan_slice.slice_id} stopped: {exc}")
        return False

    current_branch = git(repo, "branch", "--show-current") or "DETACHED"
    if current_branch != state.get("branch"):
        reason = f"branch changed since init: expected {state.get('branch')!r}, found {current_branch!r}"
        update_state_for_stop(run_json, state, "needs-human", reason)
        print(f"{plan_slice.slice_id} stopped: {reason}")
        return False
    return True


def _attempt_for_slice(state: dict[str, Any], plan_slice: PlanSlice) -> int:
    return 1 + sum(1 for entry in state.get("slices", []) if entry.get("slice_id") == plan_slice.slice_id)


def resolve_repair_action(
    repair: dict[str, Any],
    signature: str,
    session_alive: bool,
    max_repairs: int,
    gate: GateDecision,
    slice_id: str,
) -> tuple[str, GateDecision | None]:
    """Shared repair-decision core for both execution paths.

    The deterministic-batch loop (execute_slice) and the model-supervised
    finalize path must make the identical budget / circuit-breaker / mode
    decision from the same persisted repair state; this is the single copy of
    that decision so the two paths cannot drift apart.

    Returns ("terminal", terminal_gate) when the repair loop must end, or
    (mode, None) with mode in {"in-session", "fresh-session", "relaunch"}
    after updating `repair` in place:

    - budget exhausted -> terminal blocked.
    - same signature failing a third consecutive time with a live session ->
      terminal needs-human (in-session nudge, then one fresh session, then a
      human).
    - dead session -> "relaunch": consumes a round but is a runner condition,
      not a circuit-breaker step, so the breaker state is untouched.
    - first failure of a signature -> "in-session" nudge into the live session.
    - second consecutive failure of the same signature -> one "fresh-session"
      escalation, on the theory the session is anchored on a wrong premise.
    """
    if repair["round"] >= max_repairs:
        return "terminal", GateDecision(
            "blocked",
            f"repair budget exhausted for {slice_id} "
            f"({repair['round']}/{max_repairs} repairs used); last gate failure: {gate.reason}",
            gate.result,
            gate.actual_changed_files,
            signature,
        )
    streak = int(repair["signature_streak"]) + 1 if signature == repair["last_signature"] else 1
    if session_alive and streak >= 3:
        return "terminal", GateDecision(
            "needs-human",
            f"circuit breaker: gate signature {signature!r} failed {streak} consecutive times "
            f"(after an in-session repair and a fresh-session retry); last gate failure: {gate.reason}",
            gate.result,
            gate.actual_changed_files,
            signature,
        )
    round_number = int(repair["round"]) + 1
    if not session_alive:
        repair["round"] = round_number
        return "relaunch", None
    if streak == 1:
        repair.update(round=round_number, last_signature=signature, signature_streak=1)
        return "in-session", None
    repair.update(round=round_number, last_signature=signature, signature_streak=2)
    return "fresh-session", None


def reclassify_high_confidence_transient_stop(gate: GateDecision, hints: list[dict[str, Any]]) -> GateDecision:
    """Route only a narrow, high-confidence transient terminal report to repair.

    This changes retry policy, never acceptance: the complete deterministic
    gate still has to pass after the bounded repair loop.
    """
    if gate.status not in {"blocked", "fail"}:
        return gate
    matched = any(
        isinstance(hint, dict)
        and hint.get("kind") == "service_unavailable"
        and hint.get("subtype") == "transient"
        and hint.get("confidence") == "high"
        and hint.get("hard_stop") is False
        and hint.get("recovery_guidance") == "bounded-retry"
        for hint in hints
    )
    if not matched:
        return gate
    return GateDecision(
        "repairable",
        f"developer reported {gate.status}, but current-attempt evidence shows a high-confidence transient "
        "service-unavailable condition",
        gate.result,
        gate.actual_changed_files,
        "transient-service-unavailable",
    )


def _announce_launch(adapter: TmuxHarnessAdapter, args: argparse.Namespace) -> None:
    if getattr(args, "allow_profile_command", False) and not getattr(args, "harness_command", None):
        print(f"Using PM profile command for harness {adapter.harness_name!r}: {adapter.command!r}")
    if adapter.allow_unattended_default and not adapter.command_override and adapter.harness_name in KNOWN_UNATTENDED_HARNESS_COMMANDS:
        print(
            f"Using known unattended-safe default for harness {adapter.harness_name!r}: {adapter.command!r} "
            "(per-action approval is disabled; PM's post-hoc gates become the safety boundary for this run)"
        )


def _reap_stale_sessions(adapter: TmuxHarnessAdapter, run_dir: Path, run_id_value: str) -> list[dict[str, str]]:
    reaped: list[dict[str, str]] = []
    stale_dir = run_dir / "stale-sessions"
    for session_name in adapter.sessions_with_prefix(f"pm_{run_id_value}_"):
        safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in session_name)
        capture_path = stale_dir / f"{safe_name}.txt"
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        adapter.capture(session_name, capture_path)
        adapter.force_stop(session_name)
        reaped.append({"tmux_session": session_name, "evidence_path": str(capture_path)})
    return reaped


def start_model_supervised_slice(
    args: argparse.Namespace,
    repo: Path,
    state: dict[str, Any],
    plan_slice: PlanSlice,
    run_dir: Path,
    supervision_mode: str = "model-supervised",
) -> dict[str, Any]:
    run_json = run_dir / "run.json"
    if isinstance(state.get("current_slice"), dict):
        raise PmError("run already has a current slice; finalize or stop it before starting another")
    if not _check_runtime_start_preconditions(repo, state, plan_slice, run_json):
        return {"started": False, "status": state.get("status"), "reason": state.get("stop_reason")}

    args = freeze_run_launch_config(args, state)
    harness_name = state["harness"]["name"]
    configured_reviewer_tools = parse_reviewer_tools(getattr(args, "reviewer_tools", None))
    harness_model = getattr(args, "harness_model", None)
    harness_effort = getattr(args, "harness_effort", None)
    checked_at = utc_now()
    harness_identity: dict[str, Any] | None = None
    if harness_model:
        harness_identity = query_profile_model_identity(harness_name, harness_model)
        state.setdefault("harness", {})["model_requested"] = harness_model
        state["harness"]["model_identity"] = {
            **(harness_identity or {"requested": harness_model, "resolved_id": harness_model, "display_name": ""}),
            "catalog_verified": harness_identity is not None,
            "checked_at": checked_at,
            "slice_id": plan_slice.slice_id,
        }
    else:
        # Do not let a prior slice's verified model identity masquerade as
        # current when this run intentionally uses an ambient default.
        state.setdefault("harness", {}).pop("model_requested", None)
        state["harness"]["model_identity"] = {
            "requested": None,
            "resolved_id": None,
            "display_name": "",
            "catalog_verified": False,
            "checked_at": checked_at,
            "slice_id": plan_slice.slice_id,
        }
    if harness_effort:
        state.setdefault("harness", {})["effort_requested"] = harness_effort
    else:
        state.setdefault("harness", {}).pop("effort_requested", None)

    slice_artifact_dir = run_dir / "slices" / slice_dir_name(plan_slice)
    credential_warnings = ensure_slice_runtime_dirs(slice_artifact_dir, configured_reviewer_tools, harness_name)
    for warning in credential_warnings:
        print(f"warning: {warning}")
    policy_path = write_reviewer_policy(
        state,
        plan_slice,
        slice_artifact_dir,
        configured_reviewer_tools,
        getattr(args, "reviewer_model", None),
        getattr(args, "reviewer_effort", None),
    )
    reviewer_identities: dict[str, dict[str, Any]] = {}
    reviewer_model = getattr(args, "reviewer_model", None)
    for tool in configured_reviewer_tools:
        if reviewer_model:
            identity = query_profile_model_identity(tool, reviewer_model)
            reviewer_identities[tool] = {
                **(identity or {"requested": reviewer_model, "resolved_id": reviewer_model, "display_name": ""}),
                "catalog_verified": identity is not None,
                "checked_at": checked_at,
                "slice_id": plan_slice.slice_id,
            }
        else:
            reviewer_identities[tool] = {
                "requested": None,
                "resolved_id": None,
                "display_name": "",
                "catalog_verified": False,
                "checked_at": checked_at,
                "slice_id": plan_slice.slice_id,
            }
    (slice_artifact_dir / "model-identities.json").write_text(
        json.dumps(
            {
                "developer": state.get("harness", {}).get("model_identity"),
                "reviewers": reviewer_identities,
                "recorded_at": utc_now(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    before_head = git_head(repo)
    prior_context_path, prior_context_sha256 = write_prior_slice_context(state, plan_slice, slice_artifact_dir, before_head)
    prompt_path = slice_artifact_dir / "prompt.md"
    prompt_path.write_text(
        render_developer_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            configured_reviewer_tools,
            getattr(args, "reviewer_model", None),
            getattr(args, "reviewer_effort", None),
        ),
        encoding="utf-8",
    )

    max_attempts = int(state.get("policy", {}).get("max_repair_attempts", 1)) + 1
    attempt = _attempt_for_slice(state, plan_slice)
    if attempt > max_attempts:
        reason = f"repair attempt cap exhausted for {plan_slice.slice_id}: {attempt - 1}/{max_attempts}"
        update_state_for_stop(run_json, state, "blocked", reason)
        return {"started": False, "status": "blocked", "reason": reason}

    developer_session_id = str(uuid.uuid4()) if harness_name == "claude" else None
    adapter = TmuxHarnessAdapter(
        harness_name,
        resolve_harness_command(args, repo, state, developer_session_id),
        getattr(args, "allow_unattended_default", False),
        configured_reviewer_tools,
        expected_model_display=(harness_identity or {}).get("display_name"),
    )
    reaped_stale_sessions = _reap_stale_sessions(adapter, run_dir, str(state["run_id"]))
    _announce_launch(adapter, args)

    started_at = utc_now()
    before_status = git_status_text(repo)
    (slice_artifact_dir / f"git-status-before-attempt-{attempt}.txt").write_text(before_status, encoding="utf-8")
    (slice_artifact_dir / "git-status-before.txt").write_text(before_status, encoding="utf-8")
    session_name = tmux_session_name(state["run_id"], plan_slice, attempt)
    # Seed the repair session generation from the real attempt number, not a
    # constant 1: a rerun of a previously failed slice starts at attempt 2, and
    # a later fresh-session relaunch increments the generation to name its new
    # session/artifacts — seeding at 1 would relaunch as "_a2" and collide with
    # this attempt's own names.
    initial_repair = default_repair_state()
    initial_repair["session_generation"] = attempt
    state["status"] = "running"
    state["current_slice"] = current_slice_state(
        repo,
        plan_slice,
        slice_artifact_dir,
        session_name,
        attempt,
        started_at,
        before_head,
        developer_session_id,
        # Persisted so finalize-slice (a separate invocation) verifies the
        # reviewer-evidence gate from the slice's real requirement instead of
        # silently dropping it when --reviewer-tools is not re-supplied.
        configured_reviewer_tools,
        initial_repair,
        reviewer_policy_snapshot(policy_path),
        prior_slice_context={
            "path": relative_artifact_path(repo, prior_context_path),
            "sha256": prior_context_sha256,
        },
        launch_config={
            "harness_command": getattr(args, "harness_command", None),
            "harness_model": getattr(args, "harness_model", None),
            "harness_effort": getattr(args, "harness_effort", None),
            "reviewer_tools": list(configured_reviewer_tools),
            "reviewer_model": getattr(args, "reviewer_model", None),
            "reviewer_effort": getattr(args, "reviewer_effort", None),
            "allow_profile_command": bool(getattr(args, "allow_profile_command", False)),
            "allow_unattended_default": bool(getattr(args, "allow_unattended_default", False)),
        },
    )
    state.setdefault("supervision", {})["mode"] = supervision_mode
    reset_slice_pause_counters(state)
    state["stop_reason"] = None
    activate_controller_state(run_json, state)

    try:
        result_path = slice_artifact_dir / "developer-result.json"
        if result_path.exists():
            result_path.unlink()
        adapter.start(repo, session_name, slice_artifact_dir, run_json, Path(state["plan_path"]), plan_slice)
        adapter.send_prompt(session_name, prompt_path)
    except Exception as exc:
        _capture_failure_evidence(
            adapter,
            session_name=session_name,
            harness_name=harness_name,
            repo=repo,
            developer_session_id=developer_session_id,
            slice_artifact_dir=slice_artifact_dir,
            attempt=attempt,
            before_head=before_head,
        )
        adapter.force_stop(session_name)
        state["current_slice"] = None
        detail = str(exc).strip() or repr(exc)
        update_state_for_stop(run_json, state, "failed", f"failed to start model-supervised slice: {detail}")
        raise

    return {
        "started": True,
        "slice_id": plan_slice.slice_id,
        "title": plan_slice.title,
        "attempt": attempt,
        "tmux_session": session_name,
        "artifact_dir": relative_artifact_path(repo, slice_artifact_dir),
        "prompt_path": relative_artifact_path(repo, prompt_path),
        "before_head": before_head,
        "reaped_stale_sessions": reaped_stale_sessions,
    }


def _finalize_terminal(
    adapter: TmuxHarnessAdapter,
    *,
    repo: Path,
    run_json: Path,
    state: dict[str, Any],
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    session_name: str,
    started_at: str,
    before_head: str | None,
    reviewer_tools: tuple[str, ...],
    repair: dict[str, Any],
    terminal_gate: GateDecision,
) -> dict[str, Any]:
    """Record a slice's terminal outcome: the single end-of-slice transition.

    Both execution paths finish every slice through this function — pass,
    integrity/trust stops, budget/breaker terminals, and the batch driver's
    forced terminals (timeout, interrupt, refused repair delivery). It tears
    down the session, appends the slice entry, clears current_slice, and
    writes the pass/stop state.
    """
    adapter.force_stop(session_name)
    cancel_run_reviewers(run_json.parent)
    entry = slice_entry_from_gate(
        repo,
        plan_slice,
        slice_artifact_dir,
        started_at,
        terminal_gate,
        before_head,
        reviewer_tools,
        repair=dict(repair),
        reviewer_policy=(state.get("current_slice") or {}).get("reviewer_policy"),
    )
    state["slices"].append(entry)
    state["current_slice"] = None
    if terminal_gate.status == "pass":
        state["status"] = idle_status_after_pass(state)
        state["stop_reason"] = None
        write_run(run_json, state)
        return {"finalized": True, "status": "pass", "reason": terminal_gate.reason, "entry": entry}
    update_state_for_stop(run_json, state, normalize_stop_status(terminal_gate.status), terminal_gate.reason)
    return {"finalized": True, "status": terminal_gate.status, "reason": terminal_gate.reason, "entry": entry}


def finalize_model_supervised_slice(
    args: argparse.Namespace,
    repo: Path,
    state: dict[str, Any],
    plan_slice: PlanSlice,
    run_dir: Path,
) -> dict[str, Any]:
    current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
    if not current:
        raise PmError("run has no current slice to finalize")
    if current.get("slice_id") != plan_slice.slice_id:
        raise PmError(f"current slice does not match next plan slice: {current.get('slice_id')} != {plan_slice.slice_id}")

    artifact_value = current.get("artifact_dir")
    if not artifact_value:
        raise PmError("current slice has no artifact_dir")
    slice_artifact_dir = Path(str(artifact_value))
    if not slice_artifact_dir.is_absolute():
        slice_artifact_dir = repo / slice_artifact_dir
    attempt = int(current.get("attempt") or 1)
    session_name = str(current.get("tmux_session") or "")
    harness_name = state["harness"]["name"]
    developer_session_id = current.get("developer_session_id")
    adapter = TmuxHarnessAdapter(
        harness_name,
        resolve_current_harness_command(args, repo, state, str(developer_session_id) if developer_session_id else None),
        current_allow_unattended_default(args, state),
        parse_reviewer_tools(getattr(args, "reviewer_tools", None)),
    )
    before_head = str(current["before_head"])
    started_at = str(current.get("started_at") or utc_now())
    # Recovered from persisted current_slice state rather than args.reviewer_tools:
    # this is a separate invocation and may not re-supply --reviewer-tools.
    reviewer_tools = tuple(current["reviewer_tools"])

    adapter.capture(session_name, slice_artifact_dir / f"pane-capture-attempt-{attempt}.txt")
    attempt_capture = slice_artifact_dir / f"pane-capture-attempt-{attempt}.txt"
    if attempt_capture.exists():
        (slice_artifact_dir / "pane-capture.txt").write_text(attempt_capture.read_text(encoding="utf-8"), encoding="utf-8")
    capture_developer_transcript(harness_name, repo, str(developer_session_id) if developer_session_id else None, slice_artifact_dir)
    capture_reviewer_runs_summary(slice_artifact_dir)
    after_head, after_status = _capture_git_evidence(repo, slice_artifact_dir, attempt, before_head)
    context_failure = prior_slice_context_integrity_failure(repo, current)
    gate = (
        GateDecision("needs-human", context_failure, signature="prior-context-integrity")
        if context_failure
        else verify_gate(repo, state, plan_slice, slice_artifact_dir, before_head, after_head, after_status, reviewer_tools)
    )
    if gate.status == "pass":
        projected_entry = slice_entry_from_gate(
            repo,
            plan_slice,
            slice_artifact_dir,
            started_at,
            gate,
            before_head,
            reviewer_tools,
            repair=repair_state(current),
            reviewer_policy=current.get("reviewer_policy"),
            write_summary=False,
        )
        budget_failure = projected_prior_slice_context_budget_failure(
            state, plan_slice, projected_entry, after_head or before_head or "unknown"
        )
        if budget_failure:
            gate = GateDecision(
                "repairable", budget_failure, gate.result, gate.actual_changed_files, "context-budget"
            )
    if gate.status in {"blocked", "fail"}:
        # Use only the fresh current-attempt pane tail. A cumulative session
        # transcript can retain an already-recovered outage from an earlier
        # repair round and must not reclassify a later genuine terminal stop.
        pane_text = (slice_artifact_dir / "pane-capture.txt").read_text(encoding="utf-8", errors="replace")[-4000:]
        gate = reclassify_high_confidence_transient_stop(
            gate,
            extract_operational_hints(
                pane_text,
                process_running=adapter.session_exists(session_name),
                process_active=False,
                result_exists=True,
            ),
        )

    run_json = run_dir / "run.json"
    # Budget and circuit breaker are driven from the persisted
    # current_slice.repair (round-0 default when absent): _attempt_for_slice
    # counts appended slice entries, which in-session repairs deliberately do
    # not create, so it must not gate this path.
    repair = repair_state(current)
    max_repairs = int(state.get("policy", {}).get("max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS))

    def finalize_terminal(terminal_gate: GateDecision) -> dict[str, Any]:
        return _finalize_terminal(
            adapter,
            repo=repo,
            run_json=run_json,
            state=state,
            plan_slice=plan_slice,
            slice_artifact_dir=slice_artifact_dir,
            session_name=session_name,
            started_at=started_at,
            before_head=before_head,
            reviewer_tools=reviewer_tools,
            repair=repair,
            terminal_gate=terminal_gate,
        )

    if gate.status != "repairable":
        # pass, or a terminal decision (integrity/trust breaches and the
        # developer's own considered stops): force_stop, append the entry,
        # clear current_slice, and stop or idle as today.
        return finalize_terminal(gate)

    signature = gate.signature or "developer-repairable"
    session_alive = adapter.session_exists(session_name)
    mode, terminal_gate = resolve_repair_action(repair, signature, session_alive, max_repairs, gate, plan_slice.slice_id)
    if terminal_gate is not None:
        return finalize_terminal(terminal_gate)
    round_number = int(repair["round"])
    _record_repair_round_evidence(adapter, session_name, slice_artifact_dir, round_number, after_status)
    append_operational_event(
        repo,
        state,
        {
            "kind": "repair",
            "slice_id": plan_slice.slice_id,
            "round": round_number,
            "signature": signature,
            "mode": mode,
            "tmux_session": session_name,
            "gate_reason": gate.reason,
        },
    )

    repair_prompt_text = render_repair_prompt(plan_slice, slice_artifact_dir, gate, before_head=before_head)
    repair_prompt_path = slice_artifact_dir / f"repair-prompt-repair-{round_number}.md"
    repair_prompt_path.write_text(repair_prompt_text, encoding="utf-8")
    (slice_artifact_dir / "repair-prompt.md").write_text(repair_prompt_text, encoding="utf-8")

    if mode == "in-session":
        # Keep the live session and the populated current_slice (so
        # start-slice still refuses a concurrent second attempt). The PM
        # model delivers send_text via the send command — which the
        # `resuming` status accepts — waits for a fresh result, and
        # finalizes again.
        current["repair"] = dict(repair)
        state["status"] = "resuming"
        state["stop_reason"] = None
        write_run(run_json, state)
        return {
            "finalized": False,
            "status": "repairable",
            "reason": gate.reason,
            "repair": dict(repair),
            "mode": mode,
            "tmux_session": session_name,
            "repair_prompt_path": relative_artifact_path(repo, repair_prompt_path),
            "send_text": _repair_delivery_message(plan_slice, repair_prompt_path),
            "next_action": "deliver send_text into the live session with the send command, wait for a fresh result, then finalize again",
        }

    # relaunch / fresh-session: the old session is finished with; launch a
    # new session for the same slice with the original frozen prompt plus the
    # targeted repair and the cumulative residual ledger from archived rounds.
    # start-slice cannot be used here — it refuses while current_slice is
    # populated, and clearing current_slice would drop the persisted repair
    # state the circuit breaker depends on.
    adapter.force_stop(session_name)
    repair["session_generation"] = int(repair["session_generation"]) + 1
    generation = int(repair["session_generation"])
    new_developer_session_id = str(uuid.uuid4()) if harness_name == "claude" else None
    relaunch_adapter = TmuxHarnessAdapter(
        harness_name,
        resolve_current_harness_command(args, repo, state, new_developer_session_id),
        current_allow_unattended_default(args, state),
        reviewer_tools,
        expected_model_display=str(state.get("harness", {}).get("model_identity", {}).get("display_name") or "") or None,
    )
    new_session_name = tmux_session_name(state["run_id"], plan_slice, generation)
    prompt_path = slice_artifact_dir / "prompt.md"
    if not prompt_path.is_file():
        prompt_path.write_text(
            render_developer_prompt(state, plan_slice, slice_artifact_dir, run_json, reviewer_tools),
            encoding="utf-8",
        )
    fresh_prompt_path = slice_artifact_dir / f"fresh-session-prompt-repair-{round_number}.md"
    fresh_prompt_path.write_text(
        _fresh_session_repair_prompt(prompt_path, repair_prompt_text, slice_artifact_dir, round_number),
        encoding="utf-8",
    )
    # Persist the new generation/session BEFORE launching it: a crash after
    # the launch then finds run.json already pointing at the live session
    # (fully recoverable), and a crash before it leaves a recorded session
    # that simply does not exist (the next finalize fails closed). The old
    # ordering could leave an unrecorded live session actively editing.
    # current_slice.before_head stays the slice starting commit so
    # verification remains cumulative across sessions.
    current["tmux_session"] = new_session_name
    current["attempt"] = generation
    current["started_at"] = utc_now()
    current["repair"] = dict(repair)
    if new_developer_session_id:
        current["developer_session_id"] = new_developer_session_id
    else:
        current.pop("developer_session_id", None)
    state["status"] = "running"
    state["stop_reason"] = None
    write_run(run_json, state)
    try:
        relaunch_adapter.start(repo, new_session_name, slice_artifact_dir, run_json, Path(state["plan_path"]), plan_slice)
        relaunch_adapter.send_prompt(new_session_name, fresh_prompt_path)
    except Exception as exc:
        _capture_failure_evidence(
            relaunch_adapter,
            session_name=new_session_name,
            harness_name=harness_name,
            repo=repo,
            developer_session_id=new_developer_session_id,
            slice_artifact_dir=slice_artifact_dir,
            attempt=generation,
            before_head=before_head,
        )
        relaunch_adapter.force_stop(new_session_name)
        return finalize_terminal(
            GateDecision(
                "failed",
                f"failed to relaunch developer session for repair: {exc}",
                gate.result,
                gate.actual_changed_files,
                signature,
            )
        )
    return {
        "finalized": False,
        "status": "repairable",
        "reason": gate.reason,
        "repair": dict(repair),
        "mode": mode,
        "tmux_session": new_session_name,
        "repair_prompt_path": relative_artifact_path(repo, fresh_prompt_path),
        "next_action": "wait for a fresh result from the relaunched session, then finalize again",
    }


def handle_idle_stall(
    args: argparse.Namespace,
    repo: Path,
    state: dict[str, Any],
    plan_slice: PlanSlice,
    run_dir: Path,
) -> dict[str, Any]:
    """Apply the shared repair budget and circuit breaker to a proven idle stall."""
    current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
    if not current:
        raise PmError("run has no current slice")
    artifact_dir = _slice_artifact_dir(repo, current)
    session_name = str(current.get("tmux_session") or "")
    reviewer_tools = tuple(str(tool) for tool in current.get("reviewer_tools") or ())
    before_head = str(current.get("before_head") or "") or None
    started_at = str(current.get("started_at") or utc_now())
    harness_name = state["harness"]["name"]
    adapter = _current_adapter(args, repo, state)
    repair = repair_state(current)
    gate = GateDecision(
        "repairable",
        "harness made no visible progress across the configured observation ceiling",
        signature="idle-no-progress",
    )
    mode, terminal_gate = resolve_repair_action(
        repair,
        gate.signature,
        adapter.session_exists(session_name),
        int(state.get("policy", {}).get("max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS)),
        gate,
        plan_slice.slice_id,
    )
    run_json = run_dir / "run.json"

    def finalize_terminal(decision: GateDecision) -> dict[str, Any]:
        adapter.capture(session_name, artifact_dir / "pane-capture.txt")
        capture_developer_transcript(
            harness_name,
            repo,
            str(current.get("developer_session_id")) if current.get("developer_session_id") else None,
            artifact_dir,
        )
        after_head, after_status = _capture_git_evidence(
            repo, artifact_dir, int(current.get("attempt") or 1), before_head
        )
        objective = GateDecision(
            decision.status,
            decision.reason,
            result={
                "commit": {
                    "requested": bool(state.get("policy", {}).get("commit_required", True)),
                    "created": bool(after_head and after_head != before_head),
                    "hash": after_head if after_head and after_head != before_head else None,
                }
            },
            actual_changed_files=decision.actual_changed_files,
            signature=decision.signature,
        )
        return _finalize_terminal(
            adapter,
            repo=repo,
            run_json=run_json,
            state=state,
            plan_slice=plan_slice,
            slice_artifact_dir=artifact_dir,
            session_name=session_name,
            started_at=started_at,
            before_head=before_head,
            reviewer_tools=reviewer_tools,
            repair=repair,
            terminal_gate=objective,
        )

    if terminal_gate is not None:
        return finalize_terminal(terminal_gate)

    round_number = int(repair["round"])
    after_status = git_status_text(repo)
    _record_repair_round_evidence(adapter, session_name, artifact_dir, round_number, after_status)
    append_operational_event(
        repo,
        state,
        {
            "kind": "repair",
            "slice_id": plan_slice.slice_id,
            "round": round_number,
            "signature": gate.signature,
            "mode": mode,
            "tmux_session": session_name,
            "gate_reason": gate.reason,
        },
    )
    repair_prompt_text = render_repair_prompt(plan_slice, artifact_dir, gate, before_head=before_head)
    repair_prompt_path = artifact_dir / f"repair-prompt-repair-{round_number}.md"
    repair_prompt_path.write_text(repair_prompt_text, encoding="utf-8")
    (artifact_dir / "repair-prompt.md").write_text(repair_prompt_text, encoding="utf-8")

    if mode == "in-session":
        current["repair"] = dict(repair)
        state["status"] = "resuming"
        state["stop_reason"] = None
        write_run(run_json, state)
        nudge = str(state.get("supervision", {}).get("default_resume_prompt") or "Review your state and continue.")
        try:
            adapter.send_literal(session_name, nudge)
        except PmError as exc:
            return finalize_terminal(
                GateDecision("blocked", f"idle-stall repair nudge could not be delivered: {exc}", signature=gate.signature)
            )
        append_operational_event(
            repo,
            state,
            {
                "kind": "send",
                "status": "sent",
                "slice_id": plan_slice.slice_id,
                "attempt": current.get("attempt"),
                "tmux_session": session_name,
                "text": nudge,
                "reason": "automatic bounded idle-stall repair",
            },
        )
        state["status"] = "running"
        write_run(run_json, state)
        return {
            "status": "repairable",
            "mode": mode,
            "reason": gate.reason,
            "repair": dict(repair),
            "tmux_session": session_name,
            "automatic_nudge_sent": True,
        }

    adapter.force_stop(session_name)
    repair["session_generation"] = int(repair["session_generation"]) + 1
    generation = int(repair["session_generation"])
    new_developer_session_id = str(uuid.uuid4()) if harness_name == "claude" else None
    relaunch_adapter = TmuxHarnessAdapter(
        harness_name,
        resolve_current_harness_command(args, repo, state, new_developer_session_id),
        current_allow_unattended_default(args, state),
        reviewer_tools,
        expected_model_display=str(state.get("harness", {}).get("model_identity", {}).get("display_name") or "") or None,
    )
    new_session_name = tmux_session_name(state["run_id"], plan_slice, generation)
    prompt_path = artifact_dir / "prompt.md"
    fresh_prompt_path = artifact_dir / f"fresh-session-prompt-repair-{round_number}.md"
    fresh_prompt_path.write_text(
        _fresh_session_repair_prompt(prompt_path, repair_prompt_text, artifact_dir, round_number), encoding="utf-8"
    )
    current["tmux_session"] = new_session_name
    current["attempt"] = generation
    current["started_at"] = utc_now()
    current["repair"] = dict(repair)
    if new_developer_session_id:
        current["developer_session_id"] = new_developer_session_id
    else:
        current.pop("developer_session_id", None)
    state["status"] = "running"
    state["stop_reason"] = None
    write_run(run_json, state)
    try:
        relaunch_adapter.start(repo, new_session_name, artifact_dir, run_json, Path(state["plan_path"]), plan_slice)
        relaunch_adapter.send_prompt(new_session_name, fresh_prompt_path)
    except Exception as exc:
        _capture_failure_evidence(
            relaunch_adapter,
            session_name=new_session_name,
            harness_name=harness_name,
            repo=repo,
            developer_session_id=new_developer_session_id,
            slice_artifact_dir=artifact_dir,
            attempt=generation,
            before_head=before_head,
        )
        relaunch_adapter.force_stop(new_session_name)
        return finalize_terminal(
            GateDecision("failed", f"failed to relaunch stalled developer session: {exc}", signature=gate.signature)
        )
    return {
        "status": "repairable",
        "mode": mode,
        "reason": gate.reason,
        "repair": dict(repair),
        "tmux_session": new_session_name,
        "automatic_nudge_sent": False,
    }


def _record_repair_round_evidence(
    adapter: TmuxHarnessAdapter,
    session_name: str,
    slice_artifact_dir: Path,
    round_number: int,
    after_status: str,
) -> None:
    """Preserve the failing round's evidence before the next round overwrites it.

    Per-attempt artifacts are keyed on the session generation and keep being
    rewritten across in-session repair rounds that share one session; these
    per-round copies keep every round independently auditable.
    """
    result_path = slice_artifact_dir / "developer-result.json"
    if result_path.exists():
        # Atomic rename, not read+write+unlink: the poll loop breaks as soon
        # as developer-result.json exists, so the stale failing result must
        # be gone before re-polling — and a rename can never destroy a result
        # the developer happens to rewrite mid-archive (whatever is there
        # at rename time is preserved in the round archive).
        result_path.replace(slice_artifact_dir / f"developer-result-repair-{round_number}.json")
    adapter.capture(session_name, slice_artifact_dir / f"pane-capture-repair-{round_number}.txt")
    (slice_artifact_dir / f"git-status-repair-{round_number}.txt").write_text(after_status, encoding="utf-8")


def _repair_delivery_message(plan_slice: PlanSlice, repair_prompt_path: Path) -> str:
    """One-line in-session pointer to the rendered repair prompt on disk.

    Deliberately a single line: send_literal types keystrokes into the live
    TUI, where a newline can submit a partial message. The full multi-line
    correction is persisted at the named path instead of being typed.
    """
    return (
        f"PM verification did NOT pass for {plan_slice.slice_id}; the slice is NOT accepted. "
        f"Read and follow the repair instructions in {repair_prompt_path} now, fix only the gap it names, "
        "re-run the failed gate, and rewrite developer-result.json for this same slice."
    )


def _fresh_session_repair_prompt(
    original_prompt_path: Path,
    repair_prompt_text: str,
    slice_artifact_dir: Path,
    round_number: int,
) -> str:
    """Carry repair context and every archived continuation ledger into a new session."""
    residual_findings: list[dict[str, Any]] = []
    continuation_notes: list[dict[str, Any]] = []
    archived_results: list[str] = []
    for archived_path in sorted(slice_artifact_dir.glob("developer-result-repair-*.json")):
        archived_results.append(str(archived_path))
        try:
            archived = json.loads(archived_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for field, destination in (
            ("residual_findings", residual_findings),
            ("continuation_notes", continuation_notes),
        ):
            items = archived.get(field) if isinstance(archived, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item not in destination:
                    destination.append(item)

    original_prompt = original_prompt_path.read_text(encoding="utf-8")
    archives = "\n".join(f"- `{path}`" for path in archived_results) or "- none"
    ledger = json.dumps(residual_findings, indent=2, sort_keys=True)
    continuation_ledger = json.dumps(continuation_notes, indent=2, sort_keys=True)
    return (
        original_prompt.rstrip()
        + "\n\n---\n\n"
        + f"# Fresh-Session Repair Continuation (Round {round_number})\n\n"
        + "This is a continuation of the same frozen slice, not a new implementation attempt. Follow the targeted repair "
        + "instructions below. The prior sessions' archived results are listed for auditability:\n\n"
        + archives
        + "\n\nPM recovered the following cumulative `residual_findings` ledger from those archived results:\n\n"
        + "```json\n"
        + ledger
        + "\n```\n\n"
        + "Your next `developer-result.json` must retain every item in this recovered ledger, merging any newly discovered "
        + "post-plan considerations and avoiding exact duplicates. Do not erase an item merely because this repair round is clean, "
        + "and do not move a material slice-caused defect into the ledger.\n\n"
        + "PM also recovered the following cumulative `continuation_notes` ledger:\n\n"
        + "```json\n"
        + continuation_ledger
        + "\n```\n\n"
        + "Retain these decisions and lessons in the next result, merging useful new repair knowledge and avoiding exact duplicates.\n\n"
        + repair_prompt_text.rstrip()
        + "\n"
    )


def _forced_batch_terminal(
    args: argparse.Namespace,
    repo: Path,
    state: dict[str, Any],
    plan_slice: PlanSlice,
    run_dir: Path,
    terminal_gate: GateDecision,
    *,
    capture_evidence: bool = True,
) -> dict[str, Any]:
    """Force a terminal outcome for the live batch slice from persisted state.

    Used by the batch driver for conditions the shared finalize path never
    gates: timeout, interrupt, unexpected exception, refused repair delivery.
    Every input comes from the persisted current_slice — the canonical record —
    not from driver locals (the entry's reviewer_tools is what a later reconcile
    recovers, and before_head is the cumulative slice boundary).
    """
    current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
    if not current:
        raise PmError("run has no current slice to force-terminate")
    slice_artifact_dir = _slice_artifact_dir(repo, current)
    session_name = str(current.get("tmux_session") or "")
    before_head = str(current.get("before_head") or "") or None
    developer_session_id = current.get("developer_session_id")
    reviewer_tools_value = current.get("reviewer_tools")
    adapter = _current_adapter(args, repo, state)
    if capture_evidence:
        _capture_failure_evidence(
            adapter,
            session_name=session_name,
            harness_name=state["harness"]["name"],
            repo=repo,
            developer_session_id=str(developer_session_id) if developer_session_id else None,
            slice_artifact_dir=slice_artifact_dir,
            attempt=int(current.get("attempt") or 1),
            before_head=before_head,
        )
    return _finalize_terminal(
        adapter,
        repo=repo,
        run_json=run_dir / "run.json",
        state=state,
        plan_slice=plan_slice,
        slice_artifact_dir=slice_artifact_dir,
        session_name=session_name,
        started_at=str(current.get("started_at") or utc_now()),
        before_head=before_head,
        reviewer_tools=tuple(reviewer_tools_value) if isinstance(reviewer_tools_value, list) else (),
        repair=repair_state(current),
        terminal_gate=terminal_gate,
    )


def execute_slice(args: argparse.Namespace, repo: Path, state: dict[str, Any], plan_slice: PlanSlice, run_dir: Path) -> int:
    """Deterministic batch driver: the model-supervised primitives under a fixed policy.

    One engine, two drivers: this sequences the same start / wait / finalize
    primitives the model-supervised commands expose, with every judgment call
    replaced by a fixed rule — a wait is never interrupted for hard-signal
    heuristics (send-time refusal is the unconditional safety boundary),
    in-session repairs are delivered immediately, and timeout / interrupt /
    unexpected exception become forced fail-closed terminals.
    """
    try:
        started = start_model_supervised_slice(
            args, repo, state, plan_slice, run_dir, supervision_mode="deterministic-batch"
        )
    except BaseException as exc:
        recovered = load_run(run_dir)
        if isinstance(recovered.get("current_slice"), dict):
            # The exception escaped start's own launch handler after
            # current_slice was persisted (e.g. KeyboardInterrupt mid-launch,
            # which is not an Exception): fail closed instead of orphaning the
            # recorded session.
            interrupted = isinstance(exc, KeyboardInterrupt)
            outcome = _forced_batch_terminal(
                args,
                repo,
                recovered,
                plan_slice,
                run_dir,
                GateDecision(
                    "cancelled" if interrupted else "failed",
                    "interrupted by user" if interrupted else (str(exc) or repr(exc)),
                ),
            )
            print(f"{plan_slice.slice_id} stopped: {outcome['reason']}")
            return 2
        if str(recovered.get("status")) in RUN_STOP_STATUSES:
            # start's launch handler already captured evidence, tore the
            # session down, and wrote the terminal stop state before re-raising.
            print(f"{plan_slice.slice_id} stopped: {recovered.get('stop_reason')}")
            return 2
        # Pre-persist setup failure: nothing launched, nothing recorded —
        # propagate exactly as the CLI expects for a refused configuration.
        raise
    if not started.get("started"):
        return 2

    try:
        deadline = time.monotonic() + float(args.timeout_seconds)
        while True:  # one iteration per wait/finalize round
            state = load_run(run_dir)
            current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
            if not current:
                raise PmError("batch driver lost the current slice mid-run")
            slice_artifact_dir = _slice_artifact_dir(repo, current)
            attempt = int(current.get("attempt") or 1)
            session_name = str(current.get("tmux_session") or "")
            reason, _snapshot = wait_observing(
                args,
                repo,
                run_dir,
                max(0.0, deadline - time.monotonic()),
                activity_log=slice_artifact_dir / f"activity-attempt-{attempt}.jsonl",
                stop_on_hard_signals=False,
                stop_on_idle_stall=False,
            )
            if reason == "timeout":
                # Legacy timeout evidence order: final pane state and
                # transcript first, then a stop request, then the post-stop
                # pane and cumulative git evidence against the slice boundary.
                adapter = _current_adapter(args, repo, state)
                developer_session_id = current.get("developer_session_id")
                attempt_capture = slice_artifact_dir / f"pane-capture-attempt-{attempt}.txt"
                adapter.capture(session_name, attempt_capture)
                if attempt_capture.exists():
                    (slice_artifact_dir / "pane-capture.txt").write_text(
                        attempt_capture.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                capture_developer_transcript(
                    state["harness"]["name"],
                    repo,
                    str(developer_session_id) if developer_session_id else None,
                    slice_artifact_dir,
                )
                capture_reviewer_runs_summary(slice_artifact_dir)
                adapter.request_stop(session_name)
                time.sleep(min(float(args.poll_seconds), 1.0))
                adapter.capture(session_name, slice_artifact_dir / "pane-capture-timeout.txt")
                _capture_git_evidence(repo, slice_artifact_dir, attempt, str(current.get("before_head") or "") or None)
                outcome = _forced_batch_terminal(
                    args,
                    repo,
                    state,
                    plan_slice,
                    run_dir,
                    GateDecision("blocked", "timeout waiting for developer-result.json"),
                    capture_evidence=False,
                )
                print(f"{plan_slice.slice_id} stopped: {outcome['reason']}")
                return 2

            # result-ready or process-exited: gate through the shared finalize.
            state = load_run(run_dir)
            outcome = finalize_model_supervised_slice(args, repo, state, plan_slice, run_dir)
            if outcome.get("finalized"):
                if outcome.get("status") == "pass":
                    print(f"{plan_slice.slice_id} passed PM gates.")
                    return 0
                print(f"{plan_slice.slice_id} stopped: {outcome.get('reason')}")
                return 2
            if outcome.get("mode") == "in-session":
                # Deliver the repair immediately — the fixed-policy equivalent
                # of the model-supervised send step. finalize advanced and
                # persisted the repair state, so round and signature come from
                # its return value, not pre-finalize locals.
                state = load_run(run_dir)
                current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
                if not current:
                    raise PmError("batch driver lost the current slice during in-session repair")
                repair_after = outcome.get("repair") if isinstance(outcome.get("repair"), dict) else repair_state(current)
                round_number = int(repair_after.get("round") or 0)
                adapter = _current_adapter(args, repo, state)
                session_name = str(current.get("tmux_session") or "")
                try:
                    # finalize captured the complete pane immediately before
                    # returning this repair action. Check that durable capture
                    # as well as the live pane: a large embedded prompt can
                    # push an earlier hard prompt beyond a terminal's retained
                    # live scrollback even though PM already preserved it.
                    repair_capture = _slice_artifact_dir(repo, current) / f"pane-capture-repair-{round_number}.txt"
                    captured_hard_prompt = adapter.detect_hard_prompt(
                        repair_capture.read_text(encoding="utf-8") if repair_capture.is_file() else ""
                    )
                    if captured_hard_prompt["present"]:
                        raise PmError(
                            "refusing to send into hard prompt preserved in repair evidence: "
                            + ", ".join(str(kind) for kind in captured_hard_prompt["kinds"])
                        )
                    adapter.send_literal(session_name, str(outcome.get("send_text") or ""))
                except PmError as exc:
                    # send_literal refuses when a hard prompt / hard-stop hint
                    # is on screen. That refusal must stop the run with
                    # evidence, never surface as an uncaught exception that
                    # orphans it.
                    adapter.capture(
                        session_name,
                        _slice_artifact_dir(repo, current) / f"pane-capture-repair-refused-{round_number}.txt",
                    )
                    outcome = _forced_batch_terminal(
                        args,
                        repo,
                        state,
                        plan_slice,
                        run_dir,
                        GateDecision(
                            "needs-human",
                            f"repair prompt could not be delivered into the live session: {exc}",
                            signature=str(repair_after.get("last_signature") or ""),
                        ),
                    )
                    print(f"{plan_slice.slice_id} stopped: {outcome['reason']}")
                    return 2
            # in-session (delivered), fresh-session, or relaunch: the slice is
            # still live in whichever session finalize chose; each repair round
            # gets a fresh timeout window for the developer to respond.
            deadline = time.monotonic() + float(args.timeout_seconds)
    except KeyboardInterrupt:
        outcome = _forced_batch_terminal(
            args, repo, load_run(run_dir), plan_slice, run_dir, GateDecision("cancelled", "interrupted by user")
        )
        print(f"{plan_slice.slice_id} stopped: {outcome['reason']}")
        return 2
    except Exception as exc:
        # Any failure — an PmError from the harness/tmux path, a finalize
        # refusal, or an unexpected exception — must not orphan the tmux
        # session or leave run.json stuck at "running". The forced terminal
        # captures whatever evidence exists and records a failed entry so the
        # run stops fail-closed.
        outcome = _forced_batch_terminal(
            args, repo, load_run(run_dir), plan_slice, run_dir, GateDecision("failed", str(exc) or repr(exc))
        )
        print(f"{plan_slice.slice_id} stopped: {outcome['reason']}")
        return 2

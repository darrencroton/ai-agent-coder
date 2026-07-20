"""Command-line parsing and dispatch (target-design §12).

All ten commands are wired: `init`, `status` (incl. `--report`), `approve`,
`start-slice`, `observe`, `send`, `finalize` (bare, and its
`--accept`/`--steer`/`--stop` decision paths), `review`, and `stop`. This
module stays thin: argument parsing, resolving the repo/run/token,
dispatching into `slice_ops`/`review`, and formatting output. The actual
mechanics (state mutation, session control, git facts, review commissioning)
live in the modules that own them; nothing here decides anything semantic.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

from . import IntegrityError, PmError
from . import git_ops
from . import plan as plan_mod
from . import review as review_mod
from . import slice_ops
from . import state as state_mod

# Mutating commands accept --token, falling back to PM_RUN_TOKEN in the
# controller's environment — never the Developer's.
_TOKEN_ENV_VAR = "PM_RUN_TOKEN"


def _positive_seconds(value: str) -> float:
    """argparse `type=` for `--wait`/`--timeout`: a finite, strictly
    positive number of seconds. Rejects NaN/inf and non-positive values that
    would silently mean "wait/timeout do nothing" or "wait forever"."""
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a finite number > 0, got {value!r}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm", description="Mode B Lite project manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_plan = subparsers.add_parser("check-plan", help="Validate a plan file")
    check_plan.add_argument("--plan", required=True)
    check_plan.add_argument("--repo")

    init = subparsers.add_parser("init", help="Set up a new PM run")
    init.add_argument("--repo", required=True)
    init.add_argument("--plan", required=True)
    init.add_argument("--harness", required=True)
    init.add_argument("--model")
    init.add_argument("--effort")
    branch_group = init.add_mutually_exclusive_group()
    branch_group.add_argument("--branch")
    branch_group.add_argument("--create-branch")
    init.add_argument("--attest", help='comma-separated slice ids, e.g. "Slice 1,Slice 2"')
    init.add_argument("--max-attempts", type=int)
    init.add_argument("--reviewer-tools", help="comma-separated tool names")
    init.add_argument("--reviewer-model")
    init.add_argument("--reviewer-effort")
    init.add_argument("--harness-command")

    status = subparsers.add_parser("status", help="Show run status")
    status.add_argument("--report", action="store_true")
    status.add_argument("--run")
    status.add_argument("--token")

    approve = subparsers.add_parser("approve", help="Record a human approval for a gated slice")
    approve.add_argument("--slice", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--run")
    approve.add_argument("--token")

    start_slice = subparsers.add_parser("start-slice", help="Run the next eligible slice")
    start_slice.add_argument("--model")
    start_slice.add_argument("--effort")
    start_slice.add_argument("--reviewer-tools")
    start_slice.add_argument("--harness-command")
    start_slice.add_argument("--risk", help='only "elevated" is accepted; risk can never be lowered')
    start_slice.add_argument("--run")
    start_slice.add_argument("--token")

    observe = subparsers.add_parser("observe", help="Show evidence of the live session's progress")
    observe.add_argument("--wait", type=_positive_seconds)
    observe.add_argument("--run")
    observe.add_argument("--token")

    send = subparsers.add_parser("send", help="Steer the live session")
    send.add_argument("--text", required=True)
    send.add_argument("--reason", required=True)
    send.add_argument("--run")
    send.add_argument("--token")

    finalize = subparsers.add_parser("finalize", help="Run the floor and collect assessment evidence")
    finalize_group = finalize.add_mutually_exclusive_group()
    finalize_group.add_argument("--accept", help="accept the slice; reasoning must be >= 40 characters")
    finalize_group.add_argument("--steer", help="send a written correction into the live session")
    finalize_group.add_argument("--stop", help="stop the slice, recording the reason")
    finalize.add_argument("--risk", help='only "elevated" is accepted; risk can never be lowered')
    finalize.add_argument("--run")
    finalize.add_argument("--token")

    review = subparsers.add_parser("review", help="Commission an independent review of the final diff")
    review.add_argument("--slice", required=True)
    review.add_argument("--skill", required=True, choices=["drift-audit", "code-review"])
    review.add_argument("--tool")
    review.add_argument("--model")
    review.add_argument("--effort")
    review.add_argument("--reviewer-command", help="override the whole reviewer command (tests/unsupported tools)")
    review.add_argument(
        "--timeout", type=_positive_seconds,
        help="kill the reviewer process group and fail closed after N seconds; "
        "there is deliberately no default — a slow cold local model may legitimately need longer",
    )
    review.add_argument("--run")
    review.add_argument("--token")

    stop = subparsers.add_parser("stop", help="End the run, preserving evidence")
    stop.add_argument("--reason", required=True)
    stop.add_argument("--slice-status", choices=["stopped"])
    stop.add_argument("--scavenge", action="store_true")
    stop.add_argument("--run")
    stop.add_argument("--token")

    return parser


def _resolve_token(args: argparse.Namespace) -> str | None:
    token = getattr(args, "token", None)
    if token:
        return token
    return os.environ.get(_TOKEN_ENV_VAR)


def _require_token(args: argparse.Namespace) -> str:
    token = _resolve_token(args)
    if not token:
        raise PmError("run capability token required (pass --token or set PM_RUN_TOKEN)")
    return token


def _repo_from_cwd() -> Path:
    return slice_ops.repo_from_cwd(Path.cwd())


def _run_check_plan(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    repo_path = Path(args.repo) if args.repo else None
    report = plan_mod.plan_check_report(plan_path, repo=repo_path)
    for error in report["errors"]:
        print(f"ERROR: {error}")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    gated = ", ".join(report["approval_gated"]) if report["approval_gated"] else "none"
    print(f"{report['slice_count']} slice(s); approval-gated: {gated}")
    return 0 if not report["errors"] else 2


# --- init ----------------------------------------------------------------


def _run_init(args: argparse.Namespace) -> int:
    repo = git_ops.resolve_repo(Path(args.repo))
    plan_path = git_ops.resolve_plan(Path(args.plan))

    report = plan_mod.plan_check_report(plan_path, repo=repo)
    for error in report["errors"]:
        print(f"ERROR: {error}")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    if report["errors"]:
        return 2

    result = slice_ops.init_run(
        repo,
        plan_path,
        harness=args.harness,
        model=args.model,
        effort=args.effort,
        branch=args.branch,
        create_branch=args.create_branch,
        attest=args.attest,
        max_attempts=args.max_attempts,
        reviewer_tools=args.reviewer_tools,
        reviewer_model=args.reviewer_model,
        reviewer_effort=args.reviewer_effort,
        harness_command=args.harness_command,
    )

    print(f"run id: {result.run_id}")
    print(f"state dir: {result.run_dir}")
    print(f"branch: {result.branch}")
    print("slices:")
    for entry, plan_slice in zip(result.state["slices"], result.slices):
        print(
            f"  {entry['id']:<10} {plan_slice.title:<40} risk={entry['risk']:<9} "
            f"status={entry['status'] or 'pending'}"
        )
    print(f"PM_RUN_TOKEN={result.token}")
    print(
        "Keep this token out of Developer sessions — it authorizes PM state writes; "
        "the operator or PM agent should hold it, never the harness being supervised."
    )
    return 0


# --- status ----------------------------------------------------------------


def _run_status(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    # Read-only, so the token is optional — but when present (the PM agent's
    # own environment) the load is MAC-verified, and report regeneration
    # never renders from unverified state.
    token = _resolve_token(args)

    if args.report:
        state = state_mod.load_state(run_dir, token)
        report_path = slice_ops.regenerate_report(repo, run_dir, state)
        print(f"run report regenerated: {report_path}")
        mirror_path = slice_ops.run_artifact_dir(repo, state["run_id"]) / "run-report.md"
        print(f"mirror: {mirror_path}")
        return 0

    result = slice_ops.status(repo, run_dir, token)
    state = result.state

    print(f"run id: {state['run_id']}  status: {state['status']}")
    print(f"branch: {state['branch']}")
    plan_info = state.get("plan") or {}
    print(f"plan: {plan_info.get('path')}  sha256: {(plan_info.get('sha256') or '')[:12]}…")
    print(f"stop reason: {state.get('stop_reason')}")

    print("slices:")
    for entry in state.get("slices", []):
        commit = entry.get("commit")
        commit_short = commit[:10] if commit else "-"
        print(
            f"  {entry['id']:<10} status={entry.get('status') or 'pending':<10} "
            f"risk={entry.get('risk'):<9} attempts={entry.get('attempts', 0)} commit={commit_short}"
        )

    current = state.get("current_slice")
    if current:
        alive = result.current_session_alive
        before_head = (current.get("before_head") or "")[:12]
        print(
            f"current slice: {current.get('id')}  session={current.get('tmux_session')} "
            f"alive={alive}  before_head={before_head}…  started_at={current.get('started_at')} "
            f"attempts={current.get('attempts')}"
        )
    else:
        print("current slice: none")

    if result.plan_error:
        print(f"next slice: plan could not be read ({result.plan_error})")
    elif result.next_slice_id is None:
        print("next slice: none (all slices complete)")
    else:
        if result.next_slice_eligible:
            print(f"next slice: {result.next_slice_id} (eligible)")
        else:
            print(f"next slice: {result.next_slice_id} (blocked)")
            for reason in result.next_slice_reasons:
                print(f"  - {reason}")

    approvals = state.get("approvals") or {}
    if approvals:
        print("approvals:")
        for slice_id, record in approvals.items():
            print(f"  {slice_id}: {record.get('reason')} (at {record.get('at')})")
    else:
        print("approvals: none")

    return 0


# --- approve ----------------------------------------------------------------


def _run_approve(args: argparse.Namespace) -> int:
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    slice_ops.approve(repo, run_dir, token, slice_id=args.slice, reason=args.reason)
    print(f"approved {args.slice}: {args.reason}")
    return 0


# --- start-slice --------------------------------------------------------------


def _run_start_slice(args: argparse.Namespace) -> int:
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    outcome = slice_ops.start_slice(
        repo,
        run_dir,
        token,
        model=args.model,
        effort=args.effort,
        reviewer_tools=args.reviewer_tools,
        harness_command=args.harness_command,
        risk=args.risk,
    )

    if outcome.kind == "all_complete":
        print("all slices complete")
        return 0
    if outcome.kind == "blocked":
        print(f"{outcome.slice_id} is not eligible:")
        for reason in outcome.reasons:
            print(f"  - {reason}")
        return 2
    if outcome.kind == "plan_changed":
        print(f"pm: error: plan file changed mid-run: {outcome.message}", file=sys.stderr)
        return 2
    if outcome.kind == "attempts_exhausted":
        print(f"pm: error: attempt budget exhausted for {outcome.slice_id}", file=sys.stderr)
        return 2

    verb = "relaunched" if outcome.kind == "relaunched" else "launched"
    print(f"{verb} {outcome.slice_id} (attempt {outcome.attempt}) in tmux session {outcome.session}")
    if outcome.reaped:
        print(f"reaped stale sessions: {', '.join(outcome.reaped)}")
    if outcome.notes_warning:
        print(f"WARNING: {outcome.notes_warning}")
    return 0


# --- observe ------------------------------------------------------------------


def _run_observe(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    outcome = slice_ops.observe(repo, run_dir, wait=args.wait, token=_resolve_token(args))

    if not outcome.has_current_slice:
        print("no current slice")
        return 0

    print(f"slice: {outcome.slice_id}")
    if args.wait:
        print(f"waited: {outcome.elapsed_seconds:.1f}s (requested {args.wait:g}s)")
    print(f"session running: {outcome.running}")
    print(f"pane changed: {outcome.pane_changed}")
    status_note = f" (status={outcome.result_status})" if outcome.result_status else ""
    print(f"result present: {outcome.result_present}{status_note}")
    if outcome.hard_stop["present"]:
        print(f"hard-stop scan: {', '.join(outcome.hard_stop['kinds'])}")
    else:
        print("hard-stop scan: clear")
    print("--- pane tail ---")
    print(outcome.tail)
    return 0


# --- send ---------------------------------------------------------------------


def _run_send(args: argparse.Namespace) -> int:
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    slice_ops.send(repo, run_dir, token, text=args.text, reason=args.reason)
    print(f"sent: {args.text!r} ({args.reason})")
    return 0


# --- finalize (bare, and its --accept/--steer/--stop decision paths) ----------


def _print_floor_facts(report) -> None:
    for fact in report.facts:
        status = "PASS" if fact.passed else "FAIL"
        print(f"{fact.number} {fact.name} {status} {fact.detail}")


def _run_finalize(args: argparse.Namespace) -> int:
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)

    if args.accept:
        outcome = slice_ops.finalize_accept(repo, run_dir, token, reasoning=args.accept, risk=args.risk)
        print(f"slice: {outcome.slice_id}")
        if outcome.report:
            _print_floor_facts(outcome.report)
        if outcome.kind == "accepted":
            print(f"ACCEPTED {outcome.slice_id}")
            print(f"assessment: {outcome.assessment_path}")
            return 0
        print(f"pm: refused: {outcome.message}", file=sys.stderr)
        return 1

    if args.steer:
        outcome = slice_ops.finalize_steer(repo, run_dir, token, correction=args.steer, risk=args.risk)
        if outcome.kind == "steered":
            print(f"steered {outcome.slice_id} (attempt {outcome.attempts})")
            print("correction delivered directly to the live session (no artifact file written)")
            return 0
        print(f"pm: error: {outcome.message}", file=sys.stderr)
        return 2

    if args.stop:
        outcome = slice_ops.finalize_stop(repo, run_dir, token, reason=args.stop, risk=args.risk)
        print(f"STOPPED {outcome.slice_id}")
        _print_floor_facts(outcome.report)
        print(f"assessment: {outcome.assessment_path}")
        return 0

    outcome = slice_ops.finalize(repo, run_dir, token, risk=args.risk)
    print(f"slice: {outcome.slice_id}")
    _print_floor_facts(outcome.report)
    print(f"evidence: status-before={outcome.status_before_path}")
    print(f"evidence: status-after={outcome.status_after_path}")
    print(f"evidence: diff={outcome.diff_path}")
    print(f"evidence: pane={outcome.pane_path}")
    print(f"evidence: result={outcome.result_path}")
    return 0 if outcome.report.passed else 1


# --- review -----------------------------------------------------------------


def _run_review(args: argparse.Namespace) -> int:
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    outcome = review_mod.run_review(
        repo,
        run_dir,
        token,
        slice_id=args.slice,
        skill=args.skill,
        tool=args.tool,
        model=args.model,
        effort=args.effort,
        reviewer_command=args.reviewer_command,
        timeout=args.timeout,
    )
    print(f"slice: {outcome.slice_id}")
    print(f"skill: {outcome.skill}  tool: {outcome.tool}")
    print(f"reviewed head: {outcome.head}  before_head: {outcome.before_head}")
    print(f"diff: {outcome.diff_path}")
    print(f"changed files: {len(outcome.changed_files)}")
    print(f"report: {outcome.artifact_path}")
    print(f"sha256: {outcome.sha256}")
    return 0


# --- stop -----------------------------------------------------------------


def _run_stop(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()

    if args.scavenge:
        try:
            token = _require_token(args)
            run_dir = state_mod.resolve_run_dir(repo, args.run)
            state = slice_ops.load_writable_state(run_dir, token)
            outcome = slice_ops.stop(repo, run_dir, token, reason=args.reason, slice_status=args.slice_status)
            extra_killed = slice_ops.stop_scavenge_sweep(run_id=state["run_id"])
            all_killed = outcome.killed + [name for name in extra_killed if name not in outcome.killed]
            print(f"stopped run {state['run_id']}; killed sessions: {all_killed}")
            return 0
        except PmError as exc:
            killed = slice_ops.stop_scavenge_sweep(run_id=args.run)
            print(f"pm: scavenge: state unavailable ({exc}); swept sessions: {killed}")
            return 0

    token = _require_token(args)
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    outcome = slice_ops.stop(repo, run_dir, token, reason=args.reason, slice_status=args.slice_status)
    print(f"stopped run {outcome.run_id}; killed sessions: {outcome.killed}")
    return 0


_HANDLERS = {
    "check-plan": _run_check_plan,
    "init": _run_init,
    "status": _run_status,
    "approve": _run_approve,
    "start-slice": _run_start_slice,
    "observe": _run_observe,
    "send": _run_send,
    "finalize": _run_finalize,
    "review": _run_review,
    "stop": _run_stop,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        handler = _HANDLERS.get(args.command)
        if handler is not None:
            return handler(args)
        parser.error(f"unknown command: {args.command}")
        return 2
    except IntegrityError as exc:
        print(f"pm: error: INTEGRITY: {exc}", file=sys.stderr)
        return 2
    except PmError as exc:
        print(f"pm: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

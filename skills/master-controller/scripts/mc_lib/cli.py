from __future__ import annotations

import argparse
import sys

from .commands import (
    approve_slice,
    archive_sensitive,
    check_plan,
    finalize_slice,
    init_run,
    list_profiles,
    observe,
    pause_until,
    preflight,
    reconcile,
    run_next,
    run_remaining,
    send,
    start_slice,
    status,
    stop,
    stop_with_evidence,
    summarize,
    wait,
)
from .constants import DEFAULT_POLL_SECONDS, DEFAULT_TIMEOUT_SECONDS
from .models import McError


def add_repo_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="target git repository")
    parser.add_argument("--run", default="current", help="run directory, run.json path, or 'current'")


def add_harness_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--harness-command", help="override harness command for controlled local validation")
    parser.add_argument("--harness-model", help="model name/alias to compose through the MC harness profile, e.g. sonnet")
    parser.add_argument("--harness-effort", help="effort level to compose through the MC harness profile, e.g. medium")
    parser.add_argument("--worker-tools", default="", help="comma-separated worker tools expected for this run, e.g. copilot")
    parser.add_argument("--worker-model", help="model name/alias the orchestrator should use for worker launches when supported")
    parser.add_argument("--worker-effort", help="reasoning/effort level the orchestrator should use for worker launches when supported")
    parser.add_argument(
        "--allow-profile-command",
        action="store_true",
        help="use MC's capability profile to compose the unattended harness command from run requirements",
    )


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="maximum seconds to wait for orchestrator result")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="seconds between tmux/result checks")


def add_unattended_default_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-unattended-default",
        action="store_true",
        help="opt in to a known unattended-safe launch command for --harness codex/claude/copilot/opencode (disables per-action approval; MC's post-hoc gates become the safety boundary)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Master Controller state and eligibility CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a durable MC run")
    init.add_argument("--repo", required=True, help="target git repository")
    init.add_argument("--plan", required=True, help="implementation plan markdown file")
    init.add_argument("--harness", required=True, help="harness adapter name")
    init.add_argument("--worktree-root", help="optional worktree root")
    init.add_argument("--branch", help="intended branch to record for this MC run")
    init.add_argument(
        "--create-branch",
        action="store_true",
        help="with --branch, create and switch to the branch before initializing when it does not exist",
    )
    init.add_argument(
        "--assume-complete",
        help=(
            "comma-separated slice ids (e.g. 'Slice 1,Slice 2') the operator attests were already completed and "
            "committed before this run; recorded as assumed-complete so the run resumes at the next real slice"
        ),
    )
    init.add_argument(
        "--max-repair-attempts",
        type=int,
        help="per-slice repair budget for this run (default 3); 0 disables repair steering entirely",
    )
    init.add_argument(
        "--no-commit-required",
        action="store_true",
        help="record policy commit_required=false so slices are gated without requiring a commit",
    )
    init.set_defaults(func=init_run)

    check_parser = subparsers.add_parser(
        "check-plan",
        help="sanity-check a whole implementation plan before initializing an MC run",
    )
    check_parser.add_argument("--plan", required=True, help="implementation plan markdown file")
    check_parser.set_defaults(func=check_plan)

    approve = subparsers.add_parser("approve", help="record operator approval for one approval-gated slice")
    add_repo_run_args(approve)
    approve.add_argument("--slice", required=True, help="slice id to approve, e.g. 'Slice 3'")
    approve.add_argument("--reason", default="", help="approval reason recorded in run state and operational events")
    approve.set_defaults(func=approve_slice)

    for name, func, help_text in (
        ("status", status, "show current MC run state"),
        ("summarize", summarize, "summarize current MC run"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        add_repo_run_args(command)
        command.set_defaults(func=func)

    profiles_parser = subparsers.add_parser("profiles", help="list MC harness and worker capability profiles")
    profiles_parser.set_defaults(func=list_profiles)

    preflight_parser = subparsers.add_parser("preflight", help="check the next MC slice launch before running it")
    add_repo_run_args(preflight_parser)
    add_harness_args(preflight_parser)
    add_unattended_default_arg(preflight_parser)
    preflight_parser.set_defaults(func=preflight)

    run_next_parser = subparsers.add_parser("run-next", help="inspect the next slice, or run it unless --dry-run is set")
    add_repo_run_args(run_next_parser)
    run_next_parser.add_argument("--dry-run", action="store_true", help="only report next-slice eligibility")
    add_runtime_args(run_next_parser)
    add_harness_args(run_next_parser)
    add_unattended_default_arg(run_next_parser)
    run_next_parser.set_defaults(func=run_next)

    run_parser = subparsers.add_parser("run", help="run eligible slices until complete or stopped")
    add_repo_run_args(run_parser)
    run_parser.add_argument("--scope", required=True, choices=["remaining"], help="run scope")
    add_runtime_args(run_parser)
    add_harness_args(run_parser)
    add_unattended_default_arg(run_parser)
    run_parser.set_defaults(func=run_remaining, dry_run=False)

    observe_parser = subparsers.add_parser("observe", help="observe the active model-supervised slice without finalizing it")
    add_repo_run_args(observe_parser)
    add_harness_args(observe_parser)
    add_unattended_default_arg(observe_parser)
    observe_parser.set_defaults(func=observe)

    send_parser = subparsers.add_parser("send", help="send literal text to the active model-supervised tmux session")
    add_repo_run_args(send_parser)
    send_parser.add_argument("--text", required=True, help="literal text to send")
    send_parser.add_argument("--reason", required=True, help="reason recorded in the operational event log")
    add_harness_args(send_parser)
    add_unattended_default_arg(send_parser)
    send_parser.set_defaults(func=send)

    start_parser = subparsers.add_parser("start-slice", help="start the next eligible slice and return immediately")
    add_repo_run_args(start_parser)
    add_harness_args(start_parser)
    add_unattended_default_arg(start_parser)
    start_parser.set_defaults(func=start_slice)

    wait_parser = subparsers.add_parser("wait", help="observe the active slice for a bounded duration")
    add_repo_run_args(wait_parser)
    wait_parser.add_argument("--seconds", type=float, required=True, help="maximum seconds to wait")
    wait_parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="seconds between observations")
    add_harness_args(wait_parser)
    add_unattended_default_arg(wait_parser)
    wait_parser.set_defaults(func=wait)

    pause_parser = subparsers.add_parser("pause-until", help="pause and observe until an absolute timestamp plus optional buffer")
    add_repo_run_args(pause_parser)
    pause_parser.add_argument("--until", required=True, help="ISO-8601 timestamp with timezone")
    pause_parser.add_argument("--buffer-seconds", type=int, help="optional buffer added after --until")
    pause_parser.add_argument("--reason", required=True, help="pause reason recorded in state and operational events")
    pause_parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="seconds between observations")
    add_harness_args(pause_parser)
    add_unattended_default_arg(pause_parser)
    pause_parser.set_defaults(func=pause_until)

    finalize_parser = subparsers.add_parser("finalize-slice", help="capture evidence and run deterministic gates for the active slice")
    add_repo_run_args(finalize_parser)
    add_harness_args(finalize_parser)
    add_unattended_default_arg(finalize_parser)
    finalize_parser.set_defaults(func=finalize_slice)

    stop_evidence_parser = subparsers.add_parser("stop-with-evidence", help="stop the active slice after preserving evidence")
    add_repo_run_args(stop_evidence_parser)
    stop_evidence_parser.add_argument("--reason", required=True, help="stop reason recorded in run state and operational events")
    stop_evidence_parser.add_argument(
        "--status",
        default="needs-human",
        choices=["needs-human", "blocked", "failed", "cancelled"],
        help="run status to record",
    )
    add_harness_args(stop_evidence_parser)
    add_unattended_default_arg(stop_evidence_parser)
    stop_evidence_parser.set_defaults(func=stop_with_evidence)

    reconcile_parser = subparsers.add_parser("reconcile", help="re-check and repair a stopped slice from local evidence")
    add_repo_run_args(reconcile_parser)
    reconcile_parser.set_defaults(func=reconcile)

    stop_parser = subparsers.add_parser("stop", help="cancel the current MC run")
    add_repo_run_args(stop_parser)
    stop_parser.add_argument("--reason", default="cancelled by user", help="reason recorded in run state")
    add_harness_args(stop_parser)
    add_unattended_default_arg(stop_parser)
    stop_parser.set_defaults(func=stop)

    archive_parser = subparsers.add_parser("archive-sensitive", help="archive sensitive worker state from a run")
    add_repo_run_args(archive_parser)
    archive_parser.add_argument("--dry-run", action="store_true", help="print artifact moves without changing files")
    archive_parser.set_defaults(func=archive_sensitive)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except McError as exc:
        print(f"mc: {exc}", file=sys.stderr)
        return 1

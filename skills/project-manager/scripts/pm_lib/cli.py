"""Command-line parsing and dispatch.

Every subcommand below (see `_HANDLERS`) is wired the same thin way:
argument parsing, resolving the repo/run/token, dispatching into
`slice_ops`/`review`/`campaign`, and formatting output. The actual mechanics
(state mutation, session control, git facts, review commissioning, campaign
child control) live in the modules that own them; nothing here decides
anything semantic.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path

from . import IntegrityError, PmError
from . import campaign
from . import git_ops
from . import judgments
from . import leaderboard, ledger
from . import plan as plan_mod
from . import pm_paths
from . import preflight
from . import review as review_mod
from . import sandbox
from . import sessions
from . import slice_ops
from . import state as state_mod

# Mutating commands accept --token, falling back to PM_RUN_TOKEN in the
# controller's environment — never the Developer's.
_TOKEN_ENV_VAR = "PM_RUN_TOKEN"

# Backstop against a reviewer that hangs forever in an unattended run. Sized so
# only a stuck process reaches it; a legitimately slower reviewer is given a
# larger explicit --timeout.
_REVIEW_DEFAULT_TIMEOUT_SECONDS = 3600.0

# C0 control characters other than tab and newline: a question line keeps its
# indentation, while an escape sequence or carriage return could repaint the
# reader's terminal over the report's own lines.
_QUESTION_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")


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
    check_plan.add_argument(
        "--repo",
        help="the repository, resolved to its toplevel as init does: directory-shaped entries are linted "
        "against it, and a --policy file inside it protects itself by its path relative to it",
    )
    check_plan.add_argument(
        "--policy",
        metavar="FILE",
        help="a protected-paths policy file (JSON object; its 'protected_paths' list uses authorized-surface "
        "entry syntax): any authorized entry whose literal prefix reaches a protected path is an error",
    )

    init = subparsers.add_parser("init", help="Set up a new PM run")
    init.add_argument("--repo", required=True)
    init.add_argument("--plan", required=True)
    init.add_argument(
        "--harness", required=True, help="Developer harness: one of codex, claude, copilot, opencode, qwen"
    )
    init.add_argument("--model")
    init.add_argument("--effort")
    init.add_argument(
        "--model-tag",
        help="the run's default Developer tag: a label that tells apart two configurations the tool, model "
        "and effort strings cannot (a local model at two temperatures, say); the ledger groups by it",
    )
    init.add_argument("--run-tag", help="a label for the whole run; `ledger render --run-tag` filters by it")
    branch_group = init.add_mutually_exclusive_group()
    branch_group.add_argument("--branch")
    branch_group.add_argument("--create-branch")
    init.add_argument("--attest", help='comma-separated slice ids, e.g. "Slice 1,Slice 2"')
    init.add_argument("--max-attempts", type=int)
    init.add_argument(
        "--reviewer-tools", help="comma-separated Reviewer harnesses: codex, claude, copilot, opencode, qwen"
    )
    init.add_argument("--reviewer-model")
    init.add_argument("--reviewer-effort")
    init.add_argument("--harness-command")
    init.add_argument(
        "--policy",
        metavar="FILE",
        help="a protected-paths policy file (as for check-plan): its entries are frozen into run state, any "
        "authorized entry reaching one refuses init, grant refuses one, and floor fact 5 fails on one",
    )
    init.add_argument(
        "--sandbox",
        metavar="FILE",
        help="an srt base policy (macOS): the run's Developers run on a private tmux server under srt, on "
        "socket pm-run-<run id>, and its reviewers under srt too; export PM_TMUX_SOCKET as init prints it",
    )

    status = subparsers.add_parser("status", help="Show run status")
    status.add_argument("--report", action="store_true")
    status.add_argument("--run")
    status.add_argument("--token")

    approve = subparsers.add_parser("approve", help="Record a human approval for a gated slice")
    approve.add_argument("--slice", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--run")
    approve.add_argument("--token")

    grant = subparsers.add_parser(
        "grant",
        help="Extend the effective authorized surface for the in-flight slice by one exact file path, "
        "on recorded evidence (ratchets the slice to elevated and stales its recorded reviews)",
    )
    grant.add_argument("--slice", required=True)
    grant.add_argument(
        "--path",
        required=True,
        help="one exact repository-relative file path. A directory, trailing-slash subtree, or glob is "
        "refused: write envelopes belong in the plan, where check-plan lints them and a human approved "
        "them. Dependency manifests, lockfiles and licenses are refused outright",
    )
    grant.add_argument(
        "--evidence",
        required=True,
        help="the repository evidence that makes this path necessary to satisfy the slice's "
        "existing contract; >= 40 characters",
    )
    grant.add_argument("--run")
    grant.add_argument("--token")

    start_slice = subparsers.add_parser("start-slice", help="Run the next eligible slice")
    start_slice.add_argument("--model")
    start_slice.add_argument("--effort")
    start_slice.add_argument("--model-tag", help="this launch's Developer tag, overriding init's --model-tag")
    start_slice.add_argument("--reviewer-tools")
    start_slice.add_argument("--harness-command")
    start_slice.add_argument("--risk", help='only "elevated" is accepted; risk can never be lowered')
    start_slice.add_argument("--run")
    start_slice.add_argument("--token")

    observe = subparsers.add_parser("observe", help="Show evidence of the live session's progress")
    observe.add_argument("--wait", type=_positive_seconds)
    observe.add_argument(
        "--seen-marker",
        action="append",
        default=[],
        metavar="TEXT",
        help="a dialog marker (its matched literal, as printed after `dialog marker:`) that is already read and judged: "
        "it does not end the wait, and is still printed on return; repeatable. Any other marker ends the wait",
    )
    observe.add_argument("--run")
    observe.add_argument("--token")

    send = subparsers.add_parser("send", help="Steer the live session")
    send.add_argument("--text", required=True)
    send.add_argument("--reason", required=True)
    send.add_argument("--run")
    send.add_argument("--token")

    answer = subparsers.add_parser(
        "answer",
        help="Answer the Developer's open question (question.md): writes answer-<n>.md, archives the question "
        "as question-<n>.md, and sends a one-line pointer into the live session; free, recorded",
    )
    answer.add_argument("--text", required=True, help="the answer, delivered verbatim through a file")
    answer.add_argument("--run")
    answer.add_argument("--token")

    finalize = subparsers.add_parser("finalize", help="Run the floor and collect assessment evidence")
    finalize_group = finalize.add_mutually_exclusive_group()
    finalize_group.add_argument("--accept", help="accept the slice; reasoning must be >= 40 characters")
    finalize_group.add_argument("--steer", help="send a written correction into the live session")
    finalize_group.add_argument("--stop", help="stop the slice, recording the reason (requires --cause)")
    # Required with --stop and refused otherwise; argparse cannot express
    # that inside the mutually exclusive group, so _run_finalize enforces it.
    finalize.add_argument(
        "--cause",
        choices=["plan", "developer", "environment"],
        help="why the slice stopped (only with --stop): plan, developer, or environment",
    )
    finalize.add_argument("--risk", help='only "elevated" is accepted; risk can never be lowered')
    finalize.add_argument("--run")
    finalize.add_argument("--token")

    review = subparsers.add_parser("review", help="Commission an independent review of the final diff")
    review.add_argument("--slice", required=True)
    review.add_argument("--skill", required=True, choices=["drift-audit", "code-review"])
    review.add_argument("--tool", help="reviewer harness: one of codex, claude, copilot, opencode, qwen")
    review.add_argument(
        "--model",
        help="model for --tool; a provider-prefixed name is passed through as-is, "
        "e.g. an opencode-namespaced model is --tool opencode --model opencode-go/<model>",
    )
    review.add_argument("--effort")
    review.add_argument("--model-tag", help="this commission's Reviewer tag; the ledger groups by it")
    review.add_argument(
        "--adjudicated",
        action="append",
        metavar="TEXT",
        help="one ruling PM has already settled in this run, so the reviewer stops re-raising it; "
        "repeatable. Bounds reviewer attention only — it can never widen the authorized surface "
        "or weaken a criterion, and a reviewer that disagrees still files a full finding",
    )
    review.add_argument(
        "--adjudicated-file",
        metavar="PATH",
        help="one ruling per line (blank lines and lines starting with # are skipped) from a file "
        "PM reuses byte-for-byte across every commission in a panel, instead of retyping the same "
        "ruling per --adjudicated call with a risk of wording drift — a judge-reviews panel "
        "comparison requires identical adjudication context across its members. Combined with any "
        "--adjudicated flags, file lines first",
    )
    review.add_argument("--reviewer-command", help="override the whole reviewer command (tests/unsupported tools)")
    review.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=_REVIEW_DEFAULT_TIMEOUT_SECONDS,
        help="kill the reviewer process group and fail closed after N seconds "
        f"(default {_REVIEW_DEFAULT_TIMEOUT_SECONDS:g}). The default is a hang backstop, not a "
        "cadence: it is set well beyond any healthy review so it only ever fires on a stuck "
        "reviewer. A slow cold local model that legitimately needs longer takes a larger explicit "
        "--timeout; an unbounded wait is not offered, since that is the hang this bounds",
    )
    review.add_argument("--run")
    review.add_argument("--token")

    judge = subparsers.add_parser("judge-reviews", help="Record PM's structured reviewer-usefulness judgment")
    judge.add_argument("--file", required=True, help="JSON judgment input; it is not authoritative state")
    judge.add_argument("--run")
    judge.add_argument("--token")

    developer_judge = subparsers.add_parser(
        "judge-developer", help="Record PM's structured developer-contribution judgment"
    )
    developer_judge.add_argument("--file", required=True, help="JSON judgment input; it is not authoritative state")
    developer_judge.add_argument("--run")
    developer_judge.add_argument("--token")

    notes = subparsers.add_parser("notes", help="Update the run notes safely (authoritative original, then mirror)")
    notes_group = notes.add_mutually_exclusive_group(required=True)
    notes_group.add_argument("--append", help="append this text as a new trailing block")
    notes_group.add_argument("--set", dest="set_text", help="replace the whole notes file with this text")
    notes.add_argument("--run")
    notes.add_argument("--token")

    stop = subparsers.add_parser("stop", help="End the run, preserving evidence")
    stop.add_argument("--reason", required=True)
    stop.add_argument("--slice-status", choices=["stopped"])
    stop.add_argument("--scavenge", action="store_true")
    stop.add_argument("--run")
    stop.add_argument("--token")

    # The four campaign commands take a node worktree path and never a run or
    # a token, so the post-command ledger hook never fires for them.
    launch_child = subparsers.add_parser(
        "launch-child",
        help="Create a node branch and worktree and launch a child PM there in tmux on a private socket",
    )
    launch_child.add_argument("--repo", required=True, help="the repository the node worktree is added to")
    launch_child.add_argument("--base", required=True, metavar="REF", help="the commit the node branch starts at")
    launch_child.add_argument("--branch", required=True, help="the node branch to create; it must not exist")
    launch_child.add_argument("--worktree", required=True, metavar="PATH", help="the node worktree; it must not exist")
    launch_child.add_argument(
        "--prompt-file",
        required=True,
        metavar="FILE",
        help="the child's launcher, copied byte for byte to .pm/campaign/launcher.md in the worktree",
    )
    launch_child.add_argument(
        "--harness", required=True, help="child PM harness: one of codex, claude, copilot, opencode, qwen"
    )
    launch_child.add_argument("--model")
    launch_child.add_argument("--effort")
    launch_child.add_argument(
        "--socket",
        metavar="NAME",
        help="the child's private tmux server; non-blank, no whitespace, no '/', not 'default', at most 64 "
        "characters, and hosting no session (default pm-child-<b>-<6 hex>, <b> the branch with every "
        "character outside [A-Za-z0-9_-] replaced by '-' and cut to 32 characters)",
    )
    launch_child.add_argument("--harness-command")
    launch_child.add_argument(
        "--setup",
        metavar="FILE",
        help="a shell script run as `sh FILE` inside the new worktree after it is created and before the child's "
        "session starts (output in .pm/campaign/setup.log); a non-zero exit or a hang fails the launch",
    )
    launch_child.add_argument(
        "--sandbox",
        metavar="FILE",
        help="an srt base policy: run the child's tmux server, and everything it starts, inside an srt sandbox "
        "(macOS); required, and matched by SHA-256, when the launcher's dossier brief pins one in ## Sandbox",
    )

    wait_child = subparsers.add_parser(
        "wait-child", help="Show evidence of a child PM's progress; a wait returns early on a signal"
    )
    wait_child.add_argument("--worktree", required=True, metavar="PATH", help="the node worktree")
    wait_child.add_argument("--wait", type=_positive_seconds)
    wait_child.add_argument(
        "--seen-marker",
        action="append",
        default=[],
        metavar="TEXT",
        help="a dialog marker (its matched literal, as printed after `dialog marker:`) that is already read and judged: "
        "it does not end the wait, and is still printed on return; repeatable. Any other marker ends the wait",
    )

    answer_child = subparsers.add_parser(
        "answer-child",
        help="Answer the child PM's open question (cm-question.md): writes cm-answer-<n>.md, archives the "
        "question as cm-question-<n>.md, and sends a one-line pointer into the child's session",
    )
    answer_child.add_argument("--worktree", required=True, metavar="PATH", help="the node worktree")
    answer_child_mode = answer_child.add_mutually_exclusive_group(required=True)
    answer_child_mode.add_argument("--text", help="the answer, delivered verbatim through a file")
    answer_child_mode.add_argument(
        "--redeliver",
        action="store_true",
        help="re-send the pointer for the latest cm-answer-<n>.md when the child did not react to it; writes "
        "nothing; refused when a newer question or the completion file exists, or there is no answer",
    )

    stop_child = subparsers.add_parser(
        "stop-child",
        help="Capture the child PM's pane, kill its private tmux server and tear down its sandbox, if it has one; "
        "exits 2 when sessions, its srt, its control dir or its temp dir are left",
    )
    stop_child.add_argument("--worktree", required=True, metavar="PATH", help="the node worktree")
    stop_child.add_argument("--reason", required=True)

    preflight_parser = subparsers.add_parser(
        "campaign-preflight",
        help="Check a campaign dossier and product repository before any session exists: the mechanical half of reconcile",
    )
    preflight_parser.add_argument(
        "--dossier", required=True, metavar="PATH", help="the dossier repository holding brief.md"
    )
    preflight_parser.add_argument("--repo", required=True, metavar="PATH", help="the product repository")
    preflight_parser.add_argument("--integration-branch", required=True, metavar="NAME")
    preflight_parser.add_argument("--worktree", metavar="PATH", help="the integration checkout, when one exists")
    preflight_parser.add_argument(
        "--digest", metavar="HEX", help="the owner's SHA-256 of brief.md; the CM always passes it"
    )
    preflight_parser.add_argument(
        "--harness",
        action="append",
        default=[],
        metavar="NAME",
        help="a seat's harness to look for on PATH; repeatable",
    )

    # Reads only the ledger directory: no run, no token, so the post-command
    # ledger hook never fires for it.
    ledger_parser = subparsers.add_parser("ledger", help="Work with the cross-run model ledger")
    ledger_commands = ledger_parser.add_subparsers(dest="ledger_command", required=True)
    render = ledger_commands.add_parser("render", help="Render every per-run ledger file into one leaderboard")
    render.add_argument(
        "--out",
        help="output path (default: <ledger dir>/leaderboard.md, or leaderboard-<run tags joined by +>.md with --run-tag)",
    )
    render.add_argument(
        "--run-tag",
        action="append",
        metavar="TEXT",
        help="render only runs with this run tag; repeatable, and untagged runs are then left out",
    )
    tag = ledger_commands.add_parser("tag", help="Retag a finished run's per-run ledger files in place")
    tag.add_argument("run_id", metavar="RUN_ID")
    tag.add_argument("--run-tag", metavar="TEXT", help="set the run tag")
    tag.add_argument(
        "--model-tag",
        action="append",
        metavar="OLD=NEW",
        help="rename a model tag on every Developer and review (OLD `untagged` selects those with no tag); "
        "repeatable, and all renames apply at once, so a swap is allowed",
    )

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


def _load_policy(args: argparse.Namespace, repo: Path | None) -> tuple[dict | None, list[str] | None]:
    """(policy, protected entries) from `--policy`, or (None, None) without it.
    `repo` is what self-protection is resolved against; None skips it."""
    if not args.policy:
        return None, None
    policy = plan_mod.load_policy(Path(args.policy).expanduser(), repo=repo)
    return policy, policy["protected_paths"]


def _print_protected_line(policy: dict | None, report: dict) -> None:
    if policy is not None:
        print(f"protected paths: {report['protected_count']} (from {policy['path']})")


def _run_check_plan(args: argparse.Namespace) -> int:
    try:
        plan_path = git_ops.resolve_plan(Path(args.plan))
    except PmError:
        # Same shape as the plan's own ERROR lines: a missing file is a plan defect, not a crash.
        print(f"ERROR: plan file not found: {Path(args.plan).expanduser().resolve()}")
        return 2
    # Resolved to the toplevel as init does, so a subdirectory finds the same
    # repository and a policy file inside it protects itself the same way.
    repo_path = git_ops.resolve_repo(Path(args.repo)) if args.repo else None
    policy, protected = _load_policy(args, repo_path)
    report = plan_mod.plan_check_report(plan_path, repo=repo_path, protected=protected)
    for error in report["errors"]:
        print(f"ERROR: {error}")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    if policy is not None and repo_path is None:
        print("WARNING: --policy without --repo: the policy file is not checked for self-protection")
    if report["errors"]:
        # As `init` does: errors and warnings, then out, so a clean-looking
        # summary never follows an ERROR line.
        return 2
    for item in report["slice_risks"]:
        why = f" ({'; '.join(item['reasons'])})" if item["reasons"] else ""
        print(f"{item['slice']}: risk {item['risk']}{why}")
    gated = ", ".join(report["approval_gated"]) if report["approval_gated"] else "none"
    print(f"{report['slice_count']} slice(s); approval-gated: {gated}")
    _print_protected_line(policy, report)
    return 0


# --- init ----------------------------------------------------------------


def _sandbox_arg(args: argparse.Namespace) -> Path | None:
    """`--sandbox` as a path, or None when the flag is absent. A sandbox
    that was asked for is established or refused: an empty or whitespace
    value is refused with PmError, never taken for the flag's absence."""
    if args.sandbox is None:
        return None
    if not args.sandbox.strip():
        raise PmError("--sandbox needs an srt base policy file; an empty value was given, so nothing was created")
    return Path(args.sandbox)


def _run_init(args: argparse.Namespace) -> int:
    sandbox_policy = _sandbox_arg(args)
    model_tag = state_mod.tag_value(args.model_tag, flag="--model-tag")
    run_tag = state_mod.tag_value(args.run_tag, flag="--run-tag")
    repo = git_ops.resolve_repo(Path(args.repo))
    plan_path = git_ops.resolve_plan(Path(args.plan))

    # Loaded exactly once: init freezes these entries into run state, and
    # nothing reads the policy file again for the rest of the run.
    policy, protected = _load_policy(args, repo)
    report = plan_mod.plan_check_report(plan_path, repo=repo, protected=protected)
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
        model_tag=model_tag,
        run_tag=run_tag,
        policy=policy,
        sandbox_policy=sandbox_policy,
    )

    for warning in result.sandbox_warnings:
        print(f"WARNING: {warning}")
    print(f"run id: {result.run_id}")
    print(f"state dir: {result.run_dir}")
    print(f"branch: {result.branch}")
    print("slices:")
    for entry, plan_slice in zip(result.state["slices"], result.slices, strict=True):
        print(
            f"  {entry['id']:<10} {plan_slice.title:<40} risk={entry['risk']:<9} status={entry['status'] or 'pending'}"
        )
    _print_protected_line(policy, report)
    sandbox_block = result.state.get("sandbox")
    if sandbox_block:
        print(f"sandbox: control dir {sandbox_block['control_dir']}")
    # The warning is on stderr so stdout carries the token line and nothing that reads as prose after it.
    print(
        "Keep this token out of Developer sessions — it authorizes PM state writes; "
        "the operator or PM agent should hold it, never the harness being supervised.",
        file=sys.stderr,
    )
    print(f"PM_RUN_TOKEN={result.token}")
    if sandbox_block:
        # Every later command of this run refuses without it.
        print(f"export PM_TMUX_SOCKET={sandbox_block['socket']}")
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
        # Refused like plain `status`, so the two forms never disagree.
        sandbox.require_run_socket(state)
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
    sandbox_block = state.get("sandbox")
    if isinstance(sandbox_block, dict):
        print(
            f"sandbox: {sandbox_block.get('policy')} (sha256 {(sandbox_block.get('policy_sha256') or '')[:12]}) "
            f"socket {sandbox_block.get('socket')}"
        )
    print(f"stop reason: {state.get('stop_reason')}")

    # Same derivation the report uses, so run time never needs events.jsonl
    # parsed by hand. Labelled "recorded event span" rather than "elapsed":
    # `status` appends no event, so on a quiet live run this ends at the last
    # recorded activity, not now — calling that elapsed time would overstate it.
    elapsed = state_mod.run_elapsed(state_mod.read_events(run_dir))
    if elapsed is None:
        print("recorded event span: unknown (no timestamped events)")
    else:
        first_ts, last_ts, duration = elapsed
        print(f"recorded event span: {duration} ({first_ts} → {last_ts})")

    # The attempt ceiling is printed alongside every attempt count so pacing a
    # steer never requires reading run.json directly.
    max_attempts = (state.get("policy") or {}).get("max_attempts", 10)
    print("slices:")
    for entry in state.get("slices", []):
        commit = entry.get("commit")
        commit_short = commit[:10] if commit else "-"
        print(
            f"  {entry['id']:<10} status={entry.get('status') or 'pending':<10} "
            f"risk={entry.get('risk'):<9} attempts={entry.get('attempts', 0)}/{max_attempts} "
            f"commit={commit_short}"
        )

    missing = judgments.unjudged_review_ids(state)
    if missing:
        print("unjudged reviews: " + ", ".join(f"{slice_id}/{review_id}" for slice_id, review_id in missing))
    else:
        print("unjudged reviews: none")

    unranked = judgments.unranked_code_review_ids(state)
    if unranked:
        print(
            "code panels without an order: " + ", ".join(f"{slice_id}/{review_id}" for slice_id, review_id in unranked)
        )
    else:
        print("code panels without an order: none")

    events = state_mod.read_events(run_dir)
    missing_developers = judgments.unjudged_developer_origins(state, events)
    if missing_developers:
        print(
            "unjudged developer submissions: "
            + ", ".join(f"{slice_id}/event-{index} ({kind})" for slice_id, index, kind in missing_developers)
        )
    else:
        print("unjudged developer submissions: none")

    current = state.get("current_slice")
    if current:
        alive = result.current_session_alive
        before_head = (current.get("before_head") or "")[:12]
        print(
            f"current slice: {current.get('id')}  session={current.get('tmux_session')} "
            f"alive={alive}  before_head={before_head}…  started_at={current.get('started_at')} "
            f"attempts={current.get('attempts')}/{max_attempts}"
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


# --- grant --------------------------------------------------------------------


def _run_grant(args: argparse.Namespace) -> int:
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    outcome = slice_ops.grant(repo, run_dir, token, slice_id=args.slice, path=args.path, evidence=args.evidence)
    print(f"granted {outcome.slice_id}: {outcome.path}")
    print(f"evidence recorded; grants on this slice: {outcome.grant_count}")
    if outcome.elevated:
        print("risk: elevated (raised by this grant)")
    # Said plainly because the staling is the part a PM can act on wrongly: any
    # review it already commissioned for this slice no longer counts, and
    # `finalize --accept` will refuse until both are re-run past the last grant.
    print(
        "acceptance now requires a fresh drift-audit and code-review; any review already recorded for "
        "this slice is stale — re-commission both after your last grant for this slice"
    )
    return 0


# --- start-slice --------------------------------------------------------------


def _run_start_slice(args: argparse.Namespace) -> int:
    model_tag = state_mod.tag_value(args.model_tag, flag="--model-tag")
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    outcome = slice_ops.start_slice(
        repo,
        run_dir,
        token,
        model=args.model,
        effort=args.effort,
        model_tag=model_tag,
        reviewer_tools=args.reviewer_tools,
        harness_command=args.harness_command,
        risk=args.risk,
    )

    if outcome.kind == "all_complete":
        print("all slices complete")
        for note in outcome.sandbox_notes or []:
            print(f"sandbox: {note}")
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
    if outcome.server_started_on:
        print(f"sandbox: started the run's sandboxed tmux server on socket {outcome.server_started_on}")
    print(f"{verb} {outcome.slice_id} (attempt {outcome.attempt}) in tmux session {outcome.session}")
    if outcome.reaped:
        print(f"reaped stale sessions: {', '.join(outcome.reaped)}")
    if outcome.notes_warning:
        print(f"WARNING: {outcome.notes_warning}")
    return 0


# --- observe ------------------------------------------------------------------


def _print_question(outcome) -> None:
    """The open-question lines `observe` and `wait-child` share.

    The text is written by the session being watched, so every line of it is
    printed as `| <line>` between the fences, with C0 control characters
    other than tab removed. The prefix lets the reader tell the Developer's
    text from the report's own lines (a `--- end question ---` inside it is
    still prefixed), and the stripped controls keep terminal escapes out of
    the supervisor's context. This is presentation, not a trust boundary:
    the text is still the watched session's words.
    """
    if not outcome.question_present:
        print("question present: no")
        return
    print("question present: yes")
    print("--- question ---")
    if outcome.question_text is None:
        print("(question file could not be read)")
    else:
        for line in _QUESTION_CONTROL_RE.sub("", outcome.question_text).splitlines():
            print(f"| {line}")
    print("--- end question ---")


def _print_dialog_marker(outcome) -> None:
    if outcome.dialog_markers["present"]:
        # Named as a signal, not a verdict: no fact reads this, the supervisor
        # decides what a marker means. Carries the matched literal for the
        # same reason the send refusals do — a kind alone cannot be judged.
        print(f"dialog marker: {sessions.dialog_marker_detail(outcome.dialog_markers)}")
    else:
        print("dialog marker: clear")


def _print_no_signal_note(outcome, actions: str) -> None:
    # Printed, never enforced — wait length is the supervisor's judgement call.
    if outcome.no_signal:
        print(
            "note: this wait returned no signal. Re-asking at the same length cannot "
            f"return anything a longer wait would not have. Either wait longer, or act: {actions}."
        )


def _print_delivery(delivered: bool) -> None:
    """The one line saying whether a pointer's Enter visibly reached the pane (`sessions.Delivery`)."""
    if delivered:
        print("delivery: confirmed")
    else:
        print(
            "delivery: unconfirmed (the pane did not change after Enter; re-check with observe/wait-child, "
            "then --redeliver if the child did not react)"
        )


def _run_observe(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    sandbox.require_run_socket(state_mod.load_state(run_dir))
    outcome = slice_ops.observe(
        repo, run_dir, wait=args.wait, token=_resolve_token(args), seen_markers=args.seen_marker
    )

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
    _print_question(outcome)
    _print_dialog_marker(outcome)
    _print_no_signal_note(outcome, "send a nudge, finalize --steer, relaunch with start-slice, or stop")

    print("--- pane tail ---")
    print(outcome.tail)
    return 0


# --- send ---------------------------------------------------------------------


def _run_send(args: argparse.Namespace) -> int:
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    sandbox.require_run_socket(state_mod.load_state(run_dir))
    delivery = slice_ops.send(repo, run_dir, token, text=args.text, reason=args.reason)
    print(f"sent: {args.text!r} ({args.reason})")
    _print_delivery(delivery.confirmed)
    return 0


# --- answer -------------------------------------------------------------------


def _run_answer(args: argparse.Namespace) -> int:
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    sandbox.require_run_socket(state_mod.load_state(run_dir))
    outcome = slice_ops.answer(repo, run_dir, token, text=args.text)
    print(f"answered {outcome.slice_id}: {outcome.answer_path}")
    print(f"question archived: {outcome.question_archived_path}")
    _print_delivery(outcome.delivered)
    return 0


# --- finalize (bare, and its --accept/--steer/--stop decision paths) ----------


def _print_floor_facts(report) -> None:
    for fact in report.facts:
        status = "PASS" if fact.passed else "FAIL"
        print(f"{fact.number} {fact.name} {status} {fact.detail}")


_PANE_TAIL_LINES = 40


def _print_pane_tail(repo: Path, pane_path: Path) -> None:
    """Print the tail of the captured pane on every path that records an assessment.

    No floor fact reads the pane any more, so PM's own read is the only thing
    standing where fact 8 stood — and an unread pane is exactly the failure to
    design against. Printing it here means deciding a slice without the pane
    on screen takes deliberate effort, and SKILL.md requires the acceptance
    reasoning to say what it showed, so a skipped read is visible in the
    assessment rather than merely undetectable.

    `--steer` deliberately does not print it: a steer records no assessment
    and the session it is correcting is still running, so its pane is a moving
    target that the next `finalize` before acceptance shows properly.
    """
    # Read through the guard: the pane file is in the sandbox-writable `.pm/`
    # tree, and a planted link must not print a denied file to this terminal.
    try:
        lines = pm_paths.read_pm_text(repo, pane_path, errors="replace").splitlines()
    except FileNotFoundError:
        print("pane: (not captured — no live session at finalize)")
        return
    except PmError as exc:
        print(f"pane: (not shown — {exc})")
        return
    if not lines:
        # Said outright rather than printed as "0 of 0 lines", which reads like
        # a clear pane. An empty capture is absence of evidence, not evidence
        # of absence, and the reasoning that cites it should say which.
        print(f"pane: (capture at {pane_path} is empty — the session wrote nothing, or had already exited)")
        return
    print(f"pane tail ({min(len(lines), _PANE_TAIL_LINES)} of {len(lines)} lines, full capture at {pane_path}):")
    for line in lines[-_PANE_TAIL_LINES:]:
        print(f"  | {line}")


def _run_finalize(args: argparse.Namespace) -> int:
    # Checked before anything is read or written, so a refused combination
    # leaves run.json and events.jsonl untouched.
    if args.stop is not None and args.cause is None:
        raise PmError("finalize --stop requires --cause plan|developer|environment")
    if args.cause is not None and args.stop is None:
        raise PmError("--cause is only valid with finalize --stop")
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    # Every finalize form drives the run's tmux server, so a sandboxed run's
    # socket must be the one exported.
    sandbox.require_run_socket(state_mod.load_state(run_dir))

    # `is not None`, not truthiness: `--accept ""` is a decision that was
    # made and typed badly, not an absent one. Under truthiness it fell
    # through to the bare evidence-only finalize, which prints a floor report
    # and records nothing — so a PM that believed it had accepted, steered, or
    # stopped the slice would see a plausible success and no decision. Each
    # decision path refuses blank text itself, so the empty string now fails
    # closed with a message naming the flag.
    if args.accept is not None:
        outcome = slice_ops.finalize_accept(repo, run_dir, token, reasoning=args.accept, risk=args.risk)
        print(f"slice: {outcome.slice_id}")
        _print_floor_facts(outcome.report)
        _print_pane_tail(repo, outcome.pane_path)
        if outcome.kind == "accepted":
            if outcome.code_warning:
                print(f"pm: code metrics: {outcome.code_warning}", file=sys.stderr)
            print(f"ACCEPTED {outcome.slice_id}")
            if outcome.criteria_note:
                print(outcome.criteria_note)
            print(f"assessment: {outcome.assessment_path}")
            for note in outcome.sandbox_notes or []:
                print(f"sandbox: {note}")
            return 0
        print(f"pm: refused: {outcome.message}", file=sys.stderr)
        return 1

    if args.steer is not None:
        outcome = slice_ops.finalize_steer(repo, run_dir, token, correction=args.steer, risk=args.risk)
        if outcome.kind == "steered":
            print(f"steered {outcome.slice_id} (attempt {outcome.attempts})")
            _print_delivery(outcome.delivered)
            print(f"correction: {outcome.correction_path}")
            return 0
        print(f"pm: error: {outcome.message}", file=sys.stderr)
        return 2

    if args.stop is not None:
        outcome = slice_ops.finalize_stop(repo, run_dir, token, reason=args.stop, cause=args.cause, risk=args.risk)
        print(f"STOPPED {outcome.slice_id}")
        _print_floor_facts(outcome.report)
        if outcome.pane_error is not None:
            print(f"pane: (not captured — {outcome.pane_error})")
        else:
            _print_pane_tail(repo, outcome.pane_path)
        print(f"assessment: {outcome.assessment_path}")
        return 0

    outcome = slice_ops.finalize(repo, run_dir, token, risk=args.risk)
    print(f"slice: {outcome.slice_id}")
    _print_floor_facts(outcome.report)
    print(f"attempts: {outcome.attempts}/{outcome.max_attempts}")
    print(f"evidence: status-before={outcome.status_before_path}")
    print(f"evidence: status-after={outcome.status_after_path}")
    print(f"evidence: diff={outcome.diff_path}")
    print(f"evidence: result={outcome.result_path}")
    _print_pane_tail(repo, outcome.pane_path)
    return 0 if outcome.report.passed else 1


# --- review -----------------------------------------------------------------


def _load_adjudications(args: argparse.Namespace) -> list[str]:
    """Combine --adjudicated-file lines (file order first) with --adjudicated flags.

    Reusing one file across a panel's review commissions guarantees byte-identical
    adjudication context, which judge-reviews' panel comparison requires; retyping
    the same ruling per invocation risks the wording drift that breaks it instead.
    """
    items: list[str] = []
    if args.adjudicated_file:
        try:
            lines = Path(args.adjudicated_file).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise PmError(f"could not read --adjudicated-file {args.adjudicated_file}: {exc}") from exc
        rulings = [line for line in lines if line.strip() and not line.strip().startswith("#")]
        # Fail closed: a named file with no ruling would commission a panel member
        # with an adjudication context that silently differs from its siblings'.
        if not rulings:
            raise PmError(
                f"--adjudicated-file {args.adjudicated_file} holds no ruling (it is empty or only blank and # lines)"
            )
        items.extend(rulings)
    if args.adjudicated:
        items.extend(args.adjudicated)
    return items


def _run_review(args: argparse.Namespace) -> int:
    model_tag = state_mod.tag_value(args.model_tag, flag="--model-tag")
    token = _require_token(args)
    repo = _repo_from_cwd()
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    adjudications = _load_adjudications(args)
    outcome = review_mod.run_review(
        repo,
        run_dir,
        token,
        slice_id=args.slice,
        skill=args.skill,
        tool=args.tool,
        model=args.model,
        effort=args.effort,
        model_tag=model_tag,
        reviewer_command=args.reviewer_command,
        timeout=args.timeout,
        # Rendered one-per-line so the reviewer reads a list, not a run-on
        # sentence; None when unused, which the template shows as `none`.
        # Newlines inside an item are collapsed: the block sits above the frozen
        # contract in the prompt, so a multi-line item could otherwise imitate a
        # later prompt section rather than read as one ruling.
        pm_adjudications=(
            "\n".join(f"- {' '.join(item.split())}" for item in adjudications) if adjudications else None
        ),
    )
    print(f"slice: {outcome.slice_id}")
    print(f"skill: {outcome.skill}  tool: {outcome.tool}")
    tag = f"  tag: {outcome.model_tag}" if outcome.model_tag is not None else ""
    print(f"review id: {outcome.review_id}  model: {outcome.model}  effort: {outcome.effort}{tag}")
    print(f"reviewed head: {outcome.head}  before_head: {outcome.before_head}")
    print(f"diff: {outcome.diff_path}")
    print(f"changed files: {len(outcome.changed_files)}")
    print(f"report: {outcome.artifact_path}")
    print(f"sha256: {outcome.sha256}")
    return 0


def _run_judgment(args: argparse.Namespace, *, recorder, event_kind: str, label: str) -> int:
    repo = _repo_from_cwd()
    token = _require_token(args)
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    data = judgments.load_input(Path(args.file))
    outcome = recorder(run_dir, token, data)
    try:
        judgments.publish_event(run_dir, token, outcome.judgment_id, outcome.slice_id, kind=event_kind)
    except IntegrityError:
        raise
    except (OSError, PmError) as exc:
        raise PmError(
            f"{label} judgment {outcome.slice_id}/{outcome.judgment_id} was stored but event publication failed; "
            f"resolve the publication error, then retry the same input: {exc}"
        ) from exc
    verb = "recorded" if outcome.created else "already recorded"
    print(f"{label} judgment {verb}: {outcome.judgment_id}")
    return 0


def _run_judge_reviews(args: argparse.Namespace) -> int:
    return _run_judgment(
        args,
        recorder=judgments.record_judgment,
        event_kind="review-judgment",
        label="review",
    )


def _run_judge_developer(args: argparse.Namespace) -> int:
    return _run_judgment(
        args,
        recorder=judgments.record_developer_judgment,
        event_kind="developer-judgment",
        label="developer",
    )


# --- stop -----------------------------------------------------------------


def _run_stop(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()

    if args.scavenge:
        # A readable sandboxed run with the wrong socket is refused here, and
        # never falls through to the sweep below: that sweep would reach
        # whichever server PM_TMUX_SOCKET names, not the run's. A run.json
        # the OS will not read (a directory, no permission) is unreadable
        # state too, so the state-free sweep below still runs.
        readable_dir: Path | None = None
        try:
            readable_dir = state_mod.resolve_run_dir(repo, args.run)
            readable = state_mod.load_state(readable_dir)
        except (PmError, OSError):
            readable = None
        if readable is not None:
            try:
                sandbox.require_run_socket(readable)
            except PmError as exc:
                print(f"pm: scavenge: refused ({exc}); nothing was swept", file=sys.stderr)
                return 2
            if sandbox.records_sandbox(readable) and not _resolve_token(args):
                return _scavenge_without_token(repo, readable_dir, readable)
        # Only the stop itself is in the try: a failure of the sweep or a
        # print after a stop that succeeded is never relabelled below.
        try:
            token = _require_token(args)
            run_dir = state_mod.resolve_run_dir(repo, args.run)
            # `stop` authenticates and loads state itself and reports the run
            # id back, so this path reads state once. Unreadable or tampered
            # state still raises PmError (or OSError) from inside `stop`
            # before anything is killed, and a missing token before `stop`,
            # landing in the fallback sweep; a stop recorded before its
            # teardown failed lands there too, as `StopTeardownError`.
            outcome = slice_ops.stop(repo, run_dir, token, reason=args.reason, slice_status=args.slice_status)
        except (PmError, OSError) as exc:
            return _scavenge_after_failed_stop(repo, args, readable_dir, readable, exc)
        extra_killed = slice_ops.stop_scavenge_sweep(run_id=outcome.run_id)
        all_killed = outcome.killed + [name for name in extra_killed if name not in outcome.killed]
        print(f"stopped run {outcome.run_id}; killed sessions: {all_killed}")
        if outcome.pane_error is not None:
            print(f"pane: (not captured — {outcome.pane_error})")
        for note in outcome.sandbox_notes or []:
            print(f"sandbox: {note}")
        return 0

    token = _require_token(args)
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    outcome = slice_ops.stop(repo, run_dir, token, reason=args.reason, slice_status=args.slice_status)
    print(f"stopped run {outcome.run_id}; killed sessions: {outcome.killed}")
    if outcome.pane_error is not None:
        print(f"pane: (not captured — {outcome.pane_error})")
    for note in outcome.sandbox_notes or []:
        print(f"sandbox: {note}")
    return 0


def _scavenge_after_failed_stop(
    repo: Path, args: argparse.Namespace, readable_dir: Path | None, readable: dict | None, exc: Exception
) -> int:
    """`stop --scavenge` once `stop` itself failed with `exc`: the state-free
    sweep of the run's sessions and its `pm-run-<run id>` server.

    The first line says why the stop did not finish: its teardown failed
    after the stop was recorded (`StopTeardownError`), state was unavailable,
    readable state came with no token, or the stop failed otherwise. The run
    id is `--run`, except that readable state names its run by its
    directory, `--run` or not, when it records a sandbox or the stop was
    recorded; an unsandboxed run keeps the original sweep. The `pm-run-<run
    id>` server is looked for under that id, or, without `--run`, under the
    run directory's name when one resolved (unreadable state still names its
    run), and killed through `sandbox.kill_run_server`, which waits for the
    run record's `srt` when readable state leads to one. Exits 2 when that
    kill leaves sessions listed or `srt` running, or the server cannot be
    checked, when readable sandboxed state still has anything of its
    sandbox after the kill (`sandbox.sandbox_remains`), and whenever the
    stop's teardown failed: no `sandbox-stopped` event records it, whatever
    the kill here did."""
    teardown_failed = isinstance(exc, slice_ops.StopTeardownError)
    run_id = args.run
    if readable is not None and readable_dir is not None and (sandbox.records_sandbox(readable) or teardown_failed):
        run_id = readable_dir.name
    killed = slice_ops.stop_scavenge_sweep(run_id=run_id)
    if teardown_failed:
        what = "stop recorded but teardown failed"
    elif readable is None:
        what = "state unavailable"
    else:
        what = "stop failed" if _resolve_token(args) else "no token, so no stop was recorded"
    print(f"pm: scavenge: {what} ({exc}); swept sessions: {killed}")
    # A sandboxed run's server is named after its id, so it can be found
    # without state; a name tmux cannot take is no run's.
    server_run_id = run_id or (readable_dir.name if readable_dir is not None else None)
    server_left = False
    if server_run_id and re.fullmatch(r"[A-Za-z0-9._-]+", server_run_id):
        record = sandbox.run_record(readable, repo=repo) if readable is not None else None
        server_left = _scavenge_run_server(f"{sandbox.RUN_SOCKET_PREFIX}{server_run_id}", record)
    if readable is not None and sandbox.records_sandbox(readable):
        try:
            left = sandbox.sandbox_remains(readable, repo=repo)
        except PmError as remains_exc:
            left = [f"its sandbox record cannot be checked ({remains_exc})"]
        if left:
            print(
                f"pm: scavenge: the run's sandbox is not torn down: {'; '.join(left)}; finish it with `stop` and"
                " the run's token",
                file=sys.stderr,
            )
            return 2
    if teardown_failed:
        print(
            "pm: scavenge: the stop's sandbox teardown failed, and no sandbox-stopped event records it", file=sys.stderr
        )
        return 2
    return 2 if server_left else 0


def _scavenge_run_server(socket: str, record: dict | None) -> bool:
    """Kill the run's server on `socket` (`sandbox.kill_run_server`, which
    waits for `record`'s `srt`) and print what that did; return whether
    anything of it is left (a session listed, `srt` running) or it could not
    be checked. Nothing is printed when no server answers."""
    try:
        kill = sandbox.kill_run_server(socket, record)
    except PmError as exc:
        print(f"pm: scavenge: could not check tmux server {socket}: {exc}")
        return True
    if kill is None:
        return False
    remaining, srt_alive = kill
    killed_note = f"still lists sessions: {', '.join(remaining)}" if remaining else "killed"
    if srt_alive:
        killed_note += "; its srt still runs"
    print(f"pm: scavenge: sandboxed tmux server {socket} {killed_note}")
    return bool(remaining) or srt_alive


def _scavenge_without_token(repo: Path, run_dir: Path, state: dict) -> int:
    """`stop --scavenge` on a sandboxed run with readable state but no token:
    the run's sessions swept and its sandbox torn down from the host-written
    control record (`sandbox.scavenge_run`, which kills `pm-run-<run id>`),
    with nothing written to run state, which cannot be signed without the
    token. The run id is the run directory's own name, which `scavenge_run`
    holds the state to, so `--run` is not needed."""
    killed = slice_ops.stop_scavenge_sweep(run_id=run_dir.name)
    print(f"pm: scavenge: no token, so run state was read but not updated (no stop recorded); swept sessions: {killed}")
    try:
        notes = sandbox.scavenge_run(state, repo=repo, run_dir=run_dir)
    except PmError as exc:
        raise PmError(f"scavenge: swept the run's sessions, but could not tear its sandbox down: {exc}") from exc
    for note in notes or []:
        print(f"sandbox: {note}")
    return 0


# --- campaign child commands -----------------------------------------------


def _run_launch_child(args: argparse.Namespace) -> int:
    sandbox_policy = _sandbox_arg(args)
    outcome = campaign.launch_child(
        repo=Path(args.repo),
        base=args.base,
        branch=args.branch,
        worktree=Path(args.worktree),
        prompt_file=Path(args.prompt_file).expanduser(),
        harness=args.harness,
        model=args.model,
        effort=args.effort,
        socket=args.socket,
        harness_command=args.harness_command,
        setup=Path(args.setup) if args.setup else None,
        sandbox=sandbox_policy,
    )
    for warning in outcome.sandbox_warnings:
        print(f"WARNING: {warning}")
    print(
        f"child launched: session={outcome.session} socket={outcome.socket} "
        f"worktree={outcome.worktree} launcher={outcome.launcher}"
    )
    if outcome.control_dir is not None:
        print(f"sandbox: control dir {outcome.control_dir}")
    print(f"git dir: {outcome.git_dir}")
    if outcome.node_dir is not None:
        print(f"node dir: {outcome.node_dir} at dossier commit {outcome.dossier_commit}")
    else:
        print("node dir: none (the launcher lies in no git repository; no dossier commit recorded)")
    print(f"watch: {outcome.watch}")
    return 0


def _run_campaign_preflight(args: argparse.Namespace) -> int:
    checks = preflight.campaign_preflight(
        dossier=Path(args.dossier),
        repo=Path(args.repo),
        integration_branch=args.integration_branch,
        worktree=Path(args.worktree) if args.worktree else None,
        digest=args.digest,
        harnesses=tuple(args.harness),
    )
    for check in checks:
        if check.passed is None:
            print(f"note  {check.name}")
        elif check.passed:
            print(f"ok    {check.name}")
        else:
            print(f"FAIL  {check.name}")
            print(f"      -> {check.detail}")
    failed = [check for check in checks if check.passed is False]
    if failed:
        print(f"campaign preflight: {len(failed)} check(s) failed; every failure is a question to the owner")
        return 1
    compared = any(check.name == "brief.md matches the owner's digest" for check in checks)
    print("campaign preflight: all checks passed" + ("" if compared else "; no owner digest was compared"))
    return 0


def _run_wait_child(args: argparse.Namespace) -> int:
    outcome = campaign.wait_child(Path(args.worktree), wait=args.wait, seen_markers=args.seen_marker)
    if args.wait:
        print(f"waited: {outcome.elapsed_seconds:.1f}s (requested {args.wait:g}s)")
    print(f"session running: {outcome.running}")
    # `child-result.json` is written by the child, whose sandbox can write it.
    status_note = (
        f" (status={outcome.result_status}; unverified: written by the child)" if outcome.result_status else ""
    )
    print(f"result present: {outcome.result_present}{status_note}")
    _print_question(outcome)
    _print_dialog_marker(outcome)
    _print_no_signal_note(outcome, "attach to the child's tmux server and read it, or stop-child")
    if outcome.runs_error is not None:
        print(f"child runs: state root unavailable ({outcome.runs_error})")
    for run in outcome.runs:
        print(f"child run {run.run_id}: status={run.status} stop_reason={run.stop_reason} (unverified: tokenless read)")
    if outcome.pane_live_error is not None:
        print(f"pane-live: not written ({outcome.pane_live_error})")
    print("--- pane tail ---")
    print(outcome.tail)
    return 0


def _run_answer_child(args: argparse.Namespace) -> int:
    outcome = campaign.answer_child(Path(args.worktree), text=args.text, redeliver=args.redeliver)
    if args.redeliver:
        print(f"redelivered: {outcome.answer_path}")
    else:
        print(f"answered: {outcome.answer_path}")
        print(f"question archived: {outcome.question_archived_path}")
    _print_delivery(outcome.delivered)
    return 0


def _run_stop_child(args: argparse.Namespace) -> int:
    outcome = campaign.stop_child(Path(args.worktree))
    if outcome.runs_error is not None:
        print(f"child runs: state root unavailable ({outcome.runs_error}); recorded reviewer process groups unknown")
    if outcome.reviewer_pgids:
        pgids = ", ".join(str(pgid) for pgid in outcome.reviewer_pgids)
        print(
            f"reviewer process groups the child recorded: {pgids}; they are outside the child's tmux server "
            "— verify and kill them by hand"
        )
    if outcome.pane_error is not None:
        print(f"pane: not captured ({outcome.pane_error})")
    elif outcome.pane_kept:
        print(f"pane: {outcome.pane_path} (kept from an earlier capture; the session was already gone)")
    else:
        print(f"pane: {outcome.pane_path}")
    state = "was running" if outcome.was_running else "was not running"
    if outcome.server_gone:
        server = f"killed tmux server {outcome.socket}"
    elif outcome.kill_error is not None:
        server = f"tmux server {outcome.socket} could not be confirmed killed ({outcome.kill_error})"
    else:
        server = f"tmux server {outcome.socket} still has sessions: {', '.join(outcome.remaining)}"
    print(f"stopped child: session={outcome.session} ({state}); {server}")
    for line in outcome.sandbox_notes:
        print(line)
    if outcome.sandbox_error is not None:
        print(f"sandbox: not torn down ({outcome.sandbox_error})")
    print(f"reason: {args.reason}")
    # Sessions still listed after the kill, or a sandbox left up: the child is not stopped.
    return 0 if outcome.server_gone and outcome.sandbox_stopped else 2


# --- notes ----------------------------------------------------------------


def _run_notes(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    token = _require_token(args)
    run_dir = state_mod.resolve_run_dir(repo, args.run)
    # MAC-verified: notes are a PM-only write, and the run id comes from
    # authenticated state.
    state = slice_ops.load_writable_state(run_dir, token)
    mode, text = ("append", args.append) if args.append is not None else ("set", args.set_text)
    original, warning = slice_ops.write_notes(repo, run_dir, state["run_id"], text=text, mode=mode)
    print(f"notes {mode}: {original}")
    mirror = slice_ops.notes_path(repo, state["run_id"])
    print(f"mirror: {mirror}")
    if warning:
        print(f"WARNING: {warning}")
    return 0


# --- ledger render and tag ------------------------------------------------


def _run_ledger_render(args: argparse.Namespace) -> int:
    run_tags = frozenset(state_mod.tag_value(tag, flag="--run-tag") for tag in args.run_tag or [])
    root = ledger.ledger_dir()
    files = leaderboard.load_run_files(root)
    if not files:
        raise PmError(f"no per-run ledger files found under {root}")
    if args.out:
        out = Path(args.out)
    elif run_tags:
        # A filtered view beside the default leaderboard, never over it.
        joined = "+".join(re.sub(r"[^\w.-]+", "_", tag) for tag in sorted(run_tags))
        out = root / f"leaderboard-{joined}.md"
    else:
        out = root / "leaderboard.md"
    # The write replaces `out` itself, never a symlink's target, so the
    # History link is relative to the directory `out` is named in.
    try:
        text, errors = leaderboard.render_leaderboard(files, out_dir=out.parent.resolve(), run_tags=run_tags)
    except PmError as exc:
        raise PmError(f"{exc} under {root}") from exc
    try:
        state_mod._atomic_write_bytes(out, text.encode("utf-8"))
    except OSError as exc:
        raise PmError(f"cannot write the leaderboard to {out}: {exc}") from exc
    print(f"leaderboard: {out}")
    if run_tags and not args.out:
        print(f"filtered by run tag; the default {root / 'leaderboard.md'} is untouched")
    for error in errors:
        print(f"WARNING: excluded {error}")
    return 0


def _run_ledger_tag(args: argparse.Namespace) -> int:
    model_tags: dict[str, str] = {}
    for form in args.model_tag or []:
        old, sep, new = form.partition("=")
        if not sep or not old or "=" in new:
            raise PmError(f"--model-tag must be OLD=NEW with a non-empty OLD and one '=' (got {form!r})")
        if old in model_tags:
            raise PmError(f"--model-tag {old!r} is given twice")
        model_tags[old] = new
    if args.run_tag is None and not model_tags:
        raise PmError("ledger tag needs --run-tag, --model-tag, or both")
    for path in leaderboard.retag_run_files(
        ledger.ledger_dir(), args.run_id, run_tag=args.run_tag, model_tags=model_tags
    ):
        print(f"retagged: {path}")
    return 0


def _run_ledger(args: argparse.Namespace) -> int:
    return {"render": _run_ledger_render, "tag": _run_ledger_tag}[args.ledger_command](args)


_HANDLERS = {
    "check-plan": _run_check_plan,
    "init": _run_init,
    "status": _run_status,
    "approve": _run_approve,
    "grant": _run_grant,
    "start-slice": _run_start_slice,
    "observe": _run_observe,
    "send": _run_send,
    "answer": _run_answer,
    "finalize": _run_finalize,
    "review": _run_review,
    "judge-reviews": _run_judge_reviews,
    "judge-developer": _run_judge_developer,
    "notes": _run_notes,
    "stop": _run_stop,
    "ledger": _run_ledger,
    "launch-child": _run_launch_child,
    "wait-child": _run_wait_child,
    "answer-child": _run_answer_child,
    "stop-child": _run_stop_child,
    "campaign-preflight": _run_campaign_preflight,
}


def _write_ledger(args: argparse.Namespace) -> str | None:
    """Regenerate the run's ledger file after a command; return a failure, or None.

    Runs for every run-scoped command (one with a `--run` option) that has a
    token, so the per-run file follows every decision, judgment and review
    without a hand-kept command list. Silent when there is no run to write
    for (no repo, no run directory, or no `run.json`, as after
    `stop --scavenge` with the state gone). Any other failure, of any type,
    is returned for `main` to print: the ledger is never allowed to block or
    undo the command that triggered it.
    """
    if not hasattr(args, "run"):
        return None
    token = _resolve_token(args)
    if not token:
        return None
    try:
        try:
            repo = _repo_from_cwd()
            run_dir = state_mod.resolve_run_dir(repo, args.run)
        except PmError:
            return None
        if not (run_dir / "run.json").exists():
            return None
        ledger.write_run_file(run_dir, token)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        handler = _HANDLERS.get(args.command)
        if handler is None:
            parser.error(f"unknown command: {args.command}")
            return 2
        code = handler(args)
    except IntegrityError as exc:
        print(f"pm: error: INTEGRITY: {exc}", file=sys.stderr)
        return 2
    except PmError as exc:
        print(f"pm: error: {exc}", file=sys.stderr)
        code = 2

    failure = _write_ledger(args)
    if failure is not None:
        print(f"pm: ledger: not written: {failure}", file=sys.stderr)
        # `status --report` is the finish step: a run whose ledger writes all
        # failed must be noticed there, before its worktree is removed.
        if args.command == "status" and args.report:
            return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())

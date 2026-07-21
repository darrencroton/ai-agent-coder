"""Command orchestration for the slice lifecycle (target-design §3/§12).

This module wires the pieces other `pm_lib` modules already provide —
`state`, `plan`, `git_ops`, `sessions`, `profiles`, `prompts` — into the
per-command sequences described in target-design. Most of it still decides
nothing semantic: `init`/`status`/`approve`/`start-slice`/`observe`/`send`
and bare `finalize` only mutate state through the token-authenticated
`state` module, drive tmux through `sessions`, or read git/filesystem facts
through `git_ops` — `floor.py` computes the facts, never a verdict.

The one place semantic judgement enters this module is `finalize_accept` /
`finalize_steer` / `finalize_stop`: each is an explicit, recorded act the PM
agent takes through the CLI (never inferred from evidence alone), gated by
the floor (never waivable) and, on elevated slices, by review freshness
(design §5). Assessment text assembles facts around the PM's own reasoning
text; it never invents that reasoning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import IntegrityError, PmError
from . import git_ops
from . import plan as plan_mod
from . import profiles
from . import prompts
from . import sessions
from . import state as state_mod
from .floor import FloorReport, evaluate_floor

_SLICE_ID_RE = re.compile(r"^Slice\s+(?P<number>\d+)$")

# Artifact rotation / observe polling.
_OBSERVE_POLL_SECONDS = 2.0
_OBSERVE_TAIL_LINES = 40

# Controller-owned notes.md tripwire (target-design §10): a hard cap kept as
# a non-fatal warning, since a runaway notes file silently degrades every
# later Developer prompt.
_NOTES_SIZE_CAP_BYTES = 512 * 1024
# Branches a run must never land on by *implicit* default (an explicit
# --branch main is still honoured); per-slice commits piling onto a shared
# default branch is the PM Test 20 branch-default footgun.
_PROTECTED_DEFAULT_BRANCHES = frozenset({"main", "master"})

# The stop_reason recorded on attempt-budget exhaustion. Load-bearing: the
# exhaustion guard below matches on it, which is what makes the budget a
# genuine terminal stop (design §11 "mandatory stop") rather than a status
# note — after exhaustion, only `finalize --stop` (record the story) and
# `stop` remain available for the slice.
_BUDGET_EXHAUSTED_REASON = "attempt budget exhausted"


def _refuse_if_budget_exhausted(state: dict[str, Any]) -> None:
    if state.get("status") == "needs-human" and state.get("stop_reason") == _BUDGET_EXHAUSTED_REASON:
        raise PmError(
            "attempt budget exhausted for the current slice; record the outcome with "
            "finalize --stop (or stop the run) — steering, sending, and acceptance are closed"
        )


# --- Path helpers ------------------------------------------------------------


def pm_dir(repo: Path) -> Path:
    return repo / ".pm"


def runs_root(repo: Path) -> Path:
    return pm_dir(repo) / "runs"


def run_artifact_dir(repo: Path, run_id: str) -> Path:
    return runs_root(repo) / run_id


def notes_path(repo: Path, run_id: str) -> Path:
    return run_artifact_dir(repo, run_id) / "notes.md"


def slice_number(slice_id: str) -> int:
    match = _SLICE_ID_RE.match(slice_id)
    if not match:
        raise PmError(f"slice id {slice_id!r} is not in the expected 'Slice <N>' shape")
    return int(match.group("number"))


def slice_artifact_dir(repo: Path, run_id: str, slice_id: str) -> Path:
    return run_artifact_dir(repo, run_id) / "slices" / f"slice-{slice_number(slice_id):03d}"


def write_pm_gitignore(repo: Path) -> None:
    """A self-ignoring `.pm/.gitignore` (a bare `*`) so the artifact tree never
    needs individual entries in the repository's own `.gitignore`."""
    directory = pm_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    gitignore.write_text("*\n", encoding="utf-8")


# --- Controller-owned originals + mirrors (target-design §8 item 3, §9) ------
#
# PM-authored artifacts (notes.md, run-report.md, assessment.md, review
# reports) have their AUTHORITATIVE ORIGINAL under the run's state dir
# (outside the worktree, alongside run.json) and are MIRRORED into `.pm/`
# for human reading. Nothing is ever read back from the mirror for control
# decisions — only these write helpers touch the mirror side.


def notes_original_path(run_dir: Path) -> Path:
    return run_dir / "notes.md"


def slice_state_dir(run_dir: Path, slice_id: str) -> Path:
    return run_dir / "slices" / f"slice-{slice_number(slice_id):03d}"


def mirror_artifact(repo: Path, run_dir: Path, run_id: str, relative_path: str) -> Path:
    """Copy an already-written ORIGINAL (under `run_dir`) to its `.pm/`
    mirror location, creating any missing mirror directories (this is what
    lets the report/mirror tree regenerate correctly after `.pm/` has been
    deleted entirely). Returns the original path."""
    original = run_dir / relative_path
    mirror = run_artifact_dir(repo, run_id) / relative_path
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_bytes(original.read_bytes())
    return original


def write_controller_artifact(repo: Path, run_dir: Path, run_id: str, relative_path: str, content: str) -> Path:
    """Write a PM-authored controller-owned artifact: the ORIGINAL under
    `run_dir`, then its `.pm/` mirror. Returns the original path."""
    original = run_dir / relative_path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text(content, encoding="utf-8")
    return mirror_artifact(repo, run_dir, run_id, relative_path)


def write_notes(repo: Path, run_dir: Path, run_id: str, *, text: str, mode: str) -> tuple[Path, str | None]:
    """Update the run notes safely: write the AUTHORITATIVE original under the
    run state dir, then re-mirror into `.pm/` (`write_controller_artifact`).

    This is the only sanctioned writer of `notes.md`. The `.pm/` mirror is
    regenerate-only, so a direct hand-edit to it is silently clobbered by the
    next `start-slice` re-mirror (PM Test 20 secondary finding); routing every
    notes update through here removes that footgun. `mode` is "append" (add
    `text` as a new trailing block, separated by a blank line) or "set"
    (replace the whole file). Returns the original path and an optional
    over-cap warning.
    """
    if not text.strip():
        raise PmError("notes text must be non-empty (nothing to append or set)")
    original = notes_original_path(run_dir)
    if mode == "append":
        existing = original.read_text(encoding="utf-8") if original.exists() else ""
        if existing.strip():
            if not existing.endswith("\n"):
                existing += "\n"
            content = f"{existing}\n{text.rstrip()}\n"
        else:
            content = f"{text.rstrip()}\n"
    elif mode == "set":
        content = f"{text.rstrip()}\n"
    else:
        raise PmError(f"unknown notes mode: {mode!r}")
    write_controller_artifact(repo, run_dir, run_id, "notes.md", content)
    size = original.stat().st_size
    warning: str | None = None
    if size > _NOTES_SIZE_CAP_BYTES:
        warning = (
            f"notes.md is {size} bytes, over the {_NOTES_SIZE_CAP_BYTES}-byte (512 KiB) cap; "
            "a runaway notes file silently degrades every later Developer prompt — curate it down"
        )
    return original, warning


def regenerate_report(repo: Path, run_dir: Path, state: dict[str, Any]) -> Path:
    """Regenerate `run-report.md` from controller-owned data alone and
    write its original + mirror. Needs no token: this writes a plain file,
    never `run.json`."""
    events = state_mod.read_events(run_dir)
    text = state_mod.render_run_report(state, events, run_dir)
    return write_controller_artifact(repo, run_dir, state["run_id"], "run-report.md", text)


# --- Shared state access ------------------------------------------------------


def repo_from_cwd(cwd: Path) -> Path:
    return git_ops.resolve_repo(cwd)


def load_writable_state(run_dir: Path, token: str) -> dict[str, Any]:
    """Load + MAC-verify state for a mutating command.

    An integrity failure is terminal by construction: the unauthenticated
    run.json is deliberately NOT rewritten or re-signed — re-signing would
    turn attacker-controlled bytes (say, a Developer marking its own slice
    accepted) into MAC-valid state. Left unsigned, every future mutating
    command keeps failing closed on the same IntegrityError, the tampered
    file survives as evidence, and recovery is the operator's decision
    (start a new run). Only the append-only event log records the
    detection, since events carry no authority.
    """
    try:
        return state_mod.load_state(run_dir, token)
    except IntegrityError as exc:
        try:
            state_mod.append_event(run_dir, "stop", note=f"state integrity check failed: {exc}")
        except PmError:
            pass
        raise


def slice_entry(state: dict[str, Any], slice_id: str) -> dict[str, Any] | None:
    for entry in state.get("slices", []):
        if isinstance(entry, dict) and entry.get("id") == slice_id:
            return entry
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id_session_prefix(run_id: str) -> str:
    return f"pm-{run_id}"


# --- Risk ratchet (target-design §4) ------------------------------------------


def apply_risk_ratchet(entry: dict[str, Any], current: dict[str, Any] | None, *, risk_flag: str | None) -> bool:
    """Raise a slice entry's (and, when given, the live current_slice's)
    `risk` to "elevated". `plan_risk` is never touched anywhere — it is the
    plan parser's immutable fact; the ratchet only ever raises the
    separate, mutable `risk` field, and a plan-elevated slice already
    satisfies "stays elevated" without any action here.

    Returns True iff `risk_flag` was supplied and valid, so the caller
    knows to log a `risk-raise` event. Raises `PmError` for any value other
    than "elevated" — the ratchet only ever raises, never lowers.
    """
    if risk_flag is None:
        return False
    if risk_flag != "elevated":
        raise PmError("risk can only be raised: pass --risk elevated (or omit --risk); it can never be lowered")
    entry["risk"] = "elevated"
    if current is not None:
        current["risk"] = "elevated"
    return True


# --- init ---------------------------------------------------------------------


@dataclass
class InitResult:
    run_id: str
    run_dir: Path
    token: str
    state: dict[str, Any]
    slices: list[plan_mod.PlanSlice]
    branch: str


def init_run(
    repo: Path,
    plan_path: Path,
    *,
    harness: str,
    model: str | None,
    effort: str | None,
    branch: str | None,
    create_branch: str | None,
    attest: str | None,
    max_attempts: int | None,
    reviewer_tools: str | None,
    reviewer_model: str | None,
    reviewer_effort: str | None,
    harness_command: str | None,
) -> InitResult:
    """Preflight, branch setup, and state/artifact creation for `init`.

    Callers are expected to have already run `plan.plan_check_report` and
    stopped on errors (this function assumes the plan is clean); it does
    not re-check the plan.
    """
    if not _tmux_present():
        raise PmError("tmux is required to run PM; install it before running init")

    if harness_command is None and harness not in profiles.SUPPORTED_HARNESSES:
        supported = ", ".join(profiles.SUPPORTED_HARNESSES)
        raise PmError(
            f"no PM harness profile is defined for {harness!r} and no --harness-command override was "
            f"given; supported harnesses: {supported}"
        )

    if harness_command:
        candidate_executable = shlex.split(harness_command)[0] if harness_command.strip() else ""
    else:
        candidate_executable = profiles.HARNESS_PROFILES[harness]["base_command"][0]
    if not candidate_executable or not _executable_exists(candidate_executable):
        raise PmError(f"harness executable not found on PATH: {candidate_executable!r}")

    resolved_branch = _resolve_init_branch(repo, branch=branch, create_branch=create_branch)
    # Required in every case, including after a branch switch and on the
    # "use the current branch" path, which has no earlier clean check.
    git_ops.require_clean_worktree(repo)

    slices = plan_mod.parse_plan(plan_path)
    known_ids = {plan_slice.slice_id for plan_slice in slices}
    attested_ids: set[str] = set()
    if attest:
        attested_ids = {piece.strip() for piece in attest.split(",") if piece.strip()}
        unknown = attested_ids - known_ids
        if unknown:
            raise PmError(f"--attest names unknown slice id(s): {', '.join(sorted(unknown))}")

    entries = [
        {
            "id": plan_slice.slice_id,
            "title": plan_slice.title,
            "status": "attested" if plan_slice.slice_id in attested_ids else None,
            "risk": plan_slice.plan_risk,
            "plan_risk": plan_slice.plan_risk,
            "commit": None,
            "attempts": 0,
        }
        for plan_slice in slices
    ]

    harness_block = {"name": harness, "model": model, "effort": effort, "command_override": harness_command}
    reviewer_block = {
        "tools": list(profiles.parse_reviewer_tools(reviewer_tools)),
        "model": reviewer_model,
        "effort": reviewer_effort,
    }
    policy_block = {"max_attempts": max_attempts if max_attempts is not None else 3, "commit_required": True}

    state, token, run_dir = state_mod.create_run(
        repo,
        plan_path=plan_path,
        plan_sha256=plan_mod.plan_digest(plan_path),
        slice_count=len(slices),
        branch=resolved_branch,
        harness=harness_block,
        reviewer=reviewer_block,
        policy=policy_block,
        slices=entries,
    )

    write_pm_gitignore(repo)
    (run_artifact_dir(repo, state["run_id"]) / "slices").mkdir(parents=True, exist_ok=True)
    state_mod.append_event(
        run_dir, "init", note=f"harness={harness} branch={resolved_branch} slices={len(slices)}"
    )

    return InitResult(run_id=state["run_id"], run_dir=run_dir, token=token, state=state, slices=slices, branch=resolved_branch)


def _tmux_present() -> bool:
    import shutil

    return shutil.which("tmux") is not None


def _executable_exists(executable: str) -> bool:
    import shutil

    return shutil.which(executable) is not None


def _resolve_init_branch(repo: Path, *, branch: str | None, create_branch: str | None) -> str:
    if create_branch:
        git_ops.require_clean_worktree(repo)
        git_ops.git(repo, "checkout", "-b", create_branch)
        return create_branch
    if branch:
        git_ops.require_clean_worktree(repo)
        returncode, _stdout, _stderr = git_ops.git_result(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
        if returncode != 0:
            raise PmError(f"branch {branch!r} does not exist; create it first or pass --create-branch")
        git_ops.git(repo, "checkout", branch)
        return branch
    current = git_ops.current_branch(repo)
    if current is None:
        raise PmError(
            "current HEAD is detached or the repository is unborn; pass --branch <existing> or "
            "--create-branch <new> so PM has a named branch to operate on"
        )
    if current in _PROTECTED_DEFAULT_BRANCHES:
        raise PmError(
            f"refusing to run PM on the default branch {current!r} by implicit default: every slice "
            f"commit would land directly on it. Pass --create-branch <new> for a dedicated run branch, "
            f"or --branch {current} to operate on it deliberately."
        )
    return current


# --- status ---------------------------------------------------------------


@dataclass
class StatusResult:
    state: dict[str, Any]
    slices: list[plan_mod.PlanSlice] | None
    plan_error: str | None
    next_slice_id: str | None
    next_slice_eligible: bool | None
    next_slice_reasons: list[str]
    current_session_alive: bool | None


def status(repo: Path, run_dir: Path, token: str | None = None) -> StatusResult:
    # Opportunistically MAC-verified when the controller's token is
    # available: the PM agent acts on status output between mutating
    # commands, so a tampered state should surface here, not one command
    # later. Tokenless (human) reads stay unverified, as documented.
    state = state_mod.load_state(run_dir, token)
    plan_error: str | None = None
    slices: list[plan_mod.PlanSlice] | None = None
    try:
        slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    except OSError as exc:
        plan_error = str(exc)

    next_slice_id = None
    next_eligible: bool | None = None
    next_reasons: list[str] = []
    if slices is not None:
        approved_ids = frozenset((state.get("approvals") or {}).keys())
        next_plan_slice = plan_mod.next_slice(slices, state)
        if next_plan_slice is not None:
            next_slice_id = next_plan_slice.slice_id
            next_eligible, next_reasons = plan_mod.eligibility(next_plan_slice, approved_ids)

    current = state.get("current_slice")
    current_alive: bool | None = None
    if current and current.get("tmux_session"):
        current_alive = sessions.session_exists(current["tmux_session"])

    return StatusResult(
        state=state,
        slices=slices,
        plan_error=plan_error,
        next_slice_id=next_slice_id,
        next_slice_eligible=next_eligible,
        next_slice_reasons=next_reasons,
        current_session_alive=current_alive,
    )


# --- approve ----------------------------------------------------------------


def approve(repo: Path, run_dir: Path, token: str, *, slice_id: str, reason: str) -> dict[str, Any]:
    state = load_writable_state(run_dir, token)
    slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    plan_slice = plan_mod.plan_slice_by_id(slices, slice_id)
    if plan_slice is None:
        raise PmError(f"{slice_id} was not found in the plan")
    if plan_slice.approval_needed is not True:
        raise PmError(
            f"{slice_id} is not approval-gated (its Risk Flags 'Approval needed before implementation:' "
            f"line is {plan_slice.approval_needed!r}, not an explicit 'yes'); an unclear or absent flag "
            "is a planning defect that approval cannot clear"
        )
    approvals = dict(state.get("approvals") or {})
    approvals[slice_id] = {"at": _utc_now_iso(), "reason": reason}
    state["approvals"] = approvals
    state_mod.save_state(run_dir, state, token)
    state_mod.append_event(run_dir, "approve", slice_id=slice_id, note=reason)
    return state


# --- start-slice --------------------------------------------------------------


@dataclass
class StartSliceOutcome:
    kind: str  # all_complete | blocked | plan_changed | attempts_exhausted | launched | relaunched
    slice_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    attempt: int | None = None
    session: str | None = None
    reaped: list[str] = field(default_factory=list)
    message: str = ""
    notes_warning: str | None = None


def _rotate_prior_attempt(artifact_dir: Path, superseded_attempt: int) -> None:
    names = ("result.json", "pane.txt", "pane-live.txt")
    present = [name for name in names if (artifact_dir / name).exists()]
    if not present:
        return
    destination = artifact_dir / f"attempt-{superseded_attempt}"
    destination.mkdir(parents=True, exist_ok=True)
    for name in present:
        (artifact_dir / name).rename(destination / name)


def start_slice(
    repo: Path,
    run_dir: Path,
    token: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    reviewer_tools: str | None = None,
    harness_command: str | None = None,
    risk: str | None = None,
) -> StartSliceOutcome:
    state = load_writable_state(run_dir, token)
    run_id = state["run_id"]
    plan_path = Path(state["plan"]["path"])

    try:
        plan_mod.verify_plan_unchanged(state, plan_path)
    except PmError as exc:
        state["status"] = "needs-human"
        state["stop_reason"] = "plan file changed mid-run"
        state_mod.save_state(run_dir, state, token)
        state_mod.append_event(run_dir, "plan-changed", note=str(exc))
        return StartSliceOutcome(kind="plan_changed", message=str(exc))

    slices = plan_mod.parse_plan(plan_path)

    current = state.get("current_slice")
    relaunch = False
    plan_slice: plan_mod.PlanSlice | None = None
    if current and current.get("id"):
        entry = slice_entry(state, current["id"])
        if entry is not None and entry.get("status") not in ("accepted", "attested"):
            plan_slice = plan_mod.plan_slice_by_id(slices, current["id"])
            relaunch = plan_slice is not None

    if not relaunch:
        plan_slice = plan_mod.next_slice(slices, state)
        if plan_slice is None:
            # Design §3.4 (Finish): the run ends honestly with a final state
            # write and report regeneration — an all-attested run reaches
            # completion here rather than idling as "active" forever.
            if state.get("status") != "complete":
                state["status"] = "complete"
                state["stop_reason"] = None
                state_mod.save_state(run_dir, state, token)
                state_mod.append_event(run_dir, "complete", note="all slices accepted or attested")
                regenerate_report(repo, run_dir, state)
            return StartSliceOutcome(kind="all_complete")

    assert plan_slice is not None
    approved_ids = frozenset((state.get("approvals") or {}).keys())
    eligible, reasons = plan_mod.eligibility(plan_slice, approved_ids)
    if not eligible:
        return StartSliceOutcome(kind="blocked", slice_id=plan_slice.slice_id, reasons=reasons)

    current_branch = git_ops.current_branch(repo)
    if current_branch != state.get("branch"):
        raise PmError(
            f"current branch {current_branch!r} does not match the run's recorded branch "
            f"{state.get('branch')!r}; switch back before starting a slice"
        )
    if not relaunch:
        git_ops.require_clean_worktree(repo)

    entry = slice_entry(state, plan_slice.slice_id)
    if entry is None:
        raise PmError(f"{plan_slice.slice_id} is not present in the run's slice entries")

    if risk is not None:
        # `current` here (if any) belongs to whichever slice was previously
        # in flight, not necessarily this one; new_current is always built
        # fresh from entry["risk"] below, so mutating only the entry is
        # sufficient — nothing reads a stale `current["risk"]` afterward.
        apply_risk_ratchet(entry, None, risk_flag=risk)
        state_mod.append_event(
            run_dir, "risk-raise", slice_id=plan_slice.slice_id, note="operator-raised via start-slice --risk elevated"
        )

    policy = state.get("policy") or {}
    max_attempts = int(policy.get("max_attempts", 3))

    if relaunch:
        attempts = int(entry.get("attempts", 0)) + 1
        if attempts > max_attempts:
            # Exhaustion is a mandatory stop (design §11): kill the live
            # session so nothing keeps working past the budget; the slice
            # stays current so finalize --stop can record the full story.
            session = current.get("tmux_session") if current else None
            if session:
                sessions.force_stop(session)
            state["status"] = "needs-human"
            state["stop_reason"] = _BUDGET_EXHAUSTED_REASON
            state_mod.save_state(run_dir, state, token)
            state_mod.append_event(
                run_dir, "stop", slice_id=plan_slice.slice_id, note=_BUDGET_EXHAUSTED_REASON
            )
            return StartSliceOutcome(kind="attempts_exhausted", slice_id=plan_slice.slice_id)
    else:
        attempts = 0

    artifact_dir = slice_artifact_dir(repo, run_id, plan_slice.slice_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    if relaunch:
        _rotate_prior_attempt(artifact_dir, attempts - 1)

    reaped: list[str] = []
    for name in sessions.sessions_with_prefix(_run_id_session_prefix(run_id)):
        sessions.force_stop(name)
        reaped.append(name)

    if relaunch:
        before_head = current.get("before_head") if current else None
    else:
        before_head = git_ops.git_head(repo)
        (artifact_dir / "status-before.txt").write_text(git_ops.git_status_text(repo), encoding="utf-8")

    # Controller-owned notes.md: the PM agent curates the ORIGINAL (under
    # run_dir) via the `notes` command (write_notes) — start-slice's job here
    # is only to create it if absent, mirror it into `.pm/` (PM_NOTES_PATH
    # points at the mirror, since the Developer reads .pm/, never the state
    # dir) on every launch, and tripwire-warn (non-fatal) when the original
    # has grown past the cap.
    original_notes = notes_original_path(run_dir)
    if not original_notes.exists():
        original_notes.parent.mkdir(parents=True, exist_ok=True)
        original_notes.write_text("", encoding="utf-8")
    notes_size = original_notes.stat().st_size
    notes_warning: str | None = None
    if notes_size > _NOTES_SIZE_CAP_BYTES:
        notes_warning = (
            f"notes.md is {notes_size} bytes, over the {_NOTES_SIZE_CAP_BYTES}-byte (512 KiB) cap; "
            "a runaway notes file silently degrades every later Developer prompt — curate it down"
        )
    slice_notes_path = notes_path(repo, run_id)
    slice_notes_path.parent.mkdir(parents=True, exist_ok=True)
    slice_notes_path.write_bytes(original_notes.read_bytes())

    result_path = artifact_dir / "result.json"
    prompt_text = prompts.render_developer_prompt(
        plan_slice,
        plan_path=plan_path,
        artifact_dir=artifact_dir,
        notes_path=slice_notes_path,
        result_path=result_path,
    )
    prompt_path = artifact_dir / "prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    tmp_dir = artifact_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "PM_SLICE_ARTIFACT_DIR": str(artifact_dir),
        "PM_PLAN_PATH": str(plan_path),
        "PM_SLICE_ID": plan_slice.slice_id,
        "PM_NOTES_PATH": str(slice_notes_path),
        "PM_RESULT_PATH": str(result_path),
        "TMPDIR": str(tmp_dir),
    }

    harness_block = state.get("harness") or {}
    harness_name = harness_block.get("name")
    effective_override = harness_command or harness_block.get("command_override")
    launch_model = model or harness_block.get("model")
    launch_effort = effort or harness_block.get("effort")

    expected_model_display: str | None = None
    if effective_override:
        command = effective_override
    else:
        git_access_dir = None
        session_id = None
        if harness_name == "codex" and bool(policy.get("commit_required", True)):
            git_access_dir = git_ops.worktree_git_dir(repo)
        if harness_name == "claude":
            session_id = str(uuid.uuid4())
        if harness_name == "opencode" and launch_model:
            identity = profiles.query_model_identity(harness_name, launch_model)
            if identity:
                expected_model_display = identity["display_name"]
        command = profiles.compose_command(
            harness_name,
            model=launch_model,
            effort=launch_effort,
            git_access_dir=git_access_dir,
            session_id=session_id,
        )

    session_name = sessions.session_name(run_id, slice_number(plan_slice.slice_id), attempts)
    sessions.start_session(session_name, repo, command, env)
    launch_executable = shlex.split(command)[0] if command.strip() else ""
    sessions.wait_until_ready(session_name, launch_executable, expected_model_display=expected_model_display)
    sessions.send_prompt(session_name, prompts.render_launch_pointer(prompt_path))

    now = _utc_now_iso()
    new_current: dict[str, Any] = {
        "id": plan_slice.slice_id,
        "artifact_dir": str(artifact_dir),
        "tmux_session": session_name,
        "before_head": before_head,
        "started_at": (current.get("started_at") if relaunch and current and current.get("started_at") else now),
        "attempts": attempts,
        "risk": entry.get("risk", plan_slice.plan_risk),
        "plan_risk": plan_slice.plan_risk,
        "wake_at": None,
        "reviewer_pids": [],
    }
    launch_overrides: dict[str, Any] = {key: value for key, value in (("model", model), ("effort", effort)) if value}
    if reviewer_tools:
        # Recorded per slice (design §8); review._resolve_tool prefers it
        # over the run-level reviewer configuration.
        launch_overrides["reviewer_tools"] = list(profiles.parse_reviewer_tools(reviewer_tools))
    if launch_overrides:
        new_current["launch"] = launch_overrides

    state["current_slice"] = new_current
    entry["attempts"] = attempts
    # A successful launch reactivates a run that a human resumed after a
    # stop/needs-human pause; tampered state can never reach here (the MAC
    # check above fails closed before any launch).
    state["status"] = "active"
    state_mod.save_state(run_dir, state, token)
    note = f"attempt {attempts}"
    if reaped:
        note += f"; reaped stale sessions: {', '.join(reaped)}"
    state_mod.append_event(
        run_dir,
        "relaunch" if relaunch else "launch",
        slice_id=plan_slice.slice_id,
        note=note,
        evidence=str(prompt_path),
    )

    return StartSliceOutcome(
        kind="relaunched" if relaunch else "launched",
        slice_id=plan_slice.slice_id,
        attempt=attempts,
        session=session_name,
        reaped=reaped,
        notes_warning=notes_warning,
    )


# --- observe ------------------------------------------------------------------


@dataclass
class ObserveOutcome:
    has_current_slice: bool
    running: bool = False
    pane_changed: bool = False
    result_present: bool = False
    result_status: str | None = None
    hard_stop: dict[str, Any] = field(default_factory=lambda: {"present": False, "kinds": [], "markers": []})
    tail: str = ""
    slice_id: str | None = None
    elapsed_seconds: float = 0.0


def observe(repo: Path, run_dir: Path, *, wait: float | None = None, token: str | None = None) -> ObserveOutcome:
    # Same opportunistic verification as status(): see the comment there.
    state = state_mod.load_state(run_dir, token)
    current = state.get("current_slice")
    if not current or not current.get("tmux_session"):
        return ObserveOutcome(has_current_slice=False)

    session = current["tmux_session"]
    artifact_dir = Path(current["artifact_dir"])
    pane_live_path = artifact_dir / "pane-live.txt"
    previous_capture = pane_live_path.read_text(encoding="utf-8") if pane_live_path.exists() else ""
    result_path = artifact_dir / "result.json"

    initial_running = sessions.session_exists(session)
    result_existed_before = result_path.is_file()

    deadline = time.monotonic() + wait if wait else None
    wait_start = time.monotonic()
    activity = sessions.detect_activity(session, previous_capture)
    # Wait exits early ONLY on a meaningful signal — session death, result.json
    # appearing, or a hard-stop marker in the fresh capture — never on a mere
    # pane byte-change, which `detect_activity`'s "active" flags on any TUI
    # spinner/stream churn and would otherwise defeat the wait almost
    # immediately (target-design §12, Amended post-implementation).
    while deadline is not None and time.monotonic() < deadline:
        if (
            not activity["running"]
            or result_path.is_file()
            or sessions.scan_hard_stop(activity["capture"])["present"]
        ):
            break
        time.sleep(_OBSERVE_POLL_SECONDS)
        activity = sessions.detect_activity(session, previous_capture)
    elapsed_seconds = time.monotonic() - wait_start

    capture = activity["capture"]
    pane_changed = capture != previous_capture
    if pane_changed:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        pane_live_path.write_text(capture, encoding="utf-8")

    result_present = result_path.is_file()
    result_status: str | None = None
    if result_present:
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                result_status = data.get("status")
        except (OSError, json.JSONDecodeError):
            result_status = None

    hard_stop = sessions.scan_hard_stop(capture)
    tail_lines = capture.splitlines()[-_OBSERVE_TAIL_LINES:]
    tail = "\n".join(tail_lines)

    liveness_changed = activity["running"] != initial_running
    result_newly_appeared = result_present and not result_existed_before
    if pane_changed or liveness_changed or result_newly_appeared:
        state_mod.append_event(
            run_dir,
            "observe",
            slice_id=current.get("id"),
            note=(
                f"pane_changed={pane_changed} liveness_changed={liveness_changed} "
                f"running={activity['running']} result_present={result_present} "
                f"elapsed={elapsed_seconds:.1f}s"
            ),
            evidence=str(pane_live_path) if pane_changed else None,
        )

    return ObserveOutcome(
        has_current_slice=True,
        running=activity["running"],
        pane_changed=pane_changed,
        result_present=result_present,
        result_status=result_status,
        hard_stop=hard_stop,
        tail=tail,
        slice_id=current.get("id"),
        elapsed_seconds=elapsed_seconds,
    )


# --- send ---------------------------------------------------------------------


def send(repo: Path, run_dir: Path, token: str, *, text: str, reason: str) -> None:
    state = load_writable_state(run_dir, token)
    _refuse_if_budget_exhausted(state)
    current = state.get("current_slice")
    session = current.get("tmux_session") if current else None
    if not current or not session or not sessions.session_exists(session):
        raise PmError("no live session — not driving a dead pane")
    sessions.send_line(session, text)
    state_mod.append_event(run_dir, "send", slice_id=current.get("id"), note=reason)


# --- finalize -------------------------------------------------------------
#
# Bare `finalize` (this section's first function) keeps the Stage 3
# floor-and-collect behaviour. The three decision paths below —
# `finalize_accept` / `finalize_steer` / `finalize_stop` — are where
# acceptance first exists in this toolkit (target-design §3.3/§5): the
# floor is mechanical and non-waivable, but accept/steer/stop are PM's own
# recorded acts, never inferred from evidence alone.


@dataclass
class FinalizeOutcome:
    report: FloorReport
    artifact_dir: Path
    pane_path: Path
    status_before_path: Path
    status_after_path: Path
    diff_path: Path
    result_path: Path
    slice_id: str


_ACCEPT_REASONING_MIN_CHARS = 40
_REQUIRED_ELEVATED_REVIEW_SKILLS = ("code-review", "drift-audit")


def _collect_finalize_evidence(repo: Path, state: dict[str, Any], current: dict[str, Any]) -> tuple[FloorReport, Path]:
    """Shared by bare `finalize` and every decision path: capture pane +
    status-after + diff evidence under the slice's artifact dir, then
    evaluate the eight-fact floor. Never mutates or saves state."""
    slice_id = current["id"]
    artifact_dir = Path(current["artifact_dir"])
    session = current.get("tmux_session")
    pane_text = sessions.pane_text(session) if session and sessions.session_exists(session) else ""

    (artifact_dir / "pane.txt").write_text(pane_text, encoding="utf-8")
    (artifact_dir / "status-after.txt").write_text(git_ops.git_status_text(repo), encoding="utf-8")

    diff_path = artifact_dir / "diff.patch"
    after_head = git_ops.git_head(repo)
    git_ops.write_git_diff(repo, current.get("before_head"), after_head, diff_path)

    slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    report = evaluate_floor(repo, state, slices, slice_id, artifact_dir=artifact_dir, pane_text=pane_text)
    return report, artifact_dir


def finalize(repo: Path, run_dir: Path, token: str, *, risk: str | None = None) -> FinalizeOutcome:
    state = load_writable_state(run_dir, token)
    current = state.get("current_slice")
    if not current:
        raise PmError("no current slice to finalize")
    slice_id = current["id"]

    if risk is not None:
        entry = slice_entry(state, slice_id)
        if entry is None:
            raise PmError(f"{slice_id} is not present in the run's slice entries")
        if apply_risk_ratchet(entry, current, risk_flag=risk):
            state_mod.append_event(
                run_dir, "risk-raise", slice_id=slice_id, note="risk raised via bare finalize --risk elevated"
            )

    report, artifact_dir = _collect_finalize_evidence(repo, state, current)

    note = "8/8 passed" if report.passed else "failed: " + ", ".join(
        fact.name for fact in report.facts if not fact.passed
    )
    state_mod.append_event(run_dir, "floor", slice_id=slice_id, note=note, evidence=str(artifact_dir))
    # updated_at bump (and, when --risk was given, the ratchet) only — no
    # other semantic field changes in bare finalize.
    state_mod.save_state(run_dir, state, token)

    return FinalizeOutcome(
        report=report,
        artifact_dir=artifact_dir,
        pane_path=artifact_dir / "pane.txt",
        status_before_path=artifact_dir / "status-before.txt",
        status_after_path=artifact_dir / "status-after.txt",
        diff_path=artifact_dir / "diff.patch",
        result_path=artifact_dir / "result.json",
        slice_id=slice_id,
    )


# --- finalize decision paths: assessment rendering helpers --------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_review_fresh(review: dict[str, Any], head: str | None) -> bool:
    """A review is fresh for `head` iff it was recorded against exactly
    this HEAD and its artifact still exists with a matching sha256 (design
    §5: any tree change after a mandatory review invalidates it)."""
    if not isinstance(review, dict) or head is None or review.get("head") != head:
        return False
    artifact = review.get("artifact")
    if not artifact or not Path(artifact).is_file():
        return False
    return review.get("sha256") == _sha256_file(Path(artifact))


def _fresh_reviews_for_head(reviews: list[dict[str, Any]], head: str | None) -> dict[str, dict[str, Any]]:
    fresh: dict[str, dict[str, Any]] = {}
    for review in reviews:
        if _is_review_fresh(review, head):
            skill = review.get("skill")
            if skill:
                fresh[skill] = review
    return fresh


def _reviews_consulted_text(reviews: list[dict[str, Any]], head: str | None, effective_risk: str) -> str:
    if not reviews:
        return "PM assessment only (standard risk)" if effective_risk != "elevated" else "(no reviews recorded)"
    lines: list[str] = []
    for review in reviews:
        stale = "" if _is_review_fresh(review, head) else " [SUPERSEDED - stale for current HEAD]"
        lines.append(
            f"- {review.get('skill')}/{review.get('tool')} @ {review.get('head')} -> "
            f"{review.get('artifact')}{stale}"
        )
    return "\n".join(lines)


def _attempts_summary(run_dir: Path, slice_id: str, attempts: int) -> str:
    events = state_mod.read_events(run_dir)
    steer_events = [event for event in events if event.get("kind") == "steer" and event.get("slice") == slice_id]
    lines = [f"Attempts: {attempts}"]
    if steer_events:
        lines.append(f"Steer interventions: {len(steer_events)}")
        for event in steer_events:
            lines.append(f"  - {event.get('ts')}:")
            note_lines = str(event.get("note") or "").splitlines() or [""]
            for note_line in note_lines:
                lines.append(f"      {note_line}")
    return "\n".join(lines)


def _render_assessment(
    entry: dict[str, Any],
    report: FloorReport,
    *,
    reasoning: str,
    decision: str,
    head: str | None,
    reviews_text: str,
    attempts_summary: str,
) -> str:
    lines = [
        f"# Assessment: {entry.get('id')} - {entry.get('title')}",
        "",
        f"Decision: {decision}",
        f"Timestamp: {_utc_now_iso()}",
        f"Commit: {head}",
        "",
        "## Floor",
        "",
    ]
    for fact in report.facts:
        status = "PASS" if fact.passed else "FAIL"
        lines.append(f"{fact.number}. {fact.name}: {status} - {fact.detail}")
    risk = entry.get("risk")
    plan_risk = entry.get("plan_risk")
    source = "plan-declared" if risk == plan_risk else "PM-raised (ratchet)"
    lines += [
        "",
        "## Risk",
        f"Level: {risk} (source: {source}; plan_risk={plan_risk})",
        "",
        "## Reviews consulted",
        reviews_text,
        "",
        "## Attempts / interventions",
        attempts_summary,
        "",
        "## PM reasoning",
        reasoning,
        "",
    ]
    return "\n".join(lines)


# --- finalize --accept ---------------------------------------------------


@dataclass
class AcceptOutcome:
    kind: str  # accepted | floor_failed | reviews_stale
    slice_id: str
    report: FloorReport | None = None
    assessment_path: Path | None = None
    message: str = ""


def finalize_accept(repo: Path, run_dir: Path, token: str, *, reasoning: str, risk: str | None = None) -> AcceptOutcome:
    """`finalize --accept "reasoning"` (target-design §3.3/§5/§8 item 3).

    The floor is re-run in full and is never waivable. On a passing floor,
    an elevated slice additionally requires both a drift-audit and a
    code-review entry recorded fresh against the current HEAD (design §5's
    review-freshness rule) before acceptance is recorded.
    """
    stripped_reasoning = reasoning.strip()
    if len(stripped_reasoning) < _ACCEPT_REASONING_MIN_CHARS:
        raise PmError(
            f"--accept reasoning must be at least {_ACCEPT_REASONING_MIN_CHARS} characters after "
            "stripping whitespace; the assessment is the accountability record, not a rubber stamp"
        )

    state = load_writable_state(run_dir, token)
    _refuse_if_budget_exhausted(state)
    current = state.get("current_slice")
    if not current:
        raise PmError("no current slice to finalize")
    slice_id = current["id"]
    entry = slice_entry(state, slice_id)
    if entry is None:
        raise PmError(f"{slice_id} is not present in the run's slice entries")

    if apply_risk_ratchet(entry, current, risk_flag=risk):
        state_mod.append_event(run_dir, "risk-raise", slice_id=slice_id, note=stripped_reasoning.splitlines()[0][:120])

    report, artifact_dir = _collect_finalize_evidence(repo, state, current)
    floor_note = "8/8 passed" if report.passed else "failed: " + ", ".join(
        fact.name for fact in report.facts if not fact.passed
    )
    state_mod.append_event(run_dir, "floor", slice_id=slice_id, note=floor_note, evidence=str(artifact_dir))

    if not report.passed:
        state_mod.save_state(run_dir, state, token)
        failed_names = ", ".join(fact.name for fact in report.facts if not fact.passed)
        return AcceptOutcome(
            kind="floor_failed", slice_id=slice_id, report=report,
            message=f"floor failed for {slice_id}: {failed_names}; nothing accepted",
        )

    effective_risk = entry.get("risk") or "standard"
    reviews = list(entry.get("reviews") or [])
    head = git_ops.git_head(repo)

    if effective_risk == "elevated":
        fresh = _fresh_reviews_for_head(reviews, head)
        missing = sorted(set(_REQUIRED_ELEVATED_REVIEW_SKILLS) - set(fresh.keys()))
        if missing:
            state_mod.save_state(run_dir, state, token)
            return AcceptOutcome(
                kind="reviews_stale", slice_id=slice_id, report=report,
                message=(
                    f"acceptance refused: missing or stale review(s) for {', '.join(missing)} "
                    f"against HEAD {head}; re-run review --skill <name> against the current HEAD"
                ),
            )

    reviews_text = _reviews_consulted_text(reviews, head, effective_risk)
    attempts_summary = _attempts_summary(run_dir, slice_id, current.get("attempts", entry.get("attempts", 0)))
    assessment_text = _render_assessment(
        entry, report, reasoning=stripped_reasoning, decision="ACCEPTED", head=head,
        reviews_text=reviews_text, attempts_summary=attempts_summary,
    )
    assessment_relative = f"slices/slice-{slice_number(slice_id):03d}/assessment.md"
    assessment_original = write_controller_artifact(repo, run_dir, state["run_id"], assessment_relative, assessment_text)

    first_line = stripped_reasoning.splitlines()[0][:120]
    entry["status"] = "accepted"
    entry["commit"] = head
    entry["decision"] = first_line
    entry["assessment"] = str(assessment_original)
    entry["summary"] = first_line

    session = current.get("tmux_session")
    state["current_slice"] = None
    if session:
        sessions.force_stop(session)

    # Accepting the final undecided slice finishes the run (design §3.4):
    # the state write below is the final one and the report regeneration is
    # the closing act.
    slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    run_complete = plan_mod.next_slice(slices, state) is None
    if run_complete:
        state["status"] = "complete"
        state["stop_reason"] = None

    state_mod.save_state(run_dir, state, token)
    state_mod.append_event(run_dir, "accept", slice_id=slice_id, note=first_line, evidence=str(assessment_original))
    if run_complete:
        state_mod.append_event(run_dir, "complete", note="all slices accepted or attested")
    regenerate_report(repo, run_dir, state)

    return AcceptOutcome(
        kind="accepted", slice_id=slice_id, report=report, assessment_path=assessment_original,
        message=f"{slice_id} accepted",
    )


# --- finalize --steer ------------------------------------------------------


@dataclass
class SteerOutcome:
    kind: str  # steered | budget_exhausted
    slice_id: str
    attempts: int | None = None
    message: str = ""


def finalize_steer(repo: Path, run_dir: Path, token: str, *, correction: str, risk: str | None = None) -> SteerOutcome:
    """`finalize --steer "correction"`: a corrective nudge into the LIVE
    session, counted against the same attempt budget as a relaunch."""
    state = load_writable_state(run_dir, token)
    _refuse_if_budget_exhausted(state)
    current = state.get("current_slice")
    if not current:
        raise PmError("no current slice to steer")
    slice_id = current["id"]
    session = current.get("tmux_session")
    if not session or not sessions.session_exists(session):
        raise PmError(f"no live session to steer for {slice_id} — relaunch with start-slice")
    entry = slice_entry(state, slice_id)
    if entry is None:
        raise PmError(f"{slice_id} is not present in the run's slice entries")

    # Stripped copy used only to decide "is this blank" and to summarize the
    # risk-raise event's own note; the correction delivered to the session
    # and recorded on the steer event below stays exactly as given — a
    # verbatim correction can legitimately start or end with meaningful
    # whitespace (e.g. an indented code block).
    stripped_correction = correction.strip()
    if apply_risk_ratchet(entry, current, risk_flag=risk):
        note = stripped_correction.splitlines()[0][:120] if stripped_correction else "risk raised via finalize --steer"
        state_mod.append_event(run_dir, "risk-raise", slice_id=slice_id, note=note)

    # Increment FIRST, then decide: a candidate attempt count over budget
    # is never persisted, matching start_slice's relaunch-exhaustion path.
    attempts = int(current.get("attempts", 0)) + 1
    policy = state.get("policy") or {}
    max_attempts = int(policy.get("max_attempts", 3))
    if attempts > max_attempts:
        # Mandatory stop, as in start_slice's exhaustion path: the live
        # session is killed, not left running past the budget.
        sessions.force_stop(session)
        state["status"] = "needs-human"
        state["stop_reason"] = _BUDGET_EXHAUSTED_REASON
        state_mod.save_state(run_dir, state, token)
        state_mod.append_event(run_dir, "stop", slice_id=slice_id, note=_BUDGET_EXHAUSTED_REASON)
        return SteerOutcome(
            kind="budget_exhausted", slice_id=slice_id, message="attempt budget exhausted; steer refused"
        )

    current["attempts"] = attempts
    entry["attempts"] = attempts

    # Rotate the prior attempt's completion signal into attempt-<n>/ exactly
    # as a relaunch does (start_slice), so a steered session can never be
    # mistaken for complete on the pre-steer result.json — observe --wait,
    # which breaks the instant result.json exists, would otherwise return
    # immediately on stale evidence (target-design §9; Stage 7 Test 21).
    # This must stay BEFORE send_correction: rotating after delivery would
    # race the live session, which may write its fresh post-steer result
    # before we rotate — archiving the NEW result instead of the stale one.
    # If send_correction below raises (dead-session race, hard-stop refusal)
    # the rotation is harmless: the result is preserved under attempt-<n>/,
    # the attempt increment is not persisted, and a later relaunch re-rotates
    # idempotently (nothing left at top level → no-op).
    _rotate_prior_attempt(Path(current["artifact_dir"]), attempts - 1)

    # Direct live-session injection, not a persistent numbered artifact: the
    # correction is rendered from the reference-sourced wrapper and pasted
    # straight into the pane, verbatim.
    message = prompts.render_steer_message(correction)
    sessions.send_correction(session, message)

    state_mod.save_state(run_dir, state, token)
    # The complete, verbatim correction lives in the event's note (no
    # truncation, no stripping, no evidence path) — it is the only durable
    # record of what was said, now that no steer file exists to point to.
    state_mod.append_event(run_dir, "steer", slice_id=slice_id, note=correction)

    return SteerOutcome(
        kind="steered", slice_id=slice_id, attempts=attempts,
        message=f"steered {slice_id} (attempt {attempts})",
    )


# --- finalize --stop --------------------------------------------------------


@dataclass
class StopDecisionOutcome:
    slice_id: str
    assessment_path: Path
    report: FloorReport


def finalize_stop(repo: Path, run_dir: Path, token: str, *, reason: str, risk: str | None = None) -> StopDecisionOutcome:
    """`finalize --stop "reason"`: records exactly what happened, floor
    passing or not — that is the point of a stop record."""
    state = load_writable_state(run_dir, token)
    current = state.get("current_slice")
    if not current:
        raise PmError("no current slice to stop")
    slice_id = current["id"]
    entry = slice_entry(state, slice_id)
    if entry is None:
        raise PmError(f"{slice_id} is not present in the run's slice entries")

    stripped_reason = reason.strip()
    if apply_risk_ratchet(entry, current, risk_flag=risk):
        note = stripped_reason.splitlines()[0][:120] if stripped_reason else "risk raised via finalize --stop"
        state_mod.append_event(run_dir, "risk-raise", slice_id=slice_id, note=note)

    report, artifact_dir = _collect_finalize_evidence(repo, state, current)
    floor_note = "8/8 passed" if report.passed else "failed: " + ", ".join(
        fact.name for fact in report.facts if not fact.passed
    )
    state_mod.append_event(run_dir, "floor", slice_id=slice_id, note=floor_note, evidence=str(artifact_dir))

    head = git_ops.git_head(repo)
    reviews = list(entry.get("reviews") or [])
    reviews_text = _reviews_consulted_text(reviews, head, entry.get("risk") or "standard")
    attempts_summary = _attempts_summary(run_dir, slice_id, current.get("attempts", entry.get("attempts", 0)))
    assessment_text = _render_assessment(
        entry, report, reasoning=stripped_reason, decision="STOPPED", head=head,
        reviews_text=reviews_text, attempts_summary=attempts_summary,
    )
    assessment_relative = f"slices/slice-{slice_number(slice_id):03d}/assessment.md"
    assessment_original = write_controller_artifact(repo, run_dir, state["run_id"], assessment_relative, assessment_text)

    first_line = stripped_reason.splitlines()[0][:120] if stripped_reason else ""
    entry["status"] = "stopped"
    entry["decision"] = first_line
    entry["assessment"] = str(assessment_original)
    entry["summary"] = first_line

    session = current.get("tmux_session")
    if session:
        sessions.force_stop(session)
    for pgid in list(current.get("reviewer_pids") or []):
        _kill_reviewer_pgid(pgid)

    state["status"] = "needs-human"
    state["stop_reason"] = reason
    state["current_slice"] = None

    state_mod.save_state(run_dir, state, token)
    state_mod.append_event(run_dir, "slice-stop", slice_id=slice_id, note=first_line, evidence=str(assessment_original))
    regenerate_report(repo, run_dir, state)

    return StopDecisionOutcome(slice_id=slice_id, assessment_path=assessment_original, report=report)


# --- stop -----------------------------------------------------------------


def _kill_reviewer_pgid(pgid: int) -> None:
    """killpg, tolerating a process group that is already gone (ESRCH) or
    unreachable (EPERM) — a hung reviewer that stop reaps."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


@dataclass
class StopOutcome:
    run_id: str
    killed: list[str]


def stop(
    repo: Path,
    run_dir: Path,
    token: str,
    *,
    reason: str,
    slice_status: str | None = None,
) -> StopOutcome:
    state = load_writable_state(run_dir, token)
    run_id = state["run_id"]
    current = state.get("current_slice")

    if current and current.get("artifact_dir"):
        artifact_dir = Path(current["artifact_dir"])
        session = current.get("tmux_session")
        pane_text = sessions.pane_text(session) if session else ""
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "pane.txt").write_text(pane_text, encoding="utf-8")

    # Reap any recorded reviewer process groups (a hung `review` subprocess)
    # — ESRCH/EPERM tolerated. Applies whenever state is readable, including
    # the --scavenge-with-readable-state path (cli.py already routes that
    # through this same function).
    if current and current.get("reviewer_pids"):
        for pgid in list(current["reviewer_pids"]):
            _kill_reviewer_pgid(pgid)
        current["reviewer_pids"] = []

    killed: list[str] = []
    for name in sessions.sessions_with_prefix(_run_id_session_prefix(run_id)):
        sessions.force_stop(name)
        killed.append(name)

    if state.get("status") != "complete":
        state["status"] = "stopped"
    state["stop_reason"] = reason
    if slice_status and current and current.get("id"):
        entry = slice_entry(state, current["id"])
        if entry is not None:
            entry["status"] = slice_status

    state_mod.save_state(run_dir, state, token)
    state_mod.append_event(
        run_dir,
        "stop",
        slice_id=current.get("id") if current else None,
        note=reason,
        evidence=", ".join(killed) if killed else None,
    )
    regenerate_report(repo, run_dir, state)
    return StopOutcome(run_id=run_id, killed=killed)


def stop_scavenge_sweep(*, run_id: str | None) -> list[str]:
    """State-independent tmux sweep for `stop --scavenge` when state cannot be
    trusted or read at all. Narrows to the given run id's sessions when one
    is known; otherwise sweeps every PM session regardless of run."""
    prefix = _run_id_session_prefix(run_id) if run_id else "pm-"
    killed: list[str] = []
    for name in sessions.sessions_with_prefix(prefix):
        sessions.force_stop(name)
        killed.append(name)
    return killed

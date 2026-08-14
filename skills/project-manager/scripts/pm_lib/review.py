"""The `review` command: commissioning an independent review of a pinned diff.

This module shares the five-tool roster with the Developer launch path but
not its code: reviews run one-shot/exec where the tool supports it, never the
Developer's interactive tmux TUI path, so the command table below is its own.
It shares no code with `skills/orchestrator/` and never imports from it.

The Reviewer is read-only by instruction and holds no acceptance authority;
PM reads the report and records the decision itself, in
`finalize --accept/--steer/--stop`. Review input is pinned, not live: the
diff and changed-files list are generated from `before_head..HEAD` at the
moment `review` runs, so a still-running or restarted Developer session
cannot race the reviewer.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import PmError
from . import git_ops
from . import plan as plan_mod
from . import profiles
from . import prompts
from . import slice_ops
from . import state as state_mod

_STDERR_TAIL_CHARS = 4000


# --- one-shot reviewer command composition (this module's own table) ---------


def compose_reviewer_command(
    tool: str, prompt: str, *, model: str | None = None, effort: str | None = None, repo: Path
) -> list[str]:
    """Compose a one-shot reviewer invocation for `tool`. Unsupported tool
    names, and a non-default `effort` for qwen (whose tested one-shot command
    has no effort/reasoning flag), fail closed with `PmError` rather than
    silently dropping the request. opencode takes effort as `--variant`; note
    this is the one-shot `opencode run` only, so it does NOT apply to the
    Developer seat's interactive TUI (see profiles.py)."""
    repo_str = str(repo)

    if tool == "codex":
        command = ["codex", "exec", prompt]
        if model:
            command.extend(["-m", model])
        if effort:
            command.extend(["-c", f'model_reasoning_effort="{effort}"'])
        # The read-only sandbox is only a boundary while approvals cannot escalate
        # out of it, so the policy is pinned rather than inherited from the caller.
        # The sandbox is set through its config key to match the orchestrator's
        # codex profile, which must use the config form because `codex exec resume`
        # rejects `--sandbox`. Full rationale and the verifying evidence:
        # skills/orchestrator/references/codex.md.
        command.extend(
            [
                "-c",
                'sandbox_mode="read-only"',
                "-c",
                'approval_policy="never"',
                "--skip-git-repo-check",
                "-C",
                repo_str,
            ]
        )
        return command

    if tool == "claude":
        command = ["claude", "-p", prompt]
        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--effort", effort])
        command.extend(["--permission-mode", "plan", "--output-format", "text", "--add-dir", repo_str])
        return command

    if tool == "copilot":
        command = ["copilot"]
        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--effort", effort])
        command.extend(["-p", prompt, "--allow-all", "--autopilot", "--silent", "--add-dir", repo_str])
        return command

    if tool == "opencode":
        command = ["opencode", "run", prompt]
        if model:
            command.extend(["-m", model])
        # opencode's reasoning-effort control; the launch path verifies it.
        if effort:
            command.extend(["--variant", effort])
        command.extend(["--agent", "plan", "--auto", "--dir", repo_str])
        return command

    if tool == "qwen":
        if effort:
            raise PmError(
                "qwen's tested one-shot review command has no effort/reasoning flag; "
                "omit --effort for this tool or choose the configured model explicitly"
            )
        command = ["qwen", "--prompt", prompt]
        if model:
            command.extend(["--model", model])
        # --yolo for the same reason the Developer seat carries it: without it
        # qwen gates every tool call on a confirmation a one-shot run cannot
        # answer, and the reviewer hangs until --timeout.
        command.extend(["--yolo", "--sandbox", "--output-format", "text"])
        return command

    raise PmError(
        f"no reviewer command profile is defined for {tool!r}; supported tools: "
        "codex, claude, copilot, opencode, qwen"
    )


def _build_reviewer_command(
    tool: str, prompt: str, *, model: str | None, effort: str | None, repo: Path, reviewer_command_override: str | None
) -> list[str]:
    if reviewer_command_override:
        return shlex.split(reviewer_command_override) + [prompt]
    return compose_reviewer_command(tool, prompt, model=model, effort=effort, repo=repo)


def _resolve_tool(state: dict[str, Any], tool_arg: str | None, *, has_override: bool) -> str:
    if tool_arg:
        return tool_arg
    # A per-slice override recorded by `start-slice --reviewer-tools` wins
    # over the run-level reviewer configuration.
    current = state.get("current_slice") or {}
    launch = current.get("launch") or {}
    slice_tools = launch.get("reviewer_tools") or []
    if slice_tools:
        return slice_tools[0]
    tools = (state.get("reviewer") or {}).get("tools") or []
    if tools:
        return tools[0]
    if has_override:
        return "custom"
    raise PmError(
        "no reviewer tool configured: pass --tool, configure reviewer.tools at init, or use --reviewer-command"
    )


def _fresh_drift_audit_report(
    repo: Path, run_dir: Path, run_id: str, entry: dict[str, Any] | None, head: str, grant_count: int
) -> str | None:
    """A reviewer-readable path to a drift-audit report already recorded fresh
    against `head`, or None.

    Freshness reuses `slice_ops.is_review_fresh` — the same rule that decides
    whether an elevated slice's mandatory reviews still hold — so a report
    superseded by a later tree change, or by a grant recorded after it was
    commissioned, is never presented as current. The reviewer is pointed at the `.pm/`
    mirror, which sits inside the repository it is scoped to read, but only
    after re-copying the hash-verified original over it: freshness
    authenticates the controller original, so handing over a mirror that was
    not just written from it would let anything with write access to the
    gitignored mirror substitute a different report.
    """
    for review in reversed((entry or {}).get("reviews") or []):
        if review.get("skill") != "drift-audit" or not slice_ops.is_review_fresh(review, head, grant_count):
            continue
        original = Path(str(review.get("artifact")))
        try:
            relative = original.relative_to(run_dir)
        except ValueError:
            return str(original)
        slice_ops.mirror_artifact(repo, run_dir, run_id, str(relative))
        return str(slice_ops.run_artifact_dir(repo, run_id) / relative)
    return None


def _tail(path: Path, max_chars: int = _STDERR_TAIL_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _next_commission_seq(slice_dir: Path, recorded_reviews: int) -> int:
    """Next collision-free artifact sequence for a commissioned review.

    `recorded_reviews` alone undercounts: a reviewer that times out or exits
    non-zero never gets a review record, so a retry would reuse its number and
    overwrite the artifacts proving what that attempt was commissioned with.
    Any `review-<n>-*` file already present therefore raises the floor, whether
    or not the attempt it belongs to succeeded.
    """
    highest = recorded_reviews
    try:
        existing = list(slice_dir.iterdir())
    except FileNotFoundError:
        # No artifacts yet: the first commission for this slice.
        existing = []
    except OSError as exc:
        # Fail CLOSED. Treating an unreadable directory as empty would restore
        # the very collision this function exists to prevent: a failed
        # commission records no review, so the fallback would reuse its
        # sequence and overwrite the prompt proving what it was told — while a
        # known pathname stays writable even when listing is denied.
        raise PmError(
            f"cannot enumerate existing review artifacts in {slice_dir} ({exc}); "
            "refusing to commission a review that could overwrite a previous "
            "attempt's evidence"
        ) from exc
    for path in existing:
        parts = path.name.split("-", 2)
        if len(parts) < 3 or parts[0] != "review":
            continue
        try:
            highest = max(highest, int(parts[1]))
        except ValueError:
            continue
    return highest + 1


def _claim_commission_seq(slice_dir: Path, recorded_reviews: int, skill: str, tool: str) -> tuple[int, Path]:
    """Atomically claim the next sequence via its stderr artifact.

    Scanning then writing raced: concurrent commissions picked the same
    sequence and the loser overwrote the winner's evidence.
    """
    seq = _next_commission_seq(slice_dir, recorded_reviews)
    while True:
        stderr_path = slice_dir / f"review-{seq}-{skill}-{tool}-stderr.txt"
        try:
            os.close(os.open(stderr_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666))
            return seq, stderr_path
        except FileExistsError:
            seq += 1


# --- the review command --------------------------------------------------


@dataclass
class ReviewOutcome:
    slice_id: str
    skill: str
    tool: str
    model: str | None
    head: str
    before_head: str | None
    diff_path: Path
    changed_files: list[str] = field(default_factory=list)
    artifact_path: Path | None = None
    sha256: str = ""


def run_review(
    repo: Path,
    run_dir: Path,
    token: str,
    *,
    slice_id: str,
    skill: str,
    tool: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    reviewer_command: str | None = None,
    timeout: float | None = None,
    pm_adjudications: str | None = None,
) -> ReviewOutcome:
    """Commission one independent review against the slice's pinned range.

    Reviews pin to the LIVE range: `slice_id` must be the run's current
    in-flight slice, and HEAD must have advanced past `before_head` — an
    already-current or unreviewable range refuses outright (PmError) rather
    than silently reviewing nothing.

    `timeout` bounds the wait on the reviewer subprocess; on expiry the
    process group is killed and the call fails closed (PmError). This
    function itself requires it to be passed (None means an unbounded wait);
    the CLI never omits it, applying a one-hour default so an unattended run
    always regains control from a stuck reviewer. A legitimately slow cold
    local model is handled with a larger explicit `--timeout`.

    `pm_adjudications` is PM-authored free text naming rulings already settled
    in this run, so a reviewer stops re-raising them. It is the only
    PM-authored prose the reviewer ever sees, which is precisely why the
    rendered prompt is persisted beside the report: a channel that can narrow
    what a reviewer reports must leave the human an audit trail of what it
    was told.
    """
    state = slice_ops.load_writable_state(run_dir, token)
    current = state.get("current_slice")
    if not current or current.get("id") != slice_id:
        raise PmError(
            f"{slice_id} is not the current in-flight slice; reviews pin to the live range "
            f"(current: {current.get('id') if current else None})"
        )

    before_head = current.get("before_head")
    reviewed_head = git_ops.git_head(repo)
    if not reviewed_head:
        raise PmError("HEAD could not be resolved; nothing to review")
    if reviewed_head == before_head:
        raise PmError(f"HEAD has not advanced past before_head ({before_head}); nothing to review")

    # The reviewer reads the live checkout, so the tree must actually BE the
    # pinned committed state when the review starts. A dirty tree means the
    # reviewer would judge uncommitted work the range does not cover — refuse
    # rather than silently review the wrong tree.
    dirty = git_ops.meaningful_status_lines(git_ops.git_status_text(repo))
    if dirty:
        raise PmError(
            "worktree is dirty outside .pm/ — a review must run against the pinned committed tree; "
            "quiesce the Developer session and commit or restore before commissioning: " + "; ".join(dirty)
        )

    run_id = state["run_id"]
    plan_slices = plan_mod.parse_plan(Path(state["plan"]["path"]))
    plan_slice = plan_mod.plan_slice_by_id(plan_slices, slice_id)
    if plan_slice is None:
        raise PmError(f"{slice_id} was not found in the parsed plan")

    original_slice_dir = slice_ops.slice_state_dir(run_dir, slice_id)
    original_slice_dir.mkdir(parents=True, exist_ok=True)

    has_override = bool(reviewer_command)
    resolved_tool = _resolve_tool(state, tool, has_override=has_override)

    entry = slice_ops.slice_entry(state, slice_id)
    if entry is None:
        raise PmError(f"{slice_id} is not present in the run's slice entries")
    # Failed commissions also reserve their artifact sequence: they record no
    # review, so numbering from `len(reviews)` alone would overwrite them.
    seq, stderr_path = _claim_commission_seq(
        original_slice_dir, len(entry.get("reviews") or []), skill, resolved_tool
    )

    # Per-commission, so a later commission cannot replace the pinned diff a
    # running reviewer has not opened yet.
    diff_path = original_slice_dir / f"review-{seq}-{skill}-{resolved_tool}.patch"
    git_ops.write_git_diff(repo, before_head, reviewed_head, diff_path)
    if before_head and before_head != reviewed_head:
        changed_files = sorted(
            path for path in git_ops.git(repo, "diff", "--name-only", before_head, reviewed_head).splitlines() if path
        )
    else:
        changed_files = sorted(
            path for path in git_ops.git(repo, "show", "--name-only", "--format=", reviewed_head).splitlines() if path
        )

    sections = plan_slice.sections
    reviewer_block = state.get("reviewer") or {}
    resolved_model = model or reviewer_block.get("model")
    resolved_effort = effort or reviewer_block.get("effort")

    # Read once, so the grants rendered into the prompt and the count recorded
    # with the finished review are provably the same set. That count is what
    # `slice_ops.is_review_fresh` compares against the slice's current grants:
    # a review is only fresh while its own prompt showed every grant that
    # exists, which is a property of COMMISSION time, not of when the reviewer
    # happened to finish.
    commissioned_grants = plan_mod.slice_grants(state, slice_id)

    prompt_text = prompts.render_reviewer_prompt(
        # Never handed to a drift audit itself: a second audit of the same
        # commit must stay independent of the first one's verdict.
        drift_audit_report=None if skill == "drift-audit" else _fresh_drift_audit_report(
            repo, run_dir, run_id, slice_ops.slice_entry(state, slice_id), reviewed_head,
            len(commissioned_grants),
        ),
        pm_adjudications=pm_adjudications,
        pm_surface_grants=commissioned_grants,
        skill_name=skill,
        repo=str(repo),
        slice_id=slice_id,
        slice_title=plan_slice.title,
        before_head=before_head,
        reviewed_head=reviewed_head,
        diff_path=str(diff_path),
        changed_files=changed_files,
        intended_change=sections.get("Intended Change", "").rstrip(),
        acceptance_criteria=sections.get("Acceptance Criteria", "").rstrip(),
        authorized_surface=sections.get("Authorized Surface", "").rstrip(),
        explicit_non_goals=sections.get("Explicit Non-Goals", "").rstrip(),
        risk_flags=sections.get("Risk Flags", "").rstrip(),
    )

    # Checked here, not in compose_reviewer_command, which stays pure and
    # I/O-free. opencode is the only tool whose effort flag needs verifying
    # against the model; the others take effort verbatim or refuse it.
    if resolved_tool == "opencode" and resolved_effort and not reviewer_command:
        profiles.assert_opencode_variant_supported(resolved_model, resolved_effort)

    command = _build_reviewer_command(
        resolved_tool, prompt_text, model=resolved_model, effort=resolved_effort, repo=repo,
        reviewer_command_override=reviewer_command,
    )

    report_relative = f"{slice_ops.slice_relative_dir(slice_id)}/review-{seq}-{skill}-{resolved_tool}.md"
    report_original = run_dir / report_relative
    report_original.parent.mkdir(parents=True, exist_ok=True)

    # The rendered prompt is the only record of what this reviewer was told,
    # including any PM adjudications that narrowed where it spent attention.
    # Written BEFORE the reviewer runs so a timeout, crash, or non-zero exit
    # still leaves the commission auditable rather than only its silence.
    # Derived from the report's own path so the two can never diverge.
    prompt_relative = report_relative[: -len(".md")] + "-prompt.md"
    slice_ops.write_controller_artifact(repo, run_dir, run_id, prompt_relative, prompt_text)

    # The reviewer process must not inherit the PM run capability token from
    # the controller's exported environment: the token is the controller's
    # alone, and SKILL.md forbids it in any Reviewer session.
    reviewer_env = {key: value for key, value in os.environ.items() if key != "PM_RUN_TOKEN"}

    with open(report_original, "wb") as stdout_handle, open(stderr_path, "wb") as stderr_handle:
        process = subprocess.Popen(
            # stdin=DEVNULL: `codex exec` blocks on inherited stdin ("Reading
            # additional input from stdin...") and the hang looks like a slow model.
            command, cwd=str(repo), stdout=stdout_handle, stderr=stderr_handle,
            stdin=subprocess.DEVNULL, start_new_session=True, env=reviewer_env,
        )
        # start_new_session=True makes the child its own process-group
        # leader, so its pgid equals its pid at creation time — no
        # getpgid() race against a fast-exiting process.
        pgid = process.pid
        # Persist before waiting so `stop` can reap every parallel reviewer.
        with state_mod.locked_update(run_dir, token) as live_state:
            live_current = live_state.get("current_slice")
            if live_current is not None:
                live_current["reviewer_pids"] = [*(live_current.get("reviewer_pids") or []), pgid]
        # Printed before the (possibly long) wait begins — a slow-but-alive
        # local reviewer and a hung one are otherwise indistinguishable from
        # the PM seat.
        print(
            f"reviewer launched: pgid={pgid} report={report_original} stderr={stderr_path}",
            flush=True,
        )
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)  # grace for a clean exit; return value unused
            except subprocess.TimeoutExpired:
                pass
            # Unconditional: a descendant that ignores SIGTERM would survive
            # if SIGKILL only ran when the leader-wait above also timed out
            # (the leader can exit on SIGTERM while a child lingers).
            # ProcessLookupError means the group is already empty — success.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = process.wait()

    # Reload and clear the recorded pgid FIRST, on every exit path: the
    # process has exited, so a failed review must not leave a stale
    # process-group id behind for a later `stop` to SIGKILL after PID reuse.
    # (Re-reading also avoids relying on the in-memory state staying
    # accurate across the potentially long subprocess wait above.)
    # Persisted BEFORE any fallible post-processing (mirror/hash below): if
    # either raises, the cleared pgid must already be on disk so a later
    # `stop` cannot SIGKILL a reused pgid recorded as still-live.
    with state_mod.locked_update(run_dir, token) as state:
        current = state.get("current_slice")
        if current is not None and current.get("reviewer_pids"):
            current["reviewer_pids"] = [pid for pid in current["reviewer_pids"] if pid != pgid]

    if timed_out:
        state_mod.append_event(
            run_dir,
            "review",
            slice_id=slice_id,
            note=f"{skill} via {resolved_tool} timed out after {timeout:g}s; reviewer process group killed",
        )
        raise PmError(
            f"reviewer timed out after {timeout:g}s and was killed (process group {pgid}); "
            "this is not proof of a hang — a slow cold local model may just need a longer --timeout. "
            f"Its stderr is at {stderr_path}; re-commission with a larger --timeout to retry"
        )

    if returncode != 0:
        stderr_tail = _tail(stderr_path)
        raise PmError(f"reviewer command failed (exit {returncode}): {stderr_tail}")

    report_original = slice_ops.mirror_artifact(repo, run_dir, run_id, report_relative)
    sha256 = slice_ops.sha256_file(report_original)

    # Locked: preserve reviews completed concurrently.
    with state_mod.locked_update(run_dir, token) as state:
        entry = slice_ops.slice_entry(state, slice_id)
        if entry is not None:
            entry["reviews"] = [
                *(entry.get("reviews") or []),
                {
                    "skill": skill,
                    "tool": resolved_tool,
                    "model": resolved_model,
                    "head": reviewed_head,
                    "before_head": before_head,
                    "artifact": str(report_original),
                    "sha256": sha256,
                    "at": state_mod.utc_now_iso(),
                    # How many surface grants this reviewer's prompt showed.
                    # `is_review_fresh` stales the review as soon as the slice
                    # carries a different number, so a grant landing mid-review
                    # cannot be papered over by a later completion stamp.
                    "grants_seen": len(commissioned_grants),
                },
            ]
    state_mod.append_event(
        run_dir, "review", slice_id=slice_id, note=f"{skill} via {resolved_tool}", evidence=str(report_original)
    )

    return ReviewOutcome(
        slice_id=slice_id,
        skill=skill,
        tool=resolved_tool,
        model=resolved_model,
        head=reviewed_head,
        before_head=before_head,
        diff_path=diff_path,
        changed_files=changed_files,
        artifact_path=report_original,
        sha256=sha256,
    )

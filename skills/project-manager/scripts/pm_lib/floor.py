"""The mechanical floor: eight non-waivable facts.

One function surface, no decisions. Every fact here computes a true/false
condition from git, the filesystem, run state, and a pane-text string handed
in by the caller — never a model call, never prose semantics, never a tmux
shell-out (that boundary belongs to `sessions.py`; this module only imports
its pure `scan_hard_stop` text parser for fact 8). A fact that cannot be
established (a missing file, a git command that fails) is `passed=False`
with the reason in `detail` — this module never raises on ordinary
git/filesystem absence or failure, and it never writes state, contacts a
session, or renders an accept/reject verdict. That judgement belongs to the
PM agent, above this floor, in a later stage.

The eight facts are:

1. plan-digest: the plan file's current sha256 matches the run's frozen
   `plan.sha256`. A missing or unreadable plan file fails the fact.
2. identity-branch: `resolve_repo(repo)` matches the run's recorded repo,
   and the current branch matches the run's recorded branch. Detached or
   unborn HEAD fails (current_branch returns None).
3. approval: the slice's plan-parsed `approval_needed` is `False` (passes
   unconditionally), or `True` with a recorded human approval for the slice
   id in `state.approvals` (passes), or `True` without one (fails). An
   unclear flag (`None`) always fails, even with a recorded approval —
   an unclear risk flag is a planning defect, not an approval question.
4. result: `<artifact_dir>/result.json` exists, parses as JSON, is a JSON
   object, and its `"slice"` field equals the slice id being evaluated.
5. surface: changed files (`before_head`..HEAD, unioned with the dirty
   working tree — `git_ops.changed_files_between`) are a subset of the
   slice's authorized surface. `before_head` comes from
   `state.current_slice.before_head`.
6. commit-ancestry: when `state.policy.commit_required` is true — a HEAD
   commit exists, it differs from `before_head`, it descends from
   `before_head`, and it equals the tip of the run's recorded branch
   (`refs/heads/<state.branch>`). A commit that lands on a different branch
   which still descends from `before_head` fails this fact even though
   fact 2's branch check may also fail independently. When
   `commit_required` is false, the fact passes unconditionally with a
   detail noting no commit is required.
7. clean-worktree: no meaningful `git status` lines outside `.pm/`
   (`git_ops.meaningful_status_lines` already excludes it).
8. hard-stop-scan: `sessions.scan_hard_stop(pane_text)` reports no marker
   present. Empty pane text passes (nothing visible to flag).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PmError
from . import git_ops
from .plan import PlanSlice
from .plan import plan_digest as compute_plan_digest
from .plan import plan_slice_by_id
from .sessions import scan_hard_stop


@dataclass(frozen=True)
class FloorFact:
    number: int
    name: str
    passed: bool
    detail: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class FloorReport:
    facts: tuple[FloorFact, ...]

    @property
    def passed(self) -> bool:
        return all(fact.passed for fact in self.facts)


def _fact_plan_digest(state: dict[str, Any]) -> FloorFact:
    plan_info = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    recorded = plan_info.get("sha256")
    path_str = plan_info.get("path")
    if not path_str:
        return FloorFact(1, "plan-digest", False, "run state has no recorded plan path", {"recorded_sha256": recorded})
    path = Path(path_str)
    try:
        current = compute_plan_digest(path)
    except OSError as exc:
        return FloorFact(
            1, "plan-digest", False, f"plan file could not be read: {exc}", {"path": str(path), "recorded_sha256": recorded}
        )
    passed = bool(recorded) and current == recorded
    detail = (
        "plan file digest matches the frozen run digest"
        if passed
        else "plan file digest does not match the frozen run digest"
    )
    evidence = {"path": str(path), "recorded_sha256": recorded, "current_sha256": current}
    return FloorFact(1, "plan-digest", passed, detail, evidence)


def _fact_identity_branch(repo: Path, state: dict[str, Any]) -> FloorFact:
    evidence: dict[str, Any] = {"repo": str(repo), "recorded_repo": state.get("repo"), "recorded_branch": state.get("branch")}
    try:
        resolved_repo = git_ops.resolve_repo(repo)
    except PmError as exc:
        evidence["error"] = str(exc)
        return FloorFact(2, "identity-branch", False, f"repo could not be resolved: {exc}", evidence)
    evidence["resolved_repo"] = str(resolved_repo)

    recorded_repo_raw = state.get("repo")
    recorded_repo: Path | None
    try:
        recorded_repo = Path(recorded_repo_raw).resolve() if recorded_repo_raw else None
    except OSError:
        recorded_repo = None

    branch = git_ops.current_branch(repo)
    evidence["current_branch"] = branch

    repo_matches = recorded_repo is not None and resolved_repo == recorded_repo
    branch_matches = branch is not None and branch == state.get("branch")
    passed = repo_matches and branch_matches
    if not repo_matches:
        detail = "resolved repo path does not match the run's recorded repo"
    elif not branch_matches:
        detail = "current branch is detached, unborn, or does not match the run's recorded branch"
    else:
        detail = "repo path and current branch match the run state"
    return FloorFact(2, "identity-branch", passed, detail, evidence)


def _fact_approval(state: dict[str, Any], plan_slice: PlanSlice | None, slice_id: str) -> FloorFact:
    evidence: dict[str, Any] = {"slice": slice_id}
    if plan_slice is None:
        return FloorFact(3, "approval", False, f"{slice_id} was not found in the parsed plan", evidence)

    approval_needed = plan_slice.approval_needed
    evidence["approval_needed"] = approval_needed
    approvals = state.get("approvals") if isinstance(state.get("approvals"), dict) else {}
    recorded = slice_id in approvals
    evidence["recorded_approval"] = recorded

    if approval_needed is None:
        return FloorFact(3, "approval", False, "the slice's approval flag is missing or unclear", evidence)
    if approval_needed is False:
        return FloorFact(3, "approval", True, "the slice does not require approval", evidence)
    detail = (
        "a human approval is recorded for this slice"
        if recorded
        else "this slice requires a recorded human approval and none is present"
    )
    return FloorFact(3, "approval", recorded, detail, evidence)


def _fact_result(artifact_dir: Path, slice_id: str) -> FloorFact:
    result_path = artifact_dir / "result.json"
    evidence: dict[str, Any] = {"path": str(result_path)}
    if not result_path.is_file():
        return FloorFact(4, "result", False, "result.json does not exist in the slice artifact directory", evidence)
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return FloorFact(4, "result", False, f"result.json could not be parsed: {exc}", evidence)
    if not isinstance(data, dict):
        return FloorFact(4, "result", False, "result.json does not contain a JSON object", evidence)
    evidence["parsed_slice"] = data.get("slice")
    evidence["parsed_status"] = data.get("status")
    passed = data.get("slice") == slice_id
    detail = (
        "result.json exists and names the expected slice"
        if passed
        else "result.json names a different slice than the one being evaluated"
    )
    return FloorFact(4, "result", passed, detail, evidence)


def _fact_surface(repo: Path, state: dict[str, Any], plan_slice: PlanSlice | None) -> FloorFact:
    current_slice = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else {}
    before_head = current_slice.get("before_head")
    evidence: dict[str, Any] = {"before_head": before_head}
    if plan_slice is None:
        return FloorFact(5, "surface", False, "slice not found in the parsed plan; authorized surface is unknown", evidence)

    try:
        after_head = git_ops.git_head(repo)
        status_text = git_ops.git_status_text(repo)
        changed = git_ops.changed_files_between(repo, before_head, after_head, status_text)
    except PmError as exc:
        evidence["error"] = str(exc)
        return FloorFact(5, "surface", False, f"changed files could not be computed: {exc}", evidence)

    authorized = plan_slice.authorized_files
    unauthorized = git_ops.unauthorized_files(changed, authorized)
    evidence["changed_files"] = sorted(changed)
    evidence["unauthorized_files"] = unauthorized
    evidence["authorized_surface"] = list(authorized)
    passed = not unauthorized
    detail = (
        "all changed files are within the authorized surface"
        if passed
        else "changed files include entries outside the authorized surface"
    )
    return FloorFact(5, "surface", passed, detail, evidence)


def _fact_commit_ancestry(repo: Path, state: dict[str, Any]) -> FloorFact:
    policy = state.get("policy") if isinstance(state.get("policy"), dict) else {}
    commit_required = bool(policy.get("commit_required", True))
    current_slice = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else {}
    before_head = current_slice.get("before_head")
    evidence: dict[str, Any] = {"commit_required": commit_required, "before_head": before_head}

    if not commit_required:
        return FloorFact(6, "commit-ancestry", True, "no commit is required by run policy", evidence)

    try:
        head = git_ops.git_head(repo)
    except PmError as exc:
        evidence["error"] = str(exc)
        return FloorFact(6, "commit-ancestry", False, f"HEAD could not be resolved: {exc}", evidence)
    evidence["head"] = head
    if head is None:
        return FloorFact(6, "commit-ancestry", False, "no HEAD commit exists", evidence)
    if head == before_head:
        return FloorFact(6, "commit-ancestry", False, "HEAD has not advanced since before_head", evidence)

    try:
        descends = git_ops.commit_is_descendant(repo, before_head, head)
    except PmError as exc:
        evidence["error"] = str(exc)
        return FloorFact(6, "commit-ancestry", False, f"ancestry could not be computed: {exc}", evidence)
    evidence["descends_from_before_head"] = descends

    branch = state.get("branch")
    branch_head: str | None = None
    if branch:
        try:
            branch_head = git_ops.git(repo, "rev-parse", f"refs/heads/{branch}")
        except PmError:
            branch_head = None
    evidence["branch"] = branch
    evidence["branch_head"] = branch_head

    passed = descends and branch_head is not None and head == branch_head
    if not descends:
        detail = "HEAD does not descend from before_head"
    elif branch_head is None:
        detail = f"recorded branch {branch!r} could not be resolved"
    elif head != branch_head:
        detail = "HEAD is not the tip of the run's recorded branch (commit landed on a different branch)"
    else:
        detail = "a commit exists, HEAD advanced, descends from before_head, and is the recorded branch's head"
    return FloorFact(6, "commit-ancestry", passed, detail, evidence)


def _fact_clean_worktree(repo: Path) -> FloorFact:
    try:
        status_text = git_ops.git_status_text(repo)
    except PmError as exc:
        return FloorFact(7, "clean-worktree", False, f"git status failed: {exc}", {"error": str(exc)})
    meaningful = git_ops.meaningful_status_lines(status_text)
    evidence = {"dirty_lines": meaningful}
    passed = not meaningful
    detail = "worktree is clean outside .pm/" if passed else "worktree has changes outside .pm/"
    return FloorFact(7, "clean-worktree", passed, detail, evidence)


def _fact_hard_stop_scan(pane_text: str) -> FloorFact:
    result = scan_hard_stop(pane_text)
    evidence = {"kinds": list(result["kinds"]), "markers": list(result["markers"])}
    passed = not result["present"]
    detail = (
        "no hard-stop marker is visible in the captured pane"
        if passed
        else "a hard-stop marker is visible in the captured pane"
    )
    return FloorFact(8, "hard-stop-scan", passed, detail, evidence)


def evaluate_floor(
    repo: Path,
    state: dict[str, Any],
    slices: list[PlanSlice],
    slice_id: str,
    *,
    artifact_dir: Path,
    pane_text: str,
) -> FloorReport:
    plan_slice = plan_slice_by_id(slices, slice_id)
    facts = (
        _fact_plan_digest(state),
        _fact_identity_branch(repo, state),
        _fact_approval(state, plan_slice, slice_id),
        _fact_result(artifact_dir, slice_id),
        _fact_surface(repo, state, plan_slice),
        _fact_commit_ancestry(repo, state),
        _fact_clean_worktree(repo),
        _fact_hard_stop_scan(pane_text),
    )
    return FloorReport(facts=facts)

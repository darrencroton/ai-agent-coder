"""The mechanical floor: seven non-waivable facts.

One function surface, no decisions. `evaluate_floor` runs the seven
`_fact_*` functions below, in order, and each computes a true/false
condition from git, the filesystem, and run state — never a model call,
never prose semantics, never a tmux shell-out (that boundary belongs to
`sessions.py`).

Every fact here is a property of repository state: a digest, a branch, an
ancestor, a file set. There is deliberately no fact for "a blocking prompt is
visible in the pane". That one read a rendered TUI and inferred a semantic
conclusion from keywords — judgement dressed as determinism, which
docs/VISION.md principle 2 forbids and its mechanical-guarantee list never
claimed. It is now the PM agent's reading of the captured pane, recorded in
the slice assessment.

A fact that cannot be established (a missing file, a git command that fails)
is `passed=False` with the reason in `detail`: this module never raises on
ordinary git/filesystem absence or failure, and it never writes state,
contacts a session, or renders an accept/reject verdict. That judgement is
the PM agent's, above this floor.

The seven facts, in evaluation order — each `_fact_*` function below carries
the exact pass/fail wording in its `detail` strings:

1. plan-digest — the plan file still hashes to the run's frozen digest.
2. identity-branch — repo path and current branch match the run's record.
3. approval — an approval-flagged slice has a recorded human approval.
4. result — result.json exists, parses, and names this slice.
5. surface — changed files are a subset of the effective authorized surface
   (frozen plan surface + PM grants).
6. commit-ancestry — a commit exists, descends from before_head, and is the
   recorded branch's tip.
7. clean-worktree — nothing dirty outside `.pm/`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PmError
from . import git_ops
from .plan import PlanSlice
from .plan import effective_authorized_files
from .plan import plan_digest as compute_plan_digest
from .plan import plan_slice_by_id
from .plan import slice_grants


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
    """An unclear flag is checked before the recorded approval and always
    fails: it is a planning defect, not an approval question."""
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

    authorized = effective_authorized_files(plan_slice, state)
    unauthorized = git_ops.unauthorized_files(changed, authorized)
    evidence["changed_files"] = sorted(changed)
    evidence["unauthorized_files"] = unauthorized
    evidence["authorized_surface"] = list(plan_slice.authorized_files)
    evidence["granted_surface"] = [g["path"] for g in slice_grants(state, plan_slice.slice_id)]
    passed = not unauthorized
    detail = (
        "all changed files are within the effective authorized surface"
        if passed
        else "changed files include entries outside the effective authorized surface"
    )
    return FloorFact(5, "surface", passed, detail, evidence)


def _fact_commit_ancestry(repo: Path, state: dict[str, Any]) -> FloorFact:
    current_slice = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else {}
    before_head = current_slice.get("before_head")
    evidence: dict[str, Any] = {"before_head": before_head}

    # `git_head` and `commit_is_descendant` are built on `git_result`, which
    # reports a failing git by return code and never raises PmError — so
    # neither call needs a catch here. The branch rev-parse below uses `git`,
    # which does raise, and keeps its own.
    head = git_ops.git_head(repo)
    evidence["head"] = head
    if head is None:
        return FloorFact(6, "commit-ancestry", False, "no HEAD commit exists", evidence)
    if head == before_head:
        return FloorFact(6, "commit-ancestry", False, "HEAD has not advanced since before_head", evidence)

    descends = git_ops.commit_is_descendant(repo, before_head, head)
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


def evaluate_floor(
    repo: Path,
    state: dict[str, Any],
    slices: list[PlanSlice],
    slice_id: str,
    *,
    artifact_dir: Path,
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
    )
    return FloorReport(facts=facts)

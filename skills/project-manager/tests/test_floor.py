"""Protected behaviours: the mechanical floor's eight facts (target-design §3.3).

Pure git + filesystem tests, no tmux. Pins:

- The floor is exactly eight facts, numbered and named per target-design
  §3.3 / implementation-blueprint §3 Stage 2 AC: plan-digest,
  identity-branch, approval, result, surface, commit-ancestry,
  clean-worktree, hard-stop-scan.
- Each fact is independently failable without the others being coupled to
  it (a happy-path baseline, then one deliberate perturbation per test):
  - (1) an edited plan file fails plan-digest.
  - (2) a switched branch, and separately a repo-path mismatch, fail
    identity-branch.
  - (3) an approval-flagged slice without a recorded approval fails; with
    a recorded approval it passes; an unclear approval flag fails
    regardless of a recorded approval.
  - (4) a missing result.json, a wrong-slice result.json, and malformed
    JSON each fail the result fact.
  - (5) an unauthorized file change fails the surface fact, whether
    committed or left dirty in the working tree.
  - (6) no commit when required fails; a commit landed on a different
    branch that still descends from before_head fails commit-ancestry
    (and, independently, identity-branch — the blueprint's named
    branch-switch scenario) even though the commit is a real descendant;
    a HEAD that is not a descendant of before_head (reset to an unrelated
    commit) fails; commit_required=false passes without any commit.
  - (7) a dirty file outside `.pm/` fails clean-worktree; `.pm/` litter
    alone passes.
  - (8) a credential-prompt in the pane text fails ONLY the hard-stop-scan
    fact even alongside an otherwise fully valid commit (the blueprint's
    named scenario); an empty pane passes.
- `FloorReport.passed` is exactly the conjunction of all eight facts'
  `passed` values.
- `evaluate_floor` never mutates run state and never writes files outside
  the artifact directory it is given — it only reads.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import PmTestCase

from pm_lib import floor as floor_mod
from pm_lib import plan as plan_mod


def _facts_by_name(report: floor_mod.FloorReport) -> dict[str, floor_mod.FloorFact]:
    return {fact.name: fact for fact in report.facts}


class FloorTestCase(PmTestCase):
    """Shared happy-path setup: a run with one authorized-surface commit."""

    def setUp(self) -> None:
        super().setUp()
        self.artifact_dir = self.repo / ".pm" / "runs" / "current" / "slices" / "slice-001"
        self.artifact_dir.mkdir(parents=True)

    def _write_result(self, slice_id: str = "Slice 1", *, artifact_dir: Path | None = None, extra: dict | None = None) -> None:
        directory = artifact_dir or self.artifact_dir
        payload = {"slice": slice_id, "status": "complete"}
        if extra:
            payload.update(extra)
        (directory / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    def _commit_authorized_change(self) -> None:
        (self.repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        self._git("add", "a.py")
        self._git("commit", "-q", "-m", "slice 1 work")

    def _plan_path(self) -> Path:
        # Deliberately outside self.repo: a plan.md living untracked inside
        # the repo would itself show up as a "changed" (untracked) file in
        # every floor evaluation — a self-inflicted surface/dirty-worktree
        # failure that has nothing to do with the fact under test. The real
        # CLI (Stage 3) accepts an arbitrary --plan path, so this is a
        # faithful, not merely convenient, test shape.
        return self.repo.parent / "plan.md"

    def _happy_path(self, *, approval: str = "no", files: list[str] | None = ("a.py",)):
        plan_path = self.write_plan(self._plan_path(), slices=[{"approval": approval, "files": files}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        before_head = self._git("rev-parse", "HEAD").stdout.strip()
        slices = plan_mod.parse_plan(plan_path)
        return plan_path, state, token, run_dir, before_head, slices


class TestFloorHappyPath(FloorTestCase):
    def test_all_eight_facts_pass(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text="Slice complete. Tests passed."
        )

        self.assertTrue(report.passed)
        self.assertEqual([fact.number for fact in report.facts], list(range(1, 9)))
        self.assertEqual(
            [fact.name for fact in report.facts],
            [
                "plan-digest",
                "identity-branch",
                "approval",
                "result",
                "surface",
                "commit-ancestry",
                "clean-worktree",
                "hard-stop-scan",
            ],
        )
        for fact in report.facts:
            self.assertTrue(fact.passed, f"{fact.name} unexpectedly failed: {fact.detail}")

    def test_empty_pane_text_passes_hard_stop_fact(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text="")

        self.assertTrue(report.passed)
        self.assertTrue(_facts_by_name(report)["hard-stop-scan"].passed)


class TestFactPlanDigest(FloorTestCase):
    def test_edited_plan_file_fails_plan_digest_only(self) -> None:
        plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        with plan_path.open("a", encoding="utf-8") as handle:
            handle.write("\n<!-- edited after run creation -->\n")

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        facts = _facts_by_name(report)
        self.assertFalse(report.passed)
        self.assertFalse(facts["plan-digest"].passed)
        for name in ("identity-branch", "approval", "result", "surface", "commit-ancestry", "clean-worktree", "hard-stop-scan"):
            self.assertTrue(facts[name].passed, f"{name} unexpectedly failed: {facts[name].detail}")

    def test_missing_plan_file_fails_never_raises(self) -> None:
        plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )
        plan_path.unlink()

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["plan-digest"].passed)


class TestFactIdentityBranch(FloorTestCase):
    def test_branch_switch_fails_identity_branch(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )
        self._git("checkout", "-q", "-b", "other-branch")

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(report.passed)
        self.assertFalse(_facts_by_name(report)["identity-branch"].passed)

    def test_repo_path_mismatch_fails_identity_branch(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        other_repo = self.repo.parent / "other-repo"
        other_repo.mkdir()
        self.subprocess_run_init(other_repo)

        report = floor_mod.evaluate_floor(
            other_repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(report.passed)
        self.assertFalse(_facts_by_name(report)["identity-branch"].passed)

    def subprocess_run_init(self, path: Path) -> None:
        import subprocess

        subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "pm-test@example.com"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "PM Test"], check=True)
        (path / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial commit"], check=True)


class TestFactApproval(FloorTestCase):
    def test_approval_needed_without_recorded_approval_fails(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path(approval="yes")
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(report.passed)
        self.assertFalse(_facts_by_name(report)["approval"].passed)

    def test_approval_needed_with_recorded_approval_passes(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path(approval="yes")
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )
        state = self.record_approval(state, token, run_dir, slice_id="Slice 1")

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertTrue(_facts_by_name(report)["approval"].passed)

    def test_unclear_approval_flag_fails_even_with_recorded_approval(self) -> None:
        plan_path = self._plan_path()
        # Hand-write a plan whose approval line is neither exactly "yes" nor "no".
        body = (
            "# Test Plan\n\n"
            "## Slice 1: title\n\n"
            "### Intended Change\nDo the thing.\n\n"
            "### Acceptance Criteria\nIt works.\n\n"
            "### Authorized Surface\n"
            "- Files allowed to change:\n  - a.py\n"
            "- Functions/classes/components allowed to change: none.\n"
            "- Tests allowed or expected to change: none.\n\n"
            "### Explicit Non-Goals\nNothing else.\n\n"
            "### Risk Flags\n"
            "- Risky surfaces touched: none.\n"
            "- Approval needed before implementation: not yet decided.\n"
            "- Independent audit required: no.\n\n"
            "### Validation Plan\nRun the tests.\n\n"
            "### Rollback Path\ngit revert.\n\n"
        )
        plan_path.write_text(body, encoding="utf-8")
        state, token, run_dir = self.make_run(plan_path=plan_path)
        before_head = self._git("rev-parse", "HEAD").stdout.strip()
        slices = plan_mod.parse_plan(plan_path)
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )
        state = self.record_approval(state, token, run_dir, slice_id="Slice 1")

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["approval"].passed)


class TestFactResult(FloorTestCase):
    def test_missing_result_fails(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["result"].passed)

    def test_wrong_slice_result_fails(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result(slice_id="Slice 2")
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["result"].passed)

    def test_malformed_json_result_fails(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        (self.artifact_dir / "result.json").write_text("{not valid json", encoding="utf-8")
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["result"].passed)


class TestFactSurface(FloorTestCase):
    def test_unauthorized_committed_change_fails_surface(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        (self.repo / "b.py").write_text("y = 2\n", encoding="utf-8")
        self._git("add", "b.py")
        self._git("commit", "-q", "-m", "unauthorized change")
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["surface"].passed)

    def test_unauthorized_dirty_change_fails_surface(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        (self.repo / "b.py").write_text("y = 2\n", encoding="utf-8")  # dirty, uncommitted
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["surface"].passed)


class TestFactCommitAncestry(FloorTestCase):
    def test_no_commit_when_required_fails(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["commit-ancestry"].passed)

    def test_commit_on_different_branch_fails_commit_ancestry_and_identity_branch(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._git("checkout", "-q", "-b", "side-branch")
        self._commit_authorized_change()  # descends from before_head, but not on state.branch ("main")
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        facts = _facts_by_name(report)
        self.assertFalse(report.passed)
        self.assertFalse(facts["commit-ancestry"].passed)
        self.assertFalse(facts["identity-branch"].passed)

    def test_head_not_descended_fails(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        # An unrelated, orphan-history commit: HEAD advances but does not
        # descend from before_head (a stand-in for reset/amend divergence).
        self._git("checkout", "-q", "--orphan", "unrelated")
        (self.repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        self._git("add", "a.py")
        self._git("commit", "-q", "-m", "unrelated root commit")
        self._git("branch", "-f", "main", "unrelated")
        self._git("checkout", "-q", "main")
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["commit-ancestry"].passed)

    def test_commit_required_false_passes_without_any_commit(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path, policy={"max_attempts": 3, "commit_required": False})
        before_head = self._git("rev-parse", "HEAD").stdout.strip()
        slices = plan_mod.parse_plan(plan_path)
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertTrue(_facts_by_name(report)["commit-ancestry"].passed)


class TestFactCleanWorktree(FloorTestCase):
    def test_dirty_file_outside_pm_fails(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        (self.repo / "stray.txt").write_text("oops\n", encoding="utf-8")
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertFalse(_facts_by_name(report)["clean-worktree"].passed)

    def test_pm_litter_alone_passes(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )
        # self.artifact_dir already carries litter under .pm/ (result.json).

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text=""
        )
        self.assertTrue(_facts_by_name(report)["clean-worktree"].passed)


class TestFactHardStopScan(FloorTestCase):
    def test_credential_prompt_fails_only_hard_stop_fact(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )

        report = floor_mod.evaluate_floor(
            self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text="Enter API key to continue"
        )
        facts = _facts_by_name(report)
        self.assertFalse(report.passed)
        self.assertFalse(facts["hard-stop-scan"].passed)
        for name in ("plan-digest", "identity-branch", "approval", "result", "surface", "commit-ancestry", "clean-worktree"):
            self.assertTrue(facts[name].passed, f"{name} unexpectedly failed: {facts[name].detail}")


class TestFloorNeverMutates(FloorTestCase):
    def test_evaluate_floor_does_not_write_state_or_files(self) -> None:
        _plan_path, state, token, run_dir, before_head, slices = self._happy_path()
        self._commit_authorized_change()
        self._write_result()
        state = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, artifact_dir=self.artifact_dir
        )
        run_json = run_dir / "run.json"
        before_bytes = run_json.read_bytes()
        before_status = self._git("status", "--short").stdout

        floor_mod.evaluate_floor(self.repo, state, slices, "Slice 1", artifact_dir=self.artifact_dir, pane_text="benign text")

        after_bytes = run_json.read_bytes()
        after_status = self._git("status", "--short").stdout
        self.assertEqual(before_bytes, after_bytes)
        self.assertEqual(before_status, after_status)


if __name__ == "__main__":
    unittest.main()

"""Tests for `review-plan`: the round's request is built from the node
directory alone, and the launch path fails closed on a report that is not a
review.

The orchestrator is never run here: `plan_review._DELEGATE_JOBS` is pointed at
a stand-in script that logs each step it is asked for and answers from a
per-test config, so the sequence, the run root and every failure branch are
exercised without a harness.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import PlanTestCase, render_slice  # noqa: E402

from pm_lib import PmError  # noqa: E402
from pm_lib import plan_review  # noqa: E402

_PLAN = "# Node c01 plan\n\nThis plan rests on D2.\n\n" + render_slice(
    1, files=["`src/present.py`", "src/absent.py", "src/*.c", "docs/"]
)
_DECISIONS = (
    "# Decisions\n\n"
    "## D1: an uncited decision\n- At: 2026-01-01T00:00:00Z\n- Actor: CM\n- Decision: ONLY-IN-D1\n\n"
    "## D2: the cited decision\n- At: 2026-01-02T00:00:00Z\n- Actor: owner\n- Decision: ONLY-IN-D2\n"
)
_POLICY = '{"protected": ["tests/"]}\n'

# The stand-in for delegate_jobs.py: it logs its argv and run root, and
# answers `wait`, `status` and `extract` from the config file beside it.
_FAKE_DELEGATE_JOBS = """\
import json, os, sys
from pathlib import Path

config = json.loads((Path(__file__).parent / "config.json").read_text())
with open(config["log"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "root": os.environ.get("ORCHESTRATOR_ARTIFACT_ROOT")}) + "\\n")
command = sys.argv[1]
if command == "init":
    run = Path(os.environ["ORCHESTRATOR_ARTIFACT_ROOT"]) / "review-plan-fake"
    run.mkdir(parents=True, exist_ok=True)
    print(run)
elif command == "wait":
    sys.exit(config["wait_code"])
elif command == "status":
    label = sys.argv[sys.argv.index("--label") + 1]
    print(json.dumps([{"label": label, "state": config["state"], "running": False, "returncode": 0}]))
elif command == "extract":
    sys.stdout.write(config["report"])
"""


class PlanReviewFixture(PlanTestCase):
    """A dossier (a git repository) with node c01, and a product repository
    holding one of the two files the plan's surface names."""

    def setUp(self) -> None:
        super().setUp()
        root = Path(self._tmp.name)
        self.dossier = root / "dossier"
        self.node_dir = self.dossier / "nodes" / "c01"
        self.node_dir.mkdir(parents=True)
        (self.node_dir / "plan.md").write_text(_PLAN, encoding="utf-8")
        (self.node_dir / "policy.json").write_text(_POLICY, encoding="utf-8")
        (self.dossier / "decisions.md").write_text(_DECISIONS, encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "present.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        self.seat = ("codex", "gpt-6.1-sol", "high")

    def build(self, number: int) -> tuple[dict, dict]:
        return plan_review.build_round(
            node_dir=self.node_dir,
            dossier=self.dossier,
            repo=self.repo.resolve(),
            seat=self.seat,
            number=number,
            artifact_root=Path(self._tmp.name) / "results",
        )


class BuildRoundTests(PlanReviewFixture):
    def test_a_later_round_carries_the_plan_policy_cited_decision_and_prior_round(self) -> None:
        prior = plan_review.round_dir(self.node_dir, 1)
        prior.mkdir(parents=True)
        (prior / "report.md").write_text("RESULT: revise\nPRIOR-REPORT-TEXT\n", encoding="utf-8")
        (prior / "status.json").write_text('{"state": "completed"}\n', encoding="utf-8")
        (prior / "dispositions.md").write_text("PRIOR-DISPOSITIONS-TEXT\n", encoding="utf-8")
        self.assertEqual(plan_review.next_round(self.node_dir), 2)

        policy, request = self.build(2)

        context = request["context"]
        for expected in (
            "This plan rests on D2.",
            _POLICY.strip(),
            "ONLY-IN-D2",
            "PRIOR-REPORT-TEXT",
            "PRIOR-DISPOSITIONS-TEXT",
        ):
            self.assertIn(expected, context)
        self.assertNotIn("ONLY-IN-D1", context)
        # Only the existing regular file: the absent file, the glob and the subtree are not inputs.
        self.assertEqual(request["files"], ["src/present.py"])
        self.assertEqual(request["label"], "01-codex-c01-plan-review-round2")
        self.assertEqual(request["slice_id"], "c01-plan-review")
        self.assertEqual(policy["slice_id"], "c01-plan-review")
        self.assertNotIn("parent_label", request)
        self.assertNotIn("plan_sha256", request)
        self.assertEqual(policy["plan_path"], str((self.node_dir / "plan.md").resolve()))
        self.assertEqual((policy["required_model"], policy["required_effort"]), ("gpt-6.1-sol", "high"))
        self.assertEqual(request["required_skills"], ["implementation-plan"])
        self.assertTrue(request["task"].startswith("Round 2: review the revised c01 plan"))
        self.assertNotIn("Do not re-raise these dispositioned items: none.", request["constraints"])

    def test_round_one_has_no_prior_round(self) -> None:
        self.assertEqual(plan_review.next_round(self.node_dir), 1)
        self.seat = ("claude", "claude-opus-5-5", None)

        policy, request = self.build(1)

        self.assertEqual(request["label"], "01-claude-c01-plan-review-round1")
        self.assertEqual((request["effort"], policy["required_effort"]), ("default", "default"))
        self.assertIn("===== BEGIN previous round report =====\nnone\n", request["context"])
        self.assertIn(
            "===== BEGIN previous round dispositions (the Campaign Manager's) =====\nnone\n", request["context"]
        )
        self.assertIn("Do not re-raise these dispositioned items: none.", request["constraints"])
        self.assertTrue(request["task"].startswith("Round 1:"))


class ReviewPlanLaunchTests(PlanReviewFixture):
    def setUp(self) -> None:
        super().setUp()
        subprocess.run(["git", "init", "-q", str(self.dossier)], check=True)
        self.fake_dir = Path(tempfile.mkdtemp(dir=self._tmp.name))
        self.fake = self.fake_dir / "delegate_jobs.py"
        self.fake.write_text(_FAKE_DELEGATE_JOBS, encoding="utf-8")
        self.log = self.fake_dir / "log.jsonl"
        patcher = mock.patch.object(plan_review, "_DELEGATE_JOBS", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, *, report: str, wait_code: int = 0, state: str = "completed") -> None:
        config = {"log": str(self.log), "report": report, "wait_code": wait_code, "state": state}
        (self.fake_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def steps(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def review(self, **kwargs) -> plan_review.ReviewRound:
        return plan_review.review_plan(node=self.node_dir, repo=self.repo, seat=self.seat, **kwargs)

    def test_a_clean_round_is_recorded_and_the_next_round_is_a_new_directory(self) -> None:
        self.configure(report="\nRESULT: clean\nSECTION: FINDINGS\n- none\n")

        first = self.review()

        round_one = self.node_dir.resolve() / "reviews" / "round-1"
        self.assertEqual(
            (first.round_dir, first.label, first.result_line),
            (round_one, "01-codex-c01-plan-review-round1", "RESULT: clean"),
        )
        for name in ("delegate-policy.json", "request.json", "report.md", "status.json"):
            self.assertTrue((round_one / name).is_file(), name)
        self.assertEqual(json.loads((round_one / "status.json").read_text())["state"], "completed")
        steps = self.steps()
        self.assertEqual([step["argv"][0] for step in steps], ["init", "launch", "wait", "status", "extract"])
        self.assertEqual({step["root"] for step in steps}, {str(round_one / "orchestrator")})
        self.assertIn("3600", steps[2]["argv"])

        (round_one / "dispositions.md").write_text("ROUND-ONE-DISPOSITIONS\n", encoding="utf-8")
        second = self.review()

        self.assertEqual(second.round_dir.name, "round-2")
        self.assertEqual(second.label, "01-codex-c01-plan-review-round2")
        context = json.loads((second.round_dir / "request.json").read_text())["context"]
        self.assertIn("RESULT: clean", context)
        self.assertIn("ROUND-ONE-DISPOSITIONS", context)

    def test_a_round_after_a_failed_one_carries_the_last_reported_round_forward(self) -> None:
        self.configure(report="RESULT: revise\nSECTION: FINDINGS\n- FIX Slice 1: name the test.\n")
        self.review()
        (self.node_dir / "reviews" / "round-1" / "dispositions.md").write_text(
            "ROUND-ONE-DISPOSITIONS\n", encoding="utf-8"
        )
        # The failed round still writes what came back, which must not count as a reported round.
        self.configure(
            report="RESULT: revise\nSECTION: FINDINGS\n- FIX: a reviewer that then hung\n", wait_code=1, state="failed"
        )
        with self.assertRaises(PmError):
            self.review()
        self.assertTrue((self.node_dir / "reviews" / "round-2" / "report.md").is_file())
        self.configure(report="RESULT: clean\nSECTION: FINDINGS\n- none\n")

        third = self.review()

        self.assertEqual(third.round_dir.name, "round-3")
        request = json.loads((third.round_dir / "request.json").read_text())
        self.assertIn("ROUND-ONE-DISPOSITIONS", request["context"])
        self.assertIn("===== BEGIN round-1 report =====", request["context"])
        self.assertTrue(
            request["task"].startswith(
                "Round 3: review the revised c01 plan (inlined) and disposition each round-1 finding"
            )
        )

    def test_an_empty_report_fails_and_keeps_the_request(self) -> None:
        self.configure(report="  \n")
        results = Path(self._tmp.name) / "results"

        with self.assertRaisesRegex(PmError, "report is empty"):
            self.review(results=results)

        round_one = self.node_dir / "reviews" / "round-1"
        self.assertTrue((round_one / "request.json").is_file())
        self.assertFalse((round_one / "report.md").exists())
        self.assertEqual({step["root"] for step in self.steps()}, {str(results.resolve())})

    def test_a_failed_wait_fails_but_still_writes_what_came_back(self) -> None:
        self.configure(report="I looked at it and it seems fine.\n", wait_code=1, state="incomplete")

        with self.assertRaisesRegex(PmError, "state=incomplete.*the report is at"):
            self.review()

        report = self.node_dir / "reviews" / "round-1" / "report.md"
        self.assertEqual(report.read_text(encoding="utf-8"), "I looked at it and it seems fine.\n")

    def test_a_report_without_a_result_line_fails(self) -> None:
        self.configure(report="SECTION: FINDINGS\n- none\n")

        with self.assertRaisesRegex(PmError, "does not open with RESULT"):
            self.review()

    def test_the_cli_prints_the_round_label_result_and_report(self) -> None:
        self.configure(report="RESULT: revise\nSECTION: FINDINGS\n- FIX Slice 1: name the test.\n")

        code, out, err = self.run_cli(
            ["review-plan", "--node", str(self.node_dir), "--repo", str(self.repo), "--seat", "codex gpt-6.1-sol high"]
        )

        self.assertEqual(code, 0, err)
        round_one = self.node_dir.resolve() / "reviews" / "round-1"
        self.assertEqual(
            out.splitlines(),
            [
                f"round: {round_one}",
                "label: 01-codex-c01-plan-review-round1",
                "RESULT: revise",
                f"report: {round_one / 'report.md'}",
            ],
        )


if __name__ == "__main__":
    unittest.main()

"""Focused contracts for signed per-round reviewer judgments."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import judgments
from pm_lib import state as state_mod
from pm_test_helpers import PmTestCase


class TestReviewerJudgments(PmTestCase):
    def _run_with_reviews(self, count: int = 2) -> tuple[dict, str, Path]:
        plan_path = self.write_plan(self.repo.parent / "plan.md")
        state, token, run_dir = self.make_run(plan_path=plan_path)
        state_mod.append_event(run_dir, "launch", slice_id="Slice 1", note="attempt 0")
        entry = state["slices"][0]
        records = []
        for number in range(1, count + 1):
            path = run_dir / "slices" / "slice-1" / f"review-{number}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"review {number}\n", encoding="utf-8")
            records.append(
                {
                    "review_id": f"review-{number}",
                    "skill": "drift-audit" if number == 1 else "code-review",
                    "tool": "codex",
                    "model": "test-model",
                    "effort": "medium",
                    "head": "head",
                    "before_head": "before",
                    "grants_seen": 0,
                    "artifact": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "origin_event": {"index": 1, "kind": "launch", "slice": "Slice 1"},
                    "review_context": {"pm_adjudications": None, "drift_review": None},
                }
            )
        entry["reviews"] = records
        state_mod.save_state(run_dir, state, token)
        return state, token, run_dir

    def _judge(self, token: str, run_dir: Path, data: dict) -> tuple[int, str, str]:
        path = self.repo.parent / "judgment.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return self.run_cli_in_repo(
            ["judge-reviews", "--file", str(path), "--token", token]
        )

    def test_drift_score_is_signed_idempotent_and_reported(self) -> None:
        _state, token, run_dir = self._run_with_reviews()
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "drift-audit",
            "review_id": "review-1",
            "score": 2,
            "reason": "PM reproduced the specific finding.",
        }
        code, out, err = self._judge(token, run_dir, data)
        self.assertEqual(code, 0, err)
        self.assertIn("judgment-1", out)
        code, out, err = self._judge(token, run_dir, data)
        self.assertEqual(code, 0, err)
        self.assertIn("already recorded", out)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["score"], 2)
        report = state_mod.render_run_report(
            state_mod.load_state(run_dir, token),
            state_mod.read_events(run_dir),
            run_dir,
        )
        self.assertIn("drift review-1 (codex/test-model effort=medium) score 2", report)
        self.assertIn("Unjudged: Slice 1/review-2", report)

    def test_code_panel_tie_and_singleton_are_recorded(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=3)
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "rank_groups": [["review-2", "review-3"]],
            "reason": "Both reports supplied the same verified check.",
        }
        code, _out, err = self._judge(token, run_dir, data)
        self.assertEqual(code, 0, err)
        judgment = state_mod.load_state(run_dir, token)["slices"][0][
            "review_judgments"
        ][0]
        self.assertEqual(judgment["rank_groups"], [["review-2", "review-3"]])

        _state, token, run_dir = self._run_with_reviews()
        singleton = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "rank_groups": [["review-2"]],
            "reason": "Only one reviewer was commissioned.",
        }
        code, _out, err = self._judge(token, run_dir, singleton)
        self.assertEqual(code, 0, err)
        report = state_mod.render_run_report(
            state_mod.load_state(run_dir, token),
            state_mod.read_events(run_dir),
            run_dir,
        )
        self.assertIn("singleton/unranked", report)

    def test_ordered_panel_and_unavailable_mixed_context_are_independent(self) -> None:
        state, token, run_dir = self._run_with_reviews(count=5)
        ordered = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "rank_groups": [["review-2"], ["review-3", "review-4"], ["review-5"]],
            "reason": "One material finding, then a tie, then a minor check.",
        }
        code, _out, err = self._judge(token, run_dir, ordered)
        self.assertEqual(code, 0, err)

        state = state_mod.load_state(run_dir, token)
        state["slices"][0]["reviews"][2]["review_context"]["pm_adjudications"] = (
            "different"
        )
        state_mod.save_state(run_dir, state, token)
        invalid = {
            **ordered,
            "rank_groups": [["review-2", "review-3"]],
            "reason": "Not comparable.",
        }
        before = (run_dir / "run.json").read_bytes()
        code, _out, err = self._judge(token, run_dir, invalid)
        self.assertEqual(code, 2)
        self.assertIn("mixes commission contexts", err)
        self.assertEqual((run_dir / "run.json").read_bytes(), before)

        unavailable = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "status": "unavailable",
            "review_ids": ["review-2", "review-3"],
            "reason": "Instructions differed.",
            "supersedes": "judgment-1",
        }
        code, _out, err = self._judge(token, run_dir, unavailable)
        self.assertEqual(code, 0, err)

    def test_unknown_or_altered_review_is_rejected_without_state_change(self) -> None:
        state, token, run_dir = self._run_with_reviews()
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "drift-audit",
            "review_id": "review-9",
            "score": 1,
            "reason": "No such review.",
        }
        before = (run_dir / "run.json").read_bytes()
        code, _out, err = self._judge(token, run_dir, data)
        self.assertEqual(code, 2)
        self.assertIn("unknown drift-audit review ID", err)
        self.assertEqual((run_dir / "run.json").read_bytes(), before)

        Path(state["slices"][0]["reviews"][0]["artifact"]).write_text(
            "altered\n", encoding="utf-8"
        )
        data["review_id"] = "review-1"
        code, _out, err = self._judge(token, run_dir, data)
        self.assertEqual(code, 2)
        self.assertIn("missing or altered", err)

    def test_event_publication_failure_recovers_on_exact_retry(self) -> None:
        _state, token, run_dir = self._run_with_reviews()
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "drift-audit",
            "review_id": "review-1",
            "score": 1,
            "reason": "Useful evidence.",
        }
        path = self.repo.parent / "judgment.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        from unittest import mock

        with mock.patch.object(
            judgments.state_mod,
            "_append_event_unlocked",
            side_effect=OSError("disk full"),
        ):
            code, _out, err = self.run_cli_in_repo(
                ["judge-reviews", "--file", str(path), "--token", token]
            )
        self.assertEqual(code, 2)
        self.assertIn("was stored but event publication failed", err)
        self.assertEqual(
            len(state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]),
            1,
        )
        self.assertEqual(self._judge(token, run_dir, data)[0], 0)
        events = [
            event
            for event in state_mod.read_events(run_dir)
            if event["kind"] == "review-judgment"
        ]
        self.assertEqual(len(events), 1)

    def test_context_mismatches_refuse_a_panel_without_changing_state(self) -> None:
        changes = {
            "head": "other-head",
            "before_head": "other-before",
            "grants_seen": 1,
            "origin_event": {"index": 2, "kind": "steer", "slice": "Slice 1"},
            "review_context": {"pm_adjudications": "different", "drift_review": None},
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                state, token, run_dir = self._run_with_reviews(count=3)
                state = state_mod.load_state(run_dir, token)
                state["slices"][0]["reviews"][2][field] = value
                state_mod.save_state(run_dir, state, token)
                before = (run_dir / "run.json").read_bytes()
                data = {
                    "schema_version": 1,
                    "slice": "Slice 1",
                    "skill": "code-review",
                    "rank_groups": [["review-2", "review-3"]],
                    "reason": "PM cannot compare these reports.",
                }
                code, _out, err = self._judge(token, run_dir, data)
                self.assertEqual(code, 2)
                self.assertIn("mixes commission contexts", err)
                self.assertEqual((run_dir / "run.json").read_bytes(), before)

    def test_concurrent_event_retries_publish_once(self) -> None:
        _state, token, run_dir = self._run_with_reviews()
        outcome = judgments.record_judgment(
            run_dir,
            token,
            {
                "schema_version": 1,
                "slice": "Slice 1",
                "skill": "drift-audit",
                "review_id": "review-1",
                "score": 1,
                "reason": "Useful evidence.",
            },
        )
        threads = [
            threading.Thread(
                target=judgments.publish_event,
                args=(run_dir, token, outcome.judgment_id, outcome.slice_id),
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        events = [
            event
            for event in state_mod.read_events(run_dir)
            if event["kind"] == "review-judgment"
        ]
        self.assertEqual(len(events), 1)

    def test_event_publication_preserves_an_integrity_stop(self) -> None:
        _state, token, run_dir = self._run_with_reviews()
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "drift-audit",
            "review_id": "review-1",
            "score": 1,
            "reason": "Useful evidence.",
        }
        path = self.repo.parent / "judgment.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        from unittest import mock

        original = judgments.record_judgment

        def record_then_tamper(*args, **kwargs):
            outcome = original(*args, **kwargs)
            tampered = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            tampered["status"] = "stopped"
            (run_dir / "run.json").write_text(json.dumps(tampered), encoding="utf-8")
            return outcome

        with mock.patch.object(
            judgments, "record_judgment", side_effect=record_then_tamper
        ):
            code, _out, err = self.run_cli_in_repo(
                ["judge-reviews", "--file", str(path), "--token", token]
            )
        self.assertEqual(code, 2)
        self.assertIn("INTEGRITY", err)
        self.assertNotIn("retry the same input", err)

    def test_truncated_event_log_preserves_the_signed_judgment(self) -> None:
        _state, token, run_dir = self._run_with_reviews()
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "drift-audit",
            "review_id": "review-1",
            "score": 1,
            "reason": "Useful evidence.",
        }
        (run_dir / "events.jsonl").write_text("{", encoding="utf-8")
        code, _out, err = self._judge(token, run_dir, data)
        self.assertEqual(code, 2)
        self.assertIn(str(run_dir / "events.jsonl"), err)
        self.assertIn("resolve the publication error, then retry the same input", err)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]
        self.assertEqual(len(stored), 1)
        self.assertEqual((run_dir / "events.jsonl").read_text(encoding="utf-8"), "{")

    def test_invalid_score_does_not_change_signed_state(self) -> None:
        _state, token, run_dir = self._run_with_reviews()
        before = (run_dir / "run.json").read_bytes()
        code, _out, err = self._judge(
            token,
            run_dir,
            {
                "schema_version": 1,
                "slice": "Slice 1",
                "skill": "drift-audit",
                "review_id": "review-1",
                "score": True,
                "reason": "Boolean is not a score.",
            },
        )
        self.assertEqual(code, 2)
        self.assertIn("score must be integer", err)
        self.assertEqual((run_dir / "run.json").read_bytes(), before)

    def test_supersession_replaces_active_coverage(self) -> None:
        _state, token, run_dir = self._run_with_reviews()
        first = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "drift-audit",
            "review_id": "review-1",
            "score": 1,
            "reason": "Useful initial review.",
        }
        second = {
            **first,
            "score": 2,
            "reason": "PM verified the finding.",
            "supersedes": "judgment-1",
        }
        self.assertEqual(self._judge(token, run_dir, first)[0], 0)
        self.assertEqual(self._judge(token, run_dir, second)[0], 0)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]
        self.assertEqual(
            [item["judgment_id"] for item in stored], ["judgment-1", "judgment-2"]
        )
        self.assertEqual(stored[1]["supersedes"], "judgment-1")


if __name__ == "__main__":
    import unittest

    unittest.main()

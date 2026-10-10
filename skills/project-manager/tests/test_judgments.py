"""Focused contracts for signed per-round reviewer judgments."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import PmError, judgments
from pm_lib import state as state_mod
from pm_test_helpers import PmTestCase, judge_developer, judge_reviews

_MISSING = object()  # an override value meaning "omit this field"


class _JudgmentFixtures(PmTestCase):
    """Run builders shared by the judgment test classes; defines no tests."""

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
                    "origin_event": {"index": 0, "kind": "launch", "slice": "Slice 1"},
                    "review_context": {"pm_adjudications": None, "drift_review": None},
                }
            )
        entry["reviews"] = records
        state_mod.save_state(run_dir, state, token)
        return state, token, run_dir

    def _run_with_developer(self) -> tuple[str, Path]:
        plan_path = self.write_plan(self.repo.parent / "developer-plan.md")
        _state, token, run_dir = self.make_run(
            plan_path=plan_path,
            harness={"name": "codex", "model": "first-model", "effort": "medium"},
        )
        state_mod.append_event(run_dir, "launch", slice_id="Slice 1", note="attempt 0")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self.set_current_slice(
            state_mod.load_state(run_dir, token),
            token,
            run_dir,
            slice_id="Slice 1",
            before_head=head,
            developer={"tool": "codex", "model": "first-model", "effort": "medium"},
        )
        return token, run_dir


class TestReviewerJudgments(_JudgmentFixtures):
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
        code, out, err = judge_reviews(self, token, run_dir, data)
        self.assertEqual(code, 0, err)
        self.assertIn("judgment-1", out)
        code, out, err = judge_reviews(self, token, run_dir, data)
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

    def test_code_panel_order_is_recorded_and_rendered_with_close_calls(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=4)
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "order": ["review-2", "review-3", "review-4"],
            "close": ["review-4"],
            "reason": "Review 2 found the verified defect; 4 was too close to 3 to call.",
        }
        code, _out, err = judge_reviews(self, token, run_dir, data)
        self.assertEqual(code, 0, err)
        state = state_mod.load_state(run_dir, token)
        judgment = state["slices"][0]["review_judgments"][0]
        self.assertEqual(judgment["order"], ["review-2", "review-3", "review-4"])
        self.assertEqual(judgment["close"], ["review-4"])
        self.assertEqual(judgment["assessment"], "comparison")
        self.assertNotIn("rank_groups", judgment)
        report = state_mod.render_run_report(state, state_mod.read_events(run_dir), run_dir)
        self.assertIn(
            "code panel review-2 (codex/test-model effort=medium)"
            " > review-3 (codex/test-model effort=medium)"
            " ≈ review-4 (codex/test-model effort=medium)",
            report,
        )
        self.assertNotIn("singleton", report)

    def test_code_rating_and_panel_order_cover_independently(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=3)
        rating = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "review_id": "review-2",
            "score": 2,
            "reason": "The report identified and demonstrated the regression.",
        }
        order = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "order": ["review-2", "review-3"],
            "close": [],
            "reason": "Review 2 demonstrated the regression; review 3 only described it.",
        }
        self.assertEqual(judge_reviews(self, token, run_dir, rating)[0], 0)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(
            judgments.unranked_code_review_ids(state),
            [("Slice 1", "review-2"), ("Slice 1", "review-3")],
        )
        self.assertEqual(judgments.unjudged_review_ids(state), [("Slice 1", "review-1"), ("Slice 1", "review-3")])
        report = state_mod.render_run_report(state, state_mod.read_events(run_dir), run_dir)
        self.assertIn("Slice 1 attempt 1 judgment-1: code review-2", report)
        self.assertIn("Code panels without an order: Slice 1/review-2, Slice 1/review-3", report)

        self.assertEqual(judge_reviews(self, token, run_dir, order)[0], 0)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]
        self.assertEqual([item["assessment"] for item in stored], ["rating", "comparison"])
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(judgments.unranked_code_review_ids(state), [])
        self.assertNotIn(("Slice 1", "review-2"), judgments.unjudged_review_ids(state))

    def test_developer_judgment_retries_and_historical_corrections_preserve_identity(self) -> None:
        token, run_dir = self._run_with_developer()
        first = {
            "schema_version": 1,
            "slice": "Slice 1",
            "origin_event_index": 0,
            "score": 1,
            "criteria_met": 1,
            "defects": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
            "reason": "The implementation met the requested contract with minor follow-up.",
        }
        self.assertEqual(judge_developer(self, token, run_dir, first)[0], 0)
        state = state_mod.load_state(run_dir, token)
        state["current_slice"] = None
        state_mod.save_state(run_dir, state, token)
        self.assertEqual(judge_developer(self, token, run_dir, first)[0], 0)

        state = state_mod.load_state(run_dir, token)
        state_mod.append_event(run_dir, "relaunch", slice_id="Slice 1", note="attempt 1")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self.set_current_slice(
            state,
            token,
            run_dir,
            slice_id="Slice 1",
            before_head=head,
            developer={"tool": "codex", "model": "later-model", "effort": "high"},
        )
        correction = {
            **first,
            "score": 2,
            "reason": "A later PM check confirmed the original submission was excellent.",
            "supersedes": "developer-judgment-1",
        }
        self.assertEqual(judge_developer(self, token, run_dir, correction)[0], 0)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["developer_judgments"]
        self.assertEqual(stored[1]["developer"], stored[0]["developer"])
        self.assertEqual(stored[1]["submission"], stored[0]["submission"])

    def test_developer_submission_change_requires_supersession(self) -> None:
        token, run_dir = self._run_with_developer()
        first = {
            "schema_version": 1,
            "slice": "Slice 1",
            "origin_event_index": 0,
            "score": 1,
            "criteria_met": 1,
            "defects": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
            "reason": "The submission is adequate for the planned slice.",
        }
        self.assertEqual(judge_developer(self, token, run_dir, first)[0], 0)
        state = state_mod.load_state(run_dir, token)
        state["slices"][0]["grants"] = [{"path": "extra.py", "at": "now"}]
        state_mod.save_state(run_dir, state, token)
        code, _out, err = judge_developer(self, token, run_dir, first)
        self.assertEqual(code, 2)
        self.assertIn("submission changed", err)
        corrected = {
            **first,
            "score": 2,
            "reason": "The corrected submission now meets the expanded authorized surface.",
            "supersedes": "developer-judgment-1",
        }
        self.assertEqual(judge_developer(self, token, run_dir, corrected)[0], 0)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["developer_judgments"]
        self.assertEqual(stored[1]["submission"]["grants_seen"], 1)

    def test_developer_unavailable_event_retry_keeps_one_signed_judgment(self) -> None:
        token, run_dir = self._run_with_developer()
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "origin_event_index": 0,
            "status": "unavailable",
            "reason": "The PM could not assess this submission from the available evidence.",
        }
        from unittest import mock

        with mock.patch.object(
            judgments.state_mod,
            "_append_event_unlocked",
            side_effect=OSError("disk full"),
        ):
            code, _out, err = judge_developer(self, token, run_dir, data)
        self.assertEqual(code, 2)
        self.assertIn("was stored but event publication failed", err)
        state = state_mod.load_state(run_dir, token)
        [stored] = state["slices"][0]["developer_judgments"]
        self.assertEqual(stored["status"], "unavailable")
        self.assertNotIn("score", stored)

        state["current_slice"] = None
        state_mod.save_state(run_dir, state, token)
        self.assertEqual(judge_developer(self, token, run_dir, data)[0], 0)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(len(state["slices"][0]["developer_judgments"]), 1)
        events = [event for event in state_mod.read_events(run_dir) if event["kind"] == "developer-judgment"]
        self.assertEqual(len(events), 1)
        report = state_mod.render_run_report(state, state_mod.read_events(run_dir), run_dir)
        self.assertIn("developer codex/first-model effort=medium unavailable", report)

    def test_developer_correction_on_current_slice_fails_closed_on_unreadable_event_log(self) -> None:
        token, run_dir = self._run_with_developer()
        first = {
            "schema_version": 1,
            "slice": "Slice 1",
            "origin_event_index": 0,
            "score": 1,
            "criteria_met": 1,
            "defects": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
            "reason": "The submission met the requested contract.",
        }
        self.assertEqual(judge_developer(self, token, run_dir, first)[0], 0)
        correction = {
            **first,
            "score": 2,
            "reason": "A later PM check confirmed the original submission was excellent.",
            "supersedes": "developer-judgment-1",
        }
        from unittest import mock

        with mock.patch.object(judgments.state_mod, "read_events", side_effect=OSError("disk full")):
            code, _out, err = judge_developer(self, token, run_dir, correction)
        self.assertEqual(code, 2)
        self.assertIn("could not read PM event log", err)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["developer_judgments"]
        self.assertEqual(len(stored), 1)

    def test_ordered_panel_and_unavailable_mixed_context_are_independent(self) -> None:
        state, token, run_dir = self._run_with_reviews(count=5)
        ordered = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "order": ["review-2", "review-3", "review-4", "review-5"],
            "close": ["review-4"],
            "reason": "One material finding, then two near-equal checks, then a minor one.",
        }
        code, _out, err = judge_reviews(self, token, run_dir, ordered)
        self.assertEqual(code, 0, err)

        state = state_mod.load_state(run_dir, token)
        state["slices"][0]["reviews"][2]["review_context"]["pm_adjudications"] = "different"
        state_mod.save_state(run_dir, state, token)
        invalid = {
            **ordered,
            "order": ["review-2", "review-3"],
            "close": [],
            "reason": "Not comparable.",
        }
        before = (run_dir / "run.json").read_bytes()
        code, _out, err = judge_reviews(self, token, run_dir, invalid)
        self.assertEqual(code, 2)
        self.assertIn("mixes commission contexts", err)
        self.assertEqual((run_dir / "run.json").read_bytes(), before)

        unavailable = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "assessment": "comparison",
            "status": "unavailable",
            "review_ids": ["review-2", "review-3"],
            "reason": "Instructions differed.",
            "supersedes": "judgment-1",
        }
        code, _out, err = judge_reviews(self, token, run_dir, unavailable)
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
        code, _out, err = judge_reviews(self, token, run_dir, data)
        self.assertEqual(code, 2)
        self.assertIn("unknown drift-audit review ID", err)
        self.assertEqual((run_dir / "run.json").read_bytes(), before)

        Path(state["slices"][0]["reviews"][0]["artifact"]).write_text("altered\n", encoding="utf-8")
        data["review_id"] = "review-1"
        code, _out, err = judge_reviews(self, token, run_dir, data)
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
            code, _out, err = self.run_cli_in_repo(["judge-reviews", "--file", str(path), "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("was stored but event publication failed", err)
        self.assertEqual(
            len(state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]),
            1,
        )
        self.assertEqual(judge_reviews(self, token, run_dir, data)[0], 0)
        events = [event for event in state_mod.read_events(run_dir) if event["kind"] == "review-judgment"]
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
                    "order": ["review-2", "review-3"],
                    "close": [],
                    "reason": "PM cannot compare these reports.",
                }
                code, _out, err = judge_reviews(self, token, run_dir, data)
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
        events = [event for event in state_mod.read_events(run_dir) if event["kind"] == "review-judgment"]
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

        with mock.patch.object(judgments, "record_judgment", side_effect=record_then_tamper):
            code, _out, err = self.run_cli_in_repo(["judge-reviews", "--file", str(path), "--token", token])
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
        code, _out, err = judge_reviews(self, token, run_dir, data)
        self.assertEqual(code, 2)
        self.assertIn(str(run_dir / "events.jsonl"), err)
        self.assertIn("resolve the publication error, then retry the same input", err)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]
        self.assertEqual(len(stored), 1)
        self.assertEqual((run_dir / "events.jsonl").read_text(encoding="utf-8"), "{")

    def test_invalid_score_does_not_change_signed_state(self) -> None:
        _state, token, run_dir = self._run_with_reviews()
        before = (run_dir / "run.json").read_bytes()
        code, _out, err = judge_reviews(
            self,
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
        self.assertEqual(judge_reviews(self, token, run_dir, first)[0], 0)
        self.assertEqual(judge_reviews(self, token, run_dir, second)[0], 0)
        stored = state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]
        self.assertEqual([item["judgment_id"] for item in stored], ["judgment-1", "judgment-2"])
        self.assertEqual(stored[1]["supersedes"], "judgment-1")


class TestDeveloperJudgmentFields(_JudgmentFixtures):
    def _score(self, **overrides) -> dict:
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "origin_event_index": 0,
            "score": 1,
            "criteria_met": 1,
            "defects": {"P0": 0, "P1": 1, "P2": 2, "P3": 3},
            "reason": "PM reran validation and confirmed one P1 and five lesser defects.",
        }
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not _MISSING}

    def _assert_refused(self, token: str, run_dir: Path, data: dict, fragment: str) -> None:
        before = (run_dir / "run.json").read_bytes()
        code, _out, err = judge_developer(self, token, run_dir, data)
        self.assertEqual(code, 2, err)
        self.assertIn(fragment, err)
        self.assertEqual((run_dir / "run.json").read_bytes(), before)

    def test_score_stores_criteria_met_and_defects_verbatim(self) -> None:
        token, run_dir = self._run_with_developer()
        data = self._score()
        code, _out, err = judge_developer(self, token, run_dir, data)
        self.assertEqual(code, 0, err)
        state = state_mod.load_state(run_dir, token)
        [stored] = state["slices"][0]["developer_judgments"]
        self.assertEqual(stored["criteria_met"], 1)
        self.assertEqual(stored["defects"], {"P0": 0, "P1": 1, "P2": 2, "P3": 3})
        report = state_mod.render_run_report(state, state_mod.read_events(run_dir), run_dir)
        self.assertIn("score 1, criteria 1/1, defects P0 0 P1 1 P2 2 P3 3", report)
        # A value the report cannot find renders as `?`, never a crash.
        del state["slices"][0]["criteria_total"]
        report = state_mod.render_run_report(state, state_mod.read_events(run_dir), run_dir)
        self.assertIn("score 1, criteria 1/?, defects P0 0", report)

    def test_score_refuses_missing_or_out_of_range_criteria_met(self) -> None:
        cases = {
            "missing": (_MISSING, "criteria_met must be a non-negative integer"),
            "negative": (-1, "criteria_met must be a non-negative integer"),
            "boolean": (True, "criteria_met must be a non-negative integer"),
            "float": (1.0, "criteria_met must be a non-negative integer"),
            "above total": (2, "exceeds Slice 1 criteria_total 1"),
        }
        for name, (value, fragment) in cases.items():
            with self.subTest(case=name):
                token, run_dir = self._run_with_developer()
                self._assert_refused(token, run_dir, self._score(criteria_met=value), fragment)

    def test_score_refuses_missing_or_malformed_defects(self) -> None:
        full = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
        cases = {
            "missing": (_MISSING, "defects must be an object"),
            "not an object": ([0, 0, 0, 0], "defects must be an object"),
            "missing key": ({"P0": 0, "P1": 0, "P2": 0}, "exactly the keys P0, P1, P2, P3 (got P0, P1, P2)"),
            "extra key": ({**full, "P4": 0}, "exactly the keys P0, P1, P2, P3 (got P0, P1, P2, P3, P4)"),
            "negative": ({**full, "P2": -1}, "P2 must be a non-negative integer"),
            "boolean": ({**full, "P0": True}, "P0 must be a non-negative integer"),
        }
        for name, (value, fragment) in cases.items():
            with self.subTest(case=name):
                token, run_dir = self._run_with_developer()
                self._assert_refused(token, run_dir, self._score(defects=value), fragment)

    def test_unavailable_record_refuses_criteria_met_and_defects(self) -> None:
        for field, value in (
            ("criteria_met", 1),
            ("defects", {"P0": 0, "P1": 0, "P2": 0, "P3": 0}),
        ):
            with self.subTest(field=field):
                token, run_dir = self._run_with_developer()
                data = self._score(status="unavailable", score=_MISSING)
                data.pop("criteria_met")
                data.pop("defects")
                data[field] = value
                self._assert_refused(token, run_dir, data, f"unsupported fields: {field}")

    def test_entry_lacking_criteria_total_is_a_named_error(self) -> None:
        token, run_dir = self._run_with_developer()
        state = state_mod.load_state(run_dir, token)
        del state["slices"][0]["criteria_total"]
        state_mod.save_state(run_dir, state, token)
        self._assert_refused(token, run_dir, self._score(), "has no valid criteria_total in signed state")

    def test_changed_defects_is_not_an_exact_retry(self) -> None:
        token, run_dir = self._run_with_developer()
        data = self._score()
        self.assertEqual(judge_developer(self, token, run_dir, data)[0], 0)
        code, out, err = judge_developer(self, token, run_dir, data)
        self.assertEqual(code, 0, err)
        self.assertIn("already recorded", out)
        changed = self._score(defects={"P0": 1, "P1": 1, "P2": 2, "P3": 3})
        self._assert_refused(token, run_dir, changed, "already has an active judgment")
        corrected = {**changed, "supersedes": "developer-judgment-1"}
        self.assertEqual(judge_developer(self, token, run_dir, corrected)[0], 0)


class TestPanelOrderInput(_JudgmentFixtures):
    def _order(self, **overrides) -> dict:
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "order": ["review-2", "review-3", "review-4"],
            "close": ["review-3"],
            "reason": "Review 2 found the verified defect; 3 was too close to call against 2.",
        }
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not _MISSING}

    def _assert_refused(self, token: str, run_dir: Path, data: dict, fragment: str) -> None:
        before = (run_dir / "run.json").read_bytes()
        code, _out, err = judge_reviews(self, token, run_dir, data)
        self.assertEqual(code, 2, err)
        self.assertIn(fragment, err)
        self.assertEqual((run_dir / "run.json").read_bytes(), before)

    def test_accepts_order_with_close_inside_positions_after_the_first(self) -> None:
        for close in ([], ["review-3"], ["review-3", "review-4"]):
            with self.subTest(close=close):
                _state, token, run_dir = self._run_with_reviews(count=4)
                code, _out, err = judge_reviews(self, token, run_dir, self._order(close=list(reversed(close))))
                self.assertEqual(code, 0, err)
                [stored] = state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]
                self.assertEqual(stored["order"], ["review-2", "review-3", "review-4"])
                self.assertEqual(stored["close"], close)

    def test_refuses_malformed_orders(self) -> None:
        cases = {
            "rank_groups": (
                {"order": _MISSING, "close": _MISSING, "rank_groups": [["review-2"]]},
                "rank_groups is no longer supported",
            ),
            "singleton order": ({"order": ["review-2"], "close": []}, "at least two"),
            "duplicate": (
                {"order": ["review-2", "review-3", "review-2"]},
                "duplicate review ID",
            ),
            "close not in order": ({"close": ["review-5"]}, "review-5 is not in order"),
            "close at position 0": ({"close": ["review-2"]}, "review-2 is first in order"),
            "duplicate close": (
                {"close": ["review-3", "review-3"]},
                "close contains a duplicate",
            ),
            "close missing": ({"close": _MISSING}, "close must be a list"),
            "close not a list": ({"close": "review-3"}, "close must be a list"),
            "unknown id": ({"order": ["review-2", "review-9"], "close": []}, "unknown code-review review ID"),
        }
        for name, (overrides, fragment) in cases.items():
            with self.subTest(case=name):
                _state, token, run_dir = self._run_with_reviews(count=4)
                self._assert_refused(token, run_dir, self._order(**overrides), fragment)

    def test_identical_resubmission_is_a_retry_but_reversal_or_close_change_conflicts(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=4)
        data = self._order()
        self.assertEqual(judge_reviews(self, token, run_dir, data)[0], 0)
        code, out, err = judge_reviews(self, token, run_dir, data)
        self.assertEqual(code, 0, err)
        self.assertIn("already recorded", out)
        reversed_order = self._order(order=["review-4", "review-3", "review-2"], close=[])
        self._assert_refused(token, run_dir, reversed_order, "review IDs already have an active judgment")
        self._assert_refused(
            token,
            run_dir,
            self._order(close=["review-4"]),
            "review IDs already have an active judgment",
        )
        self.assertEqual(len(state_mod.load_state(run_dir, token)["slices"][0]["review_judgments"]), 1)

    def test_unavailable_record_requires_assessment_for_both_skills(self) -> None:
        cases = {
            "drift-audit": ["review-1"],
            "code-review": ["review-2", "review-3"],
        }
        for skill, ids in cases.items():
            with self.subTest(skill=skill):
                _state, token, run_dir = self._run_with_reviews(count=3)
                data = {
                    "schema_version": 1,
                    "slice": "Slice 1",
                    "skill": skill,
                    "status": "unavailable",
                    "review_ids": ids,
                    "reason": "PM could not assess this report.",
                }
                self._assert_refused(token, run_dir, data, "must state assessment 'rating' or 'comparison'")
                code, _out, err = judge_reviews(
                    self,
                    token,
                    run_dir,
                    {**data, "assessment": "rating" if skill == "drift-audit" else "comparison"},
                )
                self.assertEqual(code, 0, err)

    def test_unavailable_comparison_refuses_a_single_review_id(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=3)
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "assessment": "comparison",
            "status": "unavailable",
            "review_ids": ["review-2"],
            "reason": "PM could not compare this report.",
        }
        self._assert_refused(token, run_dir, data, "an unavailable comparison must reference at least two review IDs")

    def test_order_supersession_replaces_active_coverage(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=4)
        self.assertEqual(judge_reviews(self, token, run_dir, self._order())[0], 0)
        replacement = self._order(
            order=["review-3", "review-2", "review-4"],
            close=[],
            reason="PM re-read the reports; review 3 had the stronger evidence.",
            supersedes="judgment-1",
        )
        self.assertEqual(judge_reviews(self, token, run_dir, replacement)[0], 0)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(judgments.unranked_code_review_ids(state), [])
        report = state_mod.render_run_report(state, state_mod.read_events(run_dir), run_dir)
        self.assertIn("judgment-1: superseded code panel", report)


class TestStoredJudgmentAssessmentIsStrict(unittest.TestCase):
    """The report renderer reads each stored reviewer judgment's kind through
    `judgments.assessment_of`; a record without one is malformed, not legacy."""

    def test_record_without_assessment_is_refused_by_name(self) -> None:
        self.assertEqual(judgments.assessment_of({"assessment": "rating"}), "rating")
        with self.assertRaisesRegex(PmError, "judgment-3"):
            judgments.assessment_of({"judgment_id": "judgment-3", "skill": "drift-audit"})

    def test_unhashable_assessment_is_refused_by_name(self) -> None:
        with self.assertRaisesRegex(PmError, "judgment-4"):
            judgments.assessment_of({"judgment_id": "judgment-4", "assessment": ["rating"]})


class TestPanelGroups(_JudgmentFixtures):
    def _entry(self, token: str, run_dir: Path) -> dict:
        return state_mod.load_state(run_dir, token)["slices"][0]

    def _rate_unavailable(self, token: str, run_dir: Path, review_id: str) -> None:
        data = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "assessment": "rating",
            "status": "unavailable",
            "review_ids": [review_id],
            "reason": "PM could not assess this report.",
        }
        self.assertEqual(judge_reviews(self, token, run_dir, data)[0], 0)

    def test_groups_hold_intact_same_context_reports_of_one_origin(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=7)
        state = state_mod.load_state(run_dir, token)
        reviews = state["slices"][0]["reviews"]
        reviews[4]["review_context"]["pm_adjudications"] = "different"  # review-5
        reviews[5]["origin_event"] = {"index": 3, "kind": "steer", "slice": "Slice 1"}  # review-6
        Path(reviews[6]["artifact"]).write_text("altered\n", encoding="utf-8")  # review-7
        state_mod.save_state(run_dir, state, token)
        entry = self._entry(token, run_dir)
        # review-1 is drift-audit; 5 differs in context; 6 is another origin; 7 is altered.
        self.assertEqual(judgments.panel_groups(entry, 0), [["review-2", "review-3", "review-4"]])
        self.assertEqual(judgments.panel_groups(entry, 3), [])
        self.assertEqual(
            judgments.unranked_code_review_ids(state_mod.load_state(run_dir, token)),
            [("Slice 1", "review-2"), ("Slice 1", "review-3"), ("Slice 1", "review-4")],
        )

    def test_unavailable_rating_removes_a_member_and_a_lone_report_is_never_listed(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=4)
        self._rate_unavailable(token, run_dir, "review-4")
        self.assertEqual(judgments.panel_groups(self._entry(token, run_dir), 0), [["review-2", "review-3"]])
        self._rate_unavailable(token, run_dir, "review-3")
        self.assertEqual(judgments.panel_groups(self._entry(token, run_dir), 0), [])
        self.assertEqual(judgments.unranked_code_review_ids(state_mod.load_state(run_dir, token)), [])

    def test_order_covers_a_panel_by_inclusion_and_unavailable_comparison_covers_too(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=4)
        order = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "order": ["review-2", "review-3"],
            "close": [],
            "reason": "Review 2 was the stronger report.",
        }
        self.assertEqual(judge_reviews(self, token, run_dir, order)[0], 0)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(
            judgments.unranked_code_review_ids(state),
            [("Slice 1", "review-2"), ("Slice 1", "review-3"), ("Slice 1", "review-4")],
        )
        full = {**order, "order": ["review-2", "review-3", "review-4"], "supersedes": "judgment-1"}
        self.assertEqual(judge_reviews(self, token, run_dir, full)[0], 0)
        self.assertEqual(judgments.unranked_code_review_ids(state_mod.load_state(run_dir, token)), [])
        self._rate_unavailable(token, run_dir, "review-4")
        self.assertEqual(judgments.unranked_code_review_ids(state_mod.load_state(run_dir, token)), [])

        _state, token, run_dir = self._run_with_reviews(count=3)
        unavailable = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "assessment": "comparison",
            "status": "unavailable",
            "review_ids": ["review-2", "review-3"],
            "reason": "Instructions differed.",
        }
        self.assertEqual(len(judgments.unranked_code_review_ids(state_mod.load_state(run_dir, token))), 2)
        self.assertEqual(judge_reviews(self, token, run_dir, unavailable)[0], 0)
        self.assertEqual(judgments.unranked_code_review_ids(state_mod.load_state(run_dir, token)), [])

    def test_status_names_code_panels_without_an_order(self) -> None:
        _state, token, run_dir = self._run_with_reviews(count=3)
        code, out, err = self.run_cli_in_repo(["status", "--run", run_dir.name, "--token", token])
        self.assertEqual(code, 0, err)
        self.assertIn("code panels without an order: Slice 1/review-2, Slice 1/review-3", out)
        self.assertNotIn("singleton", out)


if __name__ == "__main__":
    import unittest

    unittest.main()

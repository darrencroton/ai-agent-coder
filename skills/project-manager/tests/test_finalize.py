"""Protected behaviours: the acceptance-bearing `finalize` decision paths, the
risk ratchet, controller-owned notes, and report regeneration.

Same conventions as `test_slice_ops.py`: `pm_lib.cli.main` in-process via
`run_cli_in_repo`, and a fake-harness `sh` script for tmux-gated scenarios.

Two rules shape most of the module. Acceptance is gated but never inferred:
the floor must pass, an elevated slice needs both mandatory reviews fresh
against the current HEAD, and any tree change stales them. And the risk
ratchet only ever raises — `--risk standard` is refused outright, and a
raise persists on the slice entry even when the `--accept` it accompanied
was refused.
"""

from __future__ import annotations

import json
import os
import re
import contextlib
import io
import shutil
import tempfile
from unittest import mock
import subprocess
import sys
import time
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import (
    PmTestCase,
    TmuxRunTestCase,
    commit_and_result_script,
    idle_script,
    parse_init_output,
    result_heredoc,
    stdin_draining_idle_script,
    trigger_gated_credential_prompt_script,
    write_fake_harness,
)

from pm_lib import PmError
from pm_lib import cli
from pm_lib import TypedNotSubmitted
from pm_lib import sessions
from pm_lib import slice_ops
from pm_lib import state as state_mod

_HAS_TMUX = shutil.which("tmux") is not None
_PM_PY = Path(__file__).resolve().parents[1] / "scripts" / "pm.py"

# Written to satisfy the acceptance contract it stands in for, pane sentence
# included (SKILL.md "Reading the pane"): a fixture that models a reasoning
# missing the only required trace of PM's pane read teaches the wrong shape to
# every maintainer who copies it.
_LONG_REASONING = (
    "This slice's diff matches the intended change exactly, validation.md shows the "
    "test suite passing, no deviations from the plan were observed, and the pane tail "
    "was clear of dialogs and usage messages."
)


# --- fake harness / reviewer script builders ----------------------------------
#
# The shared builders (readiness, commit-and-result, idle, stdin-draining,
# trigger-gated credential prompt) live in pm_test_helpers. Only the
# steer-specific harnesses and the reviewer fakes are local to this module.


def _steer_then_complete_script(repo: Path, *, authorized_file: str = "a.py") -> str:
    """Blocks reading stdin until the `finalize --steer` pointer actually
    arrives (its stable "PM correction" marker), then commits
    the authorized change and writes result.json. Completion is thus gated on
    the steer, not raced against a fixed sleep: since a steer now rotates the
    pre-steer result.json into attempt-<n>/, a harness that finished
    *before* the steer would leave no result for finalize to find. The launch
    pointer and any earlier lines are read and ignored until the marker."""
    lines = [
        "echo FAKE_HARNESS_READY",
        "while IFS= read -r line; do",
        '  case "$line" in',
        '    *"PM correction"*) break ;;',
        "  esac",
        "done",
        f'echo "authorized change" >> "{repo}/{authorized_file}"',
        f'git -C "{repo}" add "{authorized_file}"',
        f'git -C "{repo}" commit -q -m "slice work"',
        result_heredoc(),
        "sleep 2",
    ]
    return "\n".join(lines)


def _result_then_drain_script() -> str:
    """Writes result.json immediately, then drains stdin and stays alive, so
    a steer can be delivered while a now-stale completion signal already
    exists on disk (the pre-steer result must be rotated
    aside so observe --wait can't mistake it for the steered attempt's)."""
    return "echo FAKE_HARNESS_READY\n" + result_heredoc() + "\nexec cat -"


def _fake_reviewer_ok(path: Path, marker: str) -> Path:
    return write_fake_harness(path, f'echo "FAKE REVIEW OK: {marker}"\nexit 0')


def _fake_reviewer_sleep(path: Path, seconds: int = 300) -> Path:
    return write_fake_harness(path, f"sleep {seconds}")


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# --- shared base ---------------------------------------------------------------


class FinalizeTestCase(TmuxRunTestCase):
    """`TmuxRunTestCase` plus a reviewer-subprocess reaper.

    Only the extra cleanup is local: this module launches real `review`
    subprocesses, which the shared base has no reason to know about.
    """

    def setUp(self) -> None:
        super().setUp()
        self._subprocesses_to_reap: list[subprocess.Popen] = []
        self.addCleanup(self._reap_subprocesses)

    def _reap_subprocesses(self) -> None:
        for proc in self._subprocesses_to_reap:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass


# --- full end-to-end acceptance ----------------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestFullAcceptance(FinalizeTestCase):
    def test_accept_writes_assessment_clears_slice_and_regenerates_report(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self.assertIn("launched", out)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        # `observe --wait` is how an operator actually reaches this point, and
        # it must report the completed slice: it returns as soon as
        # result.json exists, so with the result already on disk this is a
        # cheap assertion on the real command rather than another timed wait.
        code, out, _err = self.run_cli_in_repo(["observe", "--wait", "20"])
        self.assertEqual(code, 0)
        self.assertIn("result present: True", out)

        # Bare `finalize` reports the floor and changes nothing. Two other
        # tests drove their own full launch to assert exactly this; the
        # numbered facts, evidence pointers, and state-neutrality checks are
        # asserted here instead, on the prologue this test already pays for.
        before_bytes = (run_dir / "run.json").read_bytes()
        code, out, _err = self.run_cli_in_repo(["finalize", "--token", token])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count(" PASS "), 7)
        for number in range(1, 8):
            self.assertRegex(out, re.compile(rf"^{number} \S+ PASS", re.MULTILINE))
        self.assertIn("evidence: diff=", out)
        self.assertIn("evidence: result=", out)
        # The pane is always accounted for, never silently omitted: PM's read
        # of it is what stands where the retired eighth fact stood. Here the
        # honest answer is that there is nothing to read — this harness commits
        # and exits before anything observes it, so no live capture was ever
        # taken — and saying so beats printing "0 of 0 lines", which reads like
        # a clear pane. `_print_pane_tail` is unit-tested separately for the
        # case where a capture does exist.
        self.assertIn("pane", out)
        self.assertIn("pane.txt", out)
        self.assertIn("is empty", out)
        self.assertNotIn("pane tail (", out)

        after = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        before = json.loads(before_bytes.decode("utf-8"))
        after.pop("updated_at")
        before.pop("updated_at")
        self.assertEqual(after, before, "bare finalize must not mutate run state")

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 0, err)
        self.assertIn("ACCEPTED", out)

        head = self._git("rev-parse", "HEAD").stdout.strip()
        state = state_mod.load_state(run_dir, token)
        entry = state["slices"][0]
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["commit"], head)
        self.assertIsNone(state["current_slice"])
        # Accepting the LAST undecided slice completes the run there and then —
        # a different production branch from `start_slice` finding nothing left
        # to do, so it is asserted on the accept itself, not on the
        # `start-slice` call at the end of this test.
        self.assertEqual(state["status"], "complete")
        self.assertIsNone(state["stop_reason"])
        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "complete" for event in events))

        assessment_path = Path(entry["assessment"])
        self.assertTrue(str(assessment_path).startswith(str(run_dir)))
        assessment_text = assessment_path.read_text(encoding="utf-8")
        self.assertIn(_LONG_REASONING, assessment_text)
        self.assertIn("PM assessment only (standard risk)", assessment_text)
        self.assertEqual(assessment_text.count(": PASS"), 7)

        mirror_path = self.repo / ".pm" / "runs" / run_id / "slices" / "slice-001" / "assessment.md"
        self.assertTrue(mirror_path.is_file())
        self.assertEqual(mirror_path.read_text(encoding="utf-8"), assessment_text)

        report_path = run_dir / "run-report.md"
        self.assertTrue(report_path.is_file())
        report_text = report_path.read_text(encoding="utf-8")
        self.assertIn(_LONG_REASONING, report_text)
        # The report must render the run as complete, not merely exist — this
        # is the human-facing statement that the run finished.
        self.assertIn("complete", report_text)
        # No `pm rate` was recorded on this run: the section still renders,
        # naming the gap rather than omitting it silently.
        self.assertIn("## Harness/Model Performance", report_text)
        self.assertIn("(not recorded)", report_text)
        report_mirror = self.repo / ".pm" / "runs" / run_id / "run-report.md"
        self.assertTrue(report_mirror.is_file())

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self.assertIn("all slices complete", out)


# --- floor failure refuses acceptance ----------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestAcceptRefusedOnFloorFailure(FinalizeTestCase):
    def test_unauthorized_file_refuses_accept(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh",
            commit_and_result_script(self.repo, unauthorized_file="b.py", delay=1.0, tail_sleep=2.0),
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 1, out + err)

        state = state_mod.load_state(run_dir, token)
        entry = state["slices"][0]
        self.assertIsNone(entry.get("status"))
        self.assertIsNotNone(state["current_slice"])


# --- reasoning too short -----------------------------------------------------


class TestAcceptRefusedOnShortReasoning(PmTestCase):
    def test_reasoning_under_forty_chars_raises_before_touching_state(self) -> None:
        plan_path = self.write_plan(slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        before_bytes = (run_dir / "run.json").read_bytes()

        code, _out, err = self.run_cli_in_repo(["finalize", "--accept", "too short", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("40", err)

        self.assertEqual((run_dir / "run.json").read_bytes(), before_bytes)

    def test_an_empty_decision_is_refused_rather_than_silently_dropped(self) -> None:
        """`--accept ""`/`--steer ""`/`--stop ""` were tested by truthiness, so
        an empty decision fell through to the bare evidence-only finalize: it
        printed a floor report, exited 0, and recorded no decision at all —
        indistinguishable, to a PM reading the output, from a run where its
        acceptance was recorded. Each must now fail closed and say so."""
        plan_path = self.write_plan(slices=[{"files": ["a.py"]}])
        _state, token, run_dir = self.make_run(plan_path=plan_path)
        before_bytes = (run_dir / "run.json").read_bytes()

        for flag in ("--accept", "--steer", "--stop"):
            with self.subTest(flag=flag):
                code, out, err = self.run_cli_in_repo(["finalize", flag, "", "--token", token])
                self.assertEqual(code, 2, out)
                self.assertIn(flag, err)
                self.assertNotIn("PASS", out, "an empty decision must not degrade to a floor dump")
                self.assertEqual((run_dir / "run.json").read_bytes(), before_bytes)


# --- elevated slice review requirement + staleness ---------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestElevatedReviewFreshness(FinalizeTestCase):
    def test_missing_then_stale_then_fresh_reviews(self) -> None:
        plan_path = self.write_plan(
            self._plan_path(), slices=[{"files": ["a.py"], "risky": "touches auth"}]
        )
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        # Missing both reviews.
        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 1, out + err)
        self.assertIn("drift-audit", out + err)
        self.assertIn("code-review", out + err)

        fake_drift = _fake_reviewer_ok(self.repo.parent / "fake_drift.sh", "drift-1")
        fake_code = _fake_reviewer_ok(self.repo.parent / "fake_code.sh", "code-1")
        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "drift-audit", "--tool", "t1",
             "--reviewer-command", str(fake_drift), "--token", token]
        )
        self.assertEqual(code, 0, err)
        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "code-review", "--tool", "t1",
             "--reviewer-command", str(fake_code), "--token", token]
        )
        self.assertEqual(code, 0, err)

        # Staleness: another commit lands after both reviews were recorded.
        (self.repo / "a.py").write_text("more authorized change\n", encoding="utf-8")
        self._git("add", "a.py")
        self._git("commit", "-q", "-m", "more slice work")

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 1, out + err)
        self.assertIn("drift-audit", out + err)
        self.assertIn("code-review", out + err)

        fake_drift2 = _fake_reviewer_ok(self.repo.parent / "fake_drift2.sh", "drift-2")
        fake_code2 = _fake_reviewer_ok(self.repo.parent / "fake_code2.sh", "code-2")
        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "drift-audit", "--tool", "t1",
             "--reviewer-command", str(fake_drift2), "--token", token]
        )
        self.assertEqual(code, 0, err)
        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "code-review", "--tool", "t1",
             "--reviewer-command", str(fake_code2), "--token", token]
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 0, out + err)
        self.assertIn("ACCEPTED", out)

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["slices"][0]["status"], "accepted")


# --- risk ratchet ------------------------------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestRiskRatchet(FinalizeTestCase):
    def test_ratchet_arms_review_requirement_rejects_lowering_and_persists(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["slices"][0]["risk"], "standard")

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, err = self.run_cli_in_repo(
            ["finalize", "--risk", "elevated", "--accept", _LONG_REASONING, "--token", token]
        )
        self.assertEqual(code, 1, out + err)
        self.assertIn("drift-audit", out + err)
        self.assertIn("code-review", out + err)

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["slices"][0]["risk"], "elevated")
        self.assertEqual(state["slices"][0]["plan_risk"], "standard")

        code, _out, err = self.run_cli_in_repo(["finalize", "--risk", "standard", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("can only be raised", err)


# --- surface grants ratchet acceptance requirements --------------------------


_LONG_GRANT_EVIDENCE = "Repository investigation confirms this path must change to satisfy the slice's contract."


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestGrantRatchetsAcceptance(FinalizeTestCase):
    def test_accept_refused_after_grant_for_missing_mandatory_reviews(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "b.py", "--evidence", _LONG_GRANT_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 0, out + err)

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 1, out + err)
        self.assertIn("drift-audit", out + err)
        self.assertIn("code-review", out + err)

        state = state_mod.load_state(run_dir, token)
        entry = state["slices"][0]
        self.assertEqual(entry["risk"], "elevated")
        self.assertIsNone(entry.get("status"))

    def test_accepted_assessment_contains_surface_grants_block(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "b.py", "--evidence", _LONG_GRANT_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 0, out + err)

        fake_drift = _fake_reviewer_ok(self.repo.parent / "fake_drift.sh", "drift-1")
        fake_code = _fake_reviewer_ok(self.repo.parent / "fake_code.sh", "code-1")
        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "drift-audit", "--tool", "t1",
             "--reviewer-command", str(fake_drift), "--token", token]
        )
        self.assertEqual(code, 0, err)
        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "code-review", "--tool", "t1",
             "--reviewer-command", str(fake_code), "--token", token]
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 0, out + err)

        state = state_mod.load_state(run_dir, token)
        assessment_text = Path(state["slices"][0]["assessment"]).read_text(encoding="utf-8")
        self.assertIn("## Surface grants", assessment_text)
        grant_at = state["slices"][0]["grants"][0]["at"]
        self.assertIn(f"b.py — granted {grant_at}: {_LONG_GRANT_EVIDENCE}", assessment_text)

    def test_accept_refused_for_a_review_commissioned_before_a_grant_but_recorded_after_it(self) -> None:
        """A reviewer already running when a grant lands finishes AFTER it, so
        its completion stamp postdates the grant while its prompt never showed
        it — the fail-open a completion-time comparison allowed. Freshness
        therefore compares `grants_seen` (what the prompt was rendered from)
        against the slice's current grant count, so the later stamp cannot
        launder a review that saw nothing.

        The review record is written directly, because reproducing the true
        interleaving would mean racing a live subprocess against a grant: what
        the gate must key on is the recorded `grants_seen`, not the timing that
        produced it."""
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "b.py", "--evidence", _LONG_GRANT_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 0, out + err)

        # Both mandatory reviews recorded AFTER the grant (a strictly later
        # `at`), but each commissioned before it, so each saw zero grants.
        head = self._git("rev-parse", "HEAD").stdout.strip()
        report = self.repo.parent / "in-flight-review.md"
        report.write_text("IN-FLIGHT REVIEW REPORT\n", encoding="utf-8")
        state = state_mod.load_state(run_dir, token)
        entry = state["slices"][0]
        entry["reviews"] = [
            {
                "skill": skill, "tool": "t1", "model": None, "head": head,
                "before_head": state["current_slice"]["before_head"],
                "artifact": str(report), "sha256": slice_ops.sha256_file(report),
                "at": "2099-01-01T00:00:00Z", "grants_seen": 0,
            }
            for skill in ("drift-audit", "code-review")
        ]
        state_mod.save_state(run_dir, state, token)

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 1, out + err)
        self.assertIn("drift-audit", out + err)
        self.assertIn("code-review", out + err)
        self.assertIsNone(state_mod.load_state(run_dir, token)["slices"][0].get("status"))

    def test_accept_refused_when_a_grant_follows_both_fresh_reviews_at_the_same_head(self) -> None:
        """The P1 regression two independent reviewers converged on: freshness
        keyed only on head + artifact + sha256, none of which a grant touches.
        So a developer could commit an authorized AND an unauthorized file at
        HEAD X, PM could commission both mandatory reviews (recorded fresh at
        HEAD X, each showing no grants), then grant the unauthorized path
        after the fact — and the two reviews, never having seen the widened
        authorization, stayed 'fresh' straight through to acceptance. A grant
        must stale a review exactly as a tree change does, so this refuses
        exactly as a missing-reviews acceptance would, and the slice must NOT
        be accepted."""
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        # Both mandatory reviews recorded fresh at the current HEAD, BEFORE
        # any grant — exactly the state the pre-fix code considered good
        # enough to accept, with each review's prompt showing no PM grants.
        fake_drift = _fake_reviewer_ok(self.repo.parent / "fake_drift.sh", "drift-1")
        fake_code = _fake_reviewer_ok(self.repo.parent / "fake_code.sh", "code-1")
        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "drift-audit", "--tool", "t1",
             "--reviewer-command", str(fake_drift), "--token", token]
        )
        self.assertEqual(code, 0, err)
        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "code-review", "--tool", "t1",
             "--reviewer-command", str(fake_code), "--token", token]
        )
        self.assertEqual(code, 0, err)

        # A grant recorded AFTER both reviews, at the SAME HEAD (no further
        # commit): head, artifact, and sha256 are all untouched by it, so
        # only grant-staling — not any tree-change check — can catch this.
        code, out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "b.py", "--evidence", _LONG_GRANT_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 0, out + err)

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 1, out + err)
        self.assertIn("drift-audit", out + err)
        self.assertIn("code-review", out + err)

        state = state_mod.load_state(run_dir, token)
        entry = state["slices"][0]
        self.assertIsNone(entry.get("status"))
        self.assertIsNotNone(state["current_slice"])


# --- steer -------------------------------------------------------------------


class TestSteerTypedStateCleanup(PmTestCase):
    def test_typed_not_submitted_preserves_the_correction_file(self) -> None:
        plan_path = self.write_plan(self.repo.parent / "plan.md", slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        artifact_dir = run_dir / "slices" / "slice-001"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.set_current_slice(
            state,
            token,
            run_dir,
            slice_id="Slice 1",
            before_head=None,
            artifact_dir=artifact_dir,
            tmux_session="pm-mocked",
            attempts=0,
        )

        with mock.patch.object(slice_ops.sessions, "session_exists", return_value=True), \
             mock.patch.object(
                 slice_ops.sessions,
                 "send_line",
                 side_effect=TypedNotSubmitted("typed, not sent"),
             ):
            with self.assertRaises(TypedNotSubmitted):
                slice_ops.finalize_steer(self.repo, run_dir, token, correction="fix the thing")

        self.assertTrue(
            (artifact_dir / "steer-attempt-1.md").is_file(),
            "a correction whose pointer is typed in the pane must not be deleted",
        )


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestSteer(FinalizeTestCase):
    def test_steer_files_the_correction_sends_a_pointer_and_exhausts_budget(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", stdin_draining_idle_script())
        code, out, _err = self._init(plan_path, harness, extra=["--max-attempts", "1"])
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

        # Leading/trailing whitespace is meaningful in a verbatim correction
        # (e.g. an indented code block) and must survive untouched.
        correction = "  Please also update the docstring.\nAnd rerun the tests before committing.  \n"
        code, out, err = self.run_cli_in_repo(["finalize", "--steer", correction, "--token", token])
        self.assertEqual(code, 0, out + err)

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["current_slice"]["attempts"], 1)
        self.assertEqual(state["slices"][0]["attempts"], 1)

        # Filed byte-exact, so the meaningful leading/trailing whitespace
        # above survives, beside the prompt.md the Developer already reads.
        correction_path = Path(state["current_slice"]["artifact_dir"]) / "steer-attempt-1.md"
        self.assertEqual(correction_path.read_text(encoding="utf-8"), correction)
        self.assertIn(str(correction_path), out)

        # Only the pointer reaches the pane, carrying the whole absolute path
        # (a bare basename would be unopenable). Neither correction line is
        # injected, so no harness TUI can split or truncate it.
        self.assertTrue(
            self._wait_for(
                lambda: str(correction_path) in sessions.pane_text(session),
                timeout=10.0,
            )
        )
        pane = sessions.pane_text(session)
        self.assertNotIn("Please also update the docstring.", pane)
        self.assertNotIn("And rerun the tests before committing.", pane)

        # The steer event's note carries the complete correction, not a
        # truncated first line, and deliberately no evidence path: the note is
        # the authoritative record even though a delivery file now exists.
        events = state_mod.read_events(run_dir)
        steer_events = [e for e in events if e["kind"] == "steer"]
        self.assertEqual(len(steer_events), 1)
        self.assertEqual(steer_events[0]["note"], correction)
        self.assertNotIn("evidence", steer_events[0])

        # Budget (max_attempts=1) is now exhausted: the next steer is refused.
        code, _out, err = self.run_cli_in_repo(
            ["finalize", "--steer", "One more nudge.", "--token", token]
        )
        self.assertEqual(code, 2, err)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "needs-human")

    def test_steer_refuses_to_overwrite_an_already_delivered_correction(self) -> None:
        """A send that succeeds while `save_state` fails leaves the attempt
        unpersisted, so a retry recomputes the same number. Rewriting the file
        would hand the Developer words the run never recorded, and the earlier
        unlink-on-refusal would delete a file its pointer already named."""
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", stdin_draining_idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

        # Stand in for that window: attempt 1's file is already on disk.
        state = state_mod.load_state(run_dir, token)
        delivered = Path(state["current_slice"]["artifact_dir"]) / "steer-attempt-1.md"
        delivered.write_text("the correction already delivered", encoding="utf-8")

        code, _out, err = self.run_cli_in_repo(["finalize", "--steer", "different words", "--token", token])
        self.assertEqual(code, 2, err)
        self.assertEqual(delivered.read_text(encoding="utf-8"), "the correction already delivered")
        self.assertEqual(state_mod.load_state(run_dir, token)["current_slice"]["attempts"], 0)

    def test_steer_rotates_stale_pre_steer_result(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _result_then_drain_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))
        artifact_dir = Path(state_mod.load_state(run_dir, token)["current_slice"]["artifact_dir"])

        # Attempt 0 wrote a result.json BEFORE any steer.
        self.assertTrue(self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=10.0))

        code, out, err = self.run_cli_in_repo(["finalize", "--steer", "Remove the dead import.", "--token", token])
        self.assertEqual(code, 0, out + err)

        # The pre-steer completion signal is rotated into attempt-0/ so the
        # steered attempt can't be mistaken for complete on stale evidence.
        # The live session is `exec cat -` and writes no new result, so the
        # top-level result.json is genuinely absent until real work lands.
        self.assertTrue((artifact_dir / "attempt-0" / "result.json").is_file())
        self.assertFalse((artifact_dir / "result.json").is_file())

    def test_steer_refuses_into_visible_dialog_prompt(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        # Trigger-gated: the marker must appear strictly AFTER injection
        # (`send_prompt` refuses into a visible one, which would fail
        # `start-slice` itself), and a fixed post-launch delay to achieve that
        # was racing the launch it was trying to follow.
        trigger = self.repo.parent / "credential_trigger"
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", trigger_gated_credential_prompt_script(trigger)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        trigger.write_text("go\n", encoding="utf-8")
        self.assertTrue(
            self._wait_for(lambda: "Enter API key" in sessions.pane_text(session), timeout=10.0)
        )

        code, _out, err = self.run_cli_in_repo(
            ["finalize", "--steer", "please continue", "--token", token]
        )
        self.assertEqual(code, 2, err)
        self.assertIn("credential_prompt", err)

        # Refused before persisting: no steer event, attempts unchanged, and
        # no correction file left behind to read as one that was delivered.
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["current_slice"]["attempts"], 0)
        self.assertFalse((Path(state["current_slice"]["artifact_dir"]) / "steer-attempt-1.md").exists())
        events = state_mod.read_events(run_dir)
        self.assertFalse([e for e in events if e["kind"] == "steer"])

    def test_pretyping_send_failure_deletes_the_correction_file(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", stdin_draining_idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        artifact_dir = Path(
            state_mod.load_state(run_dir, token)["current_slice"]["artifact_dir"]
        )

        with mock.patch.object(
            slice_ops.sessions, "send_line", side_effect=PmError("never reached the pane")
        ) as never_delivered:
            with self.assertRaises(PmError):
                slice_ops.finalize_steer(self.repo, run_dir, token, correction="fix the other thing")
        self.assertTrue(never_delivered.called, "the plain-PmError branch must reach the send")
        self.assertFalse(
            (artifact_dir / "steer-attempt-1.md").is_file(),
            "an undelivered correction must not be left looking delivered",
        )

    def test_accepted_assessment_retains_full_correction_narrative(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _steer_then_complete_script(self.repo)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)

        correction = "Please rename the helper.\nAlso add a docstring."
        code, out, err = self.run_cli_in_repo(["finalize", "--steer", correction, "--token", token])
        self.assertEqual(code, 0, out + err)

        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 0, out + err)

        state = state_mod.load_state(run_dir, token)
        assessment_text = Path(state["slices"][0]["assessment"]).read_text(encoding="utf-8")
        self.assertIn("Please rename the helper.", assessment_text)
        self.assertIn("Also add a docstring.", assessment_text)

    def test_steer_dead_session_raises_relaunch_error(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))
        sessions.force_stop(session)
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session), timeout=10.0))

        code, _out, err = self.run_cli_in_repo(
            ["finalize", "--steer", "nudge into the void", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("relaunch", err)


# --- stop decision -----------------------------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestStopDecision(FinalizeTestCase):
    def test_stop_writes_stopped_assessment_and_regenerates_report(self) -> None:
        """The stop decision end to end, including the correction narrative.

        The narrative assertions were a separate test that steered and then
        stopped on its own launched session — the same prologue as this one.
        They are not redundant with the accepted-path test, though: acceptance
        and stopping reach `_attempts_summary` through independent call sites,
        so only stopping proves the stop path is wired to it. Merged rather
        than deleted, onto a stdin-draining harness so the steer is read.
        """
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", stdin_draining_idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

        correction = "Try the other approach entirely.\nSee the notes for why."
        code, out, err = self.run_cli_in_repo(["finalize", "--steer", correction, "--token", token])
        self.assertEqual(code, 0, out + err)

        code, out, err = self.run_cli_in_repo(
            ["finalize", "--stop", "giving up on this approach", "--token", token]
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("STOPPED", out)

        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session), timeout=10.0))

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["slices"][0]["status"], "stopped")
        self.assertEqual(state["status"], "needs-human")
        self.assertEqual(state["stop_reason"], "giving up on this approach")

        assessment_path = Path(state["slices"][0]["assessment"])
        text = assessment_path.read_text(encoding="utf-8")
        self.assertIn("STOPPED", text)
        self.assertIn("giving up on this approach", text)
        # The full multi-line correction survives into the stopped assessment,
        # not just its first line.
        self.assertIn("Try the other approach entirely.", text)
        self.assertIn("See the notes for why.", text)

        self.assertTrue((run_dir / "run-report.md").is_file())


# --- notes.md controller-owned + mirror + tripwire ---------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestNotesMirrorAndTripwire(FinalizeTestCase):
    def test_notes_mirrored_at_start_slice_and_large_notes_warn(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script(sleep_seconds=20.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        (run_dir / "notes.md").write_text("decision: use approach B\n", encoding="utf-8")

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertNotIn("WARNING", out)

        mirror = self.repo / ".pm" / "runs" / run_id / "notes.md"
        self.assertTrue(mirror.is_file())
        self.assertEqual(mirror.read_text(encoding="utf-8"), "decision: use approach B\n")

    def test_oversized_notes_prints_tripwire_warning(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script(sleep_seconds=20.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        (run_dir / "notes.md").write_text("x" * (600 * 1024), encoding="utf-8")

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertIn("WARNING", out)
        self.assertIn("512", out)


# `notes` needs no tmux (init only), so it is deliberately not tmux-gated.
class TestNotesCommand(FinalizeTestCase):
    def test_set_then_append_write_authoritative_original_and_mirror(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        original = run_dir / "notes.md"
        mirror = self.repo / ".pm" / "runs" / run_id / "notes.md"

        code, _out, err = self.run_cli_in_repo(["notes", "--set", "decision: approach B", "--token", token])
        self.assertEqual(code, 0, err)
        self.assertEqual(original.read_text(encoding="utf-8"), "decision: approach B\n")
        self.assertEqual(mirror.read_text(encoding="utf-8"), "decision: approach B\n")

        code, _out, err = self.run_cli_in_repo(["notes", "--append", "lesson: watch dedup", "--token", token])
        self.assertEqual(code, 0, err)
        text = original.read_text(encoding="utf-8")
        self.assertIn("decision: approach B", text)
        self.assertIn("lesson: watch dedup", text)
        # The authoritative original carries both; because a later start-slice
        # re-mirror reads from it, appended notes are never clobbered — the
        # footgun of hand-editing only the mirror is gone.
        self.assertEqual(mirror.read_text(encoding="utf-8"), text)

    def test_empty_or_whitespace_text_is_refused(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        code, _out, err = self.run_cli_in_repo(["notes", "--append", "   ", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("non-empty", err.lower())


# `rate` needs no tmux (init only), so it is deliberately not tmux-gated,
# same as `notes` above.
class TestRateCommand(FinalizeTestCase):
    _RATING = (
        "Process discipline: 5/5 — no incidents.\n"
        "Reporting reliability: 5/5 — validation matched every check.\n"
        "Output quality: 4/5 — accepted work correct throughout."
    )

    def test_set_writes_authoritative_original_and_mirror(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        original = run_dir / "model-performance.md"
        mirror = self.repo / ".pm" / "runs" / run_id / "model-performance.md"

        code, _out, err = self.run_cli_in_repo(["rate", "--text", self._RATING, "--token", token])
        self.assertEqual(code, 0, err)
        self.assertEqual(original.read_text(encoding="utf-8"), self._RATING + "\n")
        self.assertEqual(mirror.read_text(encoding="utf-8"), self._RATING + "\n")

        # A second `rate` replaces the whole file — there is nothing to
        # append to a once-per-run rating.
        code, _out, err = self.run_cli_in_repo(["rate", "--text", "Process discipline: 3/5 — revised.", "--token", token])
        self.assertEqual(code, 0, err)
        text = original.read_text(encoding="utf-8")
        self.assertEqual(text, "Process discipline: 3/5 — revised.\n")
        self.assertNotIn("5/5", text)

    def test_empty_or_whitespace_text_is_refused(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        code, _out, err = self.run_cli_in_repo(["rate", "--text", "   ", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("non-empty", err.lower())


# --- report regenerates with .pm/ deleted ------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestReportFromControllerDataAlone(FinalizeTestCase):
    def test_status_report_recreates_mirror_after_pm_deleted(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 0, out + err)

        rating = "Process discipline: 5/5 — no incidents, whole run."
        code, out, err = self.run_cli_in_repo(["rate", "--text", rating, "--token", token])
        self.assertEqual(code, 0, out + err)

        shutil.rmtree(self.repo / ".pm")
        self.assertFalse((self.repo / ".pm").exists())

        code, out, _err = self.run_cli_in_repo(["status", "--report", "--run", run_id])
        self.assertEqual(code, 0, out)

        report_path = run_dir / "run-report.md"
        self.assertTrue(report_path.is_file())
        report_text = report_path.read_text(encoding="utf-8")
        self.assertIn(_LONG_REASONING, report_text)
        # `model-performance.md`'s original lives under the state dir, not
        # `.pm/`, so regeneration must recover it exactly like every other
        # controller-owned original this test proves survives deletion.
        self.assertIn(rating, report_text)

        mirror_path = self.repo / ".pm" / "runs" / run_id / "run-report.md"
        self.assertTrue(mirror_path.is_file())
        self.assertEqual(mirror_path.read_text(encoding="utf-8"), report_text)


# --- stop reaps a hung reviewer ----------------------------------------------


# --- attempt-budget exhaustion is a mandatory stop that closes send, ---------
# --- steer, and accept, leaving only finalize --stop and stop open ----------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestBudgetExhaustionClosesAllPaths(FinalizeTestCase):
    def test_budget_exhaustion_kills_session_and_closes_steer_send_accept(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", stdin_draining_idle_script())
        code, out, _err = self._init(plan_path, harness, extra=["--max-attempts", "0"])
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        # Attempt 0 (the initial launch) is never budget-checked — only a
        # relaunch or a steer counts against the budget — so this succeeds
        # even with max_attempts=0.
        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

        # The steer itself would be attempt 1, over the budget of 0: refused,
        # and the exhaustion is a mandatory stop that force-kills the session.
        code, _out, err = self.run_cli_in_repo(
            ["finalize", "--steer", "fix it please", "--token", token]
        )
        self.assertEqual(code, 2, err)
        self.assertIn("attempt budget exhausted", err)

        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session), timeout=10.0))

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "needs-human")
        self.assertEqual(state["stop_reason"], "attempt budget exhausted")

        code, _out, err = self.run_cli_in_repo(
            ["send", "--text", "hi", "--reason", "nudge", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("attempt budget exhausted", err)

        code, _out, err = self.run_cli_in_repo(
            ["finalize", "--accept", _LONG_REASONING, "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("attempt budget exhausted", err)

        # finalize --stop remains open even after exhaustion — recording the
        # outcome (floor passing or not) is exactly what a mandatory stop
        # still permits.
        code, out, err = self.run_cli_in_repo(
            ["finalize", "--stop", "human should look at this", "--token", token]
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("STOPPED", out)

        state = state_mod.load_state(run_dir, token)
        entry = state["slices"][0]
        self.assertEqual(entry["status"], "stopped")
        assessment_path = Path(entry["assessment"])
        self.assertTrue(assessment_path.is_file())
        self.assertIn("STOPPED", assessment_path.read_text(encoding="utf-8"))


class HungReviewerTestCase(PmTestCase):
    """Shared setup: a reviewer is a detached process group that survives its
    `review` command, so every path ending or replacing a slice must kill it."""

    def _start_hung_reviewer(self) -> tuple[str, Path, subprocess.Popen, int]:
        # plan.md must live OUTSIDE the worktree: an untracked plan.md inside
        # the repo is a dirty-tree entry, and `review` now refuses on a dirty
        # worktree (the pinned-tree guard), which would fail this test before
        # the reviewer subprocess ever launches.
        plan_path = self.write_plan(self.repo.parent / "plan.md", slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        before_head = self._git("rev-parse", "HEAD").stdout.strip()
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        (self.repo / "a.py").write_text("changed\n", encoding="utf-8")
        self._git("add", "a.py")
        self._git("commit", "-q", "-m", "advance head")

        fake_sleep = _fake_reviewer_sleep(self.repo.parent / "fake_sleep_reviewer.sh", seconds=300)

        env = dict(os.environ)
        env["PM_RUN_TOKEN"] = token
        proc = subprocess.Popen(
            [
                sys.executable, str(_PM_PY), "review",
                "--slice", "Slice 1", "--skill", "code-review", "--tool", "sleepy",
                "--reviewer-command", str(fake_sleep),
            ],
            cwd=str(self.repo), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())

        def _reviewer_pgid() -> int | None:
            reloaded = state_mod.load_state(run_dir, token)
            pids = (reloaded.get("current_slice") or {}).get("reviewer_pids") or []
            return pids[0] if pids else None

        found = False
        deadline = time.monotonic() + 15.0
        pgid = None
        while time.monotonic() < deadline:
            pgid = _reviewer_pgid()
            if pgid is not None:
                found = True
                break
            time.sleep(0.2)
        self.assertTrue(found, "reviewer pgid never appeared in state")
        self.assertTrue(_pgid_alive(pgid))
        return token, run_dir, proc, pgid

    def _assert_reaped(self, pgid: int, proc: subprocess.Popen, *, by: str) -> None:
        self.assertTrue(
            self._wait_for(lambda: not _pgid_alive(pgid), timeout=10.0),
            f"reviewer process group survived {by}",
        )
        proc.wait(timeout=10)


class TestStopReapsHungReviewer(HungReviewerTestCase):
    def test_stop_kills_reviewer_process_group(self) -> None:
        token, _run_dir, proc, pgid = self._start_hung_reviewer()
        code, out, err = self.run_cli_in_repo(["stop", "--reason", "reaping test", "--token", token])
        self.assertEqual(code, 0, out + err)
        self._assert_reaped(pgid, proc, by="stop")


class TestAcceptReapsHungReviewer(FinalizeTestCase):
    """Acceptance clears `current_slice`, discarding the recorded pgids. Without
    an explicit kill the reviewer runs on with nothing able to find it again.

    Acceptance needs a passing floor, so this drives the real launch-to-result
    flow and records a genuinely running process group as the reviewer —
    `review` itself is not needed to pin "recorded pgids get killed"."""

    def test_accept_kills_reviewer_process_group(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        pgid = os.getpgid(proc.pid)
        with state_mod.locked_update(run_dir, token) as state:
            state["current_slice"]["reviewer_pids"] = [pgid]
        self.assertTrue(_pgid_alive(pgid))

        code, out, err = self.run_cli_in_repo(
            ["finalize", "--accept", "Diff and validation evidence check out; accepting.", "--token", token]
        )
        self.assertEqual(code, 0, out + err)
        # `proc.poll()` rather than `_pgid_alive`: the sleeper is this test's
        # own child, so a killed one lingers as a zombie until it is waited.
        self.assertTrue(
            self._wait_for(lambda: proc.poll() is not None, timeout=10.0),
            "reviewer process group survived finalize --accept",
        )
        self.assertIsNone(state_mod.load_state(run_dir, token).get("current_slice"))


# --- _attempts_summary: exact multiline formatting, no tmux required --------


class TestPaneTailPrinter(unittest.TestCase):
    """`finalize`'s pane output, on its own: no run, no tmux, no session.

    The acceptance-path tests reach this printer only with an empty capture,
    because a fake harness that commits and exits is never observed alive. The
    branch that matters for the real discipline — a capture exists, so print
    its tail — needs pinning somewhere, and here it costs nothing.
    """

    def _render(self, text: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            pane = Path(tmp) / "pane.txt"
            pane.write_text(text, encoding="utf-8")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli._print_pane_tail(pane)
            return buffer.getvalue()

    def test_a_capture_prints_its_tail_bounded_to_the_last_lines(self) -> None:
        out = self._render("\n".join(f"line-{n}" for n in range(1, 101)) + "\n")
        self.assertIn(f"pane tail ({cli._PANE_TAIL_LINES} of 100 lines", out)
        self.assertIn("line-100", out)
        self.assertIn(f"line-{101 - cli._PANE_TAIL_LINES}", out)

    def test_a_short_capture_prints_whole_and_counts_honestly(self) -> None:
        out = self._render("Enter API key to continue\n")
        self.assertIn("pane tail (1 of 1 lines", out)
        self.assertIn("Enter API key to continue", out)

    def test_a_missing_capture_is_named_as_missing_not_as_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli._print_pane_tail(Path(tmp) / "absent.txt")
        self.assertIn("not captured", buffer.getvalue())


class TestAttemptsSummaryFormatting(unittest.TestCase):
    def test_multiline_steer_note_is_indented_legibly(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            state_mod.append_event(
                run_dir, "steer", slice_id="Slice 1", note="line one\nline two"
            )
            summary = slice_ops._attempts_summary(run_dir, "Slice 1", attempts=1)

            events = state_mod.read_events(run_dir)
            ts = events[0]["ts"]
            expected = "\n".join(
                [
                    "Attempts: 1",
                    "Steer interventions: 1",
                    f"  - {ts}:",
                    "      line one",
                    "      line two",
                ]
            )
            self.assertEqual(summary, expected)

    def test_no_steer_events_omits_interventions_section(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            summary = slice_ops._attempts_summary(run_dir, "Slice 1", attempts=0)
            self.assertEqual(summary, "Attempts: 0")


if __name__ == "__main__":
    unittest.main()

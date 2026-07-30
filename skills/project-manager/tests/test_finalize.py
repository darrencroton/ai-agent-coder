"""Protected behaviours: Stage 4's acceptance-bearing `finalize` decision
paths, the risk ratchet, controller-owned notes, and report regeneration.

Everything here drives `pm_lib.cli.main` in-process via `run_cli_in_repo`,
matching Stage 3's convention; tmux-gated scenarios use a tiny fake-harness
`sh` script exactly like `test_slice_ops.py`. Pins:

1. Full end-to-end acceptance (AC): init -> start-slice (fake dev commits
   the authorized change + result.json) -> bare `finalize` reports 8/8 ->
   `finalize --accept "reasoning"` accepts: the slice entry's commit is
   HEAD, `assessment.md` exists as a controller-owned original (under the
   state dir) and its `.pm/` mirror, both containing the reasoning text
   verbatim, all eight floor lines, and "PM assessment only (standard
   risk)" (no reviews were commissioned on this standard-risk slice);
   `current_slice` is cleared, the tmux session is gone, and
   `run-report.md` is regenerated. A second `start-slice` on this
   single-slice plan reports all slices complete.
2. `--accept` is refused when the floor fails (an unauthorized file
   alongside the authorized commit): nothing is accepted, exit 1.
3. `--accept` is refused when the reasoning is shorter than the 40-
   character minimum, before any state is touched.
4. An elevated slice (plan `Risky surfaces touched:` != none): `--accept`
   is refused naming both missing reviews; after a fake drift-audit and a
   fake code-review are recorded, an additional commit lands (staleness);
   `--accept` is refused again, naming the now-stale reviews, until BOTH
   are re-commissioned against the new HEAD, at which point `--accept`
   succeeds.
5. Risk ratchet: a standard-risk slice with `finalize --risk elevated
   --accept` is refused for missing reviews (proving the ratchet arms the
   review requirement before acceptance is evaluated); `--risk standard`
   is rejected outright with a PmError ("risk can only be raised"); the
   ratchet's effect on the slice entry persists in state even though that
   particular `--accept` call was refused.
6. `--steer`: the full (possibly multiline) correction is written verbatim to
   `steer-attempt-<n>.md` in the slice artifact directory, and the live
   session receives only the reference-sourced one-line pointer naming it;
   the `steer` event's
   `note` carries the complete correction verbatim and the assessment's
   "Attempts / interventions" section renders it legibly; `attempts`
   increments and persists across a fresh state load; exhausting the
   attempt budget refuses the next steer and sets `needs-human`; steering a
   dead session raises PmError directing the operator to relaunch.
7. `--stop`: the slice entry becomes "stopped", `assessment.md` records
   decision STOPPED with the `--stop` reasoning verbatim (even though the
   floor may be failing — that's the point), the run becomes
   `needs-human`, the session is killed, and the report regenerates.
8. Controller-owned `notes.md`: content written into the run's original
   `notes.md` before a launch is mirrored into `.pm/` at `start-slice`; a
   notes file over the 512 KiB cap prints a prominent (non-fatal) warning
   at `start-slice`.
9. Report-from-controller-data (AC): after an acceptance, deleting
   `.pm/` entirely and running `status --report` still exits 0, recreates
   `run-report.md` (original and mirror) from state + events + the
   assessment file under the state dir alone, and the regenerated report
   contains the assessment text.
10. `stop` reaps a hung reviewer: a `review --reviewer-command` fake that
    sleeps in the background is launched as a real subprocess; once its
    process group is recorded in `current_slice.reviewer_pids`, `stop`
    kills that process group (tolerating ESRCH).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import PmTestCase, parse_init_output, write_fake_harness

from pm_lib import sessions
from pm_lib import slice_ops
from pm_lib import state as state_mod

_HAS_TMUX = shutil.which("tmux") is not None
_PM_PY = Path(__file__).resolve().parents[1] / "scripts" / "pm.py"

_LONG_REASONING = (
    "This slice's diff matches the intended change exactly, validation.md shows the "
    "test suite passing, and no deviations from the plan were observed."
)


# --- fake harness / reviewer script builders ----------------------------------


def _result_heredoc(status: str = "done", summary: str = "did the work") -> str:
    return (
        'cat > "$PM_RESULT_PATH" <<EOF\n'
        '{"slice": "$PM_SLICE_ID", "status": "' + status + '", "summary": "' + summary + '"}\n'
        "EOF"
    )


def _commit_and_result_script(
    repo: Path, *, authorized_file: str = "a.py", unauthorized_file: str | None = None,
    delay: float = 1.0, tail_sleep: float = 2.0,
) -> str:
    lines = [
        "echo FAKE_HARNESS_READY",
        f"sleep {delay}",
        f'echo "authorized change" >> "{repo}/{authorized_file}"',
        f'git -C "{repo}" add "{authorized_file}"',
    ]
    if unauthorized_file:
        lines.append(f'echo "oops" >> "{repo}/{unauthorized_file}"')
        lines.append(f'git -C "{repo}" add "{unauthorized_file}"')
    lines.append(f'git -C "{repo}" commit -q -m "slice work"')
    lines.append(_result_heredoc())
    lines.append(f"sleep {tail_sleep}")
    return "\n".join(lines)


def _idle_script(*, sleep_seconds: float = 30.0) -> str:
    return f"echo FAKE_HARNESS_READY\nsleep {sleep_seconds}"


def _stdin_draining_idle_script() -> str:
    return "echo FAKE_HARNESS_READY\nexec cat -"


def _credential_prompt_after_ready_script(*, reveal_after: float = 3.0, sleep_seconds: float = 20.0) -> str:
    """Comes up clean so the initial slice-prompt injection succeeds, then
    reveals a credential prompt `reveal_after` seconds later — the pane must
    be clear of hard-stop markers at injection time (`send_prompt` refuses
    into one), so the prompt has to appear strictly after start-slice."""
    return f"echo FAKE_HARNESS_READY\nsleep {reveal_after}\necho 'Enter API key to continue'\nsleep {sleep_seconds}"


def _steer_then_complete_script(repo: Path, *, authorized_file: str = "a.py") -> str:
    """Blocks reading stdin until the `finalize --steer` pointer actually
    arrives (its stable "PM correction" marker), then commits
    the authorized change and writes result.json. Completion is thus gated on
    the steer, not raced against a fixed sleep: since a steer now rotates the
    pre-steer result.json into attempt-<n>/ (Stage 7), a harness that finished
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
        _result_heredoc(),
        "sleep 2",
    ]
    return "\n".join(lines)


def _result_then_drain_script() -> str:
    """Writes result.json immediately, then drains stdin and stays alive, so
    a steer can be delivered while a now-stale completion signal already
    exists on disk (Stage 7 Test 21: the pre-steer result must be rotated
    aside so observe --wait can't mistake it for the steered attempt's)."""
    return "echo FAKE_HARNESS_READY\n" + _result_heredoc() + "\nexec cat -"


def _write_fake(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_reviewer_ok(path: Path, marker: str) -> Path:
    return _write_fake(path, f'echo "FAKE REVIEW OK: {marker}"\nexit 0')


def _fake_reviewer_sleep(path: Path, seconds: int = 300) -> Path:
    return _write_fake(path, f"sleep {seconds}")


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# --- shared base ---------------------------------------------------------------


class FinalizeTestCase(PmTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Operate on a dedicated feature branch, as a real run does — the
        # implicit-current-branch init path now refuses main/master.
        self._git("checkout", "-q", "-b", "pm-work")
        self._sessions_to_reap: list[str] = []
        self._subprocesses_to_reap: list[subprocess.Popen] = []
        self.addCleanup(self._reap_sessions)
        self.addCleanup(self._reap_subprocesses)

    def _reap_sessions(self) -> None:
        for name in self._sessions_to_reap:
            sessions.force_stop(name)

    def _reap_subprocesses(self) -> None:
        for proc in self._subprocesses_to_reap:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass

    def _track_current_session(self, run_id: str, token: str) -> str | None:
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        state = state_mod.load_state(run_dir, token)
        current = state.get("current_slice") or {}
        session = current.get("tmux_session")
        if session:
            self._sessions_to_reap.append(session)
        return session

    def _wait_for(self, predicate, timeout: float = 15.0, interval: float = 0.3) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    def _plan_path(self) -> Path:
        return self.repo.parent / "plan.md"

    def _init(self, plan_path: Path, harness_script: Path, *, extra: list[str] | None = None) -> tuple[int, str, str]:
        argv = [
            "init", "--repo", str(self.repo), "--plan", str(plan_path),
            "--harness", "fake", "--harness-command", str(harness_script),
        ]
        if extra:
            argv += extra
        return self.run_cli_in_repo(argv)

    def _wait_for_result(self, run_id: str, token: str) -> bool:
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        state = state_mod.load_state(run_dir, token)
        artifact_dir = Path(state["current_slice"]["artifact_dir"])
        return self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=15.0)


# --- 1: full end-to-end acceptance -------------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestFullAcceptance(FinalizeTestCase):
    def test_accept_writes_assessment_clears_slice_and_regenerates_report(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, _err = self.run_cli_in_repo(["finalize", "--token", token])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count(" PASS "), 8)

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 0, err)
        self.assertIn("ACCEPTED", out)

        head = self._git("rev-parse", "HEAD").stdout.strip()
        state = state_mod.load_state(run_dir, token)
        entry = state["slices"][0]
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["commit"], head)
        self.assertIsNone(state["current_slice"])

        assessment_path = Path(entry["assessment"])
        self.assertTrue(str(assessment_path).startswith(str(run_dir)))
        assessment_text = assessment_path.read_text(encoding="utf-8")
        self.assertIn(_LONG_REASONING, assessment_text)
        self.assertIn("PM assessment only (standard risk)", assessment_text)
        self.assertEqual(assessment_text.count(": PASS"), 8)

        mirror_path = self.repo / ".pm" / "runs" / run_id / "slices" / "slice-001" / "assessment.md"
        self.assertTrue(mirror_path.is_file())
        self.assertEqual(mirror_path.read_text(encoding="utf-8"), assessment_text)

        report_path = run_dir / "run-report.md"
        self.assertTrue(report_path.is_file())
        self.assertIn(_LONG_REASONING, report_path.read_text(encoding="utf-8"))
        report_mirror = self.repo / ".pm" / "runs" / run_id / "run-report.md"
        self.assertTrue(report_mirror.is_file())

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self.assertIn("all slices complete", out)


# --- 2: floor failure refuses acceptance --------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestAcceptRefusedOnFloorFailure(FinalizeTestCase):
    def test_unauthorized_file_refuses_accept(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh",
            _commit_and_result_script(self.repo, unauthorized_file="b.py", delay=1.0, tail_sleep=2.0),
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


# --- 3: reasoning too short ----------------------------------------------------


class TestAcceptRefusedOnShortReasoning(PmTestCase):
    def test_reasoning_under_forty_chars_raises_before_touching_state(self) -> None:
        plan_path = self.write_plan(slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        before_bytes = (run_dir / "run.json").read_bytes()

        code, _out, err = self.run_cli_in_repo(["finalize", "--accept", "too short", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("40", err)

        self.assertEqual((run_dir / "run.json").read_bytes(), before_bytes)


# --- 4: elevated slice review requirement + staleness -------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestElevatedReviewFreshness(FinalizeTestCase):
    def test_missing_then_stale_then_fresh_reviews(self) -> None:
        plan_path = self.write_plan(
            self._plan_path(), slices=[{"files": ["a.py"], "risky": "touches auth"}]
        )
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
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


# --- 5: risk ratchet -----------------------------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestRiskRatchet(FinalizeTestCase):
    def test_ratchet_arms_review_requirement_rejects_lowering_and_persists(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
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


# --- 6: steer --------------------------------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestSteer(FinalizeTestCase):
    def test_steer_files_the_correction_sends_a_pointer_and_exhausts_budget(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _stdin_draining_idle_script())
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _stdin_draining_idle_script())
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

    def test_steer_refuses_into_visible_hard_stop_prompt(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _credential_prompt_after_ready_script(sleep_seconds=15.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
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

    def test_stopped_assessment_retains_full_correction_narrative(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _stdin_draining_idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)

        correction = "Try the other approach entirely.\nSee the notes for why."
        code, out, err = self.run_cli_in_repo(["finalize", "--steer", correction, "--token", token])
        self.assertEqual(code, 0, out + err)

        code, out, err = self.run_cli_in_repo(
            ["finalize", "--stop", "a human is needed to decide the approach", "--token", token]
        )
        self.assertEqual(code, 0, out + err)

        state = state_mod.load_state(run_dir, token)
        assessment_text = Path(state["slices"][0]["assessment"]).read_text(encoding="utf-8")
        self.assertIn("Try the other approach entirely.", assessment_text)
        self.assertIn("See the notes for why.", assessment_text)

    def test_steer_dead_session_raises_relaunch_error(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script(sleep_seconds=30.0))
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


# --- 7: stop decision -----------------------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestStopDecision(FinalizeTestCase):
    def test_stop_writes_stopped_assessment_and_regenerates_report(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

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

        self.assertTrue((run_dir / "run-report.md").is_file())


# --- 8: notes.md controller-owned + mirror + tripwire --------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestNotesMirrorAndTripwire(FinalizeTestCase):
    def test_notes_mirrored_at_start_slice_and_large_notes_warn(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script(sleep_seconds=20.0))
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script(sleep_seconds=20.0))
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())
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

    def test_requires_a_token(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, _token = parse_init_output(out)
        code, _out, err = self.run_cli_in_repo(["notes", "--set", "x", "--run", run_id])
        self.assertEqual(code, 2)
        self.assertIn("token", err.lower())

    def test_empty_or_whitespace_text_is_refused(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        code, _out, err = self.run_cli_in_repo(["notes", "--append", "   ", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("non-empty", err.lower())


# --- 9: report regenerates with .pm/ deleted ------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestReportFromControllerDataAlone(FinalizeTestCase):
    def test_status_report_recreates_mirror_after_pm_deleted(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
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

        shutil.rmtree(self.repo / ".pm")
        self.assertFalse((self.repo / ".pm").exists())

        code, out, _err = self.run_cli_in_repo(["status", "--report", "--run", run_id])
        self.assertEqual(code, 0, out)

        report_path = run_dir / "run-report.md"
        self.assertTrue(report_path.is_file())
        report_text = report_path.read_text(encoding="utf-8")
        self.assertIn(_LONG_REASONING, report_text)

        mirror_path = self.repo / ".pm" / "runs" / run_id / "run-report.md"
        self.assertTrue(mirror_path.is_file())
        self.assertEqual(mirror_path.read_text(encoding="utf-8"), report_text)


# --- 10: stop reaps a hung reviewer -------------------------------------------


# --- 11: attempt-budget exhaustion is a mandatory stop that closes send, ----
# --- steer, and accept, leaving only finalize --stop and stop open ----------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestBudgetExhaustionClosesAllPaths(FinalizeTestCase):
    def test_budget_exhaustion_kills_session_and_closes_steer_send_accept(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _stdin_draining_idle_script())
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


# --- 12: accepting the last undecided slice completes the run ---------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestAcceptingFinalSliceCompletesRun(FinalizeTestCase):
    def test_accepting_final_slice_marks_run_complete(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
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
        self.assertIn("ACCEPTED", out)

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "complete")
        self.assertIsNone(state["stop_reason"])

        report_text = (run_dir / "run-report.md").read_text(encoding="utf-8")
        self.assertIn("complete", report_text)

        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "complete" for event in events))


class TestStopReapsHungReviewer(PmTestCase):
    def test_stop_kills_reviewer_process_group(self) -> None:
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

        code, out, err = self.run_cli_in_repo(["stop", "--reason", "reaping test", "--token", token])
        self.assertEqual(code, 0, out + err)

        self.assertTrue(
            self._wait_for(lambda: not _pgid_alive(pgid), timeout=10.0),
            "reviewer process group survived stop",
        )

        proc.wait(timeout=10)

    def _wait_for(self, predicate, timeout: float = 15.0, interval: float = 0.3) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()


# --- _attempts_summary: exact multiline formatting, no tmux required --------


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

"""Protected behaviours: the acceptance-bearing `finalize` decision paths, the
risk ratchet, controller-owned notes, and report regeneration — over the
headless Developer.

Everything here drives `pm_lib.cli.main` in-process via `run_cli_in_repo`,
matching the lifecycle convention; scenarios drive a tiny fake-harness `sh`
script as a `--harness-command` override exactly like `test_slice_ops.py`. A
`finalize --steer` is a turn-based resume: the prior `-p`/`exec` turn has run
to completion and exited, so PM quiesces it (a no-op when already dead), then
launches a resume turn that the fake recognizes by PM_DEVELOPER_RESUME_SESSION_ID.
Pins:

1. Full end-to-end acceptance: init -> start-slice (fake commits the
   authorized change + result.json) -> bare `finalize` reports 8/8 ->
   `finalize --accept "reasoning"` accepts: the slice entry's commit is HEAD,
   `assessment.md` exists as a controller-owned original and its `.pm/`
   mirror, both containing the reasoning verbatim, all eight floor lines, and
   "PM assessment only (standard risk)"; `current_slice` is cleared and
   `run-report.md` is regenerated. A second `start-slice` reports complete.
2. `--accept` refused when the floor fails (unauthorized file): nothing
   accepted, exit 1.
3. `--accept` refused when reasoning is under the 40-char minimum, before any
   state is touched.
4. Elevated slice: `--accept` refused naming both missing reviews; after fake
   drift-audit + code-review, a further commit makes them stale; `--accept`
   refused again until BOTH are re-commissioned against the new HEAD.
5. Risk ratchet: `finalize --risk elevated --accept` is refused for missing
   reviews (the ratchet arms the requirement before acceptance); `--risk
   standard` is rejected; the ratchet persists.
6. `--steer`: a resume turn is launched with the full (possibly multiline)
   correction as its argument, wrapped in the reference-sourced steer
   template — no `steer-<attempt>.md` artifact anywhere; the `steer` event's
   note carries the complete correction verbatim; `attempts` increments and
   persists; exhausting the budget refuses the next steer and sets
   `needs-human`; a steer with no captured launch-bound session id fails
   closed; a steer resumes normally even though the prior turn already exited;
   the pre-steer result.json is rotated aside so a steered attempt is never
   mistaken for complete; a refused steer that already logged a `risk-raise`
   persists that ratchet so state and the event log agree.
7. `--stop`: the slice becomes "stopped", `assessment.md` records STOPPED with
   the reason verbatim, the run becomes `needs-human`, the process is
   terminated, and the report regenerates.
8. Controller-owned `notes.md`: content is mirrored into `.pm/` at
   `start-slice`; an oversized notes file prints a non-fatal warning.
9. Report-from-controller-data: after acceptance, deleting `.pm/` and running
   `status --report` still recreates `run-report.md` from state + events +
   the assessment file under the state dir alone.
10. `stop` reaps a hung reviewer process group.
11. Attempt-budget exhaustion is a mandatory stop that closes steer and
    accept, leaving only `finalize --stop` and `stop`.
12. Accepting the last undecided slice completes the run.
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
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import (
    PmTestCase,
    commit_and_result_body,
    idle_body,
    parse_init_output,
    write_fake_harness,
    write_result_cmd,
)

from pm_lib import PmError
from pm_lib import sessions
from pm_lib import slice_ops
from pm_lib import state as state_mod

_PM_PY = Path(__file__).resolve().parents[1] / "scripts" / "pm.py"

_LONG_REASONING = (
    "This slice's diff matches the intended change exactly, validation.md shows the "
    "test suite passing, and no deviations from the plan were observed."
)


# --- headless fake harness / reviewer script builders ------------------------
#
# The bodies shared with `test_slice_ops` (`write_result_cmd`, `idle_body`,
# `commit_and_result_body`) live in `pm_test_helpers`; the ones below are
# specific to this suite.
#
# A resumable `--harness-command` override prints its own launch-bound id on an
# exact line that PM captures (never synthesizes). The launch turns below emit
# it deterministically so a later steer re-correlates the id from the completed
# turn and resumes; the no-id fake omits it so a steer must block.
_OVERRIDE_SESSION_ID = "override-session-1"
_EMIT_OVERRIDE_ID = f'echo "PM_DEVELOPER_SESSION_ID: {_OVERRIDE_SESSION_ID}"'


def _steer_then_complete_body(repo: Path, *, authorized_file: str = "a.py") -> str:
    """Launch turn prints its id then idles; the resume turn
    (PM_DEVELOPER_RESUME_SESSION_ID set) commits the authorized change and writes
    result.json."""
    return "\n".join(
        [
            'turn_text="${1:-}"',
            'if [ -n "${PM_DEVELOPER_RESUME_SESSION_ID:-}" ]; then',
            f'  echo "authorized change" >> "{repo}/{authorized_file}"',
            f'  git -C "{repo}" add "{authorized_file}"',
            f'  git -C "{repo}" commit -q -m "slice work"',
            "  " + write_result_cmd(),
            "  sleep 2",
            "else",
            "  " + _EMIT_OVERRIDE_ID,
            "  echo FAKE_HARNESS_WORKING",
            "  sleep 30",
            "fi",
        ]
    )


def _steer_append_body() -> str:
    """Launch turn prints its id then idles; the resume turn appends its
    correction argument to `steer-received.txt` in the slice artifact dir (a
    headless-observable stand-in for the correction reaching the Developer)."""
    return "\n".join(
        [
            'turn_text="${1:-}"',
            'if [ -n "${PM_DEVELOPER_RESUME_SESSION_ID:-}" ]; then',
            "  printf '%s\\n' \"$turn_text\" >> \"$PM_SLICE_ARTIFACT_DIR/steer-received.txt\"",
            "  sleep 5",
            "else",
            "  " + _EMIT_OVERRIDE_ID,
            "  echo FAKE_HARNESS_WORKING",
            "  sleep 30",
            "fi",
        ]
    )


def _result_then_alive_body() -> str:
    """Launch turn prints its id, writes result.json, then stays alive; the
    resume turn idles (writes no result), so after a steer rotates the pre-steer
    result aside the top-level result.json is genuinely absent until real work
    lands."""
    return "\n".join(
        [
            'turn_text="${1:-}"',
            'if [ -n "${PM_DEVELOPER_RESUME_SESSION_ID:-}" ]; then',
            "  echo FAKE_HARNESS_RESUME",
            "  sleep 30",
            "else",
            "  " + _EMIT_OVERRIDE_ID,
            "  echo FAKE_HARNESS_WORKING",
            "  " + write_result_cmd(),
            "  sleep 30",
            "fi",
        ]
    )


def _delayed_id_steer_body(*, delay: float = 1.0) -> str:
    """Launch turn emits its exact id only after `delay`s (then idles); the
    resume turn appends its correction. Exercises the bounded pre-quiesce
    capture wait: at start-slice the id is not yet present, so an immediately
    requested steer must wait for THIS launch's own id before quiescing."""
    return "\n".join(
        [
            'turn_text="${1:-}"',
            'if [ -n "${PM_DEVELOPER_RESUME_SESSION_ID:-}" ]; then',
            "  printf '%s\\n' \"$turn_text\" >> \"$PM_SLICE_ARTIFACT_DIR/steer-received.txt\"",
            "  sleep 5",
            "else",
            f"  sleep {delay}",
            "  " + _EMIT_OVERRIDE_ID,
            "  echo FAKE_HARNESS_WORKING",
            "  sleep 30",
            "fi",
        ]
    )


def _no_session_id_body() -> str:
    """Launch turn emits NO session-id line and idles; PM can bind no id, so a
    later steer must block (fail closed) rather than resume."""
    return "echo FAKE_HARNESS_WORKING\nsleep 30"


def _credential_on_launch_body() -> str:
    """Launch turn surfaces a credential hard-stop marker in its output then
    idles; a steer must refuse to resume into it."""
    return "echo 'Enter API key to continue'\nsleep 30"


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
        # Operate on a dedicated feature branch, as a real run does.
        self._git("checkout", "-q", "-b", "pm-work")
        self._procs: list[tuple[int, int, str]] = []
        self._subprocesses_to_reap: list[subprocess.Popen] = []
        self.addCleanup(self._reap_procs)
        self.addCleanup(self._reap_subprocesses)

    def _reap_procs(self) -> None:
        for pid, pgid, identity in self._procs:
            try:
                sessions.terminate_headless(pid, pgid, identity, term_timeout=0.2, kill_timeout=1.0)
            except PmError:
                pass

    def _reap_subprocesses(self) -> None:
        for proc in self._subprocesses_to_reap:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass

    def _current(self, run_id: str, token: str) -> dict:
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        return state_mod.load_state(run_dir, token).get("current_slice") or {}

    def _track_current_process(self, run_id: str, token: str) -> tuple[int, int, str] | None:
        current = self._current(run_id, token)
        if not current.get("pid"):
            return None
        coords = (int(current["pid"]), int(current["pgid"]), str(current["identity"]))
        self._procs.append(coords)
        return coords

    def _proc_alive(self, coords: tuple[int, int, str]) -> bool:
        pid, _pgid, identity = coords
        return sessions.headless_process_alive(pid, identity)

    def _kill_proc(self, coords: tuple[int, int, str]) -> None:
        pid, pgid, identity = coords
        try:
            sessions.terminate_headless(pid, pgid, identity)
        except PmError:
            pass

    def _wait_for(self, predicate, timeout: float = 15.0, interval: float = 0.3) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    def _plan_path(self) -> Path:
        return self.repo.parent / "plan.md"

    def _artifact_dir(self, run_id: str, token: str) -> Path:
        return Path(self._current(run_id, token)["artifact_dir"])

    def _init(self, plan_path: Path, harness_script: Path, *, extra: list[str] | None = None) -> tuple[int, str, str]:
        argv = [
            "init", "--repo", str(self.repo), "--plan", str(plan_path),
            "--harness", "fake", "--harness-command", str(harness_script),
        ]
        if extra:
            argv += extra
        return self.run_cli_in_repo(argv)

    def _wait_for_result(self, run_id: str, token: str) -> bool:
        artifact_dir = self._artifact_dir(run_id, token)
        return self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=15.0)


# --- 1: full end-to-end acceptance -------------------------------------------


class TestFullAcceptance(FinalizeTestCase):
    def test_accept_writes_assessment_clears_slice_and_regenerates_report(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_body(self.repo, delay=1.0, tail_sleep=3.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
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


class TestAcceptRefusedOnFloorFailure(FinalizeTestCase):
    def test_unauthorized_file_refuses_accept(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh",
            commit_and_result_body(self.repo, unauthorized_file="b.py", delay=1.0, tail_sleep=3.0),
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
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


class TestElevatedReviewFreshness(FinalizeTestCase):
    def test_missing_then_stale_then_fresh_reviews(self) -> None:
        plan_path = self.write_plan(
            self._plan_path(), slices=[{"files": ["a.py"], "risky": "touches auth"}]
        )
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_body(self.repo, delay=1.0, tail_sleep=3.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
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


class TestRiskRatchet(FinalizeTestCase):
    def test_ratchet_arms_review_requirement_rejects_lowering_and_persists(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_body(self.repo, delay=1.0, tail_sleep=3.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["slices"][0]["risk"], "standard")

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
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


class TestSteer(FinalizeTestCase):
    def test_steer_resumes_increments_attempts_and_exhausts_budget(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _steer_append_body())
        code, out, _err = self._init(plan_path, harness, extra=["--max-attempts", "1"])
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords0 = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords0), timeout=10.0))
        artifact_dir = self._artifact_dir(run_id, token)

        # Leading/trailing whitespace is meaningful in a verbatim correction
        # (e.g. an indented code block) and must survive untouched.
        correction = "  Please also update the docstring.\nAnd rerun the tests before committing.  \n"
        code, out, err = self.run_cli_in_repo(["finalize", "--steer", correction, "--token", token])
        self.assertEqual(code, 0, out + err)
        self.assertIn("no artifact file written", out)
        self._track_current_process(run_id, token)  # track the resume process

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["current_slice"]["attempts"], 1)
        self.assertEqual(state["slices"][0]["attempts"], 1)

        # The resume turn advances the per-attempt session label to a1 while the
        # captured harness resume handle (session_id) stays stable.
        self.assertEqual(state["current_slice"]["session"], sessions.session_name(run_id, 1, 1))
        self.assertEqual(state["current_slice"]["session_id"], _OVERRIDE_SESSION_ID)

        # No steer artifact anywhere in either tree.
        self.assertFalse(any(run_dir.rglob("steer-*.md")))
        self.assertFalse(any((self.repo / ".pm" / "runs" / run_id).rglob("steer-*.md")))

        # The full multiline correction reaches the resume turn (the fake
        # records what it received into the artifact dir).
        received = artifact_dir / "steer-received.txt"
        self.assertTrue(
            self._wait_for(
                lambda: received.is_file()
                and "Please also update the docstring." in received.read_text(encoding="utf-8")
                and "And rerun the tests before committing." in received.read_text(encoding="utf-8"),
                timeout=10.0,
            )
        )

        # The steer event's note carries the complete correction verbatim, and
        # no evidence path (no file to point to).
        events = state_mod.read_events(run_dir)
        steer_events = [e for e in events if e["kind"] == "steer"]
        self.assertEqual(len(steer_events), 1)
        self.assertEqual(steer_events[0]["note"], correction)
        self.assertNotIn("evidence", steer_events[0])

        # Budget (max_attempts=1) is now exhausted: the next steer is refused.
        code, _out, err = self.run_cli_in_repo(["finalize", "--steer", "One more nudge.", "--token", token])
        self.assertEqual(code, 2, err)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "needs-human")

    def test_steer_rotates_stale_pre_steer_result(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _result_then_alive_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords0 = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords0), timeout=10.0))
        artifact_dir = self._artifact_dir(run_id, token)

        # Attempt 0 wrote a result.json BEFORE any steer.
        self.assertTrue(self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=10.0))

        code, out, err = self.run_cli_in_repo(["finalize", "--steer", "Remove the dead import.", "--token", token])
        self.assertEqual(code, 0, out + err)
        self._track_current_process(run_id, token)

        # The pre-steer completion signal is rotated into attempt-0/ so the
        # steered attempt can't be mistaken for complete on stale evidence. The
        # resume turn writes no new result, so top-level result.json is
        # genuinely absent until real work lands.
        self.assertTrue((artifact_dir / "attempt-0" / "result.json").is_file())
        self.assertFalse((artifact_dir / "result.json").is_file())

    def test_immediate_steer_binds_delayed_override_id(self) -> None:
        # Regression for the post-correction capture race: the override prints
        # its exact id only ~1s after launch, then idles. A steer requested
        # immediately after start-slice must still bind THIS launch's own id
        # via the bounded pre-quiesce capture wait. Without the fix, start-slice
        # binds nothing (the id is not out yet) and quiescing kills the process
        # before it emits the id, so the steer wrongly blocks.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _delayed_id_steer_body(delay=1.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
        # The id is not emitted until ~1s in, so start-slice bound none.
        self.assertIsNone(state_mod.load_state(run_dir, token)["current_slice"]["session_id"])

        # An immediately requested steer waits (bounded) for the launch-owned id,
        # binds it, and resumes — no synthesized or guessed id.
        code, out, err = self.run_cli_in_repo(["finalize", "--steer", "keep going", "--token", token])
        self.assertEqual(code, 0, out + err)
        self.assertIn("steered", out)
        self._track_current_process(run_id, token)

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["current_slice"]["session_id"], _OVERRIDE_SESSION_ID)
        self.assertEqual(state["current_slice"]["attempts"], 1)
        self.assertEqual(state["current_slice"]["session"], sessions.session_name(run_id, 1, 1))

    def test_steer_blocks_when_override_emits_no_launch_bound_id(self) -> None:
        # A `--harness-command` override that prints no session-id line has no
        # resumable id: PM captures none at launch, re-correlation from the
        # completed turn still finds none, and finalize --steer fails closed —
        # WITHOUT the test manually nulling authenticated state.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _no_session_id_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
        # No id was bound at launch (the override printed none).
        self.assertIsNone(state_mod.load_state(run_dir, token)["current_slice"]["session_id"])

        code, _out, err = self.run_cli_in_repo(["finalize", "--steer", "keep going", "--token", token])
        self.assertEqual(code, 2, err)
        self.assertIn("session id", err)
        self.assertIn("relaunch", err)

        # Fail-closed before persisting: attempts unchanged, no steer event.
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["current_slice"]["attempts"], 0)
        events = state_mod.read_events(run_dir)
        self.assertFalse([e for e in events if e["kind"] == "steer"])

    def test_refused_steer_persists_the_risk_ratchet_it_logged(self) -> None:
        # The ratchet is a durable one-way escalation, and `finalize --steer`
        # writes its `risk-raise` event before the refusal gates run. A refusal
        # (here: no launch-bound session id) must therefore still persist the
        # elevation: state reading "standard" while the event log records
        # "elevated" would fail open on the elevated-review requirement at
        # accept time. Nothing else the refused turn touched is persisted.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _no_session_id_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)

        code, _out, err = self.run_cli_in_repo(
            ["finalize", "--steer", "keep going", "--risk", "elevated", "--token", token]
        )
        self.assertEqual(code, 2, err)
        self.assertIn("session id", err)

        state = state_mod.load_state(run_dir, token)
        events = state_mod.read_events(run_dir)
        self.assertEqual(len([e for e in events if e["kind"] == "risk-raise"]), 1)
        self.assertEqual(state["slices"][0]["risk"], "elevated")
        self.assertEqual(state["current_slice"]["risk"], "elevated")
        # The ratchet only ever raises the mutable field, never the plan fact.
        self.assertEqual(state["slices"][0]["plan_risk"], "standard")
        # The refused turn consumed no attempt and logged no steer.
        self.assertEqual(state["current_slice"]["attempts"], 0)
        self.assertFalse([e for e in events if e["kind"] == "steer"])

    def test_steer_refuses_into_visible_hard_stop_marker(self) -> None:
        # The pre-cutover correction rule, preserved headlessly: once the prior
        # turn has quiesced, a credential/approval/usage/side-effect marker in
        # its captured output means PM must refuse the resume — before any
        # attempt increment, rotation, or steer event.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _credential_on_launch_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords = self._track_current_process(run_id, token)
        outfile = self._artifact_dir(run_id, token) / sessions.SESSION_OUTFILE
        self.assertTrue(
            self._wait_for(
                lambda: outfile.is_file() and "Enter API key" in outfile.read_text(encoding="utf-8"),
                timeout=10.0,
            )
        )

        code, _out, err = self.run_cli_in_repo(["finalize", "--steer", "please continue", "--token", token])
        self.assertEqual(code, 2, err)
        self.assertIn("credential_prompt", err)

        # Refused before persisting: attempts unchanged, no steer event.
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["current_slice"]["attempts"], 0)
        events = state_mod.read_events(run_dir)
        self.assertFalse([e for e in events if e["kind"] == "steer"])

    def test_steer_resumes_even_though_prior_turn_already_exited(self) -> None:
        # The headless inversion of the old "dead session raises" rule: a prior
        # turn that has run to completion and exited is the NORMAL precondition
        # for a steer, so a steer resumes it (after a no-op quiesce) rather than
        # refusing.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _steer_then_complete_body(self.repo))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords0 = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords0), timeout=10.0))

        # Establish the resumable-session precondition before forcing the
        # process dead. Merely observing a live PID does not prove the fake has
        # emitted its launch-bound id yet; killing it before that point would
        # correctly leave PM with no safe session to resume.
        outfile = self._artifact_dir(run_id, token) / sessions.SESSION_OUTFILE
        self.assertTrue(
            self._wait_for(
                lambda: outfile.is_file()
                and f"PM_DEVELOPER_SESSION_ID: {_OVERRIDE_SESSION_ID}"
                in outfile.read_text(encoding="utf-8"),
                timeout=10.0,
            )
        )

        # Force the known prior turn dead before steering.
        self._kill_proc(coords0)
        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords0), timeout=10.0))

        code, out, err = self.run_cli_in_repo(["finalize", "--steer", "finish the work", "--token", token])
        self.assertEqual(code, 0, out + err)
        self.assertIn("steered", out)
        self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

    def test_accepted_assessment_retains_full_correction_narrative(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _steer_then_complete_body(self.repo))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords0 = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords0), timeout=10.0))

        correction = "Please rename the helper.\nAlso add a docstring."
        code, out, err = self.run_cli_in_repo(["finalize", "--steer", correction, "--token", token])
        self.assertEqual(code, 0, out + err)
        self._track_current_process(run_id, token)

        self.assertTrue(self._wait_for_result(run_id, token))

        code, out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 0, out + err)

        state = state_mod.load_state(run_dir, token)
        assessment_text = Path(state["slices"][0]["assessment"]).read_text(encoding="utf-8")
        self.assertIn("Please rename the helper.", assessment_text)
        self.assertIn("Also add a docstring.", assessment_text)

    def test_stopped_assessment_retains_full_correction_narrative(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _steer_append_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords0 = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords0), timeout=10.0))

        correction = "Try the other approach entirely.\nSee the notes for why."
        code, out, err = self.run_cli_in_repo(["finalize", "--steer", correction, "--token", token])
        self.assertEqual(code, 0, out + err)
        self._track_current_process(run_id, token)

        code, out, err = self.run_cli_in_repo(
            ["finalize", "--stop", "a human is needed to decide the approach", "--token", token]
        )
        self.assertEqual(code, 0, out + err)

        state = state_mod.load_state(run_dir, token)
        assessment_text = Path(state["slices"][0]["assessment"]).read_text(encoding="utf-8")
        self.assertIn("Try the other approach entirely.", assessment_text)
        self.assertIn("See the notes for why.", assessment_text)


# --- 7: stop decision -----------------------------------------------------


class TestStopDecision(FinalizeTestCase):
    def test_stop_writes_stopped_assessment_and_regenerates_report(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords), timeout=10.0))

        code, out, err = self.run_cli_in_repo(
            ["finalize", "--stop", "giving up on this approach", "--token", token]
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("STOPPED", out)

        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords), timeout=10.0))

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


class TestNotesMirrorAndTripwire(FinalizeTestCase):
    def test_notes_mirrored_at_start_slice_and_large_notes_warn(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=20.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        (run_dir / "notes.md").write_text("decision: use approach B\n", encoding="utf-8")

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
        self.assertNotIn("WARNING", out)

        mirror = self.repo / ".pm" / "runs" / run_id / "notes.md"
        self.assertTrue(mirror.is_file())
        self.assertEqual(mirror.read_text(encoding="utf-8"), "decision: use approach B\n")

    def test_oversized_notes_prints_tripwire_warning(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=20.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        (run_dir / "notes.md").write_text("x" * (600 * 1024), encoding="utf-8")

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
        self.assertIn("WARNING", out)
        self.assertIn("512", out)


# `notes` needs no launch (init only), so no process is involved.
class TestNotesCommand(FinalizeTestCase):
    def test_set_then_append_write_authoritative_original_and_mirror(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
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
        self.assertEqual(mirror.read_text(encoding="utf-8"), text)

    def test_requires_a_token(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, _token = parse_init_output(out)
        code, _out, err = self.run_cli_in_repo(["notes", "--set", "x", "--run", run_id])
        self.assertEqual(code, 2)
        self.assertIn("token", err.lower())

    def test_empty_or_whitespace_text_is_refused(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        code, _out, err = self.run_cli_in_repo(["notes", "--append", "   ", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("non-empty", err.lower())


# --- 9: report regenerates with .pm/ deleted ------------------------------


class TestReportFromControllerDataAlone(FinalizeTestCase):
    def test_status_report_recreates_mirror_after_pm_deleted(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_body(self.repo, delay=1.0, tail_sleep=3.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
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


# --- 11: attempt-budget exhaustion closes steer and accept -------------------


class TestBudgetExhaustionClosesAllPaths(FinalizeTestCase):
    def test_budget_exhaustion_terminates_process_and_closes_steer_accept(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
        code, out, _err = self._init(plan_path, harness, extra=["--max-attempts", "0"])
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        # Attempt 0 (the initial launch) is never budget-checked — only a
        # relaunch or a steer counts — so this succeeds even with max_attempts=0.
        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords), timeout=10.0))

        # The steer itself would be attempt 1, over the budget of 0: refused,
        # and the exhaustion is a mandatory stop that terminates the process.
        code, _out, err = self.run_cli_in_repo(["finalize", "--steer", "fix it please", "--token", token])
        self.assertEqual(code, 2, err)
        self.assertIn("attempt budget exhausted", err)

        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords), timeout=10.0))

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "needs-human")
        self.assertEqual(state["stop_reason"], "attempt budget exhausted")

        code, _out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("attempt budget exhausted", err)

        # finalize --stop remains open even after exhaustion.
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


class TestAcceptingFinalSliceCompletesRun(FinalizeTestCase):
    def test_accepting_final_slice_marks_run_complete(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_body(self.repo, delay=1.0, tail_sleep=3.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
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


# --- 13: termination failure never claims success ----------------------------


class TestTerminationFailureInFinalize(FinalizeTestCase):
    def test_accept_does_not_record_acceptance_when_termination_fails(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_body(self.repo, delay=1.0, tail_sleep=3.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

        # The floor passes, but the Developer process group cannot be terminated:
        # acceptance must not be recorded and current authority must not clear.
        with mock.patch.object(
            sessions, "terminate_headless", side_effect=PmError("headless process group survived SIGKILL")
        ):
            code, _out, err = self.run_cli_in_repo(["finalize", "--accept", _LONG_REASONING, "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("SIGKILL", err)

        state = state_mod.load_state(run_dir, token)
        self.assertNotEqual(state["slices"][0].get("status"), "accepted")
        self.assertIsNotNone(state["current_slice"])
        # Termination is attempted before the assessment is rendered, so no
        # ACCEPTED assessment is left on disk announcing a decision the state
        # never recorded.
        self.assertFalse(any(run_dir.rglob("assessment.md")))
        self.assertFalse(any((self.repo / ".pm" / "runs" / run_id).rglob("assessment.md")))

    def test_stop_does_not_publish_assessment_when_termination_fails(self) -> None:
        # Same rule as accept, on the stop path: the Developer is terminated
        # before the STOPPED assessment is published, so a termination failure
        # cannot leave an assessment on disk announcing a stop the state never
        # recorded.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)

        with mock.patch.object(
            sessions, "terminate_headless", side_effect=PmError("headless process group survived SIGKILL")
        ):
            code, _out, err = self.run_cli_in_repo(
                ["finalize", "--stop", "the harness wedged and cannot be recovered", "--token", token]
            )
        self.assertEqual(code, 2)
        self.assertIn("SIGKILL", err)

        state = state_mod.load_state(run_dir, token)
        self.assertNotEqual(state["slices"][0].get("status"), "stopped")
        self.assertIsNotNone(state["current_slice"])
        self.assertFalse(any(run_dir.rglob("assessment.md")))
        self.assertFalse(any((self.repo / ".pm" / "runs" / run_id).rglob("assessment.md")))

    def test_budget_exhaustion_does_not_stop_when_termination_fails(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
        code, out, _err = self._init(plan_path, harness, extra=["--max-attempts", "0"])
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)

        # The steer is over budget (a mandatory stop), but the process group
        # cannot be terminated: the run must not be recorded needs-human on a
        # kill PM could not perform.
        with mock.patch.object(
            sessions, "terminate_headless", side_effect=PmError("headless process group survived SIGKILL")
        ):
            code, _out, err = self.run_cli_in_repo(["finalize", "--steer", "fix it", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("SIGKILL", err)

        state = state_mod.load_state(run_dir, token)
        self.assertNotEqual(state["status"], "needs-human")


# --- 10: stop reaps a hung reviewer -------------------------------------------


class TestStopReapsHungReviewer(PmTestCase):
    def test_stop_kills_reviewer_process_group(self) -> None:
        # plan.md must live OUTSIDE the worktree: an untracked plan.md inside
        # the repo is a dirty-tree entry, and `review` refuses on a dirty
        # worktree, which would fail before the reviewer subprocess launches.
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


# --- _attempts_summary: exact multiline formatting, no process required ------


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

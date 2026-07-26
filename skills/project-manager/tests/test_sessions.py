"""Protected behaviours: the headless Developer runner and the hard-stop marker floor.

Every test here runs unconditionally — none is skipped. The runtime tests
drive detached `/bin/sh` commands and a tiny fake harness script, never a real
coding CLI, so they need no external tool on PATH.

Pins:

- `scan_hard_stop`: at least one positive fixture per marker class — trust_prompt
  (all three directory-trust strings), approval_prompt, credential_prompt,
  permission_prompt, external_side_effect_request (a "push to remote …?"
  shape), and usage_limit_hard_stop (weekly, monthly, account/billing,
  and the generic reached/exceeded/exhausted phrasing) — plus the two
  mandatory negative fixtures (an informational sub-100% usage warning, a
  conditional "if you hit your limit" phrasing) and a prompt wrapped across
  terminal lines that still matches after whitespace normalization.
- `session_name` always starts with `pm-<run_id>` in the frozen
  `pm-<run_id>-s<NN>a<N>` shape.
- `launch_headless`: a detached launch records pid/pgid/identity and an
  outfile capturing both stdout and stderr; it refuses an explicit
  `PM_RUN_TOKEN` in the caller's env map AND strips an inherited one, so the
  Developer never receives the run capability token; a launch whose identity
  cannot be captured is killed and reaped rather than left untracked.
- `read_output_tail` is byte-bounded, empty for a missing file, and rejects a
  non-positive bound.
- Resume: a second detached turn honouring `PM_DEVELOPER_RESUME_SESSION_ID`
  continues the prior fake session and can commit.
- `terminate_headless` / `quiesce_headless`: reap an identity-checked process
  group including its descendants, never signal on an identity mismatch, and
  still reap a group orphaned by an early leader exit.
- The `developer.pid` sidecar round-trips at mode 0600, returns `None` when
  absent, and fails closed on malformed or invalid fields.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import PmError
from pm_lib import sessions
from pm_test_helpers import write_headless_fake_harness


# --- scan_hard_stop ---------------------------------------------------------


class TestScanHardStopPositiveFixtures(unittest.TestCase):
    def test_trust_prompt_markers(self) -> None:
        for marker in sessions.TRUST_PROMPT_MARKERS:
            with self.subTest(marker=marker):
                result = sessions.scan_hard_stop(f"{marker}?")
                self.assertTrue(result["present"])
                self.assertIn("trust_prompt", result["kinds"])

    def test_approval_prompt(self) -> None:
        result = sessions.scan_hard_stop("Do you want to proceed?")
        self.assertTrue(result["present"])
        self.assertIn("approval_prompt", result["kinds"])

    def test_qwen_manual_approval_prompt(self) -> None:
        result = sessions.scan_hard_stop("This action requires manual approval before continuing")
        self.assertTrue(result["present"])
        self.assertIn("approval_prompt", result["kinds"])

    def test_credential_prompt(self) -> None:
        result = sessions.scan_hard_stop("Enter API key to continue")
        self.assertTrue(result["present"])
        self.assertIn("credential_prompt", result["kinds"])

    def test_permission_prompt(self) -> None:
        result = sessions.scan_hard_stop("Permission denied")
        self.assertTrue(result["present"])
        self.assertIn("permission_prompt", result["kinds"])

    def test_external_side_effect_push_to_remote_shape(self) -> None:
        result = sessions.scan_hard_stop("Push to remote origin/main now?")
        self.assertTrue(result["present"])
        self.assertIn("external_side_effect_request", result["kinds"])

    def test_external_side_effect_approve_shape(self) -> None:
        result = sessions.scan_hard_stop("Approve deploy to production? [y/n]")
        self.assertTrue(result["present"])
        self.assertIn("external_side_effect_request", result["kinds"])

    def test_usage_limit_weekly(self) -> None:
        result = sessions.scan_hard_stop("Weekly usage limit reached. Try again next week.")
        self.assertTrue(result["present"])
        self.assertIn("usage_limit_hard_stop", result["kinds"])

    def test_usage_limit_monthly(self) -> None:
        result = sessions.scan_hard_stop("Monthly quota cap reached for this workspace.")
        self.assertTrue(result["present"])
        self.assertIn("usage_limit_hard_stop", result["kinds"])

    def test_usage_limit_billing_credits(self) -> None:
        result = sessions.scan_hard_stop("Subscription plan limit exhausted. Upgrade billing to continue.")
        self.assertTrue(result["present"])
        self.assertIn("usage_limit_hard_stop", result["kinds"])

    def test_usage_limit_generic_reached(self) -> None:
        result = sessions.scan_hard_stop("Usage limit reached.")
        self.assertTrue(result["present"])
        self.assertIn("usage_limit_hard_stop", result["kinds"])


class TestScanHardStopNegativeFixtures(unittest.TestCase):
    def test_informational_sub_100_percent_usage_warning_is_not_stopping(self) -> None:
        result = sessions.scan_hard_stop("You've used 80% of your weekly limit.")
        self.assertFalse(result["present"])
        self.assertEqual(result["kinds"], [])

    def test_conditional_if_you_hit_your_limit_is_not_stopping(self) -> None:
        result = sessions.scan_hard_stop("If you hit your limit, you can continue on usage credits.")
        self.assertFalse(result["present"])
        self.assertEqual(result["kinds"], [])

    def test_empty_text_is_not_stopping(self) -> None:
        result = sessions.scan_hard_stop("")
        self.assertFalse(result["present"])
        self.assertEqual(result["kinds"], [])
        self.assertEqual(result["markers"], [])


class TestScanHardStopWrapping(unittest.TestCase):
    def test_prompt_wrapped_across_lines_still_matches(self) -> None:
        wrapped = "Weekly usage\nlimit reached across\ntwo terminal rows."
        result = sessions.scan_hard_stop(wrapped)
        self.assertTrue(result["present"])
        self.assertIn("usage_limit_hard_stop", result["kinds"])

    def test_credential_prompt_wrapped_across_lines_still_matches(self) -> None:
        wrapped = "Enter API\nkey to continue"
        result = sessions.scan_hard_stop(wrapped)
        self.assertTrue(result["present"])
        self.assertIn("credential_prompt", result["kinds"])


# --- session_name -----------------------------------------------------------


class TestSessionName(unittest.TestCase):
    def test_starts_with_pm_run_id_prefix(self) -> None:
        name = sessions.session_name("20260718T090000Z", 3, 1)
        self.assertTrue(name.startswith("pm-20260718T090000Z"))

    def test_shape_is_stable(self) -> None:
        self.assertEqual(sessions.session_name("run-a", 1, 0), "pm-run-a-s01a0")
        self.assertEqual(sessions.session_name("run-a", 12, 2), "pm-run-a-s12a2")


class HeadlessSessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "pm-test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "PM Test"], check=True)
        (self.repo / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "initial"], check=True)
        self.artifact_dir = self.repo / "artifacts"
        self._processes: list[dict] = []
        self.addCleanup(self._cleanup_processes)

    def _launch(
        self,
        command: str,
        env: dict[str, str] | None = None,
        artifact_name: str = "artifacts",
    ) -> dict:
        process = sessions.launch_headless(
            command,
            self.repo,
            env or {},
            self.repo / artifact_name,
        )
        self._processes.append(process)
        return process

    def _cleanup_processes(self) -> None:
        for process in self._processes:
            try:
                sessions.terminate_headless(
                    process["pid"],
                    process["pgid"],
                    process["identity"],
                    term_timeout=0.2,
                    kill_timeout=1.0,
                )
            except PmError:
                pass

    def _wait_for(self, predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()


class TestHeadlessLaunchAndResume(HeadlessSessionTestCase):
    def test_launch_and_resume_write_output_result_and_commit(self) -> None:
        harness = write_headless_fake_harness(self.repo / "fake-headless.sh")
        result_path = self.repo / "result.json"
        tracked_path = self.repo / "resume.txt"

        launch = self._launch(
            f"{harness} launch-pointer",
            {"PM_RESULT_PATH": str(result_path)},
            "launch-artifacts",
        )
        self.assertTrue(self._wait_for(lambda: result_path.is_file()))
        self.assertTrue(self._wait_for(lambda: not sessions.headless_process_alive(launch["pid"], launch["identity"])))
        self.assertIn("HEADLESS_FAKE_LAUNCH", sessions.read_output_tail(Path(launch["outfile"])))

        result_path.unlink()
        resume = self._launch(
            f"{harness} resume-correction",
            {
                "PM_RESULT_PATH": str(result_path),
                "PM_DEVELOPER_RESUME_SESSION_ID": "fake-session-1",
                "PM_HEADLESS_FAKE_APPEND_PATH": str(tracked_path),
            },
            "resume-artifacts",
        )
        self.assertTrue(self._wait_for(lambda: result_path.is_file()))
        self.assertTrue(self._wait_for(lambda: not sessions.headless_process_alive(resume["pid"], resume["identity"])))
        self.assertEqual(tracked_path.read_text(encoding="utf-8"), "resume-correction\n")
        self.assertIn("HEADLESS_FAKE_RESUME:fake-session-1", sessions.read_output_tail(Path(resume["outfile"])))
        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(log.stdout.strip(), "Headless fake resume")

    def test_launch_records_live_process_coordinates_and_sanitizes_token(self) -> None:
        previous = os.environ.get("PM_RUN_TOKEN")
        os.environ["PM_RUN_TOKEN"] = "must-not-leak"
        self.addCleanup(
            lambda: os.environ.pop("PM_RUN_TOKEN", None)
            if previous is None
            else os.environ.__setitem__("PM_RUN_TOKEN", previous)
        )
        process = self._launch(
            "sh -c 'echo TOKEN_IS=${PM_RUN_TOKEN:-ABSENT}; echo STDERR_MARKER >&2; sleep 30'",
        )
        self.assertEqual(process["pid"], process["pgid"])
        self.assertTrue(process["identity"])
        self.assertTrue(self._wait_for(lambda: "TOKEN_IS=" in sessions.read_output_tail(Path(process["outfile"]))))
        self.assertIn("TOKEN_IS=ABSENT", sessions.read_output_tail(Path(process["outfile"])))
        self.assertIn("STDERR_MARKER", sessions.read_output_tail(Path(process["outfile"])))
        self.assertTrue(sessions.headless_process_alive(process["pid"], process["identity"]))

    def test_explicit_run_token_is_refused(self) -> None:
        with self.assertRaises(PmError):
            sessions.launch_headless(
                "echo should-not-run",
                self.repo,
                {"PM_RUN_TOKEN": "must-not-leak"},
                self.artifact_dir,
            )

    def test_output_tail_is_bounded_and_missing_file_is_empty(self) -> None:
        outfile = self.artifact_dir / sessions.SESSION_OUTFILE
        outfile.parent.mkdir(parents=True)
        outfile.write_text("prefix-TAIL", encoding="utf-8")
        self.assertEqual(sessions.read_output_tail(outfile, max_bytes=4), "TAIL")
        self.assertEqual(sessions.read_output_tail(self.artifact_dir / "missing.txt"), "")
        with self.assertRaises(ValueError):
            sessions.read_output_tail(outfile, max_bytes=0)

    def test_identity_capture_failure_kills_and_reaps_new_process_group(self) -> None:
        started = time.monotonic()
        with mock.patch.object(sessions, "process_identity", return_value=None):
            with self.assertRaises(PmError):
                sessions.launch_headless(
                    "sh -c 'sleep 30 & wait'",
                    self.repo,
                    {},
                    self.artifact_dir,
                )
        self.assertLess(time.monotonic() - started, 5.0)


class TestHeadlessTerminationAndQuiescence(HeadlessSessionTestCase):
    def test_terminate_reaps_identity_checked_process_group(self) -> None:
        child_pid_path = self.repo / "child.pid"
        process = self._launch(
            f"sh -c 'sleep 30 & child=$!; echo $child > {child_pid_path}; wait'"
        )
        self.assertTrue(self._wait_for(child_pid_path.is_file))
        child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
        self.assertTrue(sessions.headless_process_alive(process["pid"], process["identity"]))
        self.assertTrue(
            sessions.terminate_headless(
                process["pid"],
                process["pgid"],
                process["identity"],
                term_timeout=0.2,
                kill_timeout=2.0,
            )
        )
        self.assertFalse(sessions.headless_process_alive(process["pid"], process["identity"]))
        child_state = sessions._process_state(child_pid)
        self.assertTrue(child_state is None or "Z" in child_state)
        self.assertFalse(sessions._process_group_has_live_members(process["pgid"]))

    def test_identity_mismatch_never_signals_process(self) -> None:
        process = self._launch("sleep 30")
        self.assertFalse(
            sessions.terminate_headless(
                process["pid"],
                process["pgid"],
                "not-the-launch-identity",
                term_timeout=0.1,
                kill_timeout=0.1,
            )
        )
        self.assertTrue(sessions.headless_process_alive(process["pid"], process["identity"]))

    def test_quiesce_confirms_process_is_dead(self) -> None:
        process = self._launch("sleep 30")
        sessions.quiesce_headless(
            process["pid"],
            process["pgid"],
            process["identity"],
            term_timeout=0.2,
            kill_timeout=2.0,
        )
        self.assertFalse(sessions.headless_process_alive(process["pid"], process["identity"]))

    def test_quiesce_reaps_orphaned_process_group_after_leader_exit(self) -> None:
        child_pid_path = self.repo / "orphan-child.pid"
        process = self._launch(
            f"sh -c 'sleep 30 & echo $! > {child_pid_path}'"
        )
        self.assertTrue(self._wait_for(child_pid_path.is_file))
        child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
        self.assertTrue(
            self._wait_for(
                lambda: not sessions.headless_process_alive(
                    process["pid"], process["identity"]
                )
            )
        )
        sessions.quiesce_headless(
            process["pid"],
            process["pgid"],
            process["identity"],
            term_timeout=0.2,
            kill_timeout=2.0,
        )
        child_state = sessions._process_state(child_pid)
        self.assertTrue(child_state is None or "Z" in child_state)
        self.assertFalse(sessions._process_group_has_live_members(process["pgid"]))


class TestDeveloperSidecar(HeadlessSessionTestCase):
    def test_sidecar_round_trip_and_missing_file(self) -> None:
        process = self._launch("sleep 30")
        path = self.artifact_dir / sessions.DEVELOPER_PID_SIDECAR
        sessions.write_developer_sidecar(
            path,
            pid=process["pid"],
            pgid=process["pgid"],
            identity=process["identity"],
            run_id="run-123",
            slice_id="Slice 1",
        )
        self.assertEqual(
            sessions.read_developer_sidecar(path),
            {
                "version": 1,
                "pid": process["pid"],
                "pgid": process["pgid"],
                "identity": process["identity"],
                "run_id": "run-123",
                "slice_id": "Slice 1",
            },
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(sessions.read_developer_sidecar(self.artifact_dir / "missing.pid"))

    def test_malformed_sidecar_fails_closed(self) -> None:
        path = self.artifact_dir / sessions.DEVELOPER_PID_SIDECAR
        path.parent.mkdir(parents=True)
        path.write_text('{"pid": "wrong"}\n', encoding="utf-8")
        with self.assertRaises(PmError):
            sessions.read_developer_sidecar(path)

    def test_invalid_sidecar_values_fail_closed(self) -> None:
        path = self.artifact_dir / sessions.DEVELOPER_PID_SIDECAR
        path.parent.mkdir(parents=True)
        valid = {
            "version": 1,
            "pid": 123,
            "pgid": 123,
            "identity": "identity",
            "run_id": "run-123",
            "slice_id": "Slice 1",
        }
        invalid_values = (
            ("version", True),
            ("version", 2),
            ("pid", True),
            ("pid", -1),
            ("pgid", 123.0),
            ("pgid", 0),
            ("identity", ""),
            ("run_id", ""),
            ("slice_id", ""),
        )
        for field, invalid in invalid_values:
            with self.subTest(field=field, invalid=invalid):
                payload = dict(valid)
                payload[field] = invalid
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(PmError):
                    sessions.read_developer_sidecar(path)


if __name__ == "__main__":
    unittest.main()

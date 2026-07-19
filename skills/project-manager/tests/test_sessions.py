"""Protected behaviours: tmux session lifecycle and the hard-stop marker floor.

`scan_hard_stop`, `session_name`, and the env-token assertion run without
tmux. Everything that actually drives a tmux pane is gated with
`@unittest.skipUnless(shutil.which("tmux"), ...)` and drives a tiny fake
harness shell script — no real coding CLI, matching the retained
fake-harness test pattern (replacement-ledger §9.1/§9.3).

Pins:

- `scan_hard_stop` (target-design §11's marker floor, carried from
  old-evidence tmux_adapter.HARD_PROMPT_MARKERS + hints.py's usage-limit
  patterns): at least one positive fixture per marker class — trust_prompt
  (all three directory-trust strings), approval_prompt, credential_prompt,
  permission_prompt, external_side_effect_request (a "push to remote …?"
  shape), and usage_limit_hard_stop (weekly, monthly, account/billing,
  and the generic reached/exceeded/exhausted phrasing) — plus the two
  mandatory negative fixtures (an informational sub-100% usage warning, a
  conditional "if you hit your limit" phrasing) and a prompt wrapped across
  terminal lines that still matches after whitespace normalization.
- `session_name` always starts with `pm-<run_id>` (the scavenge sweep
  prefix) in the frozen `pm-<run_id>-s<NN>a<N>` shape.
- `start_session` refuses (PmError) when the caller's env dict contains
  `PM_RUN_TOKEN` — the Developer session must never receive the run
  capability token.
- (tmux-gated) A fresh session launch, readiness (stable-pane path),
  `send_prompt` injection landing in the pane, `send_line` refusing to send
  into a visible credential prompt, `capture_to` writing pane text,
  `detect_activity` flagging a pane change, `force_stop` killing a session,
  `sessions_with_prefix` finding sessions by prefix, and `wait_until_ready`
  raising when the session exits before becoming ready.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import PmError
from pm_lib import sessions

_HAS_TMUX = shutil.which("tmux") is not None


# --- scan_hard_stop: no tmux required ---------------------------------------


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


# --- session_name: no tmux required ------------------------------------------


class TestSessionName(unittest.TestCase):
    def test_starts_with_pm_run_id_prefix(self) -> None:
        name = sessions.session_name("20260718T090000Z", 3, 1)
        self.assertTrue(name.startswith("pm-20260718T090000Z"))

    def test_shape_is_stable(self) -> None:
        self.assertEqual(sessions.session_name("run-a", 1, 0), "pm-run-a-s01a0")
        self.assertEqual(sessions.session_name("run-a", 12, 2), "pm-run-a-s12a2")


# --- env-token assertion: no tmux required -----------------------------------


class TestStartSessionEnvTokenAssertion(unittest.TestCase):
    def test_pm_run_token_in_env_raises(self) -> None:
        with self.assertRaises(PmError):
            sessions.start_session(
                "pm-test-s01a0", Path("/tmp"), "echo hi", {"PM_RUN_TOKEN": "should-never-be-here"}
            )


# --- tmux-gated behaviour ------------------------------------------------


def _write_fake_harness(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(_HAS_TMUX, "tmux is required for session lifecycle tests")
class TmuxSessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self._sessions: list[str] = []
        self.addCleanup(self._cleanup_sessions)

    def _cleanup_sessions(self) -> None:
        for name in self._sessions:
            sessions.force_stop(name)

    def _start(self, name: str, command: str, env: dict[str, str] | None = None) -> None:
        self._sessions.append(name)
        sessions.start_session(name, self.repo, command, env or {})

    def _wait_for(self, predicate, timeout: float = 10.0, interval: float = 0.2) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()


class TestStartSessionAndBasicLifecycle(TmuxSessionTestCase):
    def test_start_session_creates_a_live_session(self) -> None:
        name = "pm-test-lifecycle-s01a0"
        self._start(name, "bash -c 'echo hello; sleep 5'")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name)))

    def test_force_stop_kills_the_session(self) -> None:
        name = "pm-test-forcestop-s01a0"
        self._start(name, "bash -c 'sleep 30'")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name)))
        sessions.force_stop(name)
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(name)))

    def test_sessions_with_prefix_finds_by_prefix(self) -> None:
        prefix = "pm-test-prefix-sweep"
        name_a = f"{prefix}-s01a0"
        name_b = f"{prefix}-s02a0"
        self._start(name_a, "bash -c 'sleep 30'")
        self._start(name_b, "bash -c 'sleep 30'")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name_a) and sessions.session_exists(name_b)))
        found = sessions.sessions_with_prefix(prefix)
        self.assertIn(name_a, found)
        self.assertIn(name_b, found)

    def test_capture_to_writes_pane_text(self) -> None:
        name = "pm-test-capture-s01a0"
        self._start(name, "bash -c 'echo CAPTURE_MARKER_TEXT; sleep 5'")
        self.assertTrue(self._wait_for(lambda: "CAPTURE_MARKER_TEXT" in sessions.pane_text(name)))
        destination = self.repo / "pane.txt"
        sessions.capture_to(name, destination)
        self.assertIn("CAPTURE_MARKER_TEXT", destination.read_text(encoding="utf-8"))

    def test_capture_to_writes_placeholder_for_dead_session(self) -> None:
        destination = self.repo / "pane-dead.txt"
        sessions.capture_to("pm-test-does-not-exist-s01a0", destination)
        content = destination.read_text(encoding="utf-8")
        self.assertTrue(content.strip())

    def test_detect_activity_flags_change(self) -> None:
        name = "pm-test-activity-s01a0"
        self._start(name, "bash -c 'sleep 1; echo NEW_ACTIVITY_LINE; sleep 5'")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name)))
        result = sessions.detect_activity(name, "")
        self.assertTrue(result["running"])
        self.assertTrue(self._wait_for(lambda: sessions.detect_activity(name, result["capture"])["active"]))


class TestSendPrompt(TmuxSessionTestCase):
    def test_send_prompt_injects_file_content_into_pane(self) -> None:
        name = "pm-test-sendprompt-s01a0"
        self._start(name, "cat -")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name)))
        prompt_path = self.repo / "prompt.txt"
        prompt_path.write_text("PM_TEST_PROMPT_MARKER_XYZ\n", encoding="utf-8")

        sessions.send_prompt(name, prompt_path)

        self.assertTrue(self._wait_for(lambda: "PM_TEST_PROMPT_MARKER_XYZ" in sessions.pane_text(name)))


class TestSendLine(TmuxSessionTestCase):
    def test_send_line_refuses_on_visible_credential_prompt(self) -> None:
        name = "pm-test-sendline-credential-s01a0"
        self._start(name, "bash -c 'echo Enter API key to continue; sleep 5'")
        self.assertTrue(self._wait_for(lambda: "Enter API key" in sessions.pane_text(name)))

        with self.assertRaises(PmError) as ctx:
            sessions.send_line(name, "please continue")
        self.assertIn("credential_prompt", str(ctx.exception))

    def test_send_line_refuses_multiline_text(self) -> None:
        with self.assertRaises(PmError):
            sessions.send_line("pm-test-doesnt-matter-s01a0", "line one\nline two")

    def test_send_line_refuses_when_session_dead(self) -> None:
        with self.assertRaises(PmError):
            sessions.send_line("pm-test-definitely-not-running-s01a0", "hello")


class TestStartSessionStripsInheritedToken(TmuxSessionTestCase):
    def test_start_session_strips_inherited_run_token(self) -> None:
        # Two distinct ways a token could leak into a Developer session's
        # inherited environment: (1) the controller PROCESS's own
        # os.environ at the moment it shells out to tmux, and (2) the
        # long-running tmux SERVER's own global environment, which a new
        # session forked into an *already-running* server inherits instead
        # of the calling process's current os.environ (verified: a bare
        # os.environ mutation is invisible to a session created in an
        # already-running default-socket server). Both are seeded here so
        # this test actually exercises start_session's "unset
        # PM_RUN_TOKEN;" prefix rather than passing vacuously because the
        # var was never inherited in the first place.
        previous_env = os.environ.get("PM_RUN_TOKEN")
        os.environ["PM_RUN_TOKEN"] = "secret-inherit-test"

        def _restore_env() -> None:
            if previous_env is None:
                os.environ.pop("PM_RUN_TOKEN", None)
            else:
                os.environ["PM_RUN_TOKEN"] = previous_env

        self.addCleanup(_restore_env)

        subprocess.run(
            ["tmux", "set-environment", "-g", "PM_RUN_TOKEN", "secret-inherit-test"], check=False
        )
        self.addCleanup(
            lambda: subprocess.run(["tmux", "set-environment", "-gu", "PM_RUN_TOKEN"], check=False)
        )

        script_path = self.repo / "fake_harness.sh"
        _write_fake_harness(script_path, 'echo "TOKEN_IS=${PM_RUN_TOKEN:-ABSENT}"\nsleep 15')

        name = "pm-test-striptoken-s01a0"
        self._start(name, str(script_path), {})
        self.assertTrue(self._wait_for(lambda: "TOKEN_IS=" in sessions.pane_text(name)))
        self.assertIn("TOKEN_IS=ABSENT", sessions.pane_text(name))


class TestSendPromptCredentialGuard(TmuxSessionTestCase):
    def test_send_prompt_refuses_into_visible_credential_prompt(self) -> None:
        name = "pm-test-sendprompt-credential-s01a0"
        self._start(name, "bash -c 'echo Enter API key to continue; sleep 30'")
        self.assertTrue(self._wait_for(lambda: "Enter API key" in sessions.pane_text(name)))

        prompt_path = self.repo / "prompt.txt"
        prompt_path.write_text("PM_TEST_PROMPT_MARKER\n", encoding="utf-8")

        with self.assertRaises(PmError) as ctx:
            sessions.send_prompt(name, prompt_path)
        self.assertIn("credential", str(ctx.exception).lower())


class TestWaitUntilReady(TmuxSessionTestCase):
    def test_stable_pane_readiness_returns_once_output_settles(self) -> None:
        name = "pm-test-readiness-stable-s01a0"
        self._start(name, "bash -c 'echo READY_BANNER_TEXT; sleep 8'")
        # "fakeharness" has no banner-keyed dispatch, so this exercises the
        # generic stable-pane heuristic directly.
        sessions.wait_until_ready(name, "fakeharness", deadline_seconds=6.0)
        self.assertIn("READY_BANNER_TEXT", sessions.pane_text(name))

    def test_exited_session_raises(self) -> None:
        name = "pm-test-readiness-exited-s01a0"
        self._start(name, "bash -c 'exit 0'")
        with self.assertRaises(PmError) as ctx:
            sessions.wait_until_ready(name, "fakeharness", deadline_seconds=5.0)
        self.assertIn("exited", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

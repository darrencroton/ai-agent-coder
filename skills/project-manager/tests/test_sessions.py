"""Protected behaviours: tmux session lifecycle and the dialog-marker guard.

`scan_hard_stop`, `session_name`, and the env-token assertion run without
tmux. Everything that drives a real pane is gated with
`@unittest.skipUnless(shutil.which("tmux"), ...)` and drives a tiny fake
harness shell script — no real coding CLI is ever launched.

`scan_hard_stop` carries both positive fixtures (one per marker class) and
the negative ones that stop it over-firing: an informational sub-100% usage
warning, a conditional "if you hit your limit", and a marker embedded in an
unrelated word.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Imported for its side effect as much as its helper: pm_test_helpers pins
# PM_TMUX_SOCKET, so both the code under test and the direct tmux calls below
# drive this process's private tmux server rather than the caller's.
from pm_test_helpers import tmux_argv, write_fake_harness

from pm_lib import PmError
from pm_lib import TypedNotSubmitted
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

    def test_qwen_folder_trust_dialog(self) -> None:
        """Pinned to the observed string, not to the marker tuple: qwen's dialog
        defaults to "Trust folder", so a phrasing the tuple stops covering would
        be confirmed by the launch injection's Enter rather than merely missed."""
        result = sessions.scan_hard_stop("Do you trust this folder?\n> 1. Trust folder\n  2. Don't trust")
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

    def test_hyphenated_marker_matches_flush_against_other_characters(self) -> None:
        """"two-factor" is a hyphenated phrase, not a bare word, so it keeps
        substring matching. Bounding it would lose a prompt a pane renders
        flush against padding — the same mid-token wrap case `-J` protects."""
        result = sessions.scan_hard_stop("xxxTwo-factor authentication required")
        self.assertTrue(result["present"])
        self.assertIn("credential_prompt", result["kinds"])

    def test_permission_prompt(self) -> None:
        result = sessions.scan_hard_stop("Grant permission to read this file?")
        self.assertTrue(result["present"])
        self.assertIn("permission_prompt", result["kinds"])


class TestScanHardStopNegativeFixtures(unittest.TestCase):
    def test_claude_code_weekly_limit_banner_is_not_a_dialog(self) -> None:
        """The verbatim banner that stopped a real run. A usage window is an
        operational recovery decision (docs/VISION.md), so PM reads it from the
        pane and decides — waiting out the reset is often the right call, and a
        scan that stopped the run could not offer that."""
        for text in (
            "You've reached 85% of your weekly limit - resets at 12am (Australia/Melbourne)",
            "Weekly usage limit reached. Try again next week.",
            "Subscription plan limit exhausted. Upgrade billing to continue.",
        ):
            with self.subTest(text=text):
                result = sessions.scan_hard_stop(text)
                self.assertFalse(result["present"])
                self.assertEqual(result["kinds"], [])

    def test_domain_vocabulary_is_not_a_dialog(self) -> None:
        """release/publish/deploy/plan/credit are ordinary words in a repository
        that ships software. The retired side-effect and billing regexes fired
        on every line below; a real harness confirmation renders through its
        TUI's own dialog text instead, which the literal markers hold."""
        for text in (
            "Update the release notes for this change?",
            "Does the release config need a new flag?",
            "Confirm the release checklist entries",
            "slice 3 has a rate limit cap of 5 requests",
            "Plan: add credits limit to billing module",
        ):
            with self.subTest(text=text):
                result = sessions.scan_hard_stop(text)
                self.assertFalse(result["present"])
                self.assertEqual(result["kinds"], [])

    def test_bare_word_marker_inside_an_unrelated_word_is_not_stopping(self) -> None:
        """A one-word marker matches on boundaries: "MFA" inside a temp path is
        not a credential prompt, and a false marker refuses every send and ends
        `observe --wait` early."""
        result = sessions.scan_hard_stop("PM_TMPDIR=/tmp/tmpq8mfa2z1/fake.sh")
        self.assertFalse(result["present"])
        self.assertEqual(result["kinds"], [])

    def test_permission_denied_outcome_is_not_a_prompt(self) -> None:
        """An operation that already failed is not a prompt waiting on a human.
        A slice whose own test asserts an unwritable directory puts this phrase
        in the pane, and a false marker there refuses the steer that would fix
        it."""
        for text in (
            "bash: /etc/shadow: Permission denied",
            "'... is not writable: could not create a probe file there: Permission denied'",
        ):
            with self.subTest(text=text):
                result = sessions.scan_hard_stop(text)
                self.assertFalse(result["present"])
                self.assertEqual(result["kinds"], [])

    def test_empty_text_is_not_stopping(self) -> None:
        result = sessions.scan_hard_stop("")
        self.assertFalse(result["present"])
        self.assertEqual(result["kinds"], [])
        self.assertEqual(result["markers"], [])


class TestPostTypingRefusalWithholdsTheEnter(unittest.TestCase):
    """Both injection paths, on the window between typing and the first Enter.

    Asserting only that `TypedNotSubmitted` is raised is not enough: moving the
    scan to *after* the first `C-m` would still raise it, while the dialog had
    already been answered — the precise harm these scans exist to prevent. So
    each case asserts that no `C-m` reached tmux at all. Fully mocked, because
    the behaviour under test is the ORDER of a scan against a keystroke, not
    anything a real pane contributes; a 1s race would be flaky and prove less.
    """

    def _run(self, call):
        clear = {"present": False, "kinds": [], "markers": []}
        dialog = sessions.scan_hard_stop("Enter API key to continue")
        sent: list[tuple[str, ...]] = []

        def record(*args, **kwargs):
            argv = tuple(args[0]) if args and isinstance(args[0], list) else tuple(args)
            sent.append(argv)
            return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")

        with mock.patch.object(sessions, "scan_live_hard_stop", side_effect=[clear, dialog]), \
             mock.patch.object(sessions, "session_exists", return_value=True), \
             mock.patch.object(sessions, "_tmux_or_raise", side_effect=record), \
             mock.patch.object(sessions, "_run_tmux", side_effect=record), \
             mock.patch.object(sessions.time, "sleep"):
            with self.assertRaises(TypedNotSubmitted) as ctx:
                call()
        return sent, str(ctx.exception)

    def test_send_line_types_but_never_presses_enter(self) -> None:
        sent, message = self._run(lambda: sessions.send_line("pm-mocked", "please continue"))
        self.assertTrue(any("please continue" in part for argv in sent for part in argv),
                        "the line should have been typed before the dialog was noticed")
        self.assertFalse([argv for argv in sent if "C-m" in argv],
                         f"no Enter may be sent once a dialog is visible; got {sent!r}")
        self.assertIn("credential_prompt", message)
        self.assertIn("typed but unsubmitted", message)

    def test_send_prompt_types_but_never_presses_enter(self) -> None:
        sent, message = self._run(lambda: sessions.send_prompt("pm-mocked", "read your contract at /x/prompt.md"))
        self.assertFalse([argv for argv in sent if "C-m" in argv],
                         f"no Enter may be sent once a dialog is visible; got {sent!r}")
        self.assertIn("credential_prompt", message)


class TestScanHardStopWrapping(unittest.TestCase):
    def test_credential_prompt_wrapped_across_lines_still_matches(self) -> None:
        wrapped = "Enter API\nkey to continue"
        result = sessions.scan_hard_stop(wrapped)
        self.assertTrue(result["present"])
        self.assertIn("credential_prompt", result["kinds"])


@unittest.skipUnless(_HAS_TMUX, "tmux is required to drive a real pane")
class TestVisiblePaneExcludesScrollback(unittest.TestCase):
    """The scoping the whole change turns on: the keystroke guard reads the
    VISIBLE pane, `pane_text` keeps full scrollback for evidence.

    A dialog answered and scrolled away an hour ago is not a dialog awaiting
    input, but it stays in `pane_text`'s 32k lines forever — and a guard that
    saw it there refused every `send` and `finalize --steer` for the rest of
    the session. Pinned to a short window so the marker is provably pushed off
    screen, since without a forced scroll this passes either way."""

    def test_marker_scrolled_off_screen_is_absent_from_the_visible_pane(self) -> None:
        session = "pm-test-visible-s01a0"
        subprocess.run(
            tmux_argv("new-session", "-d", "-s", session, "-x", "80", "-y", "5",
                      "sh -c 'echo Enter API key to continue; "
                      "for i in 1 2 3 4 5 6 7 8 9 10; do echo filler-$i; done; sleep 30'"),
            check=True, capture_output=True,
        )
        self.addCleanup(sessions.force_stop, session)
        subprocess.run(tmux_argv("set-option", "-t", session, "window-size", "manual"),
                       check=False, capture_output=True)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and "filler-10" not in sessions.pane_text(session):
            time.sleep(0.1)

        # Scrollback still holds it — that is what evidence capture wants.
        self.assertIn("Enter API key", sessions.pane_text(session))
        # The guard's view does not, so it does not refuse.
        self.assertNotIn("Enter API key", sessions.visible_pane_text(session))
        self.assertFalse(sessions.scan_live_hard_stop(session)["present"])


@unittest.skipUnless(_HAS_TMUX, "tmux is required to drive a real pane")
class TestPaneTextRejoinsHardWraps(unittest.TestCase):
    """The capture half of the same guard: whitespace normalization cannot
    repair a marker tmux split MID-TOKEN at the pane edge ("Ente"/"r API
    key" normalizes to "Ente r API key"), so `pane_text` must pass `-J`.
    Driven at a pinned 20-column width because the defect is width-
    dependent: without a forced wrap point this would pass either way."""

    def test_marker_split_mid_token_by_pane_width_is_detected(self) -> None:
        session = "pm-test-hardwrap-s01a0"
        padding = "x" * 19  # pushes the wrap boundary inside "Enter"
        subprocess.run(
            tmux_argv("new-session", "-d", "-s", session, "-x", "20", "-y", "10",
                      f"sh -c 'printf \"{padding}Enter API key to continue\\n\"; sleep 30'"),
            check=True, capture_output=True,
        )
        self.addCleanup(sessions.force_stop, session)
        # The default `window-size` option is "latest", which resizes a
        # window to match the server's most recently active client -
        # silently overriding the -x/-y pin above whenever another tmux
        # client is attached elsewhere on the same server. Pin the window
        # to manual sizing so the 20-column wrap point this test depends
        # on actually holds.
        subprocess.run(
            tmux_argv("set-window-option", "-t", session, "window-size", "manual"),
            check=True, capture_output=True,
        )
        subprocess.run(
            tmux_argv("resize-window", "-t", session, "-x", "20", "-y", "10"),
            check=True, capture_output=True,
        )

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and "continue" not in sessions.pane_text(session):
            time.sleep(0.2)

        # The fixture must still be adversarial: an unjoined capture splits
        # the marker, so this test cannot silently stop pinning anything.
        unjoined = subprocess.run(
            tmux_argv("capture-pane", "-p", "-t", session), capture_output=True, text=True, check=True
        ).stdout
        self.assertNotIn("Enter API key", unjoined)

        joined = sessions.pane_text(session)
        self.assertIn("Enter API key", joined)
        result = sessions.scan_hard_stop(joined)
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

    def test_sessions_for_run_finds_every_slice_and_attempt_of_that_run(self) -> None:
        run_id = "pm-test-run-sweep"
        name_a = sessions.session_name(run_id, 1, 0)
        name_b = sessions.session_name(run_id, 2, 3)
        self._start(name_a, "bash -c 'sleep 30'")
        self._start(name_b, "bash -c 'sleep 30'")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name_a) and sessions.session_exists(name_b)))
        found = sessions.sessions_for_run(run_id)
        self.assertIn(name_a, found)
        self.assertIn(name_b, found)

    def test_sessions_for_run_does_not_match_a_run_whose_id_extends_this_one(self) -> None:
        """`pm-<run_id>` as a string prefix cannot tell run `X` from run `X-2`,
        and `-2` is exactly the suffix `state.new_run_id` appends on a local id
        collision. Reaping (`start-slice`) and stopping both sweep by run, so a
        prefix match there would kill a different, live run's Developer
        session."""
        run_id = "pm-test-run-boundary"
        mine = sessions.session_name(run_id, 1, 0)
        theirs = sessions.session_name(f"{run_id}-2", 1, 0)
        self._start(mine, "bash -c 'sleep 30'")
        self._start(theirs, "bash -c 'sleep 30'")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(mine) and sessions.session_exists(theirs)))

        self.assertEqual(sessions.sessions_for_run(run_id), [mine])
        self.assertEqual(sessions.sessions_for_run(f"{run_id}-2"), [theirs])

    def test_sessions_for_run_ignores_unrelated_session_names(self) -> None:
        run_id = "pm-test-run-unrelated"
        mine = sessions.session_name(run_id, 1, 0)
        stray = f"pm-{run_id}-not-a-slice-session"
        self._start(mine, "bash -c 'sleep 30'")
        self._start(stray, "bash -c 'sleep 30'")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(mine) and sessions.session_exists(stray)))
        self.assertEqual(sessions.sessions_for_run(run_id), [mine])

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
    def test_send_prompt_submits_the_pointer_not_just_types_it(self) -> None:
        name = "pm-test-sendprompt-s01a0"
        # Echoes SUBMITTED:<line> only after reading a newline, so the marker
        # proves the pointer was actually submitted (an Enter landed), not
        # merely echoed by the tty as it would be with a bare `cat -`.
        self._start(name, "sh -c 'read line; echo SUBMITTED:$line; sleep 30'")
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name)))

        sessions.send_prompt(name, "read your contract at /x/prompt.md POINTER_MARKER_XYZ")

        self.assertTrue(
            self._wait_for(
                lambda: "SUBMITTED:" in sessions.pane_text(name) and "POINTER_MARKER_XYZ" in sessions.pane_text(name)
            )
        )

    def test_send_prompt_withholds_second_enter_when_a_hard_stop_appears(self) -> None:
        name = "pm-test-sendprompt-rescan-s01a0"
        # After reading the pointer (first Enter), the harness reveals a
        # credential prompt, then does a TIMED read for a second line: it
        # prints GOT_SECOND_ENTER if one arrives, NO_SECOND_ENTER if it times
        # out. The settle-and-rescan must withhold the second Enter, so
        # NO_SECOND_ENTER is the expected positive outcome. Waiting for that
        # sentinel — rather than asserting absence immediately — makes the
        # check race-robust: a broken impl that DID send the second Enter
        # would print GOT_SECOND_ENTER before the timeout instead.
        self._start(
            name,
            "bash -c 'read a; echo Enter API key to continue; "
            "if read -t 3 b; then echo GOT_SECOND_ENTER; else echo NO_SECOND_ENTER; fi; sleep 30'",
        )
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name)))

        sessions.send_prompt(name, "read your contract at /x/prompt.md")

        self.assertTrue(self._wait_for(lambda: "NO_SECOND_ENTER" in sessions.pane_text(name), timeout=8.0))
        self.assertNotIn("GOT_SECOND_ENTER", sessions.pane_text(name))

    def test_send_prompt_refuses_multiline_pointer(self) -> None:
        # A newline would mean the multi-KB contract leaked into the launch
        # message instead of the prompt.md file it must point to.
        with self.assertRaises(PmError):
            sessions.send_prompt("pm-test-doesnt-matter-s01a0", "line one\nline two")


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

    def test_send_line_withholds_second_enter_when_a_hard_stop_appears(self) -> None:
        # The steer/nudge counterpart of the send_prompt case above: the first
        # Enter can itself surface a credential prompt, and a blind second
        # would answer it. NO_SECOND_ENTER is the expected positive outcome;
        # waiting for that sentinel keeps the check race-robust, since a
        # broken impl prints GOT_SECOND_ENTER before the timeout instead.
        name = "pm-test-sendline-rescan-s01a0"
        self._start(
            name,
            "bash -c 'read a; echo Enter API key to continue; "
            "if read -t 3 b; then echo GOT_SECOND_ENTER; else echo NO_SECOND_ENTER; fi; sleep 30'",
        )
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(name)))

        sessions.send_line(name, "please continue")

        self.assertTrue(self._wait_for(lambda: "NO_SECOND_ENTER" in sessions.pane_text(name), timeout=8.0))
        self.assertNotIn("GOT_SECOND_ENTER", sessions.pane_text(name))


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

        # Scoped to this process's private tmux server (pm_test_helpers pins
        # PM_TMUX_SOCKET), so seeding a SERVER-global variable cannot reach the
        # caller's own tmux server. Restore whatever the variable was rather
        # than unconditionally unsetting it: `-gu` on a shared server would
        # silently delete a value the suite did not set.
        previous_server_env = subprocess.run(
            tmux_argv("show-environment", "-g", "PM_RUN_TOKEN"),
            check=False, capture_output=True, text=True,
        )
        had_server_env = previous_server_env.returncode == 0 and "=" in previous_server_env.stdout
        previous_server_value = (
            previous_server_env.stdout.strip().split("=", 1)[1] if had_server_env else None
        )

        def _restore_server_env() -> None:
            if previous_server_value is None:
                subprocess.run(
                    tmux_argv("set-environment", "-gu", "PM_RUN_TOKEN"),
                    check=False, capture_output=True,
                )
            else:
                subprocess.run(
                    tmux_argv("set-environment", "-g", "PM_RUN_TOKEN", previous_server_value),
                    check=False, capture_output=True,
                )

        subprocess.run(
            tmux_argv("set-environment", "-g", "PM_RUN_TOKEN", "secret-inherit-test"),
            check=False, capture_output=True,
        )
        self.addCleanup(_restore_server_env)

        script_path = self.repo / "fake_harness.sh"
        write_fake_harness(script_path, 'echo "TOKEN_IS=${PM_RUN_TOKEN:-ABSENT}"\nsleep 15')

        name = "pm-test-striptoken-s01a0"
        self._start(name, str(script_path), {})
        self.assertTrue(self._wait_for(lambda: "TOKEN_IS=" in sessions.pane_text(name)))
        self.assertIn("TOKEN_IS=ABSENT", sessions.pane_text(name))


class TestSendPromptCredentialGuard(TmuxSessionTestCase):
    def test_send_prompt_refuses_into_visible_credential_prompt(self) -> None:
        name = "pm-test-sendprompt-credential-s01a0"
        self._start(name, "bash -c 'echo Enter API key to continue; sleep 30'")
        self.assertTrue(self._wait_for(lambda: "Enter API key" in sessions.pane_text(name)))

        with self.assertRaises(PmError) as ctx:
            sessions.send_prompt(name, "read your contract at /x/prompt.md")
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

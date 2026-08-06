"""Protected behaviours: the Stage 3 slice lifecycle commands (evidence, not
acceptance).

Everything here drives `pm_lib.cli.main` in-process (via `run_cli_in_repo`),
matching an operator invoking the `pm` CLI from inside the working tree.
No real coding CLI is ever launched — tmux-gated scenarios drive a tiny
fake-harness `sh` script (`pm_test_helpers.write_fake_harness`), matching
the retained fake-harness pattern (replacement-ledger §9.1/§9.3). Pins:

1. `init` happy path: creates run state and prints the run capability token
   exactly once; writes the `.pm/` skeleton and a self-ignoring
   `.pm/.gitignore`; slice entries carry `plan_risk`; check-plan warnings
   are printed and the run still proceeds; an `init` event is recorded.
   Re-running `init` while a run already exists creates a SECOND run and
   repoints `current` — both run directories survive.
2. `init` failures, each exiting 2 with nothing created: a plan with
   errors; a dirty worktree; an unknown harness with no `--harness-command`
   override; `--attest` naming an unknown slice id; `--branch` naming a
   branch that does not exist. `--create-branch` succeeds: it creates the
   branch and switches to it.
3. Token gating: `approve`/`start-slice`/`send`/`finalize`/`stop` each exit
   2 with a "token required" message when no token is supplied (flag or
   `PM_RUN_TOKEN`); a wrong token exits 2 with a plain (non-INTEGRITY)
   message; a hand-tampered `run.json` makes every one of those commands
   exit 2 with an `INTEGRITY:`-prefixed message. `status` and `observe`
   still work with no token at all.
4. `approve`: records reason + timestamp for an approval-flagged slice; a
   non-gated slice is refused; a slice with an unclear approval flag is
   refused even though it is not exactly "no".
5. The full fake-harness flow — bare `finalize` printing eight PASS facts
   and evidence paths without mutating state — is pinned in
   `test_finalize.py`'s `TestFullAcceptance`, which already drives the same
   launch through to acceptance.
6. `finalize` with a floor failure (the fake harness also touches an
   unauthorized file): exits 1, the surface fact prints FAIL, a `floor`
   event is recorded.
7. Attempt accounting (tmux): `start-slice`, kill the session (simulate a
   dead harness), `start-slice` again → a relaunch, and `attempts` reads
   back as 1 from a **fresh** `status`/state load (the persistence AC);
   the prior attempt's `result.json` is rotated into `attempt-0/`;
   exhausting the budget (`--max-attempts 1`) refuses the next relaunch,
   sets `needs-human`, and exits 2.
8. Mid-run plan edit: `init`, edit the plan file, `start-slice` → exits 2,
   run status becomes `needs-human`, a `plan-changed` event is recorded.
9. Dead session: `observe` reports the session as not running (never
   raises); `send` refuses to drive it.
10. `send` nudge (tmux): on ONE launched session, a steered line reaches
    the pane and is recorded as a `send` event without touching `attempts`;
    then, once a trigger-gated credential prompt becomes visible, the same
    command is refused by the sessions hard-stop floor.
11. `stop` (tmux): captures `pane.txt`, kills the run's sessions, sets
    status `stopped` with the given reason. `stop --scavenge` against a
    **deleted** state directory still finds and kills a stray
    `pm-<run-id>-…` session and exits 0.
12. All slices already complete: `start-slice` completes the run, prints a
    completion message, and exits 0 without launching a session (asserted
    against a patched `start_session`, not merely implied).
13. A failed launch leaves no orphan session (tmux): when readiness or
    prompt injection raises, the session started moments earlier is killed
    rather than stranded outside `current_slice`, where no command could
    see it.
14. Real-harness composition (tmux): `start-slice` with no
    `--harness-command` override composes an actual codex launch argv.
    Every other lifecycle scenario overrides the command, so this is the
    only test that executes that branch at all.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import shutil

from pm_test_helpers import (
    TmuxRunTestCase,
    commit_and_result_script,
    idle_script,
    parse_init_output,
    result_only_script,
    trigger_gated_churn_script,
    trigger_gated_credential_prompt_script,
    trigger_gated_exit_script,
    trigger_gated_result_script,
    write_fake_harness,
)

from pm_lib import PmError
from pm_lib import sessions
from pm_lib import slice_ops
from pm_lib import state as state_mod

_HAS_TMUX = shutil.which("tmux") is not None


# --- shared base -------------------------------------------------------------
#
# The feature-branch repo, session reaper, wait helpers, `_plan_path`, and
# `_init` all live on `TmuxRunTestCase` in pm_test_helpers, shared with
# test_finalize.


SliceOpsTestCase = TmuxRunTestCase


# --- 1. init happy path --------------------------------------------------


class TestInitHappyPath(SliceOpsTestCase):
    def test_init_creates_state_pm_skeleton_and_prints_token_once(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["requirements.txt"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())

        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)

        run_id, token = parse_init_output(out)
        self.assertEqual(out.count("PM_RUN_TOKEN="), 1)
        self.assertIn("Keep this token out of Developer sessions", out)
        # A dependency-shaped surface entry is a warning, not an error —
        # the run proceeds and the warning is still printed.
        self.assertIn("WARNING", out)

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["slices"][0]["plan_risk"], state["slices"][0]["risk"])

        pm_dir = self.repo / ".pm"
        self.assertTrue((pm_dir / ".gitignore").is_file())
        self.assertEqual((pm_dir / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertTrue((pm_dir / "runs" / run_id / "slices").is_dir())

        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "init" for event in events))

    def test_reinit_creates_second_run_and_repoints_current(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())

        code1, out1, _err1 = self._init(plan_path, harness)
        self.assertEqual(code1, 0)
        run_id1, _token1 = parse_init_output(out1)

        code2, out2, _err2 = self._init(plan_path, harness)
        self.assertEqual(code2, 0)
        run_id2, _token2 = parse_init_output(out2)

        self.assertNotEqual(run_id1, run_id2)
        self.assertEqual(state_mod.resolve_run_dir(self.repo).name, run_id2)
        self.assertTrue(state_mod.resolve_run_dir(self.repo, run_id1).is_dir())
        self.assertTrue(state_mod.resolve_run_dir(self.repo, run_id2).is_dir())


# --- 2. init failures -----------------------------------------------------


class TestInitFailures(SliceOpsTestCase):
    def test_plan_with_errors_exits_two_nothing_created(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": None}])  # empty authorized surface -> error
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())

        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertFalse((self.repo / ".pm").exists())
        pointer = state_mod.state_root(self.repo) / "current"
        self.assertFalse(pointer.exists())

    def test_dirty_worktree_exits_two(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        (self.repo / "untracked.txt").write_text("oops\n", encoding="utf-8")
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())

        code, _out, err = self._init(plan_path, harness)
        self.assertEqual(code, 2)
        self.assertIn("dirty", err)
        self.assertFalse((self.repo / ".pm").exists())

    def test_unknown_harness_without_override_exits_two(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        code, _out, err = self.run_cli_in_repo(
            ["init", "--repo", str(self.repo), "--plan", str(plan_path), "--harness", "not-a-real-harness"]
        )
        self.assertEqual(code, 2)
        self.assertIn("no PM harness profile", err)
        self.assertFalse((self.repo / ".pm").exists())

    def test_attest_unknown_slice_exits_two_nothing_created(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, _out, err = self._init(plan_path, harness, extra=["--attest", "Slice 99"])
        self.assertEqual(code, 2)
        self.assertIn("unknown slice", err)
        self.assertFalse((self.repo / ".pm").exists())

    def test_branch_nonexistent_exits_two(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, _out, err = self._init(plan_path, harness, extra=["--branch", "does-not-exist"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)

    def test_create_branch_creates_and_switches(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, out, _err = self._init(plan_path, harness, extra=["--create-branch", "feature/new-branch"])
        self.assertEqual(code, 0)
        self.assertIn("feature/new-branch", out)
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(result.stdout.strip(), "feature/new-branch")

    def test_default_onto_main_refused_but_explicit_branch_main_allowed(self) -> None:
        # Implicitly landing every slice commit on the default branch is the
        # PM Test 20 footgun; refuse it, but honour an explicit --branch main.
        self._git("checkout", "-q", "main")
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())

        code, _out, err = self._init(plan_path, harness)
        self.assertEqual(code, 2)
        self.assertIn("main", err)

        code, out, _err = self._init(plan_path, harness, extra=["--branch", "main"])
        self.assertEqual(code, 0)
        self.assertIn("branch: main", out)


# --- 3. token gating -------------------------------------------------------


class TestTokenGating(SliceOpsTestCase):
    def _make_gated_run(self):
        plan_path = self.write_plan(self._plan_path())
        return self.make_run(plan_path=plan_path)

    def test_missing_token_exits_two_for_every_mutating_command(self) -> None:
        _state, _token, _run_dir = self._make_gated_run()
        cases = [
            ["approve", "--slice", "Slice 1", "--reason", "ok"],
            ["start-slice"],
            ["send", "--text", "hi", "--reason", "steer"],
            ["finalize"],
            ["stop", "--reason", "done"],
        ]
        for argv in cases:
            with self.subTest(command=argv[0]):
                code, _out, err = self.run_cli_in_repo(argv)
                self.assertEqual(code, 2)
                self.assertIn("token required", err)

    def test_wrong_token_exits_two_plain_message(self) -> None:
        _state, _token, _run_dir = self._make_gated_run()
        code, _out, err = self.run_cli_in_repo(
            ["approve", "--slice", "Slice 1", "--reason", "ok", "--token", "not-the-real-token"]
        )
        self.assertEqual(code, 2)
        self.assertNotIn("INTEGRITY", err)

    def test_tampered_state_makes_every_mutating_command_exit_two_with_integrity_prefix(self) -> None:
        _state, token, run_dir = self._make_gated_run()

        cases = [
            ["approve", "--slice", "Slice 1", "--reason", "ok", "--token", token],
            ["start-slice", "--token", token],
            ["send", "--text", "hi", "--reason", "steer", "--token", token],
            ["finalize", "--token", token],
            ["stop", "--reason", "done", "--token", token],
        ]
        current_raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        current_raw["stop_reason"] = "tamper-marker"
        tampered_bytes = json.dumps(current_raw, indent=2, sort_keys=True) + "\n"
        (run_dir / "run.json").write_text(tampered_bytes, encoding="utf-8")
        for argv in cases:
            with self.subTest(command=argv[0]):
                code, _out, err = self.run_cli_in_repo(argv)
                self.assertEqual(code, 2)
                self.assertIn("INTEGRITY:", err)
        # Tampering is terminal by construction: no command may heal or
        # re-sign the unauthenticated bytes (re-signing would launder
        # attacker-controlled state into MAC-valid state), so the tampered
        # file must survive verbatim and keep failing closed.
        self.assertEqual((run_dir / "run.json").read_text(encoding="utf-8"), tampered_bytes)
        code, _out, err = self.run_cli_in_repo(["finalize", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("INTEGRITY:", err)

    def test_status_and_observe_work_without_a_token(self) -> None:
        _state, _token, _run_dir = self._make_gated_run()
        code, _out, _err = self.run_cli_in_repo(["status"])
        self.assertEqual(code, 0)
        code, _out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)

    def test_status_verifies_state_when_token_supplied(self) -> None:
        _state, token, run_dir = self._make_gated_run()
        current_raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        current_raw["stop_reason"] = "tamper-marker-status"
        tampered_bytes = json.dumps(current_raw, indent=2, sort_keys=True) + "\n"
        (run_dir / "run.json").write_text(tampered_bytes, encoding="utf-8")

        code, _out, err = self.run_cli_in_repo(["status", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("INTEGRITY", err)

        # Plain status (no --token, no PM_RUN_TOKEN in env) skips MAC
        # verification and still succeeds against the same tampered file.
        previous = os.environ.pop("PM_RUN_TOKEN", None)
        try:
            code, _out, _err = self.run_cli_in_repo(["status"])
            self.assertEqual(code, 0)
        finally:
            if previous is not None:
                os.environ["PM_RUN_TOKEN"] = previous


# --- 4. approve -------------------------------------------------------------


class TestApprove(SliceOpsTestCase):
    def test_records_reason_and_timestamp(self) -> None:
        plan_path = self.write_plan(slices=[{"approval": "yes"}])
        _state, token, run_dir = self.make_run(plan_path=plan_path)
        code, out, _err = self.run_cli_in_repo(
            ["approve", "--slice", "Slice 1", "--reason", "reviewed by human", "--token", token]
        )
        self.assertEqual(code, 0)
        self.assertIn("Slice 1", out)
        loaded = state_mod.load_state(run_dir, token)
        record = loaded["approvals"]["Slice 1"]
        self.assertEqual(record["reason"], "reviewed by human")
        self.assertIn("T", record["at"])

    def test_non_gated_slice_refused(self) -> None:
        plan_path = self.write_plan(slices=[{"approval": "no"}])
        _state, token, _run_dir = self.make_run(plan_path=plan_path)
        code, _out, err = self.run_cli_in_repo(
            ["approve", "--slice", "Slice 1", "--reason", "why not", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("not approval-gated", err)

    def test_unclear_approval_flag_refused(self) -> None:
        plan_path = self.repo.parent / "plan.md"
        body = (
            "# Test Plan\n\n"
            "## Slice 1: title\n\n"
            "### Intended Change\nDo the thing.\n\n"
            "### Acceptance Criteria\nIt works.\n\n"
            "### Authorized Surface\n- Files allowed to change:\n  - a.py\n"
            "- Functions/classes/components allowed to change: none.\n"
            "- Tests allowed or expected to change: none.\n\n"
            "### Explicit Non-Goals\nNothing else.\n\n"
            "### Risk Flags\n- Risky surfaces touched: none.\n"
            "- Approval needed before implementation: not yet decided.\n"
            "- Independent audit required: no.\n\n"
            "### Validation Plan\nRun the tests.\n\n"
            "### Rollback Path\ngit revert.\n\n"
        )
        plan_path.write_text(body, encoding="utf-8")
        _state, token, _run_dir = self.make_run(plan_path=plan_path)
        code, _out, err = self.run_cli_in_repo(
            ["approve", "--slice", "Slice 1", "--reason", "trying anyway", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("not approval-gated", err)


# --- 5/6/7/9/10/11/12: tmux-gated flows --------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestFinalizeFloorFailure(SliceOpsTestCase):
    def test_unauthorized_file_change_fails_finalize(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh",
            commit_and_result_script(self.repo, unauthorized_file="b.py", delay=1.0, tail_sleep=2.0),
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        self.assertTrue(
            self._wait_for(
                lambda: (Path(state_mod.load_state(run_dir, token)["current_slice"]["artifact_dir"]) / "result.json").is_file(),
                timeout=15.0,
            )
        )

        code, out, _err = self.run_cli_in_repo(["finalize", "--token", token])
        self.assertEqual(code, 1)
        self.assertRegex(out, re.compile(r"^5 surface FAIL", re.MULTILINE))

        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "floor" and "surface" in event["note"] for event in events))


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestAttemptAccounting(SliceOpsTestCase):
    def test_relaunch_persists_attempts_rotates_prior_result_and_exhausts_budget(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", result_only_script(delay=0.5, tail_sleep=30.0))
        code, out, _err = self._init(plan_path, harness, extra=["--max-attempts", "1"])
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        # Attempt 0: launch, let it write a (stale, to-be-superseded) result.
        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session0 = self._track_current_session(run_id, token)
        self.assertIsNotNone(session0)
        artifact_dir = Path(state_mod.load_state(run_dir, token)["current_slice"]["artifact_dir"])
        self.assertTrue(self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=10.0))

        # Simulate a dead harness: force-kill the still-running session.
        sessions.force_stop(session0)
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session0), timeout=10.0))

        # Relaunch: attempts becomes 1 (within budget 1), prior result rotated.
        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0, out)
        self.assertIn("relaunched", out)
        session1 = self._track_current_session(run_id, token)

        # Fresh state load in a new call: attempts persisted as 1.
        reloaded = state_mod.load_state(run_dir, token)
        self.assertEqual(reloaded["current_slice"]["attempts"], 1)
        by_id = {entry["id"]: entry for entry in reloaded["slices"]}
        self.assertEqual(by_id["Slice 1"]["attempts"], 1)
        # Attempt 0's result.json was rotated out of the way before the
        # relaunch — a stale completion signal can never be mistaken for
        # the new attempt's. (Attempt 1's own script may have already
        # written a fresh result.json of its own by now, which is correct
        # and expected — this only asserts the OLD one was moved aside.)
        self.assertTrue((artifact_dir / "attempt-0" / "result.json").is_file())

        sessions.force_stop(session1)
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session1), timeout=10.0))

        # Second relaunch would need attempts=2 > max_attempts=1: refused.
        code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("attempt budget exhausted", err)
        final_state = state_mod.load_state(run_dir, token)
        self.assertEqual(final_state["status"], "needs-human")


class TestMidRunPlanEdit(SliceOpsTestCase):
    def test_plan_edited_mid_run_stops_before_next_slice(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        with plan_path.open("a", encoding="utf-8") as handle:
            handle.write("\n<!-- edited mid-run -->\n")

        code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("plan file changed mid-run", err)

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "needs-human")
        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "plan-changed" for event in events))


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestDeadSession(SliceOpsTestCase):
    def test_observe_reports_not_running_and_send_refuses(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        # Trigger-gated rather than a fixed post-launch delay: the session must
        # still be alive when `start-slice` injects, and a launch-relative
        # timer left barely a second of margin over the launch itself.
        trigger = self.repo.parent / "exit_trigger"
        harness = write_fake_harness(self.repo.parent / "fake.sh", trigger_gated_exit_script(trigger))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertIsNotNone(session)

        trigger.write_text("go\n", encoding="utf-8")
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session), timeout=15.0))

        code, out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)
        self.assertIn("session running: False", out)

        code, _out, err = self.run_cli_in_repo(["send", "--text", "hello", "--reason", "nudge", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("no live session", err)


class TestEpisodeTimeouts(unittest.TestCase):
    """`episode_timeouts` — the backwards scan behind the repeat-wait hint.

    Pure over an event list, so it is tested here rather than through a live
    session: the shapes that matter (a legacy log, a slice boundary, a signal
    mid-streak) are one literal list each.

    It is advisory only. Nothing it returns changes an exit code, which is why
    it is allowed to take no lock and to stop early on anything it cannot
    classify — a wrong count prints a slightly wrong hint, and that is the
    worst case by construction.
    """

    @staticmethod
    def _observe(slice_id: str, wake: str, elapsed: float = 1200.0) -> dict:
        return {
            "ts": "2026-08-04T12:00:00Z",
            "kind": "observe",
            "slice": slice_id,
            "note": f"requested=1200s elapsed={elapsed:.1f}s wake={wake}",
        }

    def test_no_events_is_zero(self) -> None:
        self.assertEqual(slice_ops.episode_timeouts([], "Slice 1"), (0, 0.0))

    def test_consecutive_timeouts_accumulate(self) -> None:
        events = [
            {"kind": "launch", "slice": "Slice 1", "note": ""},
            self._observe("Slice 1", "timeout", 900.0),
            self._observe("Slice 1", "timeout", 1200.0),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1"), (2, 2100.0))

    def test_a_signal_ends_the_streak(self) -> None:
        events = [
            {"kind": "launch", "slice": "Slice 1", "note": ""},
            self._observe("Slice 1", "timeout"),
            self._observe("Slice 1", "result"),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1"), (0, 0.0))

    def test_an_episode_reset_ends_the_streak(self) -> None:
        events = [
            self._observe("Slice 1", "timeout"),
            self._observe("Slice 1", "timeout"),
            {"kind": "steer", "slice": "Slice 1", "note": "fix it"},
            self._observe("Slice 1", "timeout"),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1")[0], 1)

    def test_a_send_resets_it(self) -> None:
        events = [
            self._observe("Slice 1", "timeout"),
            {"kind": "send", "slice": "Slice 1", "note": "nudge"},
            self._observe("Slice 1", "timeout"),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1")[0], 1)

    def test_intervening_non_reset_events_are_skipped(self) -> None:
        """A review or floor check does not give the Developer anything new to
        do, so it neither starts a fresh episode nor breaks the streak."""
        events = [
            {"kind": "launch", "slice": "Slice 1", "note": ""},
            self._observe("Slice 1", "timeout"),
            {"kind": "floor", "slice": "Slice 1", "note": "8/8 passed"},
            self._observe("Slice 1", "timeout"),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1")[0], 2)

    def test_another_slices_observe_stops_the_scan(self) -> None:
        events = [
            self._observe("Slice 1", "timeout"),
            self._observe("Slice 2", "timeout"),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1")[0], 0)

    def test_a_legacy_observe_stops_the_scan_rather_than_being_guessed(self) -> None:
        """Events written before `wake=` existed carry no return cause. Treating
        them as timeouts would invent a streak; treating them as signals would
        hide one. Stopping is the only honest option."""
        events = [
            {"kind": "observe", "slice": "Slice 1",
             "note": "pane_changed=True running=True result_present=False elapsed=1200.0s"},
            self._observe("Slice 1", "timeout"),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1")[0], 1)

    def test_a_missing_elapsed_still_counts_the_wait(self) -> None:
        events = [{"kind": "observe", "slice": "Slice 1", "note": "wake=timeout"}]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1"), (1, 0.0))

    def test_an_untimed_peek_neither_counts_nor_breaks_the_streak(self) -> None:
        """A bare `observe` is a glance, not a wait. Counting it would invent a
        wait nobody requested; letting it break the streak would let a glance
        between two long waits silently suppress the hint."""
        events = [
            {"kind": "launch", "slice": "Slice 1", "note": ""},
            self._observe("Slice 1", "timeout"),
            self._observe("Slice 1", "immediate", 0.0),
            self._observe("Slice 1", "timeout"),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1"), (2, 2400.0))

    def test_only_untimed_peeks_produce_no_streak(self) -> None:
        events = [
            {"kind": "launch", "slice": "Slice 1", "note": ""},
            self._observe("Slice 1", "immediate", 0.0),
            self._observe("Slice 1", "immediate", 0.0),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 1"), (0, 0.0))

    def test_an_accept_ends_the_episode(self) -> None:
        events = [
            self._observe("Slice 1", "timeout"),
            {"kind": "accept", "slice": "Slice 1", "note": "ACCEPT"},
            self._observe("Slice 2", "timeout"),
        ]
        self.assertEqual(slice_ops.episode_timeouts(events, "Slice 2")[0], 1)


_WAITED_RE = re.compile(r"^waited:\s*([\d.]+)s \(requested ([\d.]+)s\)$", re.MULTILINE)


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestObserveWaitSemantics(SliceOpsTestCase):
    """`observe --wait` honest-wait semantics (target-design §12, Amended
    post-implementation): the wait runs the full requested duration and
    breaks early ONLY on session death, `result.json` appearing, or a
    hard-stop marker — never on a mere pane byte-change."""

    def _observe_wait(self, wait_seconds: float) -> tuple[int, str, str, float, float]:
        """Run `observe --wait` and return (code, out, err, test_elapsed,
        reported_elapsed). `test_elapsed` is measured test-side with
        `time.monotonic()` around the CLI call — the production-reported
        `elapsed_seconds` (parsed from stdout) is untrustworthy as the sole
        signal, since a broken observe that returned instantly but printed
        the full duration would otherwise pass. Timing assertions must be
        based on `test_elapsed`; `reported_elapsed` is only cross-checked
        against it (see test_cosmetic_pane_churn_does_not_end_wait_early)."""
        start = time.monotonic()
        code, out, err = self.run_cli_in_repo(["observe", "--wait", str(wait_seconds)])
        test_elapsed = time.monotonic() - start
        match = _WAITED_RE.search(out)
        self.assertIsNotNone(match, out)
        return code, out, err, test_elapsed, float(match.group(1))

    def _launch(self, harness_body: str) -> tuple[str, str]:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", harness_body)
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        return run_id, token

    def _wait_then_trigger(self, trigger: Path, *, wait_seconds: float = 20.0) -> dict:
        """Run `observe --wait` on a thread, wait until it has actually polled
        at least once, then fire `trigger`.

        The "has polled" gate is a `threading.Event` set from a wrapper around
        `sessions.detect_activity`, which every poll calls. Sleeping a beat and
        inferring the poll from elapsed time would not do: the worker starts
        its clock before entering the CLI, so a scheduling or setup stall could
        satisfy the lower bound with `observe` not yet having looked at
        anything — and the whole point of these tests is that the event is
        detected MID-wait rather than found already true on the first look.

        `elapsed < wait_seconds` then proves the wait broke early. Elapsed is
        measured test-side, so an `observe` that returned instantly while
        printing a full elapsed value cannot pass.
        """
        polled = threading.Event()
        real_detect_activity = sessions.detect_activity

        def _detect_and_signal(*args, **kwargs):
            try:
                return real_detect_activity(*args, **kwargs)
            finally:
                polled.set()

        result: dict = {}

        def _run() -> None:
            start = time.monotonic()
            code, out, err = self.run_cli_in_repo(["observe", "--wait", str(wait_seconds)])
            result["elapsed"] = time.monotonic() - start
            result["code"], result["out"], result["err"] = code, out, err

        with mock.patch.object(sessions, "detect_activity", _detect_and_signal):
            thread = threading.Thread(target=_run)
            thread.start()
            self.assertTrue(polled.wait(timeout=30.0), "observe --wait never polled the session")
            trigger.write_text("go\n", encoding="utf-8")
            thread.join(timeout=wait_seconds + 15.0)
            self.assertFalse(thread.is_alive(), "observe --wait did not return in time")

        self.assertEqual(result["code"], 0, result.get("err"))
        self.assertLess(result["elapsed"], wait_seconds)
        return result

    def test_cosmetic_pane_churn_does_not_end_wait_early(self) -> None:
        from pm_lib.slice_ops import _OBSERVE_POLL_SECONDS

        # Churn starts only once the trigger exists, so readiness can settle
        # on a still pane first; it then runs continuously for the whole wait,
        # which is the condition under test.
        trigger = self.repo.parent / "churn_trigger"
        self._launch(trigger_gated_churn_script(trigger))
        trigger.write_text("go\n", encoding="utf-8")

        wait_seconds = 3 * _OBSERVE_POLL_SECONDS
        code, out, _err, test_elapsed, reported_elapsed = self._observe_wait(wait_seconds)
        self.assertEqual(code, 0)
        self.assertIn("session running: True", out)
        self.assertIn("result present: False", out)
        # A stray early break would return in ~one poll cycle; the wait must
        # instead run to (near) the full requested duration despite the
        # pane changing on every poll. This is the TEST-SIDE measurement, so
        # a broken observe that returns instantly but prints a fabricated
        # elapsed value cannot pass.
        self.assertGreaterEqual(test_elapsed, wait_seconds - 0.5)
        self.assertLess(test_elapsed, wait_seconds + _OBSERVE_POLL_SECONDS + 3.0)
        # The production-reported value must not be fabricated either: it
        # should track the test-side measurement within a sane delta.
        self.assertLess(abs(reported_elapsed - test_elapsed), 2.0)

    def test_result_json_appearing_mid_wait_ends_wait_early(self) -> None:
        trigger = self.repo.parent / "result_trigger"
        self._launch(trigger_gated_result_script(trigger))
        result = self._wait_then_trigger(trigger)
        self.assertIn("result present: True", result["out"])

    def test_session_death_mid_wait_ends_wait_early(self) -> None:
        trigger = self.repo.parent / "exit_trigger"
        self._launch(trigger_gated_exit_script(trigger))
        result = self._wait_then_trigger(trigger)
        self.assertIn("session running: False", result["out"])

    def test_hard_stop_marker_mid_wait_ends_wait_early(self) -> None:
        trigger = self.repo.parent / "credential_trigger"
        self._launch(trigger_gated_credential_prompt_script(trigger))
        result = self._wait_then_trigger(trigger)
        self.assertIn("session running: True", result["out"])
        self.assertIn("hard-stop scan:", result["out"])
        self.assertNotIn("hard-stop scan: clear", result["out"])

    def test_every_observe_logs_and_repeats_are_flagged(self) -> None:
        """Two properties on one launched session, because each costs a full
        `start-slice`.

        1. Every completed observe appends an event. Previously an event was
           written only when the pane, liveness, or result changed — so the
           no-op wait, the one worth counting, was the one that left no trace,
           and a run's own log understated its polling.
        2. From the second consecutive no-signal wait the CLI says so. It is a
           printed note, never a refusal: measured across two real runs the
           repeated waits were 20-minute waits on Developers that needed 85
           minutes, so every one of them was individually the right call and
           only the length was wrong. Refusing would have blocked correct
           behaviour; the exit code therefore stays 0.
        """
        from pm_lib.slice_ops import _OBSERVE_POLL_SECONDS

        run_id, _token = self._launch(idle_script(sleep_seconds=120.0))
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        before = len([e for e in state_mod.read_events(run_dir) if e["kind"] == "observe"])

        wait_seconds = 2 * _OBSERVE_POLL_SECONDS
        code, first_out, _err = self.run_cli_in_repo(["observe", "--wait", str(wait_seconds)])
        self.assertEqual(code, 0)
        self.assertNotIn("note:", first_out, "the first wait of an episode is never a repeat")

        code, second_out, _err = self.run_cli_in_repo(["observe", "--wait", str(wait_seconds)])
        self.assertEqual(code, 0, "the hint must never change the exit code")
        self.assertIn("2 consecutive waits", second_out)
        self.assertIn("--wait", second_out, "the hint must name a concrete larger wait")

        observes = [e for e in state_mod.read_events(run_dir) if e["kind"] == "observe"]
        self.assertEqual(len(observes) - before, 2, "every completed observe must log")
        for event in observes[-2:]:
            self.assertIn("wake=timeout", event["note"])
            self.assertIn("requested=", event["note"])
            # A pane byte-change is deliberately NOT a signal: the wait loop
            # ignores it, so classifying churn as informative would reset the
            # streak on exactly the noise this exists to see through.
            self.assertNotIn("wake=pane", event["note"])

    def test_telemetry_failure_never_costs_the_observation(self) -> None:
        """The whole point of this being advisory.

        `read_events` and `append_event` can both raise for reasons that have
        nothing to do with the session: a five-second lock timeout raises
        `PmError`, a half-written trailing line raises `JSONDecodeError`, a full
        or read-only disk raises `OSError`. Letting any of those escape would
        turn a completed 20-minute wait into `exit 2` — losing the observation
        to protect a note about it.
        """
        self._launch(idle_script(sleep_seconds=120.0))

        for target, boom in (
            ("read_events", PmError("lock held")),
            ("read_events", json.JSONDecodeError("half-written line", "", 0)),
            ("append_event", PmError("lock held")),
            ("append_event", OSError("disk full")),
        ):
            with self.subTest(target=target, error=type(boom).__name__):
                with mock.patch.object(state_mod, target, side_effect=boom):
                    code, out, err = self.run_cli_in_repo(["observe", "--wait", "1"])
                self.assertEqual(code, 0, f"{target} raising {boom!r} must not fail the observe: {err}")
                self.assertIn("session running: True", out, "the observation itself must still be reported")

    def test_a_result_present_at_death_reports_result_not_death(self) -> None:
        """A Developer that writes `result.json` and exits has succeeded.
        Recording that as `wake=death` would hide the signal the slice actually
        produced behind the fact that the process is gone."""
        # Trigger-gated so the session is guaranteed alive through injection and
        # dies only once the test says so, with result.json already written —
        # a launch-relative timer would race `start-slice` itself.
        trigger = self.repo.parent / "result_then_exit_trigger"
        run_id, _token = self._launch(trigger_gated_result_script(trigger, tail_sleep=0))
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        current = state_mod.load_state(run_dir)["current_slice"]
        result_path = Path(current["artifact_dir"]) / "result.json"

        trigger.write_text("go\n", encoding="utf-8")
        self.assertTrue(self._wait_for(lambda: result_path.is_file(), timeout=20.0))
        self.assertTrue(
            self._wait_for(lambda: not sessions.session_exists(current["tmux_session"]), timeout=20.0)
        )

        code, _out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)
        latest = [e for e in state_mod.read_events(run_dir) if e["kind"] == "observe"][-1]
        self.assertIn("wake=result", latest["note"])

    def test_an_untimed_observe_records_no_requested_wait(self) -> None:
        run_id, _token = self._launch(idle_script(sleep_seconds=120.0))
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        code, _out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)
        latest = [e for e in state_mod.read_events(run_dir) if e["kind"] == "observe"][-1]
        self.assertIn("requested=none", latest["note"])


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestSendNudge(SliceOpsTestCase):
    def test_send_delivers_a_nudge_then_refuses_once_a_credential_prompt_appears(self) -> None:
        """Both `send` outcomes on one launched session.

        They were two tests, each paying a full `start-slice` (readiness settle
        plus two 1s injection settles) to reach the same live session. The
        refusal cannot be reached from a pane that shows the marker at launch —
        `send_prompt` would refuse to inject and `start-slice` itself would
        fail — so the marker has to appear after injection either way. Gating
        it on a trigger, rather than on a fixed delay racing the launch, is
        what lets one session serve both halves.
        """
        trigger = self.repo.parent / "credential_trigger"
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", trigger_gated_credential_prompt_script(trigger)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        attempts_before = state_mod.load_state(run_dir, token)["current_slice"]["attempts"]

        # 1. A clean pane accepts the nudge: it reaches the session, is
        #    recorded as a `send` event, and is not an attempt.
        code, _out, _err = self.run_cli_in_repo(
            ["send", "--text", "PM_STEER_MARKER_XYZ", "--reason", "nudge along", "--token", token]
        )
        self.assertEqual(code, 0)
        self.assertTrue(self._wait_for(lambda: "PM_STEER_MARKER_XYZ" in sessions.pane_text(session), timeout=10.0))

        attempts_after = state_mod.load_state(run_dir, token)["current_slice"]["attempts"]
        self.assertEqual(attempts_before, attempts_after)
        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "send" and event["note"] == "nudge along" for event in events))

        # 2. Once a credential prompt is visible, the same command refuses.
        trigger.write_text("go\n", encoding="utf-8")
        self.assertTrue(self._wait_for(lambda: "Enter API key" in sessions.pane_text(session), timeout=10.0))

        code, _out, err = self.run_cli_in_repo(
            ["send", "--text", "please continue", "--reason", "nudge", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("credential_prompt", err)


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestStop(SliceOpsTestCase):
    def test_stop_captures_pane_kills_session_and_sets_status(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        artifact_dir = Path(state_mod.load_state(run_dir, token)["current_slice"]["artifact_dir"])

        code, out, _err = self.run_cli_in_repo(["stop", "--reason", "operator stop", "--token", token])
        self.assertEqual(code, 0, out)
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session), timeout=10.0))
        self.assertTrue((artifact_dir / "pane.txt").is_file())

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["stop_reason"], "operator stop")

    def test_stop_scavenge_finds_run_prefixed_session_with_state_deleted(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        shutil.rmtree(run_dir)

        code, out, _err = self.run_cli_in_repo(["stop", "--reason", "emergency", "--scavenge", "--run", run_id])
        self.assertEqual(code, 0)
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session), timeout=10.0))
        self.assertIn(session, out)


# --- 12. all slices complete --------------------------------------------


class TestAllSlicesComplete(SliceOpsTestCase):
    def test_nothing_left_to_run_completes_the_run_without_launching_a_session(self) -> None:
        """One `start-slice` against an all-attested plan, asserted whole.

        This was two tests with identical setup and action. The one named
        "without_touching_tmux" asserted only output and state — it never
        checked that no session was launched, which is the claim in its name
        and the reason a no-work `start-slice` is safe to call. `start_session`
        is patched here so that claim is actually enforced.
        """
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        _state, token, run_dir = self.make_run(plan_path=plan_path, slice_statuses={"Slice 1": "attested"})

        with mock.patch.object(sessions, "start_session") as start_session:
            code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self.assertIn("all slices complete", out)
        start_session.assert_not_called()

        state = state_mod.load_state(run_dir, token)
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["status"], "complete")

        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "complete" for event in events))

        self.assertTrue((run_dir / "run-report.md").is_file())


# --- 13. a failed launch leaves no orphan session -------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestFailedLaunchLeavesNoSession(SliceOpsTestCase):
    """`start_slice` starts the tmux session before it records the session name
    in `current_slice`. Anything raising in that window leaves a live Developer
    session that no state names: `observe`, `finalize`, and `stop`'s
    recorded-session path all read `current_slice.tmux_session`, so the session
    is invisible to every one of them while still holding a running harness.

    Both failing steps are reachable in production — readiness raises on a
    trust prompt or an early exit, and injection refuses into a visible
    credential prompt.
    """

    def _init_and_fail(self, failing_call: str) -> tuple[int, str]:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        with mock.patch.object(sessions, failing_call, side_effect=PmError("boom")):
            code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 2, err)
        return run_id, token

    def _assert_no_session_survives(self, run_id: str) -> None:
        self.assertEqual(
            sessions.sessions_for_run(run_id),
            [],
            "a failed launch left a live tmux session that no run state names",
        )

    def test_readiness_failure_leaves_no_session(self) -> None:
        run_id, _token = self._init_and_fail("wait_until_ready")
        self._assert_no_session_survives(run_id)

    def test_injection_failure_leaves_no_session(self) -> None:
        run_id, _token = self._init_and_fail("send_prompt")
        self._assert_no_session_survives(run_id)


# --- 14. real-harness composition ---------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestRealHarnessComposition(SliceOpsTestCase):
    def test_launch_without_an_override_composes_a_real_harness_command(self) -> None:
        """The composition branch this reaches hid a lost local binding on the
        sibling headless branch: `start-slice` raised NameError there while the
        whole suite stayed green.

        `start_session` is wrapped to record the composed command and start a
        stand-in in its place; readiness is stubbed because it waits on the
        composed executable's own TUI banner. Composition therefore runs for
        real without invoking a coding CLI.
        """
        composed: list[str] = []
        real_start_session = sessions.start_session

        def capture(session: str, repo: Path, command: str, env: dict[str, str]) -> None:
            composed.append(command)
            real_start_session(session, repo, "sleep 30", env)

        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        with mock.patch.object(slice_ops, "_executable_exists", return_value=True):
            code, out, _err = self.run_cli_in_repo(
                ["init", "--repo", str(self.repo), "--plan", str(plan_path), "--harness", "codex"]
            )
        self.assertEqual(code, 0, out)
        run_id, token = parse_init_output(out)

        with mock.patch.object(sessions, "start_session", capture), mock.patch.object(
            sessions, "wait_until_ready", lambda *args, **kwargs: None
        ):
            code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self._track_current_session(run_id, token)
        self.assertEqual(code, 0, err)

        self.assertEqual(len(composed), 1)
        argv = shlex.split(composed[0])
        self.assertEqual(argv, ["codex", "--no-alt-screen", "--dangerously-bypass-approvals-and-sandbox"])


if __name__ == "__main__":
    unittest.main()

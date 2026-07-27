"""Protected behaviours: the slice lifecycle commands (evidence, not
acceptance), running the headless Developer.

Everything here drives `pm_lib.cli.main` in-process (via `run_cli_in_repo`),
matching an operator invoking the `pm` CLI from inside the working tree. No
real coding CLI is ever launched — lifecycle scenarios drive a tiny
fake-harness `sh` script (`pm_test_helpers.write_fake_harness`) as a
`--harness-command` override: PM launches it detached, passes the launch
pointer (or, on a resume, the steer correction) as its final argument, and
reads its captured stdout from the slice's `session-output.txt`. Pins:

1. `init` happy path: creates run state and prints the run capability token
   exactly once; writes the `.pm/` skeleton and a self-ignoring
   `.pm/.gitignore`; slice entries carry `plan_risk`; check-plan warnings
   are printed and the run still proceeds; an `init` event is recorded.
   Re-running `init` while a run already exists creates a SECOND run and
   repoints `current` — both run directories survive. `init` launches no
   Developer process of its own.
2. `init` failures, each exiting 2 with nothing created: a plan with
   errors; a dirty worktree; an unknown harness with no `--harness-command`
   override; `--attest` naming an unknown slice id; `--branch` naming a
   branch that does not exist. `--create-branch` succeeds.
3. Token gating: `approve`/`start-slice`/`finalize`/`stop` each exit 2 with
   a "token required" message when no token is supplied; a wrong token
   exits 2 with a plain (non-INTEGRITY) message; a hand-tampered `run.json`
   makes every one of those commands exit 2 with an `INTEGRITY:`-prefixed
   message. `status` and `observe` still work with no token at all.
4. `approve`: records reason + timestamp for an approval-flagged slice; a
   non-gated slice is refused; an unclear approval flag is refused.
5. Full fake-harness flow: `init` → `start-slice` (the fake makes an
   authorized commit and writes `result.json`) → `observe --wait` until the
   result appears → `finalize`: exits 0, prints all eight floor facts as
   PASS plus evidence paths (including `session-output=`); state is
   unchanged except `updated_at`.
6. `finalize` with a floor failure (unauthorized file): exits 1, the
   surface fact prints FAIL, a `floor` event is recorded.
7. Attempt accounting: `start-slice`, terminate the process (simulate a
   dead harness), `start-slice` again → a relaunch, `attempts` reads back
   as 1 from a fresh state load; the prior attempt's `result.json` is
   rotated into `attempt-0/`; exhausting the budget refuses the next
   relaunch, sets `needs-human`, exits 2.
8. Mid-run plan edit: `init`, edit the plan, `start-slice` → exits 2, run
   status becomes `needs-human`, a `plan-changed` event is recorded.
9. Dead process: after the Developer process dies, `observe` reports the
   session as not running and never raises.
10. `observe --wait` honest-wait semantics: the wait breaks early ONLY on
    process death, `result.json`, or a hard-stop marker in the captured
    session output — never on mere output churn. "output changed" reports
    growth since the PREVIOUS observation, so back-to-back no-wait observes
    still record real progress in the event log.
11. `stop`: leaves `session-output.txt` in place, terminates the tracked
    Developer process group, sets status `stopped`. `stop --scavenge`
    against a **deleted** state directory still finds and terminates the
    process via the identity-validated `developer.pid` sidecar and exits 0;
    a corrupt sidecar, or one owned by another run, fails closed and never
    signals anything.
12. All slices already complete: `start-slice` prints a completion message
    and exits 0 without launching anything.
13. Launch-bound session id capture: claude/copilot/override use the minted
    launch id; codex/opencode/qwen correlate from THIS launch's own outfile
    only (never a global lookup) and yield None when no id is present.
14. Launch environment isolation: an inherited
    `PM_DEVELOPER_RESUME_SESSION_ID` never reaches an initial launch, and the
    controller's own environment is restored afterwards.
15. Termination failures are never swallowed: `stop`, a relaunch, and both
    `stop --scavenge` paths (readable state and sidecar-only) exit 2 and
    leave the slice's authority intact; a PID-reuse-safe "nothing to signal"
    still succeeds.
16. `_await_launch_session_id` is the bounded, fail-closed pre-quiesce wait:
    it binds a delayed launch-owned id, times out to None, and short-circuits
    on a hard-stop marker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
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


# --- headless fake harness script builders -----------------------------------
#
# Each fake receives PM's launch pointer (or, on a resume, the steer
# correction) as $1 and the PM_* environment; it writes its progress to stdout
# (captured into session-output.txt) and its completion signal to
# $PM_RESULT_PATH. No real coding CLI is ever invoked. The bodies shared with
# `test_finalize` (`write_result_cmd`, `idle_body`, `commit_and_result_body`)
# live in `pm_test_helpers`; the ones below are specific to this suite.


def _result_only_body(*, delay: float = 0.5, tail_sleep: float = 30.0) -> str:
    lines = ["echo FAKE_HARNESS_WORKING"]
    if delay:
        lines.append(f"sleep {delay}")
    lines.append(write_result_cmd())
    if tail_sleep:
        lines.append(f"sleep {tail_sleep}")
    return "\n".join(lines)


def _dies_after_body(*, delay: float = 3.0) -> str:
    """Runs, emits benign output, then exits WITHOUT writing result.json."""
    return f"echo FAKE_HARNESS_WORKING\nsleep {delay}"


def _churn_body() -> str:
    """Keeps writing output (a ticking counter) for as long as observe --wait
    might run. Never writes result.json and never prints a hard-stop marker: a
    wait against this harness must run to (near) its full deadline, proving
    output growth is no longer a wait-exit condition."""
    return (
        "echo FAKE_HARNESS_WORKING\n"
        "i=0\n"
        "while true; do\n"
        "  i=$((i+1))\n"
        "  echo tick-$i\n"
        "  sleep 0.3\n"
        "done"
    )


def _trigger_credential_body(trigger_path: Path, *, sleep_seconds: float = 30.0) -> str:
    """Emits a credential-prompt marker into the captured output only once
    `trigger_path` exists on disk — OBSERVATION-relative, so the test controls
    exactly when the marker appears mid-`observe --wait`."""
    return (
        "echo FAKE_HARNESS_WORKING\n"
        f'while [ ! -f "{trigger_path}" ]; do sleep 0.1; done\n'
        "echo 'Enter API key to continue'\n"
        f"sleep {sleep_seconds}"
    )


# --- shared base -------------------------------------------------------------


class SliceOpsTestCase(PmTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Operate on a dedicated feature branch, as a real run does — the
        # implicit-current-branch init path refuses main/master.
        self._git("checkout", "-q", "-b", "pm-work")
        self._procs: list[tuple[int, int, str]] = []
        self.addCleanup(self._reap_procs)

    def _reap_procs(self) -> None:
        for pid, pgid, identity in self._procs:
            try:
                sessions.terminate_headless(pid, pgid, identity, term_timeout=0.2, kill_timeout=1.0)
            except PmError:
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
        # Deliberately outside self.repo: a plan.md living untracked inside the
        # repo would itself show up as a dirty (untracked) worktree entry,
        # tripping init's clean-worktree preflight for reasons unrelated to the
        # behaviour under test.
        return self.repo.parent / "plan.md"

    def _init(self, plan_path: Path, harness_script: Path, *, extra: list[str] | None = None) -> tuple[int, str, str]:
        argv = [
            "init",
            "--repo",
            str(self.repo),
            "--plan",
            str(plan_path),
            "--harness",
            "fake",
            "--harness-command",
            str(harness_script),
        ]
        if extra:
            argv += extra
        return self.run_cli_in_repo(argv)

    def _artifact_dir(self, run_id: str, token: str) -> Path:
        return Path(self._current(run_id, token)["artifact_dir"])


# --- 1. init happy path --------------------------------------------------


class TestInitHappyPath(SliceOpsTestCase):
    def test_init_creates_state_pm_skeleton_and_prints_token_once(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["requirements.txt"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())

        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)

        run_id, token = parse_init_output(out)
        self.assertEqual(out.count("PM_RUN_TOKEN="), 1)
        self.assertIn("Keep this token out of Developer sessions", out)
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())

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
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": None}])  # empty surface -> error
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())

        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertFalse((self.repo / ".pm").exists())
        pointer = state_mod.state_root(self.repo) / "current"
        self.assertFalse(pointer.exists())

    def test_dirty_worktree_exits_two(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        (self.repo / "untracked.txt").write_text("oops\n", encoding="utf-8")
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())

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
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
        code, _out, err = self._init(plan_path, harness, extra=["--attest", "Slice 99"])
        self.assertEqual(code, 2)
        self.assertIn("unknown slice", err)
        self.assertFalse((self.repo / ".pm").exists())

    def test_branch_nonexistent_exits_two(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
        code, _out, err = self._init(plan_path, harness, extra=["--branch", "does-not-exist"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)

    def test_create_branch_creates_and_switches(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
        code, out, _err = self._init(plan_path, harness, extra=["--create-branch", "feature/new-branch"])
        self.assertEqual(code, 0)
        self.assertIn("feature/new-branch", out)
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(result.stdout.strip(), "feature/new-branch")

    def test_default_onto_main_refused_but_explicit_branch_main_allowed(self) -> None:
        self._git("checkout", "-q", "main")
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())

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


# --- 5. full fake-harness flow -----------------------------------------------


class TestFullFakeHarnessFlow(SliceOpsTestCase):
    def test_full_flow_finalize_all_pass(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", commit_and_result_body(self.repo, delay=1.0, tail_sleep=3.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self.assertIn("launched", out)
        # An override's launch-bound id is captured from its own output (never
        # synthesized) and may not be present the instant after Popen; the
        # deterministic capture is re-correlated at finalize --steer. Either
        # start-slice line is therefore valid here.
        self.assertIn("as headless", out)
        self._track_current_process(run_id, token)

        code, out, _err = self.run_cli_in_repo(["observe", "--wait", "20"])
        self.assertEqual(code, 0)
        self.assertTrue(self._wait_for_result(run_id, token))

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        before_bytes = (run_dir / "run.json").read_bytes()

        code, out, _err = self.run_cli_in_repo(["finalize", "--token", token])
        self.assertEqual(code, 0, out)
        for number in range(1, 9):
            self.assertRegex(out, re.compile(rf"^{number} \S+ PASS", re.MULTILINE))
        self.assertIn("evidence: diff=", out)
        self.assertIn("evidence: session-output=", out)
        self.assertIn("evidence: result=", out)

        # The captured session output is the outfile itself, under .pm/.
        session_output = self._artifact_dir(run_id, token) / sessions.SESSION_OUTFILE
        self.assertTrue(session_output.is_file())

        after = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        before = json.loads(before_bytes.decode("utf-8"))
        after.pop("updated_at")
        before.pop("updated_at")
        self.assertEqual(after, before)

    def _wait_for_result(self, run_id: str, token: str) -> bool:
        artifact_dir = self._artifact_dir(run_id, token)
        return self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=15.0)


# --- 6. floor failure ---------------------------------------------------------


class TestFinalizeFloorFailure(SliceOpsTestCase):
    def test_unauthorized_file_change_fails_finalize(self) -> None:
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

        artifact_dir = self._artifact_dir(run_id, token)
        self.assertTrue(self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=15.0))

        code, out, _err = self.run_cli_in_repo(["finalize", "--token", token])
        self.assertEqual(code, 1)
        self.assertRegex(out, re.compile(r"^5 surface FAIL", re.MULTILINE))

        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "floor" and "surface" in event["note"] for event in events))


# --- 7. attempt accounting ----------------------------------------------------


class TestAttemptAccounting(SliceOpsTestCase):
    def test_relaunch_persists_attempts_rotates_prior_result_and_exhausts_budget(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _result_only_body(delay=0.5, tail_sleep=30.0))
        code, out, _err = self._init(plan_path, harness, extra=["--max-attempts", "1"])
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        # Attempt 0: launch, let it write a (stale, to-be-superseded) result.
        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords0 = self._track_current_process(run_id, token)
        self.assertIsNotNone(coords0)
        artifact_dir = self._artifact_dir(run_id, token)
        self.assertTrue(self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=10.0))

        # Simulate a dead harness: terminate the still-running process group.
        self._kill_proc(coords0)
        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords0), timeout=10.0))

        # Relaunch: attempts becomes 1 (within budget 1), prior result rotated.
        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0, out)
        self.assertIn("relaunched", out)
        coords1 = self._track_current_process(run_id, token)

        reloaded = state_mod.load_state(run_dir, token)
        self.assertEqual(reloaded["current_slice"]["attempts"], 1)
        by_id = {entry["id"]: entry for entry in reloaded["slices"]}
        self.assertEqual(by_id["Slice 1"]["attempts"], 1)
        # Attempt 0's result.json was rotated out of the way before the
        # relaunch — a stale completion signal can never be mistaken for the
        # new attempt's.
        self.assertTrue((artifact_dir / "attempt-0" / "result.json").is_file())
        # The pre-relaunch captured output rotated alongside it.
        self.assertTrue((artifact_dir / "attempt-0" / sessions.SESSION_OUTFILE).is_file())

        self._kill_proc(coords1)
        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords1), timeout=10.0))

        # Second relaunch would need attempts=2 > max_attempts=1: refused.
        code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("attempt budget exhausted", err)
        final_state = state_mod.load_state(run_dir, token)
        self.assertEqual(final_state["status"], "needs-human")

    def test_rotation_carries_the_exit_sidecar_with_its_output(self) -> None:
        """A turn's exit status is only evidence while it sits beside the
        output it describes: left behind, it strands the prior turn's diagnosis
        AND misreports the next turn, which starts with no status of its own.
        """
        artifact_dir = Path(self.repo) / "artifacts"
        artifact_dir.mkdir()
        outfile = artifact_dir / sessions.SESSION_OUTFILE
        outfile.write_text("output\n", encoding="utf-8")
        sessions.exit_status_path(outfile).write_text("7\n", encoding="utf-8")

        slice_ops._rotate_prior_attempt(artifact_dir, 0)

        rotated = artifact_dir / "attempt-0"
        self.assertEqual(sessions.read_exit_status(rotated / sessions.SESSION_OUTFILE), 7)
        self.assertIsNone(sessions.read_exit_status(outfile))


# --- 8. mid-run plan edit -----------------------------------------------------


class TestMidRunPlanEdit(SliceOpsTestCase):
    def test_plan_edited_mid_run_stops_before_next_slice(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body())
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


# --- 9. dead process ----------------------------------------------------------


class TestDeadProcess(SliceOpsTestCase):
    def test_observe_reports_not_running_after_process_dies(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _dies_after_body(delay=3.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords = self._track_current_process(run_id, token)
        self.assertIsNotNone(coords)

        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords), timeout=15.0))

        code, out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)
        self.assertIn("session running: False", out)
        # The diagnosis, not just the liveness: this fake ends itself having
        # written no result.json — the shape that previously carried no information.
        self.assertIn("session ended: exited 0 (clean exit)", out)


# --- 10. observe --wait semantics ---------------------------------------------


_WAITED_RE = re.compile(r"^waited:\s*([\d.]+)s \(requested ([\d.]+)s\)$", re.MULTILINE)


class TestObserveWaitSemantics(SliceOpsTestCase):
    """`observe --wait` honest-wait semantics: the wait runs the full requested
    duration and breaks early ONLY on process death, `result.json` appearing,
    or a hard-stop marker in the captured session output — never on mere output
    growth."""

    def _observe_wait(self, wait_seconds: float) -> tuple[int, str, str, float, float]:
        start = time.monotonic()
        code, out, err = self.run_cli_in_repo(["observe", "--wait", str(wait_seconds)])
        test_elapsed = time.monotonic() - start
        match = _WAITED_RE.search(out)
        self.assertIsNotNone(match, out)
        return code, out, err, test_elapsed, float(match.group(1))

    def test_output_changed_tracks_growth_between_observations(self) -> None:
        # "output changed" means grown since the PREVIOUS observe, not since
        # the top of this call. A no-wait observe (PM's normal polling shape)
        # that compared two back-to-back reads of the same file would always
        # report False and drop the progress record from the event log.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)
        # Use the outfile the run state actually records, and wait for the
        # harness's own launch line so the first observation below is not
        # racing an empty file.
        outfile = Path(self._current(run_id, token)["outfile"])
        self.assertTrue(
            self._wait_for(
                lambda: outfile.is_file() and "FAKE_HARNESS_WORKING" in outfile.read_text(encoding="utf-8"),
                timeout=10.0,
            )
        )

        # First observation establishes the cursor.
        code, _out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)

        # Nothing new since then: no growth, so no change reported.
        code, out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)
        self.assertIn("output changed: False", out)

        # Output produced between two no-wait observations IS growth.
        with outfile.open("a", encoding="utf-8") as handle:
            handle.write("the developer made progress\n")
        code, out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)
        self.assertIn("output changed: True", out)

        # Growth is recorded only when it actually happens, not on every poll:
        # exactly two observations saw new bytes — the first (the harness's
        # launch line) and the third (the appended progress line).
        events = state_mod.read_events(state_mod.resolve_run_dir(self.repo, run_id))
        changed = [e for e in events if e["kind"] == "observe" and "output_changed=True" in (e.get("note") or "")]
        self.assertEqual(len(changed), 2, [e.get("note") for e in events if e["kind"] == "observe"])

    def test_cosmetic_output_churn_does_not_end_wait_early(self) -> None:
        from pm_lib.slice_ops import _OBSERVE_POLL_SECONDS

        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _churn_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)

        wait_seconds = 3 * _OBSERVE_POLL_SECONDS
        code, out, _err, test_elapsed, reported_elapsed = self._observe_wait(wait_seconds)
        self.assertEqual(code, 0)
        self.assertIn("session running: True", out)
        self.assertIn("result present: False", out)
        # The wait must run to (near) the full requested duration despite the
        # output growing on every poll. Test-side measurement, so a broken
        # observe that returns instantly but prints a fabricated elapsed value
        # cannot pass.
        self.assertGreaterEqual(test_elapsed, wait_seconds - 0.5)
        self.assertLess(test_elapsed, wait_seconds + _OBSERVE_POLL_SECONDS + 3.0)
        self.assertLess(abs(reported_elapsed - test_elapsed), 2.0)

    def test_result_json_appearing_mid_wait_ends_wait_early(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _result_only_body(delay=4.0, tail_sleep=10.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)

        wait_seconds = 25.0
        code, out, _err, test_elapsed, _reported_elapsed = self._observe_wait(wait_seconds)
        self.assertEqual(code, 0)
        self.assertIn("result present: True", out)
        # Returned early: test-side elapsed well short of the full wait, paired
        # with the result-present signal above.
        self.assertLess(test_elapsed, 20.0)

    def test_process_death_mid_wait_ends_wait_early(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _dies_after_body(delay=4.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)

        wait_seconds = 20.0
        code, out, _err, test_elapsed, _reported_elapsed = self._observe_wait(wait_seconds)
        self.assertEqual(code, 0)
        self.assertIn("session running: False", out)
        self.assertLess(test_elapsed, 15.0)

    def test_hard_stop_marker_mid_wait_ends_wait_early(self) -> None:
        """OBSERVATION-relative: the credential marker is gated on a trigger
        file this test touches partway through the wait. `observe --wait` runs
        on a background thread; the main thread sleeps a beat (more than one
        poll cycle) so the wait has polled at least once, THEN creates the
        trigger. Test-side elapsed must be greater than that beat (the wait was
        still running when the marker appeared) and less than the full wait (it
        broke early on detecting the marker)."""
        from pm_lib.slice_ops import _OBSERVE_POLL_SECONDS

        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        trigger = self.repo.parent / "credential_trigger"
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _trigger_credential_body(trigger, sleep_seconds=30.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_process(run_id, token)

        wait_seconds = 20.0
        result: dict = {}

        def _run() -> None:
            start = time.monotonic()
            code, out, err = self.run_cli_in_repo(["observe", "--wait", str(wait_seconds)])
            result["elapsed"] = time.monotonic() - start
            result["code"], result["out"], result["err"] = code, out, err

        thread = threading.Thread(target=_run)
        thread.start()

        pre_trigger_beat = 2 * _OBSERVE_POLL_SECONDS
        time.sleep(pre_trigger_beat)
        trigger.write_text("go\n", encoding="utf-8")
        thread.join(timeout=wait_seconds + 15.0)
        self.assertFalse(thread.is_alive(), "observe --wait did not return in time")

        self.assertEqual(result["code"], 0)
        self.assertIn("session running: True", result["out"])
        self.assertIn("hard-stop scan:", result["out"])
        self.assertNotIn("hard-stop scan: clear", result["out"])
        self.assertGreater(result["elapsed"], pre_trigger_beat - 0.5)
        self.assertLess(result["elapsed"], wait_seconds)


# --- 11. stop + scavenge ------------------------------------------------------


class TestStop(SliceOpsTestCase):
    def test_stop_preserves_output_terminates_process_and_sets_status(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords), timeout=10.0))

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        artifact_dir = self._artifact_dir(run_id, token)

        code, out, _err = self.run_cli_in_repo(["stop", "--reason", "operator stop", "--token", token])
        self.assertEqual(code, 0, out)
        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords), timeout=10.0))
        # The captured session output persists (the outfile is the evidence).
        self.assertTrue((artifact_dir / sessions.SESSION_OUTFILE).is_file())

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["stop_reason"], "operator stop")

    def test_stop_scavenge_terminates_via_sidecar_with_state_deleted(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords), timeout=10.0))

        # The per-run developer.pid sidecar lives under .pm/ and survives a
        # deleted state directory.
        sidecar = slice_ops.developer_sidecar_path(self.repo, run_id)
        self.assertTrue(sidecar.is_file())

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        shutil.rmtree(run_dir)

        code, out, _err = self.run_cli_in_repo(["stop", "--reason", "emergency", "--scavenge", "--run", run_id])
        self.assertEqual(code, 0)
        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords), timeout=10.0))
        self.assertIn(f"developer pid {coords[0]}", out)

    def test_stop_scavenge_never_signals_an_unvalidatable_sidecar(self) -> None:
        # The sidecar is the only handle state-less scavenge has, so it must
        # never be trusted blindly: a corrupt sidecar, and one owned by a
        # different run, both fail closed — the live Developer is left alone
        # and nothing is reported terminated.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords), timeout=10.0))

        sidecar = slice_ops.developer_sidecar_path(self.repo, run_id)
        record = json.loads(sidecar.read_text(encoding="utf-8"))
        shutil.rmtree(state_mod.resolve_run_dir(self.repo, run_id))

        for label, contents in (
            ("corrupt", "{not json"),
            ("foreign run", json.dumps({**record, "run_id": f"{run_id}-other"})),
        ):
            sidecar.write_text(contents, encoding="utf-8")
            code, out, _err = self.run_cli_in_repo(
                ["stop", "--reason", "emergency", "--scavenge", "--run", run_id]
            )
            self.assertEqual(code, 0, f"{label}: {out}")
            self.assertIn("terminated: []", out, label)
            self.assertTrue(self._proc_alive(coords), label)


# --- 12. all slices complete --------------------------------------------


class TestAllSlicesComplete(SliceOpsTestCase):
    def test_start_slice_reports_complete_without_launching(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        _state, token, run_dir = self.make_run(plan_path=plan_path, slice_statuses={"Slice 1": "attested"})

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self.assertIn("all slices complete", out)

        state = state_mod.load_state(run_dir, token)
        self.assertIsNone(state["current_slice"])

    def test_all_attested_run_transitions_to_complete(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        _state, token, run_dir = self.make_run(plan_path=plan_path, slice_statuses={"Slice 1": "attested"})

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self.assertIn("all slices complete", out)

        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "complete")

        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "complete" for event in events))

        self.assertTrue((run_dir / "run-report.md").is_file())


# --- 13. launch-bound session id correlation (provenance) --------------------


class TestLaunchSessionIdCorrelation(unittest.TestCase):
    """Launch-bound id correlation never queries a bare newest session: it binds
    only by construction (claude/copilot launch-set uuid), by an override's own
    printed id, or by a UNIQUE harness-store record matched to THIS launch's
    exact pointer + repo cwd + start-time window. Newer unrelated candidates,
    ambiguous (>1) matches, and no match all fail closed to None."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        repo = Path(self._tmp.name) / "repo"
        repo.mkdir(parents=True)
        # Resolve so store-cwd comparisons are symlink-stable (macOS /var ->
        # /private/var), matching how PM resolves the repo cwd in production.
        self.repo = repo.resolve()
        self.pointer = "read your contract at /x/prompt.md"
        self.started_at = time.time() - 1.0
        self.none_outfile = self.repo / "no-such-output.txt"

    def _capture(self, harness_name: str, *, effective_override=None, outfile=None) -> str | None:
        return slice_ops._capture_launch_session_id(
            harness_name=harness_name,
            effective_override=effective_override,
            launch_id="minted-uuid",
            outfile=outfile if outfile is not None else self.none_outfile,
            prompt=self.pointer,
            cwd=self.repo,
            started_at=self.started_at,
        )

    # -- launch-set (claude/copilot) and override --

    def test_claude_and_copilot_use_launch_set_id(self) -> None:
        for harness in ("claude", "copilot"):
            self.assertEqual(self._capture(harness), "minted-uuid")

    def test_override_captures_its_own_printed_id_never_synthesized(self) -> None:
        outfile = self.repo / "ovr.txt"
        outfile.write_text("PM_DEVELOPER_SESSION_ID: override-abc\nworking...\n", encoding="utf-8")
        self.assertEqual(
            self._capture("fake", effective_override="/tmp/fake.sh", outfile=outfile), "override-abc"
        )

    def test_override_without_printed_id_is_none(self) -> None:
        outfile = self.repo / "ovr2.txt"
        outfile.write_text("no id line at all\n", encoding="utf-8")
        self.assertIsNone(self._capture("fake", effective_override="/tmp/fake.sh", outfile=outfile))

    # -- codex: exact stdout id, else unique store record --

    def _codex_write(self, root: Path, session_id: str, *, cwd: Path, prompt: str, mtime: float) -> Path:
        subdir = root / "2026" / "07" / "23"
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"rollout-2026-07-23T00-00-00-{session_id}.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"cwd": str(cwd)}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            },
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_codex_exact_stdout_id_wins(self) -> None:
        outfile = self.repo / "codex-out.txt"
        outfile.write_text(
            "OpenAI Codex\nsession id: 11111111-2222-3333-4444-555555555555\n", encoding="utf-8"
        )
        self.assertEqual(self._capture("codex", outfile=outfile), "11111111-2222-3333-4444-555555555555")

    def test_codex_store_unique_match_ignores_newer_unrelated(self) -> None:
        root = self.home / ".codex" / "sessions"
        good = "aaaaaaaa-1111-2222-3333-444444444444"
        self._codex_write(root, good, cwd=self.repo, prompt=self.pointer, mtime=time.time())
        # Newer, but a different prompt / a different cwd → not this launch.
        self._codex_write(
            root, "bbbbbbbb-1111-2222-3333-444444444444", cwd=self.repo, prompt="another task", mtime=time.time() + 2
        )
        self._codex_write(
            root, "cccccccc-1111-2222-3333-444444444444", cwd=self.repo.parent, prompt=self.pointer, mtime=time.time() + 3
        )
        with mock.patch.object(slice_ops, "_codex_sessions_root", lambda: root):
            self.assertEqual(self._capture("codex"), good)

    def test_codex_store_ambiguous_is_none(self) -> None:
        root = self.home / ".codex" / "sessions"
        self._codex_write(root, "aaaaaaaa-1111-2222-3333-444444444444", cwd=self.repo, prompt=self.pointer, mtime=time.time())
        self._codex_write(root, "dddddddd-1111-2222-3333-444444444444", cwd=self.repo, prompt=self.pointer, mtime=time.time() + 1)
        with mock.patch.object(slice_ops, "_codex_sessions_root", lambda: root):
            self.assertIsNone(self._capture("codex"))

    def test_codex_store_no_match_is_none(self) -> None:
        root = self.home / ".codex" / "sessions"
        self._codex_write(root, "eeeeeeee-1111-2222-3333-444444444444", cwd=self.repo, prompt="other", mtime=time.time())
        with mock.patch.object(slice_ops, "_codex_sessions_root", lambda: root):
            self.assertIsNone(self._capture("codex"))

    # -- opencode: unique record via read-only SQLite --

    def _opencode_db(self, path: Path, sessions_rows, parts_rows) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("CREATE TABLE session (id TEXT, directory TEXT, time_created INTEGER)")
            connection.execute("CREATE TABLE part (id TEXT, session_id TEXT, data TEXT, time_created INTEGER)")
            connection.executemany("INSERT INTO session VALUES (?,?,?)", sessions_rows)
            connection.executemany("INSERT INTO part VALUES (?,?,?,?)", parts_rows)
            connection.commit()
        finally:
            connection.close()

    def test_opencode_store_unique_match_ignores_newer_unrelated(self) -> None:
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        now_ms = int(time.time() * 1000)
        ours = json.dumps({"type": "text", "text": self.pointer})
        other = json.dumps({"type": "text", "text": "different"})
        self._opencode_db(
            db,
            [
                ("sess-good", str(self.repo), now_ms),
                ("sess-otherdir", str(self.repo.parent), now_ms + 2000),
                ("sess-otherprompt", str(self.repo), now_ms + 3000),
            ],
            [
                ("p1", "sess-good", ours, now_ms),
                ("p2", "sess-otherdir", ours, now_ms + 2000),
                ("p3", "sess-otherprompt", other, now_ms + 3000),
            ],
        )
        with mock.patch.object(slice_ops, "_opencode_session_db", lambda: db):
            self.assertEqual(self._capture("opencode"), "sess-good")

    def test_opencode_store_ambiguous_is_none(self) -> None:
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        now_ms = int(time.time() * 1000)
        ours = json.dumps({"type": "text", "text": self.pointer})
        self._opencode_db(
            db,
            [("sess-a", str(self.repo), now_ms), ("sess-b", str(self.repo), now_ms + 1000)],
            [("p1", "sess-a", ours, now_ms), ("p2", "sess-b", ours, now_ms + 1000)],
        )
        with mock.patch.object(slice_ops, "_opencode_session_db", lambda: db):
            self.assertIsNone(self._capture("opencode"))

    def test_opencode_store_no_match_is_none(self) -> None:
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        now_ms = int(time.time() * 1000)
        self._opencode_db(
            db,
            [("sess-x", str(self.repo.parent), now_ms)],
            [("p1", "sess-x", json.dumps({"type": "text", "text": self.pointer}), now_ms)],
        )
        with mock.patch.object(slice_ops, "_opencode_session_db", lambda: db):
            self.assertIsNone(self._capture("opencode"))

    def test_opencode_store_matches_quote_wrapped_text(self) -> None:
        # Some opencode versions persist the first user-message part wrapped
        # in one extra literal pair of double quotes (PM Test 2, headless
        # round): stored text == '"' + prompt + '"'. The matcher must strip
        # exactly one such layer rather than failing the exact-match closed.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        now_ms = int(time.time() * 1000)
        wrapped = json.dumps({"type": "text", "text": f'"{self.pointer}"'})
        self._opencode_db(
            db,
            [("sess-quoted", str(self.repo), now_ms)],
            [("p1", "sess-quoted", wrapped, now_ms)],
        )
        with mock.patch.object(slice_ops, "_opencode_session_db", lambda: db):
            self.assertEqual(self._capture("opencode"), "sess-quoted")

    # -- qwen: unique record from the per-project chats store --

    def _qwen_write(self, chats: Path, session_id: str, *, cwd: Path, prompt: str, ts: str) -> Path:
        chats.mkdir(parents=True, exist_ok=True)
        path = chats / f"{session_id}.jsonl"
        row = {
            "type": "user",
            "cwd": str(cwd),
            "timestamp": ts,
            "sessionId": session_id,
            "message": {"parts": [{"text": prompt}]},
        }
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        return path

    def test_qwen_store_unique_match_ignores_newer_unrelated(self) -> None:
        chats = self.home / ".qwen" / "chats"
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._qwen_write(chats, "qwen-good", cwd=self.repo, prompt=self.pointer, ts=iso)
        self._qwen_write(chats, "qwen-otherdir", cwd=self.repo.parent, prompt=self.pointer, ts=iso)
        self._qwen_write(chats, "qwen-otherprompt", cwd=self.repo, prompt="different", ts=iso)
        with mock.patch.object(slice_ops, "_qwen_chats_root", lambda cwd: chats):
            self.assertEqual(self._capture("qwen"), "qwen-good")

    def test_qwen_store_ambiguous_is_none(self) -> None:
        chats = self.home / ".qwen" / "chats"
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._qwen_write(chats, "qwen-a", cwd=self.repo, prompt=self.pointer, ts=iso)
        self._qwen_write(chats, "qwen-b", cwd=self.repo, prompt=self.pointer, ts=iso)
        with mock.patch.object(slice_ops, "_qwen_chats_root", lambda cwd: chats):
            self.assertIsNone(self._capture("qwen"))

    def test_qwen_store_no_match_is_none(self) -> None:
        chats = self.home / ".qwen" / "chats"
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._qwen_write(chats, "qwen-x", cwd=self.repo, prompt="other", ts=iso)
        with mock.patch.object(slice_ops, "_qwen_chats_root", lambda cwd: chats):
            self.assertIsNone(self._capture("qwen"))


# --- 14. launch environment isolation -----------------------------------------


def _echo_resume_env_body() -> str:
    """Echo the child-visible PM_DEVELOPER_RESUME_SESSION_ID, then idle."""
    return 'echo "RESUME_ENV=${PM_DEVELOPER_RESUME_SESSION_ID:-UNSET}"\nsleep 30'


class TestLaunchEnvironmentIsolation(SliceOpsTestCase):
    def test_inherited_resume_session_id_is_unset_for_initial_launch(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _echo_resume_env_body())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        prior = os.environ.get("PM_DEVELOPER_RESUME_SESSION_ID")
        os.environ["PM_DEVELOPER_RESUME_SESSION_ID"] = "inherited-leak"
        try:
            code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
            self.assertEqual(code, 0)
            self._track_current_process(run_id, token)
            outfile = self._artifact_dir(run_id, token) / sessions.SESSION_OUTFILE
            self.assertTrue(
                self._wait_for(
                    lambda: outfile.is_file() and "RESUME_ENV=" in outfile.read_text(encoding="utf-8"),
                    timeout=10.0,
                )
            )
            content = outfile.read_text(encoding="utf-8")
            # The child saw the var unset even though the controller inherited one.
            self.assertIn("RESUME_ENV=UNSET", content)
            self.assertNotIn("inherited-leak", content)
            # The controller's own environment is left untouched (restored).
            self.assertEqual(os.environ.get("PM_DEVELOPER_RESUME_SESSION_ID"), "inherited-leak")
        finally:
            if prior is None:
                os.environ.pop("PM_DEVELOPER_RESUME_SESSION_ID", None)
            else:
                os.environ["PM_DEVELOPER_RESUME_SESSION_ID"] = prior


# --- 15. termination-failure propagation --------------------------------------


class TestTerminationFailurePropagates(SliceOpsTestCase):
    def _launch_idle(self):
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        coords = self._track_current_process(run_id, token)
        self.assertTrue(self._wait_for(lambda: self._proc_alive(coords), timeout=10.0))
        return run_id, token, coords

    def test_stop_does_not_claim_success_when_termination_fails(self) -> None:
        run_id, token, _coords = self._launch_idle()
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        with mock.patch.object(sessions, "terminate_headless", side_effect=PmError("headless process group survived SIGKILL")):
            code, _out, err = self.run_cli_in_repo(["stop", "--reason", "operator stop", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("SIGKILL", err)
        # State is not marked stopped and current authority is not cleared.
        state = state_mod.load_state(run_dir, token)
        self.assertNotEqual(state["status"], "stopped")
        self.assertIsNotNone(state["current_slice"])

    def test_stop_succeeds_when_terminate_returns_false_pid_reuse_safe(self) -> None:
        run_id, token, _coords = self._launch_idle()
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        # False = nothing of ours to signal (leader gone / PID reused) — a safe
        # no-op that must NOT block the stop.
        with mock.patch.object(sessions, "terminate_headless", return_value=False):
            code, out, _err = self.run_cli_in_repo(["stop", "--reason", "operator stop", "--token", token])
        self.assertEqual(code, 0, out)
        state = state_mod.load_state(run_dir, token)
        self.assertEqual(state["status"], "stopped")

    def test_relaunch_aborts_when_prior_cannot_be_terminated(self) -> None:
        run_id, token, coords = self._launch_idle()
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        before = state_mod.load_state(run_dir, token)["current_slice"]
        self._kill_proc(coords)
        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords), timeout=10.0))
        with mock.patch.object(sessions, "terminate_headless", side_effect=PmError("headless process group survived SIGKILL")):
            code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("SIGKILL", err)
        # No new attempt superseded the prior current_slice.
        after = state_mod.load_state(run_dir, token)["current_slice"]
        self.assertEqual(after["session"], before["session"])
        self.assertEqual(after["attempts"], before["attempts"])

    def test_launch_is_torn_down_when_post_launch_bookkeeping_fails(self) -> None:
        # Between Popen and the authenticated state write, a sidecar or state
        # failure would otherwise leave an autonomous Developer running with no
        # durable handle at all — the headless model has no global process list
        # to sweep. The launch must be torn down instead.
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_body(sleep_seconds=30.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        launched: dict[str, object] = {}

        def _capture_then_fail(_path, *, pid, pgid, identity, run_id, slice_id):
            launched.update(coords=(int(pid), int(pgid), str(identity)))
            raise PmError("no space left on device writing developer sidecar")

        with mock.patch.object(sessions, "write_developer_sidecar", side_effect=_capture_then_fail):
            code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("sidecar", err)

        coords = launched.get("coords")
        self.assertIsNotNone(coords, "the sidecar write should have been reached")
        self._procs.append(coords)
        self.assertTrue(self._wait_for(lambda: not self._proc_alive(coords), timeout=10.0))
        # No half-launched slice is left recorded as current.
        self.assertIsNone(state_mod.load_state(run_dir, token)["current_slice"])

    def test_scavenge_with_readable_state_reports_termination_failure(self) -> None:
        # `--scavenge` falls back to the sidecar-only sweep ONLY when the run
        # state cannot be resolved or loaded. With readable state the sweep is
        # not a fallback for a *termination* failure: swallowing one would
        # print "state unavailable" and exit 0 while the Developer lives on —
        # and with no `--run` given the fallback sweep has no sidecar path to
        # look up, so it would report nothing terminated at all.
        run_id, token, _coords = self._launch_idle()
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        with mock.patch.object(sessions, "terminate_headless", side_effect=PmError("headless process group survived SIGKILL")):
            code, out, err = self.run_cli_in_repo(
                ["stop", "--reason", "emergency", "--scavenge", "--token", token]
            )
        self.assertEqual(code, 2)
        self.assertIn("SIGKILL", err)
        self.assertNotIn("state unavailable", out)
        # State keeps the slice's authority: nothing was stopped.
        state = state_mod.load_state(run_dir, token)
        self.assertNotEqual(state["status"], "stopped")
        self.assertIsNotNone(state["current_slice"])

    def test_scavenge_reports_failure_when_termination_fails(self) -> None:
        run_id, token, _coords = self._launch_idle()
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        shutil.rmtree(run_dir)
        with mock.patch.object(sessions, "terminate_headless", side_effect=PmError("headless process group survived SIGKILL")):
            code, _out, err = self.run_cli_in_repo(
                ["stop", "--reason", "emergency", "--scavenge", "--run", run_id]
            )
        self.assertEqual(code, 2)
        self.assertIn("SIGKILL", err)


# --- 16. bounded launch-owned capture wait ------------------------------------


class TestAwaitLaunchSessionId(unittest.TestCase):
    """`_await_launch_session_id` is the bounded, fail-closed evidence-gathering
    wait finalize --steer uses before quiescing: it binds a launch-owned id that
    appears shortly after launch, times out to None when none appears, and stops
    early on a hard-stop marker — never synthesizing or guessing an id."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.outfile = self.dir / "session-output.txt"
        self.outfile.write_text("", encoding="utf-8")

    def _await(self, *, timeout: float, poll: float = 0.05):
        return slice_ops._await_launch_session_id(
            harness_name="fake",
            effective_override="/tmp/fake.sh",
            outfile=self.outfile,
            prompt="read your contract at /x/prompt.md",
            cwd=self.dir,
            started_at=time.time(),
            timeout=timeout,
            poll=poll,
        )

    def test_binds_override_id_that_appears_after_a_delay(self) -> None:
        def _emit() -> None:
            time.sleep(0.3)
            self.outfile.write_text("PM_DEVELOPER_SESSION_ID: late-id\n", encoding="utf-8")

        thread = threading.Thread(target=_emit)
        thread.start()
        self.addCleanup(thread.join)
        self.assertEqual(self._await(timeout=3.0), "late-id")

    def test_times_out_to_none_when_no_id_appears(self) -> None:
        self.outfile.write_text("working, but no id line\n", encoding="utf-8")
        start = time.monotonic()
        self.assertIsNone(self._await(timeout=0.3))
        # It actually waited the bound rather than returning instantly.
        self.assertGreaterEqual(time.monotonic() - start, 0.3)

    def test_short_circuits_on_hard_stop_marker(self) -> None:
        self.outfile.write_text("Enter API key to continue\n", encoding="utf-8")
        start = time.monotonic()
        self.assertIsNone(self._await(timeout=5.0))
        # Did not wait the full timeout — the hard-stop marker ended the wait.
        self.assertLess(time.monotonic() - start, 2.0)


if __name__ == "__main__":
    unittest.main()

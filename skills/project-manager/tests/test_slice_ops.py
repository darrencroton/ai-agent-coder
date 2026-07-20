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
5. Full fake-harness flow (tmux): `init` → `start-slice` (the fake harness
   makes an authorized commit and writes `result.json`) → `observe --wait`
   until the result appears → `finalize`: exits 0, prints all eight floor
   facts as PASS plus evidence paths; state is unchanged except
   `updated_at`. (Stage 4's `finalize --accept/--steer/--stop` decision
   paths are pinned in `test_finalize.py`, not here.)
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
10. `send` nudge (tmux): a live fake session receives a steered line that
    appears in the pane, and a `send` event is recorded without touching
    `attempts`; sending into a pane showing a credential prompt is refused
    by the sessions hard-stop floor.
11. `stop` (tmux): captures `pane.txt`, kills the run's sessions, sets
    status `stopped` with the given reason. `stop --scavenge` against a
    **deleted** state directory still finds and kills a stray
    `pm-<run-id>-…` session and exits 0.
12. All slices already complete: `start-slice` prints a completion message
    and exits 0 without touching tmux.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import shutil

from pm_test_helpers import PmTestCase, parse_init_output, write_fake_harness

from pm_lib import sessions
from pm_lib import state as state_mod

_HAS_TMUX = shutil.which("tmux") is not None


# --- fake harness script builders --------------------------------------------


def _result_heredoc(status: str = "done", summary: str = "did the work") -> str:
    return (
        'cat > "$PM_RESULT_PATH" <<EOF\n'
        '{"slice": "$PM_SLICE_ID", "status": "' + status + '", "summary": "' + summary + '"}\n'
        "EOF"
    )


def _commit_and_result_script(
    repo: Path,
    *,
    authorized_file: str = "a.py",
    unauthorized_file: str | None = None,
    delay: float = 1.0,
    tail_sleep: float = 3.0,
) -> str:
    """A fake harness that echoes a readiness marker, waits, makes a commit
    (optionally touching an unauthorized file too), writes result.json, then
    idles briefly before exiting."""
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


def _result_only_script(*, delay: float = 0.5, tail_sleep: float = 3.0) -> str:
    lines = ["echo FAKE_HARNESS_READY", f"sleep {delay}", _result_heredoc(), f"sleep {tail_sleep}"]
    return "\n".join(lines)


def _idle_script(*, sleep_seconds: float = 30.0) -> str:
    return f"echo FAKE_HARNESS_READY\nsleep {sleep_seconds}"


def _credential_prompt_script(*, reveal_after: float = 5.0, sleep_seconds: float = 30.0) -> str:
    """A harness that comes up clean, then reveals a credential prompt only
    *after* the developer prompt has been injected.

    The pane must be clear of hard-stop markers at injection time —
    `send_prompt` refuses to paste into a visible credential/approval/side-
    effect prompt (the launch-time hard-prompt floor), so a harness that
    printed the marker as its first line would fail `start-slice` itself and
    never reach the live-session `send`-refusal this scenario exercises.
    The readiness wait settles on the clean `FAKE_HARNESS_READY` pane and
    injects; `reveal_after` seconds later the credential prompt appears, and
    a subsequent `send` is what must refuse."""
    return f"echo FAKE_HARNESS_READY\nsleep {reveal_after}\necho 'Enter API key to continue'\nsleep {sleep_seconds}"


def _trigger_gated_credential_prompt_script(trigger_path: Path, *, sleep_seconds: float = 30.0) -> str:
    """Like `_credential_prompt_script`, but the credential-prompt marker
    appears only once `trigger_path` exists on disk, rather than after a
    fixed delay from harness launch. This makes the marker's appearance
    OBSERVATION-relative — the test controls exactly when it fires, mid-
    `observe --wait` — instead of launch-relative, removing the race where
    a slow `start-slice` could let a fixed-delay marker appear before
    `observe` ever starts polling (in which case an early return would
    prove nothing about *mid-wait* detection). Same launch-time-clean
    requirement as `_credential_prompt_script`: the pane must show nothing
    but `FAKE_HARNESS_READY` until the trigger appears, or `start-slice`'s
    launch-time hard-prompt floor would refuse to inject."""
    return (
        "echo FAKE_HARNESS_READY\n"
        f'while [ ! -f "{trigger_path}" ]; do sleep 0.1; done\n'
        "echo 'Enter API key to continue'\n"
        f"sleep {sleep_seconds}"
    )


def _dies_quickly_script(*, delay: float = 3.0) -> str:
    return f"echo FAKE_HARNESS_READY\nsleep {delay}"


def _stdin_draining_idle_script() -> str:
    """A harness that actively reads (and echoes) stdin, unlike a bare
    `sleep`. `send_prompt`'s injected multi-KB developer prompt would
    otherwise sit unread in the pty's canonical-mode input queue and can
    saturate it, silently dropping a *later* `send_line` steer — the same
    reason a real coding CLI (which does read stdin) doesn't hit this."""
    return "echo FAKE_HARNESS_READY\nexec cat -"


def _cosmetic_churn_script() -> str:
    """A harness that keeps changing the pane (a ticking counter) for as
    long as `observe --wait` might run, draining stdin in the background
    (`cat -`) so the injected developer prompt is still read per the
    documented Developer-fake convention. Never writes result.json and
    never prints a hard-stop marker: a wait against this harness must run
    to (near) its full deadline, proving `detect_activity`'s any-byte-
    change `active` flag is no longer used as a wait-exit condition."""
    return (
        "echo FAKE_HARNESS_READY\n"
        "cat - >/dev/null &\n"
        "i=0\n"
        "while true; do\n"
        "  i=$((i+1))\n"
        "  echo tick-$i\n"
        "  sleep 0.3\n"
        "done"
    )


# --- shared base -------------------------------------------------------------


class SliceOpsTestCase(PmTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._sessions_to_reap: list[str] = []
        self.addCleanup(self._reap_sessions)

    def _reap_sessions(self) -> None:
        for name in self._sessions_to_reap:
            sessions.force_stop(name)

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
        # Deliberately outside self.repo: a plan.md living untracked inside
        # the repo would itself show up as a dirty (untracked) worktree
        # entry, tripping init's clean-worktree preflight for reasons that
        # have nothing to do with the behaviour under test (see
        # FloorTestCase._plan_path in test_floor.py for the same reasoning).
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


# --- 1. init happy path --------------------------------------------------


class TestInitHappyPath(SliceOpsTestCase):
    def test_init_creates_state_pm_skeleton_and_prints_token_once(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["requirements.txt"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())

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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())

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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())

        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertFalse((self.repo / ".pm").exists())
        pointer = state_mod.state_root(self.repo) / "current"
        self.assertFalse(pointer.exists())

    def test_dirty_worktree_exits_two(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        (self.repo / "untracked.txt").write_text("oops\n", encoding="utf-8")
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())

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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())
        code, _out, err = self._init(plan_path, harness, extra=["--attest", "Slice 99"])
        self.assertEqual(code, 2)
        self.assertIn("unknown slice", err)
        self.assertFalse((self.repo / ".pm").exists())

    def test_branch_nonexistent_exits_two(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())
        code, _out, err = self._init(plan_path, harness, extra=["--branch", "does-not-exist"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)

    def test_create_branch_creates_and_switches(self) -> None:
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())
        code, out, _err = self._init(plan_path, harness, extra=["--create-branch", "feature/new-branch"])
        self.assertEqual(code, 0)
        self.assertIn("feature/new-branch", out)
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(result.stdout.strip(), "feature/new-branch")


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
class TestFullFakeHarnessFlow(SliceOpsTestCase):
    def test_full_flow_finalize_all_pass_and_accept_refused(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self.assertIn("launched", out)
        self._track_current_session(run_id, token)

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
        self.assertIn("evidence: pane=", out)
        self.assertIn("evidence: result=", out)

        after = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        before = json.loads(before_bytes.decode("utf-8"))
        after.pop("updated_at")
        before.pop("updated_at")
        self.assertEqual(after, before)

    def _wait_for_result(self, run_id: str, token: str) -> bool:
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        state = state_mod.load_state(run_dir, token)
        artifact_dir = Path(state["current_slice"]["artifact_dir"])
        return self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=15.0)


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestFinalizeFloorFailure(SliceOpsTestCase):
    def test_unauthorized_file_change_fails_finalize(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(
            self.repo.parent / "fake.sh",
            _commit_and_result_script(self.repo, unauthorized_file="b.py", delay=1.0, tail_sleep=2.0),
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _result_only_script(delay=0.5, tail_sleep=30.0))
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script())
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _dies_quickly_script(delay=7.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertIsNotNone(session)

        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session), timeout=15.0))

        code, out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)
        self.assertIn("session running: False", out)

        code, _out, err = self.run_cli_in_repo(["send", "--text", "hello", "--reason", "nudge", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("no live session", err)


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

    def test_cosmetic_pane_churn_does_not_end_wait_early(self) -> None:
        from pm_lib.slice_ops import _OBSERVE_POLL_SECONDS

        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _cosmetic_churn_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)

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
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        # The harness's own delay clock starts at session launch, not at
        # `observe`'s first check — start-slice's readiness wait plus prompt
        # injection alone can take several seconds, so the delay must clear
        # that bar with margin, or result.json is already there by the time
        # `observe --wait` runs its first check and elapsed would trivially
        # be ~0, proving nothing about mid-wait behaviour.
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _result_only_script(delay=9.0, tail_sleep=5.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)

        wait_seconds = 25.0
        code, out, _err, test_elapsed, _reported_elapsed = self._observe_wait(wait_seconds)
        self.assertEqual(code, 0)
        self.assertIn("result present: True", out)
        # Returned early: proven by the TEST-SIDE measurement being well
        # short of the full requested wait, paired with the result-present
        # signal above. (A parsed-elapsed lower bound proves nothing beyond
        # a launch-relative race and has been dropped.)
        self.assertLess(test_elapsed, 20.0)

    def test_session_death_mid_wait_ends_wait_early(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        # A short delay would race start-slice's own readiness/prompt-
        # injection wait (see TestDeadSession, which uses the same 7.0s for
        # the same reason) — too short and start-slice itself fails before
        # observe ever gets a live session to watch die.
        harness = write_fake_harness(self.repo.parent / "fake.sh", _dies_quickly_script(delay=7.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)

        wait_seconds = 20.0
        code, out, _err, test_elapsed, _reported_elapsed = self._observe_wait(wait_seconds)
        self.assertEqual(code, 0)
        self.assertIn("session running: False", out)
        # Returned early: TEST-SIDE elapsed well short of the full wait, plus
        # the session-death signal above.
        self.assertLess(test_elapsed, 15.0)

    def test_hard_stop_marker_mid_wait_ends_wait_early(self) -> None:
        """Deterministic, OBSERVATION-relative version (no launch-relative
        race): the credential marker is gated on a trigger file that this
        test touches explicitly partway through the wait, rather than on a
        fixed delay from harness launch. A fixed launch-relative delay
        could let a slow `start-slice` cause the marker to appear before
        `observe` ever starts polling — an early return would then prove
        nothing about *mid-wait* detection, only an upper bound. Here,
        `observe --wait` runs on a background thread; the main thread
        sleeps a short beat (comfortably more than one poll cycle) to give
        it time to have polled at least once, THEN creates the trigger
        file. TEST-SIDE elapsed must be both greater than that pre-trigger
        beat (proving the wait was genuinely still running — i.e. had not
        already returned — when the marker appeared) and less than the
        full requested wait (proving it broke early upon detecting the
        marker)."""
        from pm_lib.slice_ops import _OBSERVE_POLL_SECONDS

        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        trigger = self.repo.parent / "credential_trigger"
        harness = write_fake_harness(
            self.repo.parent / "fake.sh", _trigger_gated_credential_prompt_script(trigger, sleep_seconds=30.0)
        )
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)

        wait_seconds = 20.0
        result: dict = {}

        def _run() -> None:
            start = time.monotonic()
            code, out, err = self.run_cli_in_repo(["observe", "--wait", str(wait_seconds)])
            result["elapsed"] = time.monotonic() - start
            result["code"], result["out"], result["err"] = code, out, err

        thread = threading.Thread(target=_run)
        thread.start()

        # A short beat, comfortably longer than one poll cycle, before we
        # create the trigger file — this is what proves `observe` was still
        # actually mid-wait (had already polled at least once and not yet
        # returned) at the moment the marker appeared.
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


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestSendNudge(SliceOpsTestCase):
    def test_send_line_appears_in_pane_and_logs_event_without_touching_attempts(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _stdin_draining_idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for(lambda: sessions.session_exists(session), timeout=10.0))

        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        attempts_before = state_mod.load_state(run_dir, token)["current_slice"]["attempts"]

        code, _out, _err = self.run_cli_in_repo(
            ["send", "--text", "PM_STEER_MARKER_XYZ", "--reason", "nudge along", "--token", token]
        )
        self.assertEqual(code, 0)
        self.assertTrue(self._wait_for(lambda: "PM_STEER_MARKER_XYZ" in sessions.pane_text(session), timeout=10.0))

        attempts_after = state_mod.load_state(run_dir, token)["current_slice"]["attempts"]
        self.assertEqual(attempts_before, attempts_after)
        events = state_mod.read_events(run_dir)
        self.assertTrue(any(event["kind"] == "send" and event["note"] == "nudge along" for event in events))

    def test_send_refuses_into_visible_credential_prompt(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", _credential_prompt_script(sleep_seconds=15.0))
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        session = self._track_current_session(run_id, token)
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script(sleep_seconds=30.0))
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
        harness = write_fake_harness(self.repo.parent / "fake.sh", _idle_script(sleep_seconds=30.0))
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
    def test_start_slice_reports_complete_without_touching_tmux(self) -> None:
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


if __name__ == "__main__":
    unittest.main()

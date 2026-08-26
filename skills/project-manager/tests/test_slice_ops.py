"""Protected behaviours: the slice lifecycle commands (evidence, not acceptance).

Everything here drives `pm_lib.cli.main` in-process via `run_cli_in_repo`,
matching an operator invoking `pm` from inside the working tree. No real
coding CLI is ever launched — tmux-gated scenarios drive a tiny fake-harness
`sh` script (`pm_test_helpers.write_fake_harness`).

The acceptance-bearing `finalize` paths live in `test_finalize.py`, which
drives the same launch through to a recorded decision.
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
    PmTestCase,
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
from pm_lib import plan as plan_mod
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


# --- init happy path ---------------------------------------------------------


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


# --- init failures -----------------------------------------------------------


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
        # Refuse the implicit default; honour an explicit --branch main.
        self._git("checkout", "-q", "main")
        plan_path = self.write_plan(self._plan_path())
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())

        code, _out, err = self._init(plan_path, harness)
        self.assertEqual(code, 2)
        self.assertIn("main", err)

        code, out, _err = self._init(plan_path, harness, extra=["--branch", "main"])
        self.assertEqual(code, 0)
        self.assertIn("branch: main", out)


# --- token gating ------------------------------------------------------------


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
            ["notes", "--set", "x"],
            ["rate", "--text", "Process discipline: 5/5 — no incidents."],
            ["review", "--slice", "Slice 1", "--skill", "code-review"],
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


# --- approve -----------------------------------------------------------------


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


# --- grant ---------------------------------------------------------------


_LONG_EVIDENCE = "Repository investigation confirms this path must change to satisfy the slice's contract."


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestGrant(SliceOpsTestCase):
    """`pm grant`: widening a slice's *effective* authorized surface mid-run.

    `grant` itself never touches tmux, but its "current in-flight slice"
    check reads `current_slice`, which only a real `start-slice` launch
    sets — so every scenario here launches a fake-harness session first.
    """

    def _launch_single_slice(self, *, plan_slices: list[dict] | None = None) -> tuple[str, str, Path, Path]:
        plan_path = self.write_plan(self._plan_path(), slices=plan_slices or [{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        code, _out, _err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0)
        self._track_current_session(run_id, token)
        return run_id, token, run_dir, plan_path

    def test_grant_records_path_evidence_and_ratchets_risk_elevated(self) -> None:
        run_id, token, run_dir, _plan_path = self._launch_single_slice()

        code, out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "b.py", "--evidence", _LONG_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 0, out + err)

        state = state_mod.load_state(run_dir, token)
        entry = state["slices"][0]
        [record] = entry["grants"]
        self.assertEqual(record["path"], "b.py")
        self.assertEqual(record["evidence"], _LONG_EVIDENCE)
        self.assertIn("T", record["at"])
        self.assertEqual(entry["risk"], "elevated")
        self.assertEqual(entry["plan_risk"], "standard")

        events = state_mod.read_events(run_dir)
        self.assertTrue(any(e["kind"] == "grant" and e["note"].startswith("b.py:") for e in events))

    def test_risk_raise_event_fires_once_not_on_a_second_grant(self) -> None:
        _run_id, token, run_dir, _plan_path = self._launch_single_slice()

        code, _out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "b.py", "--evidence", _LONG_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 0, err)
        code, _out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "c.py", "--evidence", _LONG_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 0, err)

        events = state_mod.read_events(run_dir)
        risk_raise_events = [e for e in events if e["kind"] == "risk-raise"]
        self.assertEqual(len(risk_raise_events), 1)

    def test_grant_refused_when_evidence_is_too_short_after_stripping(self) -> None:
        _run_id, token, _run_dir, _plan_path = self._launch_single_slice()

        # Padded with whitespace so the RAW length clears 40 chars: the
        # refusal must be on the STRIPPED length, not the raw one.
        evidence = " " * 20 + "short reason" + " " * 20
        code, _out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "b.py", "--evidence", evidence, "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("40", err)

    def test_grant_refused_on_a_slice_that_is_not_current(self) -> None:
        _run_id, token, _run_dir, _plan_path = self._launch_single_slice(
            plan_slices=[{"files": ["a.py"]}, {"files": ["b.py"]}]
        )

        code, _out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 2", "--path", "c.py", "--evidence", _LONG_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("not the current in-flight slice", err)

    def test_grant_refused_on_invalid_path_shape(self) -> None:
        _run_id, token, _run_dir, _plan_path = self._launch_single_slice()

        code, _out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "/abs/path.py", "--evidence", _LONG_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid grant path", err)

    def test_grant_refused_on_dependency_shaped_path(self) -> None:
        _run_id, token, _run_dir, _plan_path = self._launch_single_slice()

        code, _out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "package.json", "--evidence", _LONG_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("grant refused", err)
        self.assertIn("dependency-shaped", err)

    def test_grant_refused_for_a_directory_or_glob_path_only_exact_files_are_grantable(self) -> None:
        """A grant authorizes one exact file path. A trailing-slash directory,
        a glob, the whole-repo spelling, and a plain path naming an existing
        directory must all be refused — none of them is the one discovered
        file PM has evidence for, and a surviving pattern would alias a
        surface the dangerous-surface refusal must catch (e.g. '*.toml'
        reaching pyproject.toml)."""
        _run_id, token, _run_dir, _plan_path = self._launch_single_slice()
        (self.repo / "src" / "query").mkdir(parents=True)
        (self.repo / "src" / "query" / "placeholder.py").write_text("x = 1\n", encoding="utf-8")
        self._git("add", "src/query/placeholder.py")
        self._git("commit", "-q", "-m", "add src/query directory")

        for grant_path in ("src/query/", "*.toml", "**/**", "src/query"):
            with self.subTest(path=grant_path):
                code, _out, err = self.run_cli_in_repo(
                    ["grant", "--slice", "Slice 1", "--path", grant_path, "--evidence", _LONG_EVIDENCE, "--token", token]
                )
                self.assertEqual(code, 2, err)
                self.assertIn("grant refused", err)

    def test_grant_refused_when_path_already_within_effective_surface(self) -> None:
        _run_id, token, _run_dir, _plan_path = self._launch_single_slice()

        code, _out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "a.py", "--evidence", _LONG_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("already within the effective authorized surface", err)

    def test_grant_survives_a_fresh_state_load_on_resume(self) -> None:
        _run_id, token, run_dir, plan_path = self._launch_single_slice()

        code, _out, err = self.run_cli_in_repo(
            ["grant", "--slice", "Slice 1", "--path", "b.py", "--evidence", _LONG_EVIDENCE, "--token", token]
        )
        self.assertEqual(code, 0, err)

        # A fresh state load in a brand-new call — the resume invariant: a
        # grant's authority must not depend on anything held in memory.
        reloaded = state_mod.load_state(run_dir, token)
        slices = plan_mod.parse_plan(plan_path)
        plan_slice = plan_mod.plan_slice_by_id(slices, "Slice 1")
        self.assertIn("b.py", plan_mod.effective_authorized_files(plan_slice, reloaded))


# --- tmux-gated flows --------------------------------------------------------


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
        # The fake harness writes no validation.md, so stand in for the
        # Developer-authored evidence the real one leaves behind: it must
        # rotate with the result, or attempt 1 inherits attempt 0's evidence
        # looking freshly written.
        (artifact_dir / "validation.md").write_text("attempt 0 validation\n", encoding="utf-8")

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
        rotated_validation = artifact_dir / "attempt-0" / "validation.md"
        self.assertTrue(rotated_validation.is_file())
        self.assertEqual(rotated_validation.read_text(encoding="utf-8"), "attempt 0 validation\n")
        self.assertFalse((artifact_dir / "validation.md").exists())

        sessions.force_stop(session1)
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session1), timeout=10.0))

        # Second relaunch would need attempts=2 > max_attempts=1: refused.
        code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 2)
        self.assertIn("attempt budget exhausted", err)
        final_state = state_mod.load_state(run_dir, token)
        self.assertEqual(final_state["status"], "needs-human")


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestLaunchPersistenceWindow(SliceOpsTestCase):
    def test_a_persistence_failure_after_launch_kills_the_session_and_reraises(self) -> None:
        """The window between `start_session` and a successful `save_state` is
        the one where a live Developer session exists that no state names: it
        is invisible to `observe`, `finalize`, and `stop`'s recorded-session
        path, so it would burn tokens unattended until a human noticed the
        stray tmux session. A failing `save_state` (full or read-only state
        dir) is the realistic trigger, and is injected here directly."""
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        harness = write_fake_harness(self.repo.parent / "fake.sh", idle_script())
        code, out, _err = self._init(plan_path, harness)
        self.assertEqual(code, 0)
        run_id, token = parse_init_output(out)
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)

        # The session name is recorded from the real `start_session` rather
        # than reconstructed: state never names it on this path, and a test
        # that guessed the name wrong would pass vacuously (an unknown
        # session never "exists").
        started: list[str] = []
        real_start_session = sessions.start_session

        def _recording_start_session(session_name, *args, **kwargs):
            started.append(session_name)
            return real_start_session(session_name, *args, **kwargs)

        with mock.patch.object(sessions, "start_session", _recording_start_session), mock.patch.object(
            state_mod, "save_state", side_effect=OSError("no space left on device")
        ):
            with self.assertRaises(OSError) as caught:
                self.run_cli_in_repo(["start-slice", "--token", token])

        # The original failure reaches the operator; cleanup never replaces it.
        self.assertIn("no space left on device", str(caught.exception))
        self.assertEqual(len(started), 1, "the launch must have actually happened")
        session = started[0]
        self._sessions_to_reap.append(session)
        self.assertTrue(self._wait_for(lambda: not sessions.session_exists(session), timeout=10.0))

        # Nothing was persisted, so the run is exactly where it was: no
        # current slice, and a relaunch is still attempt 0.
        reloaded = state_mod.load_state(run_dir, token)
        self.assertIsNone(reloaded.get("current_slice"))
        self.assertEqual(reloaded["slices"][0]["attempts"], 0)


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


_WAITED_RE = re.compile(r"^waited:\s*([\d.]+)s \(requested ([\d.]+)s\)$", re.MULTILINE)


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestObserveWaitSemantics(SliceOpsTestCase):
    """`observe --wait` honest-wait semantics: the wait runs the full
    requested duration and breaks early ONLY on session death, `result.json`
    appearing, or a dialog marker on the visible pane — never on a mere pane
    byte-change."""

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
        self.assertNotIn("note:", result["out"], "a result is a signal, not a no-signal wait")

    def test_session_death_mid_wait_ends_wait_early(self) -> None:
        trigger = self.repo.parent / "exit_trigger"
        self._launch(trigger_gated_exit_script(trigger))
        result = self._wait_then_trigger(trigger)
        self.assertIn("session running: False", result["out"])
        self.assertNotIn("note:", result["out"], "a dead session is a signal, not a no-signal wait")

    def test_dialog_marker_mid_wait_ends_wait_early(self) -> None:
        trigger = self.repo.parent / "credential_trigger"
        self._launch(trigger_gated_credential_prompt_script(trigger))
        result = self._wait_then_trigger(trigger)
        self.assertIn("session running: True", result["out"])
        self.assertIn("dialog marker: credential_prompt", result["out"])
        self.assertNotIn("dialog marker: clear", result["out"])
        # The matched text, not just the kind: a PM told only "credential_prompt"
        # cannot tell a real dialog from a literal marker inside ordinary output,
        # and cannot tell whether waiting for the pane to scroll will clear it.
        self.assertIn("Enter API key", result["out"])
        self.assertNotIn("note:", result["out"], "a dialog marker is a signal, not a no-signal wait")

    def test_a_wait_with_no_signal_prints_a_note(self) -> None:
        """A requested wait that elapses with the session still running, no
        result, and no dialog marker is exactly the pattern worth flagging: the
        PM should wait longer next time, not re-ask at the same length."""
        from pm_lib.slice_ops import _OBSERVE_POLL_SECONDS

        self._launch(idle_script(sleep_seconds=120.0))
        code, out, _err = self.run_cli_in_repo(
            ["observe", "--wait", str(2 * _OBSERVE_POLL_SECONDS)]
        )
        self.assertEqual(code, 0, "the note is advisory and must never change the exit code")
        self.assertIn("note: this wait returned no signal", out)

    def test_a_stable_no_signal_wait_still_leaves_a_trace(self) -> None:
        """A no-signal wait against an already-stable pane previously
        appended nothing at all — exactly the call worth counting. The first
        wait on a freshly launched session settles the pane; the second is
        the one under test."""
        from pm_lib.slice_ops import _OBSERVE_POLL_SECONDS

        run_id, _token = self._launch(idle_script(sleep_seconds=120.0))
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        wait_seconds = str(2 * _OBSERVE_POLL_SECONDS)
        self.run_cli_in_repo(["observe", "--wait", wait_seconds])
        before = len([e for e in state_mod.read_events(run_dir) if e["kind"] == "observe"])

        code, out, _err = self.run_cli_in_repo(["observe", "--wait", wait_seconds])
        self.assertEqual(code, 0)
        self.assertIn("note: this wait returned no signal", out)

        after = len([e for e in state_mod.read_events(run_dir) if e["kind"] == "observe"])
        self.assertEqual(after - before, 1, "a stable no-signal wait must still leave a trace")

    def test_an_untimed_observe_prints_no_note(self) -> None:
        self._launch(idle_script(sleep_seconds=120.0))
        code, out, _err = self.run_cli_in_repo(["observe"])
        self.assertEqual(code, 0)
        self.assertNotIn("note:", out, "no wait was requested, so there is nothing to flag")


# --- observe's event append: mandatory vs best-effort, no tmux needed --------


class TestObserveEventAppendFailure(PmTestCase):
    """`observe` appends its event on two different triggers, and they carry
    opposite failure contracts: a real change (pane/liveness/result) MUST be
    recorded, while the advisory trace for a stable no-signal wait must never
    cost the observation. Both are driven off one `append_event` call, so only
    these two tests hold the split in place.
    """

    def _observe(self, *, capture: str, previous: str, wait: float | None):
        """Run `observe` against a synthetic live session whose `append_event`
        always fails. `capture != previous` is what makes the change real."""
        plan_path = self.write_plan(slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        artifact_dir = self.repo / "artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "pane-live.txt").write_text(previous, encoding="utf-8")
        self.set_current_slice(
            state, token, run_dir,
            slice_id="Slice 1", before_head=None, artifact_dir=artifact_dir,
            tmux_session="pm-fake-session",
        )
        activity = {"running": True, "capture": capture}
        with mock.patch.object(sessions, "session_exists", return_value=True), \
             mock.patch.object(sessions, "detect_activity", return_value=activity), \
             mock.patch.object(
                 slice_ops.state_mod, "append_event", side_effect=OSError("events.jsonl unwritable")
             ):
            return slice_ops.observe(self.repo, run_dir, wait=wait, token=token)

    def test_a_real_change_propagates_an_append_failure(self) -> None:
        with self.assertRaises(OSError):
            self._observe(capture="new pane text", previous="old pane text", wait=None)

    def test_a_stable_no_signal_wait_swallows_an_append_failure(self) -> None:
        outcome = self._observe(capture="same pane", previous="same pane", wait=0.01)
        self.assertTrue(outcome.no_signal, "the fixture must reach the best-effort branch")
        self.assertFalse(outcome.pane_changed)


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
        # Both halves of the refusal matter: the matched literal, so PM can see
        # what fired and judge it, and the way forward, since the guard is not
        # overridable and a PM told only "credential_prompt" would otherwise
        # reach for a relaunch that costs an attempt.
        self.assertIn("Enter API key", err)
        self.assertIn("re-issue after the next observe", err)


class TestObserveMarkerAndDeathInTheSamePoll(SliceOpsTestCase):
    """The race the two isolated `observe` tests cannot reach: a marker ends
    the wait and the session dies inside the same poll window.

    The marker result and capture come from one `PaneObservation`; a later
    liveness read cannot replace that screen or report the session as alive.
    """

    def test_marker_wins_the_break_without_losing_liveness_or_the_capture(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        artifact_dir = run_dir / "slices" / "slice-001"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "pane-live.txt").write_text("earlier screen\n", encoding="utf-8")
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=None,
            artifact_dir=artifact_dir, tmux_session="pm-not-a-real-session",
        )

        marker_screen = "working...\nEnter API key to continue\n"
        dialog = sessions.scan_dialog_markers(marker_screen)
        with mock.patch.object(
            slice_ops.sessions, "detect_activity",
            # The pre-marker screen: the loop reads this BEFORE the dialog draws,
            # so a tail taken from it would not contain the marker.
            return_value={"running": True, "capture": "working...\n"},
        ), mock.patch.object(
            slice_ops.sessions, "scan_visible_pane",
            return_value=sessions.PaneObservation(capture=marker_screen, dialog_markers=dialog),
        ), mock.patch.object(
            # Alive when the wait began, dead by the time liveness is re-read.
            slice_ops.sessions, "session_exists", side_effect=[True, False],
        ), mock.patch.object(
            slice_ops.sessions,
            "pane_text",
            side_effect=AssertionError("marker evidence must not be reconstructed by a second read"),
        ):
            outcome = slice_ops.observe(self.repo, run_dir, wait=30.0, token=token)

        self.assertTrue(outcome.dialog_markers["present"], "the marker that ended the wait must be retained")
        self.assertIn("credential_prompt", outcome.dialog_markers["kinds"])
        self.assertFalse(outcome.running, "liveness must be re-read after a marker break")
        self.assertIn(
            "Enter API key", outcome.tail,
            "the tail must be the screen that showed the marker, not the poll before it",
        )
        self.assertIn(
            "Enter API key", (artifact_dir / "pane-live.txt").read_text(encoding="utf-8"),
            "finalize falls back to this file, so it must hold the marker screen too",
        )
        self.assertFalse(outcome.no_signal, "a marker is a signal, not a no-signal wait")
        self.assertLess(outcome.elapsed_seconds, 30.0, "the wait must break early, not run to term")

    def test_session_death_preserves_the_last_good_capture(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        artifact_dir = run_dir / "slices" / "slice-001"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        pane_live = artifact_dir / "pane-live.txt"
        pane_live.write_text("last good screen\n", encoding="utf-8")
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=None,
            artifact_dir=artifact_dir, tmux_session="pm-not-a-real-session",
        )
        clear = sessions.PaneObservation(
            capture=None,
            dialog_markers={"present": False, "kinds": [], "markers": []},
        )

        with mock.patch.object(
            slice_ops.sessions, "detect_activity", return_value={"running": False, "capture": None}
        ), mock.patch.object(
            slice_ops.sessions, "scan_visible_pane", return_value=clear
        ), mock.patch.object(
            slice_ops.sessions, "session_exists", side_effect=[True, False]
        ):
            outcome = slice_ops.observe(self.repo, run_dir, wait=30.0, token=token)

        self.assertFalse(outcome.running)
        self.assertEqual(outcome.tail, "last good screen")
        self.assertEqual(pane_live.read_text(encoding="utf-8"), "last good screen\n")


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


# --- all slices complete -----------------------------------------------------


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


# --- a failed launch leaves no orphan session --------------------------------


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


# --- real-harness composition ------------------------------------------------


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

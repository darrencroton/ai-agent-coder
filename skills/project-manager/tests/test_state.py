"""Protected behaviours: lite-1 state round-trip, authentication, and the CLI stubs.

Pins the single-copy authenticated state model (target-design §8):

- `create_run` writes `run.json`, its `run.json.mac`, and the `current`
  pointer atomically; the returned token authenticates the run and is
  never written to disk in the clear (only its SHA-256 is).
- `load_state` with the correct token verifies the MAC before trusting the
  content; a hand-edited `run.json` (state tampered by something that
  didn't hold the token) fails MAC verification and raises
  `IntegrityError`, both via `load_state(token=...)` and via the explicit
  `verify_state_mac`. A missing MAC file is the same failure. A *wrong*
  token is a distinct, non-integrity failure (`PmError`): the token itself
  doesn't match this run, which is a caller mistake, not evidence of
  tampering.
- A token-less `load_state` (read-only commands) skips MAC verification
  but still shape-validates: schema, run status, plan digest presence,
  and slice status/risk enums.
- A future schema version is refused with a message naming the version,
  not silently migrated.
- `append_event` never rewrites `run.json` (same bytes, same mtime), and
  `read_events` round-trips what was appended.
- Run-dir resolution: the `current` pointer is the default; an explicit
  run id overrides it; a missing pointer or run directory raises a
  helpful `PmError`.
- A linked worktree gets a distinct state root, so two runs created in two
  worktrees of the same repo never interfere.
- `save_state` writes atomically (no temp file survives) and refuses a
  wrong token; holding the advisory lock externally makes a concurrent
  `save_state` fail with the stale-lock message without deleting the lock.
- `new_run_id` appends `-2`, `-3`, ... on collision against a supplied
  existing-id set.
- A slice entry may be created with status "attested" (operator-attested
  prior completion) directly at creation time.
- `check-plan` exercised end-to-end through the CLI on a good and a bad
  plan (exit 0 / exit 2).
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import unittest
from pathlib import Path

from pm_test_helpers import PmTestCase

from pm_lib import IntegrityError, PmError
from pm_lib import state as state_mod


class TestCreateRunRoundTrip(PmTestCase):
    def test_create_run_writes_run_json_mac_and_current_pointer(self) -> None:
        plan_path = self.write_plan()
        state, token, run_dir = self.make_run(plan_path=plan_path)

        self.assertTrue((run_dir / "run.json").exists())
        self.assertTrue((run_dir / "run.json.mac").exists())
        self.assertTrue((run_dir / "events.jsonl").exists())
        pointer = state_mod.state_root(self.repo) / "current"
        self.assertEqual(pointer.read_text(encoding="utf-8").strip(), state["run_id"])

        self.assertEqual(state["schema"], state_mod.SCHEMA)
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["plan"]["slice_count"], 1)
        # The token is never written to disk in the clear.
        raw = (run_dir / "run.json").read_text(encoding="utf-8")
        self.assertNotIn(token, raw)

    def test_load_state_with_token_verifies_and_returns_shape(self) -> None:
        plan_path = self.write_plan()
        state, token, run_dir = self.make_run(plan_path=plan_path)
        loaded = state_mod.load_state(run_dir, token)
        self.assertEqual(loaded["run_id"], state["run_id"])

    def test_slice_entries_accept_attested_status_at_creation(self) -> None:
        plan_path = self.write_plan(slices=[{}, {}])
        state, _token, _run_dir = self.make_run(
            plan_path=plan_path, slice_statuses={"Slice 1": "attested"}
        )
        by_id = {entry["id"]: entry for entry in state["slices"]}
        self.assertEqual(by_id["Slice 1"]["status"], "attested")
        self.assertIsNone(by_id["Slice 2"]["status"])


class TestTamperDetection(PmTestCase):
    def test_hand_edited_run_json_fails_integrity_on_token_load(self) -> None:
        plan_path = self.write_plan()
        _state, token, run_dir = self.make_run(plan_path=plan_path)
        self._flip_status_byte(run_dir)
        with self.assertRaises(IntegrityError):
            state_mod.load_state(run_dir, token)

    def test_hand_edited_run_json_fails_verify_state_mac(self) -> None:
        plan_path = self.write_plan()
        _state, token, run_dir = self.make_run(plan_path=plan_path)
        self._flip_status_byte(run_dir)
        with self.assertRaises(IntegrityError):
            state_mod.verify_state_mac(run_dir, token)

    def test_missing_mac_file_is_integrity_error(self) -> None:
        plan_path = self.write_plan()
        _state, token, run_dir = self.make_run(plan_path=plan_path)
        (run_dir / "run.json.mac").unlink()
        with self.assertRaises(IntegrityError):
            state_mod.load_state(run_dir, token)
        with self.assertRaises(IntegrityError):
            state_mod.verify_state_mac(run_dir, token)

    def test_wrong_token_is_plain_pm_error_not_integrity_error(self) -> None:
        plan_path = self.write_plan()
        _state, _token, run_dir = self.make_run(plan_path=plan_path)
        wrong_token = state_mod.mint_token()
        with self.assertRaises(PmError) as ctx:
            state_mod.load_state(run_dir, wrong_token)
        self.assertNotIsInstance(ctx.exception, IntegrityError)

    def test_token_less_load_skips_mac_but_still_shape_validates(self) -> None:
        plan_path = self.write_plan()
        _state, _token, run_dir = self.make_run(plan_path=plan_path)
        (run_dir / "run.json.mac").unlink()
        loaded = state_mod.load_state(run_dir)  # must not raise despite missing MAC
        self.assertEqual(loaded["schema"], state_mod.SCHEMA)

        # But shape validation still runs: corrupt the schema field directly
        # (bypassing MAC, since no token is supplied) and confirm it's caught.
        raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        raw["schema"] = "lite-2"
        (run_dir / "run.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(PmError):
            state_mod.load_state(run_dir)

    def test_future_schema_version_is_refused_with_message(self) -> None:
        plan_path = self.write_plan()
        _state, _token, run_dir = self.make_run(plan_path=plan_path)
        raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        raw["schema"] = "lite-2"
        (run_dir / "run.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(PmError) as ctx:
            state_mod.load_state(run_dir)
        self.assertIn("lite-2", str(ctx.exception))

    def test_malformed_enum_values_rejected(self) -> None:
        plan_path = self.write_plan()
        _state, _token, run_dir = self.make_run(plan_path=plan_path)
        raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        raw["status"] = "not-a-real-status"
        (run_dir / "run.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(PmError):
            state_mod.load_state(run_dir)

    @staticmethod
    def _flip_status_byte(run_dir: Path) -> None:
        raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        raw["status"] = "needs-human" if raw["status"] != "needs-human" else "active"
        (run_dir / "run.json").write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class TestEventsAndReadback(PmTestCase):
    def test_append_event_does_not_rewrite_run_json(self) -> None:
        plan_path = self.write_plan()
        _state, _token, run_dir = self.make_run(plan_path=plan_path)
        run_json = run_dir / "run.json"
        before_bytes = run_json.read_bytes()
        before_mtime = run_json.stat().st_mtime_ns

        state_mod.append_event(run_dir, "observation", slice_id="Slice 1", note="looked fine")

        after_bytes = run_json.read_bytes()
        after_mtime = run_json.stat().st_mtime_ns
        self.assertEqual(before_bytes, after_bytes)
        self.assertEqual(before_mtime, after_mtime)

    def test_read_events_round_trips(self) -> None:
        plan_path = self.write_plan()
        _state, _token, run_dir = self.make_run(plan_path=plan_path)
        state_mod.append_event(run_dir, "observation", slice_id="Slice 1", note="a")
        state_mod.append_event(run_dir, "send", slice_id="Slice 1", note="b", evidence="pane.txt")
        events = state_mod.read_events(run_dir)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["kind"], "observation")
        self.assertEqual(events[0]["note"], "a")
        self.assertEqual(events[1]["evidence"], "pane.txt")
        for event in events:
            self.assertIn("ts", event)
            self.assertEqual(event["slice"], "Slice 1")

    def test_read_events_empty_when_no_file(self) -> None:
        plan_path = self.write_plan()
        _state, _token, run_dir = self.make_run(plan_path=plan_path)
        (run_dir / "events.jsonl").unlink()
        self.assertEqual(state_mod.read_events(run_dir), [])


class TestRunDirResolution(PmTestCase):
    def test_resolve_run_dir_uses_current_pointer_by_default(self) -> None:
        plan_path = self.write_plan()
        state, _token, run_dir = self.make_run(plan_path=plan_path)
        resolved = state_mod.resolve_run_dir(self.repo)
        self.assertEqual(resolved, run_dir)

    def test_resolve_run_dir_explicit_id_overrides_pointer(self) -> None:
        plan_path = self.write_plan()
        _state1, _token1, run_dir1 = self.make_run(plan_path=plan_path, run_id="run-a")
        _state2, _token2, run_dir2 = self.make_run(plan_path=plan_path, run_id="run-b")
        # `current` now points at run-b (the most recent create_run call).
        self.assertEqual(state_mod.resolve_run_dir(self.repo), run_dir2)
        self.assertEqual(state_mod.resolve_run_dir(self.repo, "run-a"), run_dir1)

    def test_resolve_run_dir_missing_pointer_raises_helpful_error(self) -> None:
        with self.assertRaises(PmError) as ctx:
            state_mod.resolve_run_dir(self.repo)
        self.assertIn("current PM run", str(ctx.exception))

    def test_resolve_run_dir_missing_explicit_id_raises(self) -> None:
        plan_path = self.write_plan()
        self.make_run(plan_path=plan_path)
        with self.assertRaises(PmError):
            state_mod.resolve_run_dir(self.repo, "does-not-exist")


class TestLinkedWorktreeIsolation(PmTestCase):
    def test_linked_worktree_gets_distinct_state_root_no_interference(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            linked_path = Path(tmp) / "linked"
            self._git("worktree", "add", "-b", "linked-branch", str(linked_path))

            plan_path = self.write_plan()
            main_state, _main_token, main_run_dir = self.make_run(plan_path=plan_path)

            linked_plan_path = linked_path / "plan.md"
            linked_plan_path.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
            from pm_lib import plan as plan_mod

            linked_slices = plan_mod.parse_plan(linked_plan_path)
            linked_state, _linked_token, linked_run_dir = state_mod.create_run(
                linked_path,
                plan_path=linked_plan_path,
                plan_sha256=plan_mod.plan_digest(linked_plan_path),
                slice_count=len(linked_slices),
                branch="linked-branch",
                harness={"name": "fake", "model": None, "effort": None},
                reviewer={"tools": [], "model": None, "effort": None},
                policy={"max_attempts": 3, "commit_required": True},
                slices=[
                    {"id": s.slice_id, "title": s.title, "status": None, "risk": s.plan_risk,
                     "plan_risk": s.plan_risk, "commit": None, "attempts": 0}
                    for s in linked_slices
                ],
            )

            self.assertNotEqual(main_run_dir.parent, linked_run_dir.parent)
            self.assertEqual(state_mod.resolve_run_dir(self.repo), main_run_dir)
            self.assertEqual(state_mod.resolve_run_dir(linked_path), linked_run_dir)
            self.assertNotEqual(main_state["run_id"], "collide-with-linked")  # sanity: no crash/collision
            self.assertEqual(linked_state["branch"], "linked-branch")


class TestAtomicSaveAndLocking(PmTestCase):
    def test_atomic_save_leaves_no_temp_litter(self) -> None:
        plan_path = self.write_plan()
        state, token, run_dir = self.make_run(plan_path=plan_path)
        state_mod.save_state(run_dir, state, token)
        leftovers = [p.name for p in run_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_save_state_refuses_wrong_token(self) -> None:
        plan_path = self.write_plan()
        state, _token, run_dir = self.make_run(plan_path=plan_path)
        with self.assertRaises(PmError):
            state_mod.save_state(run_dir, state, state_mod.mint_token())

    def test_concurrent_lock_holder_blocks_save_state_without_deleting_lock(self) -> None:
        plan_path = self.write_plan()
        state, token, run_dir = self.make_run(plan_path=plan_path)
        lock_path = run_dir / ".lock"

        # Hold the lock from a second file descriptor, simulating a concurrent
        # PM process, then patch the retry timeout short so this test is fast.
        holder = open(lock_path, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        original_timeout = state_mod._LOCK_TIMEOUT_SECONDS
        state_mod._LOCK_TIMEOUT_SECONDS = 0.3
        try:
            with self.assertRaises(PmError) as ctx:
                state_mod.save_state(run_dir, state, token)
            self.assertIn(str(lock_path), str(ctx.exception))
        finally:
            state_mod._LOCK_TIMEOUT_SECONDS = original_timeout
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

        self.assertTrue(lock_path.exists())  # never stolen or deleted

    def test_verified_read_pairs_json_and_mac_under_lock(self) -> None:
        plan_path = self.write_plan()
        state, token, run_dir = self.make_run(plan_path=plan_path)

        # Part 1: overwrite run.json with different, still valid-shape JSON
        # bytes directly, without touching the MAC — load_state must catch
        # the resulting json/mac mismatch and fail closed.
        raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        raw["stop_reason"] = "tampered-without-resigning"
        (run_dir / "run.json").write_text(
            json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaises(IntegrityError):
            state_mod.load_state(run_dir, token)

        # Part 2: the verified read takes the same advisory lock save_state
        # holds (so a writer mid-replace of run.json/run.json.mac can never
        # be read as a mismatched pair) — a concurrent lock holder must
        # block the read and time out with a PmError naming the lock,
        # mirroring test_concurrent_lock_holder_blocks_save_state_without_
        # deleting_lock above. A fresh, correctly-signed run isolates this
        # from the tamper detection in Part 1.
        _state2, token2, run_dir2 = self.make_run(plan_path=plan_path, run_id="lock-read-run")
        lock_path = run_dir2 / ".lock"
        holder = open(lock_path, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        original_timeout = state_mod._LOCK_TIMEOUT_SECONDS
        state_mod._LOCK_TIMEOUT_SECONDS = 0.3
        try:
            with self.assertRaises(PmError) as ctx:
                state_mod.load_state(run_dir2, token2)
            self.assertNotIsInstance(ctx.exception, IntegrityError)
            self.assertIn("lock", str(ctx.exception).lower())
        finally:
            state_mod._LOCK_TIMEOUT_SECONDS = original_timeout
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

        self.assertTrue(lock_path.exists())  # never stolen or deleted


class TestNewRunIdCollisions(unittest.TestCase):
    def test_new_run_id_bare_call_has_no_suffix(self) -> None:
        run_id = state_mod.new_run_id()
        self.assertNotIn("-2", run_id)

    def test_new_run_id_appends_suffix_on_collision(self) -> None:
        # new_run_id() derives "now" internally with no seam to freeze it, so
        # exercise the collision/suffix logic by pre-populating an
        # existing-id set with the timestamp it would produce right now.
        import datetime as _dt

        now_base = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        collided = {now_base, f"{now_base}-2"}
        run_id = state_mod.new_run_id(collided)
        self.assertEqual(run_id, f"{now_base}-3")


class TestCliCheckPlanAndStubs(PmTestCase):
    def test_check_plan_cli_exits_zero_on_good_plan(self) -> None:
        plan_path = self.write_plan()
        code, out, _err = self.run_cli(["check-plan", "--plan", str(plan_path)])
        self.assertEqual(code, 0)
        self.assertIn("slice(s)", out)

    def test_check_plan_cli_exits_two_on_bad_plan(self) -> None:
        plan_path = self.write_plan(slices=[{"files": None}])
        code, out, _err = self.run_cli(["check-plan", "--plan", str(plan_path)])
        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)


if __name__ == "__main__":
    unittest.main()

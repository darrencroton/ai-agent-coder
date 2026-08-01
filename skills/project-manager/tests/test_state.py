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
- `new_run_id` returns `<UTC timestamp>-<random nonce>`; ids minted in the
  same second still differ (the nonce is what keeps two runs in separate
  state roots from sharing a tmux session name), and it appends `-2`, `-3`,
  ... on collision against a supplied existing-id set.
- A slice entry may be created with status "attested" (operator-attested
  prior completion) directly at creation time.
- `check-plan` exercised end-to-end through the CLI on a good and a bad
  plan (exit 0 / exit 2).
"""

from __future__ import annotations

import fcntl
import json
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pm_test_helpers import PlanTestCase, PmTestCase

from pm_lib import IntegrityError, PmError
from pm_lib import sessions as sessions_mod
from pm_lib import state as state_mod


class TestLockedUpdate(PmTestCase):
    def test_concurrent_updates_do_not_lose_each_other(self) -> None:
        """save_state locks only its own write, so an unguarded
        load-mutate-save drops a concurrent writer's update."""
        plan_path = self.write_plan()
        state, token, run_dir = self.make_run(plan_path=plan_path)
        state["current_slice"] = {"id": "Slice 1", "reviewer_pids": []}
        state_mod.save_state(run_dir, state, token)

        barrier = threading.Barrier(12)

        def append(pid: int) -> None:
            barrier.wait()
            with state_mod.locked_update(run_dir, token) as live:
                current = live["current_slice"]
                current["reviewer_pids"] = [*(current.get("reviewer_pids") or []), pid]

        threads = [threading.Thread(target=append, args=(1000 + i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = state_mod.load_state(run_dir, token)["current_slice"]["reviewer_pids"]
        self.assertEqual(sorted(final), [1000 + i for i in range(12)])

    def test_verified_load_of_a_missing_run_creates_nothing(self) -> None:
        """_advisory_lock creates run_dir and .lock, so the existence check
        must stay ahead of it or a later create_run for this id refuses."""
        missing = state_mod.state_root(self.repo) / "no-such-run"
        with self.assertRaises(PmError):
            state_mod.load_state(missing, "a" * 64)
        self.assertFalse(missing.exists())

    def test_a_failed_mutation_neither_writes_nor_holds_the_lock(self) -> None:
        plan_path = self.write_plan()
        _state, token, run_dir = self.make_run(plan_path=plan_path)
        before = (run_dir / "run.json").read_bytes()
        with self.assertRaises(RuntimeError):
            with state_mod.locked_update(run_dir, token) as live:
                live["status"] = "stopped"
                raise RuntimeError("boom")
        self.assertEqual((run_dir / "run.json").read_bytes(), before)
        # The lock was released, so a later update still succeeds.
        with state_mod.locked_update(run_dir, token) as live:
            live["status"] = "stopped"
        self.assertEqual(state_mod.load_state(run_dir, token)["status"], "stopped")


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
            self.assertEqual(linked_state["branch"], "linked-branch")
            # Separate state roots are exactly why neither run's collision
            # check can see the other, so the ids must differ on their own —
            # they name the tmux sessions, on a server shared machine-wide.
            # (This replaces a comparison against an unused literal, which
            # could not have failed for any behaviour of the code.)
            self.assertNotEqual(main_state["run_id"], linked_state["run_id"])
            self.assertNotEqual(
                sessions_mod.session_name(main_state["run_id"], 1, 0),
                sessions_mod.session_name(linked_state["run_id"], 1, 0),
            )


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


class _FrozenClock:
    """Stands in for `state.datetime` so a run id's timestamp is predictable."""

    @staticmethod
    def now(tz=None):
        return datetime(2026, 7, 30, 12, 24, 23, tzinfo=timezone.utc)


class TestNewRunId(unittest.TestCase):
    def test_shape_is_timestamp_plus_random_nonce(self) -> None:
        """Asserted as a whole shape, not as the absence of one suffix. The
        previous `"-2" not in run_id` could only ever have caught an
        unconditional suffix bug, since the timestamp contains no hyphen at
        all — it did not check the format it was named for."""
        self.assertRegex(state_mod.new_run_id(), r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")

    def test_ids_minted_in_the_same_second_still_differ(self) -> None:
        """The reason the nonce exists. Two runs in two worktrees or
        repositories cannot see each other's state roots, so the timestamp and
        the `existing` check can both agree while the ids collide — and a
        collision means one shared `sessions.session_name`, on a tmux server
        that is global to the machine.

        The nonce is injected rather than drawn for real: sampling live 24-bit
        nonces and demanding they all differ is a birthday bet the suite would
        lose about once in fourteen thousand runs, and it could not have
        established randomness quality anyway. What must be pinned is that the
        nonce reaches the id at all — so a same-second pair with different
        nonces must produce different ids, and different session names."""
        with mock.patch.object(state_mod, "datetime", _FrozenClock):
            with mock.patch.object(state_mod.secrets, "token_hex", return_value="aaaaaa"):
                first = state_mod.new_run_id()
            with mock.patch.object(state_mod.secrets, "token_hex", return_value="bbbbbb"):
                second = state_mod.new_run_id()

        self.assertNotEqual(first, second)
        self.assertNotEqual(
            sessions_mod.session_name(first, 1, 0), sessions_mod.session_name(second, 1, 0)
        )

    def test_appends_suffix_on_collision(self) -> None:
        """Clock AND nonce frozen, so `base` is exactly predictable. Deriving
        the expected base from a live `datetime.now()` beside production's own
        independent `now()` call made this flake whenever the two landed either
        side of a UTC-second boundary."""
        with mock.patch.object(state_mod, "datetime", _FrozenClock), mock.patch.object(
            state_mod.secrets, "token_hex", return_value="abcdef"
        ):
            base = "20260730T122423Z-abcdef"
            self.assertEqual(state_mod.new_run_id(), base)
            self.assertEqual(state_mod.new_run_id({base, f"{base}-2"}), f"{base}-3")


class TestRunReportHeader(PlanTestCase):
    """The report header is the run's own record of its configuration.

    Harness, reviewer, and attempt budget are the three controls that decide
    whether two runs are comparable at all; a report naming only the harness
    leaves the other two to an operator's separate notes, which is exactly
    where they were observed to drift out of agreement with the run itself.
    """

    def _header(self, **overrides) -> str:
        state = {
            "run_id": "20260729T000000Z",
            "repo": "/repo", "branch": "feature/x", "status": "active",
            "plan": {"path": "/plan.md", "sha256": "a" * 64, "slice_count": 1},
            "harness": {"name": "opencode", "model": "qwen3.6-27b-bf16", "effort": None},
            "reviewer": {"tools": ["codex"], "model": "gpt-5.6-sol", "effort": "high"},
            "policy": {"max_attempts": 10, "commit_required": True},
            "slices": [], "approvals": {}, "stop_reason": None,
        }
        state.update(overrides)
        return state_mod.render_run_report(state, [], Path(self.repo))

    def test_header_records_harness_reviewer_and_budget(self) -> None:
        text = self._header()
        self.assertIn("- Harness: opencode model=qwen3.6-27b-bf16", text)
        self.assertIn("- Reviewer (run default): codex model=gpt-5.6-sol effort=high", text)
        self.assertIn("- Attempt budget: 10 per slice", text)

    def test_header_reports_total_run_time_with_its_endpoints(self) -> None:
        """The report is the single source for run time so PM never hand-parses
        events.jsonl; the endpoints ship with it so the figure is checkable."""
        state = {
            "run_id": "r", "repo": "/repo", "branch": "b", "status": "complete",
            "plan": {"path": "/plan.md", "sha256": "a" * 64}, "harness": {},
            "reviewer": {}, "policy": {}, "slices": [], "approvals": {},
            "stop_reason": None,
        }
        events = [
            {"kind": "init", "ts": "2026-07-30T02:17:16Z"},
            {"kind": "accept", "ts": "2026-07-30T03:10:30Z"},
        ]
        text = state_mod.render_run_report(state, events, Path(self.repo))
        self.assertIn(
            "- Total run time: 53m 14s (2026-07-30T02:17:16Z → 2026-07-30T03:10:30Z, "
            "first to last recorded event)",
            text,
        )

    def test_header_says_unknown_rather_than_zero_without_timestamps(self) -> None:
        self.assertIn("- Total run time: unknown (no timestamped events)", self._header())

    def test_header_survives_a_run_with_no_reviewer_configured(self) -> None:
        text = self._header(reviewer={}, policy={})
        self.assertIn("- Reviewer (run default): None", text)

    def test_absent_policy_renders_the_budget_the_toolkit_actually_enforces(self) -> None:
        """`max_attempts` is absent-tolerant in enforcement (`cli.py`,
        `slice_ops.py` both default to 10). A report printing `None` would
        state a budget no command would apply."""
        text = self._header(policy={})
        self.assertIn("- Attempt budget: 10 per slice", text)


class TestRunElapsed(unittest.TestCase):
    def _duration(self, first: str, last: str) -> str:
        elapsed = state_mod.run_elapsed([{"ts": first}, {"ts": last}])
        assert elapsed is not None
        return elapsed[2]

    def test_formats_seconds_minutes_and_hours(self) -> None:
        self.assertEqual(self._duration("2026-07-30T00:00:00Z", "2026-07-30T00:00:09Z"), "9s")
        self.assertEqual(self._duration("2026-07-30T00:00:00Z", "2026-07-30T00:05:07Z"), "5m 7s")
        self.assertEqual(self._duration("2026-07-30T00:00:00Z", "2026-07-30T02:03:04Z"), "2h 3m 4s")

    def test_spans_min_to_max_not_first_to_last_line(self) -> None:
        """Events are appended, but an out-of-order line must not yield a
        negative or truncated span."""
        elapsed = state_mod.run_elapsed([
            {"ts": "2026-07-30T01:00:00Z"},
            {"ts": "2026-07-30T00:30:00Z"},
            {"ts": "2026-07-30T00:45:00Z"},
        ])
        assert elapsed is not None
        self.assertEqual(elapsed, ("2026-07-30T00:30:00Z", "2026-07-30T01:00:00Z", "30m 0s"))

    def test_ignores_malformed_and_missing_timestamps(self) -> None:
        elapsed = state_mod.run_elapsed([
            {"kind": "init"},
            {"ts": "not-a-timestamp"},
            {"ts": 12345},
            {"ts": "2026-07-30T00:00:00Z"},
            {"ts": "2026-07-30T00:01:00Z"},
        ])
        assert elapsed is not None
        self.assertEqual(elapsed[2], "1m 0s")

    def test_returns_none_rather_than_a_fabricated_zero(self) -> None:
        self.assertIsNone(state_mod.run_elapsed([]))
        self.assertIsNone(state_mod.run_elapsed([{"kind": "init"}, {"ts": "bad"}]))

    def test_offset_naive_timestamps_are_skipped_not_mixed(self) -> None:
        """A naive stamp beside the aware ones `append_event` writes would make
        min/max raise TypeError; alone it would adopt the host's local zone."""
        elapsed = state_mod.run_elapsed([
            {"ts": "2026-07-30T00:00:00Z"},
            {"ts": "2026-07-30T09:00:00"},
            {"ts": "2026-07-30T00:02:00Z"},
        ])
        assert elapsed is not None
        self.assertEqual(elapsed[2], "2m 0s")
        self.assertIsNone(state_mod.run_elapsed([{"ts": "2026-07-30T09:00:00"}]))

    def test_single_event_is_a_zero_span_not_none(self) -> None:
        elapsed = state_mod.run_elapsed([{"ts": "2026-07-30T00:00:00Z"}])
        assert elapsed is not None
        self.assertEqual(elapsed[2], "0s")


class TestCliCheckPlanAndStubs(PlanTestCase):
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

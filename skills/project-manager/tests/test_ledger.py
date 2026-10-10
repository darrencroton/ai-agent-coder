"""Protected behaviours: ledger rows and the per-run ledger file.

`run_rows` is the ledger's whole semantics, so it is tested as the pure
function it is, on hand-built `state`/`events` with timestamps a whole number
of seconds apart. The post-command hook is tested through `cli.main` on real
runs driven by a fake harness, with `PM_LEDGER_DIR` pinned by
`pm_test_helpers` to a private directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import (  # noqa: E402
    PmTestCase,
    TmuxRunTestCase,
    commit_and_result_script,
    judge_current_developer,
    judge_reviews,
    parse_init_output,
    write_fake_harness,
)

from pm_lib import ledger  # noqa: E402
from pm_lib import state as state_mod  # noqa: E402

_HAS_TMUX = shutil.which("tmux") is not None

_LONG_REASONING = (
    "This slice's diff matches the intended change exactly, validation.md shows the "
    "test suite passing, no deviations from the plan were observed, and the pane tail "
    "was clear of dialogs and usage messages."
)

_T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
_OPUS = {"tool": "claude", "model": "claude-opus-5-5", "effort": "high"}
_SONNET = {"tool": "claude", "model": "claude-sonnet-5-5", "effort": "medium"}
_DEFECTS = {"P0": 0, "P1": 1, "P2": 0, "P3": 2}


def _ts(seconds: int) -> str:
    return (_T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Log:
    """An event log built in order; `add` returns the new event's index."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(
        self, kind: str, slice_id: str | None, at: int, data: dict | None = None
    ) -> int:
        event = {"ts": _ts(at), "kind": kind, "slice": slice_id, "note": ""}
        if data is not None:
            event["data"] = data
        self.events.append(event)
        return len(self.events) - 1

    def launch(
        self, slice_id: str, at: int, developer: dict, kind: str = "launch"
    ) -> int:
        return self.add(kind, slice_id, at, {"developer": developer})


def _entry(slice_id: str = "Slice 1", **fields) -> dict:
    return {
        "id": slice_id,
        "status": None,
        "difficulty": "moderate",
        "criteria_total": 3,
        **fields,
    }


def _state(*entries: dict) -> dict:
    return {"run_id": "run-1", "repo_name": "repo", "slices": list(entries)}


def _review(review_id: str, origin: int, skill: str = "code-review") -> dict:
    return {
        "review_id": review_id,
        "skill": skill,
        "tool": "codex",
        "model": "gpt-6.1-sol",
        "effort": "high",
        "command_override": False,
        "origin_event": {"index": origin, "kind": "launch", "slice": "Slice 1"},
    }


# --- run_rows ----------------------------------------------------------------


class TestRunRowsAttribution(unittest.TestCase):
    def test_each_slice_row_holds_only_its_own_events_and_identity(self) -> None:
        log = _Log()
        log.add("init", None, 0)
        s1 = log.launch("Slice 1", 1, _OPUS)
        s2 = log.launch("Slice 2", 2, _SONNET)
        log.add("send", "Slice 1", 3)
        s2_steer = log.add("steer", "Slice 2", 4)
        log.add("floor", "Slice 1", 5)
        log.add("accept", "Slice 1", 6)
        log.add("floor", "Slice 2", 7)
        log.add("accept", "Slice 2", 8)
        state = _state(
            _entry("Slice 1"),
            _entry("Slice 2"),
            _entry("Slice 3", status="attested"),
            _entry("Slice 4"),
        )

        rows = ledger.run_rows(state, log.events)

        self.assertEqual([row["slice_id"] for row in rows], ["Slice 1", "Slice 2"])
        first, second = rows
        self.assertEqual(
            [(sub["origin"], sub["developer"]) for sub in first["submissions"]],
            [(s1, _OPUS)],
        )
        self.assertEqual(
            [
                (sub["origin"], sub["kind"], sub["developer"])
                for sub in second["submissions"]
            ],
            [(s2, "launch", _SONNET), (s2_steer, "steer", _SONNET)],
        )
        # Slice 2's launch (t=2) and steer (t=4) never end Slice 1's working
        # time; its own floor at t=5 does.
        self.assertEqual(first["submissions"][0]["developer_s"], 4)
        self.assertEqual((first["steers"], first["nudges"]), (0, 1))
        self.assertEqual((second["steers"], second["nudges"]), (1, 0))
        self.assertEqual((first["decided_at"], second["decided_at"]), (_ts(6), _ts(8)))

    def test_a_relaunch_under_another_model_makes_a_mixed_window(self) -> None:
        log = _Log()
        launch = log.launch("Slice 1", 0, _OPUS)
        steer = log.add("steer", "Slice 1", 10)
        relaunch = log.launch("Slice 1", 20, _SONNET, kind="relaunch")

        (row,) = ledger.run_rows(_state(_entry()), log.events)

        self.assertEqual(
            [
                (sub["origin"], sub["kind"], sub["developer"])
                for sub in row["submissions"]
            ],
            [
                (launch, "launch", _OPUS),
                (steer, "steer", _OPUS),
                (relaunch, "relaunch", _SONNET),
            ],
        )
        self.assertEqual((row["steers"], row["relaunches"]), (1, 1))


class TestRunRowsOutcomes(unittest.TestCase):
    def _row(self, log: _Log, **entry_fields) -> dict:
        (row,) = ledger.run_rows(_state(_entry(**entry_fields)), log.events)
        return row

    def test_outcome_cause_and_decision_time_follow_the_precedence(self) -> None:
        accepted = _Log()
        accepted.launch("Slice 1", 0, _OPUS)
        accepted.add("floor", "Slice 1", 3)
        accepted.add("accept", "Slice 1", 5)

        stopped = _Log()
        stopped.launch("Slice 1", 0, _OPUS)
        stopped.add("slice-stop", "Slice 1", 4, {"cause": "plan"})

        exhausted = _Log()
        exhausted.launch("Slice 1", 0, _OPUS)
        exhausted.add("steer", "Slice 1", 1)
        exhausted.add("stop", "Slice 1", 3, {"cause": "developer"})
        exhausted.add("floor", "Slice 1", 6)
        exhausted.add("slice-stop", "Slice 1", 7, {"cause": "environment"})

        paused = _Log()
        paused.launch("Slice 1", 0, _OPUS)
        paused.add("stop", "Slice 1", 2)

        cases = {
            "accepted": (accepted, ("accepted", None, _ts(5), 5)),
            "stopped": (stopped, ("stopped", "plan", _ts(4), 4)),
            "exhausted": (exhausted, ("exhausted", "developer", _ts(3), 3)),
            "open": (paused, ("open", None, None, None)),
        }
        for name, (log, expected) in cases.items():
            with self.subTest(name):
                row = self._row(log)
                self.assertEqual(
                    (row["outcome"], row["cause"], row["decided_at"], row["elapsed_s"]),
                    expected,
                )

    def test_pm_stop_then_relaunch_then_accept_is_one_accepted_window(self) -> None:
        log = _Log()
        launch = log.launch("Slice 1", 0, _OPUS)
        log.add("stop", "Slice 1", 2)
        relaunch = log.launch("Slice 1", 3, _OPUS, kind="relaunch")
        log.add("floor", "Slice 1", 5)
        log.add("accept", "Slice 1", 6)

        row = self._row(log)

        self.assertEqual(row["outcome"], "accepted")
        self.assertEqual(
            [sub["origin"] for sub in row["submissions"]], [launch, relaunch]
        )
        self.assertEqual([sub["developer_s"] for sub in row["submissions"]], [2, 2])

    def test_a_fresh_launch_after_finalize_stop_opens_a_second_window(self) -> None:
        log = _Log()
        first = log.launch("Slice 1", 0, _OPUS)
        log.add("slice-stop", "Slice 1", 2, {"cause": "developer"})
        second = log.launch("Slice 1", 4, _SONNET)
        log.add("accept", "Slice 1", 6)
        code = {"lines": {}, "complexity": None, "complexity_reason": "x.py:1: bad"}

        rows = ledger.run_rows(_state(_entry(status="accepted", code=code)), log.events)

        self.assertEqual([row["window_open"] for row in rows], [first, second])
        self.assertEqual(
            [(row["outcome"], row["cause"], row["code"]) for row in rows],
            [("stopped", "developer", None), ("accepted", None, code)],
        )
        self.assertEqual([len(row["submissions"]) for row in rows], [1, 1])


class TestRunRowsDeveloperFigures(unittest.TestCase):
    def test_each_submission_carries_its_active_developer_judgment(self) -> None:
        log = _Log()
        launch = log.launch("Slice 1", 0, _OPUS)
        steer = log.add("steer", "Slice 1", 4)
        unscored = log.add("steer", "Slice 1", 8)

        def judgment(judgment_id: str, origin: int, **fields) -> dict:
            return {
                "judgment_id": judgment_id,
                "submission": {"origin_event": {"index": origin}},
                **fields,
            }

        entry = _entry(
            developer_judgments=[
                judgment("developer-judgment-1", launch, status="unavailable"),
                judgment(
                    "developer-judgment-2",
                    steer,
                    score=0,
                    criteria_met=0,
                    defects=_DEFECTS,
                ),
                judgment(
                    "developer-judgment-3",
                    steer,
                    score=2,
                    criteria_met=3,
                    defects=_DEFECTS,
                    supersedes="developer-judgment-2",
                ),
            ]
        )

        (row,) = ledger.run_rows(_state(entry), log.events)

        self.assertEqual(
            [(sub["origin"], sub["judgment"]) for sub in row["submissions"]],
            [
                (launch, {"status": "unavailable"}),
                (steer, {"score": 2, "criteria_met": 3, "defects": _DEFECTS}),
                (unscored, None),
            ],
        )

    def test_developer_time_ends_at_the_floor_not_the_decision(self) -> None:
        log = _Log()
        log.launch("Slice 1", 0, _OPUS)
        log.add("floor", "Slice 1", 7)
        log.add("accept", "Slice 1", 9)

        (row,) = ledger.run_rows(_state(_entry()), log.events)

        self.assertEqual(row["submissions"][0]["developer_s"], 7)
        self.assertEqual(row["elapsed_s"], 9)

    def test_a_run_wide_stop_ends_developer_time_and_an_unended_submission_has_none(
        self,
    ) -> None:
        log = _Log()
        log.launch("Slice 1", 0, _OPUS)
        log.add("floor", "Slice 2", 1)
        log.add("stop", None, 4)
        log.launch("Slice 2", 5, _SONNET)

        rows = ledger.run_rows(_state(_entry("Slice 1"), _entry("Slice 2")), log.events)

        self.assertEqual(
            [row["submissions"][0]["developer_s"] for row in rows], [4, None]
        )


class TestRunRowsReviews(unittest.TestCase):
    def test_reviews_failures_and_comparisons_are_projected_per_window(self) -> None:
        log = _Log()
        first = log.launch("Slice 1", 0, _OPUS)
        log.add(
            "review-failed",
            "Slice 1",
            1,
            {
                "skill": "code-review",
                "tool": "codex",
                "model": "gpt-6.1-sol",
                "effort": "high",
                "command_override": True,
                "origin_event_index": first,
                "reason": "timeout",
            },
        )
        log.add("slice-stop", "Slice 1", 2, {"cause": "plan"})
        second = log.launch("Slice 1", 3, _OPUS)
        log.add("accept", "Slice 1", 5)
        entry = _entry(
            status="accepted",
            reviews=[
                _review("review-1", first),
                _review("review-2", first),
                _review("review-3", second),
                _review("review-4", second, skill="drift-audit"),
                _review("review-5", second),
                _review("review-6", second),
            ],
            review_judgments=[
                {
                    "judgment_id": "judgment-1",
                    "skill": "code-review",
                    "assessment": "rating",
                    "review_id": "review-3",
                    "score": 2,
                },
                {
                    "judgment_id": "judgment-2",
                    "skill": "drift-audit",
                    "assessment": "rating",
                    "status": "unavailable",
                    "review_ids": ["review-4"],
                },
                {
                    "judgment_id": "judgment-3",
                    "skill": "code-review",
                    "assessment": "comparison",
                    "status": "unavailable",
                    "review_ids": ["review-1", "review-3"],
                },
                {
                    "judgment_id": "judgment-4",
                    "skill": "code-review",
                    "assessment": "comparison",
                    "order": ["review-6", "review-5"],
                    "close": ["review-5"],
                },
            ],
        )

        stopped, accepted = ledger.run_rows(_state(entry), log.events)

        self.assertEqual(
            [
                (review["review_id"], review["rating"])
                for review in stopped["reviews"][:2]
            ],
            [("review-1", None), ("review-2", None)],
        )
        self.assertEqual(
            stopped["reviews"][2],
            {
                "review_id": None,
                "failed": True,
                "skill": "code-review",
                "tool": "codex",
                "model": "gpt-6.1-sol",
                "effort": "high",
                "command_override": True,
                "origin": first,
                "reason": "timeout",
            },
        )
        self.assertEqual(
            accepted["reviews"][0],
            {
                "review_id": "review-3",
                "skill": "code-review",
                "tool": "codex",
                "model": "gpt-6.1-sol",
                "effort": "high",
                "command_override": False,
                "origin": second,
                "rating": {"score": 2},
            },
        )
        self.assertEqual(
            [
                (review["review_id"], review["rating"])
                for review in accepted["reviews"][1:]
            ],
            [
                ("review-4", {"status": "unavailable"}),
                ("review-5", None),
                ("review-6", None),
            ],
        )
        self.assertEqual(
            stopped["comparisons"],
            [{"status": "unavailable", "review_ids": ["review-1"]}],
        )
        self.assertEqual(
            accepted["comparisons"],
            [
                {"status": "unavailable", "review_ids": ["review-3"]},
                {
                    "origin": second,
                    "order": ["review-6", "review-5"],
                    "close": ["review-5"],
                },
            ],
        )


# --- the per-run file and the post-command hook --------------------------------


def _ledger_file(run_id: str) -> Path:
    return Path(os.environ["PM_LEDGER_DIR"]) / ledger.host_name() / f"{run_id}.json"


class LedgerHookTestCase(TmuxRunTestCase):
    def _launch_and_wait(self, slices: list[dict]) -> tuple[str, str, Path]:
        plan_path = self.write_plan(self._plan_path(), slices=slices)
        harness = write_fake_harness(
            self.repo.parent / "fake.sh",
            commit_and_result_script(self.repo, delay=1.0, tail_sleep=2.0),
        )
        code, out, err = self._init(plan_path, harness)
        self.assertEqual(code, 0, err)
        run_id, token = parse_init_output(out)
        self._start_and_wait(run_id, token)
        return run_id, token, state_mod.resolve_run_dir(self.repo, run_id)

    def _start_and_wait(self, run_id: str, token: str) -> None:
        code, _out, err = self.run_cli_in_repo(["start-slice", "--token", token])
        self.assertEqual(code, 0, err)
        self._track_current_session(run_id, token)
        self.assertTrue(self._wait_for_result(run_id, token))

    def _accept(
        self, token: str, run_dir: Path, slice_id: str = "Slice 1"
    ) -> tuple[int, str, str]:
        judge_current_developer(self, token, run_dir, slice_id=slice_id)
        return self.run_cli_in_repo(
            ["finalize", "--accept", _LONG_REASONING, "--token", token]
        )


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestLedgerFileFollowsCommands(LedgerHookTestCase):
    def test_accept_writes_tokenless_report_skips_and_a_late_rating_rewrites(
        self,
    ) -> None:
        run_id, token, run_dir = self._launch_and_wait([{"files": ["a.py"]}])
        reviewer = write_fake_harness(
            self.repo.parent / "fake_code.sh", 'echo "FAKE REVIEW OK"\nexit 0'
        )
        code, _out, err = self.run_cli_in_repo(
            [
                "review",
                "--slice",
                "Slice 1",
                "--skill",
                "code-review",
                "--tool",
                "t1",
                "--reviewer-command",
                str(reviewer),
                "--token",
                token,
            ]
        )
        self.assertEqual(code, 0, err)
        rating = {
            "schema_version": 1,
            "slice": "Slice 1",
            "skill": "code-review",
            "review_id": "review-1",
            "score": 1,
            "reason": "PM verified the findings against the diff.",
        }
        code, out, err = judge_reviews(self, token, run_dir, rating)
        self.assertEqual(code, 0, err)
        first_rating = out.strip().rsplit(": ", 1)[1]

        code, out, err = self._accept(token, run_dir)
        self.assertEqual(code, 0, err)
        self.assertIn("ACCEPTED", out)
        self.assertNotIn("pm: ledger", err)

        path = _ledger_file(run_id)
        written = path.read_bytes()
        payload = json.loads(written)
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["events"], len(state_mod.read_events(run_dir)))
        (row,) = payload["rows"]
        self.assertEqual(row["outcome"], "accepted")
        self.assertEqual(row["reviews"][0]["rating"], {"score": 1})

        # Deterministic: regenerating from the same state and events gives the
        # same bytes the hook wrote.
        for _ in range(2):
            ledger.write_run_file(run_dir, token)
            self.assertEqual(path.read_bytes(), written)

        # An event the file has not seen, then a tokenless report: unverified
        # state never writes, so the file stays exactly as it was.
        state_mod.append_event(
            run_dir, "observe", slice_id="Slice 1", note="out of band"
        )
        with mock.patch.dict(os.environ):
            os.environ.pop("PM_RUN_TOKEN", None)
            code, _out, err = self.run_cli_in_repo(
                ["status", "--report", "--run", run_id]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), written)

        # A rating recorded after the decision reaches the file on the next write.
        code, _out, err = judge_reviews(
            self, token, run_dir, {**rating, "score": 2, "supersedes": first_rating}
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(path.read_bytes())
        self.assertEqual(payload["events"], len(state_mod.read_events(run_dir)))
        self.assertEqual(payload["rows"][0]["reviews"][0]["rating"], {"score": 2})


@unittest.skipUnless(_HAS_TMUX, "tmux is required for slice lifecycle tests")
class TestLedgerWriteFailureNeverBlocks(LedgerHookTestCase):
    def test_a_failed_write_is_loud_and_only_the_finish_report_exits_nonzero(
        self,
    ) -> None:
        run_id, token, run_dir = self._launch_and_wait(
            [{"files": ["a.py"]}, {"files": ["a.py"]}]
        )
        blocker = self.repo.parent / "not-a-directory"
        blocker.write_text("a regular file\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"PM_LEDGER_DIR": str(blocker / "ledger")}):
            code, out, err = self._accept(token, run_dir)
            self.assertEqual(code, 0, err)
            self.assertIn("ACCEPTED", out)
            self.assertIn("pm: ledger: not written:", err)

            code, out, err = self.run_cli_in_repo(
                ["status", "--report", "--token", token]
            )
            self.assertEqual(code, 2)
            self.assertIn("run report regenerated", out)
            self.assertIn("pm: ledger: not written:", err)

        self._start_and_wait(run_id, token)
        with mock.patch.object(ledger, "run_rows", side_effect=KeyError("boom")):
            code, out, err = self._accept(token, run_dir, "Slice 2")
        self.assertEqual(code, 0, err)
        self.assertIn("ACCEPTED", out)
        self.assertIn("pm: ledger: not written: KeyError: 'boom'", err)


@unittest.skipUnless(_HAS_TMUX, "tmux is required for the scavenge sweep")
class TestLedgerSilentWithoutRunState(PmTestCase):
    def test_scavenge_after_the_run_directory_is_gone_prints_no_ledger_line(
        self,
    ) -> None:
        _state, token, run_dir = self.make_run(plan_path=self.write_plan())
        shutil.rmtree(run_dir)

        code, out, err = self.run_cli_in_repo(
            [
                "stop",
                "--reason",
                "state removed",
                "--scavenge",
                "--run",
                run_dir.name,
                "--token",
                token,
            ]
        )

        self.assertEqual(code, 0, err)
        self.assertIn("state unavailable", out)
        self.assertNotIn("pm: ledger", out + err)


if __name__ == "__main__":
    unittest.main()

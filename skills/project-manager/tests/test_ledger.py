"""Protected behaviours: ledger rows and the per-run ledger file.

`run_rows` is the ledger's whole semantics, so it is tested as the pure
function it is, on hand-built `state`/`events` with timestamps a whole number
of seconds apart. The post-command hook is tested through `cli.main` on real
runs driven by a fake harness, with `PM_LEDGER_DIR` pinned by
`pm_test_helpers` to a private directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
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

from pm_lib import cli  # noqa: E402
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


# --- pm ledger render ------------------------------------------------------------

_CODEX = ("codex", "gpt-6.1-sol", "high", False)
_CLAUDE = ("claude", "claude-opus-5-5", "high", False)
_QWEN = ("qwen", "qwen-4", None, True)
_ZERO_DEFECTS = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}


def _sub(
    developer: dict,
    score: int | None = 2,
    *,
    criteria_met: int = 3,
    defects: dict | None = None,
    developer_s: int | None = 60,
) -> dict:
    """One submission; `score=None` makes it an `unavailable` one."""
    if score is None:
        judgment: dict = {"status": "unavailable"}
    else:
        judgment = {
            "score": score,
            "criteria_met": criteria_met,
            "defects": defects or _ZERO_DEFECTS,
        }
    return {
        "origin": 0,
        "kind": "launch",
        "developer": developer,
        "judgment": judgment,
        "developer_s": developer_s,
    }


def _rev(
    review_id: str, key: tuple, rating: dict | None, skill: str = "code-review"
) -> dict:
    tool, model, effort, override = key
    return {
        "review_id": review_id,
        "skill": skill,
        "tool": tool,
        "model": model,
        "effort": effort,
        "command_override": override,
        "origin": 0,
        "rating": rating,
    }


def _failed(key: tuple, skill: str = "code-review") -> dict:
    tool, model, effort, override = key
    return {
        "review_id": None,
        "failed": True,
        "skill": skill,
        "tool": tool,
        "model": model,
        "effort": effort,
        "command_override": override,
        "origin": 0,
        "reason": "timeout",
    }


def _code(production: tuple[int, int, int], before: int, after: int) -> dict:
    lines = {
        category: {"code": 0, "comment": 0, "blank": 0}
        for category in (
            "production",
            "test",
            "documentation",
            "configuration",
            "data",
            "other",
        )
    }
    lines["production"] = dict(
        zip(("code", "comment", "blank"), production, strict=True)
    )
    return {
        "lines": lines,
        "complexity": {
            "before": {"sum": before, "max": before},
            "after": {"sum": after, "max": after},
        },
        "complexity_reason": None,
    }


def _row(
    submissions: list[dict],
    *,
    outcome: str = "accepted",
    cause: str | None = None,
    slice_id: str = "Slice 1",
    difficulty: str | None = "moderate",
    decided_at: str | None = None,
    elapsed_s: int | None = 600,
    steers: int = 0,
    nudges: int = 0,
    code: dict | None = None,
    reviews: list[dict] | None = None,
    comparisons: list[dict] | None = None,
) -> dict:
    return {
        "repo_name": "repo",
        "run_id": "run-1",
        "slice_id": slice_id,
        "window_open": 0,
        "difficulty": difficulty,
        "criteria_total": 3,
        "outcome": outcome,
        "cause": cause,
        "decided_at": decided_at if outcome != "open" else None,
        "elapsed_s": elapsed_s,
        "steers": steers,
        "relaunches": 0,
        "nudges": nudges,
        "code": code,
        "submissions": submissions,
        "reviews": reviews or [],
        "comparisons": comparisons or [],
    }


def _file(run_id: str, rows: list[dict], events: int = 10) -> bytes:
    payload = {"run_id": run_id, "events": events, "rows": rows}
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _render(*files: tuple[str, bytes]) -> str:
    text, _errors = ledger.render_leaderboard(list(files))
    return text


def _section(text: str, heading: str) -> str:
    """The body under `heading` up to the next heading of the same or higher level."""
    level = heading.split(" ", 1)[0]
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("#")
            and len(lines[index].split(" ", 1)[0]) <= len(level)
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _tables(section: str) -> list[list[dict[str, str]]]:
    """Every markdown table in `section`, each as a list of header→cell rows."""
    tables: list[list[dict[str, str]]] = []
    block: list[list[str]] = []
    for line in [*section.splitlines(), ""]:
        if line.startswith("|"):
            block.append([cell.strip() for cell in line.strip("|").split(" | ")])
            continue
        if block:
            headers = block[0]
            tables.append(
                [dict(zip(headers, cells, strict=True)) for cells in block[2:]]
            )
            block = []
    return tables


def _by_name(table: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Index a Developer table by its identity column."""
    return {row["Developer (tool / model / effort)"]: row for row in table}


_OPUS_LABEL = "claude / claude-opus-5-5 / high"
_SONNET_LABEL = "claude / claude-sonnet-5-5 / medium"


class TestLeaderboardColumns(unittest.TestCase):
    """Every column on a fixture whose values are worked out by hand."""

    def _text(self) -> str:
        rows = [
            # Two scored submissions: only the first is "first", both count.
            _row(
                [
                    _sub(
                        _OPUS,
                        1,
                        criteria_met=2,
                        defects={"P0": 0, "P1": 1, "P2": 1, "P3": 1},
                        developer_s=100,
                    ),
                    _sub(_OPUS, 2, developer_s=200),
                ],
                elapsed_s=600,
                steers=1,
                nudges=1,
                code=_code((10, 2, 1), 4, 7),
                reviews=[
                    _rev("review-1", _CODEX, {"score": 2}),
                    _rev("review-2", _CLAUDE, {"score": 1}),
                    _rev("review-3", _QWEN, {"score": 1}),
                ],
                comparisons=[
                    {
                        "origin": 0,
                        "order": ["review-1", "review-2", "review-3"],
                        "close": ["review-2"],
                    }
                ],
            ),
            _row(
                [_sub(_OPUS, 2, developer_s=300)],
                slice_id="Slice 2",
                elapsed_s=300,
                code=_code((20, 0, 3), 0, 1),
                reviews=[
                    _rev("review-1", _CODEX, {"score": 1}),
                    _rev("review-2", _CLAUDE, {"score": 2}),
                ],
                comparisons=[
                    {
                        "origin": 0,
                        "order": ["review-2", "review-1"],
                        "close": ["review-1"],
                    }
                ],
            ),
            # Exhausted: the unavailable first submission is skipped, so the
            # steer is the first scored one.
            _row(
                [
                    _sub(_OPUS, None),
                    _sub(
                        _OPUS,
                        0,
                        criteria_met=1,
                        defects={"P0": 1, "P1": 1, "P2": 0, "P3": 2},
                        developer_s=None,
                    ),
                ],
                outcome="exhausted",
                cause="developer",
                slice_id="Slice 3",
                elapsed_s=900,
                steers=2,
                nudges=1,
                reviews=[_rev("review-1", _CODEX, {"score": 2}), _failed(_QWEN)],
            ),
            # Accepted with a failed code block and no wall time.
            _row(
                [_sub(_OPUS, 2, developer_s=50)],
                slice_id="Slice 4",
                elapsed_s=None,
                code={"error": "OSError: boom"},
                reviews=[_rev("review-1", _CLAUDE, None)],
            ),
        ]
        return _render(("host/run-1.json", _file("run-1", rows)))

    def test_developer_columns_match_hand_computed_values(self) -> None:
        tables = _tables(_section(self._text(), "### All difficulties"))
        self.assertEqual(len(tables), 1)
        row = _by_name(tables[0])[_OPUS_LABEL]
        expected = {
            "Rank": "1",
            "Score (PM), mean 0–2": "1.40 (n=5)",
            "First-submission criteria met (PM), Σk/Σn": "9/12 (n=4)",
            "First-submission P0+P1 defects (PM), mean": "0.75 (n=4)",
            "First-submission P2+P3 defects (PM), mean": "1.00 (n=4)",
            "Developer time per submission, median s": "150 (n=4)",
            "Windows, n": "4",
            "Accepted, share": "3/4 (75%)",
            "Accepted on first submission, share": "2/4 (50%)",
            "Scored submissions per window, mean": "1.25 (n=4)",
            "Steers+nudges per window, mean": "1.25 (n=4)",
            "Wall time per window, median s": "600 (n=3)",
            "Δ production lines code/comment/blank, median": "15/1/2 (n=2)",
            "Δ test lines code/comment/blank, median": "0/0/0 (n=2)",
            "Δ complexity sum, median": "2 (n=2)",
        }
        self.assertEqual({key: row[key] for key in expected}, expected)

    def test_difficulty_bands_without_counted_windows_say_so(self) -> None:
        text = self._text()
        self.assertEqual(
            _section(text, "### Difficulty: easy").strip(),
            "No Developer-counted windows.",
        )
        self.assertIn(_OPUS_LABEL, _section(text, "### Difficulty: moderate"))

    def test_reviewer_columns_match_hand_computed_values(self) -> None:
        tables = _tables(_section(self._text(), "### code-review"))
        ranked, insufficient = tables
        # Codex and Claude tie on pairwise share, so group-key order decides.
        self.assertEqual(
            [row["Reviewer (tool / model / effort / override)"] for row in ranked],
            [
                "claude / claude-opus-5-5 / high / no override",
                "codex / gpt-6.1-sol / high / no override",
            ],
        )
        claude, codex = ranked
        self.assertEqual(
            [
                claude[h]
                for h in (
                    "Rank",
                    "Commissions, n",
                    "Score (PM), mean 0–2",
                    "Panels (PM), n",
                )
            ],
            ["1", "3", "1.50 (n=2)", "2"],
        )
        self.assertEqual(
            claude["Usable (PM), share rated/(rated+unavailable+failed)"], "2/2 (100%)"
        )
        self.assertEqual(claude["Pairwise wins (PM), share"], "2/3 (67%)")
        self.assertEqual(claude["Close-call wins (PM), share"], "1/2 (50%)")
        self.assertEqual(codex["Score (PM), mean 0–2"], "1.67 (n=3)")
        self.assertEqual(codex["Pairwise wins (PM), share"], "2/3 (67%)")
        self.assertEqual(codex["Close-call wins (PM), share"], "1/2 (50%)")
        (qwen,) = insufficient
        self.assertNotIn("Rank", qwen)
        self.assertEqual(
            qwen["Reviewer (tool / model / effort / override)"],
            "qwen / qwen-4 / unknown / override",
        )
        self.assertEqual(qwen["Commissions, n"], "2")
        self.assertEqual(
            qwen["Usable (PM), share rated/(rated+unavailable+failed)"], "1/2 (50%)"
        )
        self.assertEqual(qwen["Pairwise wins (PM), share"], "0/2 (0%)")
        self.assertEqual(qwen["Close-call wins (PM), share"], "n/a")

    def test_presentation_carries_no_wall_clock_or_composite_score(self) -> None:
        text = self._text()
        self.assertIn("## Errors\n\nNone.\n", text)
        self.assertNotIn("composite", text.replace("There is no composite score.", ""))
        self.assertNotIn(datetime.now(timezone.utc).strftime("%Y-%m-%d"), text)


class TestLeaderboardWindowSelection(unittest.TestCase):
    def test_open_and_plan_or_environment_stops_count_only_for_reviewers(self) -> None:
        drift = ("codex", "gpt-6.1-sol", "high", False)
        rows = [
            _row(
                [_sub(_SONNET)],
                outcome="open",
                reviews=[_rev("review-1", drift, {"score": 2}, "drift-audit")],
            ),
            _row(
                [_sub(_SONNET)],
                outcome="stopped",
                cause="plan",
                reviews=[_rev("review-1", drift, {"score": 1}, "drift-audit")],
            ),
            _row(
                [_sub(_SONNET)],
                outcome="stopped",
                cause="environment",
                reviews=[_rev("review-1", drift, {"score": 0}, "drift-audit")],
            ),
        ]
        text = _render(("host/run-1.json", _file("run-1", rows)))

        self.assertEqual(
            _section(text, "### All difficulties").strip(),
            "No Developer-counted windows.",
        )
        (ranked,) = _tables(_section(text, "### drift-audit"))
        self.assertEqual(ranked[0]["Commissions, n"], "3")
        self.assertEqual(ranked[0]["Score (PM), mean 0–2"], "1.00 (n=3)")

    def test_mixed_and_unscored_windows_are_footnoted_but_submissions_still_count(
        self,
    ) -> None:
        rows = [
            _row(
                [
                    _sub(_OPUS, 1, criteria_met=1, developer_s=10),
                    _sub(_SONNET, 2, developer_s=None),
                ]
            ),
            _row([_sub(_OPUS, None)], slice_id="Slice 2"),
        ]
        section = _section(
            _render(("host/run-1.json", _file("run-1", rows))), "### All difficulties"
        )

        self.assertIn(
            "leave out 1 window(s) with mixed Developer identities and 1 window(s) with no scored submission",
            section,
        )
        (insufficient,) = _tables(section)
        opus, sonnet = (
            _by_name(insufficient)[_OPUS_LABEL],
            _by_name(insufficient)[_SONNET_LABEL],
        )
        self.assertEqual(opus["Score (PM), mean 0–2"], "1.00 (n=1)")
        self.assertEqual(opus["First-submission criteria met (PM), Σk/Σn"], "1/3 (n=1)")
        self.assertEqual(opus["Developer time per submission, median s"], "10 (n=1)")
        self.assertEqual(sonnet["Score (PM), mean 0–2"], "2.00 (n=1)")
        self.assertEqual(sonnet["First-submission criteria met (PM), Σk/Σn"], "n/a")
        self.assertEqual(sonnet["Developer time per submission, median s"], "n/a")
        for group in (opus, sonnet):
            self.assertEqual(group["Windows, n"], "0")
            self.assertEqual(group["Accepted, share"], "n/a")
            self.assertEqual(group["Wall time per window, median s"], "n/a")

    def test_small_groups_are_unranked_and_full_ties_keep_group_key_order(self) -> None:
        def identity(model: str) -> dict:
            return {"tool": "claude", "model": model, "effort": "high"}

        rows = [
            *(
                _row([_sub(identity(model), 1)], slice_id=f"{model} {n}")
                for model in ("beta", "alpha")
                for n in range(3)
            ),
            *(
                _row([_sub(identity("zeta"), 2)], slice_id=f"zeta {n}")
                for n in range(3)
            ),
            *(_row([_sub(identity("aaa"), 2)], slice_id=f"aaa {n}") for n in range(2)),
        ]
        section = _section(
            _render(("host/run-1.json", _file("run-1", rows))), "### All difficulties"
        )
        ranked, insufficient = _tables(section)

        self.assertEqual(
            [(row["Rank"], row["Developer (tool / model / effort)"]) for row in ranked],
            [
                ("1", "claude / zeta / high"),
                ("2", "claude / alpha / high"),
                ("3", "claude / beta / high"),
            ],
        )
        self.assertIn("Insufficient data (fewer than 3 windows), unranked:", section)
        self.assertEqual(
            [row["Developer (tool / model / effort)"] for row in insufficient],
            ["claude / aaa / high"],
        )
        self.assertNotIn("Rank", insufficient[0])

    def test_usable_share_leaves_unrated_reviews_out_of_both_sides(self) -> None:
        drift = ("codex", "gpt-6.1-sol", "high", False)
        reviews = [
            _rev("review-1", drift, {"score": 2}, "drift-audit"),
            _rev("review-2", drift, {"status": "unavailable"}, "drift-audit"),
            _rev("review-3", drift, None, "drift-audit"),
            _failed(drift, "drift-audit"),
        ]
        text = _render(
            ("host/run-1.json", _file("run-1", [_row([_sub(_OPUS)], reviews=reviews)]))
        )
        (ranked,) = _tables(_section(text, "### drift-audit"))

        self.assertEqual(ranked[0]["Commissions, n"], "4")
        self.assertEqual(
            ranked[0]["Usable (PM), share rated/(rated+unavailable+failed)"],
            "1/3 (33%)",
        )


class TestLeaderboardFiles(unittest.TestCase):
    def _rows(self, decided_at: str, score: int = 2) -> list[dict]:
        return [_row([_sub(_OPUS, score)], decided_at=decided_at)]

    def test_output_ignores_repetition_host_copies_and_file_order(self) -> None:
        one = ("mac/run-1.json", _file("run-1", self._rows(_ts(10))))
        two = ("mac/run-2.json", _file("run-2", self._rows(_ts(20), 1)))
        first = _render(one, two)

        self.assertEqual(_render(one, two), first)
        self.assertEqual(_render(two, one), first)
        copied = [("studio/run-1.json", one[1]), ("studio/run-2.json", two[1])]
        self.assertEqual(_render(two, *copied, one), first)

    def test_more_events_win_and_equal_events_with_different_bytes_are_errors(
        self,
    ) -> None:
        files = [
            ("a/run-1.json", _file("run-1", self._rows(_ts(500)), events=5)),
            ("b/run-1.json", _file("run-1", self._rows(_ts(100)), events=7)),
            ("a/run-2.json", _file("run-2", self._rows(_ts(10)), events=3)),
            ("b/run-2.json", _file("run-2", self._rows(_ts(20)), events=3)),
            ("b/broken.json", b"{not json"),
            ("b/partial.json", b'{"run_id": "run-3", "events": 1}'),
        ]
        text, errors = ledger.render_leaderboard(files)

        self.assertIn("Runs: 1; slice windows: 1.", text)
        # The 5-event copy of run-1 (decided later) lost to the 7-event copy.
        self.assertIn(f"As of: {_ts(100)}", text)
        self.assertEqual(
            [error.split(":", 1)[0] for error in errors],
            ["a/run-2.json", "b/broken.json", "b/partial.json", "b/run-2.json"],
        )
        self.assertIn("different contents", errors[0])
        self.assertIn("not valid JSON", errors[1])
        self.assertIn("missing or invalid run_id, events or rows", errors[2])
        for error in errors:
            self.assertIn(f"- {error}", _section(text, "## Errors"))

    def test_as_of_is_the_latest_decision_or_unknown(self) -> None:
        files = [
            ("h/run-1.json", _file("run-1", self._rows(_ts(30)))),
            (
                "h/run-2.json",
                _file(
                    "run-2", [*self._rows(_ts(90)), _row([_sub(_OPUS)], outcome="open")]
                ),
            ),
        ]
        self.assertIn(f"As of: {_ts(90)}\n", _render(*files))
        undecided = _file("run-3", [_row([_sub(_OPUS)], outcome="open")])
        self.assertIn("As of: unknown\n", _render(("h/run-3.json", undecided)))

    def test_history_link_is_relative_to_the_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            history = Path(scratch) / "skill" / "ledger" / "historical-pre-ledger.md"
            history.parent.mkdir(parents=True)
            history.write_text("frozen\n", encoding="utf-8")
            files = [("h/run-1.json", _file("run-1", self._rows(_ts(1))))]
            with mock.patch.object(ledger, "_HISTORY_PATH", history):
                text, _ = ledger.render_leaderboard(
                    files, out_dir=Path(scratch) / "out"
                )
            with mock.patch.object(
                ledger, "_HISTORY_PATH", history.with_name("absent.md")
            ):
                plain, _ = ledger.render_leaderboard(
                    files, out_dir=Path(scratch) / "out"
                )

        self.assertIn(
            "History: [historical-pre-ledger.md](../skill/ledger/historical-pre-ledger.md)",
            text,
        )
        self.assertNotIn("History:", plain)

    def test_history_link_is_percent_encoded(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            history = Path(scratch) / "my hist#1 (old)" / "historical-pre-ledger.md"
            history.parent.mkdir()
            history.write_text("frozen\n", encoding="utf-8")
            files = [("h/run-1.json", _file("run-1", self._rows(_ts(1))))]
            with mock.patch.object(ledger, "_HISTORY_PATH", history):
                text, _ = ledger.render_leaderboard(
                    files, out_dir=Path(scratch) / "out"
                )

        self.assertIn("(../my%20hist%231%20%28old%29/historical-pre-ledger.md)", text)

    def test_malformed_row_interiors_are_errors_not_crashes(self) -> None:
        good = ("h/good.json", _file("good", self._rows(_ts(1))))

        def with_row(**fields) -> bytes:
            return _file("bad", [{**_row([_sub(_OPUS)]), **fields}])

        reviewer = _rev("review-1", _CODEX, {"score": 2})
        cases = {
            "submissions": with_row(submissions=5),
            "difficulty": with_row(difficulty=["x"]),
            "review_id": with_row(reviews=[{**reviewer, "review_id": ["a"]}]),
            "tool": with_row(reviews=[{**reviewer, "tool": {"x": 1}}]),
            "developer": with_row(
                submissions=[{**_sub(_OPUS), "developer": {"tool": ["x"]}}]
            ),
            "order": with_row(
                comparisons=[{"origin": 0, "order": [["a"]], "close": []}]
            ),
            "events": b'{"run_id": "bad", "events": ' + b"9" * 5000 + b', "rows": []}',
        }
        for name, raw in cases.items():
            with self.subTest(name):
                text, errors = ledger.render_leaderboard([good, ("h/bad.json", raw)])

                self.assertIn("Runs: 1; slice windows: 1.", text)
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0].startswith("h/bad.json: "), errors[0])
                self.assertIn(f"- {errors[0]}", _section(text, "## Errors"))

    def test_means_round_half_up_from_the_exact_value(self) -> None:
        # Scores 1×7 and 2 → 9/8 = 1.125 exactly; binary float formatting gives 1.12.
        scores = [1] * 7 + [2]
        rows = [
            _row([_sub(_OPUS, score)], slice_id=f"Slice {n}")
            for n, score in enumerate(scores)
        ]
        section = _section(
            _render(("h/run-1.json", _file("run-1", rows))), "### All difficulties"
        )
        (ranked,) = _tables(section)

        self.assertEqual(ranked[0]["Score (PM), mean 0–2"], "1.13 (n=8)")


class TestLedgerRenderCommand(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "ledger"
        patcher = mock.patch.dict(os.environ, {"PM_LEDGER_DIR": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_out_path_receives_the_leaderboard(self) -> None:
        host = self.root / "mac"
        host.mkdir(parents=True)
        (host / "run-1.json").write_bytes(
            _file("run-1", [_row([_sub(_OPUS)], decided_at=_ts(5))])
        )
        out = self.root.parent / "elsewhere" / "board.md"

        code, stdout, err = self._main(["ledger", "render", "--out", str(out)])

        self.assertEqual(code, 0, err)
        self.assertIn(str(out), stdout)
        self.assertTrue(
            out.read_text(encoding="utf-8").startswith("# PM model leaderboard\n")
        )
        self.assertFalse((self.root / "leaderboard.md").exists())

    def test_no_files_exits_two_naming_the_directory(self) -> None:
        code, _stdout, err = self._main(["ledger", "render"])

        self.assertEqual(code, 2)
        self.assertIn(f"no per-run ledger files found under {self.root}", err)
        self.assertNotIn("pm: ledger: not written", err)

    def _seed(self) -> None:
        host = self.root / "mac"
        host.mkdir(parents=True)
        (host / "run-1.json").write_bytes(
            _file("run-1", [_row([_sub(_OPUS)], decided_at=_ts(5))])
        )

    def test_an_unwritable_out_path_is_an_error_naming_it(self) -> None:
        self._seed()
        out = self.root.parent / "a-directory"
        out.mkdir()

        code, _stdout, err = self._main(["ledger", "render", "--out", str(out)])

        self.assertEqual(code, 2)
        self.assertIn(f"pm: error: cannot write the leaderboard to {out}", err)
        self.assertNotIn("Traceback", err)

    def test_history_link_is_relative_to_an_output_symlinks_own_directory(self) -> None:
        self._seed()
        history = self.root.parent / "skill" / "ledger" / "historical-pre-ledger.md"
        history.parent.mkdir(parents=True)
        history.write_text("frozen\n", encoding="utf-8")
        target = self.root.parent / "deep" / "er" / "real.md"
        target.parent.mkdir(parents=True)
        target.write_text("old\n", encoding="utf-8")
        out = self.root.parent / "board.md"
        out.symlink_to(target)

        with mock.patch.object(ledger, "_HISTORY_PATH", history):
            code, _stdout, err = self._main(["ledger", "render", "--out", str(out)])

        self.assertEqual(code, 0, err)
        self.assertIn(
            "(skill/ledger/historical-pre-ledger.md)", out.read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()

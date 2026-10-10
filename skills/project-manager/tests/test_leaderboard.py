"""Protected behaviours: the cross-run leaderboard (`pm ledger render`).

`render_leaderboard` is tested as the pure function it is, on hand-built
per-run files whose every cell is worked out by hand; `load_run_files` on a
real directory; the command through `cli.main` with `PM_LEDGER_DIR` pointed
at a private directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import OPUS, SONNET, ledger_ts  # noqa: E402

from pm_lib import cli  # noqa: E402
from pm_lib import leaderboard  # noqa: E402
from pm_lib import state as state_mod  # noqa: E402

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
    review_id: str,
    key: tuple,
    rating: dict | None,
    skill: str = "code-review",
    model_tag: str | None = None,
) -> dict:
    tool, model, effort, override = key
    return {
        "review_id": review_id,
        "skill": skill,
        "tool": tool,
        "model": model,
        "effort": effort,
        "command_override": override,
        "model_tag": model_tag,
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
    lines["production"] = dict(zip(("code", "comment", "blank"), production, strict=True))
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


def _file(run_id: str, rows: list[dict], events: int = 10, **extra) -> bytes:
    """A per-run file; `extra` adds top-level keys (a `run_tag`, say), absent by default as in older files."""
    payload = {"run_id": run_id, "events": events, "rows": rows, **extra}
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _render(*files: tuple[str, bytes]) -> str:
    text, _errors = leaderboard.render_leaderboard(list(files))
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
            if lines[index].startswith("#") and len(lines[index].split(" ", 1)[0]) <= len(level)
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
            tables.append([dict(zip(headers, cells, strict=True)) for cells in block[2:]])
            block = []
    return tables


def _by_name(table: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Index a Developer table by its identity column."""
    return {row["Developer (tool / model / effort / tag)"]: row for row in table}


_OPUS_LABEL = "claude / claude-opus-5-5 / high / untagged"
_SONNET_LABEL = "claude / claude-sonnet-5-5 / medium / untagged"


class TestLeaderboardColumns(unittest.TestCase):
    """Every column on a fixture whose values are worked out by hand."""

    def _text(self) -> str:
        rows = [
            # Two scored submissions: only the first is "first", both count.
            _row(
                [
                    _sub(
                        OPUS,
                        1,
                        criteria_met=2,
                        defects={"P0": 0, "P1": 1, "P2": 1, "P3": 1},
                        developer_s=100,
                    ),
                    _sub(OPUS, 2, developer_s=200),
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
                [_sub(OPUS, 2, developer_s=300)],
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
                    _sub(OPUS, None),
                    _sub(
                        OPUS,
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
                [_sub(OPUS, 2, developer_s=50)],
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
            [row["Reviewer (tool / model / effort / override / tag)"] for row in ranked],
            [
                "claude / claude-opus-5-5 / high / no override / untagged",
                "codex / gpt-6.1-sol / high / no override / untagged",
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
        self.assertEqual(claude["Usable (PM), share rated/(rated+unavailable+failed)"], "2/2 (100%)")
        self.assertEqual(claude["Pairwise wins (PM), share"], "2/3 (67%)")
        self.assertEqual(claude["Close-call wins (PM), share"], "1/2 (50%)")
        self.assertEqual(codex["Score (PM), mean 0–2"], "1.67 (n=3)")
        self.assertEqual(codex["Pairwise wins (PM), share"], "2/3 (67%)")
        self.assertEqual(codex["Close-call wins (PM), share"], "1/2 (50%)")
        (qwen,) = insufficient
        self.assertNotIn("Rank", qwen)
        self.assertEqual(
            qwen["Reviewer (tool / model / effort / override / tag)"],
            "qwen / qwen-4 / unknown / override / untagged",
        )
        self.assertEqual(qwen["Commissions, n"], "2")
        self.assertEqual(qwen["Usable (PM), share rated/(rated+unavailable+failed)"], "1/2 (50%)")
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
                [_sub(SONNET)],
                outcome="open",
                reviews=[_rev("review-1", drift, {"score": 2}, "drift-audit")],
            ),
            _row(
                [_sub(SONNET)],
                outcome="stopped",
                cause="plan",
                reviews=[_rev("review-1", drift, {"score": 1}, "drift-audit")],
            ),
            _row(
                [_sub(SONNET)],
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
                    _sub(OPUS, 1, criteria_met=1, developer_s=10),
                    _sub(SONNET, 2, developer_s=None),
                ]
            ),
            _row([_sub(OPUS, None)], slice_id="Slice 2"),
        ]
        section = _section(_render(("host/run-1.json", _file("run-1", rows))), "### All difficulties")

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
            *(_row([_sub(identity("zeta"), 2)], slice_id=f"zeta {n}") for n in range(3)),
            *(_row([_sub(identity("aaa"), 2)], slice_id=f"aaa {n}") for n in range(2)),
        ]
        section = _section(_render(("host/run-1.json", _file("run-1", rows))), "### All difficulties")
        ranked, insufficient = _tables(section)

        self.assertEqual(
            [(row["Rank"], row["Developer (tool / model / effort / tag)"]) for row in ranked],
            [
                ("1", "claude / zeta / high / untagged"),
                ("2", "claude / alpha / high / untagged"),
                ("3", "claude / beta / high / untagged"),
            ],
        )
        self.assertIn("Insufficient data (fewer than 3 windows), unranked:", section)
        self.assertEqual(
            [row["Developer (tool / model / effort / tag)"] for row in insufficient],
            ["claude / aaa / high / untagged"],
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
        text = _render(("host/run-1.json", _file("run-1", [_row([_sub(OPUS)], reviews=reviews)])))
        (ranked,) = _tables(_section(text, "### drift-audit"))

        self.assertEqual(ranked[0]["Commissions, n"], "4")
        self.assertEqual(
            ranked[0]["Usable (PM), share rated/(rated+unavailable+failed)"],
            "1/3 (33%)",
        )


class TestLeaderboardRanking(unittest.TestCase):
    """Sort keys below the primary statistic, on one fixture.

    Every group here sorts against its group-key order, so a test passes only
    through the rule it names.
    """

    _ALPHA = {"tool": "claude", "model": "alpha", "effort": "high", "model_tag": None}
    _ZETA = {"tool": "claude", "model": "zeta", "effort": "high", "model_tag": None}

    def _text(self) -> str:
        drift_low = ("claude", "claude-opus-5-5", "high", False)
        drift_high = ("codex", "gpt-6.1-sol", "high", False)
        rows = []
        for n in range(3):
            rows.append(
                # Zeta is accepted on its first submission; alpha needs a
                # second. Both score 2 throughout, so PM mean ties.
                _row(
                    [_sub(self._ZETA, 2)],
                    slice_id=f"zeta {n}",
                    reviews=[
                        _rev("review-1", drift_low, {"score": 1}, "drift-audit"),
                        _rev("review-2", drift_high, {"score": 2}, "drift-audit"),
                        _rev("review-3", _QWEN, {"score": 1}),
                        _rev("review-4", _CODEX, {"score": 1}),
                        _rev("review-5", _CLAUDE, {"score": 2}),
                    ],
                    # Claude's code review is never in a panel.
                    comparisons=[{"origin": 0, "order": ["review-3", "review-4"], "close": []}],
                )
            )
            rows.append(_row([_sub(self._ALPHA, 2), _sub(self._ALPHA, 2)], slice_id=f"alpha {n}"))
        return _render(("h/run-1.json", _file("run-1", rows)))

    def test_developers_tied_on_pm_mean_rank_by_first_submission_acceptance(
        self,
    ) -> None:
        (ranked,) = _tables(_section(self._text(), "### All difficulties"))

        self.assertEqual(
            [(row["Rank"], row["Developer (tool / model / effort / tag)"]) for row in ranked],
            [
                ("1", "claude / zeta / high / untagged"),
                ("2", "claude / alpha / high / untagged"),
            ],
        )

    def test_drift_audit_reviewers_rank_by_pm_mean(self) -> None:
        (ranked,) = _tables(_section(self._text(), "### drift-audit"))

        self.assertEqual(
            [(row["Rank"], row["Reviewer (tool / model / effort / override / tag)"]) for row in ranked],
            [
                ("1", "codex / gpt-6.1-sol / high / no override / untagged"),
                ("2", "claude / claude-opus-5-5 / high / no override / untagged"),
            ],
        )

    def test_a_code_reviewer_with_no_panels_ranks_last(self) -> None:
        (ranked,) = _tables(_section(self._text(), "### code-review"))

        self.assertEqual(
            [
                (
                    row["Reviewer (tool / model / effort / override / tag)"],
                    row["Pairwise wins (PM), share"],
                )
                for row in ranked
            ],
            [
                ("qwen / qwen-4 / unknown / override / untagged", "3/3 (100%)"),
                ("codex / gpt-6.1-sol / high / no override / untagged", "0/3 (0%)"),
                ("claude / claude-opus-5-5 / high / no override / untagged", "n/a"),
            ],
        )


class TestLeaderboardMedians(unittest.TestCase):
    def _section(self) -> str:
        unparsed = {
            **_code((-2, 1, 3), 0, 0),
            "complexity": None,
            "complexity_reason": "x.py:1: bad",
        }
        rows = [
            _row([_sub(OPUS)], slice_id="Slice 1", code=_code((-4, 0, 1), 4, 7)),
            _row([_sub(OPUS)], slice_id="Slice 2", code=_code((-1, 2, 0), 1, 3)),
            _row([_sub(OPUS)], slice_id="Slice 3", code=unparsed),
            _row(
                [_sub(OPUS)],
                slice_id="Slice 4",
                code={**unparsed, "lines": _code((-7, 0, 0), 0, 0)["lines"]},
            ),
        ]
        return _section(_render(("h/run-1.json", _file("run-1", rows))), "### All difficulties")

    def test_line_medians_print_halves_and_negatives_exactly(self) -> None:
        (ranked,) = _tables(self._section())

        self.assertEqual(
            ranked[0]["Δ production lines code/comment/blank, median"],
            "-3/0.5/0.5 (n=4)",
        )

    def test_windows_without_complexity_are_left_out_of_its_median(self) -> None:
        (ranked,) = _tables(self._section())

        self.assertEqual(ranked[0]["Δ complexity sum, median"], "2.5 (n=2)")

    def test_a_median_beyond_float_precision_prints_exactly(self) -> None:
        rows = [_row([_sub(OPUS, developer_s=2**61 + offset)], slice_id=f"Slice {offset}") for offset in (1, 2)]
        section = _section(_render(("h/run-1.json", _file("run-1", rows))), "### All difficulties")
        (insufficient,) = _tables(section)

        self.assertEqual(
            insufficient[0]["Developer time per submission, median s"],
            "2305843009213693953.5 (n=2)",
        )


class TestLeaderboardTags(unittest.TestCase):
    def test_identities_differing_only_by_tag_are_separate_groups(self) -> None:
        hot = {**OPUS, "model_tag": "temp-0.8"}
        rows = [
            _row(
                [_sub(OPUS)],
                reviews=[
                    _rev("review-1", _CODEX, {"score": 2}),
                    _rev("review-2", _CODEX, {"score": 0}, model_tag="hot"),
                ],
            ),
            _row([_sub(hot, 0)], slice_id="Slice 2"),
        ]
        text = _render(("h/run-1.json", _file("run-1", rows)))

        (developers,) = _tables(_section(text, "### All difficulties"))
        self.assertEqual(
            {name: row["Score (PM), mean 0–2"] for name, row in _by_name(developers).items()},
            {
                _OPUS_LABEL: "2.00 (n=1)",
                "claude / claude-opus-5-5 / high / temp-0.8": "0.00 (n=1)",
            },
        )
        (reviewers,) = _tables(_section(text, "### code-review"))
        self.assertEqual(
            [
                (
                    row["Reviewer (tool / model / effort / override / tag)"],
                    row["Score (PM), mean 0–2"],
                )
                for row in reviewers
            ],
            [
                ("codex / gpt-6.1-sol / high / no override / hot", "0.00 (n=1)"),
                ("codex / gpt-6.1-sol / high / no override / untagged", "2.00 (n=1)"),
            ],
        )

    def test_same_identity_twice_in_a_panel_is_no_pair(self) -> None:
        rows = [
            _row(
                [_sub(OPUS)],
                slice_id=f"Slice {n}",
                reviews=[
                    _rev("review-1", _CODEX, {"score": 2}),
                    _rev("review-2", _CODEX, {"score": 2}),
                    _rev("review-3", _CLAUDE, {"score": 1}),
                ],
                comparisons=[
                    {
                        "origin": 0,
                        "order": ["review-1", "review-2", "review-3"],
                        "close": ["review-2"],
                    }
                ],
            )
            for n in range(3)
        ]
        (ranked,) = _tables(_section(_render(("h/run-1.json", _file("run-1", rows))), "### code-review"))
        shares = {
            row["Reviewer (tool / model / effort / override / tag)"]: (
                row["Panels (PM), n"],
                row["Pairwise wins (PM), share"],
                row["Close-call wins (PM), share"],
            )
            for row in ranked
        }

        self.assertEqual(
            shares,
            {
                "codex / gpt-6.1-sol / high / no override / untagged": (
                    "3",
                    "6/6 (100%)",
                    "n/a",
                ),
                "claude / claude-opus-5-5 / high / no override / untagged": (
                    "3",
                    "0/6 (0%)",
                    "n/a",
                ),
            },
        )


class TestLoadRunFiles(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / "mac").mkdir()
        (self.root / "mac" / "run-1.json").write_bytes(b"{}")

    def test_a_dangling_symlink_is_listed_with_no_bytes(self) -> None:
        (self.root / "mac" / "gone.json").symlink_to(self.root / "absent.json")

        self.assertEqual(
            leaderboard.load_run_files(self.root),
            [("mac/gone.json", None), ("mac/run-1.json", b"{}")],
        )

    def test_only_files_directly_in_a_host_folder_are_read(self) -> None:
        (self.root / "top.json").write_bytes(b"{}")
        (self.root / "mac" / "deeper").mkdir()
        (self.root / "mac" / "deeper" / "run-2.json").write_bytes(b"{}")

        self.assertEqual(leaderboard.load_run_files(self.root), [("mac/run-1.json", b"{}")])


class TestLeaderboardFiles(unittest.TestCase):
    def _rows(self, decided_at: str, score: int = 2) -> list[dict]:
        return [_row([_sub(OPUS, score)], decided_at=decided_at)]

    def test_output_ignores_repetition_host_copies_and_file_order(self) -> None:
        one = ("mac/run-1.json", _file("run-1", self._rows(ledger_ts(10))))
        two = ("mac/run-2.json", _file("run-2", self._rows(ledger_ts(20), 1)))
        first = _render(one, two)

        self.assertEqual(_render(one, two), first)
        self.assertEqual(_render(two, one), first)
        copied = [("studio/run-1.json", one[1]), ("studio/run-2.json", two[1])]
        self.assertEqual(_render(two, *copied, one), first)

    def test_more_events_win_and_equal_events_with_different_bytes_are_errors(
        self,
    ) -> None:
        files = [
            ("a/run-1.json", _file("run-1", self._rows(ledger_ts(500)), events=5)),
            ("b/run-1.json", _file("run-1", self._rows(ledger_ts(100)), events=7)),
            ("a/run-2.json", _file("run-2", self._rows(ledger_ts(10)), events=3)),
            ("b/run-2.json", _file("run-2", self._rows(ledger_ts(20)), events=3)),
            ("b/broken.json", b"{not json"),
            ("b/partial.json", b'{"run_id": "run-3", "events": 1}'),
        ]
        text, errors = leaderboard.render_leaderboard(files)

        self.assertIn("Runs: 1; slice windows: 1.", text)
        # The 5-event copy of run-1 (decided later) lost to the 7-event copy.
        self.assertIn(f"As of: {ledger_ts(100)}", text)
        self.assertEqual(
            [error.split(":", 1)[0] for error in errors],
            ["a/run-2.json", "b/broken.json", "b/partial.json", "b/run-2.json"],
        )
        self.assertIn("different contents", errors[0])
        self.assertIn("not valid JSON", errors[1])
        self.assertIn("missing or invalid run_id, run_tag, events or rows", errors[2])
        for error in errors:
            self.assertIn(f"- {error}", _section(text, "## Errors"))

    def test_as_of_is_the_latest_decision_or_unknown(self) -> None:
        files = [
            ("h/run-1.json", _file("run-1", self._rows(ledger_ts(30)))),
            (
                "h/run-2.json",
                _file(
                    "run-2",
                    [*self._rows(ledger_ts(90)), _row([_sub(OPUS)], outcome="open")],
                ),
            ),
        ]
        self.assertIn(f"As of: {ledger_ts(90)}\n", _render(*files))
        undecided = _file("run-3", [_row([_sub(OPUS)], outcome="open")])
        self.assertIn("As of: unknown\n", _render(("h/run-3.json", undecided)))

    def test_history_link_is_relative_to_the_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            history = Path(scratch) / "skill" / "ledger" / "historical-pre-ledger.md"
            history.parent.mkdir(parents=True)
            history.write_text("frozen\n", encoding="utf-8")
            files = [("h/run-1.json", _file("run-1", self._rows(ledger_ts(1))))]
            with mock.patch.object(leaderboard, "_HISTORY_PATH", history):
                text, _ = leaderboard.render_leaderboard(files, out_dir=Path(scratch) / "out")
            with mock.patch.object(leaderboard, "_HISTORY_PATH", history.with_name("absent.md")):
                plain, _ = leaderboard.render_leaderboard(files, out_dir=Path(scratch) / "out")

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
            files = [("h/run-1.json", _file("run-1", self._rows(ledger_ts(1))))]
            with mock.patch.object(leaderboard, "_HISTORY_PATH", history):
                text, _ = leaderboard.render_leaderboard(files, out_dir=Path(scratch) / "out")

        self.assertIn("(../my%20hist%231%20%28old%29/historical-pre-ledger.md)", text)

    def test_malformed_row_interiors_are_errors_not_crashes(self) -> None:
        good = ("h/good.json", _file("good", self._rows(ledger_ts(1))))

        def with_row(**fields) -> bytes:
            return _file("bad", [{**_row([_sub(OPUS)]), **fields}])

        reviewer = _rev("review-1", _CODEX, {"score": 2})
        cases = {
            "submissions": with_row(submissions=5),
            "difficulty": with_row(difficulty=["x"]),
            # A band or key that differs from a real one only in type would
            # collide with it on label and sort.
            "difficulty type": with_row(difficulty=1),
            "model_tag type": with_row(submissions=[_sub({**OPUS, "model_tag": 2})]),
            "command_override type": with_row(reviews=[{**reviewer, "command_override": 1}]),
            "run_tag type": _file("bad", [_row([_sub(OPUS)])], run_tag=7),
            "review_id": with_row(reviews=[{**reviewer, "review_id": ["a"]}]),
            "tool": with_row(reviews=[{**reviewer, "tool": {"x": 1}}]),
            "developer": with_row(submissions=[{**_sub(OPUS), "developer": {"tool": ["x"]}}]),
            "order": with_row(comparisons=[{"origin": 0, "order": [["a"]], "close": []}]),
            "events": b'{"run_id": "bad", "events": ' + b"9" * 5000 + b', "rows": []}',
        }
        for name, raw in cases.items():
            with self.subTest(name):
                text, errors = leaderboard.render_leaderboard([good, ("h/bad.json", raw)])

                self.assertIn("Runs: 1; slice windows: 1.", text)
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0].startswith("h/bad.json: "), errors[0])
                self.assertIn(f"- {errors[0]}", _section(text, "## Errors"))

    def test_a_pathologically_nested_file_is_an_error_not_a_crash(self) -> None:
        good = ("h/good.json", _file("good", self._rows(ledger_ts(1))))

        text, errors = leaderboard.render_leaderboard([good, ("h/deep.json", b"[" * 200000)])

        self.assertIn("Runs: 1; slice windows: 1.", text)
        self.assertEqual(len(errors), 1)
        self.assertTrue(
            errors[0].startswith("h/deep.json: not valid JSON (RecursionError"),
            errors[0],
        )

    def test_means_round_half_up_from_the_exact_value(self) -> None:
        # Scores 1×7 and 2 → 9/8 = 1.125 exactly; binary float formatting gives 1.12.
        scores = [1] * 7 + [2]
        rows = [_row([_sub(OPUS, score)], slice_id=f"Slice {n}") for n, score in enumerate(scores)]
        section = _section(_render(("h/run-1.json", _file("run-1", rows))), "### All difficulties")
        (ranked,) = _tables(section)

        self.assertEqual(ranked[0]["Score (PM), mean 0–2"], "1.13 (n=8)")


class TestLedgerRenderCommand(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        # Resolved, so the CLI's resolved output directory and these fixture
        # paths agree even where the temp root is a symlink (macOS /var).
        self.root = Path(scratch.name).resolve() / "ledger"
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
        (host / "run-1.json").write_bytes(_file("run-1", [_row([_sub(OPUS)], decided_at=ledger_ts(5))]))
        out = self.root.parent / "elsewhere" / "board.md"

        code, stdout, err = self._main(["ledger", "render", "--out", str(out)])

        self.assertEqual(code, 0, err)
        self.assertIn(str(out), stdout)
        self.assertTrue(out.read_text(encoding="utf-8").startswith("# PM model leaderboard\n"))
        self.assertFalse((self.root / "leaderboard.md").exists())

    def test_no_files_exits_two_naming_the_directory(self) -> None:
        code, _stdout, err = self._main(["ledger", "render"])

        self.assertEqual(code, 2)
        self.assertIn(f"no per-run ledger files found under {self.root}", err)
        self.assertNotIn("pm: ledger: not written", err)

    def _seed(self) -> None:
        host = self.root / "mac"
        host.mkdir(parents=True)
        (host / "run-1.json").write_bytes(_file("run-1", [_row([_sub(OPUS)], decided_at=ledger_ts(5))]))

    def test_run_tags_keep_only_runs_carrying_one_of_them(self) -> None:
        self._seed()
        for run_id, tag in (("run-2", "alpha"), ("run-3", "beta"), ("run-4", "gamma")):
            (self.root / "mac" / f"{run_id}.json").write_bytes(
                _file(run_id, [_row([_sub(OPUS)], decided_at=ledger_ts(5))], run_tag=tag)
            )

        code, _stdout, err = self._main(["ledger", "render", "--run-tag", "beta", "--run-tag", "alpha"])

        self.assertEqual(code, 0, err)
        text = (self.root / "leaderboard.md").read_text(encoding="utf-8")
        self.assertIn(f"As of: {ledger_ts(5)}\n\nRun tags: alpha, beta\n", text)
        self.assertIn("Runs: 2; slice windows: 2.", text)

    def test_run_tags_matching_no_run_exit_two_naming_them_and_the_directory(
        self,
    ) -> None:
        self._seed()

        code, _stdout, err = self._main(["ledger", "render", "--run-tag", "nowhere"])

        self.assertEqual(code, 2)
        self.assertIn(f"no per-run ledger file has run tag nowhere under {self.root}", err)
        self.assertFalse((self.root / "leaderboard.md").exists())

    def test_the_default_output_links_the_skills_history_not_the_override(self) -> None:
        self._seed()
        history = self.root.parent / "skill" / "ledger" / "historical-pre-ledger.md"
        history.parent.mkdir(parents=True)
        history.write_text("frozen\n", encoding="utf-8")

        with mock.patch.object(leaderboard, "_HISTORY_PATH", history):
            code, _stdout, err = self._main(["ledger", "render"])

        self.assertEqual(code, 0, err)
        self.assertIn(
            "History: [historical-pre-ledger.md](../skill/ledger/historical-pre-ledger.md)",
            (self.root / "leaderboard.md").read_text(encoding="utf-8"),
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

        with mock.patch.object(leaderboard, "_HISTORY_PATH", history):
            code, _stdout, err = self._main(["ledger", "render", "--out", str(out)])

        self.assertEqual(code, 0, err)
        self.assertIn("(skill/ledger/historical-pre-ledger.md)", out.read_text(encoding="utf-8"))


class TestLedgerTagCommand(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve() / "ledger"
        patcher = mock.patch.dict(os.environ, {"PM_LEDGER_DIR": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _seed(self) -> dict[str, bytes]:
        """Two host copies of run-1 (an untagged Developer, a `cold` review) and an unrelated run-2."""
        run_1 = _file(
            "run-1",
            [_row([_sub(OPUS)], decided_at=ledger_ts(5), reviews=[_rev("r1", _CODEX, {"score": 2}, model_tag="cold")])],
        )
        run_2 = _file("run-2", [_row([_sub(SONNET)], decided_at=ledger_ts(6))])
        files = {"mac/run-1.json": run_1, "studio/run-1.json": run_1, "mac/run-2.json": run_2}
        for name, data in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return files

    def test_retag_rewrites_every_copy_identically_and_the_copies_still_collapse(self) -> None:
        files = self._seed()

        code, stdout, err = self._main(
            ["ledger", "tag", "run-1", "--run-tag", "bench", "--model-tag", "untagged=hot", "--model-tag", "cold=warm"]
        )

        self.assertEqual(code, 0, err)
        self.assertEqual(
            stdout.splitlines(),
            [f"retagged: {self.root / 'mac' / 'run-1.json'}", f"retagged: {self.root / 'studio' / 'run-1.json'}"],
        )
        mac = (self.root / "mac" / "run-1.json").read_bytes()
        self.assertEqual(mac, (self.root / "studio" / "run-1.json").read_bytes())
        self.assertNotEqual(mac, files["mac/run-1.json"])
        payload = json.loads(mac)
        self.assertEqual(payload["run_tag"], "bench")
        row = payload["rows"][0]
        self.assertEqual(row["submissions"][0]["developer"]["model_tag"], "hot")
        self.assertEqual(row["reviews"][0]["model_tag"], "warm")
        self.assertEqual((self.root / "mac" / "run-2.json").read_bytes(), files["mac/run-2.json"])

        code, _stdout, err = self._main(["ledger", "render", "--run-tag", "bench"])

        self.assertEqual(code, 0, err)
        text = (self.root / "leaderboard.md").read_text(encoding="utf-8")
        self.assertIn("Runs: 1; slice windows: 1.", text)
        self.assertIn("None.", _section(text, "## Errors"))
        self.assertIn(" / hot |", text)
        self.assertIn(" / warm |", text)

    def test_retag_refusals_exit_two_and_rewrite_nothing(self) -> None:
        files = self._seed()
        cases = {
            "unknown run id": (
                ["nowhere", "--run-tag", "x"],
                f"no per-run ledger file for run nowhere under {self.root}",
            ),
            "OLD matches nothing": (["run-1", "--model-tag", "typo=x"], "--model-tag typo"),
            "malformed form": (["run-1", "--model-tag", "cold"], "OLD=NEW"),
            "reserved NEW": (["run-1", "--model-tag", "cold=untagged"], "cannot be 'untagged'"),
            "tag containing the separator": (["run-1", "--run-tag", "a=b"], "cannot contain '='"),
            "no tag given": (["run-1"], "needs --run-tag"),
        }
        for name, (argv, expected) in cases.items():
            with self.subTest(name):
                code, stdout, err = self._main(["ledger", "tag", *argv])

                self.assertEqual(code, 2)
                self.assertIn(expected, err)
                self.assertEqual(stdout, "")
                for relative, data in files.items():
                    self.assertEqual((self.root / relative).read_bytes(), data)

    def test_retag_refuses_while_a_host_folder_cannot_be_listed(self) -> None:
        """A copy in an unlistable folder would later conflict with the retagged ones, so nothing is written."""
        files = self._seed()
        listed = leaderboard.load_run_files(self.root)
        with mock.patch.object(leaderboard, "load_run_files", return_value=[("locked/", None), *listed]):
            code, _stdout, err = self._main(["ledger", "tag", "run-1", "--run-tag", "bench"])

        self.assertEqual(code, 2)
        self.assertIn(f"host folder {self.root / 'locked/'} cannot be listed", err)
        for relative, data in files.items():
            self.assertEqual((self.root / relative).read_bytes(), data)

    def test_a_write_failure_restores_the_copies_already_rewritten(self) -> None:
        files = self._seed()
        real_write = state_mod._atomic_write_bytes

        def fail_second(path: Path, data: bytes) -> None:
            if path.parent.name == "studio":
                raise OSError("disk full")
            real_write(path, data)

        with mock.patch.object(state_mod, "_atomic_write_bytes", side_effect=fail_second):
            code, _stdout, err = self._main(["ledger", "tag", "run-1", "--run-tag", "bench"])

        self.assertEqual(code, 2)
        self.assertIn(f"cannot rewrite {self.root / 'studio' / 'run-1.json'}: disk full", err)
        self.assertIn("were restored, so nothing changed", err)
        self.assertNotIn("Traceback", err)
        # Both copies hold their original bytes, so they still collapse at render.
        for relative, data in files.items():
            self.assertEqual((self.root / relative).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()

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

_DEVELOPER = "Developer (tool / model / effort)"
_REVIEWER = "Reviewer (tool / model / effort)"
_CODEX_LABEL = "codex / gpt-6.1-sol / high"
_CLAUDE_LABEL = "claude / claude-opus-5-5 / high"
_QWEN_LABEL = "qwen / qwen-4 / unknown / override"
_OPUS_LABEL = "claude / claude-opus-5-5 / high"
_SONNET_LABEL = "claude / claude-sonnet-5-5 / medium"


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


def _failed(key: tuple, skill: str = "code-review", reason: str = "timeout") -> dict:
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
        "reason": reason,
    }


def _code(production: int, before: int, after: int, test: int = 0) -> dict:
    """A code-health block: `production` and `test` code-line deltas, a complexity sum before and after."""
    categories = ("production", "test", "documentation", "configuration", "data", "other")
    lines = {category: {"code": 0, "comment": 1, "blank": 1} for category in categories}
    lines["production"]["code"] = production
    lines["test"]["code"] = test
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
    relaunches: int = 0,
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
        "relaunches": relaunches,
        "nudges": nudges,
        "code": code,
        "submissions": submissions,
        "reviews": reviews or [],
        "comparisons": comparisons or [],
    }


def _order(*review_ids: str) -> dict:
    return {"origin": 0, "order": list(review_ids)}


def _file(run_id: str, rows: list[dict], events: int = 10, **extra) -> bytes:
    """A per-run file; `extra` adds top-level keys (a `run_tag`, say), absent by default as in older files."""
    payload = {"run_id": run_id, "events": events, "rows": rows, **extra}
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _render(*files: tuple[str, bytes]) -> str:
    text, _errors = leaderboard.render_leaderboard(list(files))
    return text


def _render_rows(rows: list[dict]) -> str:
    return _render(("h/run-1.json", _file("run-1", rows)))


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


def _developers(text: str) -> list[dict[str, str]]:
    """The one Developer table."""
    (table,) = _tables(_section(text, "## Developers"))
    return table


def _by_name(table: list[dict[str, str]], column: str = _DEVELOPER) -> dict[str, dict[str, str]]:
    """Index a table by its identity column."""
    return {row[column]: row for row in table}


def _identity(model: str) -> dict:
    return {"tool": "claude", "model": model, "effort": "high", "model_tag": None}


class TestLeaderboardColumns(unittest.TestCase):
    """Every column on a fixture whose values are worked out by hand."""

    def _text(self) -> str:
        rows = [
            # Two scored submissions: only the first is "first", so its 1, not
            # the later 2, is this window's First score.
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
                relaunches=1,
                nudges=1,
                code=_code(10, 4, 7),
                reviews=[
                    _rev("review-1", _CODEX, {"score": 2, "confirmed": 3}),
                    _rev("review-2", _CLAUDE, {"score": 1}),
                    _rev("review-3", _QWEN, {"score": 1}),
                ],
                comparisons=[_order("review-1", "review-2", "review-3")],
            ),
            _row(
                [_sub(OPUS, 2, developer_s=300)],
                slice_id="Slice 2",
                elapsed_s=300,
                code=_code(20, 0, 1),
                reviews=[
                    _rev("review-1", _CODEX, {"score": 1, "confirmed": 0}),
                    _rev("review-2", _CLAUDE, {"score": 2}),
                ],
                comparisons=[_order("review-2", "review-1")],
            ),
            # Exhausted: counted, but not accepted. The unavailable first
            # submission is skipped, so the steer is the first scored one.
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
        return _render_rows(rows)

    def test_developer_columns_match_hand_computed_values(self) -> None:
        (row,) = _developers(self._text())
        self.assertEqual(
            row,
            {
                "Difficulty": "moderate",
                "Rank": "1",
                _DEVELOPER: _OPUS_LABEL,
                # The exhausted window counts against acceptance.
                "Accepted": "3/4 (75%)",
                "First-pass accept": "2/4 (50%)",
                # (1 + 2 + 0 + 2) / 4; the mean over every scored submission would be 1.40.
                "First score (PM)": "1.25",
                "Criteria met, first (PM)": "9/12 (75%)",
                "Defects, first: P0–1 / P2–3 (PM)": "0.75 / 1.00",
                # (3 + 0 + 3 + 0) / 4: the first window's relaunch counts.
                "Interventions / window": "1.50",
                # Median of 50, 100, 200, 300 s is 150 s.
                "Dev time / submission": "2.5",
                "Wall / window": "10.0",
                "Δ code lines, prod / test": "+15 / 0",
                "Δ complexity (Python)": "+2",
            },
        )

    def test_footnote_is_left_out_when_every_window_is_credited(self) -> None:
        self.assertNotIn("Window-level columns leave out", _section(self._text(), "## Developers"))

    def test_reviewer_columns_match_hand_computed_values(self) -> None:
        section = _section(self._text(), "### code-review")
        reviewers = _tables(section)[0]

        # Codex and Claude tie on pairwise share, so group-key order decides;
        # Qwen has two commissions, so it follows unranked.
        self.assertEqual(
            [(row["Rank"], row[_REVIEWER]) for row in reviewers],
            [("1", _CLAUDE_LABEL), ("2", _CODEX_LABEL), ("–", _QWEN_LABEL)],
        )
        claude, codex, qwen = reviewers
        self.assertEqual(
            claude,
            {
                "Rank": "1",
                _REVIEWER: _CLAUDE_LABEL,
                "Commissions": "3",
                "Usable (PM)": "2/2 (100%)",
                "Failed by class": "0",
                "Score (PM)": "1.50",
                "Confirmed defects / review (PM)": "n/a",
                "Pairwise wins (PM)": "2/3 (67%)",
                "Review time": "n/a",
            },
        )
        self.assertEqual(codex["Score (PM)"], "1.67")
        # Two of Codex's three rated reviews carry `confirmed`: (3 + 0) / 2.
        self.assertEqual(codex["Confirmed defects / review (PM)"], "1.50")
        self.assertEqual(codex["Pairwise wins (PM)"], "2/3 (67%)")
        self.assertEqual(
            [qwen[h] for h in ("Commissions", "Usable (PM)", "Failed by class", "Pairwise wins (PM)")],
            ["2", "1/2 (50%)", "timeout 1", "0/2 (0%)"],
        )

    def test_reviewer_review_time_is_the_median_of_known_durations_and_n_a_without_any(self) -> None:
        timed = [{**_rev(f"review-{n}", _CODEX, {"score": 1}), "elapsed_s": secs} for n, secs in ((1, 60), (2, 120))]
        timed.append({**_rev("review-3", _CODEX, {"score": 1}), "elapsed_s": None})
        untimed = [_rev(f"review-{n}", _CLAUDE, {"score": 1}) for n in (1, 2, 3)]
        section = _section(_render_rows([_row([_sub(OPUS)], reviews=[*timed, *untimed])]), "### code-review")

        (table,) = _tables(section)
        by_reviewer = _by_name(table, _REVIEWER)
        self.assertEqual(by_reviewer[_CODEX_LABEL]["Review time"], "1.5")
        self.assertEqual(by_reviewer[_CLAUDE_LABEL]["Review time"], "n/a")
        # No two reviewers shared a panel, so there is no head-to-head table.
        self.assertNotIn("Head-to-head", section)

    def test_presentation_carries_no_wall_clock_or_composite_score(self) -> None:
        text = self._text()
        self.assertIn("## Errors\n\nNone.\n", text)
        self.assertNotIn("composite", text.replace("There is no composite score.", ""))
        self.assertNotIn("(n=", text)
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
        text = _render_rows(rows)

        self.assertEqual(_section(text, "## Developers").strip().splitlines()[-1], "No Developer-counted windows.")
        (ranked,) = _tables(_section(text, "### drift-audit"))
        self.assertEqual(ranked[0]["Commissions"], "3")
        self.assertEqual(ranked[0]["Score (PM)"], "1.00")

    def test_mixed_and_unscored_windows_are_footnoted_but_submissions_still_count(self) -> None:
        rows = [
            _row(
                [
                    _sub(OPUS, 1, criteria_met=1, developer_s=10),
                    _sub(SONNET, 2, developer_s=None),
                ]
            ),
            _row([_sub(OPUS, None)], slice_id="Slice 2"),
        ]
        text = _render_rows(rows)

        self.assertIn(
            "leave out 1 window(s) with mixed Developer identities and 1 window(s) with no scored submission",
            _section(text, "## Developers"),
        )
        opus, sonnet = _by_name(_developers(text))[_OPUS_LABEL], _by_name(_developers(text))[_SONNET_LABEL]
        # The first scored submission is Opus's, so its score is Opus's alone.
        self.assertEqual(
            [opus[h] for h in ("First score (PM)", "Criteria met, first (PM)", "Dev time / submission")],
            ["1.00", "1/3 (33%)", "0.2"],
        )
        self.assertEqual(
            [sonnet[h] for h in ("First score (PM)", "Criteria met, first (PM)", "Dev time / submission")],
            ["n/a", "n/a", "n/a"],
        )
        for group in (opus, sonnet):
            self.assertEqual([group["Rank"], group["Accepted"], group["Wall / window"]], ["–", "n/a", "n/a"])

    def test_usable_share_leaves_unrated_reviews_out_of_both_sides(self) -> None:
        drift = ("codex", "gpt-6.1-sol", "high", False)
        reviews = [
            _rev("review-1", drift, {"score": 2}, "drift-audit"),
            _rev("review-2", drift, {"status": "unavailable"}, "drift-audit"),
            _rev("review-3", drift, None, "drift-audit"),
            _failed(drift, "drift-audit"),
        ]
        (ranked,) = _tables(_section(_render_rows([_row([_sub(OPUS)], reviews=reviews)]), "### drift-audit"))

        self.assertEqual(ranked[0]["Commissions"], "4")
        self.assertEqual(ranked[0]["Usable (PM)"], "1/3 (33%)")

    def test_failed_commissions_are_counted_by_class_in_a_fixed_order(self) -> None:
        """Classes print in the review module's order, whatever order they
        failed in; a zero class is left out; an older run's `empty report` is
        the `empty` class; any other reason outside the classes (an
        unrecognised exit) is unclassified."""
        drift = ("codex", "gpt-6.1-sol", "high", False)
        reasons = ("capacity", "exit 3", "limit", "empty report", "limit", "capacity", "empty", "limit")
        reviews = [_failed(drift, "drift-audit", reason) for reason in reasons]
        (ranked,) = _tables(_section(_render_rows([_row([_sub(OPUS)], reviews=reviews)]), "### drift-audit"))

        self.assertEqual(ranked[0]["Failed by class"], "limit 3 · capacity 2 · empty 2 · unclassified 1")
        self.assertEqual(ranked[0]["Usable (PM)"], "0/8 (0%)")


class TestLeaderboardRanking(unittest.TestCase):
    """Sort keys below the primary statistic, on one fixture.

    Every group here sorts against its group-key order, so a test passes only
    through the rule it names.
    """

    _ALPHA = _identity("alpha")
    _ZETA = _identity("zeta")

    def _text(self) -> str:
        drift_low = ("claude", "claude-opus-5-5", "high", False)
        drift_high = ("codex", "gpt-6.1-sol", "high", False)
        rows = []
        for n in range(3):
            rows.append(
                # Zeta is accepted on its first submission; alpha needs a
                # second. Both score 2 throughout, so First score ties.
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
                    comparisons=[_order("review-3", "review-4")],
                )
            )
            rows.append(_row([_sub(self._ALPHA, 2), _sub(self._ALPHA, 2)], slice_id=f"alpha {n}"))
        return _render_rows(rows)

    def test_developers_tied_on_first_score_rank_by_first_pass_acceptance(self) -> None:
        self.assertEqual(
            [(row["Rank"], row[_DEVELOPER]) for row in _developers(self._text())],
            [("1", "claude / zeta / high"), ("2", "claude / alpha / high")],
        )

    def test_developers_rank_by_the_first_submission_score_not_later_ones(self) -> None:
        # Late's later submissions lift its mean over every submission (1.33)
        # above early's 1.00, but its first submissions all scored 0.
        rows = [
            *(_row([_sub(_identity("early"), 1)], slice_id=f"early {n}") for n in range(3)),
            *(_row([_sub(_identity("late"), s) for s in (0, 2, 2)], slice_id=f"late {n}") for n in range(3)),
        ]

        self.assertEqual(
            [(row["Rank"], row[_DEVELOPER], row["First score (PM)"]) for row in _developers(_render_rows(rows))],
            [("1", "claude / early / high", "1.00"), ("2", "claude / late / high", "0.00")],
        )

    def test_drift_audit_reviewers_rank_by_pm_mean(self) -> None:
        (ranked,) = _tables(_section(self._text(), "### drift-audit"))

        self.assertEqual(
            [(row["Rank"], row[_REVIEWER]) for row in ranked],
            [("1", _CODEX_LABEL), ("2", _CLAUDE_LABEL)],
        )
        self.assertNotIn("Pairwise wins (PM)", ranked[0])

    def test_a_code_reviewer_with_no_panels_ranks_last(self) -> None:
        ranked = _tables(_section(self._text(), "### code-review"))[0]

        self.assertEqual(
            [(row["Rank"], row[_REVIEWER], row["Pairwise wins (PM)"]) for row in ranked],
            [
                ("1", _QWEN_LABEL, "3/3 (100%)"),
                ("2", _CODEX_LABEL, "0/3 (0%)"),
                ("3", _CLAUDE_LABEL, "n/a"),
            ],
        )


class TestLeaderboardDifficultyBands(unittest.TestCase):
    def test_bands_run_hard_to_easy_then_others_with_ranks_restarting_and_small_groups_inline(self) -> None:
        def windows(model: str, score: int, count: int, difficulty: str | None) -> list[dict]:
            return [
                _row([_sub(_identity(model), score)], slice_id=f"{model} {difficulty} {n}", difficulty=difficulty)
                for n in range(count)
            ]

        rows = [
            *windows("alpha", 2, 3, "easy"),
            *windows("alpha", 2, 1, None),
            *windows("alpha", 2, 1, "epic"),
            # In hard: alpha and beta tie on every statistic, so group-key
            # order decides; aaa scores best but has too few windows to rank.
            *windows("beta", 1, 3, "hard"),
            *windows("alpha", 1, 3, "hard"),
            *windows("zeta", 2, 3, "hard"),
            *windows("aaa", 2, 2, "hard"),
        ]

        self.assertEqual(
            [(row["Difficulty"], row["Rank"], row[_DEVELOPER]) for row in _developers(_render_rows(rows))],
            [
                ("hard", "1", "claude / zeta / high"),
                ("", "2", "claude / alpha / high"),
                ("", "3", "claude / beta / high"),
                ("", "–", "claude / aaa / high"),
                ("easy", "1", "claude / alpha / high"),
                ("epic", "–", "claude / alpha / high"),
                ("unknown", "–", "claude / alpha / high"),
            ],
        )


class TestLeaderboardMedians(unittest.TestCase):
    def _row(self) -> dict[str, str]:
        unparsed = {**_code(-1, 0, 0, test=0), "complexity": None, "complexity_reason": "x.py:1: bad"}
        rows = [
            _row([_sub(OPUS)], slice_id="Slice 1", code=_code(-4, 4, 7, test=1)),
            _row([_sub(OPUS)], slice_id="Slice 2", code=_code(-1, 1, 3, test=2)),
            _row([_sub(OPUS)], slice_id="Slice 3", code=unparsed),
            _row([_sub(OPUS)], slice_id="Slice 4", code={**unparsed, "lines": _code(-7, 0, 0)["lines"]}),
        ]
        (row,) = _developers(_render_rows(rows))
        return row

    def test_line_medians_print_signed_halves_exactly(self) -> None:
        # Production -7, -4, -1, -1 and test 0, 0, 1, 2.
        self.assertEqual(self._row()["Δ code lines, prod / test"], "-2.5 / +0.5")

    def test_windows_without_complexity_are_left_out_of_its_median(self) -> None:
        self.assertEqual(self._row()["Δ complexity (Python)"], "+2.5")

    def test_minutes_round_half_up_from_the_exact_median(self) -> None:
        # 855 s is 14.25 min exactly; half-even rounding would print 14.2.
        rows = [_row([_sub(OPUS, developer_s=850), _sub(OPUS, developer_s=860)], elapsed_s=30)]
        (row,) = _developers(_render_rows(rows))

        self.assertEqual([row["Dev time / submission"], row["Wall / window"]], ["14.3", "0.5"])

    def test_a_median_beyond_float_precision_prints_exactly(self) -> None:
        rows = [_row([_sub(OPUS, developer_s=2**61 + offset)], slice_id=f"Slice {offset}") for offset in (1, 2)]
        (row,) = _developers(_render_rows(rows))

        # (2**61 + 1.5) / 60 = 38430716820228232.5583…; a float has ~16 digits.
        self.assertEqual(row["Dev time / submission"], "38430716820228232.6")


class TestLeaderboardTags(unittest.TestCase):
    def test_label_suffixes_appear_only_when_set_and_tags_split_groups(self) -> None:
        hot = {**OPUS, "model_tag": "temp-0.8"}
        rows = [
            _row(
                [_sub(OPUS)],
                reviews=[
                    _rev("review-1", _CODEX, {"score": 2}),
                    _rev("review-2", _CODEX, {"score": 0}, model_tag="hot"),
                    _rev("review-3", ("codex", "gpt-6.1-sol", "high", None), {"score": 1}),
                    _rev("review-4", _QWEN, {"score": 1}, model_tag="hot"),
                ],
            ),
            _row([_sub(hot, 0)], slice_id="Slice 2"),
        ]
        text = _render_rows(rows)

        self.assertEqual(
            {name: row["First score (PM)"] for name, row in _by_name(_developers(text)).items()},
            {_OPUS_LABEL: "2.00", "claude / claude-opus-5-5 / high / temp-0.8": "0.00"},
        )
        (reviewers,) = _tables(_section(text, "### code-review"))
        self.assertEqual(
            [(row[_REVIEWER], row["Score (PM)"]) for row in reviewers],
            [
                ("codex / gpt-6.1-sol / high / hot", "0.00"),
                (_CODEX_LABEL, "2.00"),
                ("codex / gpt-6.1-sol / high / override unknown", "1.00"),
                ("qwen / qwen-4 / unknown / override / hot", "1.00"),
            ],
        )


class TestLeaderboardModelTagFilter(unittest.TestCase):
    """`model_tags` shows only the rows whose identity carries one; every figure stays the unfiltered board's."""

    _MAP = {"frontier": ["claude-*"], "local": ["qwen-*", "adacs/*"]}
    # Mapped `local`; recorded `evidence` and mapped `frontier`; no model, so no tag at all.
    _LOCAL = {"tool": "opencode", "model": "adacs/coder", "effort": None, "model_tag": None}
    _EVIDENCE = {**SONNET, "model_tag": "evidence"}
    _BARE = {"tool": "codex", "model": None, "effort": None, "model_tag": None}
    _LOCAL_LABEL = "opencode / adacs/coder / unknown"
    _EVIDENCE_LABEL = f"{_SONNET_LABEL} / evidence"

    def _text(self, *model_tags: str, run_tags: frozenset[str] = frozenset()) -> str:
        rows = []
        for n in range(3):
            # Codex (untagged) over Claude (`frontier`) over Qwen (`local`) in every panel.
            rows.append(
                _row(
                    [_sub(OPUS, 2)],
                    slice_id=f"opus {n}",
                    reviews=[
                        _rev("r1", _CODEX, {"score": 2}),
                        _rev("r2", _CLAUDE, {"score": 1}),
                        _rev("r3", _QWEN, {"score": 0}),
                    ],
                    comparisons=[_order("r1", "r2", "r3")],
                )
            )
            rows.append(_row([_sub(self._LOCAL, 1)], slice_id=f"local {n}"))
        rows.append(
            _row(
                [_sub(self._EVIDENCE, 0)],
                slice_id="evidence",
                reviews=[_rev("d1", _CODEX, {"score": 2}, "drift-audit", model_tag="hot")],
            )
        )
        rows.append(_row([_sub(self._BARE, 1)], slice_id="bare"))
        text, _errors = leaderboard.render_leaderboard(
            [("h/run-1.json", _file("run-1", rows, run_tag="bench"))],
            run_tags=run_tags,
            model_tags=frozenset(model_tags),
            tag_map=self._MAP,
        )
        return text

    def test_a_mapped_tag_shows_its_rows_renumbered_with_unfiltered_statistics(self) -> None:
        full, local = self._text(), self._text("local")
        full_developer = _by_name(_developers(full))[self._LOCAL_LABEL]
        full_reviewer = _by_name(_tables(_section(full, "### code-review"))[0], _REVIEWER)[_QWEN_LABEL]

        (developer,) = _developers(local)
        self.assertEqual([full_developer["Rank"], developer["Rank"]], ["2", "1"])
        # The band label moves to the band's first shown row; every figure stays.
        self.assertEqual({**developer, "Rank": "2", "Difficulty": ""}, full_developer)
        (reviewer,) = _tables(_section(local, "### code-review"))[0]
        self.assertEqual([full_reviewer["Rank"], reviewer["Rank"]], ["3", "1"])
        self.assertEqual(reviewer["Pairwise wins (PM)"], "0/6 (0%)")
        self.assertEqual({**reviewer, "Rank": "3"}, full_reviewer)
        # No drift-audit reviewer carries `local`, so that skill is left out.
        self.assertNotIn("### drift-audit", local)

    def test_a_recorded_tag_selects_its_row_and_no_reviewer_carries_it(self) -> None:
        text = self._text("evidence")

        self.assertEqual([row[_DEVELOPER] for row in _developers(text)], [self._EVIDENCE_LABEL])
        self.assertEqual(_section(text, "## Reviewers").strip(), "No Reviewer rows carry these model tags.")

    def test_several_tags_are_a_union_and_the_head_to_head_keeps_shown_ranked_reviewers(self) -> None:
        text = self._text("frontier", "local")

        self.assertEqual(
            [(row["Rank"], row[_DEVELOPER]) for row in _developers(text)],
            [("1", _OPUS_LABEL), ("2", self._LOCAL_LABEL), ("–", self._EVIDENCE_LABEL)],
        )
        reviewers, head_to_head = _tables(_section(text, "### code-review"))
        self.assertEqual(
            [(row["Rank"], row[_REVIEWER], row["Pairwise wins (PM)"]) for row in reviewers],
            [("1", _CLAUDE_LABEL, "3/6 (50%)"), ("2", _QWEN_LABEL, "0/6 (0%)")],
        )
        self.assertEqual(
            head_to_head,
            [
                {"Reviewer": f"1 · {_CLAUDE_LABEL}", "1": "·", "2": "3–0"},
                {"Reviewer": f"2 · {_QWEN_LABEL}", "1": "0–3", "2": "·"},
            ],
        )

    def test_untagged_selects_identities_with_neither_a_recorded_nor_a_mapped_tag(self) -> None:
        text = self._text("untagged")

        self.assertEqual([row[_DEVELOPER] for row in _developers(text)], ["codex / unknown / unknown"])
        (reviewers,) = _tables(_section(text, "### code-review"))
        self.assertEqual([(row["Rank"], row[_REVIEWER]) for row in reviewers], [("1", _CODEX_LABEL)])
        # The drift-audit Codex carries `hot`.
        self.assertNotIn("### drift-audit", text)

    def test_a_reviewer_only_tag_says_no_developer_row_carries_it(self) -> None:
        text = self._text("hot")

        self.assertEqual(
            _section(text, "## Developers").strip().splitlines()[-1], "No Developer rows carry these model tags."
        )
        self.assertNotIn("### code-review", text)
        (reviewers,) = _tables(_section(text, "### drift-audit"))
        self.assertEqual([row[_REVIEWER] for row in reviewers], [f"{_CODEX_LABEL} / hot"])

    def test_the_header_names_the_model_tags_after_the_run_tags(self) -> None:
        self.assertIn(
            "As of: unknown\n\nRun tags: bench\n\nModel tags: frontier, local. Only these rows are shown;"
            " every figure is the full board's.\n\nRuns: 1; slice windows: 8.\n",
            self._text("local", "frontier", run_tags=frozenset({"bench"})),
        )

    def test_tags_no_row_carries_are_an_error_naming_them(self) -> None:
        with self.assertRaisesRegex(leaderboard.PmError, "no Developer or Reviewer row carries model tag nowhere, zz"):
            self._text("zz", "nowhere")


class TestLeaderboardHeadToHead(unittest.TestCase):
    _TINY = ("opencode", "tiny", "low", False)

    def _text(self) -> str:
        rows = [
            # Codex twice and Claude in every panel: the two Codex reviews are
            # no pair. Slice 0 also panels Tiny, which has one commission.
            _row(
                [_sub(OPUS)],
                slice_id=f"Slice {n}",
                reviews=[
                    _rev("r1", _CODEX, {"score": 2}),
                    _rev("r2", _CODEX, {"score": 2}),
                    _rev("r3", _CLAUDE, {"score": 1}),
                    *([_rev("r4", self._TINY, {"score": 2})] if n == 0 else []),
                ],
                comparisons=[_order("r4", "r1", "r2", "r3") if n == 0 else _order("r1", "r2", "r3")],
            )
            for n in range(3)
        ]
        # Qwen shares one panel with Claude and none with Codex.
        rows.append(
            _row(
                [_sub(OPUS)],
                slice_id="Slice 3",
                reviews=[_rev("r1", _QWEN, {"score": 1}), _rev("r2", _CLAUDE, {"score": 1})],
                comparisons=[_order("r2", "r1")],
            )
        )
        rows += [_row([_sub(OPUS)], slice_id=f"Slice {n}", reviews=[_rev("r1", _QWEN, {"score": 1})]) for n in (4, 5)]
        return _render_rows(rows)

    def test_pairwise_shares_count_unranked_partners_but_not_same_group_pairs(self) -> None:
        reviewers = _tables(_section(self._text(), "### code-review"))[0]

        self.assertEqual(
            [(row["Rank"], row[_REVIEWER], row["Pairwise wins (PM)"]) for row in reviewers],
            [
                ("1", _CODEX_LABEL, "6/8 (75%)"),
                ("2", _CLAUDE_LABEL, "1/8 (13%)"),
                ("3", _QWEN_LABEL, "0/1 (0%)"),
                ("–", "opencode / tiny / low", "3/3 (100%)"),
            ],
        )

    def test_matrix_reads_row_wins_against_column_among_ranked_reviewers(self) -> None:
        section = _section(self._text(), "### code-review")

        self.assertIn(
            "Head-to-head (PM): each row's wins–losses against each column, by rank above;"
            " blank where two reviewers never shared a panel.",
            section,
        )
        self.assertEqual(
            _tables(section)[1],
            [
                {"Reviewer": f"1 · {_CODEX_LABEL}", "1": "·", "2": "6–0", "3": ""},
                {"Reviewer": f"2 · {_CLAUDE_LABEL}", "1": "0–6", "2": "·", "3": "1–0"},
                {"Reviewer": f"3 · {_QWEN_LABEL}", "1": "", "2": "0–1", "3": "·"},
            ],
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

    def test_more_events_win_and_equal_events_with_different_bytes_are_errors(self) -> None:
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
        self.assertIn("missing or invalid run_id, run_tag, pm_model, events or rows", errors[2])
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

    def test_pm_model_line_counts_runs_most_first_with_unrecorded_last(self) -> None:
        rows = self._rows(ledger_ts(1))
        files = [
            ("h/run-1.json", _file("run-1", rows, pm_model="opus")),
            ("h/run-2.json", _file("run-2", rows, pm_model="opus")),
            ("h/run-3.json", _file("run-3", rows, pm_model="gpt")),
            ("h/run-4.json", _file("run-4", rows, pm_model="alpha")),
            ("h/run-5.json", _file("run-5", rows, pm_model=None)),
            # An older file has no `pm_model` key at all.
            ("h/run-6.json", _file("run-6", rows)),
        ]
        text, errors = leaderboard.render_leaderboard(files)

        self.assertEqual(errors, [])
        self.assertIn(
            "Runs: 6; slice windows: 6.\n\nPM model: opus (2 runs) · alpha (1 run) · gpt (1 run) · unrecorded (2 runs)\n",
            text,
        )

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
            "pm_model type": _file("bad", [_row([_sub(OPUS)])], pm_model=7),
            "review_id": with_row(reviews=[{**reviewer, "review_id": ["a"]}]),
            "tool": with_row(reviews=[{**reviewer, "tool": {"x": 1}}]),
            "developer": with_row(submissions=[{**_sub(OPUS), "developer": {"tool": ["x"]}}]),
            "order": with_row(comparisons=[{"origin": 0, "order": [["a"]]}]),
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
        (row,) = _developers(_render_rows(rows))

        self.assertEqual(row["First score (PM)"], "1.13")


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

    def _seed(self) -> None:
        host = self.root / "mac"
        host.mkdir(parents=True)
        (host / "run-1.json").write_bytes(_file("run-1", [_row([_sub(OPUS)], decided_at=ledger_ts(5))]))

    def test_out_path_receives_the_leaderboard(self) -> None:
        self._seed()
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

    def test_run_tags_keep_only_runs_carrying_one_of_them(self) -> None:
        self._seed()
        for run_id, tag in (("run-2", "alpha"), ("run-3", "beta"), ("run-4", "gamma")):
            (self.root / "mac" / f"{run_id}.json").write_bytes(
                _file(run_id, [_row([_sub(OPUS)], decided_at=ledger_ts(5))], run_tag=tag)
            )

        code, stdout, err = self._main(["ledger", "render", "--run-tag", "beta", "--run-tag", "alpha"])

        self.assertEqual(code, 0, err)
        # Beside the default board, never over it, and the command says so.
        self.assertFalse((self.root / "leaderboard.md").exists())
        self.assertIn("leaderboard-alpha+beta.md", stdout)
        text = (self.root / "leaderboard-alpha+beta.md").read_text(encoding="utf-8")
        self.assertIn(f"As of: {ledger_ts(5)}\n\nRun tags: alpha, beta\n", text)
        self.assertIn("Runs: 2; slice windows: 2.", text)

    def test_run_tags_matching_no_run_exit_two_naming_them_and_the_directory(self) -> None:
        self._seed()

        code, _stdout, err = self._main(["ledger", "render", "--run-tag", "nowhere"])

        self.assertEqual(code, 2)
        self.assertIn(f"no per-run ledger file has run tag nowhere under {self.root}", err)
        self.assertFalse((self.root / "leaderboard.md").exists())

    def test_model_tags_read_the_owners_map_and_name_the_default_file(self) -> None:
        self._seed()
        (self.root / "mac" / "run-2.json").write_bytes(
            _file("run-2", [_row([_sub(SONNET)], decided_at=ledger_ts(6))], run_tag="bench")
        )
        (self.root / "model-tags.json").write_text(json.dumps({"frontier": ["claude-opus-*"]}), encoding="utf-8")

        code, stdout, err = self._main(["ledger", "render", "--model-tag", "frontier"])

        self.assertEqual(code, 0, err)
        self.assertIn("filtered by model tag; the default", stdout)
        self.assertFalse((self.root / "leaderboard.md").exists())
        text = (self.root / "leaderboard-frontier.md").read_text(encoding="utf-8")
        self.assertEqual([row[_DEVELOPER] for row in _developers(text)], [_OPUS_LABEL])

        code, stdout, err = self._main(["ledger", "render", "--model-tag", "untagged", "--run-tag", "bench"])

        self.assertEqual(code, 0, err)
        self.assertIn("filtered by run tag and model tag", stdout)
        text = (self.root / "leaderboard-bench+untagged.md").read_text(encoding="utf-8")
        self.assertEqual([row[_DEVELOPER] for row in _developers(text)], [_SONNET_LABEL])

    def test_an_absent_map_gives_no_tags(self) -> None:
        self._seed()

        code, _stdout, err = self._main(["ledger", "render", "--model-tag", "untagged"])

        self.assertEqual(code, 0, err)
        text = (self.root / "leaderboard-untagged.md").read_text(encoding="utf-8")
        self.assertEqual([row[_DEVELOPER] for row in _developers(text)], [_OPUS_LABEL])

    def test_a_malformed_map_exits_two_naming_it(self) -> None:
        self._seed()
        path = self.root / "model-tags.json"
        cases = {
            "invalid JSON": (b"{", "is not valid JSON"),
            "reserved tag": (b'{"untagged": ["claude-*"]}', "cannot be 'untagged'"),
            "empty pattern list": (b'{"frontier": []}', "must map to a non-empty list of non-blank patterns"),
        }
        for name, (raw, expected) in cases.items():
            with self.subTest(name):
                path.write_bytes(raw)

                code, _stdout, err = self._main(["ledger", "render", "--model-tag", "frontier"])

                self.assertEqual(code, 2)
                self.assertIn(f"the model tag map {path}", err)
                self.assertIn(expected, err)
                self.assertEqual(sorted(p.name for p in self.root.glob("leaderboard*")), [])

    def test_an_unwritable_out_path_is_an_error_naming_it(self) -> None:
        self._seed()
        out = self.root.parent / "a-directory"
        out.mkdir()

        code, _stdout, err = self._main(["ledger", "render", "--out", str(out)])

        self.assertEqual(code, 2)
        self.assertIn(f"pm: error: cannot write the leaderboard to {out}", err)
        self.assertNotIn("Traceback", err)


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
        text = (self.root / "leaderboard-bench.md").read_text(encoding="utf-8")
        self.assertIn("Runs: 1; slice windows: 1.", text)
        self.assertIn("None.", _section(text, "## Errors"))
        self.assertIn(" / hot |", text)
        self.assertIn(" / warm |", text)

    def test_pm_model_is_set_in_every_copy_and_reaches_the_board(self) -> None:
        files = self._seed()

        code, _stdout, err = self._main(["ledger", "tag", "run-1", "--pm-model", " opus-5 "])

        self.assertEqual(code, 0, err)
        mac = (self.root / "mac" / "run-1.json").read_bytes()
        self.assertEqual(mac, (self.root / "studio" / "run-1.json").read_bytes())
        payload = json.loads(mac)
        self.assertEqual(payload["pm_model"], "opus-5")
        # Nothing else changes: the tags stay as recorded.
        self.assertEqual({**json.loads(files["mac/run-1.json"]), "pm_model": "opus-5"}, payload)

        code, _stdout, err = self._main(["ledger", "render"])

        self.assertEqual(code, 0, err)
        text = (self.root / "leaderboard.md").read_text(encoding="utf-8")
        self.assertIn("PM model: opus-5 (1 run) · unrecorded (1 run)\n", text)
        self.assertIn("None.", _section(text, "## Errors"))

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
            "blank PM model": (["run-1", "--pm-model", "  "], "--pm-model must be one non-blank line"),
            "no tag given": (["run-1"], "needs --run-tag, --pm-model, --model-tag"),
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

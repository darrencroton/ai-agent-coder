"""Protected behaviours: the code-metrics block measured at accept.

`code_block` is exercised against real fixture commits, one scenario per test.
Expected values are hand-computed from the fixture text, so a change in how
code-health counts lines or complexity shows up here rather than silently
shifting every stored block. The accept-level wiring (`entry["code"]`, the
stderr warning) is tested in `test_finalize.py`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import PmTestCase

from pm_lib import code_metrics

_CATEGORIES = ("production", "test", "documentation", "configuration", "data", "other")

_MODULE = "# header comment\ndef f(x):\n    if x:\n        return 1\n\n    return 2\n"
_TEST_MODULE = (
    "import unittest\n"
    "\n"
    "\n"
    "class T(unittest.TestCase):\n"
    "    def test_a(self):\n"
    "        if 1 and 2:\n"
    "            pass\n"
)


def _zero_lines() -> dict[str, dict[str, int]]:
    return {category: {"code": 0, "comment": 0, "blank": 0} for category in _CATEGORIES}


class CodeMetricsTestCase(PmTestCase):
    def commit(self, files: dict[str, str | None]) -> str:
        """Write (or, for None, delete) the files, commit them, and return the new HEAD."""
        for name, text in files.items():
            path = self.repo / name
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "fixture commit")
        return self._git("rev-parse", "HEAD").stdout.strip()


class TestLinesAndComplexity(CodeMetricsTestCase):
    def test_added_files_yield_exact_deltas_by_category_and_kind(self) -> None:
        before = self._git("rev-parse", "HEAD").stdout.strip()
        head = self.commit(
            {
                "src/mod.py": _MODULE,
                "tests/test_x.py": _TEST_MODULE,
                "docs/guide.md": "# Title\n\ntext\n",
            }
        )

        block = code_metrics.code_block(self.repo, before, head)

        expected = _zero_lines()
        expected["production"] = {"code": 4, "comment": 1, "blank": 1}
        expected["test"] = {"code": 5, "comment": 0, "blank": 2}
        # Markdown has no comment syntax in code-health, so its two text lines are "code".
        expected["documentation"] = {"code": 2, "comment": 0, "blank": 1}
        self.assertEqual(
            block,
            {
                "lines": expected,
                "complexity": {
                    "before": {"sum": 0, "max": 0},
                    "after": {"sum": 5, "max": 3},
                },
                "complexity_reason": None,
            },
        )

    def test_deleted_file_is_negative_and_unchanged_count_is_zero(self) -> None:
        before = self.commit(
            {
                "old.py": "def g(x):\n    if x:\n        return 1\n    return 2\n",
                "same.py": "a = 1\nb = 2\n",
            }
        )
        head = self.commit({"old.py": None, "same.py": "a = 1\nc = 3\n"})

        block = code_metrics.code_block(self.repo, before, head)

        expected = _zero_lines()
        expected["production"]["code"] = -4
        self.assertEqual(block["lines"], expected)
        self.assertEqual(
            block["complexity"],
            {"before": {"sum": 2, "max": 2}, "after": {"sum": 0, "max": 0}},
        )

    def test_unparsable_python_nulls_complexity_but_keeps_lines(self) -> None:
        before = self._git("rev-parse", "HEAD").stdout.strip()
        head = self.commit({"good.py": "x = 1\n", "bad.py": "def (:\n"})

        block = code_metrics.code_block(self.repo, before, head)

        self.assertIsNone(block["complexity"])
        self.assertRegex(block["complexity_reason"], r"^bad\.py:1: ")
        self.assertEqual(
            block["lines"]["production"], {"code": 2, "comment": 0, "blank": 0}
        )

    def test_python_that_stops_parsing_on_the_before_side_is_named_too(self) -> None:
        before = self.commit({"was_bad.py": "def (:\n"})
        head = self.commit({"was_bad.py": "x = 1\n"})

        block = code_metrics.code_block(self.repo, before, head)

        self.assertIsNone(block["complexity"])
        self.assertRegex(block["complexity_reason"], r"^was_bad\.py:1: ")
        self.assertEqual(block["lines"]["production"]["code"], 0)

    def test_no_before_head_diffs_against_the_empty_tree(self) -> None:
        head = self.commit({"src/mod.py": _MODULE})

        block = code_metrics.code_block(self.repo, None, head)

        # README.md ("hello") is part of the diff against the empty tree.
        self.assertEqual(
            block["lines"]["documentation"], {"code": 1, "comment": 0, "blank": 0}
        )
        self.assertEqual(
            block["lines"]["production"], {"code": 4, "comment": 1, "blank": 1}
        )
        self.assertEqual(
            block["complexity"],
            {"before": {"sum": 0, "max": 0}, "after": {"sum": 2, "max": 2}},
        )


class TestNeverRaises(CodeMetricsTestCase):
    def test_missing_health_file_returns_error_block(self) -> None:
        head = self._git("rev-parse", "HEAD").stdout.strip()
        missing = self.repo / "no-such-health.py"

        with mock.patch.object(code_metrics, "_HEALTH_PATH", missing):
            block = code_metrics.code_block(self.repo, None, head)

        self.assertEqual(list(block), ["error"])
        self.assertIn("FileNotFoundError", block["error"])

    def test_unknown_commit_returns_error_block(self) -> None:
        block = code_metrics.code_block(self.repo, None, "0" * 40)

        self.assertEqual(list(block), ["error"])

    def test_health_module_is_loaded_once_per_path(self) -> None:
        self.assertIs(code_metrics._load_health(), code_metrics._load_health())


if __name__ == "__main__":
    unittest.main()

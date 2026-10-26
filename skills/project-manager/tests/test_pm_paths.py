"""Protected behaviour: host access to the sandbox-writable `.pm/` tree is guarded.

`<worktree>/.pm/` is writable by the sandboxed Developer, so a planted symlink
or hard link there must never redirect a host read or write. These tests pin
the boundary of `pm_paths.open_pm_file` and its wrappers: a normal write
round-trips and creates missing directories; a symlink at the leaf, at an
intermediate directory, or at `.pm` itself is refused with a `PmError` naming
the component and leaves the link's target untouched; a hard-linked leaf is
refused before anything is truncated; a read follows no link either; and an
absent file on read is a plain `FileNotFoundError`, not a refusal.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import PmError  # noqa: E402
from pm_lib import pm_paths  # noqa: E402
from pm_lib import slice_ops  # noqa: E402


class PmPathsTestCase(unittest.TestCase):
    """A temp `repo` (the trusted root) beside an `outside` directory standing in
    for anywhere the operator can write that the sandbox must not reach."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.repo = base / "repo"
        self.repo.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        self.victim = self.outside / "victim.txt"
        self.victim.write_text("original\n", encoding="utf-8")

    def assert_refused(self, action, component: str) -> None:
        with self.assertRaises(PmError) as caught:
            action()
        message = str(caught.exception)
        self.assertIn(component, message)
        self.assertIn("sandbox-writable .pm/", message)
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "original\n")

    def access(self, path: Path, *, write: bool):
        """The guarded write (of attacker text) or read of `path`, as a callable."""
        if write:
            return lambda: pm_paths.write_pm_text(self.repo, path, "attacker\n")
        return lambda: pm_paths.read_pm_text(self.repo, path)


class TestGuardedWrite(PmPathsTestCase):
    def test_a_write_creates_missing_directories_and_round_trips(self) -> None:
        target = self.repo / ".pm" / "runs" / "r1" / "slices" / "slice-001" / "pane.txt"
        pm_paths.write_pm_text(self.repo, target, "pane text\n")
        self.assertEqual(pm_paths.read_pm_text(self.repo, target), "pane text\n")
        pm_paths.write_pm_bytes(self.repo, target, b"\x00bytes")
        self.assertEqual(pm_paths.read_pm_bytes(self.repo, target), b"\x00bytes")

    def test_a_rewrite_replaces_the_whole_file(self) -> None:
        target = self.repo / ".pm" / "pane.txt"
        pm_paths.write_pm_text(self.repo, target, "a long first capture\n")
        pm_paths.write_pm_text(self.repo, target, "short\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "short\n")

    def test_an_exclusive_write_refuses_an_existing_file(self) -> None:
        target = self.repo / ".pm" / "answer-1.md"
        pm_paths.write_pm_text(self.repo, target, "first", exclusive=True)
        with self.assertRaises(FileExistsError):
            pm_paths.write_pm_text(self.repo, target, "second", exclusive=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "first")

    def test_a_path_outside_the_pm_tree_is_refused(self) -> None:
        with self.assertRaises(PmError):
            pm_paths.write_pm_text(self.repo, self.repo / "elsewhere.txt", "x")
        with self.assertRaises(PmError):
            pm_paths.write_pm_text(self.repo, self.repo / ".pm" / ".." / "elsewhere.txt", "x")
        self.assertFalse((self.repo / "elsewhere.txt").exists())

    def test_a_symlinked_leaf_is_refused_and_its_target_untouched(self) -> None:
        pm = self.repo / ".pm"
        pm.mkdir()
        leaf = pm / "pane.txt"
        leaf.symlink_to(self.victim)
        for write in (True, False):
            with self.subTest(write=write):
                self.assert_refused(self.access(leaf, write=write), "pane.txt")
        self.assert_refused(lambda: pm_paths.read_pm_bytes(self.repo, leaf), "pane.txt")
        self.assert_refused(lambda: pm_paths.pm_file_present(self.repo, leaf), "pane.txt")

    def test_a_dangling_symlinked_leaf_is_refused_and_not_created(self) -> None:
        pm = self.repo / ".pm"
        pm.mkdir()
        missing = self.outside / "not-yet.txt"
        (pm / "pane.txt").symlink_to(missing)
        self.assert_refused(lambda: pm_paths.write_pm_text(self.repo, pm / "pane.txt", "attacker\n"), "pane.txt")
        self.assertFalse(missing.exists())

    def test_a_symlinked_intermediate_directory_is_refused(self) -> None:
        pm = self.repo / ".pm"
        (pm / "runs").mkdir(parents=True)
        (pm / "runs" / "r1").symlink_to(self.outside)
        target = pm / "runs" / "r1" / "victim.txt"
        for write in (True, False):
            with self.subTest(write=write):
                self.assert_refused(self.access(target, write=write), "r1")

    def test_a_symlinked_pm_root_is_refused_and_nothing_is_created_below_it(self) -> None:
        (self.repo / ".pm").symlink_to(self.outside)
        target = self.repo / ".pm" / "victim.txt"
        self.assert_refused(lambda: pm_paths.write_pm_text(self.repo, target, "attacker\n"), ".pm")
        self.assert_refused(lambda: pm_paths.ensure_pm_dir(self.repo, self.repo / ".pm" / "runs" / "r1"), ".pm")
        self.assertEqual(sorted(path.name for path in self.outside.iterdir()), ["victim.txt"])

    def test_a_hard_linked_leaf_is_refused_before_it_is_truncated_or_read(self) -> None:
        pm = self.repo / ".pm"
        pm.mkdir()
        os.link(self.victim, pm / "pane.txt")
        for write in (True, False):
            with self.subTest(write=write):
                self.assert_refused(self.access(pm / "pane.txt", write=write), "pane.txt")

    def test_a_directory_at_the_leaf_is_refused(self) -> None:
        pm = self.repo / ".pm"
        (pm / "pane.txt").mkdir(parents=True)
        self.assert_refused(lambda: pm_paths.write_pm_text(self.repo, pm / "pane.txt", "x"), "pane.txt")

    def test_a_fifo_at_the_leaf_is_refused_not_waited_on(self) -> None:
        pm = self.repo / ".pm"
        pm.mkdir()
        os.mkfifo(pm / "pane-live.txt")
        self.assert_refused(lambda: pm_paths.read_pm_text(self.repo, pm / "pane-live.txt"), "pane-live.txt")

    def test_ensure_pm_dir_creates_real_directories_and_refuses_a_link(self) -> None:
        pm_paths.ensure_pm_dir(self.repo, self.repo / ".pm" / "runs" / "r1")
        self.assertTrue((self.repo / ".pm" / "runs" / "r1").is_dir())
        (self.repo / ".pm" / "runs" / "r2").symlink_to(self.outside)
        self.assert_refused(lambda: pm_paths.ensure_pm_dir(self.repo, self.repo / ".pm" / "runs" / "r2" / "x"), "r2")


class TestGuardedRead(PmPathsTestCase):
    def test_an_absent_file_is_file_not_found_and_not_present(self) -> None:
        missing = self.repo / ".pm" / "runs" / "r1" / "pane-live.txt"
        with self.assertRaises(FileNotFoundError):
            pm_paths.read_pm_text(self.repo, missing)
        self.assertFalse(pm_paths.pm_file_present(self.repo, missing))
        self.assertFalse((self.repo / ".pm").exists(), "a read must not create directories")

    def test_a_present_file_is_present_and_the_question_head_reads_through_the_guard(self) -> None:
        question = self.repo / ".pm" / "question.md"
        pm_paths.write_pm_text(self.repo, question, "which?\n")
        self.assertTrue(pm_paths.pm_file_present(self.repo, question))
        self.assertEqual(slice_ops.read_question_head(self.repo, question), "which?")

    def test_a_symlinked_question_is_refused_not_reported_unreadable(self) -> None:
        pm = self.repo / ".pm"
        pm.mkdir()
        (pm / "question.md").symlink_to(self.victim)
        self.assert_refused(lambda: slice_ops.read_question_head(self.repo, pm / "question.md"), "question.md")


if __name__ == "__main__":
    unittest.main()

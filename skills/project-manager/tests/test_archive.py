"""Protected behaviours: `archive.archive_child`, the capture `stop-child --archive` makes.

The fixture is a node worktree as a stopped campaign child leaves it,
built without tmux: a product repository whose committed `.gitignore`
ignores `output/` and HANDOFF.md, a linked worktree holding the child's
`.pm/` tree (its manifest's `node_dir` pointing into a small committed
dossier whose brief names `output/` as a git-ignored output directory), a
state root, a partly staged edit, an untracked file, and an `output/`
directory holding a file and a nested `git init` repository. Everything
sits under the test's temp directory, outside the repository under test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import PmTestCase, make_linked_worktree

from pm_lib import PmError
from pm_lib import archive
from pm_lib import state as state_mod

_BRIEF = "# Brief: t\n\n## Allowed paths\n- README.md\n- Git-ignored output directories: `output/`\n"
_GIT_IDENTITY = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args], check=True, text=True, capture_output=True
    ).stdout


def _nested_repository(path: Path, *, commit: bool) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "result.txt").write_text("nested output\n", encoding="utf-8")
    if commit:
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "nested")


class ArchiveTestCase(PmTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / ".gitignore").write_text("output/\n/HANDOFF.md\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-q", "-m", "ignore output")
        self.dossier = self.repo.parent / "dossier"
        self.node_dir = self.dossier / "nodes" / "n1"
        self.node_dir.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.dossier)], check=True)
        (self.dossier / "brief.md").write_text(_BRIEF, encoding="utf-8")
        (self.node_dir / "launcher.md").write_text("# Launcher\n", encoding="utf-8")
        _git(self.dossier, "add", "-A")
        _git(self.dossier, "commit", "-q", "-m", "dossier")

        self.worktree = make_linked_worktree(self.repo, self.repo.parent / "node-wt", "campaign/t/n1").resolve()
        pm = self.worktree / ".pm"
        (pm / "campaign").mkdir(parents=True)
        (pm / ".gitignore").write_text("*\n", encoding="utf-8")
        self.manifest = {"branch": "campaign/t/n1", "node_dir": str(self.node_dir.resolve())}
        (pm / "campaign" / "child.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        # A link the child planted: archived as the link, its target never read.
        self.secret = self.repo.parent / "secret.txt"
        self.secret.write_text("not for the archive\n", encoding="utf-8")
        (pm / "campaign" / "planted").symlink_to(self.secret)
        (pm / "runs" / "run-1").mkdir(parents=True)
        (pm / "runs" / "run-1" / "slice.log").write_text("slice log\n", encoding="utf-8")
        state_root = state_mod.state_root(self.worktree)
        (state_root / "run-1").mkdir(parents=True)
        (state_root / "run-1" / "run.json").write_text('{"status": "stopped"}\n', encoding="utf-8")
        (self.worktree / "HANDOFF.md").write_text("# Handoff\n", encoding="utf-8")

        # A partly staged edit and an untracked file: the uncommitted work.
        (self.worktree / "README.md").write_text("staged version\n", encoding="utf-8")
        _git(self.worktree, "add", "README.md")
        (self.worktree / "README.md").write_text("unstaged version\n", encoding="utf-8")
        (self.worktree / "new.txt").write_text("untracked\n", encoding="utf-8")

        output = self.worktree / "output"
        output.mkdir()
        (output / "data.txt").write_text("measured\n", encoding="utf-8")
        _nested_repository(output / "nested", commit=False)

        self.results = self.repo.parent / "results"
        self.validation = self.dossier.resolve() / "validation" / "n1"


class TestArchiveChild(ArchiveTestCase):
    def test_the_captures_land_in_order_and_the_output_is_copied_with_its_nested_repository(self) -> None:
        tmpdir = Path(tempfile.mkdtemp(dir=self.repo.parent))
        (tmpdir / "scratch.txt").write_text("sandbox scratch\n", encoding="utf-8")
        os.mkfifo(tmpdir / "pipe")

        outcome = archive.archive_child(self.worktree, self.manifest, self.results, tmpdir)

        self.assertEqual(outcome.validation_dir, self.validation)
        self.assertEqual(outcome.results_dir, self.results.resolve() / "n1")
        steps = [
            "HANDOFF.md ->",
            "/.pm/campaign/ ->",
            "/.pm/runs/ ->",
            "archived state root",
            "staged.patch",
            "status.txt",
            "git add -A",
            "diff.patch",
            "/output/ ->",
            "copied sandbox temp dir",
            "removal rule holds: 2 changes listed",
        ]
        positions = [next(i for i, line in enumerate(outcome.lines) if step in line) for step in steps]
        self.assertEqual(positions, sorted(positions), outcome.lines)

        validation = self.validation
        self.assertEqual((validation / "HANDOFF.md").read_text(encoding="utf-8"), "# Handoff\n")
        self.assertEqual(json.loads((validation / "campaign" / "child.json").read_text()), self.manifest)
        self.assertTrue((validation / "campaign" / "planted").is_symlink(), "a planted link is copied as a link")
        self.assertEqual(os.readlink(validation / "campaign" / "planted"), str(self.secret))
        self.assertTrue((validation / "runs" / "run-1" / "slice.log").is_file())
        self.assertTrue((validation / "state" / "run-1" / "run.json").is_file())
        staged = (validation / "staged.patch").read_text(encoding="utf-8")
        self.assertIn("+staged version", staged)
        self.assertNotIn("unstaged version", staged, "staged.patch is written before anything is staged")
        status = (validation / "status.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(status, ["MM README.md", "?? new.txt"], "ignored paths (.pm/, output/) are not listed")
        diff = (validation / "diff.patch").read_text(encoding="utf-8")
        self.assertIn("+unstaged version", diff)
        self.assertIn("+untracked", diff)
        self.assertNotIn(".pm/", diff, "git-ignored paths stay out of diff.patch")

        copied = outcome.results_dir / "output"
        self.assertEqual((copied / "data.txt").read_text(encoding="utf-8"), "measured\n")
        self.assertEqual((copied / "nested" / "result.txt").read_text(encoding="utf-8"), "nested output\n")
        self.assertTrue((copied / "nested" / ".git" / "HEAD").is_file(), "a nested repository is copied as files")
        self.assertEqual((outcome.results_dir / "sandbox-tmp" / "scratch.txt").read_text(), "sandbox scratch\n")
        self.assertFalse(os.path.lexists(outcome.results_dir / "sandbox-tmp" / "pipe"))
        self.assertTrue(any(line.startswith(f"skipped {tmpdir / 'pipe'}") for line in outcome.lines))
        # Nothing is removed: the sources all stay for the stop command to remove.
        for source in (self.worktree / "output" / "nested" / ".git", self.worktree / ".pm" / "runs", tmpdir):
            self.assertTrue(source.exists(), source)

        # A re-run completes over the first archive and keeps its pre-staging capture.
        again = archive.archive_child(self.worktree, self.manifest, self.results, tmpdir)
        self.assertTrue(any(line.startswith("kept ") for line in again.lines), again.lines)
        self.assertEqual((validation / "staged.patch").read_text(encoding="utf-8"), staged)

    def test_a_nested_repository_outside_the_output_directories_is_refused_naming_it(self) -> None:
        _nested_repository(self.worktree / "stray", commit=True)
        with self.assertRaises(archive.ArchiveIncomplete) as caught:
            archive.archive_child(self.worktree, self.manifest, self.results, None)
        message = str(caught.exception)
        self.assertIn("stray", message)
        self.assertIn("nested repository outside the brief's git-ignored output directories", message)
        self.assertIn("the worktree is kept", message)
        self.assertTrue((self.worktree / "stray" / ".git").is_dir())
        self.assertTrue(any("git add -A" in line for line in caught.exception.outcome.lines))
        self.assertFalse(self.results.exists(), "nothing is copied to the results root after a refusal")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes through a 0500 directory")
    def test_a_results_directory_overlapping_the_worktree_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaisesRegex(PmError, "overlaps the worktree"):
            archive.archive_child(self.worktree, self.manifest, self.worktree / "saved", None)
        self.assertFalse((self.worktree / "saved").exists())
        self.assertFalse((self.dossier / "validation").exists())

    def test_a_hard_link_is_copied_from_an_output_directory_and_refused_under_pm(self) -> None:
        """A local clone hard-links its git objects, so output trees may hold them; the child-writable `.pm/` may not."""
        os.link(self.worktree / "output" / "data.txt", self.worktree / "output" / "data-link.txt")
        outcome = archive.archive_child(self.worktree, self.manifest, self.results, None)
        self.assertTrue((outcome.results_dir / "output" / "data-link.txt").is_file())
        os.link(self.worktree / ".pm" / "campaign" / "child.json", self.worktree / ".pm" / "campaign" / "linked.json")
        with self.assertRaisesRegex(archive.ArchiveIncomplete, "a hard link"):
            archive.archive_child(self.worktree, self.manifest, self.results, None)

    def test_an_unwritable_results_root_names_the_copy_and_leaves_every_source_in_place(self) -> None:
        self.results.mkdir()
        self.results.chmod(0o500)
        self.addCleanup(self.results.chmod, 0o700)
        tmpdir = Path(tempfile.mkdtemp(dir=self.repo.parent))
        (tmpdir / "scratch.txt").write_text("sandbox scratch\n", encoding="utf-8")
        with self.assertRaises(archive.ArchiveIncomplete) as caught:
            archive.archive_child(self.worktree, self.manifest, self.results, tmpdir)
        message = str(caught.exception)
        self.assertIn(
            f"could not copy {self.worktree / 'output'} to {self.results.resolve() / 'n1' / 'output'}", message
        )
        self.assertIn("the worktree is kept", message)
        for source in (
            self.worktree / "output" / "data.txt",
            self.worktree / "output" / "nested" / ".git" / "HEAD",
            self.worktree / ".pm" / "campaign" / "child.json",
            tmpdir / "scratch.txt",
        ):
            self.assertTrue(source.is_file(), source)

    def test_a_manifest_with_no_node_dir_has_no_dossier_to_archive_into(self) -> None:
        with self.assertRaises(PmError) as caught:
            archive.archive_child(self.worktree, {**self.manifest, "node_dir": None}, self.results, None)
        self.assertIn("no dossier to archive into", str(caught.exception))
        self.assertFalse(self.validation.exists())

    def test_a_node_dir_not_under_nodes_in_a_dossier_is_refused(self) -> None:
        with self.assertRaises(PmError) as caught:
            archive.archive_child(self.worktree, {**self.manifest, "node_dir": str(self.dossier)}, self.results, None)
        self.assertIn("is not <dossier>/nodes/<node>", str(caught.exception))

    def test_a_linked_pm_runs_directory_is_refused_never_followed(self) -> None:
        elsewhere = self.repo.parent / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "private.txt").write_text("private\n", encoding="utf-8")
        runs = self.worktree / ".pm" / "runs"
        shutil.rmtree(runs)
        runs.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(archive.ArchiveIncomplete) as caught:
            archive.archive_child(self.worktree, self.manifest, self.results, None)
        self.assertIn(f"refused {runs}: a symbolic link", str(caught.exception))
        self.assertFalse((self.validation / "runs").exists())


class TestRemovalRule(unittest.TestCase):
    def test_a_listed_change_with_an_empty_diff_forbids_the_removal(self) -> None:
        outcome = archive.ArchiveOutcome(node="n1", validation_dir=Path("v"), results_dir=Path("r"))
        with self.assertRaises(PmError) as caught:
            archive._check_removal_rule("?? lost.txt\n", b"\n", outcome)
        self.assertIn("status.txt lists 1 changes but diff.patch is empty", str(caught.exception))
        archive._check_removal_rule("", b"", outcome)
        self.assertEqual(outcome.lines, ["removal rule holds: no uncommitted change"])


if __name__ == "__main__":
    unittest.main()

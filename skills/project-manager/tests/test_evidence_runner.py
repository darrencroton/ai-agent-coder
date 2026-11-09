"""Protected behaviours: the capture-only evidence runner skeleton.

`references/evidence-runner.sh` is a shell script a Campaign Manager copies
into a dossier, so it is tested the way it is used: each case builds a
temporary git repository with one commit, copies the skeleton as `run.sh`
with the two REPLACE_ values substituted and the example steps replaced by the
case's own, runs `sh run.sh` from the repository root, and checks the exit
status, stderr and `output/<id>/`. The module also runs `shellcheck` over the
skeleton when it is installed.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pm_test_helpers import PlanTestCase  # noqa: E402

SKELETON = Path(__file__).resolve().parents[1] / "references" / "evidence-runner.sh"
NODE_ID = "node-1"
_BEGIN = "# --- measurements ---\n"
_END = "# --- end measurements ---\n"


class EvidenceRunnerCase(PlanTestCase):
    """A one-commit git repository holding a `.gitignore` of `output/`."""

    def setUp(self) -> None:
        super().setUp()
        (self.repo / ".gitignore").write_text("output/\n", encoding="utf-8")
        (self.repo / "secret").mkdir()
        (self.repo / "secret" / "key.txt").write_text("hidden\n", encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "pm-test@example.com")
        self._git("config", "user.name", "PM Test")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "measured commit")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        # run.sh is the dossier's file, not the product's: keep it out of the
        # worktree's status without committing it (the head is fixed first).
        with (self.repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
            handle.write("run.sh\n")
        self.out = self.repo / "output" / NODE_ID

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, text=True, capture_output=True)

    def run_steps(self, steps: str) -> subprocess.CompletedProcess:
        """Install the skeleton with `steps` as its measurements and run it."""
        text = SKELETON.read_text(encoding="utf-8")
        text = text.replace("OUT=output/REPLACE_NODE_ID", f"OUT=output/{NODE_ID}")
        text = text.replace("EXPECTED_HEAD=REPLACE_WITH_THE_MEASURED_PRODUCT_SHA", f"EXPECTED_HEAD={self.head}")
        start = text.index(_BEGIN) + len(_BEGIN)
        text = text[:start] + steps + "\n" + text[text.index(_END) :]
        (self.repo / "run.sh").write_text(text, encoding="utf-8")
        return subprocess.run(["sh", "run.sh"], cwd=self.repo, text=True, capture_output=True, check=False)

    def manifest_names(self) -> list[str]:
        lines = (self.out / "manifest.sha256").read_text(encoding="utf-8").splitlines()
        return sorted(line.split("  ", 1)[1] for line in lines)

    def written_files(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.out))
            for path in self.out.rglob("*")
            if path.is_file() and path.name not in {"manifest.sha256"}
        )


class TestDeclaredFiles(EvidenceRunnerCase):
    def test_a_measured_command_that_prints_nothing_is_a_measurement_with_an_empty_log(self) -> None:
        """cmp or diff -q on identical outputs is silent on success: a capture, not a capture failure."""
        result = self.run_steps("measure a.log a.status echo hello\nmeasure b.log b.status true\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out / "b.log").read_bytes(), b"")
        self.assertIn("b.log", self.manifest_names())

    def test_commands_that_print_give_a_manifest_listing_every_file(self) -> None:
        result = self.run_steps("measure a.log a.status echo hello\nmeasure b.log b.status echo world\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.manifest_names()
        self.assertEqual(names, self.written_files())
        for name in ("a.log", "a.status", "b.log", "b.status", "context.txt", "declared.txt"):
            self.assertIn(name, names)
        self.assertEqual(
            (self.out / "declared.txt").read_text(encoding="utf-8").splitlines(),
            ["log a.log", "file a.status", "log b.log", "file b.status"],
        )
        listed = dict(
            line.split("  ", 1)[::-1]
            for line in (self.out / "manifest.sha256").read_text(encoding="utf-8").splitlines()
        )
        digest = hashlib.sha256((self.out / "declared.txt").read_bytes()).hexdigest()
        self.assertEqual(listed["declared.txt"], digest)

    def test_expect_names_a_file_a_stage_must_write_and_fill(self) -> None:
        for steps, missing in (
            ("measure a.log a.status echo hello\nexpect extra.txt\n", "extra.txt is missing"),
            ('measure a.log a.status echo hello\nexpect out.log\n: > "$OUT/out.log"\n', "out.log is empty"),
        ):
            with self.subTest(missing=missing):
                result = self.run_steps(steps)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(missing, result.stderr)
                self.assertFalse((self.out / "manifest.sha256").exists())
                shutil.rmtree(self.out)


class TestRequire(EvidenceRunnerCase):
    def test_an_unmet_expected_value_stops_the_runner(self) -> None:
        result = self.run_steps(
            "measure a.log a.status echo hello\nrequire a.status 1\nmeasure b.log b.status echo not-reached\n"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("a.status", result.stderr)
        self.assertFalse((self.out / "b.log").exists())

    def test_a_met_expected_value_and_a_bare_require_pass(self) -> None:
        result = self.run_steps("measure a.log a.status echo hello\nrequire a.status 0\nrequire a.log\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_missing_file_stops_the_runner(self) -> None:
        result = self.run_steps("require nothing.status\n")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("nothing.status", result.stderr)


class TestCleanTree(EvidenceRunnerCase):
    STEP = "measure a.log a.status echo hello\n"

    def test_a_transient_heredoc_file_is_ignored(self) -> None:
        (self.repo / "sh-thd-abc").write_text("x\n", encoding="utf-8")
        result = self.run_steps(self.STEP)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = (self.out / "context.txt").read_text(encoding="utf-8")
        self.assertIn("worktree_clean_at_start=yes", context)
        self.assertIn("worktree_clean_after=yes", context)

    def test_any_other_untracked_file_still_fails_the_check(self) -> None:
        (self.repo / "other.txt").write_text("x\n", encoding="utf-8")
        result = self.run_steps(self.STEP)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("other.txt", result.stderr)

    def test_a_file_written_by_a_step_fails_the_check_after_the_run(self) -> None:
        result = self.run_steps("measure a.log a.status sh -c 'echo x > leaked.txt && echo done'\n")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("leaked.txt", result.stderr)


class TestSparseClone(EvidenceRunnerCase):
    def test_excluded_paths_are_absent_and_the_clone_is_at_the_measured_commit(self) -> None:
        result = self.run_steps("sparse_clone clone '/*' '!/secret'\nmeasure a.log a.status echo hello\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        clone = self.out / "clone"
        self.assertTrue((clone / "README.md").is_file())
        self.assertFalse((clone / "secret").exists())
        head = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"], check=True, text=True, capture_output=True
        ).stdout.strip()
        self.assertEqual(head, self.head)

    def test_a_clone_that_cannot_be_made_stops_the_runner(self) -> None:
        result = self.run_steps('mkdir "$OUT/clone" && echo x > "$OUT/clone/f"\nsparse_clone clone\n')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("clone", result.stderr)


@unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
class TestShellcheck(unittest.TestCase):
    def test_the_skeleton_is_shellcheck_clean(self) -> None:
        result = subprocess.run(["shellcheck", str(SKELETON)], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

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
import os
import shutil
import signal
import subprocess
import sys
import time
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

    def install_steps(self, steps: str) -> None:
        """Install the skeleton as `run.sh` with `steps` as its measurements."""
        text = SKELETON.read_text(encoding="utf-8")
        text = text.replace("OUT=output/REPLACE_NODE_ID", f"OUT=output/{NODE_ID}")
        text = text.replace("EXPECTED_HEAD=REPLACE_WITH_THE_MEASURED_PRODUCT_SHA", f"EXPECTED_HEAD={self.head}")
        start = text.index(_BEGIN) + len(_BEGIN)
        text = text[:start] + steps + "\n" + text[text.index(_END) :]
        (self.repo / "run.sh").write_text(text, encoding="utf-8")

    def run_steps(self, steps: str) -> subprocess.CompletedProcess:
        """Install the skeleton with `steps` as its measurements and run it."""
        self.install_steps(steps)
        return subprocess.run(["sh", "run.sh"], cwd=self.repo, text=True, capture_output=True, check=False)

    def exit_file(self) -> Path:
        return self.repo / "output" / f"{NODE_ID}.exit"

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
                self.exit_file().unlink()

    def test_a_capture_whose_names_exceed_arg_max_still_gets_its_manifest(self) -> None:
        """A capture of thousands of files must not fail at the manifest and lose the measurement."""
        arg_max = int(subprocess.run(["getconf", "ARG_MAX"], text=True, capture_output=True, check=True).stdout)
        # 200-character names: past ARG_MAX on one command line (capped for an unlimited stack).
        count = min(arg_max // 200 + 100, 50_000)
        step = (
            "python3 -c 'import pathlib, sys\n"
            'out = pathlib.Path(sys.argv[1]) / "many"\n'
            "out.mkdir()\n"
            "for i in range(int(sys.argv[2])):\n"
            '    (out / (f"{i:06d}" + "x" * 194)).write_text("x")\n'
            # Names copied from the product can hold quotes and spaces.
            '(out / ("it" + chr(39) + "s here")).write_text("x")\n'
            f'\' "$OUT" {count}\n'
        )
        result = self.run_steps(step)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest_names(), self.written_files())
        self.assertEqual(sum(1 for name in self.manifest_names() if name.startswith("many/")), count + 1)


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

    def test_the_clone_copies_no_hook_templates(self) -> None:
        """A sandbox denies writes under .git/hooks, so the clone must not copy a template's hooks."""
        template = Path(self._tmp.name) / "template"
        (template / "hooks").mkdir(parents=True)
        (template / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\n", encoding="utf-8")
        result = self.run_steps(f'export GIT_TEMPLATE_DIR="{template}"\nsparse_clone clone\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        hooks = self.out / "clone" / ".git" / "hooks"
        self.assertEqual(list(hooks.iterdir()) if hooks.exists() else [], [])

    def test_a_clone_that_cannot_be_made_stops_the_runner(self) -> None:
        result = self.run_steps('mkdir "$OUT/clone" && echo x > "$OUT/clone/f"\nsparse_clone clone\n')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("clone", result.stderr)


class TestStepNames(EvidenceRunnerCase):
    def test_a_steps_own_variables_survive_the_helpers(self) -> None:
        """POSIX sh has no locals: a step's loop over f, with t and got set, must not be clobbered."""
        result = self.run_steps(
            "t=mine\n"
            "got=mine\n"
            "for f in a b; do\n"
            '  expect "extra-$f.txt"\n'
            '  measure "$f.log" "$f.status" echo "$f"\n'
            "  require a.status 0\n"
            '  echo "$f $t $got" >> "$OUT/seen.txt"\n'
            "done\n"
            "expect seen.txt\n"
            'for n in a b; do echo x > "$OUT/extra-$n.txt"; done\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out / "seen.txt").read_text(encoding="utf-8"), "a mine mine\nb mine mine\n")


class TestExitRecord(EvidenceRunnerCase):
    def test_a_successful_run_records_zero_beside_the_output(self) -> None:
        result = self.run_steps("measure a.log a.status echo hello\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.exit_file().read_text(encoding="utf-8"), "0\n")
        self.assertNotIn(self.exit_file().name, self.manifest_names())
        self.assertFalse(self.exit_file().with_suffix(".exit.tmp").exists())

    def test_a_failure_after_the_reservation_records_two(self) -> None:
        result = self.run_steps("require nothing.status\n")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.exit_file().read_text(encoding="utf-8"), "2\n")

    def test_a_refused_second_run_leaves_the_first_record_alone(self) -> None:
        self.assertEqual(self.run_steps("measure a.log a.status echo hello\n").returncode, 0)
        second = self.run_steps("measure a.log a.status echo hello\n")
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("already exists", second.stderr)
        self.assertEqual(self.exit_file().read_text(encoding="utf-8"), "0\n")

    def test_a_pre_existing_record_refuses_the_run_before_the_reservation(self) -> None:
        self.exit_file().parent.mkdir()
        self.exit_file().write_text("7\n", encoding="utf-8")
        result = self.run_steps("measure a.log a.status echo hello\n")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"{self.exit_file().relative_to(self.repo)} already exists", result.stderr)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.exit_file().read_text(encoding="utf-8"), "7\n")

    def test_a_run_killed_by_a_signal_does_not_record_zero(self) -> None:
        """Under macOS /bin/sh the EXIT trap after SIGTERM sees the status of the last completed command, 0;
        dash runs no EXIT trap on a signal and writes no record. Either way, never 0."""
        self.install_steps("measure a.log a.status sh -c 'echo up; exec sleep 60'\n")
        runner = subprocess.Popen(
            ["sh", "run.sh"],
            cwd=self.repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            log = self.out / "a.log"
            deadline = time.monotonic() + 30
            while not (log.exists() and log.read_text(encoding="utf-8")):  # the step is running
                self.assertLess(time.monotonic(), deadline, "the step never started")
                time.sleep(0.05)
            os.kill(runner.pid, signal.SIGTERM)
            runner.wait(timeout=30)
        finally:
            try:
                os.killpg(runner.pid, signal.SIGKILL)  # the orphaned sleep
            except ProcessLookupError:
                pass
        if sys.platform == "darwin":  # /bin/sh is bash there, which runs the trap
            self.assertTrue(self.exit_file().exists())
        if self.exit_file().exists():
            self.assertEqual(self.exit_file().read_text(encoding="utf-8"), "2\n")


@unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
class TestShellcheck(unittest.TestCase):
    def test_the_skeleton_is_shellcheck_clean(self) -> None:
        result = subprocess.run(["shellcheck", str(SKELETON)], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

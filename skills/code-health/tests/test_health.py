"""Boundary tests for the portable code-health evidence collector."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import health


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


class Repo:
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="health-test-")
        sh(["git", "init", "-q", "."], self.path)
        sh(["git", "config", "user.email", "t@example.com"], self.path)
        sh(["git", "config", "user.name", "T"], self.path)
        return self

    def __exit__(self, *args):
        shutil.rmtree(self.path, ignore_errors=True)

    def write(self, name, contents):
        full = os.path.join(self.path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(contents)

    def commit(self, message="commit"):
        sh(["git", "add", "-A"], self.path)
        sh(["git", "commit", "-q", "-m", message], self.path)
        return sh(["git", "rev-parse", "HEAD"], self.path).stdout.strip()


class InRepo(unittest.TestCase):
    def run_health(self, repo, *args):
        old = os.getcwd()
        os.chdir(repo.path)
        try:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                return health.main(list(args))
        finally:
            os.chdir(old)


class TestScopeAndCoverage(InRepo):
    def test_untracked_nonignored_source_is_in_scope(self):
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.commit()
            repo.write("new.py", "y = 2\n")
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                source, _ = health.scan(Path(repo.path), health.tracked_and_untracked(Path(repo.path)))
            finally:
                os.chdir(old)
            self.assertEqual([f.path for f in source], ["a.py", "new.py"])

    def test_index_stage_rows_are_deduplicated_and_disclosed(self):
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            root = Path(repo.path)
            original = health.git
            def fake(current, *args, **kwargs):
                if args[:2] == ("ls-files", "-co"):
                    return "a.py\0a.py\0a.py\0"
                if args[:2] == ("ls-files", "--unmerged"):
                    return "100644 abc 1\ta.py\0" "100644 def 2\ta.py\0"
                return original(current, *args, **kwargs)
            health.git = fake
            try:
                self.assertEqual(health.tracked_and_untracked(root), ["a.py"])
                self.assertEqual(health.unmerged_paths(root), ["a.py"])
            finally:
                health.git = original

    def test_base_snapshot_ignores_export_ignore(self):
        with Repo() as repo:
            repo.write(".gitattributes", "tests/ export-ignore\n")
            repo.write("tests/heavy.py", "def f(x):\n return x\n")
            base = repo.commit()
            with health.archive_revision(Path(repo.path), base) as snapshot:
                self.assertTrue(Path(snapshot, "tests/heavy.py").is_file())

    def test_symlink_is_not_followed_and_is_disclosed(self):
        with Repo() as repo:
            repo.write("real/impl.py", "x = 1\n")
            os.symlink("real/impl.py", Path(repo.path, "link.py"))
            repo.commit()
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["analyze", "--all"]))
            finally:
                os.chdir(old)
            self.assertEqual([row["path"] for row in bundle["facts"]["composition"]["files"]
                              if row["language"] == "Python"], ["real/impl.py"])
            self.assertEqual(bundle["repository"]["coverage_limits"][0]["paths"], ["link.py"])
            self.assertEqual(self.run_health(repo, "analyze", "--all", "--require-coverage"), 0)

    def test_ignored_source_is_absent_from_composition(self):
        with Repo() as repo:
            repo.write(".gitignore", "ignored.py\n")
            repo.write("a.py", "x = 1\n")
            repo.commit()
            repo.write("ignored.py", "hidden = True\n")
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["analyze", "--all"]))
            finally:
                os.chdir(old)
            paths = {row["path"] for row in bundle["facts"]["composition"]["files"]}
            self.assertNotIn("ignored.py", paths)

    def test_non_python_complexity_and_dependencies_are_coverage_gaps(self):
        with Repo() as repo:
            repo.write("app.js", "export const f = x => x\n")
            repo.commit()
            self.assertEqual(self.run_health(repo, "analyze", "--all", "--require-coverage", "--json"), 3)

    def test_unreadable_language_is_not_reported_covered(self):
        source = [health.SourceFile("ok.py", "Python", "production", "rule", "x = 1\n", {})]
        structure = health.python_structure(source)
        coverage = health.coverage_matrix(
            source,
            [{"path": "bad.py", "reason": "unreadable_utf8"}],
            structure,
            health.LizardResult(False, None, [], "missing"),
        )
        self.assertTrue(all(row["status"] == "unavailable" for row in coverage))

    def test_unreadable_non_source_is_recorded(self):
        with Repo() as repo:
            path = Path(repo.path, "data.json")
            path.write_bytes(b"\xff\xfe")
            repo.commit()
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["analyze", "--all"]))
            finally:
                os.chdir(old)
            self.assertEqual(bundle["facts"]["unreadable_files"][0]["path"], "data.json")


class TestPythonFacts(unittest.TestCase):
    def test_ast_complexity_and_cycle_are_raw_measured_facts(self):
        source = [
            health.SourceFile("a.py", "Python", "production", "rule", "import b\n\ndef f(x):\n if x and x > 1:\n  return 1\n return 0\n", {}),
            health.SourceFile("b.py", "Python", "production", "rule", "import a\n", {}),
        ]
        facts = health.python_structure(source)
        self.assertEqual(facts["functions"][0]["cyclomatic"], 3)
        self.assertEqual(facts["imports"]["cycles"], [["a.py", "b.py"]])
        self.assertEqual(facts["imports"]["reverse_reachability"][0]["reverse_reachability"], 1)

    def test_syntax_error_is_coverage_gap_not_zero_complexity(self):
        source = [health.SourceFile("broken.py", "Python", "production", "rule", "def x(:\n", {})]
        facts = health.python_structure(source)
        coverage = health.coverage_matrix(source, [], facts, health.LizardResult(False, None, [], "missing"))
        self.assertTrue(facts["parse_errors"])
        self.assertEqual(next(r for r in coverage if r["metric_family"] == "cyclomatic")["status"], "unavailable")

    def test_ambiguous_suffix_import_is_not_guessed(self):
        source = [
            health.SourceFile("a/state.py", "Python", "production", "rule", "x = 1\n", {}),
            health.SourceFile("b/state.py", "Python", "production", "rule", "x = 2\n", {}),
            health.SourceFile("use.py", "Python", "production", "rule", "import state\n", {}),
        ]
        self.assertEqual(health.python_structure(source)["imports"]["edges"], [])

    def test_lambda_decisions_are_counted_in_enclosing_function(self):
        source = [health.SourceFile("a.py", "Python", "production", "rule",
                                    "def f(g):\n return g(lambda x: 1 if x else 2)\n", {})]
        self.assertEqual(health.python_structure(source)["functions"][0]["cyclomatic"], 2)

    def test_pyi_module_name_has_no_trailing_dot(self):
        self.assertEqual(health.module_name("pkg/a.pyi"), "pkg.a")

    def test_import_prefers_implementation_over_type_stub(self):
        source = [
            health.SourceFile("main.py", "Python", "production", "rule", "import pkg.a\n", {}),
            health.SourceFile("pkg/a.py", "Python", "production", "rule", "x = 1\n", {}),
            health.SourceFile("pkg/a.pyi", "Python", "production", "rule", "x: int\n", {}),
        ]
        self.assertEqual(health.python_structure(source)["imports"]["edges"],
                         [{"from": "main.py", "to": "pkg/a.py"}])

    def test_python_coverage_precedes_optional_lizard(self):
        source = [health.SourceFile("a.py", "Python", "production", "rule", "x = 1\n", {})]
        row = next(item for item in health.coverage_matrix(
            source, [], health.python_structure(source), health.LizardResult(True, "1.0", []),
        ) if item["metric_family"] == "cyclomatic")
        self.assertEqual(row["reason"], "Python stdlib AST")


class TestDifferentialMechanics(InRepo):
    def test_whitespace_normalised_windows_compare_by_count(self):
        current = [health.SourceFile("new.py", "Python", "production", "rule", "a = 1\nb = 2\na=1\nb=2\n", {})]
        base = [health.SourceFile("old.py", "Python", "production", "rule", "a=1\nb=2\n", {})]
        facts = health.duplication_facts(current, 2, base, {"new.py"})
        row = next(r for r in facts["signatures"] if r["current_count"] == 2)
        self.assertEqual(row["base_count"], 1)
        self.assertEqual(row["count_delta"], 1)
        self.assertEqual(row["changed_side_occurrences"][0]["path"], "new.py")

    def test_a_new_unique_window_is_not_reported_as_duplication(self):
        current = [health.SourceFile("new.py", "Python", "production", "rule", "a = 1\nb = 2\n", {})]
        facts = health.duplication_facts(current, 2, [], {"new.py"})
        self.assertEqual(facts["signatures"], [])

    def test_rename_is_reported_and_empty_differential_is_exit_three(self):
        with Repo() as repo:
            repo.write("old.py", "x = 1\n")
            base = repo.commit()
            sh(["git", "mv", "old.py", "new.py"], repo.path)
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                changed, renames = health.changed_paths(Path(repo.path), base)
            finally:
                os.chdir(old)
            self.assertEqual(changed, {"new.py"})
            self.assertEqual(renames, {"new.py": "old.py"})
            # No working change after committing must be a coverage/empty diff result.
            repo.commit("rename")
            self.assertEqual(self.run_health(repo, "analyze", "--base", "HEAD", "--json"), 3)

    def test_deletion_only_change_is_not_an_empty_differential(self):
        with Repo() as repo:
            repo.write("gone.py", "x = 1\n")
            base = repo.commit()
            os.unlink(os.path.join(repo.path, "gone.py"))
            self.assertEqual(self.run_health(repo, "analyze", "--base", base, "--json"), 0)

    def test_history_shallow_guard(self):
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.commit()
            # Git config cannot reliably simulate a shallow clone; the guard is
            # isolated by replacing the git query with a tiny deterministic fake.
            original = health.git
            def fake(root, *args, **kwargs):
                if args == ("rev-parse", "--is-shallow-repository"):
                    return "true\n"
                return original(root, *args, **kwargs)
            health.git = fake
            try:
                self.assertEqual(health.history_facts(Path(repo.path), 4)["reason"], "shallow_repository")
            finally:
                health.git = original

    def test_history_reports_revision_and_line_churn(self):
        with Repo() as repo:
            repo.write("a.py", "one = 1\n")
            repo.commit()
            repo.write("a.py", "one = 1\ntwo = 2\nthree = 3\n")
            repo.commit("grow")
            row = health.history_facts(Path(repo.path), 10)["churn"][0]
            self.assertEqual(row, {"path": "a.py", "revisions": 2, "additions": 3, "deletions": 0})

    def test_history_accepts_sha256_commit_ids(self):
        digest = "a" * 64
        original = health.git
        def fake(root, *args, **kwargs):
            if args == ("rev-parse", "--is-shallow-repository"):
                return "false\n"
            if args and args[0] == "log":
                return f"{digest}\n1\t0\ta.py\n"
            if args == ("rev-parse", "HEAD"):
                return digest + "\n"
            raise AssertionError(args)
        health.git = fake
        try:
            self.assertEqual(health.history_facts(Path("."), 1)["commits_considered"], 1)
        finally:
            health.git = original

    def test_history_rename_keys_are_repository_paths(self):
        with Repo() as repo:
            repo.write("src/alpha.py", "x = 1\n")
            repo.commit()
            sh(["git", "mv", "src/alpha.py", "src/beta.py"], repo.path)
            repo.commit("rename")
            paths = {row["path"] for row in health.history_facts(Path(repo.path), 10)["churn"]}
            self.assertFalse(any("=>" in path for path in paths))

    def test_candidates_require_changed_values_and_honor_renames(self):
        current = [
            health.SourceFile("renamed.py", "Python", "production", "rule", "", {"code": 20}),
            health.SourceFile("changed.py", "Python", "production", "rule", "", {"code": 5}),
        ]
        base = [
            health.SourceFile("old.py", "Python", "production", "rule", "", {"code": 20}),
            health.SourceFile("changed.py", "Python", "production", "rule", "", {"code": 4}),
        ]
        structure = {"functions": [
            {"path": "renamed.py", "name": "f", "line": 1, "cyclomatic": 9},
            {"path": "changed.py", "name": "g", "line": 1, "cyclomatic": 2},
        ], "imports": {"cycles": []}}
        old_structure = {"functions": [
            {"path": "old.py", "name": "f", "line": 1, "cyclomatic": 9},
            {"path": "changed.py", "name": "g", "line": 1, "cyclomatic": 1},
        ], "imports": {"cycles": []}}
        rows = health.candidates(current, base, structure, [], {"signatures": []},
                                 {"renamed.py", "changed.py"}, old_structure, [],
                                 {"renamed.py": "old.py"}, True)
        self.assertEqual({(row["kind"], row["path"]) for row in rows},
                         {("cyclomatic", "changed.py"), ("file_size", "changed.py")})

    def test_candidates_are_bounded_per_family(self):
        functions = [{"path": f"f{i}.py", "name": "f", "line": 1, "cyclomatic": i + 2}
                     for i in range(7)]
        structure = {"functions": functions, "imports": {"cycles": []}}
        rows = health.candidates([], [], structure, [], {"signatures": []},
                                 {row["path"] for row in functions}, {"functions": [], "imports": {"cycles": []}},
                                 [], {}, True)
        self.assertEqual(sum(row["kind"] == "cyclomatic" for row in rows), health.TOP_PER_FAMILY)

    def test_json_is_deterministic_and_has_bundle_version_not_schema_version(self):
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.commit()
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle_a, _ = health.build_bundle(health.parser().parse_args(["analyze", "--all"]))
                bundle_b, _ = health.build_bundle(health.parser().parse_args(["analyze", "--all"]))
            finally:
                os.chdir(old)
            encoded_a = json.dumps(bundle_a, sort_keys=True, separators=(",", ":"))
            self.assertEqual(encoded_a, json.dumps(bundle_b, sort_keys=True, separators=(",", ":")))
            self.assertIn("bundle_version", bundle_a)
            self.assertNotIn("schema_version", bundle_a)
            self.assertNotIn("code-health-base-", encoded_a)


class TestLizardBoundaries(unittest.TestCase):
    def test_parser_handles_captured_headerless_csv(self):
        captured = '4,3,18,1,4,"check@7-10@src/check.js","src/check.js","check","check(x)",7,10\n'
        rows = health.parse_lizard_csv(captured)
        self.assertEqual(rows, [{"path": "src/check.js", "name": "check", "line": 7,
                                 "cyclomatic": 3, "collector": "lizard"}])

    def test_parser_handles_headered_csv_variant(self):
        captured = "nloc,ccn,token,param,length,location\n4,3,18,1,4,check@7-10@src/check.js\n"
        self.assertEqual(health.parse_lizard_csv(captured)[0]["cyclomatic"], 3)

    def test_bad_csv_becomes_visible_collector_error(self):
        source = [health.SourceFile("a.js", "JavaScript", "production", "rule", "x\n", {})]
        completed = subprocess.CompletedProcess([], 0, stdout="bad,row\n", stderr="")
        with patch.object(health.shutil, "which", return_value="/bin/lizard"), \
             patch.object(health.subprocess, "run", return_value=completed):
            result = health.lizard_complexity(Path("."), source, health.LizardResult(True, "1", []))
        self.assertIn("unparsable", result.error)

    def test_lizard_batches_large_file_sets(self):
        source = [health.SourceFile(f"f{i}.js", "JavaScript", "production", "rule", "", {})
                  for i in range(201)]
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(health.shutil, "which", return_value="/bin/lizard"), \
             patch.object(health.subprocess, "run", return_value=completed) as run:
            result = health.lizard_complexity(Path("."), source, health.LizardResult(True, "1", []))
        self.assertIsNone(result.error)
        self.assertEqual(run.call_count, 2)

    def test_install_is_dry_run_without_yes(self):
        old_status = health.lizard_status
        old_managers = health.package_managers
        health.lizard_status = lambda: health.LizardResult(False, None, [], "missing")
        health.package_managers = lambda: ["pip"]
        out = StringIO()
        try:
            with redirect_stdout(out):
                result = health.install_lizard(health.parser().parse_args(["install"]))
        finally:
            health.lizard_status = old_status
            health.package_managers = old_managers
        self.assertEqual(result, 0)
        self.assertIn("dry run", out.getvalue())
        self.assertIn("pip install --user lizard", out.getvalue())


if __name__ == "__main__":
    unittest.main()

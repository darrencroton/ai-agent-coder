"""Boundary tests for the portable code-health evidence collector."""

from __future__ import annotations

import json
import os
import random
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


def churn_facts_in(repo, maximum=10):
    return health.churn_facts(Path(repo.path), maximum)


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

    def bundle(self, repo, *args):
        """Build a bundle with *repo* as the working directory."""
        old = os.getcwd()
        os.chdir(repo.path)
        try:
            return health.build_bundle(health.parser().parse_args(list(args)))
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
            bundle, _ = self.bundle(repo, "analyze", "--all")
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
            bundle, _ = self.bundle(repo, "analyze", "--all")
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
            bundle, _ = self.bundle(repo, "analyze", "--all")
            self.assertEqual(bundle["facts"]["unreadable_files"][0]["path"], "data.json")

    def test_binary_data_asset_is_not_recorded_unreadable(self):
        # A font, image, or other `data`-classified path is expected to be binary;
        # failing to decode it as UTF-8 is not a defect worth surfacing alongside
        # genuinely unreadable source, and it stays absent from totals_by_category
        # exactly as an unreadable file always has.
        with Repo() as repo:
            path = Path(repo.path, "glyph.woff2")
            path.write_bytes(b"\x00\x01\x02\xff\xfe")
            repo.write("app.py", "x = 1\n")
            repo.commit()
            bundle, _ = self.bundle(repo, "analyze", "--all")
            self.assertNotIn("glyph.woff2", {row["path"] for row in bundle["facts"]["unreadable_files"]})
            self.assertNotIn("glyph.woff2", {row["path"] for row in bundle["facts"]["composition"]["files"]})

    def test_binary_data_asset_does_not_shadow_coverage_matrix(self):
        # composition's unreadable list is disjoint from scan's; the coverage
        # matrix is derived from scan's list only, so a binary data asset must not
        # move any coverage cell regardless of how it is disclosed.
        with Repo() as repo:
            Path(repo.path, "glyph.woff2").write_bytes(b"\x00\x01\x02\xff\xfe")
            repo.write("app.py", "x = 1\n")
            repo.commit()
            bundle, _ = self.bundle(repo, "analyze", "--all")
            self.assertTrue(all(row["status"] != "unavailable" for row in bundle["coverage"]
                                 if row["language"] == "Python"))


class TestPythonFacts(unittest.TestCase):
    def test_ast_complexity_and_cycle_are_raw_measured_facts(self):
        source = [
            health.SourceFile("a.py", "Python", "production", "rule", "import b\n\ndef f(x):\n if x and x > 1:\n  return 1\n return 0\n", {}),
            health.SourceFile("b.py", "Python", "production", "rule", "import a\n", {}),
        ]
        facts = health.python_structure(source)
        self.assertEqual(facts["functions"][0]["cyclomatic"], 3)
        self.assertEqual(facts["imports"]["cycles"], [["a.py", "b.py"]])
        self.assertEqual(facts["imports"]["nodes"][0]["reverse_reachability"], 1)

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

    def test_overlapping_duplicate_windows_collapse_to_one_block(self):
        """A repeated 10-line block is one finding, not the eight overlapping
        windows a sliding scan sees; the row reports the whole block's length."""
        block = "".join(f"line{i} = {i}\n" for i in range(10))
        source = [health.SourceFile("a.py", "Python", "production", "rule",
                                    block + "separator = 0\n" + block, {})]
        rows = health.duplication_facts(source, 3, [], {"a.py"})["signatures"]
        self.assertEqual([(r["current_count"], r["line_count"]) for r in rows], [(2, 10)])
        self.assertEqual([o["start_line"] for o in rows[0]["current_occurrences"]], [1, 12])

    def test_a_more_repeated_tail_is_not_absorbed_into_a_longer_block(self):
        """`p q r` twice and `q r` a third time. Merging keys on the whole
        occurrence set, so the count-3 run keeps its own row instead of being
        swallowed by the count-2 run and losing an occurrence."""
        source = [health.SourceFile("a.py", "Python", "production", "rule",
                                    "p = 1\nq = 2\nr = 3\nz = 9\np = 1\nq = 2\nr = 3\ny = 8\nq = 2\nr = 3\n", {})]
        rows = health.duplication_facts(source, 2, [], set())["signatures"]
        self.assertEqual(sorted((r["current_count"], r["line_count"]) for r in rows), [(2, 2), (3, 2)])

    def test_a_run_member_reports_the_extent_that_repeats_at_its_own_lines(self):
        """`ab` occurs three times in both; `bc` rises 2 -> 3 and so becomes a
        continuation of `ab`. It must keep its occurrences or the new
        relationship yields no candidate, and its line count must describe what
        repeats at ITS lines -- the whole run's length is anchored a line
        earlier, so claiming it here would cite a block that is not duplicated."""
        a, b, c = "aa = 1\n", "bb = 2\n", "cc = 3\n"
        base = [health.SourceFile("f.py", "Python", "production", "rule",
                                  a + b + c + "x=0\n" + a + b + c + "y=0\n" + a + b + "z=0\n", {})]
        current = [health.SourceFile("f.py", "Python", "production", "rule",
                                     a + b + c + "x=0\n" + a + b + c + "y=0\n" + a + b + c + "z=0\n", {})]
        facts = health.duplication_facts(current, 2, base, {"f.py"})
        risen = next(r for r in facts["signatures"] if r["count_delta"] > 0)
        self.assertEqual((risen["current_count"], risen["base_count"], risen["line_count"]), (3, 2, 2))
        self.assertEqual([o["start_line"] for o in risen["current_occurrences"]], [2, 6, 10])
        diff = health.Differential(active=True, changed=frozenset({"f.py"}), source=tuple(base),
                                   structure={"functions": [], "imports": {"cycles": []}})
        rows = health.candidates([], {"functions": [], "imports": {"cycles": []}}, {}, [], facts, diff)
        # Both are true: the 3-line run grew from repeating twice to three
        # times (reported at its head), and its 2-line tail rose 2 -> 3.
        self.assertEqual([(r["line"], r["line_count"]) for r in rows if r["kind"] == "duplication"],
                         [(1, 3), (2, 2)])
        # The other direction: the base holds the longer run, so a current row
        # must not borrow a length the current tree does not have.
        base = [health.SourceFile("g.py", "Python", "production", "rule",
                                  a + b + c + "x=0\n" + a + b + c + "\n", {})]
        current = [health.SourceFile("g.py", "Python", "production", "rule",
                                     a + b + "p=0\n" + a + b + "q=0\n" + a + b + "r=0\n", {})]
        rows = health.duplication_facts(current, 2, base, {"g.py"})["signatures"]
        self.assertEqual([(r["current_count"], r["line_count"]) for r in rows], [(3, 2)])

    def test_a_clone_that_grows_without_repeating_more_is_still_a_candidate(self):
        """Merging runs made extent a real measured value, so extent has to
        enter the differential too. A block that doubles from 6 to 12 lines
        while still occurring twice has a changed raw value; keying only on
        occurrence count would report nothing and read as clean."""
        head = "".join(f"h{i} = {i}\n" for i in range(6))
        tail = "".join(f"t{i} = {i}\n" for i in range(6))
        base = [health.SourceFile("a.py", "Python", "production", "rule",
                                  head + "sep = 0\n" + head, {})]
        current = [health.SourceFile("a.py", "Python", "production", "rule",
                                     head + tail + "sep = 0\n" + head + tail, {})]
        facts = health.duplication_facts(current, 6, base, {"a.py"})
        row = facts["signatures"][0]
        self.assertEqual((row["current_count"], row["base_count"], row["count_delta"]), (2, 2, 0))
        self.assertEqual((row["line_count"], row["base_line_count"]), (12, 6))
        diff = health.Differential(active=True, changed=frozenset({"a.py"}), source=tuple(base),
                                   structure={"functions": [], "imports": {"cycles": []}})
        rows = health.candidates([], {"functions": [], "imports": {"cycles": []}}, {}, [], facts, diff)
        self.assertEqual([(r["line_count"], r["base_line_count"])
                          for r in rows if r["kind"] == "duplication"], [(12, 6)])

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

    def test_churn_counts_revisions_and_lines_under_literal_paths(self):
        """One `git log --numstat`. Rename detection is off, so a renamed file
        keeps two literal path keys rather than one `old => new` arrow that no
        other fact in the bundle could be joined against."""
        with Repo() as repo:
            repo.write("src/alpha.py", "one = 1\n")
            repo.commit()
            repo.write("src/alpha.py", "one = 1\ntwo = 2\nthree = 3\n")
            repo.commit("grow")
            sh(["git", "mv", "src/alpha.py", "src/beta.py"], repo.path)
            repo.commit("rename")
            facts = churn_facts_in(repo)
            rows = {row["path"]: row for row in facts["churn"]}
            self.assertEqual(facts["commits_considered"], 3)
            self.assertFalse(any("=>" in path for path in rows))
            self.assertEqual(rows["src/alpha.py"],
                             {"path": "src/alpha.py", "revisions": 3, "additions": 3, "deletions": 3})

    def test_churn_is_refused_on_a_shallow_clone(self):
        """Truncated history must read as unavailable, never as low churn, and
        `--require-coverage` must be able to stop on it."""
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.commit()
            original = health.git
            def fake(root, *args, **kwargs):
                if args == ("rev-parse", "--is-shallow-repository"):
                    return "true\n"
                return original(root, *args, **kwargs)
            health.git = fake
            try:
                self.assertEqual(churn_facts_in(repo)["reason"], "shallow_repository")
                self.assertEqual(self.run_health(repo, "analyze", "--all", "--history",
                                                 "--require-coverage", "--json"), 3)
            finally:
                health.git = original

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
        diff = health.Differential(active=True, changed=frozenset({"renamed.py", "changed.py"}),
                                   renames={"renamed.py": "old.py"}, source=tuple(base),
                                   structure=old_structure)
        rows = health.candidates(current, structure, {}, [], {"signatures": []}, diff)
        self.assertEqual({(row["kind"], row["path"]) for row in rows},
                         {("cyclomatic", "changed.py"), ("file_size", "changed.py")})

    def test_candidates_are_bounded_per_family(self):
        functions = [{"path": f"f{i}.py", "name": "f", "line": 1, "cyclomatic": i + 2}
                     for i in range(7)]
        structure = {"functions": functions, "imports": {"cycles": []}}
        diff = health.Differential(active=True, changed=frozenset(row["path"] for row in functions),
                                   structure={"functions": [], "imports": {"cycles": []}})
        rows = health.candidates([], structure, {}, [], {"signatures": []}, diff)
        self.assertEqual(sum(row["kind"] == "cyclomatic" for row in rows), health.TOP_PER_FAMILY)

    def test_json_is_deterministic_and_has_bundle_version_not_schema_version(self):
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.commit()
            bundle_a, _ = self.bundle(repo, "analyze", "--all")
            bundle_b, _ = self.bundle(repo, "analyze", "--all")
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


def c_file(path, text):
    return health.SourceFile(path, health.language_for(path), "production", "rule", text, {})


class TestCIncludeDependencies(unittest.TestCase):
    def test_quoted_includes_build_edges_and_angle_includes_do_not(self):
        source = [
            c_file("src/a.c", '#include "b.h"\n#include <stdio.h>\n'),
            c_file("src/b.h", "int b(void);\n"),
        ]
        facts = health.c_include_structure(source)
        self.assertEqual([(e["from"], e["to"]) for e in facts["edges"]], [("src/a.c", "src/b.h")])

    def test_include_cycle_is_measured(self):
        source = [
            c_file("src/b.h", '#include "c.h"\n'),
            c_file("src/c.h", '#include "b.h"\n'),
        ]
        self.assertEqual(health.c_include_structure(source)["cycles"], [["src/b.h", "src/c.h"]])

    def test_guarded_include_still_counts_as_an_edge(self):
        """No preprocessor evaluation: a conditional include is raw evidence."""
        source = [c_file("a.c", '#ifdef WANT\n#  include "b.h"\n#endif\n'), c_file("b.h", "\n")]
        self.assertEqual([(e["from"], e["to"]) for e in health.c_include_structure(source)["edges"]],
                         [("a.c", "b.h")])

    def test_unmeasured_same_named_file_makes_the_basename_ambiguous(self):
        """An unmeasured twin must block the guess, not be invisible to it."""
        source = [c_file("src/a.c", '#include "missing/foo.h"\n'), c_file("other/foo.h", "\n")]
        facts = health.c_include_structure(source, ["src/a.c", "other/foo.h", "alias/foo.h"])
        self.assertEqual(facts["edges"], [])
        self.assertEqual(facts["unresolved_includes"][0]["reason"], "ambiguous_basename")

    def test_ambiguous_basename_yields_no_edge(self):
        source = [
            c_file("a.c", '#include "util.h"\n'),
            c_file("one/util.h", "\n"),
            c_file("two/util.h", "\n"),
        ]
        facts = health.c_include_structure(source)
        self.assertEqual(facts["edges"], [])
        self.assertEqual(facts["unresolved_includes"][0]["reason"], "ambiguous_basename")

    def test_external_header_is_recorded_as_unresolved_not_an_edge(self):
        source = [c_file("a.c", '#include "third_party/zlib.h"\n')]
        facts = health.c_include_structure(source)
        self.assertEqual(facts["edges"], [])
        self.assertEqual(facts["unresolved_includes"][0]["reason"], "not_in_repository")

    def test_c_dependencies_are_covered(self):
        source = [c_file("a.c", '#include "b.h"\n'), c_file("b.h", "\n")]
        coverage = health.coverage_matrix(
            source, [], health.python_structure(source), health.LizardResult(False, None, [], "missing"),
        )
        rows = {(r["language"], r["metric_family"]): r["status"] for r in coverage}
        self.assertEqual(rows[("C", "dependencies")], "covered")
        self.assertEqual(rows[("C", "cyclomatic")], "unavailable")

    def test_unreadable_c_family_file_voids_every_c_dependency_row(self):
        """One include graph spans the family, so one unreadable member voids it."""
        source = [c_file("src/a.c", '#include "b.h"\n')]
        coverage = health.coverage_matrix(
            source, [{"path": "src/b.h", "reason": "unreadable_utf8"}],
            health.python_structure(source), health.LizardResult(False, None, [], "missing"),
        )
        rows = {(r["language"], r["metric_family"]): r["status"]
                for r in coverage if r["metric_family"] == "dependencies"}
        self.assertTrue(all(status == "unavailable" for status in rows.values()), rows)

    def test_commented_out_include_is_not_an_edge(self):
        for text in ('/*\n#include "b.h"\n*/\n', '// #include "b.h"\n'):
            source = [c_file("a.c", text), c_file("b.h", "\n")]
            self.assertEqual(health.c_include_structure(source)["edges"], [], text)

    def test_raw_strings_are_masked_in_cxx_but_absent_from_c(self):
        for text in ('const char *s = R"(\n#include "b.h"\n)";\n',
                     'const char *s = R""(\n#include "b.h"\n)"";\n',
                     'const char *s = u8R"x(\n#include "b.h"\n)x";\n'):
            source = [c_file("a.cpp", text), c_file("b.h", "\n")]
            self.assertEqual(health.c_include_structure(source)["edges"], [], text)
        # C has no raw strings, so the same bytes are ordinary code there.
        source = [c_file("a.c", 'R"(\n#include "b.h"\n)"\n'), c_file("b.h", "\n")]
        self.assertEqual(len(health.c_include_structure(source)["edges"]), 1)

    def test_identifier_ending_in_R_is_not_a_raw_string_opener(self):
        """`TAG_ERROR"(` is string concatenation; treating it as a raw string
        silently deleted every include below it."""
        text = ('#include "log.h"\n'
                'void g(void){ fprintf(stderr, TAG_ERROR"(%d", rc); }\n'
                '#include "extra.h"\n'
                'static const char *p = "a)";\n')
        source = [c_file("src/log.c", text), c_file("src/log.h", "\n"), c_file("src/extra.h", "\n")]
        self.assertEqual(sorted(e["to"] for e in health.c_include_structure(source)["edges"]),
                         ["src/extra.h", "src/log.h"])

    def test_unterminated_raw_opener_does_not_blank_the_rest_of_the_file(self):
        source = [c_file("a.cpp", 'auto s = R"zz(never closed\n#include "b.h"\n'), c_file("b.h", "\n")]
        self.assertEqual(len(health.c_include_structure(source)["edges"]), 1)

    def test_odd_apostrophe_does_not_hide_a_following_comment(self):
        text = ('#include "tuning.hpp"\n'
                "constexpr int k = 1'000;   /* retired\n"
                '#include "legacy.hpp"\n*/\n')
        source = [c_file("src/t.cpp", text), c_file("src/tuning.hpp", "\n"), c_file("src/legacy.hpp", "\n")]
        self.assertEqual([e["to"] for e in health.c_include_structure(source)["edges"]], ["src/tuning.hpp"])

    def test_line_comment_continued_by_a_backslash_still_masks(self):
        source = [c_file("a.c", '// disabled \\\n#include "b.h"\n'), c_file("b.h", "\n")]
        self.assertEqual(health.c_include_structure(source)["edges"], [])

    def test_exact_target_present_but_unmeasured_beats_a_basename_guess(self):
        source = [c_file("src/a.c", '#include "include/foo.h"\n'), c_file("other/foo.h", "\n")]
        facts = health.c_include_structure(source, ["src/a.c", "include/foo.h", "other/foo.h"])
        self.assertEqual(facts["edges"], [])
        self.assertEqual(facts["unresolved_includes"][0]["reason"], "in_repository_unmeasured_language")

    def test_byte_order_mark_does_not_hide_the_first_include(self):
        source = [c_file("a.c", '﻿#include "b.h"\n'), c_file("b.h", "\n")]
        self.assertEqual([(e["from"], e["to"]) for e in health.c_include_structure(source)["edges"]],
                         [("a.c", "b.h")])

    def test_paths_escaping_the_repository_never_reach_the_basename_fallback(self):
        for target in ("/opt/ext/util.h", "C:/ext/util.h", "../../util.h", "..\\..\\util.h"):
            source = [c_file("src/a.c", f'#include "{target}"\n'), c_file("src/util.h", "\n")]
            facts = health.c_include_structure(source)
            self.assertEqual(facts["edges"], [], target)
            self.assertEqual(facts["unresolved_includes"][0]["reason"], "outside_repository", target)

    def test_parent_relative_include_inside_the_repository_resolves(self):
        """`../include/b.h` from src/ is ordinary C layout, not an escape."""
        for target in ("../include/b.h", "..\\include\\b.h"):
            source = [c_file("src/a.c", f'#include "{target}"\n'), c_file("include/b.h", "\n")]
            self.assertEqual([(e["from"], e["to"]) for e in health.c_include_structure(source)["edges"]],
                             [("src/a.c", "include/b.h")], target)

    def test_unresolved_includes_carry_a_line_and_are_deduplicated(self):
        source = [c_file("a.c", '#include "missing.h"\n\n#include "missing.h"\n')]
        rows = health.c_include_structure(source)["unresolved_includes"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["line"], 1)

    def test_string_literals_are_not_mistaken_for_comments(self):
        """Masking must not erase code: a literal is skipped, not blanked."""
        source = [c_file("a.c", '#include "dir//b.h"\n'), c_file("dir/b.h", "\n")]
        self.assertEqual([(e["from"], e["to"]) for e in health.c_include_structure(source)["edges"]],
                         [("a.c", "dir/b.h")])
        source = [c_file("a.c", 'char *s = "/*";\n#include "b.h"\nchar *t = "*/";\n'), c_file("b.h", "\n")]
        self.assertEqual([(e["from"], e["to"]) for e in health.c_include_structure(source)["edges"]],
                         [("a.c", "b.h")])

    def test_unterminated_quote_does_not_swallow_later_includes(self):
        source = [c_file("a.c", "char *bad = 'x;\n#include \"b.h\"\n"), c_file("b.h", "\n")]
        self.assertEqual(len(health.c_include_structure(source)["edges"]), 1)

    def test_present_but_unmeasured_header_is_not_reported_absent(self):
        source = [c_file("a.c", '#include "body.inc"\n')]
        rows = health.c_include_structure(source, ["a.c", "body.inc"])["unresolved_includes"]
        self.assertEqual(rows[0]["reason"], "in_repository_unmeasured_language")

    def test_edges_record_how_they_were_resolved(self):
        source = [c_file("src/a.c", '#include "b.h"\n#include "far.h"\n'),
                  c_file("src/b.h", "\n"), c_file("elsewhere/far.h", "\n")]
        facts = health.c_include_structure(source)
        self.assertEqual({(e["from"], e["to"]): e["via"] for e in facts["edges"]},
                         {("src/a.c", "src/b.h"): "exact_relative",
                          ("src/a.c", "elsewhere/far.h"): "unique_basename"})
        # Guessed edges are counted, not listed: on a repository built with -I
        # paths almost every edge is a guess and the list buried the ratio.
        self.assertEqual(facts["resolution_counts"], {"exact_relative": 1, "unique_basename": 1})

    def test_common_cxx_header_extensions_are_measured(self):
        source = [c_file("src/foo.cc", '#include "foo.hh"\n'), c_file("src/foo.hh", "\n")]
        self.assertEqual([(e["from"], e["to"]) for e in health.c_include_structure(source)["edges"]],
                         [("src/foo.cc", "src/foo.hh")])


class TestGraphFactsExtraction(unittest.TestCase):
    """The extraction is a silent-regression risk, so pin the actual output."""

    def python_graph(self, files):
        source = [health.SourceFile(p, "Python", "production", "rule", t, {}) for p, t in files]
        return health.python_structure(source)["imports"]

    def test_python_import_graph_output_is_exact(self):
        graph = self.python_graph([
            ("pkg/a.py", "import pkg.b\n"),
            ("pkg/b.py", "import pkg.c\n"),
            ("pkg/c.py", "import pkg.a\n"),
            ("pkg/lonely.py", "x = 1\n"),
        ])
        self.assertEqual([(e["from"], e["to"]) for e in graph["edges"]],
                         [("pkg/a.py", "pkg/b.py"), ("pkg/b.py", "pkg/c.py"), ("pkg/c.py", "pkg/a.py")])
        self.assertEqual(graph["cycles"], [["pkg/a.py", "pkg/b.py", "pkg/c.py"]])
        self.assertEqual(graph["nodes"], [
            {"path": "pkg/a.py", "fan_in": 1, "fan_out": 1, "reverse_reachability": 2},
            {"path": "pkg/b.py", "fan_in": 1, "fan_out": 1, "reverse_reachability": 2},
            {"path": "pkg/c.py", "fan_in": 1, "fan_out": 1, "reverse_reachability": 2},
            {"path": "pkg/lonely.py", "fan_in": 0, "fan_out": 0, "reverse_reachability": 0},
        ])

    def test_isolated_node_keeps_a_node_row(self):
        graph = self.python_graph([("solo.py", "x = 1\n")])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["nodes"],
                         [{"path": "solo.py", "fan_in": 0, "fan_out": 0, "reverse_reachability": 0}])

    @staticmethod
    def naive_reverse_reachability(graph):
        """Distinct other nodes that reach each node, by explicit traversal."""
        expected = {}
        for node in graph:
            seen, queue = {node}, [n for n, ds in graph.items() if node in ds]
            while queue:
                item = queue.pop()
                if item not in seen:
                    seen.add(item)
                    queue.extend(n for n, ds in graph.items() if item in ds)
            expected[node] = len(seen) - 1
        return expected

    def measured_reverse_reachability(self, graph):
        edges = {(src, dst) for src, targets in graph.items() for dst in targets}
        return health.reverse_reachability(health.strongly_connected_components(graph), edges)

    def test_reverse_reachability_matches_a_per_node_traversal(self):
        """The condensation replaced a BFS per node; results must be identical."""
        graph = {"a": ["b"], "b": ["c", "a"], "c": ["d"], "d": [], "e": ["d"]}
        self.assertEqual(self.measured_reverse_reachability(graph),
                         self.naive_reverse_reachability(graph))

    def test_reverse_reachability_matches_traversal_on_random_graphs(self):
        """Cycles, shared ancestors and isolated nodes at once; a seeded sweep
        is the only practical check on the condensation's bookkeeping."""
        rng = random.Random(20260808)
        for _ in range(200):
            nodes = [f"n{i}" for i in range(rng.randint(1, 12))]
            graph = {node: sorted({other for other in nodes
                                   if other != node and rng.random() < 0.25}) for node in nodes}
            self.assertEqual(self.measured_reverse_reachability(graph),
                             self.naive_reverse_reachability(graph), graph)

    def test_scc_order_lets_ancestors_be_visited_first(self):
        """reverse_reachability walks the condensation backwards, so Tarjan must
        emit every component before the components that reach it."""
        graph = {"a": ["b"], "b": ["c"], "c": [], "d": ["b"]}
        order = {component[0]: index
                 for index, component in enumerate(health.strongly_connected_components(graph))}
        self.assertLess(order["c"], order["b"])
        self.assertLess(order["b"], order["a"])
        self.assertLess(order["b"], order["d"])

    def test_scc_is_iterative_and_survives_a_deep_chain(self):
        depth = 2000
        graph = {f"h{i}": [f"h{i + 1}"] for i in range(depth)}
        graph[f"h{depth}"] = []
        self.assertEqual(len(health.strongly_connected_components(graph)), depth + 1)


class TestCDifferentialWiring(unittest.TestCase):
    def candidates_for(self, current, base, changed):
        diff = health.Differential(active=True, changed=frozenset(changed), source=tuple(base),
                                   structure=health.python_structure(base),
                                   includes=health.c_include_structure(base))
        return [row for row in health.candidates(
            current, health.python_structure(current), health.c_include_structure(current),
            [], {"signatures": []}, diff,
        ) if row["kind"] == "dependency_cycle"]

    def test_new_include_cycle_surfaces_and_pre_existing_one_does_not(self):
        cyclic = [c_file("b.h", '#include "c.h"\n'), c_file("c.h", '#include "b.h"\n')]
        acyclic = [c_file("b.h", "\n"), c_file("c.h", '#include "b.h"\n')]
        self.assertEqual([r["members"] for r in self.candidates_for(cyclic, acyclic, {"b.h"})],
                         [["b.h", "c.h"]])
        self.assertEqual(self.candidates_for(cyclic, cyclic, {"b.h"}), [])

    def test_shrinking_a_cycle_is_not_a_new_relationship(self):
        """Deleting c.h leaves the untouched a.h<->b.h cycle; nothing is new."""
        base = [c_file("a.h", '#include "b.h"\n'),
                c_file("b.h", '#include "a.h"\n#include "c.h"\n'),
                c_file("c.h", '#include "b.h"\n')]
        current = [c_file("a.h", '#include "b.h"\n'), c_file("b.h", '#include "a.h"\n')]
        self.assertEqual(self.candidates_for(current, base, {"c.h", "b.h"}), [])

    def test_cycle_rewired_inside_a_former_cycle_is_new(self):
        """Subset of a base cycle, but b.h->a.h is a brand-new edge."""
        base = [c_file("a.h", '#include "b.h"\n'), c_file("b.h", '#include "c.h"\n'),
                c_file("c.h", '#include "a.h"\n')]
        current = [c_file("a.h", '#include "b.h"\n'), c_file("b.h", '#include "a.h"\n'), c_file("c.h", "\n")]
        self.assertEqual([r["members"] for r in self.candidates_for(current, base, {"b.h"})], [["a.h", "b.h"]])

    def test_cycle_created_by_a_resolution_change_is_new(self):
        """Deleting the file that made a basename ambiguous completes a cycle."""
        base = [c_file("src/a.h", '#include "b.h"\n'), c_file("x/b.h", '#include "a.h"\n'), c_file("y/b.h", "\n")]
        current = base[:2]
        self.assertEqual([r["members"] for r in self.candidates_for(current, base, {"y/b.h"})],
                         [["src/a.h", "x/b.h"]])


class TestLanguageRecognition(unittest.TestCase):
    def test_fortran_and_make_are_recognised_rather_than_other(self):
        for path, language in (("sim.f90", "Fortran"), ("SIM.F90", "Fortran"), ("old.f", "Fortran"),
                               ("Makefile", "Make"), ("makefile", "Make"), ("rules.mk", "Make")):
            self.assertEqual(health.language_for(path), language, path)
            self.assertEqual(health.category_for(path, language)[0], "production", path)

    def test_unrecognised_language_would_be_invisible_in_coverage(self):
        """Guards the reason Fortran was added: no row means no 'unavailable'."""
        source = [c_file("sim.f90", "program p\nend program\n")]
        coverage = health.coverage_matrix(
            source, [], health.python_structure(source), health.LizardResult(False, None, [], "missing"),
        )
        families = {r["metric_family"] for r in coverage if r["language"] == "Fortran"}
        self.assertEqual(families, set(health.METRIC_FAMILIES))

    def test_comment_styles_are_language_specific(self):
        fortran = health.line_counts("! note\nprogram p\nend program\n", "Fortran")
        make = health.line_counts("# note\nall:\n\tcc a.c\n", "Make")
        self.assertEqual((fortran["comment"], fortran["code"]), (1, 2))
        self.assertEqual((make["comment"], make["code"]), (1, 2))

    def test_fortran_fixed_form_comment_is_not_claimed(self):
        """Documented undercount: fixed-form 'C' in column 1 reads as code."""
        self.assertEqual(health.line_counts("C legacy comment\n", "Fortran")["comment"], 0)

    def test_make_recipe_hash_is_code_not_comment(self):
        counts = health.line_counts("# real\nall:\n\t# shell\n", "Make")
        self.assertEqual((counts["comment"], counts["code"]), (1, 2))


class TestCoverageHonesty(InRepo):
    def test_unreadable_recognised_language_fails_require_coverage(self):
        with Repo() as repo:
            Path(repo.path, "sim.f90").write_bytes(b"\xff\xfe program\n")
            repo.commit()
            self.assertEqual(self.run_health(repo, "analyze", "--all", "--require-coverage", "--json"), 3)

    def test_unreadable_base_file_is_not_a_clean_baseline(self):
        with Repo() as repo:
            repo.write("a.c", '#include "b.h"\n')
            Path(repo.path, "b.h").write_bytes(b"\xff\xfe bad\n")
            base = repo.commit()
            repo.write("b.h", "int ok(void);\n")
            bundle, _ = self.bundle(repo, "analyze", "--base", base)
            rows = {(r["language"], r["metric_family"]): r["status"]
                    for r in bundle["coverage"] if r["metric_family"] == "dependencies"}
            self.assertTrue(all(status == "unavailable" for status in rows.values()), rows)

    def test_repository_metadata_is_not_an_unmeasured_language(self):
        self.assertEqual(health.category_for("LICENSE", None)[0], "documentation")
        self.assertEqual(health.category_for("skills/x/config.jsonc", None)[0], "configuration")

    def test_metadata_stem_does_not_hide_an_unmeasured_source_file(self):
        """`version.awk` is source we cannot measure, not a document."""
        self.assertEqual(health.category_for("version.awk", None)[0], "other")
        self.assertEqual(health.category_for("readme.json", None)[0], "configuration")
        # Data and build products are not unmeasured source either.
        self.assertEqual(health.category_for("img/logo.png", None)[0], "data")

    def test_base_unreadable_file_is_disclosed_and_suppresses_fake_deltas(self):
        with Repo() as repo:
            repo.write("a.c", "int a(int x){ if(x){return 1;} return 0; }\n")
            Path(repo.path, "b.h").write_bytes(b"\xff\xfe bad\n")
            base = repo.commit()
            repo.write("b.h", "int ok(void);\n")
            bundle, _ = self.bundle(repo, "analyze", "--base", base)
            self.assertEqual([row["path"] for row in bundle["facts"]["base_unreadable_files"]], ["b.h"])
            self.assertIn("unreadable_base_files",
                          {limit["kind"] for limit in bundle["repository"]["coverage_limits"]})
            self.assertEqual([c for c in bundle["candidates"] if c["path"] == "b.h"], [])

    def test_symlinked_target_is_named_not_guessed(self):
        with Repo() as repo:
            repo.write("src/a.c", '#include "include/foo.h"\n')
            repo.write("other/foo.h", "int real(void);\n")
            os.makedirs(os.path.join(repo.path, "include"), exist_ok=True)
            os.symlink("../other/foo.h", os.path.join(repo.path, "include", "foo.h"))
            repo.commit()
            bundle, _ = self.bundle(repo, "analyze", "--all")
            facts = bundle["facts"]["c_includes"]
            self.assertEqual(facts["edges"], [])
            self.assertEqual(facts["unresolved_includes"][0]["reason"], "in_repository_unmeasured_language")

    def test_unrecognised_file_is_disclosed_but_does_not_fail_coverage(self):
        """Whether an unclassified file is source is a judgement, so it is
        disclosed rather than gated; blocking made ordinary repositories fail
        over a `.babelrc`."""
        with Repo() as repo:
            repo.write("model.zzz", "blob\n")
            repo.write("a.py", "x = 1\n")
            repo.commit()
            self.assertEqual(self.run_health(repo, "detect", "--require-coverage", "--json"), 0)
            bundle, _ = self.bundle(repo, "detect")
            # Grouped by extension with a count: the one field is the whole
            # disclosure, so a reader has a single place to look.
            self.assertEqual(bundle["repository"]["unrecognised_extensions"], {".zzz": 1})

    def test_ordinary_config_dotfiles_do_not_fail_coverage(self):
        for name in (".babelrc", ".stylelintrc", ".browserslistrc", ".prettierignore",
                     ".eslintignore", ".watchmanconfig", ".gitignore"):
            self.assertEqual(health.category_for(name, None)[0], "configuration", name)
        # Still unmeasured source, not silently reclassified.
        self.assertEqual(health.category_for(".mystery", None)[0], "other")
        self.assertEqual(health.language_for(".bashrc"), "Shell")

    def test_base_parse_error_suppresses_phantom_cycles(self):
        with Repo() as repo:
            repo.write("a.py", "import b\n\ndef f(:\n    pass\n")
            repo.write("b.py", "import a\n\ndef g():\n    pass\n")
            base = repo.commit()
            repo.write("a.py", "import b\n\ndef f():\n    pass\n")
            bundle, _ = self.bundle(repo, "analyze", "--base", base)
            self.assertEqual([c for c in bundle["candidates"] if c["kind"] == "dependency_cycle"], [])
            self.assertIn("base_parse_errors",
                          {limit["kind"] for limit in bundle["repository"]["coverage_limits"]})

    def test_base_parse_error_voids_only_the_families_that_need_a_parse(self):
        """The base text read fine, so composition and duplication are honest;
        only the AST families lose their baseline, and they say why."""
        with Repo() as repo:
            repo.write("a.py", "def f(:\n    pass\n")
            base = repo.commit()
            repo.write("a.py", "def f():\n    pass\n")
            bundle, exit_three = self.bundle(repo, "analyze", "--base", base, "--require-coverage")
            rows = {r["metric_family"]: (r["status"], r["reason"]) for r in bundle["coverage"]}
            self.assertEqual(rows["composition"][0], "covered")
            self.assertEqual(rows["duplication"][0], "covered")
            self.assertEqual(rows["cyclomatic"], ("unavailable", "base source in this language failed to parse"))
            self.assertEqual(rows["dependencies"][0], "unavailable")
            # The voided AST families are still required coverage for a
            # language in scope, so the same run fails --require-coverage.
            self.assertTrue(exit_three)

    def test_base_only_failure_invents_no_row_for_an_absent_language(self):
        """A base-only unreadable .rb must not produce Ruby coverage rows, nor
        demand Ruby coverage, in a repository that no longer contains Ruby."""
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            Path(repo.path, "legacy.rb").write_bytes(b"\xff\xfe bad\n")
            base = repo.commit()
            os.unlink(os.path.join(repo.path, "legacy.rb"))
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, exit_three = health.build_bundle(
                    health.parser().parse_args(["analyze", "--base", base, "--require-coverage"]))
            finally:
                os.chdir(old)
            self.assertEqual({row["language"] for row in bundle["coverage"]}, {"Python"})
            self.assertFalse(exit_three)
            # Still disclosed as an incomplete baseline, just not as a gate.
            self.assertEqual([row["path"] for row in bundle["facts"]["base_unreadable_files"]], ["legacy.rb"])

    def test_unreadable_base_file_voids_duplication_in_every_language(self):
        """Duplication signatures span all languages at once, so a base file
        nobody can read understates a base count anywhere. Here the same six
        lines sat in an unreadable `legacy.rb`, so Python's count reads 1 -> 2
        when the truth is 2 -> 2: the comparison must report unavailable and
        fail --require-coverage rather than emit a phantom rise."""
        with Repo() as repo:
            block = "".join(f"shared{i} = {i}\n" for i in range(6))
            repo.write("a.py", block)
            Path(repo.path, "legacy.rb").write_bytes(block.encode() + b"\xff\xfe\n")
            base = repo.commit()
            os.unlink(os.path.join(repo.path, "legacy.rb"))
            repo.write("a.py", block + "spacer = 1\n" + block)
            bundle, exit_three = self.bundle(
                repo, "analyze", "--base", base, "--require-coverage", "--duplicate-lines", "6")
            rows = {(r["language"], r["metric_family"]): r["status"] for r in bundle["coverage"]}
            self.assertEqual(rows[("Python", "duplication")], "unavailable")
            self.assertTrue(exit_three)
            self.assertEqual([c for c in bundle["candidates"] if c["kind"] == "duplication"], [])
            # Still no invented row for a language the current tree lacks.
            self.assertEqual({language for language, _ in rows}, {"Python"})

    def test_an_unchanged_unreadable_file_does_not_void_duplication(self):
        """The rule above must fire only when the two pools can differ. A file
        that is unreadable and never changes is missing from both sides, so
        blocking on it would permanently fail --require-coverage on the routine
        shape of an encoding fixture or a legacy Latin-1 source file."""
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            Path(repo.path, "fixtures/bad.rb").parent.mkdir(parents=True, exist_ok=True)
            Path(repo.path, "fixtures/bad.rb").write_bytes(b"\xff\xfe bad\n")
            base = repo.commit()
            repo.write("a.py", "x = 2\n")
            bundle, exit_three = self.bundle(repo, "analyze", "--base", base, "--require-coverage")
            rows = {(r["language"], r["metric_family"]): r["status"] for r in bundle["coverage"]}
            self.assertEqual(rows[("Python", "duplication")], "covered")
            self.assertFalse(exit_three)

    def test_a_renamed_unreadable_base_file_is_matched_by_its_base_name(self):
        """The unreadable list is base-side and the changed set is current-side,
        so a rename must be resolved to base identity before they are compared.
        Here `legacy.rb` becomes a readable `renamed.py` holding the same block:
        the base pool is short of that text while the current pool has it, which
        is exactly the asymmetry that fabricates a rise."""
        with Repo() as repo:
            block = "".join(f"row{i} = {i}\n" for i in range(20))
            repo.write("common.py", block)
            Path(repo.path, "legacy.rb").write_bytes(block.encode() + b"\xff")
            base = repo.commit()
            sh(["git", "mv", "legacy.rb", "renamed.py"], repo.path)
            repo.write("renamed.py", block)
            bundle, exit_three = self.bundle(
                repo, "analyze", "--base", base, "--require-coverage", "--duplicate-lines", "6")
            self.assertEqual(bundle["invocation"]["rename_map"], {"renamed.py": "legacy.rb"})
            rows = {(r["language"], r["metric_family"]): r["status"] for r in bundle["coverage"]}
            self.assertEqual(rows[("Python", "duplication")], "unavailable")
            self.assertTrue(exit_three)
            self.assertEqual([c for c in bundle["candidates"] if c["kind"] == "duplication"], [])

    def test_a_base_parse_error_still_allows_a_file_size_delta(self):
        """A parse failure voids only the families that need an AST. The base
        text was read, so its line count is comparable and the growth must
        still reach the reading list."""
        with Repo() as repo:
            repo.write("a.py", "def f(:\n    pass\n")
            base = repo.commit()
            repo.write("a.py", "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(20)))
            bundle, _ = self.bundle(repo, "analyze", "--base", base)
            sizes = [c for c in bundle["candidates"] if c["kind"] == "file_size"]
            self.assertEqual([(c["path"], c["delta_from_base"]) for c in sizes], [("a.py", 19)])

    def test_unrecognised_extensions_describe_the_current_tree(self):
        """A base-only unrecognised path is not evidence about the tree now."""
        with Repo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.write("gone.zzz", "blob\n")
            base = repo.commit()
            os.unlink(os.path.join(repo.path, "gone.zzz"))
            bundle, _ = self.bundle(repo, "analyze", "--base", base)
            self.assertEqual(bundle["repository"]["unrecognised_extensions"], {})

    def test_base_side_resolution_uses_the_same_inventory_as_the_current_tree(self):
        with Repo() as repo:
            repo.write("src/a.h", '#include "include/foo.h"\n')
            repo.write("other/foo.h", '#include "../src/a.h"\n')
            os.makedirs(os.path.join(repo.path, "include"), exist_ok=True)
            os.symlink("../other/foo.h", os.path.join(repo.path, "include", "foo.h"))
            base = repo.commit()
            repo.write("src/a.h", '#include "../other/foo.h"\n')
            bundle, _ = self.bundle(repo, "analyze", "--base", base)
            cycles = [c["members"] for c in bundle["candidates"] if c["kind"] == "dependency_cycle"]
            self.assertEqual(cycles, [["other/foo.h", "src/a.h"]])

    def test_detect_does_not_build_the_include_graph(self):
        with Repo() as repo:
            repo.write("a.c", '#include "b.h"\n')
            repo.write("b.h", "\n")
            repo.commit()
            detected, _ = self.bundle(repo, "detect")
            analyzed, _ = self.bundle(repo, "analyze", "--all")
            self.assertNotIn("c_includes", detected["facts"])
            self.assertEqual(len(analyzed["facts"]["c_includes"]["edges"]), 1)


if __name__ == "__main__":
    unittest.main()

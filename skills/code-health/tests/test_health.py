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
        self.assertEqual(facts["basename_resolved_edges"],
                         [{"from": "src/a.c", "to": "elsewhere/far.h"}])

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
        self.assertEqual({r["path"]: r["value"] for r in graph["fan_in"]},
                         {"pkg/a.py": 1, "pkg/b.py": 1, "pkg/c.py": 1, "pkg/lonely.py": 0})
        self.assertEqual({r["path"]: r["value"] for r in graph["fan_out"]},
                         {"pkg/a.py": 1, "pkg/b.py": 1, "pkg/c.py": 1, "pkg/lonely.py": 0})
        self.assertEqual({r["path"]: r["reverse_reachability"] for r in graph["reverse_reachability"]},
                         {"pkg/a.py": 2, "pkg/b.py": 2, "pkg/c.py": 2, "pkg/lonely.py": 0})

    def test_isolated_node_keeps_a_row_in_every_series(self):
        graph = self.python_graph([("solo.py", "x = 1\n")])
        self.assertEqual(graph["edges"], [])
        for series in ("fan_in", "fan_out", "reverse_reachability"):
            self.assertEqual([r["path"] for r in graph[series]], ["solo.py"], series)

    def test_reverse_reachability_matches_a_per_node_traversal(self):
        """The condensation replaced a BFS per node; results must be identical."""
        graph = {"a": ["b"], "b": ["c", "a"], "c": ["d"], "d": [], "e": ["d"]}
        cycles = [c for c in health.strongly_connected_components(graph) if len(c) > 1]
        expected = {}
        for node in graph:
            seen, queue = {node}, [n for n, ds in graph.items() if node in ds]
            while queue:
                item = queue.pop()
                if item not in seen:
                    seen.add(item)
                    queue.extend(n for n, ds in graph.items() if item in ds)
            expected[node] = len(seen) - 1
        self.assertEqual(health.reverse_reachability(graph, cycles), expected)

    def test_scc_is_iterative_and_survives_a_deep_chain(self):
        depth = 2000
        graph = {f"h{i}": [f"h{i + 1}"] for i in range(depth)}
        graph[f"h{depth}"] = []
        self.assertEqual(len(health.strongly_connected_components(graph)), depth + 1)


class TestCDifferentialWiring(unittest.TestCase):
    def candidates_for(self, current, base, changed):
        return [row for row in health.candidates(
            current, base, health.python_structure(current), [], {"signatures": []}, changed,
            health.python_structure(base), [], {}, True,
            health.c_include_structure(current), health.c_include_structure(base),
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
    def test_unrecognised_extension_is_disclosed_in_detect(self):
        with Repo() as repo:
            repo.write("model.zzz", "data\n")
            repo.commit()
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["detect"]))
            finally:
                os.chdir(old)
            self.assertIn("model.zzz", bundle["repository"]["unrecognised_extensions"])
            self.assertIn("unrecognised_extensions",
                          {limit["kind"] for limit in bundle["repository"]["coverage_limits"]})

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
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["analyze", "--base", base]))
            finally:
                os.chdir(old)
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

    def test_base_unreadable_file_is_disclosed_and_suppresses_fake_deltas(self):
        with Repo() as repo:
            repo.write("a.c", "int a(int x){ if(x){return 1;} return 0; }\n")
            Path(repo.path, "b.h").write_bytes(b"\xff\xfe bad\n")
            base = repo.commit()
            repo.write("b.h", "int ok(void);\n")
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["analyze", "--base", base]))
            finally:
                os.chdir(old)
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
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["analyze", "--all"]))
            finally:
                os.chdir(old)
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
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["detect"]))
            finally:
                os.chdir(old)
            self.assertIn("model.zzz", bundle["repository"]["unrecognised_extensions"])

    def test_ordinary_config_dotfiles_do_not_fail_coverage(self):
        for name in (".babelrc", ".stylelintrc", ".browserslistrc", ".prettierignore",
                     ".eslintignore", ".watchmanconfig", ".gitignore"):
            self.assertEqual(health.category_for(name, None)[0], "configuration", name)
        # Still unmeasured source, not silently reclassified.
        self.assertEqual(health.category_for(".mystery", None)[0], "other")
        self.assertEqual(health.language_for(".bashrc"), "Shell")

    def test_data_assets_are_not_unmeasured_source(self):
        self.assertEqual(health.category_for("img/logo.png", None)[0], "data")
        self.assertEqual(health.category_for("model.zzz", None)[0], "other")

    def test_base_parse_error_suppresses_phantom_cycles(self):
        with Repo() as repo:
            repo.write("a.py", "import b\n\ndef f(:\n    pass\n")
            repo.write("b.py", "import a\n\ndef g():\n    pass\n")
            base = repo.commit()
            repo.write("a.py", "import b\n\ndef f():\n    pass\n")
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["analyze", "--base", base]))
            finally:
                os.chdir(old)
            self.assertEqual([c for c in bundle["candidates"] if c["kind"] == "dependency_cycle"], [])
            self.assertIn("base_parse_errors",
                          {limit["kind"] for limit in bundle["repository"]["coverage_limits"]})

    def test_base_parse_error_fails_require_coverage(self):
        with Repo() as repo:
            repo.write("a.py", "import b\n\ndef f(:\n    pass\n")
            repo.write("b.py", "import a\n")
            base = repo.commit()
            repo.write("a.py", "import b\n\ndef f():\n    pass\n")
            self.assertEqual(self.run_health(repo, "analyze", "--base", base, "--require-coverage", "--json"), 3)

    def test_source_dotfile_is_measured_rather_than_called_configuration(self):
        self.assertEqual(health.language_for(".bashrc"), "Shell")
        self.assertEqual(health.category_for(".gitignore", None)[0], "configuration")
        # An unknown dotfile stays unmeasured source rather than silently
        # becoming configuration with no coverage row.
        self.assertEqual(health.category_for(".mystery", None)[0], "other")

    def test_base_side_resolution_uses_the_same_inventory_as_the_current_tree(self):
        with Repo() as repo:
            repo.write("src/a.h", '#include "include/foo.h"\n')
            repo.write("other/foo.h", '#include "../src/a.h"\n')
            os.makedirs(os.path.join(repo.path, "include"), exist_ok=True)
            os.symlink("../other/foo.h", os.path.join(repo.path, "include", "foo.h"))
            base = repo.commit()
            repo.write("src/a.h", '#include "../other/foo.h"\n')
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                bundle, _ = health.build_bundle(health.parser().parse_args(["analyze", "--base", base]))
            finally:
                os.chdir(old)
            cycles = [c["members"] for c in bundle["candidates"] if c["kind"] == "dependency_cycle"]
            self.assertEqual(cycles, [["other/foo.h", "src/a.h"]])

    def test_detect_does_not_build_the_include_graph(self):
        with Repo() as repo:
            repo.write("a.c", '#include "b.h"\n')
            repo.write("b.h", "\n")
            repo.commit()
            old = os.getcwd()
            os.chdir(repo.path)
            try:
                detected, _ = health.build_bundle(health.parser().parse_args(["detect"]))
                analyzed, _ = health.build_bundle(health.parser().parse_args(["analyze", "--all"]))
            finally:
                os.chdir(old)
            self.assertNotIn("c_includes", detected["facts"])
            self.assertEqual(len(analyzed["facts"]["c_includes"]["edges"]), 1)


if __name__ == "__main__":
    unittest.main()

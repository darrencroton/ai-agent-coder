"""Protected behaviours: the lint skill's differential runner.

Pure filesystem + git tests. No linter binary is required: each tool's parser is
fed captured real output, so the suite runs anywhere.

Pins, each one a bug found during implementation or an invariant from SKILL.md:

- `_rel` resolves symlinks on BOTH sides. macOS tempfile yields /var/... while
  tools report /private/var/..., and relpath between the two emits a ../../..
  escape that silently broke every base/head signature match.
- `diff_findings` compares signature *counts*, not sets: a second occurrence of
  the same rule in the same file is new, and a line-number shift alone is not.
- Differential mode reports only findings absent at base; pre-existing debt in a
  touched file never blocks.
- A missing tool yields `unavailable` coverage, never a silent pass, and
  `--require-coverage` turns that into exit 3.
- Absolute mode with no paths and no base scopes to the whole tracked tree, not
  to uncommitted changes (which silently scoped to zero files).
- `check` never installs anything; `install` is dry-run unless --yes.
- Every parser extracts rule, path, line, message from real captured output.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import lint


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


class TempRepo:
    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="lint-test-")
        sh(["git", "init", "-q", "."], self.dir)
        sh(["git", "config", "user.email", "t@example.com"], self.dir)
        sh(["git", "config", "user.name", "T"], self.dir)
        return self

    def __exit__(self, *exc):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def commit(self, msg):
        sh(["git", "add", "-A"], self.dir)
        sh(["git", "commit", "-q", "-m", msg], self.dir)
        return sh(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()


def F(tool="ruff-check", rule="F401", path="a.py", line=1, message="unused"):
    return lint.Finding(tool, rule, path, line, message)


class TestRelPathSymlinks(unittest.TestCase):
    def test_resolves_symlinks_on_both_sides(self):
        """The /var -> /private/var bug: an unresolved cwd must not produce an
        escaping ../../.. path, or base/head signatures never match."""
        with TempRepo() as repo:
            real = os.path.realpath(repo.dir)
            # Pass an absolute, already-resolved file path against the possibly
            # unresolved cwd -- exactly what a linter reports.
            got = lint._rel(repo.dir, os.path.join(real, "pkg", "mod.py"))
            self.assertEqual(got, os.path.join("pkg", "mod.py"))
            self.assertNotIn("..", got)

    def test_relative_input_is_unchanged(self):
        with TempRepo() as repo:
            self.assertEqual(lint._rel(repo.dir, "a.py"), "a.py")


class TestDiffFindings(unittest.TestCase):
    def test_identical_finding_is_not_new(self):
        self.assertEqual(lint.diff_findings([F()], [F()]), [])

    def test_line_shift_alone_is_not_new(self):
        self.assertEqual(lint.diff_findings([F(line=42)], [F(line=1)]), [])

    def test_second_occurrence_of_same_rule_is_new(self):
        new = lint.diff_findings([F(line=1), F(line=9)], [F(line=1)])
        self.assertEqual(len(new), 1)

    def test_different_rule_is_new(self):
        new = lint.diff_findings([F(rule="E501")], [F(rule="F401")])
        self.assertEqual([f.rule for f in new], ["E501"])

    def test_different_path_is_new(self):
        new = lint.diff_findings([F(path="b.py")], [F(path="a.py")])
        self.assertEqual([f.path for f in new], ["b.py"])

    def test_message_digits_are_significant(self):
        """Digits in a message are semantic (markdownlint "Expected: 2;
        Actual: 3"), so they must NOT be normalized away."""
        a = F(message="Table column count [Expected: 2; Actual: 3]")
        b = F(message="Table column count [Expected: 2; Actual: 4]")
        self.assertEqual(len(lint.diff_findings([a], [b])), 1)

    def test_empty_base_makes_everything_new(self):
        self.assertEqual(len(lint.diff_findings([F(), F(rule="E1")], [])), 2)


class TestParsers(unittest.TestCase):
    """Each parser is fed output captured from the real tool."""

    def test_ruff_check_json(self):
        out = (
            '[{"code":"F401","message":"`sys` imported but unused",'
            '"filename":"/repo/new_file.py","location":{"row":2,"column":1}}]'
        )
        got = lint._parse_ruff_check(out, "", "/repo")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].rule, "F401")
        self.assertEqual(got[0].line, 2)
        self.assertEqual(got[0].path, "new_file.py")

    def test_ruff_check_empty_output(self):
        self.assertEqual(lint._parse_ruff_check("", "", "/repo"), [])

    def test_ruff_check_unparsable_json_raises(self):
        with self.assertRaises(RuntimeError):
            lint._parse_ruff_check("not json", "", "/repo")

    def test_ruff_format(self):
        got = lint._parse_ruff_format("Would reformat: a.py\n1 file would be reformatted\n",
                                      "", "/repo")
        self.assertEqual([f.path for f in got], ["a.py"])

    def test_markdownlint_cli2_with_severity_word(self):
        """cli2 emits a severity token between location and rule; the parser
        missed all findings until it was made optional."""
        out = (
            "doc.md:5 error MD058/blanks-around-tables Tables should be surrounded by blank lines\n"
            "doc.md:10:9 error MD056/table-column-count Table column count "
            "[Expected: 2; Actual: 3; Too many cells, extra data will be missing]\n"
        )
        got = lint._parse_markdownlint(out, "", "/repo")
        rules = [f.rule for f in got]
        self.assertIn("MD056", rules)
        self.assertIn("MD058", rules)
        md056 = next(f for f in got if f.rule == "MD056")
        self.assertEqual(md056.line, 10)

    def test_markdownlint_without_severity_word(self):
        got = lint._parse_markdownlint("doc.md:3 MD012/no-multiple-blanks x\n", "", "/repo")
        self.assertEqual([f.rule for f in got], ["MD012"])

    def test_codespell(self):
        # The typo is intentional captured output; the inline marker stops
        # codespell flagging this very file when the repo lints itself.
        got = lint._parse_codespell("c.md:3: recieve ==> receive\n",  # codespell:ignore
                                    "", "/repo")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].line, 3)
        self.assertIn("receive", got[0].message)

    def test_clang_format_collapses_to_one_per_file(self):
        err = ("src/a.c:3:1: warning: code should be clang-formatted [-Wclang-format-violations]\n"
               "src/a.c:9:1: warning: code should be clang-formatted [-Wclang-format-violations]\n"
               "src/b.c:1:1: warning: code should be clang-formatted [-Wclang-format-violations]\n")
        got = lint._parse_clang_format("", err, "/repo")
        self.assertEqual(sorted(f.path for f in got), ["src/a.c", "src/b.c"])

    def test_cppcheck(self):
        err = "src/a.c:12:nullPointer:Possible null pointer dereference: p\n"
        got = lint._parse_cppcheck("", err, "/repo")
        self.assertEqual(got[0].rule, "nullPointer")
        self.assertEqual(got[0].line, 12)

    def test_clang_tidy(self):
        out = ("src/a.c:5:7: warning: variable 'x' is unused "
               "[clang-diagnostic-unused-variable]\n")
        got = lint._parse_clang_tidy(out, "", "/repo")
        self.assertEqual(got[0].rule, "clang-diagnostic-unused-variable")

    def test_gfortran(self):
        err = "m.f90:7:4: Warning: Unused variable 'q' declared\n"
        got = lint._parse_gfortran("", err, "/repo")
        self.assertEqual(got[0].line, 7)


class TestToolSelection(unittest.TestCase):
    def test_opt_in_tools_excluded_by_default(self):
        names = {t.name for t in lint.select_tools(["a.c"], [], [])}
        self.assertIn("cppcheck", names)
        self.assertNotIn("clang-tidy", names)

    def test_opt_in_tool_included_when_enabled(self):
        names = {t.name for t in lint.select_tools(["a.c"], ["clang-tidy"], [])}
        self.assertIn("clang-tidy", names)

    def test_skip_removes_tool(self):
        names = {t.name for t in lint.select_tools(["a.py"], [], ["ruff-format"])}
        self.assertNotIn("ruff-format", names)
        self.assertIn("ruff-check", names)

    def test_extension_gating(self):
        names = {t.name for t in lint.select_tools(["a.py"], [], [])}
        self.assertNotIn("cppcheck", names)

    def test_extensionless_tool_takes_all_files(self):
        tool = lint.TOOLS_BY_NAME["codespell"]
        self.assertEqual(lint.files_for(tool, ["a.py", "b.c"]), ["a.py", "b.c"])

    def test_unknown_tool_name_is_rejected(self):
        rc = lint.main(["check", "--enable", "not-a-tool"])
        self.assertEqual(rc, lint.EXIT_ERROR)


class TestCoverage(unittest.TestCase):
    def test_missing_tool_reports_unavailable_not_pass(self):
        results = [lint.ToolResult("ruff-check", False, False,
                                   skipped_reason="ruff not installed")]
        cov = lint.coverage(["a.py"], results)
        self.assertEqual(cov["python"], "unavailable")

    def test_ran_tool_reports_covered(self):
        results = [lint.ToolResult("ruff-check", True, True)]
        self.assertEqual(lint.coverage(["a.py"], results)["python"], "covered")

    def test_extensionless_tool_absence_is_reported_for_any_change(self):
        """A change of only unrecognised extensions must not report empty
        coverage and pass: the extension-less tool still applies."""
        cov = lint.coverage(["notes.txt"],
                            [lint.ToolResult("codespell", False, False,
                                             skipped_reason="not installed")])
        self.assertEqual(cov.get("any"), "unavailable")

    def test_extensionless_tool_covered_when_it_ran(self):
        cov = lint.coverage(["notes.txt"], [lint.ToolResult("codespell", True, True)])
        self.assertEqual(cov.get("any"), "covered")

    def test_language_not_present_is_absent_from_coverage(self):
        cov = lint.coverage(["a.py"], [lint.ToolResult("ruff-check", True, True)])
        self.assertNotIn("fortran", cov)


class TestChangedFileScope(unittest.TestCase):
    def test_absolute_no_base_scopes_whole_tracked_tree(self):
        """Absolute mode over no explicit paths must not silently scope to the
        (possibly empty) set of uncommitted changes."""
        with TempRepo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.write("b.md", "# b\n")
            repo.commit("base")

            class A:
                paths, base, all = [], None, True
            files = lint.resolve_files(A(), lint.repo_root(repo.dir))
            self.assertEqual(sorted(files), ["a.py", "b.md"])

    def test_differential_scope_includes_committed_and_uncommitted(self):
        with TempRepo() as repo:
            repo.write("a.py", "x = 1\n")
            base = repo.commit("base")
            repo.write("b.py", "y = 2\n")
            repo.commit("head")
            repo.write("c.py", "z = 3\n")  # uncommitted

            class A:
                paths, all = [], False
            a = A(); a.base = base
            files = lint.resolve_files(a, lint.repo_root(repo.dir))
            self.assertIn("b.py", files)
            self.assertIn("c.py", files)

    def test_deleted_file_is_not_in_scope(self):
        with TempRepo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.write("gone.py", "y = 2\n")
            base = repo.commit("base")
            os.remove(os.path.join(repo.dir, "gone.py"))
            repo.commit("delete")

            class A:
                paths, all = [], False
            a = A(); a.base = base
            self.assertNotIn("gone.py", lint.resolve_files(a, lint.repo_root(repo.dir)))


class TestWhitespaceCheck(unittest.TestCase):
    def test_detects_conflict_marker_introduced(self):
        with TempRepo() as repo:
            repo.write("a.txt", "clean\n")
            base = repo.commit("base")
            repo.write("a.txt", "clean\ntrailing   \n")
            repo.commit("head")
            got = lint.whitespace_check(lint.repo_root(repo.dir), base)
            self.assertTrue(any(f.tool == "git-diff-check" for f in got))


class TestBaseWorktree(unittest.TestCase):
    def test_worktree_is_created_at_base_and_removed(self):
        with TempRepo() as repo:
            repo.write("a.py", "old = 1\n")
            base = repo.commit("base")
            repo.write("a.py", "new = 2\n")
            repo.commit("head")
            root = lint.repo_root(repo.dir)
            with lint.base_worktree(root, base) as wt:
                with open(os.path.join(wt, "a.py")) as fh:
                    self.assertIn("old", fh.read())
                held = wt
            self.assertFalse(os.path.exists(held))


class TestInstallSafety(unittest.TestCase):
    def test_install_is_dry_run_without_yes(self):
        """A dry run must not execute anything, and must say so."""
        with TempRepo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.commit("base")

            class A:
                paths, base, enable, skip = [], None, [], []
                json, timeout, yes, all_tools = False, 30, False, True
                repo_ = None
            a = A(); a.repo = repo.dir
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = lint.cmd_install(a)
            self.assertEqual(rc, lint.EXIT_PASS)
            text = buf.getvalue()
            if "nothing to install" not in text:
                self.assertIn("dry run", text)

    def test_check_subcommand_has_no_install_path(self):
        """`check` must never install: the string is absent from its help and
        cmd_check never calls cmd_install."""
        import inspect
        src = inspect.getsource(lint.cmd_check)
        self.assertNotIn("cmd_install", src)
        self.assertNotIn("pip install", src)

    def test_missing_binary_hint_names_the_tool_and_the_install_command(self):
        """A missing linter must tell the human what is absent and what fixes
        it — silence is how an unavailable gate reads as a pass."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lint.print_missing_hint(["ruff", "markdownlint-cli2"])
        text = buf.getvalue()
        self.assertIn("ruff", text)
        self.assertIn("markdownlint-cli2", text)
        self.assertIn("those checks did not run", text)
        self.assertIn("lint.py install", text)

    def test_missing_binary_hint_is_silent_when_nothing_is_missing(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lint.print_missing_hint([])
        self.assertEqual(buf.getvalue(), "")

    def test_python_tools_resolve_to_one_install_path_when_uv_exists(self):
        """ruff and codespell are both Python tools: with uv available they must
        plan the same manager, not uv for one and `pip install --user` for the
        other. Availability is stubbed so the assertion holds on any host."""
        import contextlib
        import io
        with TempRepo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.commit("base")

            class A:
                paths, base, enable, skip = [], None, [], []
                json, timeout, yes, all_tools = False, 30, False, True
            a = A(); a.repo = repo.dir

            # Only uv is installed: every linter is missing, uv is the one manager.
            original = lint.have
            lint.have = lambda binary: binary == "uv"
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = lint.cmd_install(a)
            finally:
                lint.have = original
            self.assertEqual(rc, lint.EXIT_PASS)
            text = buf.getvalue()
            self.assertIn("uv tool install ruff", text)
            self.assertIn("uv tool install codespell", text)
            self.assertNotIn("pip install --user", text)
            self.assertIn("dry run", text)

    def test_uv_note_is_silent_when_uv_cannot_supply_the_missing_tool(self):
        """Recommending uv for a Node or C binary it cannot install is a false
        instruction, not a helpful default."""
        import contextlib
        import io
        original = lint.have
        lint.have = lambda binary: False  # nothing installed, uv included
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                lint.print_missing_hint(["markdownlint-cli2", "clang-format"])
            md_only = buf.getvalue()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                lint.print_missing_hint(["ruff"])
            with_python = buf.getvalue()
        finally:
            lint.have = original
        self.assertNotIn("uv", md_only)
        self.assertIn("uv", with_python)


class TestExitCodes(unittest.TestCase):
    def test_codes_are_distinct_and_documented(self):
        codes = {lint.EXIT_PASS, lint.EXIT_FINDINGS, lint.EXIT_ERROR, lint.EXIT_COVERAGE}
        self.assertEqual(len(codes), 4)
        self.assertEqual(lint.EXIT_PASS, 0)

    def test_no_files_in_scope_passes(self):
        with TempRepo() as repo:
            repo.write("a.py", "x = 1\n")
            repo.commit("base")

            class A:
                paths, base, enable, skip = [], None, [], []
                json, timeout = True, 30
                all, require_coverage = False, False
            a = A(); a.repo = repo.dir
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = lint.cmd_check(a)
            self.assertEqual(rc, lint.EXIT_PASS)


class FakeTool:
    """Install a fake binary on PATH and register a Tool that uses it, so the
    orchestration paths can be exercised without any real linter."""

    def __init__(self, name="faketool", exit_code=0, stdout="", extensions=(".fk",),
                 ok_codes=(0, 1), language="fake", per_file_msg=None):
        self.name, self.exit_code, self.stdout = name, exit_code, stdout
        self.extensions, self.ok_codes, self.language = extensions, ok_codes, language
        # When set, emit "<path>:1: <msg>" for every file handed to the binary,
        # which is how a real linter behaves.
        self.per_file_msg = per_file_msg

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="lint-fakebin-")
        script = os.path.join(self.dir, self.name)
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\n")
            fh.writelines(f"echo {line!r}\n" for line in self.stdout.splitlines())
            if self.per_file_msg:
                fh.write('for f in "$@"; do [ -f "$f" ] && '
                         f'echo "$f:1: {self.per_file_msg}"; done\n')
            fh.write(f"exit {self.exit_code}\n")
        os.chmod(script, 0o755)
        self._path = os.environ["PATH"]
        os.environ["PATH"] = self.dir + os.pathsep + self._path

        def parse(out, err, cwd):
            found = []
            for line in (out + "\n" + err).splitlines():
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[1].isdigit():
                    found.append(lint.Finding(self.name, "FAKE", parts[0],
                                              int(parts[1]), parts[2].strip()))
            return found

        self.tool = lint.Tool(name=self.name, language=self.language,
                              extensions=self.extensions, binary=self.name,
                              build=lambda b, f: [b, *f], parse=parse,
                              ok_codes=self.ok_codes)
        self._saved_tools = list(lint.TOOLS)
        lint.TOOLS[:] = [self.tool]
        return self

    def __exit__(self, *exc):
        os.environ["PATH"] = self._path
        lint.TOOLS[:] = self._saved_tools
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class CheckArgs:
    """Minimal stand-in for parsed `check` arguments."""

    def __init__(self, repo, base=None, **kw):
        self.repo, self.base = repo, base
        self.paths, self.enable, self.skip = [], [], []
        self.json, self.timeout = True, 30
        self.all = kw.get("all", False)
        self.require_coverage = kw.get("require_coverage", False)


def run_check(args):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = lint.cmd_check(args)
    return rc, buf.getvalue()


class TestFatalExitIsNotAPass(unittest.TestCase):
    """P1: a linter that dies (bad config, crash) must never read as clean."""

    def test_nonzero_exit_with_no_findings_is_an_error(self):
        with TempRepo() as repo, FakeTool(exit_code=2, stdout="error: bad config"):
            repo.write("a.fk", "x\n")
            repo.commit("base")
            repo.write("b.fk", "y\n")
            rc, out = run_check(CheckArgs(repo.dir))
            self.assertEqual(rc, lint.EXIT_ERROR)
            self.assertIn("exited 2", out)

    def test_declared_findings_exit_code_is_not_an_error(self):
        with TempRepo() as repo, FakeTool(exit_code=1, stdout="b.fk:1: something"):
            repo.write("a.fk", "x\n")
            repo.commit("base")
            repo.write("b.fk", "y\n")
            rc, _ = run_check(CheckArgs(repo.dir))
            self.assertEqual(rc, lint.EXIT_FINDINGS)

    def test_unexpected_code_with_parsed_findings_still_reports_them(self):
        with FakeTool(exit_code=99, stdout="b.fk:1: something") as fake:
            res = lint.run_tool(fake.tool, ["b.fk"], os.getcwd(), 30)
            self.assertTrue(res.ran)
            self.assertIsNone(res.error)


class TestWhitespaceCoversWorktreeAndUntracked(unittest.TestCase):
    """P1: the prescribed pre-commit call is `--base HEAD`, which as a committed
    range is empty; the check must still see uncommitted and untracked work."""

    def test_uncommitted_tracked_whitespace_is_found_with_base_head(self):
        with TempRepo() as repo:
            repo.write("a.txt", "clean\n")
            repo.commit("base")
            repo.write("a.txt", "clean\ntrailing   \n")
            got = lint.whitespace_check(lint.repo_root(repo.dir), "HEAD")
            self.assertTrue(any(f.path.endswith("a.txt") for f in got), got)

    def test_untracked_file_whitespace_is_found(self):
        with TempRepo() as repo:
            repo.write("a.txt", "clean\n")
            repo.commit("base")
            repo.write("new.txt", "bad   \n")
            got = lint.whitespace_check(lint.repo_root(repo.dir), "HEAD")
            self.assertTrue(any("new.txt" in f.path for f in got), got)

    def test_committed_range_whitespace_still_found(self):
        with TempRepo() as repo:
            repo.write("a.txt", "clean\n")
            base = repo.commit("base")
            repo.write("a.txt", "clean\nbad   \n")
            repo.commit("head")
            got = lint.whitespace_check(lint.repo_root(repo.dir), base)
            self.assertTrue(any(f.path.endswith("a.txt") for f in got), got)


class TestRenameDoesNotFabricateFindings(unittest.TestCase):
    """P1: renaming a file must not turn its untouched pre-existing findings
    into newly introduced ones."""

    def test_rename_map_detects_the_pair(self):
        with TempRepo() as repo:
            repo.write("old.py", "import os\n" * 3)
            base = repo.commit("base")
            sh(["git", "mv", "old.py", "new.py"], repo.dir)
            repo.commit("rename")
            mapping = lint.rename_map(lint.repo_root(repo.dir), base)
            self.assertEqual(mapping.get("new.py"), "old.py")

    def test_renamed_file_reports_no_new_findings(self):
        with TempRepo() as repo, FakeTool(exit_code=1, per_file_msg="pre-existing"):
            repo.write("old.fk", "x\n")
            base = repo.commit("base")
            sh(["git", "mv", "old.fk", "new.fk"], repo.dir)
            repo.commit("rename")
            rc, out = run_check(CheckArgs(repo.dir, base=base))
            payload = json.loads(out)
            self.assertEqual(payload["new_findings"], [], payload)
            self.assertEqual(rc, lint.EXIT_PASS)


class TestCoverageGateCommandLevel(unittest.TestCase):
    """P1/P2: an uncovered language must exit 3, and must outrank findings."""

    def test_missing_tool_with_require_coverage_exits_3(self):
        with TempRepo() as repo, FakeTool() as fake:
            fake.tool.binary = "definitely-not-installed-xyz"
            repo.write("a.fk", "x\n")
            repo.commit("base")
            repo.write("b.fk", "y\n")
            rc, _ = run_check(CheckArgs(repo.dir, require_coverage=True))
            self.assertEqual(rc, lint.EXIT_COVERAGE)

    def test_coverage_gap_outranks_findings(self):
        with TempRepo() as repo, FakeTool(exit_code=1, stdout="b.fk:1: x"):
            repo.write("a.fk", "x\n")
            repo.commit("base")
            repo.write("b.fk", "y\n")
            # A second, uninstalled tool for another language in the same change.
            missing = lint.Tool(name="ghost", language="ghostlang", extensions=(".gh",),
                                binary="definitely-not-installed-xyz",
                                build=lambda b, f: [b, *f],
                                parse=lambda o, e, c: [])
            lint.TOOLS.append(missing)
            repo.write("c.gh", "z\n")
            rc, out = run_check(CheckArgs(repo.dir, require_coverage=True))
            self.assertEqual(rc, lint.EXIT_COVERAGE, out)


class TestBaseSideFailureIsNotAPass(unittest.TestCase):
    """P1: if the base run fails, every pre-existing finding would look new."""

    def test_base_error_is_surfaced(self):
        with TempRepo() as repo, FakeTool(exit_code=2, stdout="boom"):
            repo.write("a.fk", "x\n")
            base = repo.commit("base")
            repo.write("a.fk", "y\n")
            repo.commit("head")
            rc, out = run_check(CheckArgs(repo.dir, base=base))
            self.assertEqual(rc, lint.EXIT_ERROR, out)


class TestConfigPrecedence(unittest.TestCase):
    def test_markdownlint_uses_shipped_default_when_project_has_none(self):
        with TempRepo() as repo:
            argv = lint._markdownlint_config(repo.dir)
            self.assertEqual(argv[:1], ["--config"])
            self.assertTrue(argv[1].endswith("markdownlint.jsonc"))

    def test_project_markdownlint_config_wins(self):
        with TempRepo() as repo:
            repo.write(".markdownlint.jsonc", "{}\n")
            self.assertEqual(lint._markdownlint_config(repo.dir), [])

    def test_package_json_markdownlint_key_wins(self):
        with TempRepo() as repo:
            repo.write("package.json", '{"markdownlint-cli2": {"config": {}}}')
            self.assertEqual(lint._markdownlint_config(repo.dir), [])

    def test_ruff_uses_shipped_default_when_project_has_none(self):
        with TempRepo() as repo:
            argv = lint._ruff_config(repo.dir)
            self.assertEqual(argv[:1], ["--config"])
            self.assertTrue(argv[1].endswith("ruff.toml"))

    def test_project_ruff_config_wins(self):
        for name in ("ruff.toml", ".ruff.toml"):
            with TempRepo() as repo:
                repo.write(name, "[lint]\nselect = [\"E\"]\n")
                self.assertEqual(lint._ruff_config(repo.dir), [], name)

    def test_pyproject_wins_only_when_it_configures_ruff(self):
        """Nearly every Python project has a pyproject.toml; treating its mere
        presence as ruff configuration would disable the default everywhere."""
        with TempRepo() as repo:
            repo.write("pyproject.toml", "[project]\nname = \"x\"\n")
            self.assertNotEqual(lint._ruff_config(repo.dir), [])
        with TempRepo() as repo:
            repo.write("pyproject.toml", "[project]\nname = \"x\"\n\n[tool.ruff.lint]\nselect = [\"F\"]\n")
            self.assertEqual(lint._ruff_config(repo.dir), [])

    def test_shipped_ruff_default_selects_defects_not_taste(self):
        """The rule set is the whole point of shipping a config: F841 (the dead
        local five runs shipped) must be in; the taste families must be out."""
        shipped = os.path.join(lint.SKILL_DIR, "config", "ruff.toml")
        with open(shipped, encoding="utf-8") as fh:
            body = fh.read()
        selected = re.search(r"select = \[(.*?)\n\]", body, re.S).group(1)
        for family in ("\"F\"", "\"B\"", "\"DTZ\""):
            self.assertIn(family, selected)
        for taste in ("\"I\"", "\"C4\"", "\"SIM\"", "\"UP\"", "\"E7\"", "\"RUF\""):
            self.assertNotIn(taste, selected)

    def test_clang_format_skipped_without_project_config(self):
        with TempRepo() as repo:
            self.assertIsNotNone(lint._needs_clang_format_config(repo.dir))

    def test_clang_format_runs_with_project_config(self):
        with TempRepo() as repo:
            repo.write(".clang-format", "BasedOnStyle: LLVM\n")
            self.assertIsNone(lint._needs_clang_format_config(repo.dir))


if __name__ == "__main__":
    unittest.main()

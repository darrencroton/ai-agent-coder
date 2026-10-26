"""Protected behaviours: git facts, status parsing, and surface matching.

Surface matching is segment-aware (`PurePosixPath.full_match`, not fnmatch,
so a single "*" never crosses a "/"). The git-fact tests run against a real
temporary repository with actual commits rather than string fixtures.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pm_test_helpers import PmTestCase

from pm_lib import PmError
from pm_lib import git_ops


class TestSurfaceMatching(unittest.TestCase):
    def test_trailing_slash_matches_subtree(self) -> None:
        self.assertTrue(git_ops.is_authorized_path("src/deep/file.py", ["src/"]))
        self.assertTrue(git_ops.is_authorized_path("src/file.py", ["src/"]))
        self.assertFalse(git_ops.is_authorized_path("other/file.py", ["src/"]))

    def test_plain_path_does_not_match_beneath_directory(self) -> None:
        self.assertFalse(git_ops.is_authorized_path("src/file.py", ["src"]))
        self.assertTrue(git_ops.is_authorized_path("src", ["src"]))

    def test_glob_star_is_segment_aware(self) -> None:
        self.assertTrue(git_ops.is_authorized_path("a.md", ["*.md"]))
        self.assertFalse(git_ops.is_authorized_path("deep/a.md", ["*.md"]))

    def test_double_star_glob_matches_recursively(self) -> None:
        self.assertTrue(git_ops.is_authorized_path("deep/a.md", ["**/*.md"]))
        self.assertTrue(git_ops.is_authorized_path("a.md", ["**/*.md"]))

    def test_directory_scoped_star_glob_does_not_cross_segments(self) -> None:
        self.assertTrue(git_ops.is_authorized_path("src/a.py", ["src/*.py"]))
        self.assertFalse(git_ops.is_authorized_path("src/deep/a.py", ["src/*.py"]))

    def test_question_mark_matches_single_char_in_segment(self) -> None:
        self.assertTrue(git_ops.is_authorized_path("a1.py", ["a?.py"]))
        self.assertFalse(git_ops.is_authorized_path("a12.py", ["a?.py"]))
        self.assertFalse(git_ops.is_authorized_path("deep/a1.py", ["a?.py"]))

    def test_exact_string_match(self) -> None:
        self.assertTrue(git_ops.is_authorized_path("README.md", ["README.md"]))
        self.assertFalse(git_ops.is_authorized_path("README.md.bak", ["README.md"]))

    def test_unauthorized_files_filters_correctly(self) -> None:
        changed = {"src/a.py", "docs/readme.md", "unrelated.txt"}
        result = git_ops.unauthorized_files(changed, ["src/", "docs/readme.md"])
        self.assertEqual(result, ["unrelated.txt"])

    def test_normalize_backtick_wrapped_entry_with_glob(self) -> None:
        self.assertEqual(git_ops.normalize_authorized_entry("`*.md`"), "*.md")
        self.assertTrue(git_ops.is_authorized_path("a.md", ["`*.md`"]))
        self.assertFalse(git_ops.is_authorized_path("deep/a.md", ["`*.md`"]))

    def test_normalize_backtick_with_trailing_annotation(self) -> None:
        self.assertEqual(git_ops.normalize_authorized_entry("`src/new.py` (new file)"), "src/new.py")

    def test_normalize_plain_entry_strips_trailing_period(self) -> None:
        self.assertEqual(git_ops.normalize_authorized_entry("README.md."), "README.md")


class TestProtectedMatching(unittest.TestCase):
    def test_plain_directory_entry_covers_its_subtree_and_itself_but_not_a_sibling_prefix(self) -> None:
        protected = ["instruments"]
        self.assertTrue(git_ops.is_protected_path("instruments/check.py", protected))
        self.assertTrue(git_ops.is_protected_path("instruments", protected))
        self.assertFalse(git_ops.is_protected_path("instrumentsx/a", protected))
        # The authorized-surface matcher must never widen the same way.
        self.assertFalse(git_ops.is_authorized_path("instruments/check.py", protected))

    def test_slash_glob_and_exact_entries_behave_as_the_authorized_matcher(self) -> None:
        self.assertTrue(git_ops.is_protected_path("src/deep/a.py", ["src/"]))
        self.assertFalse(git_ops.is_protected_path("srcx/a.py", ["src/"]))
        self.assertTrue(git_ops.is_protected_path("src/a.py", ["src/*.py"]))
        self.assertFalse(git_ops.is_protected_path("src/deep/a.py", ["src/*.py"]))
        self.assertTrue(git_ops.is_protected_path("policy.json", ["`policy.json` (the policy)"]))
        self.assertFalse(git_ops.is_protected_path("policy.json.bak", ["policy.json"]))


class TestStatusParsing(unittest.TestCase):
    def test_leading_space_status_code_preserved(self) -> None:
        # An unstaged modify: code is " M" (leading space is meaningful, not stripped).
        self.assertEqual(git_ops.status_path(" M README.md"), "README.md")

    def test_rename_takes_target_path(self) -> None:
        self.assertEqual(git_ops.status_path("R  old.py -> new.py"), "new.py")

    def test_quoted_path_stripped_of_surrounding_quotes_only(self) -> None:
        self.assertEqual(git_ops.status_path('?? "odd file.txt"'), "odd file.txt")

    def test_pm_dir_and_prefix_filtered_from_meaningful_lines(self) -> None:
        status_text = "?? .pm\n?? .pm/run.json\n M README.md\n?? .pmx/not-filtered.txt\n"
        lines = git_ops.meaningful_status_lines(status_text)
        joined = "\n".join(lines)
        self.assertNotIn(".pm\n", joined + "\n")
        self.assertNotIn("run.json", joined)
        self.assertIn("README.md", joined)
        # ".pmx" is a distinct path, not ".pm" or ".pm/"-prefixed — never filtered.
        self.assertIn("not-filtered.txt", joined)

    def test_status_changed_files_excludes_pm(self) -> None:
        status_text = "?? .pm/run.json\n M README.md\n"
        self.assertEqual(git_ops.status_changed_files(status_text), {"README.md"})


class TestGitFacts(PmTestCase):
    def test_git_head_and_is_full_commit_hash(self) -> None:
        head = git_ops.git_head(self.repo)
        self.assertIsNotNone(head)
        self.assertTrue(git_ops.is_full_commit_hash(head))
        self.assertFalse(git_ops.is_full_commit_hash("not-a-hash"))
        self.assertFalse(git_ops.is_full_commit_hash(None))

    def test_current_branch_reports_main_and_none_when_detached(self) -> None:
        self.assertEqual(git_ops.current_branch(self.repo), "main")
        head = git_ops.git_head(self.repo)
        self._git("checkout", "-q", head)
        self.assertIsNone(git_ops.current_branch(self.repo))

    def test_changed_files_between_commits_and_dirty_union(self) -> None:
        before_head = git_ops.git_head(self.repo)
        (self.repo / "committed.py").write_text("x = 1\n", encoding="utf-8")
        self._git("add", "committed.py")
        self._git("commit", "-q", "-m", "add committed.py")
        after_head = git_ops.git_head(self.repo)
        (self.repo / "dirty.py").write_text("y = 2\n", encoding="utf-8")
        after_status = git_ops.git_status_text(self.repo)
        changed = git_ops.changed_files_between(self.repo, before_head, after_head, after_status)
        self.assertIn("committed.py", changed)
        self.assertIn("dirty.py", changed)

    def test_changed_files_between_reports_both_sides_of_a_committed_rename(self) -> None:
        """Without `--no-renames`, git reports only a rename's destination, so
        a file moved out of a protected or unauthorized path would be invisible
        to fact 5. Covers both the diff branch and the unborn-before `show`
        branch."""
        (self.repo / "protected").mkdir()
        (self.repo / "protected" / "check.py").write_text("print('instrument')\n" * 20, encoding="utf-8")
        self._git("add", "protected/check.py")
        self._git("commit", "-q", "-m", "add protected/check.py")
        before_head = git_ops.git_head(self.repo)
        (self.repo / "src").mkdir()
        self._git("mv", "protected/check.py", "src/check.py")
        self._git("commit", "-q", "-m", "move check.py")
        after_head = git_ops.git_head(self.repo)
        after_status = git_ops.git_status_text(self.repo)

        between = git_ops.changed_files_between(self.repo, before_head, after_head, after_status)
        self.assertEqual(between, {"protected/check.py", "src/check.py"})
        shown = git_ops.changed_files_between(self.repo, None, after_head, after_status)
        self.assertEqual(shown, {"protected/check.py", "src/check.py"})

    def test_dirty_rename_reports_both_sides(self) -> None:
        """The dirty-tree counterpart of the committed-rename test: plain
        `git status` collapses an uncommitted `git mv` to its destination."""
        (self.repo / "protected").mkdir()
        (self.repo / "protected" / "check.py").write_text("print('instrument')\n" * 20, encoding="utf-8")
        self._git("add", "protected/check.py")
        self._git("commit", "-q", "-m", "add protected/check.py")
        head = git_ops.git_head(self.repo)
        (self.repo / "src").mkdir()
        self._git("mv", "protected/check.py", "src/check.py")
        status = git_ops.git_status_text(self.repo)

        expected = {"protected/check.py", "src/check.py"}
        self.assertEqual(git_ops.status_changed_files(status), expected)
        self.assertEqual(git_ops.changed_files_between(self.repo, head, head, status), expected)

    def test_commit_is_descendant_true(self) -> None:
        before_head = git_ops.git_head(self.repo)
        (self.repo / "next.py").write_text("z = 3\n", encoding="utf-8")
        self._git("add", "next.py")
        self._git("commit", "-q", "-m", "next commit")
        after_head = git_ops.git_head(self.repo)
        self.assertTrue(git_ops.commit_is_descendant(self.repo, before_head, after_head))

    def test_commit_is_descendant_false(self) -> None:
        head1 = git_ops.git_head(self.repo)
        (self.repo / "next.py").write_text("z = 3\n", encoding="utf-8")
        self._git("add", "next.py")
        self._git("commit", "-q", "-m", "next commit")
        head2 = git_ops.git_head(self.repo)
        # head1 is not a descendant of head2 (it is the ancestor).
        self.assertFalse(git_ops.commit_is_descendant(self.repo, head2, head1))

    def test_commit_is_descendant_none_after_head_is_false(self) -> None:
        self.assertFalse(git_ops.commit_is_descendant(self.repo, git_ops.git_head(self.repo), None))

    def test_commit_is_descendant_none_before_head_is_true(self) -> None:
        self.assertTrue(git_ops.commit_is_descendant(self.repo, None, git_ops.git_head(self.repo)))

    def test_require_clean_worktree_raises_on_dirty_outside_pm(self) -> None:
        (self.repo / "dirty.txt").write_text("oops\n", encoding="utf-8")
        with self.assertRaises(PmError):
            git_ops.require_clean_worktree(self.repo)

    def test_require_clean_worktree_passes_with_only_pm_litter(self) -> None:
        pm_dir = self.repo / ".pm"
        pm_dir.mkdir()
        (pm_dir / "scratch.txt").write_text("litter\n", encoding="utf-8")
        git_ops.require_clean_worktree(self.repo)  # must not raise

    def test_worktree_git_dir_distinct_for_linked_worktree(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            linked_path = Path(tmp) / "linked"
            self._git("worktree", "add", "-b", "linked-branch", str(linked_path))
            main_dir = git_ops.worktree_git_dir(self.repo)
            linked_dir = git_ops.worktree_git_dir(linked_path)
            self.assertNotEqual(main_dir, linked_dir)

    def test_git_diff_text_produces_a_patch_for_working_tree_change(self) -> None:
        (self.repo / "README.md").write_text("hello, changed\n", encoding="utf-8")
        content, error = git_ops.git_diff_text(self.repo, None, None)
        self.assertIn("README.md", content)
        self.assertIsNone(error)

    def test_git_diff_text_covers_first_commit_on_unborn_branch_run(self) -> None:
        # A run initialized before the first commit has before_head=None; the
        # committed slice must still appear in the evidence patch, mirroring
        # changed_files_between's `git show` handling of the same case.
        (self.repo / "new-file.txt").write_text("first slice\n", encoding="utf-8")
        self._git("add", "new-file.txt")
        self._git("commit", "-q", "-m", "first slice commit")
        after_head = git_ops.git_head(self.repo)
        content, error = git_ops.git_diff_text(self.repo, None, after_head)
        self.assertIn("new-file.txt", content)
        self.assertIsNone(error)

    def test_gitlink_in_index_is_refused_by_status_and_worktree_diff(self) -> None:
        # A gitlink makes the host's `git status` and ref-less `git diff` execute
        # the submodule's configuration, so both refuse before running anything.
        # `--cacheinfo` plants the entry without a populated submodule.
        head = git_ops.git_head(self.repo)
        self._git("update-index", "--add", "--cacheinfo", f"160000,{head},sub")
        self.assertEqual(git_ops.gitlinks(self.repo), ["sub"])
        with self.assertRaisesRegex(PmError, "submodules are not supported.*sub"):
            git_ops.git_status_text(self.repo)
        with self.assertRaisesRegex(PmError, "submodules are not supported.*sub"):
            git_ops.git_diff_text(self.repo, None, None)

    def test_git_diff_text_returns_empty_diff_and_error_text_on_bad_ref(self) -> None:
        content, error = git_ops.git_diff_text(self.repo, "0" * 40, "1" * 40)
        self.assertEqual(content, "")
        self.assertIsNotNone(error)
        self.assertTrue(error.strip())

    def test_resolve_repo_and_resolve_plan(self) -> None:
        resolved = git_ops.resolve_repo(self.repo)
        self.assertEqual(resolved, self.repo.resolve())
        with self.assertRaises(PmError):
            git_ops.resolve_repo(self.repo / "does-not-exist")
        plan_path = self.repo / "plan.md"
        plan_path.write_text("# Plan\n", encoding="utf-8")
        self.assertEqual(git_ops.resolve_plan(plan_path), plan_path.resolve())
        with self.assertRaises(PmError):
            git_ops.resolve_plan(self.repo / "missing-plan.md")

    def test_git_raises_pm_error_with_stderr_on_failure(self) -> None:
        with self.assertRaises(PmError):
            git_ops.git(self.repo, "not-a-real-git-command")

    def test_git_result_never_raises(self) -> None:
        returncode, _stdout, stderr = git_ops.git_result(self.repo, "not-a-real-git-command")
        self.assertNotEqual(returncode, 0)
        self.assertTrue(stderr)


class TestGitCommonDirName(PmTestCase):
    def test_linked_worktree_names_the_main_repository(self) -> None:
        # Cleanups run last-registered-first: the temp dir is registered
        # first so the worktree is removed while its directory still exists.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        linked = tmp / "linked-worktree"
        self._git("worktree", "add", "-q", "-b", "linked-branch", str(linked))
        self.addCleanup(self._git, "worktree", "remove", "--force", str(linked))

        self.assertEqual(git_ops.git_common_dir_name(self.repo), self.repo.name)
        self.assertEqual(git_ops.git_common_dir_name(linked), self.repo.name)


if __name__ == "__main__":
    unittest.main()

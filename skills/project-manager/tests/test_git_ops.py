"""Protected behaviours: git facts, status parsing, and surface matching.

Pins the segment-aware surface-matching semantics (PurePosixPath.full_match,
not fnmatch, so a
single "*" never crosses a "/"), plus the git-fact helpers PM's floor will
build on in later stages:

- Surface matching: a trailing "/" entry matches anything with that
  prefix (subtree); a plain path matches only itself, never anything
  beneath a same-named directory; a single "*" or "?" is segment-aware
  (does not cross "/"); "**/*.md" matches recursively; exact strings match
  exactly; entries are normalized (backtick-unwrapped, trailing period and
  annotation stripped) before any of the above.
- `git status --short` parsing: the two-character status code is
  positional, so a leading space is preserved, never stripped, by
  `status_path`; a rename line's path is the target; quoted paths lose
  their surrounding quotes only; `.pm` and `.pm/`-prefixed lines are
  filtered out of "meaningful" status.
- `changed_files_between` unions the committed diff (before_head..after_head)
  with the dirty-tree status, against a real temp repo with an actual
  commit in it — not string fixtures.
- `commit_is_descendant` is true only when `after_head` is a real
  descendant of `before_head` (or `before_head` is None); false otherwise.
- `require_clean_worktree` raises on any dirty file outside `.pm/` and
  passes when only `.pm/` litter is dirty.
- `worktree_git_dir` resolves to a distinct directory for a linked
  worktree, so PM state never collides between worktrees of the same repo.
- `write_git_diff` writes a real patch for a working-tree change, and on a
  bad ref writes an empty (but valid) diff file plus a `git-diff-error.txt`
  sidecar recording why — never a diff-shaped file that is actually an
  error message.
"""

from __future__ import annotations

import unittest

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

    def test_write_git_diff_produces_a_patch_for_working_tree_change(self) -> None:
        (self.repo / "README.md").write_text("hello, changed\n", encoding="utf-8")
        destination = self.repo / "diff.patch"
        git_ops.write_git_diff(self.repo, None, None, destination)
        content = destination.read_text(encoding="utf-8")
        self.assertIn("README.md", content)
        self.assertFalse((destination.parent / "git-diff-error.txt").exists())

    def test_write_git_diff_writes_error_sidecar_on_bad_ref(self) -> None:
        destination = self.repo / "diff.patch"
        git_ops.write_git_diff(self.repo, "0" * 40, "1" * 40, destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), "")
        sidecar = destination.parent / "git-diff-error.txt"
        self.assertTrue(sidecar.exists())
        self.assertTrue(sidecar.read_text(encoding="utf-8").strip())

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


if __name__ == "__main__":
    unittest.main()

"""`hooks/pm-poll-guard.py`, driven as the harness drives it: JSON on stdin,
a decision off stdout (empty means allow; a deny body still exits 0).

HOME is redirected per test so digest stamps never touch the real machine.
The Bash cases use command strings taken verbatim from a real PM session
(`caf72e97`, 1391 turns, ~26% spent polling) — an earlier allowlist-of-
inspectors draft would have missed most of them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[1] / "hooks" / "pm-poll-guard.py"

# BELOW the harness's own 10s hook timeout, not merely finite: an input taking
# 15s would pass a laxer bound here while timing out in production.
_HOOK_TIMEOUT_SECONDS = 8.0

# A realistic Claude Code scratchpad task-output path: the layout the hook
# anchors on, with a uid, project slug and session uuid.
_TASK_OUTPUT = (
    "/private/tmp/claude-654982451/-Users-dcroton-Local-git-repos-mimic/"
    "caf72e97-e44f-4125-88ee-18353dc23bbc/tasks/binm8c00u.output"
)
_RUN_ID = "20260803T111212Z-15b2c9"
_REVIEW_ORIGINAL = f".git/pm/{_RUN_ID}/slices/slice-002/review-4-drift-audit-opencode.md"
_REVIEW_MIRROR = f".pm/runs/{_RUN_ID}/slices/slice-003/result.json"


class PollGuardTestCase(unittest.TestCase):
    """Runs the hook as a subprocess with an isolated HOME and a PM-shaped cwd."""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory(prefix="pm-poll-guard-home-")
        self._cwd = tempfile.TemporaryDirectory(prefix="pm-poll-guard-cwd-")
        self.addCleanup(self._home.cleanup)
        self.addCleanup(self._cwd.cleanup)
        self.home = Path(self._home.name)
        self.cwd = Path(self._cwd.name)
        # The PM-run gate: the guard only ever acts inside a run.
        (self.cwd / ".pm").mkdir()

    def invoke(self, payload: dict | str) -> tuple[int, str]:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        try:
            completed = subprocess.run(
                [sys.executable, str(_HOOK)],
                input=raw,
                capture_output=True,
                text=True,
                # The harness gives the hook ten seconds. A bounded timeout here
                # turns "the hook hangs" into a named failure instead of a
                # wedged test module, which is the only way this suite can
                # actually enforce the no-hang half of fail-open.
                timeout=_HOOK_TIMEOUT_SECONDS,
                env={**os.environ, "HOME": str(self.home)},
            )
        except subprocess.TimeoutExpired:
            self.fail(f"the hook did not terminate within {_HOOK_TIMEOUT_SECONDS}s")
        return completed.returncode, completed.stdout

    def assertAllowed(self, payload: dict | str, msg: str = "") -> None:
        code, stdout = self.invoke(payload)
        self.assertEqual(code, 0, "the hook must always exit 0")
        self.assertEqual(stdout.strip(), "", msg or "expected an allow (empty stdout)")

    def assertDenied(self, payload: dict | str, msg: str = "") -> None:
        code, stdout = self.invoke(payload)
        self.assertEqual(code, 0, "the hook must always exit 0, even when denying")
        self.assertNotEqual(stdout.strip(), "", msg or "expected a deny")
        decision = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertTrue(decision["permissionDecisionReason"].strip(), "a deny must explain itself")

    # --- payload builders ----------------------------------------------------

    def read_payload(self, file_path: str, *, cwd: Path | None = None, **extra) -> dict:
        return {
            "session_id": "session-a",
            "cwd": str(self.cwd if cwd is None else cwd),
            "tool_name": "Read",
            "tool_input": {"file_path": file_path, **extra},
        }

    def bash_payload(self, command: str, *, background: bool = True, cwd: Path | None = None) -> dict:
        return {
            "session_id": "session-a",
            "cwd": str(self.cwd if cwd is None else cwd),
            "tool_name": "Bash",
            "tool_input": {"command": command, "run_in_background": background},
        }

    def task_output(self, content: str) -> str:
        """A real file on disk at a path matching the scratchpad layout.

        The digest branch reads the file, so the path has to exist and the
        content has to be controllable to distinguish "unchanged" from
        "changed".
        """
        target = self.home / "scratch" / "claude-654982451" / "proj" / (
            "caf72e97-e44f-4125-88ee-18353dc23bbc"
        ) / "tasks" / "b91idsq93.output"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target)


class TestReadBranchUnchanged(PollGuardTestCase):
    """The Read branch predates this suite and its behaviour is deliberately frozen.

    It is the highest-yield part of the guard — on the measured session it
    denied 273 of 294 repeat reads — so these cases exist to catch a regression
    in it, not to describe new behaviour.
    """

    def test_first_read_is_allowed(self) -> None:
        path = self.task_output("partial output\n")
        self.assertAllowed(self.read_payload(path), "the first read of a task output must pass")

    def test_reread_with_identical_bytes_is_denied(self) -> None:
        path = self.task_output("partial output\n")
        self.assertAllowed(self.read_payload(path))
        self.assertDenied(self.read_payload(path), "an identical re-read learns nothing")

    def test_reread_after_content_changed_is_allowed(self) -> None:
        path = self.task_output("partial output\n")
        self.assertAllowed(self.read_payload(path))
        self.task_output("partial output\nmore\n")
        self.assertAllowed(self.read_payload(path), "new bytes are new information")

    def test_identical_reread_is_denied_again_after_a_change(self) -> None:
        path = self.task_output("one\n")
        self.assertAllowed(self.read_payload(path))
        self.task_output("two\n")
        self.assertAllowed(self.read_payload(path))
        self.assertDenied(self.read_payload(path))

    def test_outside_a_pm_run_nothing_is_denied(self) -> None:
        non_pm = Path(tempfile.mkdtemp(prefix="not-a-pm-run-"))
        self.addCleanup(lambda: non_pm.rmdir() if not any(non_pm.iterdir()) else None)
        path = self.task_output("same\n")
        self.assertAllowed(self.read_payload(path, cwd=non_pm))
        self.assertAllowed(
            self.read_payload(path, cwd=non_pm), "the PM-run gate must hold even on a repeat"
        )

    def test_an_ordinary_repository_file_is_allowed(self) -> None:
        ordinary = self.cwd / "tasks" / "abc.output"
        ordinary.parent.mkdir(parents=True, exist_ok=True)
        ordinary.write_text("x", encoding="utf-8")
        self.assertAllowed(self.read_payload(str(ordinary)))
        self.assertAllowed(
            self.read_payload(str(ordinary)),
            "a repository's own tasks/ directory must never match the scratchpad layout",
        )

    def test_a_paginated_read_of_unchanged_bytes_is_allowed(self) -> None:
        """The stamp digests the WHOLE file, so it cannot speak for a windowed
        read: asking for a different `offset`/`limit` of unchanged bytes
        genuinely returns something the previous read did not show. Denying it
        would be the one failure this guard must not have — a false deny that
        blocks a legitimate look — so any paginated read passes."""
        path = self.task_output("line one\nline two\nline three\n")
        self.assertAllowed(self.read_payload(path))
        self.assertAllowed(
            self.read_payload(path, offset=2),
            "a different window of the same bytes is new information",
        )
        self.assertAllowed(self.read_payload(path, limit=1))
        self.assertAllowed(self.read_payload(path, offset=2, limit=1))
        # The whole-file re-read is still denied: pagination widens nothing else.
        self.assertDenied(self.read_payload(path))

    def test_a_different_session_does_not_inherit_a_stamp(self) -> None:
        path = self.task_output("same\n")
        self.assertAllowed(self.read_payload(path))
        other = self.read_payload(path)
        other["session_id"] = "session-b"
        self.assertAllowed(other, "a stamp from another session must not deny a fresh read")


class TestBashBranch(PollGuardTestCase):
    """Backgrounded wait-then-inspect on a PM artifact.

    Every DENY command here is copied from the measured session, with only the
    absolute prefixes shortened where they do not affect matching.
    """

    def test_hand_rolled_waiter_on_a_task_output_is_denied(self) -> None:
        self.assertDenied(
            self.bash_payload(
                "cd /Users/dcroton/Local/git-repos/mimic; "
                f"until [ -s {_TASK_OUTPUT} ]; do sleep 30; done; "
                f"echo DONE; tail -5 {_TASK_OUTPUT}"
            )
        )

    def test_grep_waiter_on_a_live_review_report_is_denied(self) -> None:
        # The live report is the ORIGINAL under the state dir; the .pm/ mirror
        # is only written after the reviewer exits cleanly, so a guard anchored
        # on the mirror alone would miss every poll of a running review.
        self.assertDenied(
            self.bash_payload(
                "cd /Users/dcroton/Local/git-repos/mimic; "
                f'until grep -qi "^## Verdict|Verdict:" {_REVIEW_ORIGINAL} 2>/dev/null; '
                "do sleep 60; done; echo VERDICT"
            )
        )

    def test_waiter_on_a_mirrored_result_json_is_denied(self) -> None:
        self.assertDenied(
            self.bash_payload(
                "cd /Users/dcroton/Local/git-repos/mimic; "
                f"until [ -f {_REVIEW_MIRROR} ]; do sleep 60; done; "
                'echo "RESULT PRESENT"; git log --oneline -1'
            )
        )

    def test_sleep_then_listing_a_slice_directory_is_denied(self) -> None:
        self.assertDenied(
            self.bash_payload(
                "sleep 500; cd /Users/dcroton/Local/git-repos/mimic; "
                f"ls .pm/runs/{_RUN_ID}/slices/slice-003/ 2>/dev/null; git log --oneline -1"
            )
        )

    def test_tail_follow_is_denied(self) -> None:
        self.assertDenied(self.bash_payload(f"tail -F {_TASK_OUTPUT}"))

    def test_a_collision_suffixed_run_id_is_still_matched(self) -> None:
        # new_run_id appends -2, -3, ... when two runs mint the same id.
        self.assertDenied(
            self.bash_payload(
                f"sleep 300; cat .git/pm/{_RUN_ID}-2/slices/slice-001/review-1-code-review-codex.md"
            )
        )

    def test_a_four_digit_slice_directory_is_still_matched(self) -> None:
        self.assertDenied(
            self.bash_payload(f"sleep 300; cat .git/pm/{_RUN_ID}/slices/slice-1000/review-1-x.md")
        )

    # --- must stay allowed ---------------------------------------------------

    def test_the_same_command_in_the_foreground_is_allowed(self) -> None:
        """The escape hatch, and the reason this rule can be strict.

        A foreground wait blocks the turn but spawns no second completion
        notification, so it is not the waste being bounded — and it is the
        right tool when no notification is genuinely coming, e.g. after a
        session resume.
        """
        self.assertAllowed(
            self.bash_payload(f"sleep 900; tail -4 {_TASK_OUTPUT}", background=False)
        )

    def test_inspecting_without_waiting_is_allowed(self) -> None:
        self.assertAllowed(self.bash_payload(f"tail -4 {_TASK_OUTPUT}"))
        self.assertAllowed(self.bash_payload(f"cat {_REVIEW_ORIGINAL}"))

    def test_the_toolkits_own_waiters_are_allowed(self) -> None:
        """`observe --wait` and `review` are how a PM is supposed to wait, and
        `review` launches the very reviewer it then waits on — denying it would
        prevent the work as well as the watching."""
        self.assertAllowed(
            self.bash_payload(
                "cd /Users/dcroton/Local/git-repos/mimic && "
                "python3 ~/.claude/skills/project-manager/scripts/pm.py observe --wait 1800"
            )
        )
        self.assertAllowed(
            self.bash_payload(
                "python3 ~/.claude/skills/project-manager/scripts/pm.py review "
                f'--slice "Slice 2" --skill code-review --tool codex; sleep 5; cat {_REVIEW_ORIGINAL}'
            )
        )

    def test_the_exemption_needs_a_real_invocation_not_the_words(self) -> None:
        """The exemption once matched the bare substring `pm.py`, so appending a
        comment to any poll disabled the guard entirely. It now needs an actual
        `pm.py review`/`observe` invocation, and comments are stripped before
        anything is matched — otherwise `# pm.py review` reopened the same hole
        one word wider."""
        self.assertDenied(
            self.bash_payload(f"sleep 60; cat {_REVIEW_MIRROR} # pm.py"),
            "a mention of pm.py in a comment must not exempt a poll",
        )
        self.assertDenied(
            self.bash_payload(f"sleep 60; cat {_REVIEW_MIRROR} # pm.py review"),
            "a full invocation inside a comment must not exempt a poll either",
        )
        # `#` begins a comment at the start of a word, which includes straight
        # after a `;`, `&&` or `(` with no space.
        for joiner in (";#", "&&#", "|#", "(#"):
            with self.subTest(joiner=joiner):
                self.assertDenied(
                    self.bash_payload(f"sleep 60; cat {_REVIEW_MIRROR}{joiner} pm.py review"),
                    f"a comment introduced by {joiner!r} must still be stripped",
                )
        self.assertDenied(
            self.bash_payload(f"python3 pm.py status; sleep 60; cat {_REVIEW_MIRROR}"),
            "an unrelated pm.py subcommand must not exempt a poll",
        )

    def test_a_commented_out_artifact_path_is_not_a_poll(self) -> None:
        """Comment stripping cuts both ways, and this is the harmless direction:
        a path only mentioned in a comment is not being inspected."""
        self.assertAllowed(self.bash_payload(f"sleep 60; echo hi # {_REVIEW_MIRROR}"))

    def test_waiting_on_a_non_pm_target_is_allowed(self) -> None:
        self.assertAllowed(
            self.bash_payload(
                "until [ -s /tmp/pm-cr1.out ] && [ -s /tmp/pm-cr2.out ]; do sleep 30; done; "
                'echo "BOTH DONE"; tail -2 /tmp/pm-cr1.out'
            ),
            "a path outside a PM run is the operator's business",
        )

    def test_a_shape_alike_path_outside_a_run_directory_is_allowed(self) -> None:
        self.assertAllowed(
            self.bash_payload(f"sleep 60; cat /tmp/notes-{_RUN_ID}.md"),
            "the run-id shape alone must not make a path a PM artifact",
        )

    def test_an_unrelated_pm_directory_is_allowed(self) -> None:
        """Matching a bare `/pm/<run-id>/` would deny background work on
        somebody else's data that happens to be laid out that way. The anchor is
        the directory structure the toolkit actually creates."""
        self.assertAllowed(
            self.bash_payload(f"sleep 30; cat /srv/customer/pm/{_RUN_ID}/input.json")
        )

    def test_a_suffix_alike_directory_is_allowed(self) -> None:
        """`archive.pm/runs/...` is not the `.pm/` mirror; the match needs a real
        path-component boundary. Excluding only word characters and dots was not
        enough — a `-`, `~` or `+` prefix walked straight through."""
        for prefix in ("archive.pm", "archive-.pm", "old~.pm", "a+.pm", "archive=.pm"):
            with self.subTest(prefix=prefix):
                self.assertAllowed(
                    self.bash_payload(
                        f"sleep 30; cat {prefix}/runs/{_RUN_ID}/slices/slice-001/x.md"
                    )
                )

    def test_the_real_mirror_is_still_matched_after_a_separator(self) -> None:
        """The boundary must not be so strict that it stops matching the actual
        mirror, which appears after a slash, a space, or a quote."""
        for form in (
            f"/Users/me/repo/.pm/runs/{_RUN_ID}/slices/slice-001/x.md",
            f'"{_REVIEW_MIRROR}"',
            f"'{_REVIEW_MIRROR}'",
        ):
            with self.subTest(form=form):
                self.assertDenied(self.bash_payload(f"sleep 30; cat {form}"))

    def test_a_linked_worktree_state_dir_is_matched(self) -> None:
        """`worktree_git_dir` puts a linked worktree's state under
        `.git/worktrees/<name>/pm/<run-id>/`, so polling there is the same waste
        as polling the ordinary location."""
        self.assertDenied(
            self.bash_payload(
                f"sleep 60; cat .git/worktrees/wt-a/pm/{_RUN_ID}/slices/slice-001/review-1-x.md"
            )
        )

    def test_outside_a_pm_run_bash_is_untouched(self) -> None:
        non_pm = Path(tempfile.mkdtemp(prefix="not-a-pm-run-"))
        self.addCleanup(lambda: non_pm.rmdir() if not any(non_pm.iterdir()) else None)
        self.assertAllowed(
            self.bash_payload(f"until [ -s {_TASK_OUTPUT} ]; do sleep 30; done", cwd=non_pm)
        )

    def test_an_ordinary_backgrounded_command_is_allowed(self) -> None:
        self.assertAllowed(self.bash_payload("pytest -q tests/"))
        self.assertAllowed(self.bash_payload("sleep 30; echo done"))


class TestFailsOpen(PollGuardTestCase):
    """Anything unexpected allows. This is the property that lets the guard ship."""

    def test_malformed_json_allows(self) -> None:
        self.assertAllowed("{not json at all", "a malformed payload must not block a call")

    def test_empty_stdin_allows(self) -> None:
        self.assertAllowed("")

    def test_missing_tool_input_allows(self) -> None:
        self.assertAllowed({"session_id": "s", "cwd": str(self.cwd), "tool_name": "Bash"})

    def test_missing_cwd_allows(self) -> None:
        self.assertAllowed(
            {"session_id": "s", "tool_name": "Bash",
             "tool_input": {"command": f"sleep 9; cat {_TASK_OUTPUT}", "run_in_background": True}}
        )

    def test_a_non_string_command_allows(self) -> None:
        self.assertAllowed(
            {"session_id": "s", "cwd": str(self.cwd), "tool_name": "Bash",
             "tool_input": {"command": ["sleep", "900"], "run_in_background": True}}
        )

    def test_an_unrelated_tool_allows(self) -> None:
        self.assertAllowed(
            {"session_id": "s", "cwd": str(self.cwd), "tool_name": "Write",
             "tool_input": {"file_path": _TASK_OUTPUT, "content": "x"}}
        )

    def test_a_payload_without_tool_name_still_dispatches_by_shape(self) -> None:
        """The matcher in settings.json already selects the tool, so a payload
        that omits `tool_name` must behave as it did before the Bash branch
        existed rather than silently stop guarding."""
        self.assertDenied(
            {"session_id": "s", "cwd": str(self.cwd),
             "tool_input": {"command": f"sleep 900; tail -4 {_TASK_OUTPUT}",
                            "run_in_background": True}}
        )

    def test_an_ambiguous_shape_without_tool_name_allows(self) -> None:
        """Two discriminators and no `tool_name` means the hook cannot tell
        which tool is running. Guessing could deny a call it has not even
        identified, so it allows."""
        path = self.task_output("same\n")
        self.assertAllowed(self.read_payload(path))  # prime the digest stamp
        self.assertAllowed(
            {"session_id": "session-a", "cwd": str(self.cwd),
             "tool_input": {"file_path": path,
                            "command": f"sleep 900; tail -4 {_TASK_OUTPUT}",
                            "run_in_background": True}}
        )

    def test_a_very_large_command_terminates(self) -> None:
        """Guards against a matcher that degrades badly on pathological input;
        the assertion is really the subprocess timeout in `invoke`."""
        self.assertAllowed(self.bash_payload("echo " + ("a" * 200_000)))
        self.assertDenied(
            self.bash_payload(f"sleep 60; cat {_REVIEW_MIRROR} # " + ("b" * 200_000))
        )


if __name__ == "__main__":
    unittest.main()

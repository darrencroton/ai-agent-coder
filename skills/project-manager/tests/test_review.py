"""Protected behaviours: the `review` command (Stage 4).

Pins `pm_lib.prompts.compile_skill_bundle` / `render_reviewer_prompt` (the
transitive skill-bundle embedding spec, re-specified fresh from
`skills/orchestrator/scripts/reviewer_contract.py` as behavioural evidence
only — nothing here imports orchestrator code) and `pm_lib.review` (the
one-shot reviewer command table and the end-to-end `review` command):

1. `compile_skill_bundle("code-review")` against the real `skills/` tree
   embeds SKILL.md *and* every locally-linked Markdown resource — asserted
   by the literal presence of `references/review-matrix.md` content, not
   just SKILL.md's. (SKILL.md-only embedding would silently truncate the
   review contract — the exact defect this test exists to catch.)
2. Path-escape guard: a temporary skill whose SKILL.md links `../outside.md`
   (a target outside the skill's own directory) raises `PmError` naming the
   escaping path, even before checking whether that path exists.
3. A linked-but-missing file raises `PmError`.
4. One-shot reviewer command composition, per tool, from `review.
   compose_reviewer_command` (review.py's own table — never imported from
   orchestrator): codex, claude, copilot compose with optional model/effort
   flags; opencode and qwen compose with an optional model flag but raise
   `PmError` the moment a non-default effort is requested (their tested
   one-shot commands have no effort flag); an unsupported tool name raises
   `PmError`.
5. `render_reviewer_prompt` renders the full contract (pinned range,
   before/after heads, diff path, changed files, contract sections, the
   embedded skill bundle) with no unresolved `{placeholder}` left over.
6. End-to-end via `--reviewer-command` (a fake script that reads the
   rendered prompt as its final argument and prints a report to stdout):
   the review is recorded in state — head, sha256 of the written report,
   and artifact path — the report exists both as the controller-owned
   original (under the state dir) and its `.pm/` mirror, `reviewer_pids`
   is empty again once the subprocess completes, and a `review` event is
   logged.
7. A failing reviewer command (nonzero exit) raises `PmError` quoting the
   captured stderr tail and records nothing: no review entry is appended
   to the slice's `reviews` list and no `review` event is logged.
8. `review` refuses a slice that is not the run's current in-flight slice,
   and refuses when HEAD has not advanced past `before_head` (nothing to
   review).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_test_helpers import PmTestCase, parse_init_output

from pm_lib import PmError
from pm_lib import review as review_mod
from pm_lib import state as state_mod
from pm_lib import prompts

_REAL_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"


def _write_fake_reviewer(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# --- 1-3: transitive skill-bundle embedding ----------------------------------


class TestCompileSkillBundle(unittest.TestCase):
    def test_code_review_bundle_embeds_linked_review_matrix(self) -> None:
        bundle = prompts.compile_skill_bundle("code-review", skills_root=_REAL_SKILLS_ROOT)
        self.assertIn("BEGIN EMBEDDED SKILL FILE:", bundle)
        self.assertIn("name: code-review", bundle)  # SKILL.md frontmatter
        # The literal review-matrix.md content, not just a reference to it.
        self.assertIn("Use this as a required checklist", bundle)
        self.assertIn("review-matrix.md", bundle)

    def test_drift_audit_bundle_embeds_at_least_skill_md(self) -> None:
        bundle = prompts.compile_skill_bundle("drift-audit", skills_root=_REAL_SKILLS_ROOT)
        self.assertIn("BEGIN EMBEDDED SKILL FILE:", bundle)
        self.assertIn("SKILL.md", bundle)

    def test_path_escape_guard_raises_naming_the_escaping_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_root = Path(tmp) / "skills"
            skill_dir = skills_root / "leaky"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: leaky\n---\nSee [outside](../outside.md) for more.\n", encoding="utf-8"
            )
            # Deliberately not created: the escape must be caught before any
            # existence check, so a missing target does not mask it.
            with self.assertRaises(PmError) as ctx:
                prompts.compile_skill_bundle("leaky", skills_root=skills_root)
            self.assertIn("outside.md", str(ctx.exception))
            self.assertIn("escape", str(ctx.exception).lower())

    def test_missing_linked_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_root = Path(tmp) / "skills"
            skill_dir = skills_root / "broken"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: broken\n---\nSee [missing](missing.md) for more.\n", encoding="utf-8"
            )
            with self.assertRaises(PmError) as ctx:
                prompts.compile_skill_bundle("broken", skills_root=skills_root)
            self.assertIn("missing.md", str(ctx.exception))

    def test_missing_skill_raises(self) -> None:
        with self.assertRaises(PmError):
            prompts.compile_skill_bundle("does-not-exist", skills_root=_REAL_SKILLS_ROOT)


# --- 4: one-shot reviewer command composition --------------------------------


class TestComposeReviewerCommand(unittest.TestCase):
    def test_codex(self) -> None:
        command = review_mod.compose_reviewer_command(
            "codex", "PROMPT", model="gpt-5", effort="high", repo=Path("/repo")
        )
        self.assertEqual(
            command,
            [
                "codex", "exec", "PROMPT",
                "-m", "gpt-5",
                "-c", 'model_reasoning_effort="high"',
                "--sandbox", "read-only", "--skip-git-repo-check", "-C", "/repo",
            ],
        )

    def test_codex_omits_absent_model_and_effort(self) -> None:
        command = review_mod.compose_reviewer_command("codex", "PROMPT", repo=Path("/repo"))
        self.assertEqual(
            command,
            ["codex", "exec", "PROMPT", "--sandbox", "read-only", "--skip-git-repo-check", "-C", "/repo"],
        )

    def test_claude(self) -> None:
        command = review_mod.compose_reviewer_command(
            "claude", "PROMPT", model="opus", effort="high", repo=Path("/repo")
        )
        self.assertEqual(
            command,
            [
                "claude", "-p", "PROMPT",
                "--model", "opus", "--effort", "high",
                "--permission-mode", "plan", "--output-format", "text", "--add-dir", "/repo",
            ],
        )

    def test_copilot(self) -> None:
        command = review_mod.compose_reviewer_command(
            "copilot", "PROMPT", model="gpt-5", effort="high", repo=Path("/repo")
        )
        self.assertEqual(
            command,
            [
                "copilot",
                "--model", "gpt-5", "--effort", "high",
                "-p", "PROMPT", "--allow-all-tools", "--autopilot", "--silent", "--add-dir", "/repo",
            ],
        )

    def test_opencode_with_model_no_effort(self) -> None:
        command = review_mod.compose_reviewer_command(
            "opencode", "PROMPT", model="my-model", repo=Path("/repo")
        )
        self.assertEqual(
            command,
            ["opencode", "run", "PROMPT", "-m", "my-model", "--agent", "plan", "--auto", "--dir", "/repo"],
        )

    def test_opencode_effort_fails_closed(self) -> None:
        with self.assertRaises(PmError):
            review_mod.compose_reviewer_command("opencode", "PROMPT", effort="high", repo=Path("/repo"))

    def test_qwen_with_model_no_effort(self) -> None:
        command = review_mod.compose_reviewer_command("qwen", "PROMPT", model="qwen-max", repo=Path("/repo"))
        self.assertEqual(
            command,
            ["qwen", "--prompt", "PROMPT", "--model", "qwen-max", "--sandbox", "--output-format", "text"],
        )

    def test_qwen_effort_fails_closed(self) -> None:
        with self.assertRaises(PmError):
            review_mod.compose_reviewer_command("qwen", "PROMPT", effort="high", repo=Path("/repo"))

    def test_unknown_tool_fails(self) -> None:
        with self.assertRaises(PmError):
            review_mod.compose_reviewer_command("not-a-real-tool", "PROMPT", repo=Path("/repo"))


# --- 5: reviewer prompt rendering --------------------------------------------


class TestRenderReviewerPrompt(unittest.TestCase):
    def test_renders_contract_and_pinned_range_with_no_unresolved_placeholders(self) -> None:
        rendered = prompts.render_reviewer_prompt(
            skill_name="code-review",
            repo="/repo",
            slice_id="Slice 3",
            slice_title="Do the thing",
            before_head="a" * 40,
            reviewed_head="b" * 40,
            diff_path="/repo/.pm/runs/run-a/slices/slice-003/review-input-code-review.patch",
            changed_files=["a.py", "b.py"],
            intended_change="Change the thing.",
            acceptance_criteria="It works.",
            authorized_surface="- a.py",
            explicit_non_goals="Nothing else.",
            risk_flags="- Risky surfaces touched: none.",
            skills_root=_REAL_SKILLS_ROOT,
        )
        self.assertIn("code-review", rendered)
        self.assertIn("/repo", rendered)
        self.assertIn("Slice 3", rendered)
        self.assertIn("Do the thing", rendered)
        self.assertIn("a" * 40, rendered)
        self.assertIn("b" * 40, rendered)
        self.assertIn("review-input-code-review.patch", rendered)
        self.assertIn("a.py", rendered)
        self.assertIn("b.py", rendered)
        self.assertIn("Change the thing.", rendered)
        self.assertIn("It works.", rendered)
        self.assertIn("Nothing else.", rendered)
        self.assertIn("Risky surfaces touched: none.", rendered)
        # The embedded skill bundle is present, not just referenced.
        self.assertIn("BEGIN EMBEDDED SKILL FILE:", rendered)
        self.assertIn("Use this as a required checklist", rendered)
        # No leftover unresolved placeholder field names.
        for field in ("skill_name", "repo", "slice_id", "before_head", "reviewed_head", "diff_path"):
            self.assertNotIn("{" + field + "}", rendered)

    def test_stray_brace_in_custom_template_raises_naming_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken-reviewer-prompt.md"
            path.write_text("```md\nSkill: {skill_name}\nBroken: { not a field }\n```\n", encoding="utf-8")
            with self.assertRaises(PmError) as ctx:
                prompts.render_reviewer_prompt(
                    skill_name="code-review",
                    repo="/repo",
                    slice_id="Slice 1",
                    slice_title="T",
                    before_head="a",
                    reviewed_head="b",
                    diff_path="/x.patch",
                    changed_files=["a.py"],
                    intended_change="x",
                    acceptance_criteria="x",
                    authorized_surface="x",
                    explicit_non_goals="x",
                    risk_flags="x",
                    reference_path=path,
                    skills_root=_REAL_SKILLS_ROOT,
                )
            self.assertIn(str(path), str(ctx.exception))


# --- 6-8: end-to-end review command -------------------------------------------


class ReviewCommandTestCase(PmTestCase):
    def _plan_path(self) -> Path:
        # Kept outside self.repo for the same reason as the slice-ops tests:
        # an untracked plan.md inside the worktree trips the clean-worktree
        # preflight for reasons unrelated to the behaviour under test.
        return self.repo.parent / "plan.md"

    def _init_and_advance(self, *, slices: list[dict] | None = None) -> tuple[str, str, Path]:
        """init a run, wire a fake idle harness, start-slice is not needed —
        review only reads current_slice.id/before_head, so this test suite
        sets current_slice directly (pm_test_helpers.set_current_slice) and
        advances HEAD with a plain git commit, exactly like the floor tests
        do for the same reason (no tmux dependency for this command)."""
        plan_path = self.write_plan(self._plan_path(), slices=slices or [{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(plan_path=plan_path)
        before_head = self._git("rev-parse", "HEAD").stdout.strip()
        return token, before_head, run_dir

    def _advance_head(self, filename: str = "a.py") -> None:
        (self.repo / filename).write_text("changed\n", encoding="utf-8")
        self._git("add", filename)
        self._git("commit", "-q", "-m", "advance head")


class TestReviewEndToEnd(ReviewCommandTestCase):
    def test_successful_fake_reviewer_records_review_and_clears_pids(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        updated = self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer.sh",
            'echo "FAKE REVIEW REPORT"\ntest -n "$1" && echo "received a prompt argument"\nexit 0',
        )

        code, out, err = self.run_cli_in_repo(
            [
                "review", "--slice", "Slice 1", "--skill", "code-review",
                "--tool", "faketool", "--reviewer-command", str(fake),
                "--token", token,
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Slice 1", out)

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        self.assertEqual(len(entry["reviews"]), 1)
        review_record = entry["reviews"][0]
        self.assertEqual(review_record["skill"], "code-review")
        self.assertEqual(review_record["tool"], "faketool")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(review_record["head"], head)
        self.assertEqual(review_record["before_head"], before_head)

        artifact_path = Path(review_record["artifact"])
        self.assertTrue(artifact_path.is_file())
        self.assertTrue(str(artifact_path).startswith(str(run_dir)))
        self.assertEqual(hashlib.sha256(artifact_path.read_bytes()).hexdigest(), review_record["sha256"])
        self.assertIn("FAKE REVIEW REPORT", artifact_path.read_text(encoding="utf-8"))

        mirror_path = self.repo / ".pm" / "runs" / reloaded["run_id"] / "slices" / "slice-001" / artifact_path.name
        self.assertTrue(mirror_path.is_file())
        self.assertEqual(mirror_path.read_bytes(), artifact_path.read_bytes())

        self.assertEqual(reloaded["current_slice"]["reviewer_pids"], [])

        events = state_mod.read_events(run_dir)
        self.assertTrue(any(e["kind"] == "review" for e in events))

    def test_failing_fake_reviewer_records_nothing(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[])
        self._advance_head()

        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_fail.sh",
            'echo "boom" 1>&2\nexit 1',
        )

        code, _out, err = self.run_cli_in_repo(
            [
                "review", "--slice", "Slice 1", "--skill", "code-review",
                "--tool", "faketool", "--reviewer-command", str(fake),
                "--token", token,
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("boom", err)

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        self.assertEqual(entry.get("reviews") or [], [])
        events = state_mod.read_events(run_dir)
        self.assertFalse(any(e["kind"] == "review" for e in events))


class TestReviewRefusals(ReviewCommandTestCase):
    def test_refused_on_non_current_slice(self) -> None:
        token, before_head, run_dir = self._init_and_advance(slices=[{"files": ["a.py"]}, {"files": ["b.py"]}])
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(state, token, run_dir, slice_id="Slice 2", before_head=before_head)
        self._advance_head()

        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "code-review", "--tool", "faketool", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("current", err.lower())

    def test_refused_when_head_equals_before_head(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(state, token, run_dir, slice_id="Slice 1", before_head=before_head)
        # No commit made: HEAD is still before_head.

        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "code-review", "--tool", "faketool", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("nothing to review", err.lower())

    def test_no_tool_configured_and_no_override_fails(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(state, token, run_dir, slice_id="Slice 1", before_head=before_head)
        self._advance_head()

        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "code-review", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("no reviewer tool", err.lower())


# --- reviewer env sanitization, pgid clearing on failure, dirty worktree ------
# --- refusal, and the per-slice reviewer-tool override (new production ------
# --- behaviour pinned here; see module docstring items 6-8 for the ----------
# --- surrounding contract) ----------------------------------------------------


class TestReviewerEnvSanitization(ReviewCommandTestCase):
    def test_reviewer_env_never_contains_run_token(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_envcheck.sh",
            'echo "TOKEN_IS=${PM_RUN_TOKEN:-ABSENT}"\nexit 0',
        )

        previous = os.environ.get("PM_RUN_TOKEN")
        os.environ["PM_RUN_TOKEN"] = "should-never-reach-reviewer"

        def _restore() -> None:
            if previous is None:
                os.environ.pop("PM_RUN_TOKEN", None)
            else:
                os.environ["PM_RUN_TOKEN"] = previous

        self.addCleanup(_restore)

        # The worktree must be clean at review time (a separate, new
        # requirement pinned by TestReviewDirtyWorktreeRefusal below) — the
        # helpers above only commit tracked changes, so nothing here leaves
        # stray untracked files.
        code, out, err = self.run_cli_in_repo(
            [
                "review", "--slice", "Slice 1", "--skill", "code-review",
                "--tool", "faketool", "--reviewer-command", str(fake),
                "--token", token,
            ]
        )
        self.assertEqual(code, 0, err)

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        artifact_path = Path(entry["reviews"][0]["artifact"])
        self.assertIn("TOKEN_IS=ABSENT", artifact_path.read_text(encoding="utf-8"))


class TestReviewerPidsClearedOnFailure(ReviewCommandTestCase):
    def test_failed_reviewer_clears_recorded_process_group(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_fail_pgid.sh",
            'echo "boom" 1>&2\nexit 1',
        )

        code, _out, err = self.run_cli_in_repo(
            [
                "review", "--slice", "Slice 1", "--skill", "code-review",
                "--tool", "faketool", "--reviewer-command", str(fake),
                "--token", token,
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("boom", err)

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        self.assertEqual(entry.get("reviews") or [], [])
        # The failure path must not leave a stale pgid behind for a later
        # `stop` to SIGKILL after PID reuse.
        self.assertEqual(reloaded["current_slice"]["reviewer_pids"], [])


class TestReviewDirtyWorktreeRefusal(ReviewCommandTestCase):
    def test_dirty_worktree_refuses_review(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        # An uncommitted change to a tracked file: HEAD has legitimately
        # advanced past before_head, but the tree the reviewer would read
        # is no longer the pinned committed state.
        (self.repo / "README.md").write_text("dirty content\n", encoding="utf-8")

        code, _out, err = self.run_cli_in_repo(
            ["review", "--slice", "Slice 1", "--skill", "code-review", "--tool", "faketool", "--token", token]
        )
        self.assertEqual(code, 2)
        self.assertIn("dirty", err)

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        self.assertEqual(entry.get("reviews") or [], [])


class TestResolveToolOverride(ReviewCommandTestCase):
    def test_slice_launch_reviewer_tools_override_wins(self) -> None:
        plan_path = self.write_plan(self._plan_path(), slices=[{"files": ["a.py"]}])
        state, token, run_dir = self.make_run(
            plan_path=plan_path, reviewer={"tools": ["claude"], "model": None, "effort": None}
        )
        before_head = self._git("rev-parse", "HEAD").stdout.strip()
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head,
            launch={"reviewer_tools": ["opencode"]},
        )
        reloaded = state_mod.load_state(run_dir, token)

        # No --tool arg: the slice's launch-time override ("opencode") wins
        # over the run-level reviewer.tools configuration ("claude").
        self.assertEqual(review_mod._resolve_tool(reloaded, None, has_override=False), "opencode")
        # An explicit --tool arg still wins over both.
        self.assertEqual(review_mod._resolve_tool(reloaded, "codex", has_override=False), "codex")


if __name__ == "__main__":
    unittest.main()

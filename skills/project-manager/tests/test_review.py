"""Protected behaviours: the `review` command (Stage 4).

Pins `pm_lib.prompts.compile_skill_bundle` / `render_reviewer_prompt` (the
transitive skill-bundle embedding spec, re-specified fresh from
`skills/orchestrator/scripts/delegate_contract.py` as behavioural evidence
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
9. No default timeout: a moderately-slow-but-finite fake reviewer (a few
   seconds, then exit 0) is left to run to completion and recorded, and a
   monkeypatch of `subprocess.Popen.wait` proves the call is made with
   `timeout=None` when `--timeout` is absent.
10. A `--timeout` kill takes the whole process GROUP, not just the leader:
    a fake reviewer backgrounds a child, in the same process group, that
    ignores SIGTERM while the leader does not — proven via
    `os.killpg(pgid, 0)` raising `ProcessLookupError` (the group, not
    merely the leader pid, is gone) after the timeout kill.
11. The `reviewer launched: ...` line reaches stdout before the reviewer
    subprocess completes: a sentinel-gated fake reviewer blocks until a
    file appears, `run_review` runs on a background thread, and the launch
    line is observed while the reviewer is still alive.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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


_LAUNCH_RE = re.compile(r"reviewer launched: pgid=(\d+) report=(\S+) stderr=(\S+)")


class TestReviewTimeout(ReviewCommandTestCase):
    """`--timeout` (target-design §12, Amended post-implementation): the
    launch line (pgid/report/stderr paths) prints before the wait begins,
    and a reviewer that outlives `--timeout` is killed (process group) and
    fails closed rather than being confused with a legitimately slow one."""

    def test_slow_reviewer_times_out_kills_process_and_fails_closed(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_slow.sh",
            'echo "should not finish"\nsleep 30\necho "REPORT"\n',
        )

        code, out, err = self.run_cli_in_repo(
            [
                "review", "--slice", "Slice 1", "--skill", "code-review",
                "--tool", "faketool", "--reviewer-command", str(fake),
                "--token", token, "--timeout", "1",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("timed out", err.lower())
        self.assertIn("not proof", err.lower())

        match = _LAUNCH_RE.search(out)
        self.assertIsNotNone(match, out)
        pgid = int(match.group(1))
        # The kill happens synchronously before `review` returns, but poll
        # briefly for CI-timing safety rather than asserting instantaneously.
        deadline = time.monotonic() + 5.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(pgid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.2)
        self.assertFalse(alive, "reviewer process should be dead after the timeout kill")

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        self.assertEqual(entry.get("reviews") or [], [])
        self.assertEqual(reloaded["current_slice"]["reviewer_pids"], [])

        events = state_mod.read_events(run_dir)
        timeout_events = [
            e for e in events if e["kind"] == "review" and "timed out" in (e.get("note") or "")
        ]
        self.assertEqual(len(timeout_events), 1)

    def test_fast_reviewer_with_generous_timeout_succeeds(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_fast.sh",
            'echo "FAKE REVIEW REPORT"\nexit 0',
        )

        code, out, err = self.run_cli_in_repo(
            [
                "review", "--slice", "Slice 1", "--skill", "code-review",
                "--tool", "faketool", "--reviewer-command", str(fake),
                "--token", token, "--timeout", "30",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(_LAUNCH_RE.search(out), out)

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        self.assertEqual(len(entry["reviews"]), 1)

    def test_timeout_kills_entire_process_group_not_just_leader(self) -> None:
        """Fix-A regression: a SIGTERM-ignoring descendant, in the SAME
        process group as the reviewer leader (no setsid/setpgid call of its
        own — it inherits the leader's pgid), must not survive a timeout
        kill just because the LEADER exits cleanly on SIGTERM.

        The fake reviewer backgrounds a child (via a plain `sh -c ...`, not
        a `(...)` subshell — POSIX guarantees `$$` inside a `(...)`
        subshell stays the PARENT's pid, so that would not give us the
        child's real pid) that ignores SIGTERM. Deterministic ordering,
        with no scheduling race: the child writes its own real pid (`$!`
        from the leader's perspective) to a file and only THEN writes an
        "alive" marker; the LEADER busy-waits on that marker before its own
        long sleep, so by the time `--timeout` can fire the child is
        provably alive and scheduled — removing the race where, if the
        child were never scheduled before a 1s timeout, the test could pass
        vacuously (leader killed, group empty, but no SIGTERM-ignoring
        descendant was ever actually killed). `--timeout 3` gives ample
        margin over the marker busy-wait.

        The foreground (leader) process does NOT trap SIGTERM and so
        terminates promptly on it. Under the bug this guards against, the
        leader's prompt clean exit made the leader-only
        `process.wait(timeout=5)` succeed, which SKIPPED the SIGKILL
        escalation entirely (it was nested inside that wait's own
        `except TimeoutExpired` clause) — leaving the child alive. Proven
        two ways: (1) the child's specific pid, read back from the file it
        wrote, is dead (`os.kill(child_pid, 0)` raises); and (2) the whole
        process GROUP is gone (`os.killpg(pgid, 0)` raises) — not merely
        the leader pid, which alone would say nothing about a surviving
        descendant."""
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        child_marker = self.repo.parent / "child_alive_marker"
        child_pid_file = self.repo.parent / "child_pid"
        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_term_ignoring_child.sh",
            f'sh -c "trap \'\' TERM; echo alive > \'{child_marker}\'; sleep 30" &\n'
            "child_pid=$!\n"
            f'echo "$child_pid" > "{child_pid_file}"\n'
            f'while [ ! -f "{child_marker}" ]; do sleep 0.05; done\n'
            "sleep 30\n",
        )

        code, out, err = self.run_cli_in_repo(
            [
                "review", "--slice", "Slice 1", "--skill", "code-review",
                "--tool", "faketool", "--reviewer-command", str(fake),
                "--token", token, "--timeout", "3",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("timed out", err.lower())

        match = _LAUNCH_RE.search(out)
        self.assertIsNotNone(match, out)
        pgid = int(match.group(1))

        # The marker/pid files existing at all proves the child was really
        # alive and scheduled before the timeout fired — the scheduling
        # race this test guards against.
        self.assertTrue(child_marker.is_file(), "the child must have been alive before the timeout fired")
        self.assertTrue(child_pid_file.is_file(), "the child must have recorded its own pid before the timeout fired")
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())

        deadline = time.monotonic() + 10.0
        group_dead = False
        child_dead = False
        while time.monotonic() < deadline:
            if not group_dead:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    group_dead = True
            if not child_dead:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    child_dead = True
            if group_dead and child_dead:
                break
            time.sleep(0.2)
        self.assertTrue(
            child_dead,
            "the SIGTERM-ignoring child's specific pid must be dead after the timeout kill",
        )
        self.assertTrue(
            group_dead,
            "the entire process GROUP (leader + the SIGTERM-ignoring child) must be "
            "dead after a timeout kill, not merely the leader pid",
        )


class TestReviewNoDefaultTimeout(ReviewCommandTestCase):
    """No `--timeout` means no ceiling at all (target-design §12, Amended
    post-implementation): a legitimately slow cold local model is the PM's
    judgement call, not a hard default."""

    def test_no_timeout_lets_moderately_slow_reviewer_run_to_completion(self) -> None:
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_moderately_slow.sh",
            'sleep 3\necho "FAKE REVIEW REPORT"\nexit 0',
        )

        code, _out, err = self.run_cli_in_repo(
            [
                "review", "--slice", "Slice 1", "--skill", "code-review",
                "--tool", "faketool", "--reviewer-command", str(fake),
                "--token", token,
                # Deliberately no --timeout.
            ]
        )
        self.assertEqual(code, 0, err)

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        self.assertEqual(len(entry["reviews"]), 1)

    def test_no_timeout_passes_none_to_process_wait(self) -> None:
        """Direct proof, not just behavioural inference: with no `--timeout`,
        `run_review` calls the REVIEWER subprocess's `.wait(timeout=...)`
        with `timeout=None` — verified by monkeypatching
        `subprocess.Popen.wait` to record the `timeout` kwarg it is invoked
        with.

        The spy must be isolated to the reviewer's own `Popen` instance: a
        global patch would also observe `run_review`'s several `git`
        subprocesses (`git_ops` uses `subprocess.run`, which internally
        constructs its own `Popen` and calls `self.wait(timeout=...)` via
        `communicate()` — with `timeout=None`, since `git_ops` never passes
        one). Those git waits would satisfy a bare `assertIn(None, ...)`
        even if the REVIEWER's own wait later gained a default timeout,
        making the assertion vacuous. `Popen` always exposes the resolved
        command as the `.args` attribute, so the spy identifies "the
        reviewer's Popen" by checking whether the fake reviewer script's
        own path appears in `.args` — no other subprocess this test spawns
        can match that substring."""
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_wait_spy.sh",
            'echo "FAKE REVIEW REPORT"\nexit 0',
        )
        fake_str = str(fake)

        reviewer_wait_timeouts: list[float | None] = []
        original_wait = subprocess.Popen.wait

        def _spy_wait(self_popen, timeout=None):
            args = self_popen.args
            args_text = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
            if fake_str in args_text:
                reviewer_wait_timeouts.append(timeout)
            return original_wait(self_popen, timeout=timeout)

        with mock.patch.object(subprocess.Popen, "wait", _spy_wait):
            code, _out, err = self.run_cli_in_repo(
                [
                    "review", "--slice", "Slice 1", "--skill", "code-review",
                    "--tool", "faketool", "--reviewer-command", str(fake),
                    "--token", token,
                    # Deliberately no --timeout.
                ]
            )
        self.assertEqual(code, 0, err)
        # Exactly one Popen matched the reviewer (the git subprocesses along
        # the way are filtered out by construction), and its wait received
        # timeout=None.
        self.assertEqual(reviewer_wait_timeouts, [None])


class TestReviewLaunchVisibilityOrdering(ReviewCommandTestCase):
    def _start_sentinel_gated_reviewer(self) -> tuple[threading.Thread, io.StringIO, dict, Path, Path, str]:
        """Prepare a slice ready for review and a sentinel-gated fake
        reviewer that blocks until `sentinel` (its return value) appears,
        then start `run_review` on a background thread capturing its
        stdout into the returned `io.StringIO`. Returns
        `(thread, captured, result, sentinel, run_dir, token)`; `result`
        receives `result["outcome"]` on success or `result["error"]` on
        exception, once the thread completes."""
        token, before_head, run_dir = self._init_and_advance()
        state = state_mod.load_state(run_dir, token)
        self.set_current_slice(
            state, token, run_dir, slice_id="Slice 1", before_head=before_head, reviewer_pids=[]
        )
        self._advance_head()

        sentinel = self.repo.parent / "release_reviewer"
        fake = _write_fake_reviewer(
            self.repo.parent / "fake_reviewer_sentinel.sh",
            f'while [ ! -f "{sentinel}" ]; do sleep 0.1; done\necho "FAKE REVIEW REPORT"\nexit 0',
        )

        captured = io.StringIO()
        result: dict = {}

        def _run() -> None:
            try:
                with redirect_stdout(captured):
                    result["outcome"] = review_mod.run_review(
                        self.repo, run_dir, token,
                        slice_id="Slice 1", skill="code-review", tool="faketool",
                        reviewer_command=str(fake),
                    )
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion below
                result["error"] = exc

        thread = threading.Thread(target=_run)
        thread.start()
        return thread, captured, result, sentinel, run_dir, token

    def _wait_for_launch_line(self, captured: io.StringIO, *, timeout: float = 10.0) -> re.Match | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = _LAUNCH_RE.search(captured.getvalue())
            if match:
                return match
            time.sleep(0.05)
        return None

    def test_launch_line_appears_before_reviewer_completes(self) -> None:
        """The `reviewer launched: ...` line must reach stdout BEFORE the
        (possibly long) subprocess wait completes, not merely before
        `run_review` returns — otherwise a slow-but-alive reviewer is
        indistinguishable from a hung one until it finishes (target-design
        §12, Amended post-implementation). Sentinel-gated: the fake
        reviewer blocks until a sentinel file appears, `run_review` runs on
        a background thread, and the launch line is asserted present while
        the reviewer subprocess is still alive (thread still running)."""
        thread, captured, result, sentinel, run_dir, token = self._start_sentinel_gated_reviewer()
        try:
            match = self._wait_for_launch_line(captured)
            self.assertIsNotNone(match, captured.getvalue())
            # The reviewer is still blocked on the sentinel here: the launch
            # line reached stdout BEFORE the subprocess completed, not just
            # before `run_review` returned.
            self.assertTrue(thread.is_alive(), "reviewer already finished before the launch line was observed")
        finally:
            sentinel.write_text("go\n", encoding="utf-8")
            thread.join(timeout=10.0)

        self.assertNotIn("error", result, result.get("error"))
        outcome = result["outcome"]
        self.assertEqual(outcome.slice_id, "Slice 1")

        reloaded = state_mod.load_state(run_dir, token)
        entry = reloaded["slices"][0]
        self.assertEqual(len(entry["reviews"]), 1)

    def test_reviewer_pgid_persisted_to_state_before_wait_begins(self) -> None:
        """Fix-B regression: the reviewer's pgid must be written to disk
        (`run.json`, via `reviewer_pids`) BEFORE `run_review` blocks on the
        subprocess wait — not merely by the time it returns — so a
        concurrent `stop` can reap a hung reviewer mid-run.

        This is deterministic, not a timing race: in `run_review`,
        `state_mod.save_state(...)` (persisting the pgid) happens
        strictly before the `print(...)` of the launch line, in the same
        thread of execution — so the instant we observe the launch line in
        captured stdout, the save has already definitely happened. We also
        assert the reviewer thread is still alive (still blocked on the
        sentinel, i.e. still inside — or not yet past — the wait) at the
        moment we read the state back from disk, so this cannot be
        satisfied by a `run_review` that happened to finish first."""
        thread, captured, result, sentinel, run_dir, token = self._start_sentinel_gated_reviewer()
        try:
            match = self._wait_for_launch_line(captured)
            self.assertIsNotNone(match, captured.getvalue())
            pgid = int(match.group(1))
            self.assertTrue(thread.is_alive(), "reviewer already finished before we checked persisted state")

            reloaded = state_mod.load_state(run_dir, token)
            self.assertIn(pgid, reloaded["current_slice"]["reviewer_pids"])
        finally:
            sentinel.write_text("go\n", encoding="utf-8")
            thread.join(timeout=10.0)

        self.assertNotIn("error", result, result.get("error"))


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

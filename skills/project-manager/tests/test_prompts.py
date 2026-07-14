"""Developer-prompt and repair-prompt rendering tests."""

from pm_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the pm module


class PromptRenderingTests(PmTestCase):
    def test_prompt_rendering_includes_frozen_contract(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        plan_slice = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = pm.render_developer_prompt(state, plan_slice, slice_artifact_dir, run_json)
        self.assertIn("Selected slice: Slice 1 - First Slice", prompt)
        self.assertIn("Authorized surface:", prompt)
        self.assertIn("README.md", prompt)
        self.assertIn("developer-result.json", prompt)
        self.assertIn(str(pm.skill_root() / "references" / "run-state-schema.md"), prompt)
        self.assertIn(str(pm.reviewer_jobs_path()), prompt)
        self.assertIn(str(slice_artifact_dir / "reviewer-runs"), prompt)
        self.assertIn(str(slice_artifact_dir / "tmp"), prompt)
        self.assertIn(str(slice_artifact_dir / "tool-homes"), prompt)
        self.assertIn(str(slice_artifact_dir / "copilot-home"), prompt)
        self.assertIn('run_dir="$(python3 ', prompt)
        self.assertIn('launch --run-dir "$run_dir"', prompt)
        self.assertIn("Embedded PM slice delegation contract:", prompt)
        self.assertIn("Project Manager Slice Reviewer Contract", prompt)
        self.assertIn("reviewer-evidence.md", prompt)
        self.assertIn("Available reviewer tool(s) for this run: none available for this run", prompt)
        self.assertNotIn(str(run_json), prompt)
        self.assertIn("Controller state is not a developer input", prompt)
        self.assertNotIn("ai-pm-control", prompt)

    def test_prompt_rendering_states_available_reviewer_tools_for_delegation(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        plan_slice = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = pm.render_developer_prompt(state, plan_slice, slice_artifact_dir, run_json, ("codex",))
        self.assertIn("Available reviewer tool(s) for this run: codex", prompt)
        self.assertIn("which reviewer PM has made available for delegation", prompt)

    def test_prompt_mirrors_mode_a_delegate_for_independence_with_local_fallback(self):
        # The Mode B per-slice prompt must read like the Mode A launcher: prefer
        # delegating the drift-audit and code-review to a separate model for
        # independence, fall back to a local self-audit when no reviewer is
        # available (a valid accepted outcome), and always keep the gate with
        # the developer. It must also name the opt-in independence gate.
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        plan_slice = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = pm.render_developer_prompt(state, plan_slice, slice_artifact_dir, run_json, ("codex",))
        self.assertIn("Mode B counterpart of the Mode A", prompt)
        self.assertIn("Prefer delegating this as a hostile, independent audit to the available reviewer", prompt)
        self.assertIn("Prefer delegating code review as an independent review to the available reviewer", prompt)
        self.assertIn("perform the drift-audit locally yourself", prompt)
        self.assertIn("perform the review locally yourself", prompt)
        self.assertIn("You still hold every gate", prompt)
        self.assertIn("Independent audit required: yes", prompt)
        self.assertIn("do not launch code review unless the authorization verdict is `PASS`", prompt)
        self.assertIn("Do not launch drift-audit and code-review reviewers in parallel", prompt)
        self.assertIn("final code-review verdict to be exactly `PASS`", prompt)
        self.assertIn("residual_findings", prompt)

    def test_prompt_states_audit_skill_reminder_only_on_opt_in_slice(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        base = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        default_prompt = pm.render_developer_prompt(state, base, slice_artifact_dir, run_json)
        self.assertNotIn("never `[]` and never both skills in one request", default_prompt)

        sections = dict(base.sections)
        sections["Risk Flags"] = sections.get("Risk Flags", "") + "\n- Independent audit required: yes"
        opt_in_slice = pm.PlanSlice(base.number, base.title, base.body, sections)
        opt_in_prompt = pm.render_developer_prompt(state, opt_in_slice, slice_artifact_dir, run_json)
        self.assertIn('exactly `["drift-audit"]` or exactly `["code-review"]`', opt_in_prompt)
        self.assertIn("never `[]` and never both skills in one request", opt_in_prompt)

    def test_prompt_local_audit_is_valid_when_no_reviewer_available(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        plan_slice = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = pm.render_developer_prompt(state, plan_slice, slice_artifact_dir, run_json)
        self.assertIn("none available for this run", prompt)
        self.assertIn("that is a valid, accepted outcome, not a failure", prompt)

    def test_prompt_configured_reviewer_failure_falls_back_only_on_default_slice(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        base = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"

        default_prompt = pm.render_developer_prompt(state, base, slice_artifact_dir, run_json, ("codex",))
        self.assertIn("cannot launch or cannot honor its authentication, model, or effort contract", default_prompt)
        self.assertIn("preserve the exact failure in `reviewer-evidence.md`", default_prompt)
        self.assertIn("On a default slice, then perform the affected audit(s) locally as Developer self-audit", default_prompt)
        self.assertIn("the failed reviewer attempt is evidence, not a blocker", default_prompt)

        sections = dict(base.sections)
        sections["Risk Flags"] = sections.get("Risk Flags", "") + "\n- Independent audit required: yes"
        opt_in_slice = pm.PlanSlice(base.number, base.title, base.body, sections)
        opt_in_prompt = pm.render_developer_prompt(state, opt_in_slice, slice_artifact_dir, run_json, ("codex",))
        self.assertIn("do not substitute Developer self-audit", opt_in_prompt)
        self.assertIn("preserve the failure and stop", opt_in_prompt)
        self.assertIn("record the blocker in `developer-result.json` and stop", opt_in_prompt)

    def test_prompt_rendering_states_reviewer_model_and_effort(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        plan_slice = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = pm.render_developer_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            ("codex",),
            "gpt-5.5",
            "low",
        )
        self.assertIn("Available reviewer model for this run: gpt-5.5", prompt)
        self.assertIn("Available reviewer effort for this run: low", prompt)
        self.assertIn('"model": "gpt-5.5"', prompt)
        self.assertIn('"effort": "low"', prompt)
        self.assertIn("Do not construct or invoke a Reviewer harness command yourself", prompt)

    def test_prompt_rendering_embeds_compact_pm_delegation_contract(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        plan_slice = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = pm.render_developer_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            ("claude", "copilot"),
            "some-model",
            "medium",
        )
        self.assertIn("Project Manager Slice Reviewer Contract", prompt)
        self.assertIn('"model": "some-model"', prompt)
        self.assertIn('"effort": "medium"', prompt)
        self.assertNotIn("Reviewer model/effort guidance:", prompt)
        self.assertNotIn("references/claude.md", prompt)
        self.assertNotIn("references/codex.md", prompt)
        self.assertLess(len(prompt.split()), 4000)

    def test_repair_prompt_covers_every_repairable_signature(self):
        # Every repairable signature must render a complete prompt (no
        # KeyError/IndexError from stray braces) that states the slice is not
        # accepted, quotes the gate reason, re-anchors the authorized surface,
        # and repeats the invariant instructions.
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        from pm_lib.gates import REPAIRABLE_SIGNATURES

        # One distinctive stanza marker per repairable signature, so a wrong
        # stanza selection cannot pass on the shared invariants alone.
        stanza_markers = {
            "validation": "Fix only the validation gap",
            "drift": "Fix only the drift audit gap",
            "review": "Fix only the code review gap",
            "reviewer-evidence": "Fix only the reviewer evidence gap",
            "unauthorized-files": "restore-only",
            "changed-files-mismatch": "No file edits are needed",
            "result-malformed": "valid JSON matching the required schema",
            "commit-missing": "commit skill",
            "dirty-worktree": "uncommitted changes outside `.ai-pm/`",
            "developer-repairable": "You reported status `repairable` yourself",
            "residual-ledger-mismatch": "copy every legitimate non-blocking post-plan consideration",
        }
        self.assertEqual(set(stanza_markers), set(REPAIRABLE_SIGNATURES))

        for signature in sorted(REPAIRABLE_SIGNATURES):
            gate = pm.GateDecision(
                "repairable",
                f"gate reason for {signature} with literal {{braces}} kept",
                None,
                ("README.md",),
                signature=signature,
            )
            prompt = pm_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head="a" * 40)
            self.assertIn("NOT accepted", prompt, signature)
            self.assertIn(f"gate reason for {signature} with literal {{braces}} kept", prompt)
            self.assertIn(f"category: {signature}", prompt)
            self.assertIn(stanza_markers[signature], prompt, signature)
            self.assertIn("- README.md", prompt)
            self.assertIn("Do not change any other file.", prompt)
            self.assertIn("developer-result.json", prompt)
            self.assertIn("git rev-parse HEAD", prompt)
            self.assertIn("Slice 1", prompt)
            self.assertIn("Delegation posture remains unchanged", prompt)
            self.assertIn("Preserve and update `residual_findings`", prompt)

    def test_repair_prompt_reviewer_evidence_preserves_existing_work(self):
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = pm.GateDecision(
            "repairable",
            "required reviewer tool(s) (opencode) were never actually invoked",
            None,
            ("README.md",),
            signature="reviewer-evidence",
        )
        prompt = pm_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head="a" * 40)
        self.assertIn("do NOT re-implement", prompt)
        self.assertIn("reviewer evidence", prompt)
        self.assertIn("were never actually invoked", prompt)

    def test_repair_prompt_unauthorized_files_is_restore_only(self):
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        before = "b" * 40
        gate = pm.GateDecision(
            "repairable",
            "unauthorized changed files: EVIL.md",
            None,
            ("EVIL.md", "README.md"),
            signature="unauthorized-files",
        )
        prompt = pm_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head=before)
        self.assertIn("OUTSIDE your authorized surface: EVIL.md", prompt)
        self.assertIn(f"git checkout {before} -- EVIL.md", prompt)
        self.assertIn("touch nothing else", prompt)
        # The authorized file must not be named in the restore command.
        self.assertNotIn(f"git checkout {before} -- EVIL.md README.md", prompt)

    def test_repair_prompt_unauthorized_files_quotes_awkward_paths(self):
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        before = "c" * 40
        gate = pm.GateDecision(
            "repairable",
            "unauthorized changed files: bad name.md, glob*.md",
            None,
            ("bad name.md", "glob*.md"),
            signature="unauthorized-files",
        )
        prompt = pm_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head=before)
        # Paths with spaces or metacharacters must survive a literal copy of
        # the restore command as single arguments.
        self.assertIn(f"git checkout {before} -- 'bad name.md' 'glob*.md'", prompt)

    def test_repair_prompt_changed_files_mismatch_needs_no_edits(self):
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = pm.GateDecision(
            "repairable",
            "developer changed_files does not match git evidence",
            None,
            ("README.md",),
            signature="changed-files-mismatch",
        )
        prompt = pm_runtime.render_repair_prompt(plan_slice, artifact, gate)
        self.assertIn("No file edits are needed", prompt)
        self.assertIn("exactly match the actual diff: README.md", prompt)

    def test_repair_prompt_dirty_worktree_lists_meaningful_status(self):
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "git-status-after.txt").write_text("M  README.md\n?? .ai-pm/scratch.txt\n", encoding="utf-8")
        gate = pm.GateDecision(
            "repairable",
            "post-commit worktree is dirty outside .ai-pm/",
            None,
            ("README.md",),
            signature="dirty-worktree",
        )
        prompt = pm_runtime.render_repair_prompt(plan_slice, artifact, gate)
        self.assertIn("M  README.md", prompt)
        self.assertNotIn(".ai-pm/scratch.txt", prompt)

    def test_git_status_text_preserves_leading_space_on_first_line(self):
        # `git status --short` is positional: " M file" (unstaged modify)
        # starts with a meaningful space. A stripped read shifted the first
        # line's path parse by one character ("EADME.md").
        self.prepare_committed_repo()
        (self.repo / "seed.txt").write_text("modified but unstaged\n", encoding="utf-8")
        status_text = pm.git_status_text(self.repo)
        self.assertTrue(status_text.startswith(" M "), repr(status_text.splitlines()[0]))
        self.assertEqual(pm.status_changed_files(status_text), {"seed.txt"})

    def test_repair_prompt_fails_closed_on_unknown_signature(self):
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = pm.GateDecision("repairable", "reason", None, (), signature="mystery")
        with self.assertRaisesRegex(pm.PmError, "no repair stanza"):
            pm_runtime.render_repair_prompt(plan_slice, artifact, gate)

    def test_repair_template_does_not_change_main_prompt_template(self):
        # The repair block is a second fenced template in the same reference
        # file; the main loader must still pick the original block.
        template = pm.load_prompt_template()
        self.assertIn("You are the slice Developer for Project Manager.", template)
        self.assertNotIn("NOT accepted", template)
        repair = pm_runtime.load_repair_template()
        self.assertIn("NOT accepted", repair)
        self.assertNotIn("Reviewer helper sequence", repair)

    def test_rendered_prompt_states_claude_reviewer_auth_policy(self):
        plan_slice = pm.parse_plan(self.plan)[0]
        state = self.init_run()
        artifact_dir = Path("/tmp/artifacts")
        run_json = Path("/tmp/run.json")
        prompt = pm.render_developer_prompt(state, plan_slice, artifact_dir, run_json, ("claude",))
        self.assertIn("Available reviewer tool(s) for this run: claude", prompt)
        self.assertIn("Reviewer auth policy:", prompt)
        self.assertIn("PM does not set CLAUDE_CONFIG_DIR", prompt)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", prompt)


if __name__ == "__main__":
    unittest.main()

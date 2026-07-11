"""Orchestrator-prompt and repair-prompt rendering tests."""

from mc_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the mc module


class PromptRenderingTests(McTestCase):
    def test_prompt_rendering_includes_frozen_contract(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(state, plan_slice, slice_artifact_dir, run_json)
        self.assertIn("Selected slice: Slice 1 - First Slice", prompt)
        self.assertIn("Authorized surface:", prompt)
        self.assertIn("README.md", prompt)
        self.assertIn("orchestrator-result.json", prompt)
        self.assertIn(str(mc.skill_root() / "references" / "run-state-schema.md"), prompt)
        self.assertIn(str(mc.worker_jobs_path()), prompt)
        self.assertIn(str(slice_artifact_dir / "worker-runs"), prompt)
        self.assertIn(str(slice_artifact_dir / "tmp"), prompt)
        self.assertIn(str(slice_artifact_dir / "tool-homes"), prompt)
        self.assertIn(str(slice_artifact_dir / "copilot-home"), prompt)
        self.assertIn('run_dir="$(python3 ', prompt)
        self.assertIn('launch --run-dir "$run_dir"', prompt)
        self.assertIn("Embedded ai-orchestrator instructions:", prompt)
        self.assertIn("Deterministic Worker Contract", prompt)
        self.assertIn("worker-evidence.md", prompt)
        self.assertIn("Required worker tool(s) for this run: none configured for this run", prompt)

    def test_prompt_rendering_states_configured_worker_tools_authoritatively(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(state, plan_slice, slice_artifact_dir, run_json, ("codex",))
        self.assertIn("Required worker tool(s) for this run: codex", prompt)
        self.assertIn("Every configured tool is required to complete", prompt)

    def test_prompt_rendering_states_worker_model_and_effort(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            ("codex",),
            "gpt-5.5",
            "low",
        )
        self.assertIn("Required worker model for this run: gpt-5.5", prompt)
        self.assertIn("Required worker effort for this run: low", prompt)
        self.assertIn('"model": "gpt-5.5"', prompt)
        self.assertIn('"effort": "low"', prompt)
        self.assertIn("Do not construct or invoke a worker harness command yourself", prompt)

    def test_prompt_rendering_embeds_ai_orchestrator_instead_of_worker_flag_guidance(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            ("claude", "copilot"),
            "some-model",
            "medium",
        )
        self.assertIn("BEGIN EMBEDDED SKILL FILE:", prompt)
        self.assertIn("name: ai-orchestrator", prompt)
        self.assertIn('"model": "some-model"', prompt)
        self.assertIn('"effort": "medium"', prompt)
        self.assertNotIn("Worker model/effort guidance:", prompt)

    def test_repair_prompt_covers_every_repairable_signature(self):
        # Every repairable signature must render a complete prompt (no
        # KeyError/IndexError from stray braces) that states the slice is not
        # accepted, quotes the gate reason, re-anchors the authorized surface,
        # and repeats the invariant instructions.
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        from mc_lib.gates import REPAIRABLE_SIGNATURES

        # One distinctive stanza marker per repairable signature, so a wrong
        # stanza selection cannot pass on the shared invariants alone.
        stanza_markers = {
            "validation": "Fix only the validation gap",
            "drift": "Fix only the drift audit gap",
            "review": "Fix only the code review gap",
            "worker-evidence": "Fix only the worker evidence gap",
            "unauthorized-files": "restore-only",
            "changed-files-mismatch": "No file edits are needed",
            "result-malformed": "valid JSON matching the required schema",
            "commit-missing": "commit skill",
            "dirty-worktree": "uncommitted changes outside `.ai-mc/`",
            "orchestrator-repairable": "You reported status `repairable` yourself",
        }
        self.assertEqual(set(stanza_markers), set(REPAIRABLE_SIGNATURES))

        for signature in sorted(REPAIRABLE_SIGNATURES):
            gate = mc.GateDecision(
                "repairable",
                f"gate reason for {signature} with literal {{braces}} kept",
                None,
                ("README.md",),
                signature=signature,
            )
            prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head="a" * 40)
            self.assertIn("NOT accepted", prompt, signature)
            self.assertIn(f"gate reason for {signature} with literal {{braces}} kept", prompt)
            self.assertIn(f"category: {signature}", prompt)
            self.assertIn(stanza_markers[signature], prompt, signature)
            self.assertIn("- README.md", prompt)
            self.assertIn("Do not change any other file.", prompt)
            self.assertIn("orchestrator-result.json", prompt)
            self.assertIn("git rev-parse HEAD", prompt)
            self.assertIn("Slice 1", prompt)

    def test_repair_prompt_worker_evidence_preserves_existing_work(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = mc.GateDecision(
            "repairable",
            "required worker tool(s) (opencode) were never actually invoked",
            None,
            ("README.md",),
            signature="worker-evidence",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head="a" * 40)
        self.assertIn("do NOT re-implement", prompt)
        self.assertIn("worker evidence", prompt)
        self.assertIn("were never actually invoked", prompt)

    def test_repair_prompt_unauthorized_files_is_restore_only(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        before = "b" * 40
        gate = mc.GateDecision(
            "repairable",
            "unauthorized changed files: EVIL.md",
            None,
            ("EVIL.md", "README.md"),
            signature="unauthorized-files",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head=before)
        self.assertIn("OUTSIDE your authorized surface: EVIL.md", prompt)
        self.assertIn(f"git checkout {before} -- EVIL.md", prompt)
        self.assertIn("touch nothing else", prompt)
        # The authorized file must not be named in the restore command.
        self.assertNotIn(f"git checkout {before} -- EVIL.md README.md", prompt)

    def test_repair_prompt_unauthorized_files_quotes_awkward_paths(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        before = "c" * 40
        gate = mc.GateDecision(
            "repairable",
            "unauthorized changed files: bad name.md, glob*.md",
            None,
            ("bad name.md", "glob*.md"),
            signature="unauthorized-files",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head=before)
        # Paths with spaces or metacharacters must survive a literal copy of
        # the restore command as single arguments.
        self.assertIn(f"git checkout {before} -- 'bad name.md' 'glob*.md'", prompt)

    def test_repair_prompt_changed_files_mismatch_needs_no_edits(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = mc.GateDecision(
            "repairable",
            "orchestrator changed_files does not match git evidence",
            None,
            ("README.md",),
            signature="changed-files-mismatch",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate)
        self.assertIn("No file edits are needed", prompt)
        self.assertIn("exactly match the actual diff: README.md", prompt)

    def test_repair_prompt_dirty_worktree_lists_meaningful_status(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "git-status-after.txt").write_text("M  README.md\n?? .ai-mc/scratch.txt\n", encoding="utf-8")
        gate = mc.GateDecision(
            "repairable",
            "post-commit worktree is dirty outside .ai-mc/",
            None,
            ("README.md",),
            signature="dirty-worktree",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate)
        self.assertIn("M  README.md", prompt)
        self.assertNotIn(".ai-mc/scratch.txt", prompt)

    def test_git_status_text_preserves_leading_space_on_first_line(self):
        # `git status --short` is positional: " M file" (unstaged modify)
        # starts with a meaningful space. A stripped read shifted the first
        # line's path parse by one character ("EADME.md").
        self.prepare_committed_repo()
        (self.repo / "seed.txt").write_text("modified but unstaged\n", encoding="utf-8")
        status_text = mc.git_status_text(self.repo)
        self.assertTrue(status_text.startswith(" M "), repr(status_text.splitlines()[0]))
        self.assertEqual(mc.status_changed_files(status_text), {"seed.txt"})

    def test_repair_prompt_fails_closed_on_unknown_signature(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = mc.GateDecision("repairable", "reason", None, (), signature="mystery")
        with self.assertRaisesRegex(mc.McError, "no repair stanza"):
            mc_runtime.render_repair_prompt(plan_slice, artifact, gate)

    def test_repair_template_does_not_change_main_prompt_template(self):
        # The repair block is a second fenced template in the same reference
        # file; the main loader must still pick the original block.
        template = mc.load_prompt_template()
        self.assertIn("You are the slice orchestrator for Master Controller.", template)
        self.assertNotIn("NOT accepted", template)
        repair = mc_runtime.load_repair_template()
        self.assertIn("NOT accepted", repair)
        self.assertNotIn("Worker helper sequence", repair)

    def test_rendered_prompt_states_claude_worker_auth_policy(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        state = self.init_run()
        artifact_dir = Path("/tmp/artifacts")
        run_json = Path("/tmp/run.json")
        prompt = mc.render_orchestrator_prompt(state, plan_slice, artifact_dir, run_json, ("claude",))
        self.assertIn("Required worker tool(s) for this run: claude", prompt)
        self.assertIn("Worker auth policy:", prompt)
        self.assertIn("MC does not set CLAUDE_CONFIG_DIR", prompt)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", prompt)


if __name__ == "__main__":
    unittest.main()

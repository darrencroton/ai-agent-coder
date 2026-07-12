"""Tmux adapter, harness profile, readiness, preflight, and credential tests."""

from mc_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the mc module


class HarnessAdapterProfileTests(McTestCase):
    def test_adapter_command_construction_exports_mc_environment(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        adapter = mc.TmuxHarnessAdapter("codex", "python fake.py")
        command = adapter.build_shell_command(Path("/tmp/artifacts"), Path("/tmp/run.json"), self.plan, plan_slice)
        self.assertIn("AI_ORCHESTRATOR_ARTIFACT_ROOT=/tmp/artifacts/worker-runs", command)
        # Tool homes are redirected only for that tool as a worker; with no
        # worker tools configured no home redirect may leak into the launch.
        self.assertNotIn("COPILOT_HOME=", command)
        self.assertNotIn("CODEX_HOME=", command)
        self.assertIn("MC_RESULT_SCHEMA_PATH=", command)
        self.assertIn("MC_SLICE_ARTIFACT_DIR=/tmp/artifacts", command)
        self.assertIn("MC_SLICE_ID='Slice 1'", command)
        self.assertIn("MC_SLICE_TMP_DIR=/tmp/artifacts/tmp", command)
        self.assertIn("MC_TOOL_HOME_ROOT=/tmp/artifacts/tool-homes", command)
        self.assertIn("MC_WORKER_JOBS_PATH=", command)
        self.assertIn("MC_WORKER_POLICY_PATH=/tmp/artifacts/worker-policy.json", command)
        self.assertIn("TMPDIR=/tmp/artifacts/tmp", command)
        self.assertTrue(command.endswith("python fake.py"))

    def test_codex_profile_command_composes_worker_and_commit_requirements(self):
        self.prepare_committed_repo()
        state = self.init_run()
        command = mc.profile_command("codex", self.repo, state, ("copilot",))
        self.assertIn("codex --no-alt-screen -s workspace-write -a never", command)
        self.assertIn("sandbox_workspace_write.network_access=true", command)
        self.assertIn("--add-dir", command)
        self.assertIn(str(mc.git_access_path(self.repo)), command)

    def test_claude_profile_command_composes_model_and_session_id(self):
        self.prepare_committed_repo()
        state = self.init_run()
        command = mc.profile_command("claude", self.repo, state, ("codex",), "fixed-session-id", "sonnet")
        parts = shlex.split(command)
        self.assertEqual(parts, ["claude", "--permission-mode", "auto", "--model", "sonnet", "--session-id", "fixed-session-id"])

    def test_codex_profile_command_composes_model_override(self):
        self.prepare_committed_repo()
        state = self.init_run()
        command = mc.profile_command("codex", self.repo, state, (), harness_model="some-model")
        parts = shlex.split(command)
        self.assertIn("-m", parts)
        self.assertEqual(parts[parts.index("-m") + 1], "some-model")

    def test_codex_profile_command_composes_effort_override(self):
        self.prepare_committed_repo()
        state = self.init_run()
        command = mc.profile_command("codex", self.repo, state, (), harness_effort="medium")
        parts = shlex.split(command)
        self.assertIn("-c", parts)
        self.assertIn('model_reasoning_effort="medium"', parts)

    def test_harness_model_requires_profile_command(self):
        self.prepare_committed_repo()
        state = self.init_run()
        args = argparse.Namespace(harness_command=None, allow_profile_command=False, worker_tools="", harness_model="sonnet")
        with self.assertRaisesRegex(mc.McError, "only supported with --allow-profile-command"):
            mc.resolve_harness_command(args, self.repo, state)

    def test_harness_effort_requires_profile_command(self):
        self.prepare_committed_repo()
        state = self.init_run()
        args = argparse.Namespace(harness_command=None, allow_profile_command=False, worker_tools="", harness_effort="medium")
        with self.assertRaisesRegex(mc.McError, "only supported with --allow-profile-command"):
            mc.resolve_harness_command(args, self.repo, state)

    def test_copilot_profile_composes_orchestrator_command(self):
        self.prepare_committed_repo()
        state = self.init_run()
        command = mc.profile_command("copilot", self.repo, state, (), harness_model="claude-sonnet-4.6")
        parts = shlex.split(command)
        self.assertEqual(
            parts,
            ["copilot", "--allow-all-tools", "--autopilot", "--model", "claude-sonnet-4.6"],
        )

    def test_opencode_profile_composes_orchestrator_command(self):
        self.prepare_committed_repo()
        state = self.init_run()
        command = mc.profile_command(
            "opencode", self.repo, state, (), harness_model="macstudio/qwen/qwen3.6-27b-q8", harness_effort="high"
        )
        parts = shlex.split(command)
        self.assertEqual(
            parts,
            ["opencode", "--auto", "-m", "macstudio/qwen/qwen3.6-27b-q8", "--variant", "high"],
        )

    def test_codex_unattended_default_uses_no_alt_screen(self):
        adapter = mc.TmuxHarnessAdapter("codex", None, allow_unattended_default=True)
        self.assertEqual(adapter.command, "codex --no-alt-screen -s workspace-write -a never")

    def test_opencode_unattended_default(self):
        adapter = mc.TmuxHarnessAdapter("opencode", None, allow_unattended_default=True)
        self.assertEqual(adapter.command, "opencode --auto")

    def test_copilot_unattended_default(self):
        adapter = mc.TmuxHarnessAdapter("copilot", None, allow_unattended_default=True)
        self.assertEqual(adapter.command, "copilot --allow-all-tools --autopilot")

    def test_opencode_readiness_wait_blocks_on_trust_prompt(self):
        adapter = mc.TmuxHarnessAdapter("opencode", "opencode")
        calls = [
            mc.CommandResult(0, "", ""),
            mc.CommandResult(0, "Do you trust the files in this directory?", ""),
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls), mock.patch.object(mc_tmux_adapter.time, "sleep"):
            with self.assertRaisesRegex(mc.McError, "trust prompt"):
                adapter.wait_until_prompt_ready("session")

    def test_opencode_readiness_wait_accepts_ready_composer(self):
        adapter = mc.TmuxHarnessAdapter("opencode", "opencode")
        calls = [
            mc.CommandResult(0, "", ""),
            mc.CommandResult(0, 'Ask anything... "Fix broken tests"', ""),
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls), mock.patch.object(mc_tmux_adapter.time, "sleep") as sleep:
            adapter.wait_until_prompt_ready("session")
        sleep.assert_called()

    def test_copilot_readiness_wait_blocks_on_trust_prompt(self):
        adapter = mc.TmuxHarnessAdapter("copilot", "copilot")
        calls = [
            mc.CommandResult(0, "", ""),
            mc.CommandResult(0, "Do you trust the files in this folder?", ""),
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls), mock.patch.object(mc_tmux_adapter.time, "sleep"):
            with self.assertRaisesRegex(mc.McError, "trust prompt"):
                adapter.wait_until_prompt_ready("session")

    # Copilot's positive stable-pane readiness path has no unit test here,
    # matching claude's existing test coverage (mocking time.sleep as a no-op
    # makes real time.monotonic() spin through the stability window too fast
    # to exercise reliably). Both were verified manually against a live tmux
    # session; see the notes on the copilot HARNESS_PROFILES entry.

    def test_codex_readiness_wait_blocks_on_trust_prompt(self):
        adapter = mc.TmuxHarnessAdapter("codex", "codex")
        calls = [
            mc.CommandResult(0, "", ""),
            mc.CommandResult(0, "Do you trust the contents of this directory?", ""),
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls), mock.patch.object(mc_tmux_adapter.time, "sleep"):
            with self.assertRaisesRegex(mc.McError, "trust prompt"):
                adapter.wait_until_prompt_ready("session")

    def test_codex_readiness_wait_accepts_ready_composer(self):
        adapter = mc.TmuxHarnessAdapter("codex", "codex")
        calls = [
            mc.CommandResult(0, "", ""),
            mc.CommandResult(0, "OpenAI Codex\n\n› Summarize recent commits", ""),
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls), mock.patch.object(mc_tmux_adapter.time, "sleep") as sleep:
            adapter.wait_until_prompt_ready("session")
        sleep.assert_called()

    def test_adapter_detect_activity_reports_pane_changes(self):
        adapter = mc.TmuxHarnessAdapter("codex", "python fake.py")
        calls = [
            mc.CommandResult(0, "", ""),
            mc.CommandResult(0, "new pane text", ""),
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls):
            activity = adapter.detect_activity("session", "old pane text")
        self.assertTrue(activity["running"])
        self.assertTrue(activity["active"])
        self.assertEqual(activity["capture"], "new pane text")

    def test_adapter_detect_activity_reports_stopped_session(self):
        adapter = mc.TmuxHarnessAdapter("codex", "python fake.py")
        with mock.patch.object(mc_tmux_adapter, "run_command", return_value=mc.CommandResult(1, "", "missing")):
            activity = adapter.detect_activity("session", "old pane text")
        self.assertFalse(activity["running"])
        self.assertFalse(activity["active"])
        self.assertEqual(activity["capture"], "")

    def test_adapter_send_literal_uses_literal_input_and_robust_submit(self):
        adapter = mc.TmuxHarnessAdapter("codex", "codex")
        calls = [
            mc.CommandResult(0, "", ""),  # session_exists
            mc.CommandResult(0, "ready", ""),  # pane capture
            mc.CommandResult(0, "", ""),  # literal send
            mc.CommandResult(0, "", ""),  # first submit
            mc.CommandResult(0, "", ""),  # second submit
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls) as run, mock.patch.object(mc_tmux_adapter.time, "sleep"):
            adapter.send_literal("session", "continue; $(no shell)")
        self.assertEqual(run.call_args_list[2].args[0], ["tmux", "send-keys", "-t", "session", "-l", "continue; $(no shell)"])
        self.assertEqual(run.call_args_list[3].args[0], ["tmux", "send-keys", "-t", "session", "C-m"])
        self.assertEqual(run.call_args_list[4].args[0], ["tmux", "send-keys", "-t", "session", "C-m"])

    def test_adapter_send_literal_refuses_hard_prompt(self):
        adapter = mc.TmuxHarnessAdapter("codex", "codex")
        calls = [
            mc.CommandResult(0, "", ""),
            mc.CommandResult(0, "Approve this action before continuing", ""),
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls), mock.patch.object(mc_tmux_adapter.time, "sleep"):
            with self.assertRaisesRegex(mc.McError, "hard prompt"):
                adapter.send_literal("session", "continue")

    def test_adapter_lists_sessions_by_run_prefix(self):
        adapter = mc.TmuxHarnessAdapter("codex", "codex")
        result = mc.CommandResult(0, "mc_run_slice-001_a1\nother\nmc_run_slice-002_a1\n", "")
        with mock.patch.object(mc_tmux_adapter, "run_command", return_value=result):
            self.assertEqual(adapter.sessions_with_prefix("mc_run_"), ["mc_run_slice-001_a1", "mc_run_slice-002_a1"])

    def test_adapter_session_helpers_tolerate_missing_tmux(self):
        adapter = mc.TmuxHarnessAdapter("codex", "codex")
        destination = Path(self.tmp.name) / "capture.txt"
        with mock.patch.object(mc_tmux_adapter.shutil, "which", return_value=None):
            self.assertFalse(adapter.session_exists("session"))
            self.assertEqual(adapter.sessions_with_prefix("mc_run_"), [])
            adapter.capture("session", destination)
        self.assertIn("tmux was unavailable", destination.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for preflight test")
    def test_preflight_passes_with_explicit_harness_command(self):
        self.prepare_committed_repo()
        self.init_run()
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=sys.executable,
            worker_tools="",
            allow_profile_command=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(mc.preflight(args), 0)
        self.assertIn("Preflight passed.", output.getvalue())

    def test_seed_worker_credentials_copies_codex_auth_when_requested(self):
        fake_codex_home = Path(self.tmp.name) / "fake-codex-home"
        fake_codex_home.mkdir()
        (fake_codex_home / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
        slice_artifact_dir = Path(self.tmp.name) / "slice-001"
        paths = mc.slice_paths(slice_artifact_dir)
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(fake_codex_home)}):
            warnings = mc.seed_worker_credentials(paths, ("codex",), "claude")
        self.assertEqual(warnings, [])
        seeded = paths["codex_home"] / "auth.json"
        self.assertEqual(seeded.read_text(encoding="utf-8"), '{"token": "secret"}')
        self.assertEqual(seeded.stat().st_mode & 0o777, 0o600)

    def test_seed_worker_credentials_skips_when_tool_is_orchestrator_itself(self):
        fake_codex_home = Path(self.tmp.name) / "fake-codex-home"
        fake_codex_home.mkdir()
        (fake_codex_home / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
        slice_artifact_dir = Path(self.tmp.name) / "slice-001"
        paths = mc.slice_paths(slice_artifact_dir)
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(fake_codex_home)}):
            warnings = mc.seed_worker_credentials(paths, ("codex",), "codex")
        self.assertEqual(warnings, [])
        self.assertFalse((paths["codex_home"] / "auth.json").exists())

    def test_seed_worker_credentials_warns_when_source_missing(self):
        fake_codex_home = Path(self.tmp.name) / "missing-codex-home"
        slice_artifact_dir = Path(self.tmp.name) / "slice-001"
        paths = mc.slice_paths(slice_artifact_dir)
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(fake_codex_home)}):
            warnings = mc.seed_worker_credentials(paths, ("codex",), "claude")
        self.assertEqual(len(warnings), 1)
        self.assertIn("codex worker credential source not found", warnings[0])

    def test_slice_environment_isolates_worker_home_but_not_orchestrators_own(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact_dir = Path("/tmp/artifacts")
        run_json = Path("/tmp/run.json")
        claude_orchestrator_env = mc.slice_environment(artifact_dir, run_json, self.plan, plan_slice, "claude", ("codex",))
        self.assertEqual(claude_orchestrator_env["CODEX_HOME"], str(artifact_dir / "codex-home"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", claude_orchestrator_env)

        codex_orchestrator_env = mc.slice_environment(artifact_dir, run_json, self.plan, plan_slice, "codex", ("codex",))
        self.assertNotIn("CODEX_HOME", codex_orchestrator_env)

        codex_with_claude_worker_env = mc.slice_environment(artifact_dir, run_json, self.plan, plan_slice, "codex", ("claude",))
        self.assertNotIn("CLAUDE_CONFIG_DIR", codex_with_claude_worker_env)

        no_worker_env = mc.slice_environment(artifact_dir, run_json, self.plan, plan_slice)
        self.assertNotIn("CODEX_HOME", no_worker_env)
        self.assertNotIn("CLAUDE_CONFIG_DIR", no_worker_env)

    def test_profile_command_claude_appends_session_id(self):
        self.prepare_committed_repo()
        state = self.init_run()
        command = mc.profile_command("claude", self.repo, state, (), "fixed-session-id")
        self.assertIn("--session-id fixed-session-id", command)

    def test_profile_command_claude_composes_model_effort_and_session_id(self):
        self.prepare_committed_repo()
        state = self.init_run()
        command = mc.profile_command(
            "claude",
            self.repo,
            state,
            (),
            "fixed-session-id",
            harness_model="sonnet",
            harness_effort="medium",
        )
        self.assertIn("--model sonnet", command)
        self.assertIn("--effort medium", command)
        self.assertIn("--session-id fixed-session-id", command)

    def test_capture_orchestrator_transcript_copies_existing_session_file(self):
        slice_artifact_dir = Path(self.tmp.name) / "slice-001"
        slice_artifact_dir.mkdir()
        session_id = "abc-123"
        expected_source = Path(self.tmp.name) / "claude-project" / f"{session_id}.jsonl"
        expected_source.parent.mkdir(parents=True)
        expected_source.write_text('{"type": "user"}\n', encoding="utf-8")
        with mock.patch.object(mc_runtime, "claude_orchestrator_transcript_path", return_value=expected_source):
            mc.capture_orchestrator_transcript("claude", self.repo, session_id, slice_artifact_dir)
        self.assertEqual(
            (slice_artifact_dir / "orchestrator-transcript.jsonl").read_text(encoding="utf-8"),
            '{"type": "user"}\n',
        )
        self.assertFalse((slice_artifact_dir / "orchestrator-transcript-note.txt").exists())

    def test_capture_orchestrator_transcript_notes_when_session_file_missing(self):
        slice_artifact_dir = Path(self.tmp.name) / "slice-001"
        slice_artifact_dir.mkdir()
        missing_source = Path(self.tmp.name) / "claude-project" / "missing.jsonl"
        with mock.patch.object(mc_runtime, "claude_orchestrator_transcript_path", return_value=missing_source):
            mc.capture_orchestrator_transcript("claude", self.repo, "some-id", slice_artifact_dir)
        self.assertFalse((slice_artifact_dir / "orchestrator-transcript.jsonl").exists())
        note = (slice_artifact_dir / "orchestrator-transcript-note.txt").read_text(encoding="utf-8")
        self.assertIn("orchestrator transcript not found", note)

    def test_capture_orchestrator_transcript_noop_for_non_claude_harness(self):
        slice_artifact_dir = Path(self.tmp.name) / "slice-001"
        slice_artifact_dir.mkdir()
        mc.capture_orchestrator_transcript("codex", self.repo, "some-id", slice_artifact_dir)
        self.assertFalse((slice_artifact_dir / "orchestrator-transcript.jsonl").exists())
        self.assertFalse((slice_artifact_dir / "orchestrator-transcript-note.txt").exists())

    def test_preflight_checks_worker_credential_source(self):
        self.prepare_committed_repo()
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="claude", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        missing_codex_home = Path(self.tmp.name) / "missing-codex-home"
        preflight_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=None,
            worker_tools="codex",
            allow_profile_command=True,
        )
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(missing_codex_home)}):
            with contextlib.redirect_stdout(output):
                result = mc.preflight(preflight_args)
        self.assertEqual(result, 2)
        self.assertIn("codex worker credential source", output.getvalue())

    def test_preflight_skips_credential_check_when_worker_tool_is_orchestrator(self):
        self.prepare_committed_repo()
        state = self.init_run()
        missing_codex_home = Path(self.tmp.name) / "missing-codex-home"
        preflight_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=None,
            worker_tools="codex",
            allow_profile_command=True,
        )
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(missing_codex_home)}):
            with contextlib.redirect_stdout(output):
                mc.preflight(preflight_args)
        self.assertNotIn("codex worker credential source", output.getvalue())

    def test_preflight_fails_when_opt_in_slice_has_no_worker(self):
        # An opt-in slice ("Independent audit required: yes") with no
        # --worker-tools configured must fail at preflight, so the operator
        # learns at setup time instead of only at the finalize gate.
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8").replace(
                "- Approval needed before implementation: no.",
                "- Approval needed before implementation: no.\n- Independent audit required: yes.",
                1,
            ),
            encoding="utf-8",
        )
        self.prepare_committed_repo()
        self.init_run()
        preflight_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=None,
            worker_tools="",
            allow_profile_command=True,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = mc.preflight(preflight_args)
        self.assertEqual(result, 2)
        self.assertIn("independent-audit worker available", output.getvalue())


    # --- Review fixes: fail-closed parsing -------------------------------

    def test_tool_homes_marked_sensitive(self):
        self.assertIn("tool-homes", mc.SENSITIVE_ARTIFACT_NAMES)

    def test_claude_readiness_blocks_on_trust_prompt(self):
        adapter = mc.TmuxHarnessAdapter("claude", "claude")
        calls = [
            mc.CommandResult(0, "", ""),  # session_exists
            mc.CommandResult(0, "Do you trust the files in this folder?", ""),  # pane capture
        ]
        with mock.patch.object(mc_tmux_adapter, "run_command", side_effect=calls), mock.patch.object(mc_tmux_adapter.time, "sleep"):
            with self.assertRaisesRegex(mc.McError, "trust prompt"):
                adapter._wait_claude_ready("session")

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for preflight parity test")
    def test_preflight_flags_bare_interactive_harness(self):
        self.prepare_committed_repo()
        self.init_run()
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=None,
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(mc.preflight(args), 2)
        self.assertIn("harness launch resolves", output.getvalue())
        self.assertIn("deadlock", output.getvalue())

    def test_codex_ready_falls_back_to_stable_pane_when_banner_missing(self):
        # A codex CLI update that rewords its banner must degrade to the
        # stable-pane heuristic instead of hard-failing every launch.
        class FakeTime:
            def __init__(self):
                self.now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += max(float(seconds), 0.01)

        adapter = mc.TmuxHarnessAdapter("codex", "python fake.py")
        with mock.patch.object(mc_tmux_adapter, "time", FakeTime()):
            with mock.patch.object(adapter, "session_exists", return_value=True):
                with mock.patch.object(adapter, "_pane_text", return_value="new codex ui without the old banner"):
                    adapter.wait_until_prompt_ready("some-session")

    def test_worker_jobs_module_exposes_claude_project_root(self):
        module = mc.worker_jobs_module()
        self.assertTrue(hasattr(module, "claude_project_root"))


if __name__ == "__main__":
    unittest.main()

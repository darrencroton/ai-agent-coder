"""Model-supervised primitives and repair/circuit-breaker state tests."""

import time

from pm_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the pm module


class SupervisionRepairTests(PmTestCase):
    def test_cancel_run_reviewers_scans_current_and_prior_slice_artifacts(self):
        run_dir = self.repo / ".ai-pm" / "runs" / "test"
        first = run_dir / "slices" / "slice-001"
        second = run_dir / "slices" / "slice-002"
        first.mkdir(parents=True)
        second.mkdir(parents=True)

        with mock.patch.object(pm_runtime, "cancel_reviewer_runs", side_effect=[[{"slice": 1}], [{"slice": 2}]]) as cancel, mock.patch.object(
            pm_runtime, "capture_reviewer_runs_summary"
        ) as capture:
            results = pm.cancel_run_reviewers(run_dir)

        self.assertEqual(results, [{"slice": 1}, {"slice": 2}])
        self.assertEqual(cancel.call_args_list, [mock.call(first), mock.call(second)])
        self.assertEqual(capture.call_args_list, [mock.call(first), mock.call(second)])

    def test_cancel_reviewer_runs_terminates_tracked_wrapper_and_child_group(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        run_dir = artifact / "reviewer-runs" / "reviewers-test"
        run_dir.mkdir(parents=True)
        reviewer_jobs = pm.reviewer_jobs_module()
        reviewer_jobs.ensure_manifest(run_dir)
        launched = reviewer_jobs.start_tracked_reviewer(
            run_dir,
            "01-python-long-reviewer",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=self.repo,
        )
        status_path = Path(launched["status_file"])
        deadline = time.time() + 5
        while not status_path.exists() and time.time() < deadline:
            time.sleep(0.05)

        results = pm.cancel_reviewer_runs(artifact)

        self.assertEqual(results[0]["returncode"], 0, results)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "cancelled")
        reviewer_jobs._LIBRARY_WRAPPERS.pop(int(launched["pid"])).wait(timeout=5)
        self.assertFalse(reviewer_jobs.process_running(int(launched["pid"])))

    def test_idle_stall_signature_uses_shared_repair_escalation(self):
        repair = pm_state.default_repair_state()
        gate = pm.GateDecision("repairable", "idle", signature="idle-no-progress")
        first, terminal = pm.resolve_repair_action(repair, gate.signature, True, 3, gate, "Slice 1")
        self.assertEqual((first, terminal), ("in-session", None))
        second, terminal = pm.resolve_repair_action(repair, gate.signature, True, 3, gate, "Slice 1")
        self.assertEqual((second, terminal), ("fresh-session", None))
        third, terminal = pm.resolve_repair_action(repair, gate.signature, True, 3, gate, "Slice 1")
        self.assertEqual(third, "terminal")
        self.assertEqual(terminal.status, "needs-human")
        self.assertIn("circuit breaker", terminal.reason)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_start_wait_finalize_records_pass(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake_harness.py"
        write_fake_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=10,
            poll_seconds=0.1,
            reason="test",
            until=pm.utc_now(),
            buffer_seconds=0,
            status="needs-human",
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        before_start = git(self.repo, "rev-parse", "HEAD")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.start_slice(command_args), 0)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        running = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["supervision"]["mode"], "model-supervised")
        self.assertEqual(running["current_slice"]["before_head"], before_start)
        self.assertEqual(running["current_slice"]["launch_config"]["harness_command"], command_args.harness_command)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.wait(command_args), 0)
        final_output = io.StringIO()
        with contextlib.redirect_stdout(final_output):
            final_code = pm.finalize_slice(command_args)
        self.assertEqual(final_code, 0, final_output.getvalue())
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "partial")
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["status"], "pass")
        self.assertEqual(state["slices"][0]["changed_files"], ["README.md"])
        self.assertTrue((run_dir / "slices" / "slice-001" / "observation-latest.json").exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_usage_limit_pause_resume_trial_records_pass(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "usage_limit_resume_harness.py"
        write_usage_limit_resume_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=3,
            poll_seconds=0.1,
            reason="rolling usage reset",
            until=pm.utc_now(),
            buffer_seconds=0,
            text="You were interrupted. Review what you were doing then continue.",
            status="needs-human",
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.start_slice(command_args), 0)
        wait_output = io.StringIO()
        with contextlib.redirect_stdout(wait_output):
            self.assertEqual(pm.wait(command_args), 0)
        first_wait = json.loads(wait_output.getvalue())
        self.assertEqual(first_wait["wait_status"], "timeout")
        usage_hint = next(hint for hint in first_wait["observation"]["operational_hints"] if hint["kind"] == "usage_limit")
        self.assertEqual(usage_hint["subtype"], "rolling_window")
        self.assertFalse(usage_hint["hard_stop"])
        self.assertEqual(usage_hint["recovery_guidance"], "pause-until-reset-plus-buffer-then-send-continuation")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.pause_until(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.send(command_args), 0)
        command_args.seconds = 10
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.finalize_slice(command_args), 0)

        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["slices"][0]["status"], "pass")
        self.assertEqual(state["slices"][0]["changed_files"], ["README.md"])
        events = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("pause", [event["kind"] for event in events])
        self.assertIn("send", [event["kind"] for event in events])

    def test_model_supervised_usage_limit_process_exit_requires_finalize_or_stop(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["supervision"]["mode"] = "model-supervised"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm.utc_now(),
            "before_head": git(self.repo, "rev-parse", "HEAD"),
            "pause": None,
            "reviewer_tools": [],
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
            "prior_slice_context": self.prior_context_metadata(artifact),
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {
            "running": False,
            "active": False,
            "capture": "Usage limit reached. Try again in 1 minute.",
        }
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("Usage limit reached. Try again in 1 minute.\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=10,
            poll_seconds=0.1,
            reason="rolling usage reset",
            until=pm.utc_now(),
            buffer_seconds=0,
            status="needs-human",
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            wait_output = io.StringIO()
            with contextlib.redirect_stdout(wait_output):
                self.assertEqual(pm.wait(command_args), 0)
            wait_result = json.loads(wait_output.getvalue())
            self.assertEqual(wait_result["wait_status"], "process-exited")
            usage_hint = next(hint for hint in wait_result["observation"]["operational_hints"] if hint["kind"] == "usage_limit")
            self.assertEqual(usage_hint["recovery_guidance"], "restart-from-clean-authorized-state-or-stop-for-user")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.finalize_slice(command_args), 2)
        state = json.loads((((self.repo / ".ai-pm" / "current").resolve()) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("developer result missing", state["stop_reason"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_finalize_blocks_missing_result_after_process_exit(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "no_result_harness.py"
        write_no_result_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.finalize_slice(command_args), 2)
        state = json.loads((((self.repo / ".ai-pm" / "current").resolve()) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("developer result missing", state["stop_reason"])

    def test_wait_observing_policy_flag_gates_hard_signals(self):
        # The one shared wait loop serves both drivers; stop_on_hard_signals
        # is the per-driver policy. True (model-supervised) breaks on a hard
        # prompt so the model can judge it; False (batch) keeps polling —
        # detection markers are broad substring matches, and the safety
        # boundary is send-time refusal, not the wait. The activity log is
        # appended on every poll either way.
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {
            "running": True,
            "active": True,
            "capture": "Do you trust the files in this folder?\n",
        }
        fake_adapter.detect_hard_prompt.side_effect = pm.TmuxHarnessAdapter.detect_hard_prompt
        wait_args = self._finalize_args()
        wait_args.poll_seconds = 0.05
        activity_log = artifact / "activity-attempt-1.jsonl"
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            reason, snapshot = pm_observation.wait_observing(
                wait_args, self.repo.resolve(), run_dir, 5, activity_log=activity_log
            )
            self.assertEqual(reason, "hard-prompt")
            self.assertTrue(snapshot["prompt_on_screen"]["present"])
            # Breaking on the first poll still records exactly one activity
            # line: the audit trail must not depend on winning a race.
            first_wait_lines = activity_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(first_wait_lines), 1)
            batch_reason, _snapshot = pm_observation.wait_observing(
                wait_args, self.repo.resolve(), run_dir, 0.2, activity_log=activity_log, stop_on_hard_signals=False
            )
        self.assertEqual(batch_reason, "timeout")
        lines = activity_log.read_text(encoding="utf-8").splitlines()
        # The batch wait appends per poll, not per call: a 0.2s wait at a
        # 0.05s cadence must add several lines before timing out.
        self.assertGreaterEqual(len(lines) - len(first_wait_lines), 2)
        for line in lines:
            self.assertEqual(set(json.loads(line)), {"active", "checked_at", "running"})

    def test_start_slice_rerun_seeds_repair_generation_from_attempt(self):
        # A rerun of a previously failed slice starts at attempt 2; the repair
        # session generation must seed from that real attempt, or a later
        # fresh-session relaunch would increment 1 -> 2 and collide with this
        # attempt's own session and artifact names.
        self.prepare_committed_repo()
        state = self.init_run()
        state["slices"].append(
            pm.slice_entry_from_gate(
                self.repo,
                pm.parse_plan(self.plan)[0],
                (self.repo / ".ai-pm" / "current").resolve() / "slices" / "slice-001",
                pm.utc_now(),
                pm.GateDecision("failed", "prior attempt", {"changed_files": []}, ()),
                git(self.repo, "rev-parse", "HEAD"),
                repair=pm_state.default_repair_state(),
                reviewer_policy={"sha256": "a" * 64, "policy": {}},
            )
        )
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        fake_adapter = mock.Mock()
        fake_adapter.sessions_with_prefix.return_value = []
        fake_adapter.harness_name = "codex"
        fake_adapter.allow_unattended_default = False
        fake_adapter.command_override = "python fake.py"
        fake_adapter.command = "python fake.py"
        args = argparse.Namespace(
            harness_command="python fake.py",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            result = pm.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)
        self.assertTrue(result["started"])
        self.assertEqual(result["attempt"], 2)
        self.assertTrue(result["tmux_session"].endswith("_a2"))
        persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["current_slice"]["repair"]["session_generation"], 2)
        self.assertEqual(persisted["current_slice"]["repair"]["round"], 0)

    def test_finalize_keeps_session_alive_on_repairable_gate(self):
        # A repairable PM gate with budget remaining must not tear the session
        # down: no force_stop, no slice entry, current_slice kept, status set
        # to the send-eligible `resuming`, and the repair prompt surfaced. The
        # current slice carries the required explicit round-zero repair state.
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        self._write_failing_validation_result(artifact)
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        output = io.StringIO()
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(output):
                self.assertEqual(pm.finalize_slice(self._finalize_args()), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["finalized"])
        self.assertEqual(result["status"], "repairable")
        self.assertEqual(result["mode"], "in-session")
        self.assertIn("NOT accepted", result["send_text"])
        self.assertNotIn("\n", result["send_text"])
        fake_adapter.force_stop.assert_not_called()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "resuming")
        self.assertEqual(state["slices"], [])
        self.assertEqual(state["current_slice"]["repair"], {
            "round": 1,
            "last_signature": "validation",
            "signature_streak": 1,
            "session_generation": 1,
        })
        self.assertTrue((artifact / "repair-prompt.md").exists())
        self.assertTrue((artifact / "repair-prompt-repair-1.md").exists())
        # The stale failing result was archived so a re-finalize cannot
        # instantly re-read it.
        self.assertTrue((artifact / "developer-result-repair-1.json").exists())
        self.assertFalse((artifact / "developer-result.json").exists())
        events = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual(len(repair_events), 1)
        self.assertEqual(repair_events[0]["mode"], "in-session")
        self.assertEqual(repair_events[0]["signature"], "validation")
        self.assertEqual(repair_events[0]["round"], 1)

    def test_fresh_session_repair_prompt_preserves_archived_context_ledgers(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(
            state,
            run_dir,
            repair={"round": 1, "last_signature": "validation", "signature_streak": 1, "session_generation": 1},
        )
        finding = {
            "source": "validation",
            "severity": "low",
            "summary": "A pre-existing warning should remain visible",
            "disposition": "pre-existing",
            "rationale": "The warning predates this slice and does not affect its acceptance criteria.",
            "suggested_follow_up": "Review the warning after the plan completes.",
        }
        note = {
            "category": "validation-lesson",
            "summary": "The targeted validator emits a harmless pre-existing warning.",
            "rationale": "Later slices should distinguish it from a new regression.",
            "applies_to": "remaining validation slices",
        }
        self._write_failing_validation_result(artifact)
        result_path = artifact / "developer-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["residual_findings"] = [finding]
        result["continuation_notes"] = [note]
        result_path.write_text(json.dumps(result), encoding="utf-8")

        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        output = io.StringIO()
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(output):
                self.assertEqual(pm.finalize_slice(self._finalize_args()), 0)

        finalized = json.loads(output.getvalue())
        self.assertEqual(finalized["mode"], "fresh-session")
        fresh_prompt = artifact / "fresh-session-prompt-repair-2.md"
        self.assertEqual(finalized["repair_prompt_path"], str(fresh_prompt.relative_to(self.repo.resolve())))
        prompt_text = fresh_prompt.read_text(encoding="utf-8")
        self.assertIn("developer-result-repair-2.json", prompt_text)
        self.assertIn(finding["summary"], prompt_text)
        self.assertIn(note["summary"], prompt_text)
        self.assertIn("must retain every item", prompt_text)
        self.assertIn("Retain these decisions and lessons", prompt_text)
        fake_adapter.send_prompt.assert_called_once_with(finalized["tmux_session"], fresh_prompt)

    def test_start_slice_persists_reviewer_tools_for_later_finalize(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        fake_adapter = mock.Mock()
        fake_adapter.sessions_with_prefix.return_value = []
        fake_adapter.harness_name = "codex"
        fake_adapter.allow_unattended_default = False
        fake_adapter.command_override = "python fake.py"
        fake_adapter.command = "python fake.py"
        args = argparse.Namespace(
            harness_command="python fake.py",
            reviewer_tools="opencode",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            result = pm.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)
        self.assertTrue(result["started"])
        persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["current_slice"]["reviewer_tools"], ["opencode"])
        policy = json.loads((run_dir / "slices" / "slice-001" / "reviewer-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["required_tools"], ["opencode"])
        self.assertEqual(policy["slice_id"], "Slice 1")
        self.assertEqual(policy["plan_sha256"], state["plan"]["sha256"])
        prompt = (run_dir / "slices" / "slice-001" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("Project Manager Slice Reviewer Contract", prompt)
        self.assertIn("reviewer_jobs.py launch", prompt)

    def test_reviewer_policy_makes_role_and_access_intrinsic(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        sections = dict(plan_slice.sections)
        sections["Validation Plan"] += "\n- Reviewer evidence: run one bounded read-only support check."
        read_only_slice = pm.PlanSlice(plan_slice.number, plan_slice.title, plan_slice.body, sections)
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        policy_path = pm.write_reviewer_policy(state, read_only_slice, artifact, ("opencode",), "model", None)
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["schema_version"], 2)
        self.assertNotIn("allowed_access", policy)
        self.assertNotIn("allowed_roles", policy)

    def test_generated_reviewer_policy_matches_orchestrator_schema_v2(self):
        state = self.init_run()
        (self.repo / "README.md").write_text("review context\n", encoding="utf-8")
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        policy_path = pm.write_reviewer_policy(state, plan_slice, artifact, ("opencode",), "model", None)
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        reviewer_run = artifact / "reviewer-runs" / "reviewers-contract-test"
        reviewer_run.mkdir(parents=True)
        request = {
            "schema_version": 2,
            "label": "01-opencode-review-docs",
            "slice_id": plan_slice.slice_id,
            "plan_sha256": state["plan"]["sha256"],
            "tool": "opencode",
            "model": "model",
            "effort": "default",
            "task": "Review the requested documentation evidence.",
            "context": "Read-only contract integration test.",
            "required_skills": [],
            "files": ["README.md"],
            "constraints": ["Do not edit files."],
            "expected_output": "RESULT: PASS or FAIL with evidence.",
        }
        contract_path = PM_PATH.parents[2] / "orchestrator" / "scripts" / "reviewer_contract.py"
        spec = importlib.util.spec_from_file_location("pm_policy_reviewer_contract", contract_path)
        reviewer_contract = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = reviewer_contract
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(reviewer_contract)

        normalized = reviewer_contract.validate_contract(policy, request, reviewer_run)

        self.assertEqual(normalized["role"], "reviewer")
        self.assertEqual(normalized["access"], "read-only")

    def test_reviewer_policy_reserves_audit_skill_sets_only_on_opt_in_slice(self):
        # Regression: PM Test 2 found a Developer could launch a
        # required-audit reviewer with an empty required_skills. reviewer-policy.json
        # now carries the exact reserved combinations on an opt-in slice, and
        # none on a default slice, so the launcher's pre-launch check has
        # something to enforce against.
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        base = pm.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)

        default_policy_path = pm.write_reviewer_policy(state, base, artifact, ("opencode",), "model", None)
        default_policy = json.loads(default_policy_path.read_text(encoding="utf-8"))
        self.assertEqual(default_policy["reserved_skill_sets"], [])

        sections = dict(base.sections)
        sections["Risk Flags"] = sections.get("Risk Flags", "") + "\n- Independent audit required: yes"
        opt_in_slice = pm.PlanSlice(base.number, base.title, base.body, sections)
        self.assertTrue(opt_in_slice.independent_audit_required)
        opt_in_policy_path = pm.write_reviewer_policy(state, opt_in_slice, artifact, ("opencode",), "model", None)
        opt_in_policy = json.loads(opt_in_policy_path.read_text(encoding="utf-8"))
        self.assertEqual(opt_in_policy["reserved_skill_sets"], [["drift-audit"], ["code-review"]])

    def test_stop_with_evidence_records_terminal_slice_attempt(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("unaccepted work\n", encoding="utf-8")
        state["status"] = "running"
        state["current_slice"] = pm.current_slice_state(
            self.repo.resolve(),
            plan_slice,
            artifact,
            "pm_test_slice-001_a1",
            1,
            pm.utc_now(),
            before,
            reviewer_tools=("opencode",),
            reviewer_policy={"sha256": "a" * 64, "policy": {}},
            prior_slice_context=self.prior_context_metadata(artifact),
        )
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            reason="reviewer contract violation",
            status="needs-human",
            harness_command="python fake.py",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with (
            mock.patch.object(pm_commands, "_current_adapter", return_value=fake_adapter),
            mock.patch.object(
                pm_commands,
                "_capture_git_evidence",
                wraps=pm_commands._capture_git_evidence,
            ) as capture_git_evidence,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(pm.stop_with_evidence(args), 0)
        capture_git_evidence.assert_called_once_with(self.repo.resolve(), artifact, 1, before)
        stopped = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertIsNone(stopped["current_slice"])
        self.assertEqual(stopped["status"], "needs-human")
        self.assertEqual(len(stopped["slices"]), 1)
        self.assertEqual(stopped["slices"][0]["status"], "needs-human")
        self.assertEqual(stopped["slices"][0]["changed_files"], ["README.md"])
        self.assertEqual(stopped["slices"][0]["gate_reason"], "reviewer contract violation")

    def test_stop_with_evidence_recovers_from_corrupted_worktree_state(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["current_slice"] = pm.current_slice_state(
            self.repo.resolve(),
            plan_slice,
            artifact,
            "pm_test_slice-001_a1",
            1,
            pm.utc_now(),
            git(self.repo, "rev-parse", "HEAD"),
            reviewer_policy={"sha256": "a" * 64, "policy": {}},
            prior_slice_context=self.prior_context_metadata(artifact),
        )
        pm.activate_controller_state(run_dir / "run.json", state)
        (run_dir / "run.json").write_text("{corrupted", encoding="utf-8")
        fake_adapter = mock.Mock()
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            reason="controller integrity breach",
            status="needs-human",
            harness_command="python fake.py",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
            harness_effort=None,
            reviewer_model=None,
            reviewer_effort=None,
        )
        output = io.StringIO()

        with mock.patch.object(pm_commands, "_current_adapter", return_value=fake_adapter), contextlib.redirect_stdout(output):
            self.assertEqual(pm.stop_with_evidence(args), 0)

        result = json.loads(output.getvalue())
        self.assertTrue(result["controller_state_recovered"])
        self.assertTrue(Path(result["tamper_evidence_path"]).is_file())
        stopped = pm.load_run(run_dir)
        self.assertEqual(stopped["status"], "needs-human")
        self.assertIsNone(stopped["current_slice"])
        fake_adapter.force_stop.assert_called_once_with("pm_test_slice-001_a1")

    def test_stop_with_evidence_halts_when_both_state_copies_are_unreadable(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        pm.activate_controller_state(run_dir / "run.json", state)
        (run_dir / "run.json").write_text("{broken mirror", encoding="utf-8")
        controller_path = pm.controller_state_path(run_dir)
        self.assertIsNotNone(controller_path)
        controller_path.write_text("{broken controller", encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.sessions_with_prefix.return_value = ["pm_test_slice-001_a1"]

        def fake_capture(session_name, destination):
            destination.write_text(f"captured {session_name}\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            reason="unreadable state",
            status="needs-human",
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
            harness_effort=None,
            reviewer_model=None,
            reviewer_effort=None,
        )
        output = io.StringIO()

        with mock.patch.object(pm_commands, "TmuxHarnessAdapter", return_value=fake_adapter), mock.patch.object(
            pm_commands, "cancel_run_reviewers", return_value=[]
        ) as cancel_reviewers, contextlib.redirect_stdout(output):
            self.assertEqual(pm.stop_with_evidence(args), 0)

        result = json.loads(output.getvalue())
        self.assertFalse(result["state_updated"])
        self.assertTrue(Path(result["evidence_path"]).is_file())
        fake_adapter.force_stop.assert_called_once_with("pm_test_slice-001_a1")
        cancel_reviewers.assert_called_once_with(run_dir)

    def test_stop_with_evidence_halts_even_when_plan_changed_mid_run(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        before = git(self.repo, "rev-parse", "HEAD")
        state["status"] = "running"
        state["current_slice"] = pm.current_slice_state(
            self.repo.resolve(),
            plan_slice,
            artifact,
            "pm_test_slice-001_a1",
            1,
            pm.utc_now(),
            before,
            reviewer_policy={"sha256": "a" * 64, "policy": {}},
            prior_slice_context=self.prior_context_metadata(artifact),
        )
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        self.plan.write_text(self.plan.read_text(encoding="utf-8") + "\nEdited mid-run.\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            reason="operator stop",
            status="needs-human",
            harness_command="python fake.py",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )

        fake_adapter = mock.Mock()
        with mock.patch.object(pm_commands, "_current_adapter", return_value=fake_adapter), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.stop_with_evidence(args), 0)
        fake_adapter.force_stop.assert_called_once_with("pm_test_slice-001_a1")
        stopped = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(stopped["status"], "needs-human")
        self.assertIsNone(stopped["current_slice"])

    def test_finalize_enforces_reviewer_evidence_from_persisted_state(self):
        # finalize-slice is a separate invocation that may not re-supply
        # --reviewer-tools: the reviewer-evidence gate must still fire from the
        # requirement persisted in current_slice at start-slice time.
        # Mark Slice 1 opt-in ("Independent audit required: yes") so its reviewer
        # requirement arms the gate; by default reviewer delegation is
        # reporting-only. Edit before prepare_committed_repo so the flag is
        # committed with the plan and the worktree stays clean for the gate.
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8").replace(
                "- Approval needed before implementation: no.",
                "- Approval needed before implementation: no.\n- Independent audit required: yes.",
                1,
            ),
            encoding="utf-8",
        )
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = run_dir / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state["status"] = "running"
        state["supervision"]["mode"] = "model-supervised"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm.utc_now(),
            "before_head": before,
            "reviewer_tools": ["opencode"],
            "pause": None,
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
            "prior_slice_context": self.prior_context_metadata(artifact),
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        output = io.StringIO()
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(output):
                # _finalize_args passes reviewer_tools="" — the gate must come
                # from persisted state, not this invocation's flags.
                self.assertEqual(pm.finalize_slice(self._finalize_args()), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "repairable")
        self.assertEqual(result["repair"]["last_signature"], "reviewer-evidence")
        self.assertIn("reviewer-evidence.md", result["reason"])

    def test_finalize_pass_still_force_stops_session(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = run_dir / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state["status"] = "running"
        state["supervision"]["mode"] = "model-supervised"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm.utc_now(),
            "before_head": before,
            "reviewer_tools": [],
            "pause": None,
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
            "prior_slice_context": self.prior_context_metadata(artifact),
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.finalize_slice(self._finalize_args()), 0)
        fake_adapter.force_stop.assert_called_once_with("pm_test_slice-001_a1")
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "partial")
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["status"], "pass")

    def test_finalize_routes_oversized_projected_context_into_repair_before_acceptance(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with mock.patch.object(
                pm_runner,
                "projected_prior_slice_context_budget_failure",
                return_value="accepted reporting would exceed the next context budget",
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(pm.finalize_slice(self._finalize_args()), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "repairable")
        self.assertEqual(result["repair"]["last_signature"], "context-budget")
        fake_adapter.force_stop.assert_not_called()
        updated = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["status"], "resuming")
        self.assertIsNotNone(updated["current_slice"])
        self.assertEqual(updated["slices"], [])
        self.assertIn("cumulative context too large", (artifact / "repair-prompt-repair-1.md").read_text(encoding="utf-8"))

    def test_finalize_integrity_gate_is_terminal_without_repair(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        self._write_failing_validation_result(artifact, slice_id="Slice 99")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.finalize_slice(self._finalize_args()), 2)
        fake_adapter.force_stop.assert_called_once()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("slice_id does not match", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(len(state["slices"]), 1)
        self.assertEqual(state["slices"][0]["repair"], pm_state.default_repair_state())
        self.assertFalse((artifact / "repair-prompt.md").exists())

    def test_finalize_rejects_tampered_prior_slice_context_without_repair(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        self._write_failing_validation_result(artifact)
        (artifact / "prior-slice-context.md").write_text("tampered context\n", encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.finalize_slice(self._finalize_args()), 2)

        fake_adapter.force_stop.assert_called_once()
        updated = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["status"], "needs-human")
        self.assertIn("prior-slice context SHA-256 mismatch", updated["stop_reason"])
        self.assertIsNone(updated["current_slice"])
        self.assertEqual(updated["slices"][0]["status"], "needs-human")
        self.assertFalse((artifact / "repair-prompt.md").exists())

    def test_finalize_budget_exhaustion_is_terminal(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(
            state,
            run_dir,
            repair={"round": 3, "last_signature": "validation", "signature_streak": 1, "session_generation": 1},
        )
        self._write_failing_validation_result(artifact)
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.finalize_slice(self._finalize_args()), 2)
        fake_adapter.force_stop.assert_called_once()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("repair budget exhausted", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(len(state["slices"]), 1)
        self.assertEqual(state["slices"][0]["repair"]["round"], 3)

    def test_repair_state_requires_complete_schema_v3_state(self):
        with self.assertRaisesRegex(pm.PmError, "missing required repair state"):
            pm_state.repair_state(None)
        with self.assertRaisesRegex(pm.PmError, "missing required repair state"):
            pm_state.repair_state({"slice_id": "Slice 1"})
        self.assertEqual(
            pm_state.repair_state(
                {"repair": {"round": 2, "last_signature": "drift", "signature_streak": 1, "session_generation": 3}}
            ),
            {"round": 2, "last_signature": "drift", "signature_streak": 1, "session_generation": 3},
        )

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_send_then_finalize_accepts_corrected_slice(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "in_session_repair.py"
        write_in_session_repair_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=20,
            poll_seconds=0.1,
            reason="repair delivery",
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.wait(command_args), 0)
        finalize_output = io.StringIO()
        with contextlib.redirect_stdout(finalize_output):
            self.assertEqual(pm.finalize_slice(command_args), 0)
        first = json.loads(finalize_output.getvalue())
        self.assertEqual(first["status"], "repairable")
        self.assertEqual(first["mode"], "in-session")
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "resuming")
        self.assertIsNotNone(state["current_slice"])
        command_args.text = first["send_text"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.send(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.finalize_slice(command_args), 0)
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "partial")
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["status"], "pass")
        self.assertEqual(state["slices"][0]["repair"]["round"], 1)
        self.assertEqual(state["slices"][0]["changed_files"], ["README.md"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_circuit_breaker_matches_batch_path(self):
        # Same signature: in-session nudge, then a fresh-session relaunch by
        # finalize (start-slice refuses while current_slice is populated),
        # then terminal — identical to the batch-path breaker.
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "always_failing_validation.py"
        write_always_failing_validation_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=20,
            poll_seconds=0.1,
            reason="repair delivery",
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.wait(command_args), 0)
        first_output = io.StringIO()
        with contextlib.redirect_stdout(first_output):
            self.assertEqual(pm.finalize_slice(command_args), 0)
        first = json.loads(first_output.getvalue())
        self.assertEqual(first["mode"], "in-session")
        command_args.text = first["send_text"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.send(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.wait(command_args), 0)
        second_output = io.StringIO()
        with contextlib.redirect_stdout(second_output):
            self.assertEqual(pm.finalize_slice(command_args), 0)
        second = json.loads(second_output.getvalue())
        self.assertEqual(second["mode"], "fresh-session")
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["current_slice"]["attempt"], 2)
        self.assertTrue(state["current_slice"]["tmux_session"].endswith("_a2"))
        self.assertEqual(state["current_slice"]["repair"]["signature_streak"], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.finalize_slice(command_args), 2)
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("circuit breaker", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["repair"]["round"], 2)

    def test_start_slice_reaps_stale_run_sessions_before_launch(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        fake_adapter = mock.Mock()
        stale_session = f"pm_{state['run_id']}_slice-099_a1"
        fake_adapter.sessions_with_prefix.return_value = [stale_session]
        fake_adapter.harness_name = "codex"
        fake_adapter.allow_unattended_default = False
        fake_adapter.command_override = "python fake.py"
        fake_adapter.command = "python fake.py"

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(f"captured {session_name}\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        args = argparse.Namespace(
            harness_command="python fake.py",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            result = pm.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)

        fake_adapter.force_stop.assert_called_with(stale_session)
        self.assertEqual(result["reaped_stale_sessions"][0]["tmux_session"], stale_session)
        evidence = Path(result["reaped_stale_sessions"][0]["evidence_path"])
        self.assertTrue(evidence.exists())
        self.assertIn(stale_session, evidence.read_text(encoding="utf-8"))

    def test_pause_until_persists_pause_state_and_budget_counters(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm.utc_now(),
            "before_head": "a" * 40,
            "pause": None,
            "reviewer_tools": [],
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
            "prior_slice_context": self.prior_context_metadata(artifact),
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "paused"}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            until=pm.utc_now(),
            buffer_seconds=0,
            reason="rolling reset",
            poll_seconds=0.1,
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.pause_until(args), 0)
        paused = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(paused["status"], "resuming")
        self.assertIsNone(paused["current_slice"]["pause"])
        self.assertEqual(paused["supervision"]["pause_counters"]["consecutive_pauses_current_slice"], 1)

    def test_wait_returns_when_hard_stop_hint_appears(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm.utc_now(),
            "before_head": "a" * 40,
            "pause": None,
            "reviewer_tools": [],
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
            "prior_slice_context": self.prior_context_metadata(artifact),
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Monthly quota limit reached."}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=30,
            poll_seconds=0.1,
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )

        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(pm.wait(args), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["wait_status"], "hard-stop-hint")
        self.assertTrue(result["observation"]["operational_hints"][0]["hard_stop"])

    def test_pause_until_refuses_hard_stop_hint_and_budget_exhaustion(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm.utc_now(),
            "before_head": "a" * 40,
            "pause": None,
            "reviewer_tools": [],
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
            "prior_slice_context": self.prior_context_metadata(artifact),
        }
        state["supervision"]["max_single_pause_seconds"] = 0
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Session limit reached. Try again in 1 minute."}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            until=(datetime.now(timezone.utc) + timedelta(minutes=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            buffer_seconds=0,
            reason="rolling reset",
            poll_seconds=0.1,
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with self.assertRaisesRegex(pm.PmError, "max_single_pause_seconds"):
                pm.pause_until(args)

        state["supervision"]["max_single_pause_seconds"] = 21600
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Weekly usage limit reached."}
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with self.assertRaisesRegex(pm.PmError, "hard-stop operational hint"):
                pm.pause_until(args)

    def test_reset_slice_pause_counters(self):
        state = {"supervision": {"pause_counters": {"consecutive_pauses_current_slice": 2, "cumulative_pause_seconds_run": 900}}}
        pm.reset_slice_pause_counters(state)
        self.assertEqual(state["supervision"]["pause_counters"]["consecutive_pauses_current_slice"], 0)
        # The cumulative per-run budget must survive the per-slice reset.
        self.assertEqual(state["supervision"]["pause_counters"]["cumulative_pause_seconds_run"], 900)

    # --- Shared repair-decision core ---------------------------------------

    def test_resolve_repair_action_decisions(self):
        gate = pm.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = pm_state.default_repair_state()

        mode, terminal = pm.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("in-session", None))
        self.assertEqual(repair, {"round": 1, "last_signature": "validation", "signature_streak": 1, "session_generation": 1})

        mode, terminal = pm.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("fresh-session", None))
        self.assertEqual(repair["signature_streak"], 2)

        mode, terminal = pm.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual(mode, "terminal")
        self.assertEqual(terminal.status, "needs-human")
        self.assertIn("circuit breaker", terminal.reason)

    def test_resolve_repair_action_dead_session_relaunch_keeps_breaker(self):
        gate = pm.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = pm_state.default_repair_state()
        repair.update(round=1, last_signature="validation", signature_streak=1)
        mode, terminal = pm.resolve_repair_action(repair, "validation", False, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("relaunch", None))
        self.assertEqual(repair["round"], 2)
        # Breaker state untouched: a dead session is a runner condition.
        self.assertEqual(repair["signature_streak"], 1)

    def test_resolve_repair_action_budget_exhaustion_is_terminal(self):
        gate = pm.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = pm_state.default_repair_state()
        repair["round"] = 3
        mode, terminal = pm.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual(mode, "terminal")
        self.assertEqual(terminal.status, "blocked")
        self.assertIn("repair budget exhausted", terminal.reason)

    # --- Readiness fallback and orphan detection ---------------------------


if __name__ == "__main__":
    unittest.main()

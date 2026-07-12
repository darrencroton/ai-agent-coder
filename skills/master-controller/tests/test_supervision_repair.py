"""Model-supervised primitives and repair/circuit-breaker state tests."""

from mc_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the mc module


class SupervisionRepairTests(McTestCase):
    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_start_wait_finalize_records_pass(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake_harness.py"
        write_fake_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=10,
            poll_seconds=0.1,
            reason="test",
            until=mc.utc_now(),
            buffer_seconds=0,
            status="needs-human",
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        before_start = git(self.repo, "rev-parse", "HEAD")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.start_slice(command_args), 0)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        running = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["supervision"]["mode"], "model-supervised")
        self.assertEqual(running["current_slice"]["before_head"], before_start)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.finalize_slice(command_args), 0)
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
            self.assertEqual(mc.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=3,
            poll_seconds=0.1,
            reason="rolling usage reset",
            until=mc.utc_now(),
            buffer_seconds=0,
            text="You were interrupted. Review what you were doing then continue.",
            status="needs-human",
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.start_slice(command_args), 0)
        wait_output = io.StringIO()
        with contextlib.redirect_stdout(wait_output):
            self.assertEqual(mc.wait(command_args), 0)
        first_wait = json.loads(wait_output.getvalue())
        self.assertEqual(first_wait["wait_status"], "timeout")
        usage_hint = next(hint for hint in first_wait["observation"]["operational_hints"] if hint["kind"] == "usage_limit")
        self.assertEqual(usage_hint["subtype"], "rolling_window")
        self.assertFalse(usage_hint["hard_stop"])
        self.assertEqual(usage_hint["recovery_guidance"], "pause-until-reset-plus-buffer-then-send-continuation")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.pause_until(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.send(command_args), 0)
        command_args.seconds = 10
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.finalize_slice(command_args), 0)

        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["supervision"]["mode"] = "model-supervised"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": mc.utc_now(),
            "before_head": git(self.repo, "rev-parse", "HEAD"),
            "pause": None,
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
            until=mc.utc_now(),
            buffer_seconds=0,
            status="needs-human",
            harness_command=None,
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            wait_output = io.StringIO()
            with contextlib.redirect_stdout(wait_output):
                self.assertEqual(mc.wait(command_args), 0)
            wait_result = json.loads(wait_output.getvalue())
            self.assertEqual(wait_result["wait_status"], "process-exited")
            usage_hint = next(hint for hint in wait_result["observation"]["operational_hints"] if hint["kind"] == "usage_limit")
            self.assertEqual(usage_hint["recovery_guidance"], "restart-from-clean-authorized-state-or-stop-for-user")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.finalize_slice(command_args), 2)
        state = json.loads((((self.repo / ".ai-mc" / "current").resolve()) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("orchestrator result missing", state["stop_reason"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_finalize_blocks_missing_result_after_process_exit(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "no_result_harness.py"
        write_no_result_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.finalize_slice(command_args), 2)
        state = json.loads((((self.repo / ".ai-mc" / "current").resolve()) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("orchestrator result missing", state["stop_reason"])

    def test_wait_observing_policy_flag_gates_hard_signals(self):
        # The one shared wait loop serves both drivers; stop_on_hard_signals
        # is the per-driver policy. True (model-supervised) breaks on a hard
        # prompt so the model can judge it; False (batch) keeps polling —
        # detection markers are broad substring matches, and the safety
        # boundary is send-time refusal, not the wait. The activity log is
        # appended on every poll either way.
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {
            "running": True,
            "active": True,
            "capture": "Do you trust the files in this folder?\n",
        }
        fake_adapter.detect_hard_prompt.side_effect = mc.TmuxHarnessAdapter.detect_hard_prompt
        wait_args = self._finalize_args()
        wait_args.poll_seconds = 0.05
        activity_log = artifact / "activity-attempt-1.jsonl"
        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            reason, snapshot = mc_observation.wait_observing(
                wait_args, self.repo.resolve(), run_dir, 5, activity_log=activity_log
            )
            self.assertEqual(reason, "hard-prompt")
            self.assertTrue(snapshot["prompt_on_screen"]["present"])
            # Breaking on the first poll still records exactly one activity
            # line: the audit trail must not depend on winning a race.
            first_wait_lines = activity_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(first_wait_lines), 1)
            batch_reason, _snapshot = mc_observation.wait_observing(
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
        state["slices"].append({"slice_id": "Slice 1", "status": "failed"})
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        plan_slice = mc.parse_plan(self.plan)[0]
        fake_adapter = mock.Mock()
        fake_adapter.sessions_with_prefix.return_value = []
        fake_adapter.harness_name = "codex"
        fake_adapter.allow_unattended_default = False
        fake_adapter.command_override = "python fake.py"
        fake_adapter.command = "python fake.py"
        args = argparse.Namespace(
            harness_command="python fake.py",
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            result = mc.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)
        self.assertTrue(result["started"])
        self.assertEqual(result["attempt"], 2)
        self.assertTrue(result["tmux_session"].endswith("_a2"))
        persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["current_slice"]["repair"]["session_generation"], 2)
        self.assertEqual(persisted["current_slice"]["repair"]["round"], 0)

    def test_finalize_keeps_session_alive_on_repairable_gate(self):
        # A repairable MC gate with budget remaining must not tear the session
        # down: no force_stop, no slice entry, current_slice kept, status set
        # to the send-eligible `resuming`, and the repair prompt surfaced. The
        # current_slice here has NO repair key, proving the round-0 default
        # for runs created before the repair loop existed.
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        self._write_failing_validation_result(artifact)
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        output = io.StringIO()
        with mock.patch.object(mc_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(output):
                self.assertEqual(mc.finalize_slice(self._finalize_args()), 0)
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
        self.assertTrue((artifact / "orchestrator-result-repair-1.json").exists())
        self.assertFalse((artifact / "orchestrator-result.json").exists())
        events = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual(len(repair_events), 1)
        self.assertEqual(repair_events[0]["mode"], "in-session")
        self.assertEqual(repair_events[0]["signature"], "validation")
        self.assertEqual(repair_events[0]["round"], 1)

    def test_start_slice_persists_worker_tools_for_later_finalize(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        plan_slice = mc.parse_plan(self.plan)[0]
        fake_adapter = mock.Mock()
        fake_adapter.sessions_with_prefix.return_value = []
        fake_adapter.harness_name = "codex"
        fake_adapter.allow_unattended_default = False
        fake_adapter.command_override = "python fake.py"
        fake_adapter.command = "python fake.py"
        args = argparse.Namespace(
            harness_command="python fake.py",
            worker_tools="opencode",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            result = mc.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)
        self.assertTrue(result["started"])
        persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["current_slice"]["worker_tools"], ["opencode"])
        policy = json.loads((run_dir / "slices" / "slice-001" / "worker-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["required_tools"], ["opencode"])
        self.assertEqual(policy["slice_id"], "Slice 1")
        self.assertEqual(policy["plan_sha256"], state["plan"]["sha256"])
        prompt = (run_dir / "slices" / "slice-001" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("name: ai-orchestrator", prompt)
        self.assertIn("worker_jobs.py launch", prompt)

    def test_worker_policy_restricts_explicit_read_only_plan_requirement(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        plan_slice = mc.parse_plan(self.plan)[0]
        sections = dict(plan_slice.sections)
        sections["Validation Plan"] += "\n- Worker evidence: run one bounded read-only support check."
        read_only_slice = mc.PlanSlice(plan_slice.number, plan_slice.title, plan_slice.body, sections)
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        policy_path = mc.write_worker_policy(state, read_only_slice, artifact, ("opencode",), "model", None)
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["allowed_access"], ["read-only"])

    def test_stop_with_evidence_records_terminal_slice_attempt(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("unaccepted work\n", encoding="utf-8")
        state["status"] = "running"
        state["current_slice"] = mc.current_slice_state(
            self.repo.resolve(), plan_slice, artifact, "mc_test_slice-001_a1", 1, mc.utc_now(), before, worker_tools=("opencode",)
        )
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            reason="worker contract violation",
            status="needs-human",
            harness_command="python fake.py",
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_commands, "_current_adapter", return_value=fake_adapter), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.stop_with_evidence(args), 0)
        stopped = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertIsNone(stopped["current_slice"])
        self.assertEqual(stopped["status"], "needs-human")
        self.assertEqual(len(stopped["slices"]), 1)
        self.assertEqual(stopped["slices"][0]["status"], "needs-human")
        self.assertEqual(stopped["slices"][0]["changed_files"], ["README.md"])
        self.assertEqual(stopped["slices"][0]["gate_reason"], "worker contract violation")

    def test_finalize_enforces_worker_evidence_from_persisted_state(self):
        # finalize-slice is a separate invocation that may not re-supply
        # --worker-tools: the worker-evidence gate must still fire from the
        # requirement persisted in current_slice at start-slice time.
        # Mark Slice 1 opt-in ("Independent audit required: yes") so its worker
        # requirement arms the gate; by default worker delegation is
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
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": mc.utc_now(),
            "before_head": before,
            "worker_tools": ["opencode"],
            "pause": None,
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        output = io.StringIO()
        with mock.patch.object(mc_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(output):
                # _finalize_args passes worker_tools="" — the gate must come
                # from persisted state, not this invocation's flags.
                self.assertEqual(mc.finalize_slice(self._finalize_args()), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "repairable")
        self.assertEqual(result["repair"]["last_signature"], "worker-evidence")
        self.assertIn("worker-evidence.md", result["reason"])

    def test_finalize_pass_still_force_stops_session(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": mc.utc_now(),
            "before_head": before,
            "worker_tools": [],
            "pause": None,
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        with mock.patch.object(mc_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.finalize_slice(self._finalize_args()), 0)
        fake_adapter.force_stop.assert_called_once_with("mc_test_slice-001_a1")
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "partial")
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["status"], "pass")

    def test_finalize_integrity_gate_is_terminal_without_repair(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        self._write_failing_validation_result(artifact, slice_id="Slice 99")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        with mock.patch.object(mc_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.finalize_slice(self._finalize_args()), 2)
        fake_adapter.force_stop.assert_called_once()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("slice_id does not match", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(len(state["slices"]), 1)
        self.assertNotIn("repair", state["slices"][0])
        self.assertFalse((artifact / "repair-prompt.md").exists())

    def test_finalize_budget_exhaustion_is_terminal(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
        with mock.patch.object(mc_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.finalize_slice(self._finalize_args()), 2)
        fake_adapter.force_stop.assert_called_once()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("repair budget exhausted", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(len(state["slices"]), 1)
        self.assertEqual(state["slices"][0]["repair"]["round"], 3)

    def test_repair_state_defaults_when_absent(self):
        # Codex #8: runs created before the repair loop have no
        # current_slice.repair and must load with a round-0 default;
        # normalize_run_state deliberately does not backfill it.
        self.assertEqual(
            mc_state.repair_state(None),
            {"round": 0, "last_signature": "", "signature_streak": 0, "session_generation": 1},
        )
        self.assertEqual(mc_state.repair_state({"slice_id": "Slice 1"})["round"], 0)
        self.assertEqual(
            mc_state.repair_state({"repair": {"round": 2, "last_signature": "drift"}}),
            {"round": 2, "last_signature": "drift", "signature_streak": 0, "session_generation": 1},
        )

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_send_then_finalize_accepts_corrected_slice(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "in_session_repair.py"
        write_in_session_repair_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=20,
            poll_seconds=0.1,
            reason="repair delivery",
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.wait(command_args), 0)
        finalize_output = io.StringIO()
        with contextlib.redirect_stdout(finalize_output):
            self.assertEqual(mc.finalize_slice(command_args), 0)
        first = json.loads(finalize_output.getvalue())
        self.assertEqual(first["status"], "repairable")
        self.assertEqual(first["mode"], "in-session")
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "resuming")
        self.assertIsNotNone(state["current_slice"])
        command_args.text = first["send_text"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.send(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.finalize_slice(command_args), 0)
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
            self.assertEqual(mc.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=20,
            poll_seconds=0.1,
            reason="repair delivery",
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.wait(command_args), 0)
        first_output = io.StringIO()
        with contextlib.redirect_stdout(first_output):
            self.assertEqual(mc.finalize_slice(command_args), 0)
        first = json.loads(first_output.getvalue())
        self.assertEqual(first["mode"], "in-session")
        command_args.text = first["send_text"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.send(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.wait(command_args), 0)
        second_output = io.StringIO()
        with contextlib.redirect_stdout(second_output):
            self.assertEqual(mc.finalize_slice(command_args), 0)
        second = json.loads(second_output.getvalue())
        self.assertEqual(second["mode"], "fresh-session")
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["current_slice"]["attempt"], 2)
        self.assertTrue(state["current_slice"]["tmux_session"].endswith("_a2"))
        self.assertEqual(state["current_slice"]["repair"]["signature_streak"], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.finalize_slice(command_args), 2)
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("circuit breaker", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["repair"]["round"], 2)

    def test_start_slice_reaps_stale_run_sessions_before_launch(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        plan_slice = mc.parse_plan(self.plan)[0]
        fake_adapter = mock.Mock()
        stale_session = f"mc_{state['run_id']}_slice-099_a1"
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
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            result = mc.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)

        fake_adapter.force_stop.assert_called_with(stale_session)
        self.assertEqual(result["reaped_stale_sessions"][0]["tmux_session"], stale_session)
        evidence = Path(result["reaped_stale_sessions"][0]["evidence_path"])
        self.assertTrue(evidence.exists())
        self.assertIn(stale_session, evidence.read_text(encoding="utf-8"))

    def test_pause_until_persists_pause_state_and_budget_counters(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": mc.utc_now(),
            "before_head": "a" * 40,
            "pause": None,
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "paused"}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            until=mc.utc_now(),
            buffer_seconds=0,
            reason="rolling reset",
            poll_seconds=0.1,
            harness_command=None,
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.pause_until(args), 0)
        paused = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(paused["status"], "resuming")
        self.assertIsNone(paused["current_slice"]["pause"])
        self.assertEqual(paused["supervision"]["pause_counters"]["consecutive_pauses_current_slice"], 1)

    def test_wait_returns_when_hard_stop_hint_appears(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": mc.utc_now(),
            "before_head": "a" * 40,
            "pause": None,
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
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )

        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(mc.wait(args), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["wait_status"], "hard-stop-hint")
        self.assertTrue(result["observation"]["operational_hints"][0]["hard_stop"])

    def test_pause_until_refuses_hard_stop_hint_and_budget_exhaustion(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": mc.utc_now(),
            "before_head": "a" * 40,
            "pause": None,
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
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with self.assertRaisesRegex(mc.McError, "max_single_pause_seconds"):
                mc.pause_until(args)

        state["supervision"]["max_single_pause_seconds"] = 21600
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Weekly usage limit reached."}
        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with self.assertRaisesRegex(mc.McError, "hard-stop operational hint"):
                mc.pause_until(args)

    def test_reset_slice_pause_counters(self):
        state = {"supervision": {"pause_counters": {"consecutive_pauses_current_slice": 2, "cumulative_pause_seconds_run": 900}}}
        mc.reset_slice_pause_counters(state)
        self.assertEqual(state["supervision"]["pause_counters"]["consecutive_pauses_current_slice"], 0)
        # The cumulative per-run budget must survive the per-slice reset.
        self.assertEqual(state["supervision"]["pause_counters"]["cumulative_pause_seconds_run"], 900)

    # --- Shared repair-decision core ---------------------------------------

    def test_resolve_repair_action_decisions(self):
        gate = mc.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = mc_state.default_repair_state()

        mode, terminal = mc.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("in-session", None))
        self.assertEqual(repair, {"round": 1, "last_signature": "validation", "signature_streak": 1, "session_generation": 1})

        mode, terminal = mc.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("fresh-session", None))
        self.assertEqual(repair["signature_streak"], 2)

        mode, terminal = mc.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual(mode, "terminal")
        self.assertEqual(terminal.status, "needs-human")
        self.assertIn("circuit breaker", terminal.reason)

    def test_resolve_repair_action_dead_session_relaunch_keeps_breaker(self):
        gate = mc.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = mc_state.default_repair_state()
        repair.update(round=1, last_signature="validation", signature_streak=1)
        mode, terminal = mc.resolve_repair_action(repair, "validation", False, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("relaunch", None))
        self.assertEqual(repair["round"], 2)
        # Breaker state untouched: a dead session is a runner condition.
        self.assertEqual(repair["signature_streak"], 1)

    def test_resolve_repair_action_budget_exhaustion_is_terminal(self):
        gate = mc.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = mc_state.default_repair_state()
        repair["round"] = 3
        mode, terminal = mc.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual(mode, "terminal")
        self.assertEqual(terminal.status, "blocked")
        self.assertIn("repair budget exhausted", terminal.reason)

    # --- Readiness fallback and orphan detection ---------------------------


if __name__ == "__main__":
    unittest.main()

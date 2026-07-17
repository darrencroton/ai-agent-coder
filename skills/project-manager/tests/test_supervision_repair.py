"""Model-supervised primitives and repair/circuit-breaker state tests."""

import time

from pm_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the pm module


class SupervisionRepairTests(PmTestCase):
    def test_merge_maximal_ledger_item_keeps_fuller_version_instead_of_duplicating(self):
        """PM test 16 (report-mc-test16-...md): a later archived round adding a
        field (e.g. `location`) to an earlier round's item is a superset, not a
        different item — merging must keep one fuller entry, not both, so a
        fresh-session prompt doesn't present the Developer with near-duplicate
        findings for the same underlying issue. `runner.py` and `gates.py` must
        share one merge routine (`pm_gates.merge_maximal_ledger_item`) rather
        than each keeping a subtly different local copy."""
        earlier = {
            "source": "code-review",
            "severity": "low",
            "summary": "Missing input validation on negative terms",
            "disposition": "deferred-inconsequential",
        }
        fuller = dict(earlier, location="algorithms.py:9")

        destination: list = []
        pm_gates.merge_maximal_ledger_item(destination, earlier)
        pm_gates.merge_maximal_ledger_item(destination, fuller)
        self.assertEqual(destination, [fuller])

        destination = []
        pm_gates.merge_maximal_ledger_item(destination, fuller)
        pm_gates.merge_maximal_ledger_item(destination, earlier)
        self.assertEqual(destination, [fuller])

        destination = []
        pm_gates.merge_maximal_ledger_item(destination, earlier)
        pm_gates.merge_maximal_ledger_item(destination, earlier)
        self.assertEqual(destination, [earlier])

        unrelated = {
            "source": "code-review",
            "severity": "medium",
            "summary": "A distinct finding",
            "disposition": "deferred-inconsequential",
        }
        destination = []
        pm_gates.merge_maximal_ledger_item(destination, earlier)
        pm_gates.merge_maximal_ledger_item(destination, unrelated)
        self.assertEqual(destination, [earlier, unrelated])

    def test_merge_maximal_ledger_item_collapses_multiple_incomparable_supersets(self):
        """Codex review of PM test 16's fix (independent code-review via
        orchestrator, gpt-5.6-sol/high): a base item followed by two
        independently-extended, mutually incomparable variants (`+location`
        and `+ticket`) followed by a version carrying both fields must
        collapse to the one, fullest entry — not leave an earlier variant
        behind as a stale duplicate."""
        base = {
            "source": "code-review",
            "severity": "low",
            "summary": "Missing input validation on negative terms",
            "disposition": "deferred-inconsequential",
        }
        with_location = dict(base, location="algorithms.py:9")
        with_ticket = dict(base, ticket="PM-42")
        fullest = dict(base, location="algorithms.py:9", ticket="PM-42")

        destination: list = []
        for item in (base, with_location, with_ticket, fullest):
            pm_gates.merge_maximal_ledger_item(destination, item)
        self.assertEqual(destination, [fullest])

        # Order independence: the fullest version arriving in the middle must
        # still absorb both partial variants, in either arrival order.
        destination = []
        for item in (with_location, fullest, with_ticket):
            pm_gates.merge_maximal_ledger_item(destination, item)
        self.assertEqual(destination, [fullest])

    def test_missing_ledger_items_reports_only_fullest_variant_across_rounds(self):
        """The same multi-variant collapse must hold for the gate's own
        missing-items accumulation (`gates._missing_ledger_items`), not only
        for the fresh-session prompt's merge — a base item archived in round 1
        followed by a fuller `+location` version archived in round 2, with no
        fresh coverage, must be reported as one missing item, not two."""
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        base = {
            "source": "code-review",
            "severity": "low",
            "summary": "Missing input validation on negative terms",
            "disposition": "deferred-inconsequential",
        }
        fuller = dict(base, location="algorithms.py:9")
        for round_number, findings in ((1, [base]), (2, [fuller])):
            (artifact / f"developer-result-repair-{round_number}.json").write_text(
                json.dumps(
                    {
                        "schema_version": pm_constants.SCHEMA_VERSION,
                        "slice_id": "Slice 1",
                        "status": "repairable",
                        "residual_findings": findings,
                        "continuation_notes": [],
                    }
                ),
                encoding="utf-8",
            )

        missing = pm_gates._missing_ledger_items(artifact, {"residual_findings": []})

        self.assertEqual(missing, {"residual_findings": [fuller]})

    def test_cancel_run_reviewers_scans_current_and_prior_slice_artifacts(self):
        run_dir = self.repo / ".ai-pm" / "runs" / "test"
        first = run_dir / "slices" / "slice-001"
        second = run_dir / "slices" / "slice-002"
        first.mkdir(parents=True)
        second.mkdir(parents=True)

        with mock.patch.object(pm_runtime, "cancel_reviewer_runs", side_effect=[[{"slice": 1}], [{"slice": 2}]]) as cancel, mock.patch.object(
            pm_runtime, "capture_reviewer_runs_summary"
        ) as capture:
            results = pm_runtime.cancel_run_reviewers(run_dir)

        self.assertEqual(results, [{"slice": 1}, {"slice": 2}])
        self.assertEqual(cancel.call_args_list, [mock.call(first), mock.call(second)])
        self.assertEqual(capture.call_args_list, [mock.call(first), mock.call(second)])

    def test_cancel_reviewer_runs_terminates_tracked_wrapper_and_child_group(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        run_dir = artifact / "reviewer-runs" / "reviewers-test"
        run_dir.mkdir(parents=True)
        reviewer_jobs = pm_runtime.reviewer_jobs_module()
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

        results = pm_runtime.cancel_reviewer_runs(artifact)

        self.assertEqual(results[0]["returncode"], 0, results)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "cancelled")
        reviewer_jobs._LIBRARY_WRAPPERS.pop(int(launched["pid"])).wait(timeout=5)
        self.assertFalse(reviewer_jobs.process_running(int(launched["pid"])))

    def test_idle_stall_signature_uses_shared_repair_escalation(self):
        repair = pm_state.default_repair_state()
        gate = pm_models.GateDecision("repairable", "idle", signature="idle-no-progress")
        first, terminal = pm_runner.resolve_repair_action(repair, gate.signature, True, 3, gate, "Slice 1")
        self.assertEqual((first, terminal), ("in-session", None))
        second, terminal = pm_runner.resolve_repair_action(repair, gate.signature, True, 3, gate, "Slice 1")
        self.assertEqual((second, terminal), ("fresh-session", None))
        third, terminal = pm_runner.resolve_repair_action(repair, gate.signature, True, 3, gate, "Slice 1")
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
            self.assertEqual(pm_commands.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=10,
            poll_seconds=0.1,
            reason="test",
            until=pm_utils.utc_now(),
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
            self.assertEqual(pm_commands.start_slice(command_args), 0)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        running = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["supervision"]["mode"], "model-supervised")
        self.assertEqual(running["current_slice"]["before_head"], before_start)
        self.assertEqual(running["current_slice"]["launch_config"]["harness_command"], command_args.harness_command)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.wait(command_args), 0)
        final_output = io.StringIO()
        with contextlib.redirect_stdout(final_output):
            final_code = pm_commands.finalize_slice(command_args)
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
            self.assertEqual(pm_commands.init_run(args), 0)
        command_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            seconds=3,
            poll_seconds=0.1,
            reason="rolling usage reset",
            until=pm_utils.utc_now(),
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
            self.assertEqual(pm_commands.start_slice(command_args), 0)
        wait_output = io.StringIO()
        with contextlib.redirect_stdout(wait_output):
            self.assertEqual(pm_commands.wait(command_args), 0)
        first_wait = json.loads(wait_output.getvalue())
        self.assertEqual(first_wait["wait_status"], "timeout")
        usage_hint = next(hint for hint in first_wait["observation"]["operational_hints"] if hint["kind"] == "usage_limit")
        self.assertEqual(usage_hint["subtype"], "rolling_window")
        self.assertFalse(usage_hint["hard_stop"])
        self.assertEqual(usage_hint["recovery_guidance"], "pause-until-reset-plus-buffer-then-send-continuation")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.pause_until(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.send(command_args), 0)
        command_args.seconds = 10
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.finalize_slice(command_args), 0)

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
            "started_at": pm_utils.utc_now(),
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
            until=pm_utils.utc_now(),
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
                self.assertEqual(pm_commands.wait(command_args), 0)
            wait_result = json.loads(wait_output.getvalue())
            self.assertEqual(wait_result["wait_status"], "process-exited")
            usage_hint = next(hint for hint in wait_result["observation"]["operational_hints"] if hint["kind"] == "usage_limit")
            self.assertEqual(usage_hint["recovery_guidance"], "restart-from-clean-authorized-state-or-stop-for-user")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm_commands.finalize_slice(command_args), 2)
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
            self.assertEqual(pm_commands.init_run(args), 0)
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
            self.assertEqual(pm_commands.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.finalize_slice(command_args), 2)
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
        fake_adapter.detect_hard_prompt.side_effect = pm_tmux_adapter.TmuxHarnessAdapter.detect_hard_prompt
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
            pm_state.slice_entry_from_gate(
                self.repo,
                pm_plan.parse_plan(self.plan)[0],
                (self.repo / ".ai-pm" / "current").resolve() / "slices" / "slice-001",
                pm_utils.utc_now(),
                pm_models.GateDecision("failed", "prior attempt", {"changed_files": []}, ()),
                git(self.repo, "rev-parse", "HEAD"),
                repair=pm_state.default_repair_state(),
                reviewer_policy={"sha256": "a" * 64, "policy": {}},
                prior_slice_context={"path": "prior-slice-context.md", "sha256": "b" * 64},
            )
        )
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm_plan.parse_plan(self.plan)[0]
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
            result = pm_runner.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)
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
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)
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
            "operational_round": 0,
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

    def test_repair_round_refreshes_reviewer_policy_and_invalidates_stale_evidence(self):
        # Finding 15: reviewer-policy.json's digest binds Reviewer launch
        # contracts to one slice attempt/repair round (gates.py's exact-match
        # policy_sha256 check). Without a per-round refresh, a Reviewer PASS
        # obtained before a tree-changing repair keeps satisfying the opt-in
        # independent-audit gate for final work the Reviewer never saw.
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = run_dir / "slices" / "slice-001"
        self.write_validated_reviewer_run(artifact, tool="opencode")
        old_digest = hashlib.sha256((artifact / "reviewer-policy.json").read_bytes()).hexdigest()
        (artifact / "reviewer-evidence.md").write_text(
            "# Reviewer Evidence\n- Label: 01-opencode-drift-audit\n- Result summary: reviewer ran.\n",
            encoding="utf-8",
        )
        self._write_failing_validation_result(artifact)
        state["status"] = "running"
        state["supervision"]["mode"] = "model-supervised"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm_utils.utc_now(),
            "before_head": before,
            "reviewer_tools": ["opencode"],
            "pause": None,
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": pm_runtime.reviewer_policy_snapshot(artifact / "reviewer-policy.json"),
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
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["mode"], "in-session")

        new_digest = hashlib.sha256((artifact / "reviewer-policy.json").read_bytes()).hexdigest()
        self.assertNotEqual(new_digest, old_digest)
        state_after = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        new_snapshot = state_after["current_slice"]["reviewer_policy"]
        self.assertEqual(new_snapshot["sha256"], new_digest)
        self.assertEqual(new_snapshot["policy"]["repair_round"], 1)
        self.assertEqual(new_snapshot["policy"]["session_generation"], 1)
        self.assertEqual(new_snapshot["policy"]["before_head"], before)

        # Reviewer evidence minted under the OLD digest (before this repair
        # round) must no longer satisfy the gate against the refreshed policy.
        failure = pm_gates.reviewer_evidence_failure(artifact, ("opencode",), new_snapshot)
        self.assertIsNotNone(failure)
        self.assertIn("opencode", failure)

        provenance = pm_gates.reviewer_audit_provenance(artifact, ("opencode",), new_snapshot)
        for audit in ("drift-audit", "code-review"):
            self.assertNotEqual(provenance[audit]["performed_by"], "reviewer")

    def test_idle_stall_repair_refreshes_reviewer_policy_digest_but_not_round_budget(self):
        """Test 17 (report-mc-test17-...md): an idle stall's repair round
        must not compete with a real defect-fixing repair for the same
        bounded max_repair_attempts budget — the reported harm was the
        3-round budget being exhausted by pure idle churn unrelated to any
        implementation defect. But an idle-stall repair prompt explicitly
        tells the developer to "continue the interrupted work" (prompts.py),
        which can go on to touch the tree before the next result — so,
        unlike an earlier draft of this fix, reviewer-policy.json's digest
        must still be refreshed on every repair round exactly as for any
        other signature (see test_repair_round_refreshes_reviewer_policy_
        and_invalidates_stale_evidence above); only the round-budget
        counter is exempted, not the evidence-invalidation safety
        invariant. A first orchestrator code review of this fix correctly
        FAILed a version that skipped the digest refresh."""
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = run_dir / "slices" / "slice-001"
        self.write_validated_reviewer_run(artifact, tool="opencode")
        (artifact / "reviewer-evidence.md").write_text(
            "# Reviewer Evidence\n- Label: 01-opencode-drift-audit\n- Result summary: reviewer ran.\n",
            encoding="utf-8",
        )
        old_digest = hashlib.sha256((artifact / "reviewer-policy.json").read_bytes()).hexdigest()
        pre_repair_snapshot = pm_runtime.reviewer_policy_snapshot(artifact / "reviewer-policy.json")
        # Establish the evidence is genuinely valid before the repair, so the
        # post-repair assertion below proves invalidation, not merely that
        # some unrelated mismatch happens to fail the gate.
        self.assertIsNone(pm_gates.reviewer_evidence_failure(artifact, ("opencode",), pre_repair_snapshot))
        state["status"] = "running"
        state["supervision"]["mode"] = "model-supervised"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm_utils.utc_now(),
            "before_head": before,
            "reviewer_tools": ["opencode"],
            "pause": None,
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": pre_repair_snapshot,
            "prior_slice_context": self.prior_context_metadata(artifact),
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True
        args = self._finalize_args()
        plan_slice = pm_plan.plan_slice_by_id(pm_plan.parse_plan(self.plan), "Slice 1")

        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            result = pm_runner.handle_idle_stall(args, self.repo, state, plan_slice, run_dir)

        self.assertEqual(result["mode"], "in-session")
        fake_adapter.send_literal.assert_called_once()

        # Safety invariant preserved: the digest still changes every round,
        # so evidence minted before this repair no longer satisfies the gate
        # (the developer must relaunch — a real cost, but a safe one).
        new_digest = hashlib.sha256((artifact / "reviewer-policy.json").read_bytes()).hexdigest()
        self.assertNotEqual(new_digest, old_digest)
        state_after = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state_after["current_slice"]["reviewer_policy"]["sha256"], new_digest)
        failure = pm_gates.reviewer_evidence_failure(
            artifact, ("opencode",), state_after["current_slice"]["reviewer_policy"]
        )
        self.assertIsNotNone(failure)

        # Budget fix: this idle-stall round left the shared round counter at
        # its starting value, so it did not consume any of the 3-round
        # max_repair_attempts budget that a real defect-fixing repair uses.
        # It did advance the separate operational_round counter instead.
        self.assertEqual(state_after["current_slice"]["repair"]["round"], 0)
        self.assertEqual(state_after["current_slice"]["repair"]["operational_round"], 1)
        self.assertEqual(state_after["current_slice"]["repair"]["last_signature"], "idle-no-progress")
        self.assertEqual(state_after["current_slice"]["repair"]["signature_streak"], 1)

        # A second, CONSECUTIVE operational refresh must invalidate again
        # relative to the round-1 refresh above -- not just relative to the
        # pre-repair fixture (whose policy shape differs from a real
        # slice-start write and so would trivially differ regardless).
        # Without operational_round in write_reviewer_policy's digest inputs,
        # a second in-session operational refresh would write byte-identical
        # JSON to the first (repair_round and session_generation both stay
        # frozen across in-session operational repairs), silently making the
        # "safety invariant preserved" claim above false for any run with
        # more than one idle-stall in a row. A second orchestrator code
        # review of this fix correctly FAILed a version that only fixed the
        # round-0-to-round-1 case. Called directly (bypassing
        # resolve_repair_action/handle_idle_stall) because a second
        # same-signature call would escalate to fresh-session mode, which
        # launches a real session outside this unit test's scope.
        current_slice = state["current_slice"]
        current_slice["repair"]["operational_round"] = 2
        pm_runner._refresh_reviewer_policy_for_repair(
            args, state, plan_slice, artifact, current_slice, current_slice["repair"]
        )
        digest_after_second_repair = hashlib.sha256((artifact / "reviewer-policy.json").read_bytes()).hexdigest()
        self.assertNotEqual(digest_after_second_repair, new_digest)
        failure_after_second = pm_gates.reviewer_evidence_failure(
            artifact, ("opencode",), current_slice["reviewer_policy"]
        )
        self.assertIsNotNone(failure_after_second)

    def test_resolve_repair_action_exempts_non_tree_changing_signatures_from_round_budget(self):
        """Direct unit coverage of the resolve_repair_action change: idle-stall
        and transient-service-unavailable repairs never advance `round` or
        trip budget exhaustion, but three consecutive failures of either
        still hits the same-signature circuit breaker (needs-human) exactly
        as any other signature would — they are unbounded on the shared
        budget, not unbounded altogether."""
        # Control: an ordinary signature exhausts a budget of 1 on its
        # second call (round 0->1 the first call, then 1 >= 1 the second).
        control_repair = pm_state.default_repair_state()
        control_gate = pm_models.GateDecision("repairable", "a real defect", signature="validation")
        mode, terminal = pm_runner.resolve_repair_action(control_repair, "validation", True, 1, control_gate, "Slice 1")
        self.assertEqual((mode, terminal), ("in-session", None))
        mode, terminal = pm_runner.resolve_repair_action(control_repair, "validation", True, 1, control_gate, "Slice 1")
        self.assertEqual(mode, "terminal")
        self.assertEqual(terminal.status, "blocked")
        self.assertIn("repair budget exhausted", terminal.reason)

        for signature in ("idle-no-progress", "transient-service-unavailable"):
            with self.subTest(signature=signature):
                # A generous operational budget (5) isolates the
                # same-signature circuit breaker below from the separate
                # operational_round budget check exercised afterward — with
                # budget=1 the operational_round exhaustion would otherwise
                # fire at the same third call as the circuit breaker.
                repair = pm_state.default_repair_state()
                gate = pm_models.GateDecision("repairable", "operational", signature=signature)
                mode, terminal = pm_runner.resolve_repair_action(repair, signature, True, 5, gate, "Slice 1")
                self.assertEqual((mode, terminal), ("in-session", None))
                self.assertEqual(repair["round"], 0)
                self.assertEqual(repair["operational_round"], 1)
                # The circuit breaker still fires on a third consecutive
                # failure of the identical operational signature, well
                # inside the generous operational budget.
                mode, terminal = pm_runner.resolve_repair_action(repair, signature, True, 5, gate, "Slice 1")
                self.assertEqual(mode, "fresh-session")
                self.assertIsNone(terminal)
                self.assertEqual(repair["round"], 0)
                mode, terminal = pm_runner.resolve_repair_action(repair, signature, True, 5, gate, "Slice 1")
                self.assertEqual(mode, "terminal")
                self.assertEqual(terminal.status, "needs-human")
                self.assertIn("circuit breaker", terminal.reason)

                # Separately: the operational_round budget alone (immune to
                # signature alternation and dead sessions) still bounds a run
                # that never trips the same-signature streak. Alternating
                # between the two operational signatures resets `streak` to
                # 1 every call, and a dead session never touches
                # last_signature/signature_streak at all -- exactly the two
                # evasions an earlier draft of this fix was vulnerable to.
                alternating = pm_state.default_repair_state()
                other_signature = next(
                    s for s in ("idle-no-progress", "transient-service-unavailable") if s != signature
                )
                alt_gate = pm_models.GateDecision("repairable", "operational", signature=other_signature)
                sequence = [(signature, gate, True), (other_signature, alt_gate, False)] * 3
                terminal = None
                for call_signature, call_gate, session_alive in sequence:
                    mode, terminal = pm_runner.resolve_repair_action(
                        alternating, call_signature, session_alive, 3, call_gate, "Slice 1"
                    )
                    if terminal is not None:
                        break
                self.assertIsNotNone(terminal, "an alternating operational signature must still be bounded")
                self.assertEqual(terminal.status, "blocked")
                self.assertIn("operational repair budget exhausted", terminal.reason)

    def test_fresh_session_repair_prompt_preserves_archived_context_ledgers(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(
            state,
            run_dir,
            repair={
                "round": 1,
                "last_signature": "validation",
                "signature_streak": 1,
                "session_generation": 1,
                "operational_round": 0,
            },
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
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)

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
        plan_slice = pm_plan.parse_plan(self.plan)[0]
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
            result = pm_runner.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)
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
        plan_slice = pm_plan.parse_plan(self.plan)[0]
        sections = dict(plan_slice.sections)
        sections["Validation Plan"] += "\n- Reviewer evidence: run one bounded read-only support check."
        read_only_slice = pm_models.PlanSlice(plan_slice.number, plan_slice.title, plan_slice.body, sections)
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        policy_path = pm_runtime.write_reviewer_policy(
            state, read_only_slice, artifact, ("opencode",), "model", None,
            before_head="a" * 40, session_generation=1, repair_round=0,
        )
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["schema_version"], 2)
        self.assertNotIn("allowed_access", policy)
        self.assertNotIn("allowed_roles", policy)

    def test_generated_reviewer_policy_matches_orchestrator_schema_v2(self):
        state = self.init_run()
        (self.repo / "README.md").write_text("review context\n", encoding="utf-8")
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm_plan.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        policy_path = pm_runtime.write_reviewer_policy(
            state, plan_slice, artifact, ("opencode",), "model", None,
            before_head="a" * 40, session_generation=1, repair_round=0,
        )
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
        base = pm_plan.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)

        default_policy_path = pm_runtime.write_reviewer_policy(
            state, base, artifact, ("opencode",), "model", None,
            before_head="a" * 40, session_generation=1, repair_round=0,
        )
        default_policy = json.loads(default_policy_path.read_text(encoding="utf-8"))
        self.assertEqual(default_policy["reserved_skill_sets"], [])

        sections = dict(base.sections)
        sections["Risk Flags"] = sections.get("Risk Flags", "") + "\n- Independent audit required: yes"
        opt_in_slice = pm_models.PlanSlice(base.number, base.title, base.body, sections)
        self.assertTrue(opt_in_slice.independent_audit_required)
        opt_in_policy_path = pm_runtime.write_reviewer_policy(
            state, opt_in_slice, artifact, ("opencode",), "model", None,
            before_head="a" * 40, session_generation=1, repair_round=0,
        )
        opt_in_policy = json.loads(opt_in_policy_path.read_text(encoding="utf-8"))
        self.assertEqual(opt_in_policy["reserved_skill_sets"], [["drift-audit"], ["code-review"]])

    def test_stop_with_evidence_records_terminal_slice_attempt(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm_plan.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("unaccepted work\n", encoding="utf-8")
        state["status"] = "running"
        state["current_slice"] = pm_state.current_slice_state(
            self.repo.resolve(),
            plan_slice,
            artifact,
            "pm_test_slice-001_a1",
            1,
            pm_utils.utc_now(),
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
            self.assertEqual(pm_commands.stop_with_evidence(args), 0)
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
        plan_slice = pm_plan.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["status"] = "running"
        state["current_slice"] = pm_state.current_slice_state(
            self.repo.resolve(),
            plan_slice,
            artifact,
            "pm_test_slice-001_a1",
            1,
            pm_utils.utc_now(),
            git(self.repo, "rev-parse", "HEAD"),
            reviewer_policy={"sha256": "a" * 64, "policy": {}},
            prior_slice_context=self.prior_context_metadata(artifact),
        )
        pm_state.activate_controller_state(run_dir / "run.json", state)
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
            self.assertEqual(pm_commands.stop_with_evidence(args), 0)

        result = json.loads(output.getvalue())
        self.assertTrue(result["controller_state_recovered"])
        self.assertTrue(Path(result["tamper_evidence_path"]).is_file())
        stopped = pm_state.load_run(run_dir)
        self.assertEqual(stopped["status"], "needs-human")
        self.assertIsNone(stopped["current_slice"])
        fake_adapter.force_stop.assert_called_once_with("pm_test_slice-001_a1")

    def test_stop_with_evidence_halts_when_both_state_copies_are_unreadable(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        pm_state.activate_controller_state(run_dir / "run.json", state)
        (run_dir / "run.json").write_text("{broken mirror", encoding="utf-8")
        controller_path = pm_state.controller_state_path(run_dir)
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
            self.assertEqual(pm_commands.stop_with_evidence(args), 0)

        result = json.loads(output.getvalue())
        self.assertFalse(result["state_updated"])
        self.assertTrue(Path(result["evidence_path"]).is_file())
        fake_adapter.force_stop.assert_called_once_with("pm_test_slice-001_a1")
        cancel_reviewers.assert_called_once_with(run_dir)

    def test_stop_with_evidence_halts_even_when_plan_changed_mid_run(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm_plan.parse_plan(self.plan)[0]
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        before = git(self.repo, "rev-parse", "HEAD")
        state["status"] = "running"
        state["current_slice"] = pm_state.current_slice_state(
            self.repo.resolve(),
            plan_slice,
            artifact,
            "pm_test_slice-001_a1",
            1,
            pm_utils.utc_now(),
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
            self.assertEqual(pm_commands.stop_with_evidence(args), 0)
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
            "started_at": pm_utils.utc_now(),
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
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)
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
            "started_at": pm_utils.utc_now(),
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
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)
        fake_adapter.force_stop.assert_called_once_with("pm_test_slice-001_a1")
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "partial")
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["status"], "pass")

    def test_finalize_blocks_pass_result_that_drops_an_archived_ledger_item(self):
        # finalize_model_supervised_slice must thread current_slice.repair's
        # last_signature into verify_gate: a fresh pass result that silently
        # drops a residual finding archived by an earlier repair round is not
        # accepted, and the run stays live for a targeted repair instead.
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(state, run_dir)
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        archived_finding = {
            "source": "validation",
            "severity": "low",
            "summary": "Flaky retry needed for the slow CI shard.",
            "disposition": "deferred-inconsequential",
            "rationale": "Out of scope for this slice.",
            "suggested_follow_up": "Track in a later slice.",
        }
        (artifact / "developer-result-repair-1.json").write_text(
            json.dumps(
                {
                    "schema_version": pm_constants.SCHEMA_VERSION,
                    "slice_id": "Slice 1",
                    "status": "repairable",
                    "residual_findings": [archived_finding],
                    "continuation_notes": [],
                }
            ),
            encoding="utf-8",
        )
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        output = io.StringIO()
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(output):
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["finalized"])
        self.assertEqual(result["status"], "repairable")
        self.assertEqual(result["repair"]["last_signature"], "ledger-retention")
        fake_adapter.force_stop.assert_not_called()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "resuming")
        # The rendered in-session repair prompt must carry the full archived
        # item end-to-end (every field, not just the summary a truncated gate
        # reason could carry alone) so a Developer can actually copy it
        # verbatim, satisfying the gate's field-retention check.
        repair_prompt_text = (artifact / "repair-prompt-repair-1.md").read_text(encoding="utf-8")
        for field_value in archived_finding.values():
            self.assertIn(field_value, repair_prompt_text, field_value)

    def test_finalize_context_budget_repair_round_exempts_ledger_retention(self):
        # A round that follows a context-budget repair is explicitly instructed
        # to condense ledger wording; last_repair_signature="context-budget"
        # must reach verify_gate so that round's ledger drop does not block
        # acceptance.
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = self._model_supervised_current_slice(
            state,
            run_dir,
            repair={
                "round": 1,
                "last_signature": "context-budget",
                "signature_streak": 1,
                "session_generation": 1,
                "operational_round": 0,
            },
        )
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        archived_finding = {
            "source": "validation",
            "severity": "low",
            "summary": "Flaky retry needed for the slow CI shard.",
            "disposition": "deferred-inconsequential",
            "rationale": "Out of scope for this slice.",
            "suggested_follow_up": "Track in a later slice.",
        }
        (artifact / "developer-result-repair-1.json").write_text(
            json.dumps(
                {
                    "schema_version": pm_constants.SCHEMA_VERSION,
                    "slice_id": "Slice 1",
                    "status": "repairable",
                    "residual_findings": [archived_finding],
                    "continuation_notes": [],
                }
            ),
            encoding="utf-8",
        )
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture
        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)
        fake_adapter.force_stop.assert_called_once()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["status"], "pass")

    def test_finalize_persists_gate_time_reviewer_provenance_despite_adverse_cancel(self):
        # Finding 17: cancel_run_reviewers refreshes reviewer-runs-summary.json
        # for evidence capture, but it must run only after the slice entry is
        # built and persisted. If the entry were rebuilt from post-cancel
        # evidence (or cancel ran first), a reviewer that turns adverse in
        # that window would become the durable "latest" verdict even though
        # the gate itself saw a clean PASS.
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
        self.write_validated_reviewer_run(artifact)
        reviewer_policy = pm_runtime.reviewer_policy_snapshot(artifact / "reviewer-policy.json")
        state["status"] = "running"
        state["supervision"]["mode"] = "model-supervised"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm_utils.utc_now(),
            "before_head": before,
            "reviewer_tools": ["opencode"],
            "pause": None,
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": reviewer_policy,
            "prior_slice_context": self.prior_context_metadata(artifact),
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.session_exists.return_value = True

        def fake_capture(session_name, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("pane\n", encoding="utf-8")

        fake_adapter.capture.side_effect = fake_capture

        def adversarial_cancel(_run_dir):
            reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
            status_path = next(reviewer_run.glob("*code-review-status.json"))
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["skill_verdicts"]["code-review"] = "FAIL"
            status["finished_at"] = "2026-01-01T00:05:00Z"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            pm_runtime.capture_reviewer_runs_summary(artifact)
            return []

        with mock.patch.object(pm_runner, "TmuxHarnessAdapter", return_value=fake_adapter):
            with mock.patch.object(pm_runner, "cancel_run_reviewers", side_effect=adversarial_cancel) as cancel:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)

        cancel.assert_called_once()
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["entry"]["audit_provenance"]["code-review"]["performed_by"], "reviewer")
        persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["slices"][0]["audit_provenance"]["code-review"]["performed_by"], "reviewer")
        self.assertEqual(persisted["slices"][0]["audit_provenance"]["code-review"]["reviewer_tool"], "opencode")
        # The adverse rewrite did land on disk (evidence capture still ran) —
        # only the persisted *entry* must stay unaffected by it.
        summary = json.loads((artifact / "reviewer-runs-summary.json").read_text(encoding="utf-8"))
        adverse_status = next(
            status
            for run in summary["runs"]
            for status in run["reviewers"]
            if "code-review" in status.get("label", "")
        )
        self.assertEqual(adverse_status["skill_verdicts"]["code-review"], "FAIL")

    def test_finalize_pass_builds_slice_entry_exactly_once(self):
        # Finding 17: the passing path must build the slice entry a single
        # time and reuse it for the context-budget projection and the
        # persisted record, rather than building a projected entry and then a
        # second, independent one inside _finalize_terminal.
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
            "started_at": pm_utils.utc_now(),
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
            with mock.patch.object(pm_runner, "slice_entry_from_gate", wraps=pm_runner.slice_entry_from_gate) as spy:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)

        self.assertEqual(spy.call_count, 1)
        result = json.loads(output.getvalue())
        persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["slices"][0], result["entry"])
        self.assertIsNotNone(persisted["slices"][0]["completed_at"])

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
                    self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 0)

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
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 2)
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
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 2)

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
            repair={
                "round": 3,
                "last_signature": "validation",
                "signature_streak": 1,
                "session_generation": 1,
                "operational_round": 0,
            },
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
                self.assertEqual(pm_commands.finalize_slice(self._finalize_args()), 2)
        fake_adapter.force_stop.assert_called_once()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("repair budget exhausted", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(len(state["slices"]), 1)
        self.assertEqual(state["slices"][0]["repair"]["round"], 3)

    def test_repair_state_requires_complete_schema_v3_state(self):
        with self.assertRaisesRegex(pm_models.PmError, "missing required repair state"):
            pm_state.repair_state(None)
        with self.assertRaisesRegex(pm_models.PmError, "missing required repair state"):
            pm_state.repair_state({"slice_id": "Slice 1"})
        self.assertEqual(
            pm_state.repair_state(
                {
                    "repair": {
                        "round": 2,
                        "last_signature": "drift",
                        "signature_streak": 1,
                        "session_generation": 3,
                        "operational_round": 1,
                    }
                }
            ),
            {"round": 2, "last_signature": "drift", "signature_streak": 1, "session_generation": 3, "operational_round": 1},
        )

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_model_supervised_send_then_finalize_accepts_corrected_slice(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "in_session_repair.py"
        write_in_session_repair_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.init_run(args), 0)
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
            self.assertEqual(pm_commands.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.wait(command_args), 0)
        finalize_output = io.StringIO()
        with contextlib.redirect_stdout(finalize_output):
            self.assertEqual(pm_commands.finalize_slice(command_args), 0)
        first = json.loads(finalize_output.getvalue())
        self.assertEqual(first["status"], "repairable")
        self.assertEqual(first["mode"], "in-session")
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "resuming")
        self.assertIsNotNone(state["current_slice"])
        command_args.text = first["send_text"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.send(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.finalize_slice(command_args), 0)
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
            self.assertEqual(pm_commands.init_run(args), 0)
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
            self.assertEqual(pm_commands.start_slice(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.wait(command_args), 0)
        first_output = io.StringIO()
        with contextlib.redirect_stdout(first_output):
            self.assertEqual(pm_commands.finalize_slice(command_args), 0)
        first = json.loads(first_output.getvalue())
        self.assertEqual(first["mode"], "in-session")
        command_args.text = first["send_text"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.send(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.wait(command_args), 0)
        second_output = io.StringIO()
        with contextlib.redirect_stdout(second_output):
            self.assertEqual(pm_commands.finalize_slice(command_args), 0)
        second = json.loads(second_output.getvalue())
        self.assertEqual(second["mode"], "fresh-session")
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["current_slice"]["attempt"], 2)
        self.assertTrue(state["current_slice"]["tmux_session"].endswith("_a2"))
        self.assertEqual(state["current_slice"]["repair"]["signature_streak"], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.wait(command_args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm_commands.finalize_slice(command_args), 2)
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("circuit breaker", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["slices"][0]["repair"]["round"], 2)

    def test_start_slice_reaps_stale_run_sessions_before_launch(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm_plan.parse_plan(self.plan)[0]
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
            result = pm_runner.start_model_supervised_slice(args, self.repo.resolve(), state, plan_slice, run_dir)

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
            "started_at": pm_utils.utc_now(),
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
            until=pm_utils.utc_now(),
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
                self.assertEqual(pm_commands.pause_until(args), 0)
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
            "started_at": pm_utils.utc_now(),
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
                self.assertEqual(pm_commands.wait(args), 0)

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
            "started_at": pm_utils.utc_now(),
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
            with self.assertRaisesRegex(pm_models.PmError, "max_single_pause_seconds"):
                pm_commands.pause_until(args)

        state["supervision"]["max_single_pause_seconds"] = 21600
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Weekly usage limit reached."}
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with self.assertRaisesRegex(pm_models.PmError, "hard-stop operational hint"):
                pm_commands.pause_until(args)

    def test_reset_slice_pause_counters(self):
        state = {"supervision": {"pause_counters": {"consecutive_pauses_current_slice": 2, "cumulative_pause_seconds_run": 900}}}
        pm_state.reset_slice_pause_counters(state)
        self.assertEqual(state["supervision"]["pause_counters"]["consecutive_pauses_current_slice"], 0)
        # The cumulative per-run budget must survive the per-slice reset.
        self.assertEqual(state["supervision"]["pause_counters"]["cumulative_pause_seconds_run"], 900)

    # --- Shared repair-decision core ---------------------------------------

    def test_resolve_repair_action_decisions(self):
        gate = pm_models.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = pm_state.default_repair_state()

        mode, terminal = pm_runner.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("in-session", None))
        self.assertEqual(
            repair,
            {
                "round": 1,
                "last_signature": "validation",
                "signature_streak": 1,
                "session_generation": 1,
                "operational_round": 0,
            },
        )

        mode, terminal = pm_runner.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("fresh-session", None))
        self.assertEqual(repair["signature_streak"], 2)

        mode, terminal = pm_runner.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual(mode, "terminal")
        self.assertEqual(terminal.status, "needs-human")
        self.assertIn("circuit breaker", terminal.reason)

    def test_resolve_repair_action_dead_session_relaunch_keeps_breaker(self):
        gate = pm_models.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = pm_state.default_repair_state()
        repair.update(round=1, last_signature="validation", signature_streak=1)
        mode, terminal = pm_runner.resolve_repair_action(repair, "validation", False, 3, gate, "Slice 1")
        self.assertEqual((mode, terminal), ("relaunch", None))
        self.assertEqual(repair["round"], 2)
        # Breaker state untouched: a dead session is a runner condition.
        self.assertEqual(repair["signature_streak"], 1)

    def test_resolve_repair_action_budget_exhaustion_is_terminal(self):
        gate = pm_models.GateDecision("repairable", "validation did not pass", None, (), "validation")
        repair = pm_state.default_repair_state()
        repair["round"] = 3
        mode, terminal = pm_runner.resolve_repair_action(repair, "validation", True, 3, gate, "Slice 1")
        self.assertEqual(mode, "terminal")
        self.assertEqual(terminal.status, "blocked")
        self.assertIn("repair budget exhausted", terminal.reason)

    # --- Readiness fallback and orphan detection ---------------------------


if __name__ == "__main__":
    unittest.main()

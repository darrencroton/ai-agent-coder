"""Observe/send primitives, operational hints, and hard-prompt detection tests."""

from pm_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the pm module


class ObservationHintTests(PmTestCase):
    def test_idle_stall_requires_separate_windows_spanning_configured_ceiling(self):
        self.prepare_committed_repo()
        state = self.init_run()
        state["current_slice"] = {"slice_id": "Slice 1", "attempt": 1}
        snapshot = {"current_slice": {"slice_id": "Slice 1", "attempt": 1}}
        base = datetime(2026, 7, 13, tzinfo=timezone.utc)
        for offset in (0, 300, 600):
            pm.append_operational_event(
                self.repo,
                state,
                {
                    "kind": "observation",
                    "slice_id": "Slice 1",
                    "attempt": 1,
                    "operational_hint_kinds": ["idle_no_progress"],
                    "detected_at": (base + timedelta(seconds=offset)).isoformat(),
                },
            )
        self.assertTrue(pm.idle_stall_due(self.repo, state, snapshot))
        pm.append_operational_event(
            self.repo,
            state,
            {
                "kind": "observation",
                "slice_id": "Slice 1",
                "attempt": 1,
                "operational_hint_kinds": [],
                "detected_at": (base + timedelta(seconds=601)).isoformat(),
            },
        )
        self.assertFalse(pm.idle_stall_due(self.repo, state, snapshot))

    def test_idle_stall_window_resets_after_automatic_repair_send(self):
        self.prepare_committed_repo()
        state = self.init_run()
        state["current_slice"] = {"slice_id": "Slice 1", "attempt": 1}
        snapshot = {"current_slice": {"slice_id": "Slice 1", "attempt": 1}}
        base = datetime(2026, 7, 13, tzinfo=timezone.utc)
        for offset in (0, 300, 600):
            pm.append_operational_event(
                self.repo,
                state,
                {
                    "kind": "observation",
                    "slice_id": "Slice 1",
                    "attempt": 1,
                    "operational_hint_kinds": ["idle_no_progress"],
                    "detected_at": (base + timedelta(seconds=offset)).isoformat(),
                },
            )
        pm.append_operational_event(
            self.repo,
            state,
            {
                "kind": "send",
                "slice_id": "Slice 1",
                "attempt": 1,
                "detected_at": (base + timedelta(seconds=601)).isoformat(),
            },
        )
        pm.append_operational_event(
            self.repo,
            state,
            {
                "kind": "observation",
                "slice_id": "Slice 1",
                "attempt": 1,
                "operational_hint_kinds": ["idle_no_progress"],
                "detected_at": (base + timedelta(seconds=602)).isoformat(),
            },
        )
        self.assertFalse(pm.idle_stall_due(self.repo, state, snapshot))
    def test_observe_without_current_slice_returns_snapshot_and_event(self):
        state = self.init_run()
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(pm.observe(args), 0)

        snapshot = json.loads(output.getvalue())
        self.assertIsNone(snapshot["current_slice"])
        self.assertEqual(snapshot["result"]["parse_status"], "no-current-slice")
        records = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[0]["kind"], "observation")

    def test_observe_current_slice_captures_live_pane_and_result_state(self):
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
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")

        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {"running": True, "active": True, "capture": "pane text"}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(pm.observe(args), 0)

        snapshot = json.loads(output.getvalue())
        self.assertEqual(snapshot["process"]["running"], True)
        self.assertEqual(snapshot["pane"]["tail"], "pane text")
        self.assertTrue((artifact / "pane-capture-live-latest.txt").exists())
        self.assertTrue((artifact / "observation-latest.json").exists())

    def test_send_records_literal_text_for_current_session(self):
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
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "ready"}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            text="You were interrupted. Continue.",
            reason="resume after reset",
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.send(args), 0)
        fake_adapter.send_literal.assert_called_once_with("pm_test_slice-001_a1", "You were interrupted. Continue.")
        records = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[-1]["kind"], "send")
        self.assertEqual(records[-1]["text"], "You were interrupted. Continue.")

    def test_send_refuses_hard_prompt_from_observation(self):
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
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Approve this action"}
        fake_adapter.detect_hard_prompt.return_value = {"present": True, "kinds": ["approval_prompt"], "markers": ["Approve this action"]}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            text="continue",
            reason="test",
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with self.assertRaisesRegex(pm.PmError, "hard prompt"):
                pm.send(args)
        fake_adapter.send_literal.assert_not_called()

    def test_hard_prompt_detection_ignores_pm_safety_text(self):
        safety_text = (
            "Commit creation is authorized only after validation. "
            "Do not push, open a PR, release, deploy, change dependencies/licenses, "
            "request secrets, or perform destructive actions unless explicitly authorized."
        )

        hard_prompt = pm.TmuxHarnessAdapter.detect_hard_prompt(safety_text)
        self.assertFalse(hard_prompt["present"])

        hints = pm.extract_operational_hints(safety_text, process_running=True, result_exists=False)
        self.assertFalse(any(hint["kind"] == "external_side_effect_request" for hint in hints))

    def test_full_rendered_developer_prompt_triggers_no_hard_prompt_or_hard_stop_hint(self):
        # render_developer_prompt embeds the compact PM slice delegation
        # contract. A doc phrase anywhere in that contract that
        # happens to collide with a HARD_PROMPT_MARKERS substring would make
        # the repair-send guard and _raise_on_hard_stop_hints refuse delivery
        # on almost every run, since the embedded contract stays in tmux
        # scrollback for most of a slice's life. Regression for the
        # prompt collision found in review.
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        plan_slice = pm.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = pm.render_developer_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            ("claude", "codex", "copilot", "opencode"),
            "some-model",
            "medium",
        )
        self.assertIn("Project Manager Slice Reviewer Contract", prompt)

        hard_prompt = pm.TmuxHarnessAdapter.detect_hard_prompt(prompt)
        self.assertFalse(hard_prompt["present"], hard_prompt.get("kinds"))

        hints = pm.extract_operational_hints(prompt, process_running=True, result_exists=False)
        hard_stop_hints = [hint["kind"] for hint in hints if hint.get("hard_stop")]
        self.assertEqual(hard_stop_hints, [])

    def test_hard_prompt_detection_keeps_external_side_effect_prompts(self):
        prompt = "Approve deploy to production? [y/n]"

        hard_prompt = pm.TmuxHarnessAdapter.detect_hard_prompt(prompt)
        self.assertTrue(hard_prompt["present"])
        self.assertIn("external_side_effect_request", hard_prompt["kinds"])

        hints = pm.extract_operational_hints(prompt, process_running=True, result_exists=False)
        external = next(hint for hint in hints if hint["kind"] == "external_side_effect_request")
        self.assertTrue(external["hard_stop"])

    def test_operational_hints_ignore_instructional_timeout_flags(self):
        text = 'Use reviewer_jobs.py wait --run-dir "$run_dir" --label check --timeout 300.'
        hints = pm.extract_operational_hints(text, process_running=True, result_exists=False)
        self.assertFalse(any(hint["kind"] == "network_transient" for hint in hints))

        real_error = pm.extract_operational_hints(
            "Network error: request timed out while contacting the provider.", process_running=True, result_exists=False
        )
        self.assertTrue(any(hint["kind"] == "network_transient" for hint in real_error))

    def test_operational_hints_parse_rolling_limit_duration(self):
        now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone(timedelta(hours=10)))

        hints = pm.extract_operational_hints(
            "Usage limit reached. Try again in 2 hours 30 minutes.",
            process_running=True,
            result_exists=False,
            now=now,
        )

        usage = next(hint for hint in hints if hint["kind"] == "usage_limit")
        self.assertEqual(usage["subtype"], "rolling_window")
        self.assertFalse(usage["hard_stop"])
        self.assertEqual(usage["retry_after_seconds"], 9000)
        self.assertEqual(usage["reset_at"], "2026-07-05T16:30:00+10:00")
        self.assertEqual(usage["recovery_guidance"], "pause-until-reset-plus-buffer-then-send-continuation")

    def test_operational_hints_parse_rolling_limit_absolute_time_around_midnight(self):
        now = datetime(2026, 7, 5, 23, 55, tzinfo=timezone(timedelta(hours=10)))

        hints = pm.extract_operational_hints(
            "Session limit reached and will reset at 12:10AM.",
            process_running=True,
            result_exists=False,
            now=now,
        )

        usage = next(hint for hint in hints if hint["kind"] == "usage_limit")
        self.assertEqual(usage["subtype"], "rolling_window")
        self.assertFalse(usage["hard_stop"])
        self.assertEqual(usage["reset_at"], "2026-07-06T00:10:00+10:00")

        utc_hints = pm.extract_operational_hints(
            "Usage limit reached and will reset at 14:30 UTC.",
            process_running=True,
            result_exists=False,
            now=datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc),
        )
        utc_usage = next(hint for hint in utc_hints if hint["kind"] == "usage_limit")
        self.assertEqual(utc_usage["reset_at"], "2026-07-05T14:30:00+00:00")

    def test_operational_hints_prefer_relative_duration_over_absolute_time(self):
        now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone(timedelta(hours=10)))

        hints = pm.extract_operational_hints(
            "Usage limit reached. It resets at 6:00pm, but try again in 45 minutes.",
            process_running=True,
            result_exists=False,
            now=now,
        )

        usage = next(hint for hint in hints if hint["kind"] == "usage_limit")
        self.assertEqual(usage["retry_after_seconds"], 2700)
        self.assertEqual(usage["reset_at"], "2026-07-05T14:45:00+10:00")

    def test_operational_hints_mark_weekly_monthly_account_and_unknown_limits_hard_stop(self):
        cases = [
            ("Weekly usage limit reached. Try again next week.", "weekly_window"),
            ("Monthly quota cap reached for this workspace.", "monthly_window"),
            ("Subscription plan limit exhausted. Upgrade billing to continue.", "account_or_billing"),
            ("Usage limit reached.", "unknown_limit"),
        ]
        for text, subtype in cases:
            with self.subTest(subtype=subtype):
                hints = pm.extract_operational_hints(text, process_running=True, now=datetime(2026, 7, 5, tzinfo=timezone.utc))
                usage = next(hint for hint in hints if hint["kind"] == "usage_limit" and hint["subtype"] == subtype)
                self.assertTrue(usage["hard_stop"])
                self.assertEqual(usage["recovery_guidance"], "stop-for-user")

    def test_operational_hints_surface_sub_cap_weekly_usage_warning_without_blocking(self):
        text = (
            "You've used 91% of your weekly limit · resets Jul 9 at 8am (Australia/Sydney). "
            "Until July 7, you can use up to 50% of your plan's weekly usage limit on Fable 5. "
            "If you hit your limit, you can continue on Fable 5 with usage credits."
        )

        hints = pm.extract_operational_hints(text, process_running=True, result_exists=False)

        usage = next(hint for hint in hints if hint["kind"] == "usage_limit")
        self.assertEqual(usage["subtype"], "warning")
        self.assertFalse(usage["hard_stop"])
        self.assertEqual(usage["recovery_guidance"], "continue-with-observation")

    def test_operational_hints_classify_service_unavailable_and_ambiguous_absolute_reset(self):
        now = datetime(2026, 7, 5, 0, 10, tzinfo=timezone(timedelta(hours=10)))

        service = pm.extract_operational_hints(
            "Service unavailable. Please try again later in 10 minutes.",
            process_running=True,
            now=now,
        )
        service_hint = next(hint for hint in service if hint["kind"] == "service_unavailable")
        self.assertFalse(service_hint["hard_stop"])
        self.assertEqual(service_hint["confidence"], "high")
        self.assertEqual(service_hint["retry_after_seconds"], 600)

        generic = pm.extract_operational_hints("Unexpected server error", process_running=True, now=now)
        generic_hint = next(hint for hint in generic if hint["kind"] == "service_unavailable")
        self.assertEqual(generic_hint["confidence"], "medium")

        ambiguous = pm.extract_operational_hints(
            "Session limit reached and will reset at 11:55pm.",
            process_running=True,
            now=now,
            max_single_pause_seconds=21600,
        )
        usage = next(hint for hint in ambiguous if hint["kind"] == "usage_limit")
        self.assertEqual(usage["subtype"], "unknown_limit")
        self.assertTrue(usage["hard_stop"])

    def test_only_high_confidence_service_unavailable_reclassifies_terminal_report(self):
        terminal = pm.GateDecision("blocked", "developer reported blocked", {"status": "blocked"})
        high = {
            "kind": "service_unavailable",
            "subtype": "transient",
            "confidence": "high",
            "hard_stop": False,
            "recovery_guidance": "bounded-retry",
        }
        repaired = pm.reclassify_high_confidence_transient_stop(terminal, [high])
        self.assertEqual(repaired.status, "repairable")
        self.assertEqual(repaired.signature, "transient-service-unavailable")
        for changed in (
            {**high, "confidence": "medium"},
            {**high, "kind": "network_transient"},
            {**high, "hard_stop": True},
        ):
            with self.subTest(changed=changed):
                self.assertIs(pm.reclassify_high_confidence_transient_stop(terminal, [changed]), terminal)

    def test_operational_hints_distinguish_live_and_exited_rolling_limit_guidance(self):
        now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
        text = "Usage limit reached. Try again in 1 hour."

        live = pm.extract_operational_hints(text, process_running=True, result_exists=False, now=now)
        exited = pm.extract_operational_hints(text, process_running=False, result_exists=False, now=now)
        ready = pm.extract_operational_hints(text, process_running=False, result_exists=True, now=now)

        self.assertEqual(
            next(h for h in live if h["kind"] == "usage_limit")["recovery_guidance"],
            "pause-until-reset-plus-buffer-then-send-continuation",
        )
        self.assertEqual(
            next(h for h in exited if h["kind"] == "usage_limit")["recovery_guidance"],
            "restart-from-clean-authorized-state-or-stop-for-user",
        )
        self.assertEqual(next(h for h in ready if h["kind"] == "usage_limit")["recovery_guidance"], "finalize-slice")

    def test_observe_exposes_operational_hints_and_send_refuses_hard_stop_hint(self):
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
        }
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        fake_adapter = mock.Mock()
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Weekly usage limit reached."}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            text="continue",
            reason="test",
            harness_command=None,
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )

        with mock.patch.object(pm_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(pm.observe(args), 0)
            snapshot = json.loads(output.getvalue())
            self.assertEqual(snapshot["operational_hints"][0]["kind"], "usage_limit")
            self.assertTrue(snapshot["operational_hints"][0]["hard_stop"])
            with self.assertRaisesRegex(pm.PmError, "hard-stop operational hint"):
                pm.send(args)
        fake_adapter.send_literal.assert_not_called()

    def test_send_rejects_multiline_text(self):
        self.prepare_committed_repo()
        self.init_run()
        args = argparse.Namespace(repo=str(self.repo), run="current", text="line one\nline two", reason="test")
        with self.assertRaisesRegex(pm.PmError, "single line"):
            pm.send(args)

    def test_send_literal_rejects_multiline_text(self):
        adapter = pm.TmuxHarnessAdapter("codex", "python fake.py")
        with self.assertRaisesRegex(pm.PmError, "single line"):
            adapter.send_literal("some-session", "line one\nline two")


if __name__ == "__main__":
    unittest.main()

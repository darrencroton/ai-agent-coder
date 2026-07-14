"""Batch runtime (run-next / run remaining / reconcile / stop) tests."""

from pm_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the pm module


class RuntimeBatchTests(PmTestCase):
    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_records_model_supervised_start_failure_detail(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake.py"
        write_hanging_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)

        with mock.patch.object(pm.TmuxHarnessAdapter, "send_prompt", side_effect=pm.PmError("injected send failure")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.run_next(self._run_next_args(harness)), 2)

        state = json.loads(((self.repo / ".ai-pm" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertIn("failed to start model-supervised slice: injected send failure", state["stop_reason"])

    def test_reconcile_repairs_failed_slice_after_commit_hash_evidence_mismatch(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = run_dir / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash="0" * 40)
        entry = self.terminal_slice_entry(
            state,
            status="fail",
            artifact_dir=str(artifact.relative_to(self.repo.resolve())),
            before_head=before,
            commit={"requested": True, "created": True, "hash": "0" * 40},
        )
        entry.update(
            changed_files=["README.md"],
            validation=[{"command": "test", "result": "pass", "notes": ""}],
            drift_audit={"verdict": "PASS", "path": "drift-audit.md"},
            code_review={"verdict": "PASS", "path": "code-review.md"},
            gate_reason="reported commit is not the current HEAD",
        )
        state["slices"].append(entry)
        state["status"] = "failed"
        state["stop_reason"] = "reported commit is not the current HEAD"
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")

        args = argparse.Namespace(repo=str(self.repo), run="current")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.reconcile(args), 0)

        repaired = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(repaired["slices"][0]["status"], "pass")
        self.assertEqual(repaired["slices"][0]["commit"]["hash"], after)
        self.assertEqual(repaired["status"], "partial")

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_executes_toy_harness_and_records_pass(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake_harness.py"
        write_fake_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(run_args), 0)
        state = json.loads(((self.repo / ".ai-pm" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["supervision"]["mode"], "deterministic-batch")
        self.assertEqual(state["slices"][0]["status"], "pass")
        self.assertEqual(state["slices"][0]["changed_files"], ["README.md"])
        slice_dir = (self.repo / ".ai-pm" / "current").resolve() / "slices" / "slice-001"
        self.assertTrue((slice_dir / "pane-capture.txt").exists())
        self.assertTrue((slice_dir / "pane-capture-live-latest.txt").exists())
        activity_path = slice_dir / "activity-attempt-1.jsonl"
        self.assertTrue(activity_path.exists())
        activity = json.loads(activity_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(set(activity), {"active", "checked_at", "running"})
        # First-attempt-pass guardrail: exactly one session, no repair
        # artifacts, and the explicit round-zero slice-entry shape.
        self.assertFalse((slice_dir / "activity-attempt-2.jsonl").exists())
        self.assertFalse((slice_dir / "repair-prompt.md").exists())
        self.assertFalse((slice_dir / "repair-prompt-repair-1.md").exists())
        self.assertFalse((slice_dir / "developer-result-repair-1.json").exists())
        self.assertFalse((slice_dir / "pane-capture-repair-1.txt").exists())
        self.assertEqual(
            set(state["slices"][0]),
            {
                "slice_id", "title", "status", "started_at", "completed_at", "artifact_dir",
                "before_head", "changed_files", "summary", "validation", "drift_audit", "code_review",
                "audit_provenance",
                "commit", "next_action", "blockers", "gate_reason", "reviewer_tools", "reviewer_policy",
                "repair", "residual_findings", "slice_summary",
            },
        )
        self.assertEqual(state["slices"][0]["audit_provenance"]["drift-audit"]["performed_by"], "developer-self-audit")
        self.assertEqual(state["slices"][0]["audit_provenance"]["code-review"]["performed_by"], "developer-self-audit")

    def test_run_next_refuses_while_current_slice_is_active(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        self._model_supervised_current_slice(
            state,
            run_dir,
            repair={"round": 1, "last_signature": "validation", "signature_streak": 1, "session_generation": 1},
        )
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=5,
            poll_seconds=0.1,
            harness_command="python fake.py",
        )
        with self.assertRaisesRegex(pm.PmError, "active current slice"):
            pm.run_next(run_args)
        run_args.scope = "remaining"
        with self.assertRaisesRegex(pm.PmError, "active current slice"):
            pm.run_remaining(run_args)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_remaining_completes_two_toy_slices(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake_harness.py"
        write_fake_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            scope="remaining",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_remaining(run_args), 0)
        state = json.loads(((self.repo / ".ai-pm" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "complete")
        self.assertEqual([entry["status"] for entry in state["slices"]], ["pass", "pass"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_blocks_when_session_exits_without_result(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "no_result_harness.py"
        write_no_result_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(run_args), 2)
        state = json.loads(((self.repo / ".ai-pm" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("developer result missing", state["stop_reason"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_times_out_hanging_session_with_evidence(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "hanging_harness.py"
        write_hanging_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=3,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(run_args), 2)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        slice_dir = run_dir / "slices" / "slice-001"
        self.assertEqual(state["status"], "blocked")
        self.assertIn("timeout waiting for developer-result.json", state["stop_reason"])
        self.assertIsNone(state["current_slice"])
        self.assertEqual(state["supervision"]["mode"], "deterministic-batch")
        self.assertTrue((slice_dir / "pane-capture-timeout.txt").exists())
        self.assertTrue((slice_dir / "pane-capture.txt").exists())
        self.assertTrue((slice_dir / "activity-attempt-1.jsonl").exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_retries_once_after_repairable_result(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "repairable_then_pass.py"
        write_repairable_then_pass_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(run_args), 0)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["slices"][0]["status"], "pass")
        self.assertTrue((run_dir / "slices" / "slice-001" / "activity-attempt-2.jsonl").exists())
        # The dead-session relaunch consumed one repair round but was not a
        # circuit-breaker step: the breaker state stays untouched.
        self.assertEqual(state["slices"][0]["repair"]["round"], 1)
        self.assertEqual(state["slices"][0]["repair"]["last_signature"], "")
        self.assertEqual(state["slices"][0]["repair"]["session_generation"], 2)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_repairs_in_session_without_new_session(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "in_session_repair.py"
        write_in_session_repair_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(self._run_next_args(harness)), 0)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        slice_dir = run_dir / "slices" / "slice-001"
        self.assertEqual(state["slices"][0]["status"], "pass")
        # One repair round, one session: no attempt-2 artifacts.
        self.assertEqual(state["slices"][0]["repair"], {
            "round": 1,
            "last_signature": "validation",
            "signature_streak": 1,
            "session_generation": 1,
        })
        self.assertFalse((slice_dir / "activity-attempt-2.jsonl").exists())
        # The stale failing result was archived, not re-read.
        archived = json.loads((slice_dir / "developer-result-repair-1.json").read_text(encoding="utf-8"))
        self.assertEqual(archived["validation"], [])
        self.assertTrue((slice_dir / "repair-prompt-repair-1.md").exists())
        self.assertTrue((slice_dir / "pane-capture-repair-1.txt").exists())
        final = json.loads((slice_dir / "developer-result.json").read_text(encoding="utf-8"))
        self.assertEqual(final["changed_files"], ["README.md"])
        events = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual([event["mode"] for event in repair_events], ["in-session"])
        self.assertEqual(repair_events[0]["signature"], "validation")

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_circuit_breaker_escalates_then_stops(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "always_failing_validation.py"
        write_always_failing_validation_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(self._run_next_args(harness)), 2)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        slice_dir = run_dir / "slices" / "slice-001"
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("circuit breaker", state["stop_reason"])
        self.assertIn("validation", state["stop_reason"])
        # Round 1 was an in-session nudge, round 2 a fresh-session escalation,
        # and the third consecutive failure tripped the breaker without
        # consuming a round. Per-round evidence is preserved separately.
        self.assertEqual(state["slices"][0]["repair"]["round"], 2)
        self.assertEqual(state["slices"][0]["repair"]["signature_streak"], 2)
        self.assertEqual(state["slices"][0]["repair"]["session_generation"], 2)
        self.assertTrue((slice_dir / "activity-attempt-2.jsonl").exists())
        # Every per-round artifact family survives across rounds.
        for round_number in (1, 2):
            self.assertTrue((slice_dir / f"developer-result-repair-{round_number}.json").exists())
            self.assertTrue((slice_dir / f"pane-capture-repair-{round_number}.txt").exists())
            self.assertTrue((slice_dir / f"git-status-repair-{round_number}.txt").exists())
        self.assertTrue((slice_dir / "repair-prompt-repair-1.md").exists())
        self.assertFalse((slice_dir / "developer-result-repair-3.json").exists())
        events = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual([event["mode"] for event in repair_events], ["in-session", "fresh-session"])

    def test_repair_delivery_message_is_single_line_pointer(self):
        # send_literal types keystrokes into a live TUI, where a newline can
        # submit a partial message: the in-session delivery must stay one line
        # and point at the full rendered prompt on disk.
        plan_slice = pm.parse_plan(self.plan)[0]
        prompt_path = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001" / "repair-prompt-repair-1.md"
        message = pm_runner._repair_delivery_message(plan_slice, prompt_path)
        self.assertNotIn("\n", message)
        self.assertIn("NOT accepted", message)
        self.assertIn("Slice 1", message)
        self.assertIn(str(prompt_path), message)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_default_budget_exhausts_across_alternating_signatures(self):
        # Alternating signatures never trip the same-signature circuit
        # breaker, so the default budget (3) is the bound that ends the run.
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "alternating_failures.py"
        write_alternating_failure_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(self._run_next_args(harness, timeout_seconds=30)), 2)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("repair budget exhausted", state["stop_reason"])
        self.assertEqual(state["slices"][0]["repair"]["round"], 3)
        self.assertEqual(state["slices"][0]["repair"]["session_generation"], 1)
        events = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual([event["mode"] for event in repair_events], ["in-session"] * 3)
        self.assertEqual(
            [event["signature"] for event in repair_events],
            ["validation", "review", "validation"],
        )

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_stops_with_evidence_when_repair_delivery_hits_hard_prompt(self):
        self.prepare_committed_repo()
        # Name and launch this executable as `codex` so PM waits for the fake
        # Codex ready banner before prompt injection. The test is about a hard
        # prompt appearing at repair delivery, not custom-command startup.
        harness = Path(self.tmp.name) / "codex"
        write_hard_prompt_at_repair_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_args = self._run_next_args(harness)
        run_args.harness_command = shlex.quote(str(harness))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(run_args), 2)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        slice_dir = run_dir / "slices" / "slice-001"
        self.assertEqual(state["status"], "needs-human", state.get("stop_reason"))
        self.assertIn("repair prompt could not be delivered", state["stop_reason"])
        self.assertIn("hard prompt", state["stop_reason"])
        self.assertTrue((slice_dir / "pane-capture-repair-refused-1.txt").exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_repair_budget_exhaustion_blocks(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "in_session_repair.py"
        write_in_session_repair_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        state = json.loads(run_json.read_text(encoding="utf-8"))
        self.assertEqual(state["policy"]["max_repair_attempts"], 3)
        state["policy"]["max_repair_attempts"] = 0
        run_json.write_text(json.dumps(state), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(self._run_next_args(harness)), 2)
        state = json.loads(run_json.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("repair budget exhausted", state["stop_reason"])
        slice_dir = (self.repo / ".ai-pm" / "current").resolve() / "slices" / "slice-001"
        self.assertFalse((slice_dir / "repair-prompt-repair-1.md").exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_integrity_gate_stops_immediately_without_repair(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "wrong_slice_id.py"
        write_wrong_slice_id_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(self._run_next_args(harness)), 2)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        slice_dir = run_dir / "slices" / "slice-001"
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("slice_id does not match", state["stop_reason"])
        self.assertEqual(state["slices"][0]["repair"], pm_state.default_repair_state())
        self.assertFalse((slice_dir / "repair-prompt-repair-1.md").exists())
        self.assertFalse((slice_dir / "developer-result-repair-1.json").exists())
        self.assertFalse((slice_dir / "activity-attempt-2.jsonl").exists())

    def test_run_remaining_stops_on_approval_needed_second_slice(self):
        write_plan(self.plan)
        text = self.plan.read_text(encoding="utf-8").replace(
            "Approval needed before implementation: no.\n\n### Validation Plan\n- Commands to run:\n  - git diff --check\n\n### Rollback Path\n- Revert CHANGELOG.md.",
            "Approval needed before implementation: yes.\n\n### Validation Plan\n- Commands to run:\n  - git diff --check\n\n### Rollback Path\n- Revert CHANGELOG.md.",
        )
        self.plan.write_text(text, encoding="utf-8")
        state = self.init_run()
        state["slices"].append(self.terminal_slice_entry(state))
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        run_json.write_text(json.dumps(state), encoding="utf-8")
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            scope="remaining",
            dry_run=False,
            timeout_seconds=1,
            poll_seconds=0.1,
            harness_command=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_remaining(run_args), 2)
        stopped = json.loads(run_json.read_text(encoding="utf-8"))
        self.assertEqual(stopped["status"], "needs-human")
        self.assertIn("approval", stopped["stop_reason"])

    def test_stop_records_cancelled_state(self):
        self.init_run()
        args = argparse.Namespace(repo=str(self.repo), run="current", reason="test stop", harness_command=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.stop(args), 0)
        state = json.loads(((self.repo / ".ai-pm" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["stop_reason"], "test stop")

    def test_run_remaining_verifies_plan_before_completion_check(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-pm" / "current").resolve() / "run.json"
        state["slices"].append(self.terminal_slice_entry(state))
        run_json.write_text(json.dumps(state), encoding="utf-8")
        self.plan.write_text(self.plan.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            scope="remaining",
            dry_run=False,
            timeout_seconds=1,
            poll_seconds=0.1,
            harness_command=None,
        )
        with self.assertRaisesRegex(pm.PmError, "plan file changed"):
            pm.run_remaining(args)

    def test_reconcile_verifies_plan_before_gate_recheck(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["slices"].append(
            self.terminal_slice_entry(
                state,
                status="fail",
                artifact_dir=str(artifact.relative_to(self.repo.resolve())),
            )
        )
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        self.plan.write_text(self.plan.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), run="current")
        with self.assertRaisesRegex(pm.PmError, "plan file changed"):
            pm.reconcile(args)

    def test_run_next_stops_when_branch_changed(self):
        self.prepare_committed_repo()
        self.init_run()
        git(self.repo, "checkout", "-b", "unexpected-branch")
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=1,
            poll_seconds=0.1,
            harness_command=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.run_next(run_args), 2)
        state = json.loads(((self.repo / ".ai-pm" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("branch changed since init", state["stop_reason"])

    def test_normalize_stop_status_maps_fail_and_unknown(self):
        self.assertEqual(pm.normalize_stop_status("fail"), "failed")
        self.assertEqual(pm.normalize_stop_status("weird"), "blocked")
        self.assertEqual(pm.normalize_stop_status("needs-human"), "needs-human")
        self.assertEqual(pm.normalize_stop_status("blocked"), "blocked")

    def test_reconcile_uses_recorded_before_head(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["slices"].append(
            self.terminal_slice_entry(
                state,
                status="fail",
                artifact_dir=str(artifact.relative_to(self.repo.resolve())),
                before_head="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                commit={"requested": True, "created": True, "hash": "0" * 40},
            )
        )
        state["status"] = "failed"
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        captured = {}

        def fake_gate(repo, run_state, plan_slice, art, before, after, status, reviewer_tools=()):
            captured["before"] = before
            return pm.GateDecision("fail", "still bad", {"changed_files": []}, ())

        args = argparse.Namespace(repo=str(self.repo), run="current")
        with mock.patch.object(pm_commands, "verify_gate", fake_gate):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.reconcile(args), 2)
        self.assertEqual(captured["before"], "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    # --- Review fixes: harness readiness / launch parity -----------------

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_blocks_on_unexpected_gate_exception(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake_harness.py"
        write_fake_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with mock.patch.object(pm_runner, "verify_gate", side_effect=ValueError("boom")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.run_next(run_args), 2)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertIn("boom", state["stop_reason"])
        self.assertIsNone(state["current_slice"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_records_cancelled_state_on_keyboard_interrupt(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake_harness.py"
        write_fake_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with mock.patch.object(pm_runner, "verify_gate", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.run_next(run_args), 2)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["stop_reason"], "interrupted by user")
        self.assertIsNone(state["current_slice"])

    # --- Approval-gated slices (approve / --assume-complete) --------------

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for orphan detection test")
    def test_status_warns_when_active_session_is_gone(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state["status"] = "running"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "Toy",
            "artifact_dir": f".ai-pm/runs/{state['run_id']}/slices/slice-001",
            "tmux_session": "pm_no_such_session_xyz",
            "attempt": 1,
            "started_at": pm.utc_now(),
            "before_head": git(self.repo, "rev-parse", "HEAD"),
            "pause": None,
            "reviewer_tools": [],
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
        }
        (run_dir / "run.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(pm.status(argparse.Namespace(repo=str(self.repo), run="current")), 0)
        output = buffer.getvalue()
        self.assertIn("WARNING", output)
        self.assertIn("pm_no_such_session_xyz", output)

    # --- Cross-skill dependency contract ---------------------------------


if __name__ == "__main__":
    unittest.main()

"""Deterministic gate verification and worker-evidence tests."""

from mc_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the mc module


class GateVerificationTests(McTestCase):
    def test_gate_blocks_unauthorized_changed_file(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "UNAUTHORIZED.md").write_text("bad\n", encoding="utf-8")
        git(self.repo, "add", "UNAUTHORIZED.md")
        git(self.repo, "commit", "-m", "Bad change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["UNAUTHORIZED.md"], commit_hash=after)
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "unauthorized-files")
        self.assertIn("unauthorized changed files", decision.reason)

    def test_gate_blocks_missing_validation(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], validation_result=None, commit_hash=after)
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "validation")
        self.assertIn("validation evidence is missing", decision.reason)

    def test_gate_blocks_pass_with_risks_drift(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], drift="PASS WITH RISKS", commit_hash=after)
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "drift")
        self.assertIn("drift audit verdict is not PASS", decision.reason)

    def test_gate_blocks_failed_review(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], review="FAIL", commit_hash=after)
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "review")
        self.assertIn("code review verdict is not PASS", decision.reason)

    def test_gate_fails_closed_on_malformed_audit_objects(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result_data(
            artifact,
            {
                "schema_version": 1,
                "slice_id": "Slice 1",
                "status": "pass",
                "summary": "",
                "changed_files": ["README.md"],
                "validation": [{"command": "test", "result": "pass", "notes": ""}],
                "drift_audit": None,
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": True, "created": True, "hash": after},
                "next_action": "",
                "blockers": [],
            },
        )
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "drift")
        self.assertIn("drift audit verdict is not PASS", decision.reason)

    def test_gate_accepts_repo_relative_artifact_paths(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        drift_path = artifact.relative_to(self.repo) / "drift-audit.md"
        review_path = artifact.relative_to(self.repo) / "code-review.md"
        self.write_gate_result_data(
            artifact,
            {
                "schema_version": 1,
                "slice_id": "Slice 1",
                "status": "pass",
                "summary": "",
                "changed_files": ["README.md"],
                "validation": [{"command": "test", "result": "pass", "notes": ""}],
                "drift_audit": {"verdict": "PASS", "path": str(drift_path)},
                "code_review": {"verdict": "PASS", "path": str(review_path)},
                "commit": {"requested": True, "created": True, "hash": after},
                "next_action": "",
                "blockers": [],
            },
        )
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "pass")
        self.assertEqual(decision.signature, "")

    def test_gate_reconciles_fabricated_commit_hash_when_local_evidence_is_clear(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        self.assertTrue(after.startswith(git(self.repo, "rev-parse", "--short", "HEAD")))
        fabricated = git(self.repo, "rev-parse", "--short", "HEAD") + "0" * 33
        self.assertNotEqual(fabricated, after)
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=fabricated)
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "pass")
        self.assertIn("corrected reported commit hash", decision.reason)
        result = json.loads((artifact / "orchestrator-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["commit"]["hash"], after)
        self.assertTrue((artifact / "mc-reconciliation.json").exists())

    def test_gate_blocks_commit_hash_reconciliation_when_head_did_not_advance(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=[], commit_hash="0" * 40)
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, before, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "integrity-head")
        self.assertIn("did not advance HEAD", decision.reason)

    def test_gate_blocks_reset_to_unrelated_head_even_with_truthful_hash(self):
        # Codex #4: an orchestrator that resets to a commit not descended from
        # the slice start and *truthfully* reports that HEAD must fail the
        # integrity gate, not pass because reported_hash == after_head skipped
        # the reconciliation branch where the descendant check used to live.
        self.prepare_committed_repo()
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Slice start")
        before = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "reset", "--hard", base)
        (self.repo / "README.md").write_text("unrelated line of history\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Unrelated history")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "integrity-head")
        self.assertIn("not descended from the slice starting commit", decision.reason)

    def test_gate_classifies_changed_files_mismatch_as_repairable(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md", "CHANGELOG.md"], commit_hash=after)
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "changed-files-mismatch")
        self.assertIn("does not match git evidence", decision.reason)

    def test_gate_classifies_commit_missing_as_repairable(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=None)
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "commit-missing")
        self.assertIn("required commit was not created", decision.reason)

    def test_gate_classifies_dirty_worktree_as_repairable(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        # Unstaged modify: the first status line is " M README.md" with a
        # leading space that is part of the positional status code. This is
        # the exact shape a stdout-stripping status read used to mangle into
        # "EADME.md" (misclassified as an unauthorized file).
        (self.repo / "README.md").write_text("uncommitted follow-up\n", encoding="utf-8")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "dirty-worktree")
        self.assertIn("worktree is dirty", decision.reason)

    def test_gate_classifies_malformed_result_as_repairable(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "orchestrator-result.json").write_text("{not json", encoding="utf-8")
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, before, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("invalid orchestrator result", decision.reason)

    def test_gate_classifies_schema_and_status_errors_as_result_malformed(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        state = self.init_run()
        plan_slice = mc.parse_plan(self.plan)[0]

        self.write_gate_result_data(artifact, {"schema_version": 99, "slice_id": "Slice 1", "status": "pass"})
        decision = mc.verify_gate(self.repo, state, plan_slice, artifact, before, before, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("schema_version", decision.reason)

        self.write_gate_result_data(artifact, {"schema_version": 1, "slice_id": "Slice 1", "status": "victory"})
        decision = mc.verify_gate(self.repo, state, plan_slice, artifact, before, before, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("status is invalid", decision.reason)

    def test_gate_blocks_missing_result_without_repair_signature(self):
        # Absence of orchestrator-result.json is a runner condition (dead or
        # unresponsive session), not a steerable content defect: it stays
        # terminal `blocked` with no repair signature.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, before, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.signature, "")
        self.assertIn("orchestrator result missing", decision.reason)

    def test_gate_classifies_slice_id_mismatch_as_terminal(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result_data(artifact, {"schema_version": 1, "slice_id": "Slice 2", "status": "pass"})
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, before, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "slice-id-mismatch")
        self.assertIn("slice_id does not match", decision.reason)

    def test_gate_passes_through_orchestrator_self_report_signatures(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        state = self.init_run()
        plan_slice = mc.parse_plan(self.plan)[0]

        self.write_gate_result_data(artifact, {"schema_version": 1, "slice_id": "Slice 1", "status": "repairable"})
        decision = mc.verify_gate(self.repo, state, plan_slice, artifact, before, before, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "orchestrator-repairable")

        self.write_gate_result_data(artifact, {"schema_version": 1, "slice_id": "Slice 1", "status": "needs-human"})
        decision = mc.verify_gate(self.repo, state, plan_slice, artifact, before, before, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "")

    def _opt_in_slice(self):
        # Return Slice 1 with the opt-in "Independent audit required: yes" flag
        # set, so MC's worker-launch verification is armed as a blocking gate.
        # By default (without this flag) worker delegation is reporting-only and
        # never blocks acceptance.
        base = mc.parse_plan(self.plan)[0]
        sections = dict(base.sections)
        sections["Risk Flags"] = sections.get("Risk Flags", "") + "\n- Independent audit required: yes"
        return mc.PlanSlice(base.number, base.title, base.body, sections)

    def test_gate_default_slice_accepts_without_worker_evidence(self):
        # Default posture: a slice not marked "Independent audit required: yes"
        # is accepted even when a worker was made available but no genuine worker
        # evidence exists — a locally self-audited slice is a valid outcome, and
        # worker delegation is reporting-only, not a gate.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        self.write_worker_policy(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)
        default_slice = mc.parse_plan(self.plan)[0]
        self.assertFalse(default_slice.independent_audit_required)

        decision = mc.verify_gate(
            self.repo, state, default_slice, artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "pass")

    def test_gate_opt_in_without_available_worker_stops_terminally(self):
        # An opt-in slice with no worker made available is an operator/plan
        # config mismatch the orchestrator cannot repair, so it fails closed
        # terminally (needs-human) rather than burning the repair budget.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state = self.init_run()

        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ()
        )

        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "worker-unavailable")
        self.assertIn("no worker tool was made available", decision.reason)

    def test_gate_blocks_pass_when_required_worker_never_launched(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        self.write_worker_policy(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)

        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("have no worker-evidence.md", decision.reason)

    def test_gate_blocks_pass_when_worker_evidence_is_narration_without_a_launch(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "worker-evidence.md").write_text(
            "# Worker Evidence\n- Result summary: worker was not launched; orchestrator did the check itself.\n",
            encoding="utf-8",
        )
        self.write_worker_policy(artifact)
        # A worker-runs directory was init'd but no worker was ever started in it,
        # exactly reproducing the observed OpenCode/OpenCode Test 5 failure mode.
        worker_run = artifact / "worker-runs" / "workers-1"
        worker_run.mkdir(parents=True)
        (worker_run / "manifest.json").write_text(json.dumps({"workers": {}}), encoding="utf-8")
        mc.capture_worker_runs_summary(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)

        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("no worker was started in it", decision.reason)

    def test_gate_accepts_pass_when_required_worker_has_real_run_evidence(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "worker-evidence.md").write_text(
            "# Worker Evidence\n- Label: 01-opencode-readonly-check\n- Result summary: confirmed unchanged.\n",
            encoding="utf-8",
        )
        self.write_validated_worker_run(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)

        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "pass")

    def test_gate_rejects_hand_authored_manifest_with_no_real_launch_footprint(self):
        # An orchestrator has full write access to its own worker-runs tree
        # and can read worker-policy.json (including its own sha256) to
        # compute a matching policy_sha256. It could therefore hand-author a
        # manifest.json + <label>-status.json pair that satisfies every
        # digest/identity/model/effort/access/role/repo check without ever
        # invoking worker_jobs.py launch or a real harness process. The gate
        # must still reject that: a genuine start_tracked_worker launch always
        # records a positive pid and always creates real outfile/errfile
        # inside worker_artifact_root before the child process starts.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "worker-evidence.md").write_text(
            "# Worker Evidence\n- Label: 01-opencode-forged\n- Result summary: opencode worker ran successfully.\n",
            encoding="utf-8",
        )
        policy_path, policy = self.write_worker_policy(artifact)
        policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        label = "01-opencode-forged"
        worker_run = artifact / "worker-runs" / "workers-1"
        worker_run.mkdir(parents=True)
        contract = {
            "status": "pass",
            "policy_sha256": policy_sha,
            "slice_id": "Slice 1",
            "plan_sha256": policy["plan_sha256"],
            "tool": "opencode",
            "model": "default",
            "effort": "default",
            "role": "junior-worker",
            "access": "read-only",
            "repo_path": str(self.repo.resolve()),
            "cwd": str(self.repo.resolve()),
        }
        # Deliberately no pid, no outfile, no errfile, and no worker_jobs.py
        # invocation anywhere in this test — only three hand-written JSON
        # files, exactly mirroring the zero-effort forgery this closes.
        (worker_run / "manifest.json").write_text(
            json.dumps({"workers": {label: {"tool": "opencode", "launch_contract": contract}}}),
            encoding="utf-8",
        )
        (worker_run / f"{label}-status.json").write_text(
            json.dumps({"label": label, "state": "completed", "returncode": 0}), encoding="utf-8"
        )
        mc.capture_worker_runs_summary(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)

        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("without matching validated launch contracts", decision.reason)
        self.assertIn("real subprocess pid", decision.reason)

    def test_gate_rejects_worker_policy_changed_after_mc_snapshot(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "worker-evidence.md").write_text("# Worker Evidence\n- Label: 01-opencode-readonly-check\n", encoding="utf-8")
        self.write_validated_worker_run(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)
        policy_path = artifact / "worker-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["allowed_access"] = ["read-only", "workspace-write", "unrestricted"]
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("changed after MC created", decision.reason)

    def test_gate_requires_successful_evidence_for_every_configured_worker_tool(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "worker-evidence.md").write_text("# Worker Evidence\n- Label: 01-opencode-readonly-check\n", encoding="utf-8")
        policy_path, policy = self.write_worker_policy(artifact)
        policy["required_tools"] = ["opencode", "codex"]
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        label = "01-opencode-readonly-check"
        worker_run = artifact / "worker-runs" / "workers-1"
        worker_run.mkdir(parents=True)
        contract = {
            "status": "pass",
            "policy_sha256": policy_sha,
            "slice_id": "Slice 1",
            "plan_sha256": policy["plan_sha256"],
            "tool": "opencode",
            "model": "default",
            "effort": "default",
            "role": "junior-worker",
            "access": "read-only",
            "repo_path": str(self.repo.resolve()),
            "cwd": str(self.repo.resolve()),
        }
        (worker_run / "manifest.json").write_text(
            json.dumps({"workers": {label: {"tool": "opencode", "command": ["opencode", "run"], "launch_contract": contract}}}),
            encoding="utf-8",
        )
        (worker_run / f"{label}-status.json").write_text(
            json.dumps({"label": label, "state": "completed", "returncode": 0}), encoding="utf-8"
        )
        mc.capture_worker_runs_summary(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)

        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode", "codex")
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("codex", decision.reason)

    def test_gate_rejects_matching_executable_without_validated_launch_contract(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "worker-evidence.md").write_text("# Worker Evidence\n- Label: 01-opencode-raw\n", encoding="utf-8")
        self.write_worker_policy(artifact)
        worker_run = artifact / "worker-runs" / "workers-1"
        worker_run.mkdir(parents=True)
        (worker_run / "manifest.json").write_text(
            json.dumps({"workers": {"01-opencode-raw": {"tool": "opencode", "command": ["opencode", "run"]}}}),
            encoding="utf-8",
        )
        (worker_run / "01-opencode-raw-status.json").write_text(
            json.dumps({"label": "01-opencode-raw", "state": "completed", "returncode": 0}), encoding="utf-8"
        )
        mc.capture_worker_runs_summary(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)
        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("without matching validated launch contracts", decision.reason)
        self.assertIn("correct the semantic worker request", decision.reason)

    def test_gate_blocks_pass_when_worker_is_mislabeled_but_actually_a_different_executable(self):
        # Reproduces a live OpenCode/OpenCode test run: a worker labeled
        # "01-opencode-drift-check" whose manifest recorded "tool": "bash"
        # because the orchestrator ran a shell one-liner through worker_jobs.py
        # instead of actually invoking `opencode`.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "worker-evidence.md").write_text(
            "# Worker Evidence\n- Label: 01-opencode-drift-check\n- Result summary: drift check passed.\n",
            encoding="utf-8",
        )
        self.write_worker_policy(artifact)
        worker_run = artifact / "worker-runs" / "workers-1"
        worker_run.mkdir(parents=True)
        (worker_run / "manifest.json").write_text(
            json.dumps({"workers": {"01-opencode-drift-check": {"tool": "bash", "command": ["bash", "-c", "grep foo"]}}}),
            encoding="utf-8",
        )
        (worker_run / "01-opencode-drift-check-status.json").write_text(
            json.dumps({"label": "01-opencode-drift-check", "state": "completed", "returncode": 0}),
            encoding="utf-8",
        )
        mc.capture_worker_runs_summary(artifact)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)

        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("were never actually invoked", decision.reason)

    def test_gate_ignores_worker_evidence_when_no_worker_tool_is_required(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state = self.init_run()

        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))

        self.assertEqual(decision.status, "pass")

    def test_capture_worker_runs_summary_records_status_files(self):
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        worker_run = artifact / "worker-runs" / "workers-1"
        worker_run.mkdir(parents=True)
        (worker_run / "01-codex-check-status.json").write_text(
            json.dumps({"label": "01-codex-check", "state": "completed", "returncode": 0}),
            encoding="utf-8",
        )

        mc.capture_worker_runs_summary(artifact)

        summary = json.loads((artifact / "worker-runs-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["runs"][0]["workers"][0]["label"], "01-codex-check")

    def test_capture_worker_runs_summary_skips_current_symlink(self):
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        worker_root = artifact / "worker-runs"
        worker_run = worker_root / "workers-1"
        worker_run.mkdir(parents=True)
        (worker_run / "manifest.json").write_text(json.dumps({"workers": {}}), encoding="utf-8")
        (worker_run / "01-codex-check-status.json").write_text(
            json.dumps({"label": "01-codex-check", "state": "completed", "returncode": 0}),
            encoding="utf-8",
        )
        os.symlink(worker_run, worker_root / "current")

        mc.capture_worker_runs_summary(artifact)

        summary = json.loads((artifact / "worker-runs-summary.json").read_text(encoding="utf-8"))
        self.assertEqual([Path(entry["run_dir"]).name for entry in summary["runs"]], ["workers-1"])

    def test_worker_delegation_overview_flags_missing_contracted_marker(self):
        # Regression (MC Test 11): a worker that refused its task but exited
        # cleanly (state completed, returncode 0) passed the process-level
        # worker-evidence gate and was invisible in every summary. The
        # overview must surface, per worker, whether the output contains the
        # marker its own request's expected_output contracted.
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        for name, out_text in (("workers-1", "How would you like to proceed?"), ("workers-2", "RESULT: pass — verified.")):
            worker_run = artifact / "worker-runs" / name
            worker_run.mkdir(parents=True)
            (worker_run / "01-opencode-check-out.txt").write_text(out_text, encoding="utf-8")
            (worker_run / "manifest.json").write_text(
                json.dumps(
                    {
                        "workers": {
                            "01-opencode-check": {
                                "tool": "opencode",
                                "outfile": str(worker_run / "01-opencode-check-out.txt"),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (worker_run / "01-opencode-check-request.json").write_text(
                json.dumps({"expected_output": "Return RESULT: pass or RESULT: blocked."}),
                encoding="utf-8",
            )
            (worker_run / "01-opencode-check-status.json").write_text(
                json.dumps({"label": "01-opencode-check", "state": "completed", "returncode": 0}),
                encoding="utf-8",
            )

        overview = mc.worker_delegation_overview(artifact)

        self.assertEqual(len(overview), 2)
        by_run = {Path(entry["run_dir"]).name: entry for entry in overview}
        self.assertEqual(by_run["workers-1"]["contracted_marker"], "absent")
        self.assertEqual(by_run["workers-1"]["state"], "completed")
        self.assertEqual(by_run["workers-1"]["returncode"], 0)
        self.assertEqual(by_run["workers-1"]["output_tail"], "How would you like to proceed?")
        self.assertEqual(by_run["workers-2"]["contracted_marker"], "present")

    def test_worker_delegation_overview_reports_na_without_contracted_marker(self):
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        worker_run = artifact / "worker-runs" / "workers-1"
        worker_run.mkdir(parents=True)
        (worker_run / "01-opencode-scan-out.txt").write_text("free-form notes", encoding="utf-8")
        (worker_run / "manifest.json").write_text(
            json.dumps(
                {
                    "workers": {
                        "01-opencode-scan": {
                            "tool": "opencode",
                            "outfile": str(worker_run / "01-opencode-scan-out.txt"),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (worker_run / "01-opencode-scan-request.json").write_text(
            json.dumps({"expected_output": "Describe what you found."}),
            encoding="utf-8",
        )

        overview = mc.worker_delegation_overview(artifact)

        self.assertEqual(overview[0]["contracted_marker"], "n/a")
        self.assertEqual(overview[0]["state"], "unknown")

    def test_gate_fails_closed_on_string_validation_entry(self):
        self.prepare_committed_repo()
        before, after = self._commit_readme_change()
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result_data(
            artifact,
            {
                "schema_version": 1,
                "slice_id": "Slice 1",
                "status": "pass",
                "summary": "",
                "changed_files": ["README.md"],
                "validation": ["git diff --check ran fine"],
                "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": True, "created": True, "hash": after},
                "next_action": "",
                "blockers": [],
            },
        )
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "validation")
        self.assertIn("validation entries are malformed", decision.reason)

    def test_gate_fails_closed_on_string_changed_files(self):
        self.prepare_committed_repo()
        before, after = self._commit_readme_change()
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result_data(
            artifact,
            {
                "schema_version": 1,
                "slice_id": "Slice 1",
                "status": "pass",
                "summary": "",
                "changed_files": "README.md",
                "validation": [{"command": "test", "result": "pass", "notes": ""}],
                "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": True, "created": True, "hash": after},
                "next_action": "",
                "blockers": [],
            },
        )
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "changed-files-mismatch")
        self.assertIn("changed_files is malformed", decision.reason)

    def test_artifact_exists_requires_nonempty_in_tree_file(self):
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        (artifact / "drift-audit.md").write_text("", encoding="utf-8")
        self.assertFalse(mc.artifact_exists(self.repo, artifact, {}, "drift_audit", "drift-audit.md"))
        (artifact / "drift-audit.md").write_text("verdict\n", encoding="utf-8")
        self.assertTrue(mc.artifact_exists(self.repo, artifact, {}, "drift_audit", "drift-audit.md"))
        artifact_relative = artifact.relative_to(self.repo) / "drift-audit.md"
        self.assertTrue(
            mc.artifact_exists(self.repo, artifact, {"drift_audit": {"path": str(artifact_relative)}}, "drift_audit", "drift-audit.md")
        )
        (self.repo / "README.md").write_text("not an audit artifact\n", encoding="utf-8")
        self.assertFalse(
            mc.artifact_exists(self.repo, artifact, {"drift_audit": {"path": "README.md"}}, "drift_audit", "drift-audit.md")
        )
        # An existing file outside the run must not satisfy the evidence check.
        self.assertFalse(
            mc.artifact_exists(self.repo, artifact, {"drift_audit": {"path": sys.executable}}, "drift_audit", "drift-audit.md")
        )

    # --- Review fixes: run integrity -------------------------------------

    def test_gate_blocks_empty_validation_summary(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "validation-summary.md").write_text("", encoding="utf-8")
        state = self.init_run()
        decision = mc.verify_gate(self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "validation")
        self.assertIn("missing or empty", decision.reason)

    def test_gate_blocks_worker_that_never_completed_successfully(self):
        # A required worker that was genuinely launched with the right
        # executable but crashed (or is still running) proves nothing was
        # delegated; launch alone must not satisfy the worker-evidence gate.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "worker-evidence.md").write_text(
            "# Worker Evidence\n- Label: 01-opencode-readonly-check\n- Result summary: worker ran.\n",
            encoding="utf-8",
        )
        self.write_validated_worker_run(artifact, state="failed", returncode=1)
        state = self.init_run()
        self.attach_worker_policy_snapshot(state, artifact)
        decision = mc.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("never completed successfully", decision.reason)

    # --- Send guards and event-log behavior --------------------------------


if __name__ == "__main__":
    unittest.main()

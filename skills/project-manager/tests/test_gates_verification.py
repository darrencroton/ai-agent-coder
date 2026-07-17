"""Deterministic gate verification and reviewer-evidence tests."""

from pm_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the pm module


class GateVerificationTests(PmTestCase):
    def test_audit_provenance_records_explicit_developer_self_audit_without_reviewer(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=[])
        result = json.loads((artifact / "developer-result.json").read_text(encoding="utf-8"))

        provenance = pm.reviewer_audit_provenance(
            artifact,
            (),
            None,
            developer_result=result,
            repo=self.repo,
        )

        for audit in ("drift-audit", "code-review"):
            self.assertEqual(provenance[audit]["performed_by"], "developer-self-audit")
            self.assertIsNone(provenance[audit]["reviewer_tool"])
            self.assertIn("no Reviewer was configured", provenance[audit]["fallback_context"])

    def test_audit_provenance_is_not_observed_without_execution_evidence(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"

        provenance = pm.reviewer_audit_provenance(artifact, (), None)

        for audit in ("drift-audit", "code-review"):
            self.assertEqual(provenance[audit]["performed_by"], "not-observed")
            self.assertIn("no validated Reviewer evidence", provenance[audit]["fallback_context"])

    def test_audit_provenance_records_validated_reviewer_tool_and_label(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_validated_reviewer_run(artifact)
        snapshot = pm.reviewer_policy_snapshot(artifact / "reviewer-policy.json")

        provenance = pm.reviewer_audit_provenance(artifact, ("opencode",), snapshot)

        for audit in ("drift-audit", "code-review"):
            self.assertEqual(provenance[audit]["performed_by"], "reviewer")
            self.assertEqual(provenance[audit]["reviewer_tool"], "opencode")
            self.assertIn(audit, provenance[audit]["reviewer_label"])
            self.assertIsNone(provenance[audit]["fallback_context"])

    def test_audit_provenance_supports_mixed_reviewer_and_developer_self_audit(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=[])
        result = json.loads((artifact / "developer-result.json").read_text(encoding="utf-8"))
        self.write_validated_reviewer_run(artifact)
        reviewer_run = next((artifact / "reviewer-runs").iterdir())
        code_status = next(reviewer_run.glob("*code-review-status.json"))
        status = json.loads(code_status.read_text(encoding="utf-8"))
        status["skill_verdicts"]["code-review"] = "FAIL"
        code_status.write_text(json.dumps(status), encoding="utf-8")
        pm.capture_reviewer_runs_summary(artifact)
        snapshot = pm.reviewer_policy_snapshot(artifact / "reviewer-policy.json")

        provenance = pm.reviewer_audit_provenance(
            artifact,
            ("opencode",),
            snapshot,
            developer_result=result,
            repo=self.repo,
        )

        self.assertEqual(provenance["drift-audit"]["performed_by"], "reviewer")
        self.assertEqual(provenance["code-review"]["performed_by"], "developer-self-audit")
        self.assertIn("no successful validated Reviewer PASS evidence", provenance["code-review"]["fallback_context"])

    def test_audit_provenance_rejects_non_intrinsic_role_or_access(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=[])
        result = json.loads((artifact / "developer-result.json").read_text(encoding="utf-8"))
        self.write_validated_reviewer_run(artifact)
        reviewer_run = next((artifact / "reviewer-runs").iterdir())
        manifest_path = reviewer_run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest["reviewers"].values():
            entry["launch_contract"]["access"] = "workspace-write"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        pm.capture_reviewer_runs_summary(artifact)
        snapshot = pm.reviewer_policy_snapshot(artifact / "reviewer-policy.json")

        provenance = pm.reviewer_audit_provenance(
            artifact,
            ("opencode",),
            snapshot,
            developer_result=result,
            repo=self.repo,
        )

        self.assertTrue(all(record["performed_by"] == "developer-self-audit" for record in provenance.values()))

    def test_audit_provenance_rejects_developer_artifact_outside_slice(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        outside = self.repo / "outside-audit.md"
        outside.write_text("PASS\n", encoding="utf-8")
        result = {
            "drift_audit": {"verdict": "PASS", "path": str(outside)},
            "code_review": {"verdict": "", "path": ""},
        }

        provenance = pm.reviewer_audit_provenance(
            artifact,
            (),
            None,
            developer_result=result,
            repo=self.repo,
        )

        self.assertEqual(provenance["drift-audit"]["performed_by"], "not-observed")

    def test_timeout_without_developer_result_records_audits_not_observed(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        gate = pm.GateDecision("blocked", "Developer session timed out")

        entry = pm.slice_entry_from_gate(
            self.repo,
            pm.parse_plan(self.plan)[0],
            artifact,
            pm.utc_now(),
            gate,
        )

        self.assertTrue(all(record["performed_by"] == "not-observed" for record in entry["audit_provenance"].values()))

    def test_missing_result_terminal_entry_records_audits_not_observed(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        gate = pm.GateDecision("blocked", f"developer result missing: {artifact / 'developer-result.json'}")

        entry = pm.slice_entry_from_gate(
            self.repo,
            pm.parse_plan(self.plan)[0],
            artifact,
            pm.utc_now(),
            gate,
        )

        self.assertTrue(all(record["performed_by"] == "not-observed" for record in entry["audit_provenance"].values()))

    def test_pre_audit_stop_records_audits_not_observed(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        gate = pm.GateDecision(
            "blocked",
            "Developer stopped before audit stage",
            {
                "schema_version": pm.SCHEMA_VERSION,
                "slice_id": "Slice 1",
                "status": "blocked",
                "summary": "stopped before audit",
                "changed_files": [],
                "validation": [],
                "commit": {"requested": False, "created": False, "hash": None},
                "next_action": "",
                "blockers": ["pre-audit stop"],
                "residual_findings": [],
                "continuation_notes": [],
            },
        )

        entry = pm.slice_entry_from_gate(
            self.repo,
            pm.parse_plan(self.plan)[0],
            artifact,
            pm.utc_now(),
            gate,
        )

        self.assertTrue(all(record["performed_by"] == "not-observed" for record in entry["audit_provenance"].values()))

    def test_gate_blocks_unauthorized_changed_file(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "UNAUTHORIZED.md").write_text("bad\n", encoding="utf-8")
        git(self.repo, "add", "UNAUTHORIZED.md")
        git(self.repo, "commit", "-m", "Bad change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["UNAUTHORIZED.md"], commit_hash=after)
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], validation_result=None, commit_hash=after)
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], drift="PASS WITH RISKS", commit_hash=after)
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], review="FAIL", commit_hash=after)
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result_data(
            artifact,
            {
                "schema_version": pm.SCHEMA_VERSION,
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
                "residual_findings": [],
                "continuation_notes": [],
            },
        )
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        drift_path = artifact.relative_to(self.repo) / "drift-audit.md"
        review_path = artifact.relative_to(self.repo) / "code-review.md"
        self.write_gate_result_data(
            artifact,
            {
                "schema_version": pm.SCHEMA_VERSION,
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
                "residual_findings": [],
                "continuation_notes": [],
            },
        )
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=fabricated)
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))

        self.assertEqual(decision.status, "pass")
        self.assertIn("corrected reported commit hash", decision.reason)
        result = json.loads((artifact / "developer-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["commit"]["hash"], after)
        self.assertTrue((artifact / "pm-reconciliation.json").exists())

    def test_gate_blocks_commit_hash_reconciliation_when_head_did_not_advance(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=[], commit_hash="0" * 40)
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, before, pm.git_status_text(self.repo))

        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "integrity-head")
        self.assertIn("did not advance HEAD", decision.reason)

    def test_gate_blocks_reset_to_unrelated_head_even_with_truthful_hash(self):
        # Codex #4: a Developer that resets to a commit not descended from
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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))

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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md", "CHANGELOG.md"], commit_hash=after)
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))

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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=None)
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))

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
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "dirty-worktree")
        self.assertIn("worktree is dirty", decision.reason)

    def test_gate_classifies_malformed_result_as_repairable(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "developer-result.json").write_text("{not json", encoding="utf-8")
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, before, pm.git_status_text(self.repo))

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("invalid developer result", decision.reason)

    def test_gate_classifies_schema_and_status_errors_as_result_malformed(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        state = self.init_run()
        plan_slice = pm.parse_plan(self.plan)[0]

        self.write_gate_result_data(artifact, {"schema_version": 2, "slice_id": "Slice 1", "status": "pass"})
        decision = pm.verify_gate(self.repo, state, plan_slice, artifact, before, before, pm.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("schema_version", decision.reason)

        self.write_gate_result_data(artifact, {"schema_version": pm.SCHEMA_VERSION, "slice_id": "Slice 1", "status": "victory"})
        decision = pm.verify_gate(self.repo, state, plan_slice, artifact, before, before, pm.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("status is invalid", decision.reason)

    def test_gate_blocks_missing_result_without_repair_signature(self):
        # Absence of developer-result.json is a runner condition (dead or
        # unresponsive session), not a steerable content defect: it stays
        # terminal `blocked` with no repair signature.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, before, pm.git_status_text(self.repo))

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.signature, "")
        self.assertIn("developer result missing", decision.reason)

    def test_gate_classifies_slice_id_mismatch_as_terminal(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result_data(artifact, {"schema_version": pm.SCHEMA_VERSION, "slice_id": "Slice 2", "status": "pass"})
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, before, pm.git_status_text(self.repo))

        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "slice-id-mismatch")
        self.assertIn("slice_id does not match", decision.reason)

    def test_gate_passes_through_developer_self_report_signatures(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        state = self.init_run()
        plan_slice = pm.parse_plan(self.plan)[0]

        self.write_gate_result_data(artifact, {
            "schema_version": pm.SCHEMA_VERSION, "slice_id": "Slice 1", "status": "repairable",
            "residual_findings": [], "continuation_notes": [],
        })
        decision = pm.verify_gate(self.repo, state, plan_slice, artifact, before, before, pm.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "developer-repairable")

        self.write_gate_result_data(artifact, {
            "schema_version": pm.SCHEMA_VERSION, "slice_id": "Slice 1", "status": "needs-human",
            "residual_findings": [], "continuation_notes": [],
        })
        decision = pm.verify_gate(self.repo, state, plan_slice, artifact, before, before, pm.git_status_text(self.repo))
        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "")

        self.write_gate_result_data(artifact, {
            "schema_version": pm.SCHEMA_VERSION,
            "slice_id": "Slice 1",
            "status": "blocked",
            "residual_findings": [],
            "continuation_notes": [{"category": "broken"}],
        })
        decision = pm.verify_gate(self.repo, state, plan_slice, artifact, before, before, pm.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "result-malformed")
        entry = pm.slice_entry_from_gate(
            self.repo,
            plan_slice,
            artifact,
            pm.utc_now(),
            pm.GateDecision("needs-human", decision.reason, decision.result),
            before,
            reviewer_policy={"sha256": "a" * 64, "policy": {}},
        )
        self.assertEqual(entry["continuation_notes"], [])

    def _opt_in_slice(self):
        # Return Slice 1 with the opt-in "Independent audit required: yes" flag
        # set, so PM's reviewer-launch verification is armed as a blocking gate.
        # By default (without this flag) reviewer delegation is reporting-only and
        # never blocks acceptance.
        base = pm.parse_plan(self.plan)[0]
        sections = dict(base.sections)
        sections["Risk Flags"] = sections.get("Risk Flags", "") + "\n- Independent audit required: yes"
        return pm.PlanSlice(base.number, base.title, base.body, sections)

    def test_gate_default_slice_accepts_without_reviewer_evidence(self):
        # Default posture: a slice not marked "Independent audit required: yes"
        # is accepted even when a reviewer was made available but no genuine reviewer
        # evidence exists — a locally self-audited slice is a valid outcome, and
        # reviewer delegation is reporting-only, not a gate.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        self.write_reviewer_policy(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)
        default_slice = pm.parse_plan(self.plan)[0]
        self.assertFalse(default_slice.independent_audit_required)

        decision = pm.verify_gate(
            self.repo, state, default_slice, artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "pass")
        entry = pm.slice_entry_from_gate(
            self.repo,
            default_slice,
            artifact,
            pm.utc_now(),
            decision,
            before,
            ("opencode",),
            reviewer_policy=state["current_slice"]["reviewer_policy"],
        )
        for audit in ("drift-audit", "code-review"):
            self.assertEqual(entry["audit_provenance"][audit]["performed_by"], "developer-self-audit")

    def test_gate_requires_explicit_residual_findings_ledger(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        result_path = artifact / "developer-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        del result["residual_findings"]
        result_path.write_text(json.dumps(result), encoding="utf-8")
        state = self.init_run()

        decision = pm.verify_gate(
            self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("residual_findings is missing", decision.reason)

    def test_gate_requires_and_preserves_structured_continuation_notes(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        note = {
            "category": "implementation-lesson",
            "summary": "The parser requires normalized paths.",
            "rationale": "Later slices share the same parser.",
            "applies_to": "Slices 2-3",
            "location": "README.md",
        }
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after, continuation_notes=[note])
        state = self.init_run()
        plan_slice = pm.parse_plan(self.plan)[0]
        decision = pm.verify_gate(
            self.repo, state, plan_slice, artifact, before, after, pm.git_status_text(self.repo)
        )
        self.assertEqual(decision.status, "pass")
        entry = pm.slice_entry_from_gate(
            self.repo, plan_slice, artifact, pm.utc_now(), decision, before,
            reviewer_policy={"sha256": "a" * 64, "policy": {}},
        )
        self.assertEqual(entry["continuation_notes"], [note])
        self.assertIn("parser requires normalized paths", (artifact / "slice-summary.md").read_text(encoding="utf-8"))

        malformed = json.loads((artifact / "developer-result.json").read_text(encoding="utf-8"))
        malformed["continuation_notes"] = [{"category": "mystery", "summary": "x", "rationale": "y", "applies_to": "z"}]
        (artifact / "developer-result.json").write_text(json.dumps(malformed), encoding="utf-8")
        decision = pm.verify_gate(
            self.repo, state, plan_slice, artifact, before, after, pm.git_status_text(self.repo)
        )
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("continuation_notes[0].category is invalid", decision.reason)

        too_many = [note] * (pm.MAX_CONTINUATION_NOTES + 1)
        malformed["continuation_notes"] = too_many
        (artifact / "developer-result.json").write_text(json.dumps(malformed), encoding="utf-8")
        decision = pm.verify_gate(
            self.repo, state, plan_slice, artifact, before, after, pm.git_status_text(self.repo)
        )
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("exceeds the maximum", decision.reason)

        malformed["continuation_notes"] = [dict(note, summary="x" * (pm.MAX_CONTINUATION_FIELD_CHARS + 1))]
        (artifact / "developer-result.json").write_text(json.dumps(malformed), encoding="utf-8")
        decision = pm.verify_gate(
            self.repo, state, plan_slice, artifact, before, after, pm.git_status_text(self.repo)
        )
        self.assertEqual(decision.signature, "result-malformed")
        self.assertIn("exceeds the maximum", decision.reason)

    def test_gate_failure_routes_transient_idle_and_prior_context_signatures(self):
        for signature, expected_status in (
            ("transient-service-unavailable", "repairable"),
            ("idle-no-progress", "repairable"),
            ("prior-context-integrity", "needs-human"),
        ):
            with self.subTest(signature=signature):
                decision = pm.gate_failure(signature, "reason text")
                self.assertEqual(decision.status, expected_status)
                self.assertEqual(decision.signature, signature)
        with self.assertRaises(pm.PmError):
            pm.gate_failure("not-a-real-signature", "reason text")

    def _archived_residual_finding(self):
        return {
            "source": "validation",
            "severity": "low",
            "summary": "Flaky retry needed for the slow CI shard.",
            "disposition": "deferred-inconsequential",
            "rationale": "Out of scope for this slice.",
            "suggested_follow_up": "Track in a later slice.",
        }

    def _archived_continuation_note(self):
        return {
            "category": "risk-warning",
            "summary": "The shared cache is not thread-safe under this slice's changes.",
            "rationale": "A later slice adding concurrency must account for this.",
            "applies_to": "Slices 2-3",
        }

    def _write_archived_repair_result(self, artifact, round_number, *, residual_findings=(), continuation_notes=()):
        (artifact / f"developer-result-repair-{round_number}.json").write_text(
            json.dumps(
                {
                    "schema_version": pm.SCHEMA_VERSION,
                    "slice_id": "Slice 1",
                    "status": "repairable",
                    "residual_findings": list(residual_findings),
                    "continuation_notes": list(continuation_notes),
                }
            ),
            encoding="utf-8",
        )

    def test_gate_rejects_pass_result_dropping_archived_residual_finding(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        archived_finding = self._archived_residual_finding()
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        self._write_archived_repair_result(artifact, 1, residual_findings=[archived_finding])
        state = self.init_run()

        decision = pm.verify_gate(
            self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "ledger-retention")
        self.assertIn("developer-result-repair-1.json", decision.reason)
        self.assertIn("Flaky retry needed", decision.reason)
        self.assertIn("residual_findings", decision.reason)

    def test_gate_rejects_pass_result_dropping_archived_continuation_note(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        archived_note = self._archived_continuation_note()
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        self._write_archived_repair_result(artifact, 1, continuation_notes=[archived_note])
        state = self.init_run()

        decision = pm.verify_gate(
            self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "ledger-retention")
        self.assertIn("developer-result-repair-1.json", decision.reason)
        self.assertIn("thread-safe", decision.reason)
        self.assertIn("continuation_notes", decision.reason)

    def test_gate_accepts_pass_result_retaining_every_archived_ledger_item(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        archived_finding = self._archived_residual_finding()
        archived_note = self._archived_continuation_note()
        self.write_gate_result(
            artifact,
            changed_files=["README.md"],
            commit_hash=after,
            residual_findings=[archived_finding],
            continuation_notes=[archived_note],
        )
        self._write_archived_repair_result(
            artifact, 1, residual_findings=[archived_finding], continuation_notes=[archived_note]
        )
        state = self.init_run()

        decision = pm.verify_gate(
            self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo)
        )

        self.assertEqual(decision.status, "pass")

    def test_gate_ledger_retention_exempts_context_budget_repair_round(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        archived_finding = self._archived_residual_finding()
        # Fresh result condenses/drops the archived item, as a context-budget
        # repair explicitly instructs — the exemption must still accept.
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        self._write_archived_repair_result(artifact, 1, residual_findings=[archived_finding])
        state = self.init_run()

        decision = pm.verify_gate(
            self.repo,
            state,
            pm.parse_plan(self.plan)[0],
            artifact,
            before,
            after,
            pm.git_status_text(self.repo),
            last_repair_signature="context-budget",
        )

        self.assertEqual(decision.status, "pass")

    def test_gate_repairs_empty_residual_ledger_when_review_lists_observation(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "code-review.md").write_text(
            "## Findings\n\n1. [P3] Non-blocking observation about a pre-existing helper.\n\n## Verdict\n\nPASS\n",
            encoding="utf-8",
        )
        state = self.init_run()
        decision = pm.verify_gate(
            self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo)
        )
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "residual-ledger-mismatch")

    def test_gate_accepts_explicitly_empty_or_resolved_review_findings(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        for body in ("- none", "1. [P2] Fixed in the reviewed commit", "1. Addressed by the reviewed refactor"):
            artifact = self.repo / ".ai-pm" / "runs" / body.replace(" ", "-") / "slices" / "slice-001"
            self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
            (artifact / "code-review.md").write_text(f"## Findings\n\n{body}\n", encoding="utf-8")
            state = self.init_run()
            decision = pm.verify_gate(
                self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo)
            )
            self.assertEqual(decision.status, "pass")

    def test_slice_and_run_reports_propagate_residual_findings(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        finding = {
            "source": "code-review",
            "severity": "info",
            "location": "legacy.py:7",
            "summary": "Legacy helper could be clarified later.",
            "disposition": "pre-existing",
            "rationale": "The slice neither touches nor depends on the helper.",
            "suggested_follow_up": "Consider a separate cleanup plan.",
        }
        self.write_gate_result(
            artifact,
            changed_files=["README.md"],
            commit_hash=after,
            residual_findings=[finding],
        )
        state = self.init_run()
        plan_slice = pm.parse_plan(self.plan)[0]
        decision = pm.verify_gate(
            self.repo, state, plan_slice, artifact, before, after, pm.git_status_text(self.repo)
        )
        self.assertEqual(decision.status, "pass")

        entry = pm.slice_entry_from_gate(
            self.repo,
            plan_slice,
            artifact,
            pm.utc_now(),
            decision,
            before,
            reviewer_policy={"sha256": "a" * 64, "policy": {}},
            prior_slice_context={"path": "prior-slice-context.md", "sha256": "b" * 64},
        )
        self.assertEqual(entry["residual_findings"], [finding])
        self.assertEqual(entry["audit_provenance"]["drift-audit"]["performed_by"], "developer-self-audit")
        self.assertEqual(entry["audit_provenance"]["code-review"]["performed_by"], "developer-self-audit")
        slice_summary = artifact / "slice-summary.md"
        slice_text = slice_summary.read_text(encoding="utf-8")
        self.assertIn("Legacy helper could be clarified later", slice_text)
        self.assertIn("Drift audit performed by: developer-self-audit", slice_text)
        self.assertIn("Code review performed by: developer-self-audit", slice_text)
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        state["slices"] = [entry]
        state["status"] = "complete"
        pm.write_run(run_dir / "run.json", state)
        report = (run_dir / "run-report.md").read_text(encoding="utf-8")
        self.assertIn("Legacy helper could be clarified later", report)
        self.assertIn("Consider a separate cleanup plan", report)
        self.assertIn("Drift audit performed by: developer-self-audit", report)
        self.assertIn("Code review performed by: developer-self-audit", report)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(pm.summarize(argparse.Namespace(repo=str(self.repo), run="current")), 0)
        self.assertIn("drift audit performed by: developer-self-audit", output.getvalue())
        self.assertIn("code review performed by: developer-self-audit", output.getvalue())

    def test_run_report_groups_superseded_and_authoritative_outcomes(self):
        state = {
            "run_id": "test",
            "status": "partial",
            "branch": "main",
            "plan_path": "plan.md",
            "stop_reason": None,
            "plan": {"slice_count": 1},
            "slices": [
                {"slice_id": "Slice 1", "title": "Work", "status": "blocked", "residual_findings": []},
                {
                    "slice_id": "Slice 1",
                    "title": "Work",
                    "status": "pass",
                    "commit": {"hash": "a" * 40},
                    "residual_findings": [],
                    "continuation_notes": [],
                },
            ],
        }
        report = pm_state.render_run_report(state)
        self.assertEqual(report.count("### Slice 1 — Work"), 1)
        self.assertIn("Recorded outcome 1 — superseded", report)
        self.assertIn("Recorded outcome 2 — authoritative", report)
        self.assertIn("Completed slices: 1/1", report)

    def test_gate_opt_in_without_available_reviewer_stops_terminally(self):
        # An opt-in slice with no reviewer made available is an operator/plan
        # config mismatch the developer cannot repair, so it fails closed
        # terminally (needs-human) rather than burning the repair budget.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state = self.init_run()

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ()
        )

        self.assertEqual(decision.status, "needs-human")
        self.assertEqual(decision.signature, "reviewer-unavailable")
        self.assertIn("no reviewer tool was made available", decision.reason)

    def test_gate_blocks_pass_when_required_reviewer_never_launched(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        self.write_reviewer_policy(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("have no reviewer-evidence.md", decision.reason)

    def test_gate_blocks_pass_when_reviewer_evidence_is_narration_without_a_launch(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text(
            "# Reviewer Evidence\n- Result summary: reviewer was not launched; developer did the check itself.\n",
            encoding="utf-8",
        )
        self.write_reviewer_policy(artifact)
        # A reviewer-runs directory was init'd but no reviewer was ever started in it,
        # exactly reproducing the observed OpenCode/OpenCode Test 5 failure mode.
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        (reviewer_run / "manifest.json").write_text(json.dumps({"reviewers": {}}), encoding="utf-8")
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("no reviewer was started in it", decision.reason)

    def test_gate_accepts_pass_when_required_reviewer_has_real_run_evidence(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text(
            "# Reviewer Evidence\n- Label: 01-opencode-readonly-check\n- Result summary: confirmed unchanged.\n",
            encoding="utf-8",
        )
        self.write_validated_reviewer_run(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "pass")
        entry = pm.slice_entry_from_gate(
            self.repo,
            self._opt_in_slice(),
            artifact,
            pm.utc_now(),
            decision,
            before,
            ("opencode",),
            reviewer_policy=state["current_slice"]["reviewer_policy"],
        )
        for audit in ("drift-audit", "code-review"):
            self.assertEqual(entry["audit_provenance"][audit]["performed_by"], "reviewer")
            self.assertEqual(entry["audit_provenance"][audit]["reviewer_tool"], "opencode")
            self.assertIn(audit, entry["audit_provenance"][audit]["reviewer_label"])

    def test_gate_rejects_completed_reviewer_with_adverse_audit_verdict(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text("# Reviewer Evidence\n", encoding="utf-8")
        self.write_validated_reviewer_run(artifact)
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        status_path = next(reviewer_run.glob("*code-review-status.json"))
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["skill_verdicts"]["code-review"] = "FAIL"
        status_path.write_text(json.dumps(status), encoding="utf-8")
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("code-review=FAIL", decision.reason)

    def test_gate_rejects_completed_reviewer_without_helper_recorded_verdict(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text("# Reviewer Evidence\n", encoding="utf-8")
        self.write_validated_reviewer_run(artifact)
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        status_path = next(reviewer_run.glob("*drift-audit-status.json"))
        status = json.loads(status_path.read_text(encoding="utf-8"))
        del status["skill_verdicts"]
        status_path.write_text(json.dumps(status), encoding="utf-8")
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("drift-audit=missing", decision.reason)

    def test_gate_rejects_latest_adverse_audit_verdict_after_earlier_pass(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text("# Reviewer Evidence\n", encoding="utf-8")
        self.write_validated_reviewer_run(artifact)
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        manifest_path = reviewer_run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        original_label = next(label for label in manifest["reviewers"] if "code-review" in label)
        retry_label = f"{original_label}-r1"
        retry_entry = dict(manifest["reviewers"][original_label])
        retry_entry["outfile"] = str(reviewer_run / f"{retry_label}-out.txt")
        retry_entry["errfile"] = str(reviewer_run / f"{retry_label}-err.txt")
        Path(retry_entry["outfile"]).write_text("", encoding="utf-8")
        Path(retry_entry["errfile"]).write_text("", encoding="utf-8")
        manifest["reviewers"][retry_label] = retry_entry
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (reviewer_run / f"{retry_label}-status.json").write_text(
            json.dumps(
                {
                    "label": retry_label,
                    "state": "completed",
                    "returncode": 0,
                    "finished_at": "2026-01-01T00:01:00Z",
                    "skill_verdicts": {"code-review": "FAIL"},
                }
            ),
            encoding="utf-8",
        )
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("code-review=FAIL", decision.reason)

    def test_gate_opt_in_requires_distinct_drift_and_code_review_contracts(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text("# Reviewer Evidence\n- Drift audit only.\n", encoding="utf-8")
        self.write_validated_reviewer_run(artifact)
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        manifest_path = reviewer_run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        review_label = next(label for label in manifest["reviewers"] if "code-review" in label)
        del manifest["reviewers"][review_label]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (reviewer_run / f"{review_label}-status.json").unlink()
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("code-review", decision.reason)
        self.assertIn("separate validated launch", decision.reason)

    def test_gate_rejects_hand_authored_manifest_with_no_real_launch_footprint(self):
        # A Developer has full write access to its own reviewer-runs tree
        # and can read reviewer-policy.json (including its own sha256) to
        # compute a matching policy_sha256. It could therefore hand-author a
        # manifest.json + <label>-status.json pair that satisfies every
        # digest/identity/model/effort/access/role/repo check without ever
        # invoking reviewer_jobs.py launch or a real harness process. The gate
        # must still reject that: a genuine start_tracked_reviewer launch always
        # records a positive pid and always creates real outfile/errfile
        # inside reviewer_artifact_root before the child process starts.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text(
            "# Reviewer Evidence\n- Label: 01-opencode-forged\n- Result summary: opencode reviewer ran successfully.\n",
            encoding="utf-8",
        )
        policy_path, policy = self.write_reviewer_policy(artifact)
        policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        label = "01-opencode-forged"
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        contract = {
            "status": "pass",
            "policy_sha256": policy_sha,
            "slice_id": "Slice 1",
            "plan_sha256": policy["plan_sha256"],
            "tool": "opencode",
            "model": "default",
            "effort": "default",
            "role": "reviewer",
            "access": "read-only",
            "repo_path": str(self.repo.resolve()),
            "cwd": str(self.repo.resolve()),
        }
        # Deliberately no pid, no outfile, no errfile, and no reviewer_jobs.py
        # invocation anywhere in this test — only three hand-written JSON
        # files, exactly mirroring the zero-effort forgery this closes.
        (reviewer_run / "manifest.json").write_text(
            json.dumps({"reviewers": {label: {"tool": "opencode", "launch_contract": contract}}}),
            encoding="utf-8",
        )
        (reviewer_run / f"{label}-status.json").write_text(
            json.dumps({"label": label, "state": "completed", "returncode": 0}), encoding="utf-8"
        )
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("without matching validated launch contracts", decision.reason)
        self.assertIn("real subprocess pid", decision.reason)

    def test_gate_rejects_reviewer_policy_changed_after_pm_snapshot(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text("# Reviewer Evidence\n- Label: 01-opencode-readonly-check\n", encoding="utf-8")
        self.write_validated_reviewer_run(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)
        policy_path = artifact / "reviewer-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["required_model"] = "unapproved-model"
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("changed after PM created", decision.reason)

    def test_gate_requires_successful_evidence_for_every_configured_reviewer_tool(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text("# Reviewer Evidence\n- Label: 01-opencode-readonly-check\n", encoding="utf-8")
        policy_path, policy = self.write_reviewer_policy(artifact)
        policy["required_tools"] = ["opencode", "codex"]
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        label = "01-opencode-readonly-check"
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        contract = {
            "status": "pass",
            "policy_sha256": policy_sha,
            "slice_id": "Slice 1",
            "plan_sha256": policy["plan_sha256"],
            "tool": "opencode",
            "model": "default",
            "effort": "default",
            "role": "reviewer",
            "access": "read-only",
            "repo_path": str(self.repo.resolve()),
            "cwd": str(self.repo.resolve()),
        }
        (reviewer_run / "manifest.json").write_text(
            json.dumps({"reviewers": {label: {"tool": "opencode", "command": ["opencode", "run"], "launch_contract": contract}}}),
            encoding="utf-8",
        )
        (reviewer_run / f"{label}-status.json").write_text(
            json.dumps({"label": label, "state": "completed", "returncode": 0}), encoding="utf-8"
        )
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode", "codex")
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("codex", decision.reason)

    def test_gate_rejects_matching_executable_without_validated_launch_contract(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text("# Reviewer Evidence\n- Label: 01-opencode-raw\n", encoding="utf-8")
        self.write_reviewer_policy(artifact)
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        (reviewer_run / "manifest.json").write_text(
            json.dumps({"reviewers": {"01-opencode-raw": {"tool": "opencode", "command": ["opencode", "run"]}}}),
            encoding="utf-8",
        )
        (reviewer_run / "01-opencode-raw-status.json").write_text(
            json.dumps({"label": "01-opencode-raw", "state": "completed", "returncode": 0}), encoding="utf-8"
        )
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)
        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("without matching validated launch contracts", decision.reason)
        self.assertIn("correct the semantic reviewer request", decision.reason)

    def test_gate_blocks_pass_when_reviewer_is_mislabeled_but_actually_a_different_executable(self):
        # Reproduces a live OpenCode/OpenCode test run: a reviewer labeled
        # "01-opencode-drift-check" whose manifest recorded "tool": "bash"
        # because the developer ran a shell one-liner through reviewer_jobs.py
        # instead of actually invoking `opencode`.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text(
            "# Reviewer Evidence\n- Label: 01-opencode-drift-check\n- Result summary: drift check passed.\n",
            encoding="utf-8",
        )
        self.write_reviewer_policy(artifact)
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        (reviewer_run / "manifest.json").write_text(
            json.dumps({"reviewers": {"01-opencode-drift-check": {"tool": "bash", "command": ["bash", "-c", "grep foo"]}}}),
            encoding="utf-8",
        )
        (reviewer_run / "01-opencode-drift-check-status.json").write_text(
            json.dumps({"label": "01-opencode-drift-check", "state": "completed", "returncode": 0}),
            encoding="utf-8",
        )
        pm.capture_reviewer_runs_summary(artifact)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)

        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )

        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("were never actually invoked", decision.reason)

    def test_gate_ignores_reviewer_evidence_when_no_reviewer_tool_is_required(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        state = self.init_run()

        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))

        self.assertEqual(decision.status, "pass")

    def test_capture_reviewer_runs_summary_records_status_files(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        (reviewer_run / "01-codex-check-status.json").write_text(
            json.dumps({"label": "01-codex-check", "state": "completed", "returncode": 0}),
            encoding="utf-8",
        )

        pm.capture_reviewer_runs_summary(artifact)

        summary = json.loads((artifact / "reviewer-runs-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["runs"][0]["reviewers"][0]["label"], "01-codex-check")

    def test_capture_reviewer_runs_summary_skips_current_symlink(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        reviewer_root = artifact / "reviewer-runs"
        reviewer_run = reviewer_root / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        (reviewer_run / "manifest.json").write_text(json.dumps({"reviewers": {}}), encoding="utf-8")
        (reviewer_run / "01-codex-check-status.json").write_text(
            json.dumps({"label": "01-codex-check", "state": "completed", "returncode": 0}),
            encoding="utf-8",
        )
        os.symlink(reviewer_run, reviewer_root / "current")

        pm.capture_reviewer_runs_summary(artifact)

        summary = json.loads((artifact / "reviewer-runs-summary.json").read_text(encoding="utf-8"))
        self.assertEqual([Path(entry["run_dir"]).name for entry in summary["runs"]], ["reviewers-1"])

    def test_reviewer_delegation_overview_flags_missing_contracted_marker(self):
        # Regression (PM Test 11): a reviewer that refused its task but exited
        # cleanly (state completed, returncode 0) passed the process-level
        # reviewer-evidence gate and was invisible in every summary. The
        # overview must surface, per reviewer, whether the output contains the
        # marker its own request's expected_output contracted.
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        for name, out_text in (("reviewers-1", "How would you like to proceed?"), ("reviewers-2", "RESULT: pass — verified.")):
            reviewer_run = artifact / "reviewer-runs" / name
            reviewer_run.mkdir(parents=True)
            (reviewer_run / "01-opencode-check-out.txt").write_text(out_text, encoding="utf-8")
            (reviewer_run / "manifest.json").write_text(
                json.dumps(
                    {
                        "reviewers": {
                            "01-opencode-check": {
                                "tool": "opencode",
                                "outfile": str(reviewer_run / "01-opencode-check-out.txt"),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (reviewer_run / "01-opencode-check-request.json").write_text(
                json.dumps({"expected_output": "Return RESULT: pass or RESULT: blocked."}),
                encoding="utf-8",
            )
            (reviewer_run / "01-opencode-check-status.json").write_text(
                json.dumps({"label": "01-opencode-check", "state": "completed", "returncode": 0}),
                encoding="utf-8",
            )

        overview = pm.reviewer_delegation_overview(artifact)

        self.assertEqual(len(overview), 2)
        by_run = {Path(entry["run_dir"]).name: entry for entry in overview}
        self.assertEqual(by_run["reviewers-1"]["contracted_marker"], "absent")
        self.assertEqual(by_run["reviewers-1"]["state"], "completed")
        self.assertEqual(by_run["reviewers-1"]["returncode"], 0)
        self.assertEqual(by_run["reviewers-1"]["output_tail"], "How would you like to proceed?")
        self.assertEqual(by_run["reviewers-2"]["contracted_marker"], "present")

    def test_reviewer_delegation_overview_reports_na_without_contracted_marker(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        (reviewer_run / "01-opencode-scan-out.txt").write_text("free-form notes", encoding="utf-8")
        (reviewer_run / "manifest.json").write_text(
            json.dumps(
                {
                    "reviewers": {
                        "01-opencode-scan": {
                            "tool": "opencode",
                            "outfile": str(reviewer_run / "01-opencode-scan-out.txt"),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (reviewer_run / "01-opencode-scan-request.json").write_text(
            json.dumps({"expected_output": "Describe what you found."}),
            encoding="utf-8",
        )

        overview = pm.reviewer_delegation_overview(artifact)

        self.assertEqual(overview[0]["contracted_marker"], "n/a")
        self.assertEqual(overview[0]["state"], "unknown")

    def test_gate_fails_closed_on_string_validation_entry(self):
        self.prepare_committed_repo()
        before, after = self._commit_readme_change()
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result_data(
            artifact,
            {
                "schema_version": pm.SCHEMA_VERSION,
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
                "residual_findings": [],
                "continuation_notes": [],
            },
        )
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "validation")
        self.assertIn("validation entries are malformed", decision.reason)

    def test_gate_fails_closed_on_string_changed_files(self):
        self.prepare_committed_repo()
        before, after = self._commit_readme_change()
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result_data(
            artifact,
            {
                "schema_version": pm.SCHEMA_VERSION,
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
                "residual_findings": [],
                "continuation_notes": [],
            },
        )
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "changed-files-mismatch")
        self.assertIn("changed_files is malformed", decision.reason)

    def test_artifact_exists_requires_nonempty_in_tree_file(self):
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        (artifact / "drift-audit.md").write_text("", encoding="utf-8")
        self.assertFalse(pm.artifact_exists(self.repo, artifact, {}, "drift_audit", "drift-audit.md"))
        (artifact / "drift-audit.md").write_text("verdict\n", encoding="utf-8")
        self.assertTrue(pm.artifact_exists(self.repo, artifact, {}, "drift_audit", "drift-audit.md"))
        artifact_relative = artifact.relative_to(self.repo) / "drift-audit.md"
        self.assertTrue(
            pm.artifact_exists(self.repo, artifact, {"drift_audit": {"path": str(artifact_relative)}}, "drift_audit", "drift-audit.md")
        )
        (self.repo / "README.md").write_text("not an audit artifact\n", encoding="utf-8")
        self.assertFalse(
            pm.artifact_exists(self.repo, artifact, {"drift_audit": {"path": "README.md"}}, "drift_audit", "drift-audit.md")
        )
        # An existing file outside the run must not satisfy the evidence check.
        self.assertFalse(
            pm.artifact_exists(self.repo, artifact, {"drift_audit": {"path": sys.executable}}, "drift_audit", "drift-audit.md")
        )

    # --- Review fixes: run integrity -------------------------------------

    def test_gate_blocks_empty_validation_summary(self):
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "validation-summary.md").write_text("", encoding="utf-8")
        state = self.init_run()
        decision = pm.verify_gate(self.repo, state, pm.parse_plan(self.plan)[0], artifact, before, after, pm.git_status_text(self.repo))
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "validation")
        self.assertIn("missing or empty", decision.reason)

    def test_gate_blocks_reviewer_that_never_completed_successfully(self):
        # A required reviewer that was genuinely launched with the right
        # executable but crashed (or is still running) proves nothing was
        # delegated; launch alone must not satisfy the reviewer-evidence gate.
        self.prepare_committed_repo()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = self.repo / ".ai-pm" / "runs" / "test" / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash=after)
        (artifact / "reviewer-evidence.md").write_text(
            "# Reviewer Evidence\n- Label: 01-opencode-readonly-check\n- Result summary: reviewer ran.\n",
            encoding="utf-8",
        )
        self.write_validated_reviewer_run(artifact, state="failed", returncode=1)
        state = self.init_run()
        self.attach_reviewer_policy_snapshot(state, artifact)
        decision = pm.verify_gate(
            self.repo, state, self._opt_in_slice(), artifact, before, after, pm.git_status_text(self.repo), ("opencode",)
        )
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "reviewer-evidence")
        self.assertIn("never completed successfully", decision.reason)

    def test_start_slice_writes_reviewer_policy_bound_to_attempt_and_round(self):
        # Finding 15: reviewer-policy.json must bind to the slice's starting
        # commit, the real attempt number, and repair round 0 at slice start
        # — otherwise its SHA-256 stays identical across the slice's repair
        # rounds and a Reviewer PASS obtained before a later tree-changing
        # repair keeps satisfying the opt-in independent-audit gate for final
        # work the Reviewer never saw.
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-pm" / "current").resolve()
        plan_slice = pm.parse_plan(self.plan)[0]
        before_head = git(self.repo, "rev-parse", "HEAD")
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
        artifact = run_dir / "slices" / "slice-001"
        policy = json.loads((artifact / "reviewer-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["before_head"], before_head)
        self.assertEqual(policy["session_generation"], 1)
        self.assertEqual(policy["repair_round"], 0)
        persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["current_slice"]["before_head"], before_head)
        self.assertEqual(persisted["current_slice"]["reviewer_policy"]["policy"]["before_head"], before_head)

    # --- Send guards and event-log behavior --------------------------------


if __name__ == "__main__":
    unittest.main()

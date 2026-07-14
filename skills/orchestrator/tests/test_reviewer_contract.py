from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import reviewer_contract


def load_reviewer_jobs():
    spec = importlib.util.spec_from_file_location("test_reviewer_jobs", SCRIPT_DIR / "reviewer_jobs.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewerContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "target.py").write_text("print('ok')\n", encoding="utf-8")
        self.artifact_root = self.repo / ".reviewer-runs"
        self.run_dir = self.artifact_root / "reviewers-test"
        self.run_dir.mkdir(parents=True)
        self.policy = {
            "schema_version": 2,
            "run_id": "run-1",
            "slice_id": "Slice 1",
            "plan_sha256": "a" * 64,
            "repo_path": str(self.repo),
            "reviewer_artifact_root": str(self.artifact_root),
            "required_tools": ["opencode"],
            "required_model": "provider/model",
            "required_effort": "default",
        }
        self.request = {
            "schema_version": 2,
            "label": "01-opencode-check-output",
            "slice_id": "Slice 1",
            "plan_sha256": "a" * 64,
            "tool": "opencode",
            "model": "provider/model",
            "effort": "default",
            "task": "Check the output.",
            "context": "A bounded validation task.",
            "required_skills": [],
            "files": ["target.py"],
            "constraints": ["Cite the actual evidence."],
            "expected_output": "RESULT: pass or RESULT: blocked.",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def validate(self, *, tool: str = "opencode"):
        policy = dict(self.policy, required_tools=[tool])
        request = dict(self.request, tool=tool)
        return reviewer_contract.validate_contract(policy, request, self.run_dir)

    def test_normalized_contract_owns_reviewer_role_and_read_only_access(self):
        contract = self.validate()
        self.assertEqual(contract["schema_version"], 2)
        self.assertEqual(contract["role"], "reviewer")
        self.assertEqual(contract["access"], "read-only")
        self.assertNotIn("role", self.request)
        self.assertNotIn("access", self.request)

    def test_request_rejects_retired_role_access_and_write_fields(self):
        for field, value in (
            ("role", "senior-reviewer"),
            ("access", "workspace-write"),
            ("worker_role", "junior-worker"),
            ("authorized_files", ["target.py"]),
        ):
            with self.subTest(field=field):
                request = dict(self.request, **{field: value})
                with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
                    reviewer_contract.validate_contract(self.policy, request, self.run_dir)
                self.assertEqual(raised.exception.issues[0].code, "unknown-field")
                self.assertEqual(raised.exception.issues[0].field, f"request.{field}")

    def test_request_requires_explicit_tool_model_and_effort(self):
        for field in ("tool", "model", "effort"):
            with self.subTest(field=field):
                request = dict(self.request)
                del request[field]
                with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
                    reviewer_contract.validate_contract(self.policy, request, self.run_dir)
                self.assertIn(
                    field,
                    {issue.field for issue in raised.exception.issues if issue.code == "missing-field"},
                )

    def test_policy_rejects_retired_and_unknown_fields(self):
        for field, value in (
            ("worker_artifact_root", str(self.artifact_root)),
            ("allowed_roles", ["reviewer"]),
            ("allowed_access", ["read-only"]),
            ("authorized_files", ["target.py"]),
            ("extension", True),
        ):
            with self.subTest(field=field):
                policy = dict(self.policy, **{field: value})
                with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
                    reviewer_contract.validate_contract(policy, self.request, self.run_dir)
                self.assertEqual(raised.exception.issues[0].code, "unknown-field")
                self.assertEqual(raised.exception.issues[0].field, f"policy.{field}")

    def test_schema_v1_is_rejected_for_policy_and_request(self):
        for target in ("policy", "request"):
            policy = dict(self.policy)
            request = dict(self.request)
            (policy if target == "policy" else request)["schema_version"] = 1
            with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
                reviewer_contract.validate_contract(policy, request, self.run_dir)
            self.assertIn("schema-version", {issue.code for issue in raised.exception.issues})

    def test_schema_v1_run_manifest_is_rejected_without_migration(self):
        reviewer_jobs = load_reviewer_jobs()
        reviewer_jobs.write_json(
            self.run_dir / reviewer_jobs.MANIFEST_NAME,
            {"schema_version": 1, "reviewers": {}},
        )
        with self.assertRaisesRegex(reviewer_jobs.ReviewerJobsError, "Start a new .orchestrator run"):
            reviewer_jobs.ensure_manifest(self.run_dir)

    def test_every_harness_is_reviewer_eligible_and_uses_contract_repo(self):
        repo_flags = {
            "claude": "--add-dir",
            "codex": "-C",
            "copilot": "--add-dir",
            "opencode": "--dir",
        }
        for tool, repo_flag in repo_flags.items():
            with self.subTest(tool=tool):
                contract = self.validate(tool=tool)
                command = reviewer_contract.compose_reviewer_command(contract, "prompt")
                self.assertEqual(Path(command[command.index(repo_flag) + 1]), self.repo.resolve())

        self.assertEqual(set(reviewer_contract.REVIEWER_PROFILES), set(repo_flags))
        self.assertEqual(
            reviewer_contract.REVIEWER_PROFILES["copilot"]["read_only_enforcement"],
            "prompt-enforced",
        )

    def test_harness_commands_select_only_reviewer_modes(self):
        cases = {
            "claude": ("--permission-mode", "plan"),
            "codex": ("--sandbox", "read-only"),
            "opencode": ("--agent", "plan"),
        }
        for tool, (flag, expected) in cases.items():
            with self.subTest(tool=tool):
                command = reviewer_contract.compose_reviewer_command(self.validate(tool=tool), "prompt")
                self.assertEqual(command[command.index(flag) + 1], expected)
                self.assertNotIn("acceptEdits", command)
                self.assertNotIn("workspace-write", command)
                self.assertNotIn("build", command)

        copilot = reviewer_contract.compose_reviewer_command(self.validate(tool="copilot"), "prompt")
        self.assertIn("--autopilot", copilot)

    def test_opencode_nondefault_effort_fails_closed(self):
        policy = dict(self.policy, required_effort="high")
        request = dict(self.request, effort="high")
        contract = reviewer_contract.validate_contract(policy, request, self.run_dir)
        with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
            reviewer_contract.compose_reviewer_command(contract, "prompt")
        self.assertEqual(raised.exception.issues[0].code, "unsupported-effort")
        self.assertEqual(raised.exception.issues[0].field, "effort")

    def test_prompt_intrinsically_forbids_mutation_for_every_harness(self):
        for tool in reviewer_contract.REVIEWER_PROFILES:
            with self.subTest(tool=tool):
                prompt = reviewer_contract.render_reviewer_prompt(self.validate(tool=tool))
                self.assertIn("ROLE: reviewer", prompt)
                self.assertIn("ACCESS: read-only", prompt)
                self.assertIn("must not create, edit, delete, move, or format files", prompt)
                self.assertIn("Do not run tests or commands that may write", prompt)
                self.assertIn("Do not perform Git, GitHub, commit, branch, staging, push", prompt)
                self.assertIn("Do not invoke orchestrator, re-delegate", prompt)

    def test_contract_mismatch_returns_actionable_corrections(self):
        self.request["slice_id"] = "Slice 2"
        self.request["model"] = "wrong/model"
        with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
            reviewer_contract.validate_contract(self.policy, self.request, self.run_dir)
        issues = {issue.code: issue for issue in raised.exception.issues}
        self.assertIn("slice-mismatch", issues)
        self.assertIn("model-mismatch", issues)
        self.assertIn("Rewrite the request for Slice 1", issues["slice-mismatch"].correction)
        self.assertIn("provider/model", issues["model-mismatch"].correction)

    def test_missing_and_outside_inputs_fail_closed(self):
        self.request["files"] = ["missing.py", "../outside.py"]
        with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
            reviewer_contract.validate_contract(self.policy, self.request, self.run_dir)
        codes = {issue.code for issue in raised.exception.issues}
        self.assertIn("missing-input", codes)
        self.assertIn("path-outside-repo", codes)

    def test_reserved_skill_sets_reject_mixed_and_accept_exact_request(self):
        self.policy["reserved_skill_sets"] = [["drift-audit"], ["code-review"]]
        self.request["required_skills"] = ["drift-audit", "code-review"]
        with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
            reviewer_contract.validate_contract(self.policy, self.request, self.run_dir)
        self.assertIn("reserved-skill-mismatch", {issue.code for issue in raised.exception.issues})

        self.request["required_skills"] = ["drift-audit"]
        contract = reviewer_contract.validate_contract(self.policy, self.request, self.run_dir)
        self.assertEqual(contract["required_skills"], ["drift-audit"])

    def test_reserved_skill_sets_fail_closed_when_malformed(self):
        malformed_values = (
            [["drift-audit"], "code-review"],
            [[]],
            [["drift-audit", ""]],
            [["drift-audit", 7]],
        )
        for value in malformed_values:
            with self.subTest(value=value):
                self.policy["reserved_skill_sets"] = value
                with self.assertRaises(reviewer_contract.ReviewerContractError) as raised:
                    reviewer_contract.validate_contract(self.policy, self.request, self.run_dir)
                self.assertIn(
                    "policy-reserved-skill-sets-malformed",
                    {issue.code for issue in raised.exception.issues},
                )

    def test_launch_records_schema_v2_normalized_evidence_and_artifacts(self):
        reviewer_jobs = load_reviewer_jobs()
        self.request["required_skills"] = ["drift-audit"]
        policy_path = self.repo / "reviewer-policy.json"
        request_path = self.repo / "reviewer-request.json"
        policy_path.write_text(json.dumps(self.policy), encoding="utf-8")
        request_path.write_text(json.dumps(self.request), encoding="utf-8")
        reviewer_jobs.ensure_manifest(self.run_dir)
        args = mock.Mock(run_dir=str(self.run_dir), policy=str(policy_path), request=str(request_path), depends_on=None)
        with mock.patch.dict(os.environ, {reviewer_jobs.ARTIFACT_ROOT_ENV: str(self.artifact_root)}), mock.patch.object(
            reviewer_jobs,
            "start_tracked_reviewer",
            return_value={"label": self.request["label"], "pid": 123, "run_dir": str(self.run_dir)},
        ) as start, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reviewer_jobs.command_launch(args), 0)

        evidence = start.call_args.kwargs["launch_contract"]
        self.assertEqual(evidence["schema_version"], 2)
        self.assertEqual(evidence["role"], "reviewer")
        self.assertEqual(evidence["access"], "read-only")
        self.assertEqual(evidence["required_skills"], ["drift-audit"])
        self.assertEqual(evidence["cwd"], str(self.repo.resolve()))
        self.assertTrue((self.run_dir / f"{self.request['label']}-launch.json").is_file())
        self.assertTrue((self.run_dir / f"{self.request['label']}-prompt.md").is_file())

    def test_rejected_launch_writes_feedback_and_starts_nothing(self):
        reviewer_jobs = load_reviewer_jobs()
        self.request["role"] = "reviewer"
        policy_path = self.repo / "reviewer-policy.json"
        request_path = self.repo / "reviewer-request.json"
        policy_path.write_text(json.dumps(self.policy), encoding="utf-8")
        request_path.write_text(json.dumps(self.request), encoding="utf-8")
        reviewer_jobs.ensure_manifest(self.run_dir)
        args = mock.Mock(run_dir=str(self.run_dir), policy=str(policy_path), request=str(request_path), depends_on=None)
        with mock.patch.dict(os.environ, {reviewer_jobs.ARTIFACT_ROOT_ENV: str(self.artifact_root)}), mock.patch.object(
            reviewer_jobs, "start_tracked_reviewer"
        ) as start, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reviewer_jobs.command_launch(args), 2)
        start.assert_not_called()
        feedback = json.loads((self.run_dir / f"{self.request['label']}-request-feedback.json").read_text())
        self.assertEqual(feedback["issues"][0]["code"], "unknown-field")

    def test_required_skill_bundle_includes_transitive_markdown_references(self):
        bundle = reviewer_contract.compile_skill_bundle("orchestrator")
        self.assertIn("# Deterministic Reviewer Contract", bundle)
        self.assertIn("reviewer_jobs.py launch", bundle)

    def test_audit_skill_prompt_and_verdict_extraction(self):
        reviewer_jobs = load_reviewer_jobs()
        self.request["required_skills"] = ["drift-audit"]
        prompt = reviewer_contract.render_reviewer_prompt(
            reviewer_contract.validate_contract(self.policy, self.request, self.run_dir)
        )
        self.assertIn("PM_AUDIT_VERDICT: PASS | PASS WITH RISKS | FAIL | BLOCKED", prompt)
        self.assertEqual(
            reviewer_jobs.audit_skill_verdicts(["code-review"], "report\nPM_AUDIT_VERDICT: PASS\n"),
            {"code-review": "PASS"},
        )
        self.assertEqual(
            reviewer_jobs.audit_skill_verdicts(
                ["drift-audit"], "PM_AUDIT_VERDICT: FAIL\nPM_AUDIT_VERDICT: PASS\n"
            ),
            {"drift-audit": None},
        )

    def test_force_cancel_does_not_signal_reused_child_pid(self):
        reviewer_jobs = load_reviewer_jobs()
        status_file = self.run_dir / "01-opencode-check-status.json"
        status_file.write_text(
            json.dumps(
                {
                    "label": "01-opencode-check",
                    "state": "running",
                    "child_pid": 4242,
                    "child_identity": "original-start original-command",
                }
            ),
            encoding="utf-8",
        )
        entry = {
            "label": "01-opencode-check",
            "pid": 3131,
            "status_file": str(status_file),
        }

        with mock.patch.object(reviewer_jobs, "process_identity", return_value="reused-start unrelated-command"), mock.patch.object(
            reviewer_jobs, "tracked_wrapper_running", return_value=False
        ), mock.patch.object(reviewer_jobs.os, "killpg") as killpg:
            reviewer_jobs.force_cancel_entry(entry)

        killpg.assert_not_called()
        self.assertEqual(
            reviewer_jobs.audit_skill_verdicts(
                ["drift-audit"],
                "PM_AUDIT_VERDICT: FAIL\nPM_AUDIT_VERDICT: PASS\n",
            ),
            {"drift-audit": None},
        )

    def test_force_cancel_surfaces_permission_failure_without_claiming_cancelled(self):
        reviewer_jobs = load_reviewer_jobs()
        status_file = self.run_dir / "01-opencode-permission-status.json"
        status_file.write_text(
            json.dumps(
                {
                    "label": "01-opencode-permission",
                    "state": "running",
                    "child_pid": 4242,
                    "child_identity": "original-start",
                }
            ),
            encoding="utf-8",
        )
        entry = {"label": "01-opencode-permission", "pid": 3131, "status_file": str(status_file)}

        with mock.patch.object(reviewer_jobs, "process_identity", return_value="original-start"), mock.patch.object(
            reviewer_jobs.os, "getpgid", return_value=4242
        ), mock.patch.object(reviewer_jobs.os, "killpg", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                reviewer_jobs.force_cancel_entry(entry)

        status = json.loads(status_file.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "running")

    def test_cancel_terminates_identity_verified_orphan_child(self):
        reviewer_jobs = load_reviewer_jobs()
        reviewer_jobs.ensure_manifest(self.run_dir)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            status_file = self.run_dir / "01-opencode-orphan-child.status.json"
            outfile = self.run_dir / "01-opencode-orphan-child.stdout.log"
            errfile = self.run_dir / "01-opencode-orphan-child.stderr.log"
            reviewer_jobs.write_json(
                status_file,
                {
                    "label": "01-opencode-orphan-child",
                    "state": "running",
                    "child_pid": child.pid,
                    "child_identity": reviewer_jobs.process_identity(child.pid),
                },
            )
            manifest = reviewer_jobs.load_manifest(self.run_dir)
            manifest["reviewers"]["01-opencode-orphan-child"] = {
                "label": "01-opencode-orphan-child",
                "pid": 99999999,
                "status_file": str(status_file),
                "outfile": str(outfile),
                "errfile": str(errfile),
            }
            reviewer_jobs.save_manifest(self.run_dir, manifest)
            args = mock.Mock(run_dir=str(self.run_dir), label=None, timeout=2.0, interval=0.05, json=True)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reviewer_jobs.command_cancel(args), 0)

            child.wait(timeout=5)
            self.assertEqual(child.returncode, -signal.SIGTERM)
            status = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "cancelled")
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def test_cancel_attempts_remaining_reviewers_after_one_permission_failure(self):
        reviewer_jobs = load_reviewer_jobs()
        reviewer_jobs.ensure_manifest(self.run_dir)
        manifest = reviewer_jobs.load_manifest(self.run_dir)
        for index in (1, 2):
            label = f"0{index}-opencode-reviewer"
            status_file = self.run_dir / f"{label}-status.json"
            outfile = self.run_dir / f"{label}-out.txt"
            errfile = self.run_dir / f"{label}-err.txt"
            outfile.write_text("", encoding="utf-8")
            errfile.write_text("", encoding="utf-8")
            reviewer_jobs.write_json(status_file, {"label": label, "state": "running"})
            manifest["reviewers"][label] = {
                "label": label,
                "pid": 9000 + index,
                "status_file": str(status_file),
                "outfile": str(outfile),
                "errfile": str(errfile),
            }
        reviewer_jobs.save_manifest(self.run_dir, manifest)

        def force(entry):
            if entry["label"].startswith("01-"):
                raise PermissionError("denied")
            reviewer_jobs.mark_cancelled_entry(entry, forced=True, returncode=-signal.SIGKILL)

        args = mock.Mock(run_dir=str(self.run_dir), label=None, timeout=0.0, interval=0.01, json=True)
        with mock.patch.object(reviewer_jobs, "tracked_wrapper_running", return_value=False), mock.patch.object(
            reviewer_jobs, "tracked_child_running", return_value=True
        ), mock.patch.object(reviewer_jobs, "signal_tracked_child", return_value=True), mock.patch.object(
            reviewer_jobs, "force_cancel_entry", side_effect=force
        ) as force_cancel, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(reviewer_jobs.ReviewerJobsError, "01-opencode-reviewer"):
                reviewer_jobs.command_cancel(args)

        self.assertEqual(force_cancel.call_count, 2)
        second_status = json.loads((self.run_dir / "02-opencode-reviewer-status.json").read_text(encoding="utf-8"))
        self.assertEqual(second_status["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()

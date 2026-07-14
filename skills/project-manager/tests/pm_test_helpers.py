"""Shared fixtures, fake harnesses, and the loaded pm module for the PM test suite.

Split from the original single-file test_mc.py (archived at archive/) so each
themed test module stays navigable. Test modules star-import this module and
subclass PmTestCase, which carries setUp/tearDown and the shared instance helpers.
"""
import argparse
import contextlib
import hashlib
import io
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path


PM_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pm.py"
SPEC = importlib.util.spec_from_file_location("pm", PM_PATH)
pm = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pm
SPEC.loader.exec_module(pm)
from pm_lib import runtime as pm_runtime  # noqa: E402
from pm_lib import tmux_adapter as pm_tmux_adapter  # noqa: E402
from pm_lib import commands as pm_commands  # noqa: E402
from pm_lib import observation as pm_observation  # noqa: E402
from pm_lib import runner as pm_runner  # noqa: E402
from pm_lib import state as pm_state  # noqa: E402


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def write_plan(path, approval="no", include_authorized=True):
    authorized = """- Files allowed to change:
  - README.md
- Functions/classes/components allowed to change: none.
- Tests allowed or expected to change: none."""
    if not include_authorized:
        authorized = """- Functions/classes/components allowed to change: none.
- Tests allowed or expected to change: none."""
    path.write_text(
        f"""# Test Plan

## Slice 1: First Slice

### Intended Change
- Add docs.

### Acceptance Criteria
- Dry run identifies this slice.

### Authorized Surface
{authorized}

### Explicit Non-Goals
- Do not change runtime code.

### Risk Flags
- Risky surfaces touched: none.
- Approval needed before implementation: {approval}.

### Validation Plan
- Commands to run:
  - git diff --check

### Rollback Path
- Revert README.md.

## Slice 2: Second Slice

### Intended Change
- Add more docs.

### Acceptance Criteria
- Dry run identifies this slice after Slice 1.

### Authorized Surface
- Files allowed to change:
  - CHANGELOG.md
- Functions/classes/components allowed to change: none.
- Tests allowed or expected to change: none.

### Explicit Non-Goals
- Do not change runtime code.

### Risk Flags
- Risky surfaces touched: none.
- Approval needed before implementation: no.

### Validation Plan
- Commands to run:
  - git diff --check

### Rollback Path
- Revert CHANGELOG.md.
""",
        encoding="utf-8",
    )


def configure_git_identity(repo):
    git(repo, "config", "user.email", "pm-test@example.invalid")
    git(repo, "config", "user.name", "PM Test")


def commit_all(repo, message="seed"):
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)


def write_fake_harness(path):
    path.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import subprocess
            import time
            from pathlib import Path

            artifact = Path(os.environ["PM_SLICE_ARTIFACT_DIR"])
            slice_id = os.environ["PM_SLICE_ID"]
            target = "README.md" if slice_id == "Slice 1" else "CHANGELOG.md"
            Path(target).write_text(f"{slice_id} completed\\n", encoding="utf-8")
            subprocess.run(["git", "add", target], check=True)
            subprocess.run(["git", "commit", "-m", f"Complete {slice_id}"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            (artifact / "validation-summary.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "drift-audit.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "code-review.md").write_text("PASS\\n", encoding="utf-8")
            result = {
                "schema_version": 3,
                "slice_id": slice_id,
                "status": "pass",
                "summary": f"{slice_id} done",
                "changed_files": [target],
                "validation": [{"command": "toy validation", "result": "pass", "notes": ""}],
                "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": True, "created": True, "hash": commit_hash},
                "next_action": "",
                "blockers": [],
                "residual_findings": [],
            }
            (artifact / "developer-result.json").write_text(json.dumps(result), encoding="utf-8")
            time.sleep(5)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_no_result_harness(path):
    path.write_text(
        textwrap.dedent(
            """
            import time

            time.sleep(1)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_hanging_harness(path):
    # Never writes a result and outlives any test timeout, so the batch
    # driver's --timeout-seconds path is what ends the run.
    path.write_text(
        textwrap.dedent(
            """
            import time

            time.sleep(60)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_usage_limit_resume_harness(path):
    path.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import subprocess
            import sys
            import termios
            import time
            from pathlib import Path

            artifact = Path(os.environ["PM_SLICE_ARTIFACT_DIR"])
            slice_id = os.environ["PM_SLICE_ID"]
            attrs = termios.tcgetattr(sys.stdin)
            attrs[3] = attrs[3] & ~(termios.ECHO | termios.ICANON)
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 1
            termios.tcsetattr(sys.stdin, termios.TCSANOW, attrs)
            time.sleep(2.5)
            print("\\033[2J\\033[HUsage limit reached. Try again in 1 minute.", flush=True)
            seen = ""
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                chunk = os.read(sys.stdin.fileno(), 4096).decode(errors="ignore")
                if chunk:
                    seen += chunk
                if "You were interrupted. Review what you were doing then continue." in seen:
                    break
                time.sleep(0.05)
            else:
                raise SystemExit(3)

            Path("README.md").write_text("resumed after rolling limit\\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], check=True)
            subprocess.run(["git", "commit", "-m", "Complete resumed slice"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            (artifact / "validation-summary.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "drift-audit.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "code-review.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "developer-result.json").write_text(json.dumps({
                "schema_version": 3,
                "slice_id": slice_id,
                "status": "pass",
                "summary": "resumed after rolling limit",
                "changed_files": ["README.md"],
                "validation": [{"command": "toy validation", "result": "pass", "notes": ""}],
                "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": True, "created": True, "hash": commit_hash},
                "next_action": "",
                "blockers": [],
                "residual_findings": [],
            }), encoding="utf-8")
            time.sleep(2)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_repairable_then_pass_harness(path):
    path.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import subprocess
            import time
            from pathlib import Path

            artifact = Path(os.environ["PM_SLICE_ARTIFACT_DIR"])
            marker = artifact / "repair-marker"
            slice_id = os.environ["PM_SLICE_ID"]
            if not marker.exists():
                marker.write_text("seen\\n", encoding="utf-8")
                (artifact / "developer-result.json").write_text(json.dumps({
                    "schema_version": 3,
                    "slice_id": slice_id,
                    "status": "repairable",
                    "summary": "retry",
                    "changed_files": [],
                    "validation": [],
                    "drift_audit": {"verdict": "", "path": ""},
                    "code_review": {"verdict": "", "path": ""},
                    "commit": {"requested": False, "created": False, "hash": None},
                    "next_action": "retry",
                    "blockers": [],
                    "residual_findings": [],
                }), encoding="utf-8")
                time.sleep(1)
                raise SystemExit(0)

            Path("README.md").write_text("repaired\\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], check=True)
            subprocess.run(["git", "commit", "-m", "Complete repaired slice"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            (artifact / "validation-summary.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "drift-audit.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "code-review.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "developer-result.json").write_text(json.dumps({
                "schema_version": 3,
                "slice_id": slice_id,
                "status": "pass",
                "summary": "repaired",
                "changed_files": ["README.md"],
                "validation": [{"command": "toy validation", "result": "pass", "notes": ""}],
                "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": True, "created": True, "hash": commit_hash},
                "next_action": "",
                "blockers": [],
                "residual_findings": [],
            }), encoding="utf-8")
            time.sleep(1)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


_STDIN_RAW_PREAMBLE = """
import json
import os
import subprocess
import sys
import termios
import time
from pathlib import Path

artifact = Path(os.environ["PM_SLICE_ARTIFACT_DIR"])
slice_id = os.environ["PM_SLICE_ID"]
attrs = termios.tcgetattr(sys.stdin)
attrs[3] = attrs[3] & ~(termios.ECHO | termios.ICANON)
attrs[6][termios.VMIN] = 0
attrs[6][termios.VTIME] = 1
termios.tcsetattr(sys.stdin, termios.TCSANOW, attrs)


def write_failing_validation_result():
    (artifact / "developer-result.json").write_text(json.dumps({
        "schema_version": 3,
        "slice_id": slice_id,
        "status": "pass",
        "summary": "no validation yet",
        "changed_files": [],
        "validation": [],
        "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
        "code_review": {"verdict": "PASS", "path": "code-review.md"},
        "commit": {"requested": False, "created": False, "hash": None},
        "next_action": "",
        "blockers": [],
        "residual_findings": [],
    }), encoding="utf-8")


def wait_for_repair_prompt(deadline_seconds=25):
    seen = ""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        chunk = os.read(sys.stdin.fileno(), 4096).decode(errors="ignore")
        if chunk:
            seen += chunk
        if "NOT accepted" in seen:
            return True
        time.sleep(0.05)
    return False


def wait_for_initial_prompt(deadline_seconds=20):
    # Wait until PM's initial prompt injection has arrived. A fixture that
    # shows a hard prompt on screen must do so only *after* injection, or it
    # races PM's readiness check, which correctly refuses to paste into a
    # visible hard prompt and the run times out instead of exercising the
    # intended repair-time refusal.
    seen = ""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        chunk = os.read(sys.stdin.fileno(), 4096).decode(errors="ignore")
        if chunk:
            seen += chunk
        if slice_id in seen:
            # The frozen slice id is part of every rendered prompt, so this
            # proves prompt injection without coupling to explanatory prose.
            return True
        time.sleep(0.05)
    return False
"""


def write_in_session_repair_harness(path):
    # Fails the validation gate once, then completes the slice properly when
    # the repair prompt arrives in the same session.
    path.write_text(
        _STDIN_RAW_PREAMBLE
        + textwrap.dedent(
            """
            write_failing_validation_result()
            if not wait_for_repair_prompt():
                raise SystemExit(3)
            Path("README.md").write_text("repaired in session\\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], check=True)
            subprocess.run(["git", "commit", "-m", "Complete repaired slice"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            (artifact / "validation-summary.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "drift-audit.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "code-review.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "developer-result.json").write_text(json.dumps({
                "schema_version": 3,
                "slice_id": slice_id,
                "status": "pass",
                "summary": "repaired in session",
                "changed_files": ["README.md"],
                "validation": [{"command": "toy validation", "result": "pass", "notes": ""}],
                "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": True, "created": True, "hash": commit_hash},
                "next_action": "",
                "blockers": [],
                "residual_findings": [],
            }), encoding="utf-8")
            time.sleep(2)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_always_failing_validation_harness(path):
    # Keeps failing the same validation gate on every round, in every session,
    # so the signature-keyed circuit breaker escalates and then trips.
    path.write_text(
        _STDIN_RAW_PREAMBLE
        + textwrap.dedent(
            """
            write_failing_validation_result()
            while True:
                if not wait_for_repair_prompt():
                    raise SystemExit(0)
                write_failing_validation_result()
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_alternating_failure_harness(path):
    # Alternates between two different repairable signatures (validation,
    # review) so the same-signature circuit breaker never trips and the
    # default repair budget is what ends the run.
    path.write_text(
        _STDIN_RAW_PREAMBLE
        + textwrap.dedent(
            """
            def write_failing_review_result():
                (artifact / "validation-summary.md").write_text("PASS\\n", encoding="utf-8")
                (artifact / "drift-audit.md").write_text("PASS\\n", encoding="utf-8")
                (artifact / "developer-result.json").write_text(json.dumps({
                    "schema_version": 3,
                    "slice_id": slice_id,
                    "status": "pass",
                    "summary": "review failed",
                    "changed_files": [],
                    "validation": [{"command": "toy validation", "result": "pass", "notes": ""}],
                    "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                    "code_review": {"verdict": "FAIL", "path": "code-review.md"},
                    "commit": {"requested": False, "created": False, "hash": None},
                    "next_action": "",
                    "blockers": [],
                    "residual_findings": [],
                }), encoding="utf-8")

            round_index = 0
            write_failing_validation_result()
            while True:
                if not wait_for_repair_prompt():
                    raise SystemExit(0)
                round_index += 1
                if round_index % 2 == 0:
                    write_failing_validation_result()
                else:
                    write_failing_review_result()
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_hard_prompt_at_repair_harness(path):
    # Puts a hard trust prompt on screen after the initial prompt has been
    # injected, then reports a repairable result: the repair delivery must
    # refuse and stop the run with evidence. Waiting for injection first is
    # load-bearing — see wait_for_initial_prompt in the preamble.
    path.write_text(
        "#!/usr/bin/env python3\n"
        + _STDIN_RAW_PREAMBLE
        + textwrap.dedent(
            """
            print("OpenAI Codex ›", flush=True)
            if not wait_for_initial_prompt():
                raise SystemExit(3)
            print("Do you trust the files in this folder?", flush=True)
            time.sleep(0.5)
            write_failing_validation_result()
            time.sleep(30)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_wrong_slice_id_harness(path):
    # Reports a result for a different slice: a terminal integrity breach that
    # must stop immediately with no repair round.
    path.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import time
            from pathlib import Path

            artifact = Path(os.environ["PM_SLICE_ARTIFACT_DIR"])
            (artifact / "developer-result.json").write_text(json.dumps({
                "schema_version": 3,
                "slice_id": "Slice 99",
                "status": "pass",
                "summary": "worked the wrong slice",
                "changed_files": [],
                "validation": [{"command": "toy validation", "result": "pass", "notes": ""}],
                "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": False, "created": False, "hash": None},
                "next_action": "",
                "blockers": [],
                "residual_findings": [],
            }), encoding="utf-8")
            time.sleep(5)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


class PmTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        self.plan = self.repo / "plan.md"
        write_plan(self.plan)

    def tearDown(self):
        self.tmp.cleanup()

    def init_run(self):
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.init_run(args), 0)
        current = self.repo / ".ai-pm" / "current"
        self.assertTrue(current.is_symlink())
        return json.loads((current.resolve() / "run.json").read_text(encoding="utf-8"))

    def terminal_slice_entry(
        self,
        state,
        *,
        slice_id="Slice 1",
        title="First Slice",
        status="pass",
        artifact_dir=None,
        before_head=None,
        commit=None,
    ):
        """Return a complete schema-v3 terminal entry for state-focused tests."""
        ordinal = int(slice_id.rsplit(" ", 1)[-1])
        return {
            "slice_id": slice_id,
            "title": title,
            "status": status,
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
            "artifact_dir": artifact_dir or f".ai-pm/runs/{state['run_id']}/slices/slice-{ordinal:03d}",
            "before_head": before_head or "a" * 40,
            "changed_files": [],
            "summary": "",
            "validation": [],
            "drift_audit": {"verdict": None, "path": ""},
            "code_review": {"verdict": None, "path": ""},
            "audit_provenance": {
                audit: {
                    "performed_by": "not-observed",
                    "reviewer_tool": None,
                    "reviewer_label": None,
                    "fallback_context": "test fixture did not observe audit execution",
                }
                for audit in ("drift-audit", "code-review")
            },
            "commit": commit or {"requested": False, "created": False, "hash": None},
            "next_action": "",
            "blockers": [],
            "residual_findings": [],
            "gate_reason": "test fixture",
            "reviewer_tools": [],
            "repair": pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
            "slice_summary": f".ai-pm/runs/{state['run_id']}/slices/slice-{ordinal:03d}/slice-summary.md",
        }

    def write_reviewer_policy(self, artifact, *, tool="opencode"):
        artifact.mkdir(parents=True, exist_ok=True)
        policy = {
            "schema_version": 2,
            "run_id": "test",
            "slice_id": "Slice 1",
            "plan_sha256": "a" * 64,
            "repo_path": str(self.repo.resolve()),
            "reviewer_artifact_root": str(artifact / "reviewer-runs"),
            "required_tools": [tool],
            "required_model": "default",
            "required_effort": "default",
        }
        policy_path = artifact / "reviewer-policy.json"
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return policy_path, policy

    def write_validated_reviewer_run(self, artifact, *, tool="opencode", label="01-opencode-readonly-check", state="completed", returncode=0):
        policy_path, policy = self.write_reviewer_policy(artifact, tool=tool)
        policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        reviewer_run = artifact / "reviewer-runs" / "reviewers-1"
        reviewer_run.mkdir(parents=True)
        base_contract = {
            "status": "pass",
            "policy_sha256": policy_sha,
            "slice_id": "Slice 1",
            "plan_sha256": policy["plan_sha256"],
            "tool": tool,
            "model": "default",
            "effort": "default",
            "role": "reviewer",
            "access": "read-only",
            "repo_path": str(self.repo.resolve()),
            "cwd": str(self.repo.resolve()),
        }
        # start_tracked_reviewer always records a positive subprocess pid and
        # always creates outfile/errfile via `.open("wb")` before the child
        # process starts, inside reviewer_artifact_root. The gate now requires
        # that real footprint, so a genuine-launch fixture must match it.
        labels = {
            label.replace("readonly-check", "drift-audit"): "drift-audit",
            label.replace("readonly-check", "code-review"): "code-review",
        }
        reviewers = {}
        for audit_label, required_skill in labels.items():
            outfile = reviewer_run / f"{audit_label}-out.txt"
            errfile = reviewer_run / f"{audit_label}-err.txt"
            outfile.write_text("", encoding="utf-8")
            errfile.write_text("", encoding="utf-8")
            reviewers[audit_label] = {
                "tool": tool,
                "command": [tool, "run"],
                "pid": 4242,
                "outfile": str(outfile),
                "errfile": str(errfile),
                "launch_contract": {**base_contract, "required_skills": [required_skill]},
            }
        (reviewer_run / "manifest.json").write_text(
            json.dumps({"reviewers": reviewers}),
            encoding="utf-8",
        )
        for audit_label, required_skill in labels.items():
            (reviewer_run / f"{audit_label}-status.json").write_text(
                json.dumps(
                    {
                        "label": audit_label,
                        "state": state,
                        "returncode": returncode,
                        "finished_at": "2026-01-01T00:00:00Z",
                        "skill_verdicts": {required_skill: "PASS"},
                    }
                ),
                encoding="utf-8",
            )
        pm.capture_reviewer_runs_summary(artifact)

    def attach_reviewer_policy_snapshot(self, state, artifact):
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "reviewer_policy": pm.reviewer_policy_snapshot(artifact / "reviewer-policy.json"),
        }

    def write_gate_result(
        self,
        artifact,
        *,
        changed_files,
        validation_result="pass",
        drift="PASS",
        review="PASS",
        commit_hash=None,
        residual_findings=None,
    ):
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "validation-summary.md").write_text("validation\n", encoding="utf-8")
        (artifact / "drift-audit.md").write_text("drift\n", encoding="utf-8")
        (artifact / "code-review.md").write_text("review\n", encoding="utf-8")
        result = {
            "schema_version": 3,
            "slice_id": "Slice 1",
            "status": "pass",
            "summary": "",
            "changed_files": changed_files,
            "validation": [] if validation_result is None else [{"command": "test", "result": validation_result, "notes": ""}],
            "drift_audit": {"verdict": drift, "path": "drift-audit.md"},
            "code_review": {"verdict": review, "path": "code-review.md"},
            "commit": {"requested": True, "created": bool(commit_hash), "hash": commit_hash},
            "next_action": "",
            "blockers": [],
            "residual_findings": list(residual_findings or []),
        }
        (artifact / "developer-result.json").write_text(json.dumps(result), encoding="utf-8")

    def write_gate_result_data(self, artifact, result):
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "validation-summary.md").write_text("validation\n", encoding="utf-8")
        (artifact / "drift-audit.md").write_text("drift\n", encoding="utf-8")
        (artifact / "code-review.md").write_text("review\n", encoding="utf-8")
        (artifact / "developer-result.json").write_text(json.dumps(result), encoding="utf-8")

    def prepare_committed_repo(self):
        configure_git_identity(self.repo)
        self.plan.write_text(self.plan.read_text(encoding="utf-8"), encoding="utf-8")
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        commit_all(self.repo)

    def _write_failing_validation_result(self, artifact, slice_id="Slice 1"):
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "developer-result.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "slice_id": slice_id,
                    "status": "pass",
                    "summary": "",
                    "changed_files": [],
                    "validation": [],
                    "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                    "code_review": {"verdict": "PASS", "path": "code-review.md"},
                    "commit": {"requested": False, "created": False, "hash": None},
                    "next_action": "",
                    "blockers": [],
                    "residual_findings": [],
                }
            ),
            encoding="utf-8",
        )

    def _model_supervised_current_slice(self, state, run_dir, repair=None):
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        current = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
            "tmux_session": "pm_test_slice-001_a1",
            "attempt": 1,
            "started_at": pm.utc_now(),
            "before_head": git(self.repo, "rev-parse", "HEAD"),
            "reviewer_tools": [],
            "pause": None,
            "repair": dict(repair) if repair is not None else pm_state.default_repair_state(),
            "reviewer_policy": {"sha256": "a" * 64, "policy": {}},
        }
        state["status"] = "running"
        state["supervision"]["mode"] = "model-supervised"
        state["current_slice"] = current
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        return artifact

    def _finalize_args(self):
        return argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command="python fake.py",
            reviewer_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )

    def _run_next_args(self, harness, timeout_seconds=20):
        return argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=timeout_seconds,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )

    def _commit_readme_change(self):
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        return before, git(self.repo, "rev-parse", "HEAD")

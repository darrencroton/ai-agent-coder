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


MC_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mc.py"
SPEC = importlib.util.spec_from_file_location("mc", MC_PATH)
mc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = mc
SPEC.loader.exec_module(mc)
from mc_lib import runtime as mc_runtime  # noqa: E402
from mc_lib import tmux_adapter as mc_tmux_adapter  # noqa: E402
from mc_lib import commands as mc_commands  # noqa: E402
from mc_lib import observation as mc_observation  # noqa: E402
from mc_lib import runner as mc_runner  # noqa: E402
from mc_lib import state as mc_state  # noqa: E402


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
    git(repo, "config", "user.email", "mc-test@example.invalid")
    git(repo, "config", "user.name", "MC Test")


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

            artifact = Path(os.environ["MC_SLICE_ARTIFACT_DIR"])
            slice_id = os.environ["MC_SLICE_ID"]
            target = "README.md" if slice_id == "Slice 1" else "CHANGELOG.md"
            Path(target).write_text(f"{slice_id} completed\\n", encoding="utf-8")
            subprocess.run(["git", "add", target], check=True)
            subprocess.run(["git", "commit", "-m", f"Complete {slice_id}"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            (artifact / "validation-summary.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "drift-audit.md").write_text("PASS\\n", encoding="utf-8")
            (artifact / "code-review.md").write_text("PASS\\n", encoding="utf-8")
            result = {
                "schema_version": 1,
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
            }
            (artifact / "orchestrator-result.json").write_text(json.dumps(result), encoding="utf-8")
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

            artifact = Path(os.environ["MC_SLICE_ARTIFACT_DIR"])
            slice_id = os.environ["MC_SLICE_ID"]
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
            (artifact / "orchestrator-result.json").write_text(json.dumps({
                "schema_version": 1,
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

            artifact = Path(os.environ["MC_SLICE_ARTIFACT_DIR"])
            marker = artifact / "repair-marker"
            slice_id = os.environ["MC_SLICE_ID"]
            if not marker.exists():
                marker.write_text("seen\\n", encoding="utf-8")
                (artifact / "orchestrator-result.json").write_text(json.dumps({
                    "schema_version": 1,
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
            (artifact / "orchestrator-result.json").write_text(json.dumps({
                "schema_version": 1,
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

artifact = Path(os.environ["MC_SLICE_ARTIFACT_DIR"])
slice_id = os.environ["MC_SLICE_ID"]
attrs = termios.tcgetattr(sys.stdin)
attrs[3] = attrs[3] & ~(termios.ECHO | termios.ICANON)
attrs[6][termios.VMIN] = 0
attrs[6][termios.VTIME] = 1
termios.tcsetattr(sys.stdin, termios.TCSANOW, attrs)


def write_failing_validation_result():
    (artifact / "orchestrator-result.json").write_text(json.dumps({
        "schema_version": 1,
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
            (artifact / "orchestrator-result.json").write_text(json.dumps({
                "schema_version": 1,
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
                (artifact / "orchestrator-result.json").write_text(json.dumps({
                    "schema_version": 1,
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
    # Puts a hard trust prompt on screen, then reports a repairable result:
    # the repair delivery must refuse and stop the run with evidence.
    path.write_text(
        _STDIN_RAW_PREAMBLE
        + textwrap.dedent(
            """
            print("Do you trust the files in this folder?", flush=True)
            time.sleep(0.5)
            write_failing_validation_result()
            time.sleep(30)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


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

            artifact = Path(os.environ["MC_SLICE_ARTIFACT_DIR"])
            (artifact / "orchestrator-result.json").write_text(json.dumps({
                "schema_version": 1,
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
            }), encoding="utf-8")
            time.sleep(5)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


class MasterControllerTests(unittest.TestCase):
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
            self.assertEqual(mc.init_run(args), 0)
        current = self.repo / ".ai-mc" / "current"
        self.assertTrue(current.is_symlink())
        return json.loads((current.resolve() / "run.json").read_text(encoding="utf-8"))

    def write_worker_policy(self, artifact, *, tool="opencode"):
        artifact.mkdir(parents=True, exist_ok=True)
        policy = {
            "schema_version": 1,
            "run_id": "test",
            "slice_id": "Slice 1",
            "plan_sha256": "a" * 64,
            "repo_path": str(self.repo.resolve()),
            "worker_artifact_root": str(artifact / "worker-runs"),
            "required_tools": [tool],
            "required_model": "default",
            "required_effort": "default",
            "allowed_access": ["read-only", "workspace-write"],
            "allowed_roles": ["junior-worker", "senior-worker"],
            "authorized_files": ["README.md"],
        }
        policy_path = artifact / "worker-policy.json"
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return policy_path, policy

    def write_validated_worker_run(self, artifact, *, tool="opencode", label="01-opencode-readonly-check", state="completed", returncode=0):
        policy_path, policy = self.write_worker_policy(artifact, tool=tool)
        policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        worker_run = artifact / "worker-runs" / "workers-1"
        worker_run.mkdir(parents=True)
        contract = {
            "status": "pass",
            "policy_sha256": policy_sha,
            "slice_id": "Slice 1",
            "plan_sha256": policy["plan_sha256"],
            "tool": tool,
            "model": "default",
            "effort": "default",
            "role": "junior-worker",
            "access": "read-only",
            "repo_path": str(self.repo.resolve()),
            "cwd": str(self.repo.resolve()),
        }
        # start_tracked_worker always records a positive subprocess pid and
        # always creates outfile/errfile via `.open("wb")` before the child
        # process starts, inside worker_artifact_root. The gate now requires
        # that real footprint, so a genuine-launch fixture must match it.
        outfile = worker_run / f"{label}-out.txt"
        errfile = worker_run / f"{label}-err.txt"
        outfile.write_text("", encoding="utf-8")
        errfile.write_text("", encoding="utf-8")
        (worker_run / "manifest.json").write_text(
            json.dumps(
                {
                    "workers": {
                        label: {
                            "tool": tool,
                            "command": [tool, "run"],
                            "pid": 4242,
                            "outfile": str(outfile),
                            "errfile": str(errfile),
                            "launch_contract": contract,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (worker_run / f"{label}-status.json").write_text(
            json.dumps({"label": label, "state": state, "returncode": returncode}), encoding="utf-8"
        )
        mc.capture_worker_runs_summary(artifact)

    def attach_worker_policy_snapshot(self, state, artifact):
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "worker_policy": mc.worker_policy_snapshot(artifact / "worker-policy.json"),
        }

    def test_run_state_creation(self):
        state = self.init_run()
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["repo_path"], str(self.repo.resolve()))
        self.assertEqual(state["plan_path"], str(self.plan.resolve()))
        self.assertEqual(state["harness"]["name"], "codex")
        self.assertEqual(state["plan"]["slice_count"], 2)
        self.assertEqual(state["supervision"]["mode"], "deterministic-batch")
        self.assertIn("rolling_usage_limit", state["supervision"]["pause_policy"])
        self.assertEqual(state["supervision"]["pause_counters"]["cumulative_pause_seconds_run"], 0)
        self.assertEqual(state["operational_events_path"], f".ai-mc/runs/{state['run_id']}/operational-events.jsonl")

    def test_init_can_create_and_switch_to_authorized_branch(self):
        self.prepare_committed_repo()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            branch="mc-trial/pi-calculator",
            create_branch=True,
        )

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)

        self.assertEqual(git(self.repo, "branch", "--show-current"), "mc-trial/pi-calculator")
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["branch"], "mc-trial/pi-calculator")

    def test_init_requires_authorization_to_create_missing_branch(self):
        self.prepare_committed_repo()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            branch="missing-branch",
            create_branch=False,
        )

        with self.assertRaisesRegex(mc.McError, "does not exist"):
            mc.init_run(args)

    def test_init_refuses_branch_switch_from_dirty_worktree(self):
        self.prepare_committed_repo()
        (self.repo / "pi_calculator.py").write_text("dirty\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            branch="mc-trial/pi-calculator",
            create_branch=True,
        )

        with self.assertRaisesRegex(mc.McError, "dirty worktree"):
            mc.init_run(args)

    def test_old_run_state_loads_with_supervision_defaults(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        state.pop("supervision")
        state.pop("operational_events_path")
        run_json.write_text(json.dumps(state), encoding="utf-8")

        loaded = mc.load_run(run_json)

        self.assertEqual(loaded["supervision"]["mode"], "deterministic-batch")
        self.assertEqual(loaded["supervision"]["max_consecutive_pauses_per_slice"], 2)
        self.assertEqual(loaded["operational_events_path"], f".ai-mc/runs/{state['run_id']}/operational-events.jsonl")

    def test_append_operational_event_does_not_rewrite_run_json(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        before = run_json.read_text(encoding="utf-8")

        event = mc.append_operational_event(self.repo, state, {"kind": "manual_note", "status": "recorded"})
        second = mc.append_operational_event(self.repo, state, {"kind": "manual_note", "status": "recorded"})

        self.assertEqual(run_json.read_text(encoding="utf-8"), before)
        self.assertEqual(event["event_id"], "op-0001")
        self.assertEqual(second["event_id"], "op-0002")
        event_path = self.repo / state["operational_events_path"]
        self.assertTrue(event_path.exists())
        records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["event_id"] for record in records], ["op-0001", "op-0002"])
        self.assertEqual(records[0]["kind"], "manual_note")

    def test_current_slice_state_records_before_head_and_pause_slot(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        state = mc.current_slice_state(
            self.repo,
            plan_slice,
            self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001",
            "mc_test_slice-001_a1",
            1,
            "2026-01-01T00:00:00Z",
            "a" * 40,
        )

        self.assertEqual(state["before_head"], "a" * 40)
        self.assertEqual(state["pause"], None)
        self.assertEqual(state["artifact_dir"], ".ai-mc/runs/test/slices/slice-001")

    def test_status_displays_paused_current_slice_fields(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        state["status"] = "paused"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": ".ai-mc/runs/test/slices/slice-001",
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": "2026-01-01T00:00:00Z",
            "before_head": "b" * 40,
            "pause": {"paused_until": "2026-01-01T01:00:00Z", "reason": "rolling usage limit reset"},
        }
        run_json.write_text(json.dumps(state), encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            self.assertEqual(mc.status(argparse.Namespace(repo=str(self.repo), run="current")), 0)

        rendered = output.getvalue()
        self.assertIn("Supervision mode: deterministic-batch", rendered)
        self.assertIn("Current before_head: " + "b" * 40, rendered)
        self.assertIn("Paused until: 2026-01-01T01:00:00Z (rolling usage limit reset)", rendered)

    def test_runnable_slice(self):
        slices = mc.parse_plan(self.plan)
        runnable, reasons = mc.eligibility(slices[0])
        self.assertTrue(runnable)
        self.assertEqual(reasons, [])
        self.assertEqual(slices[0].authorized_files, ["README.md"])

    def test_approval_needed_slice_blocks(self):
        write_plan(self.plan, approval="yes")
        slices = mc.parse_plan(self.plan)
        runnable, reasons = mc.eligibility(slices[0])
        self.assertFalse(runnable)
        self.assertTrue(any(reason.startswith("slice is approval-needed") for reason in reasons), reasons)

    def test_missing_authorized_surface_blocks(self):
        write_plan(self.plan, include_authorized=False)
        slices = mc.parse_plan(self.plan)
        runnable, reasons = mc.eligibility(slices[0])
        self.assertFalse(runnable)
        self.assertIn("authorized surface has no files allowed to change", reasons)

    def test_next_slice_skips_completed_state(self):
        self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        state = json.loads(run_json.read_text(encoding="utf-8"))
        state["slices"].append({"slice_id": "Slice 1", "status": "pass"})
        run_json.write_text(json.dumps(state), encoding="utf-8")
        slices = mc.parse_plan(self.plan)
        self.assertEqual(mc.next_slice(slices, state).slice_id, "Slice 2")

    def test_previous_completed_head_returns_prior_completed_commit(self):
        state = {
            "slices": [
                {
                    "slice_id": "Slice 1",
                    "status": "pass",
                    "commit": {"hash": "a" * 40},
                },
                {
                    "slice_id": "Slice 2",
                    "status": "fail",
                    "commit": {"hash": "b" * 40},
                },
            ],
        }

        self.assertEqual(mc.previous_completed_head(state, "Slice 2"), "a" * 40)

    def test_final_slice_stops_before_future_work(self):
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8")
            + """
## Future Work Outside This Plan

- Do not include this in Slice 2.

## Next Chat Prompt

Continue later.
""",
            encoding="utf-8",
        )
        slices = mc.parse_plan(self.plan)
        self.assertNotIn("Future Work", slices[-1].sections["Rollback Path"])
        self.assertNotIn("Next Chat Prompt", slices[-1].sections["Rollback Path"])

    def test_prompt_rendering_includes_frozen_contract(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(state, plan_slice, slice_artifact_dir, run_json)
        self.assertIn("Selected slice: Slice 1 - First Slice", prompt)
        self.assertIn("Authorized surface:", prompt)
        self.assertIn("README.md", prompt)
        self.assertIn("orchestrator-result.json", prompt)
        self.assertIn(str(mc.skill_root() / "references" / "run-state-schema.md"), prompt)
        self.assertIn(str(mc.worker_jobs_path()), prompt)
        self.assertIn(str(slice_artifact_dir / "worker-runs"), prompt)
        self.assertIn(str(slice_artifact_dir / "tmp"), prompt)
        self.assertIn(str(slice_artifact_dir / "tool-homes"), prompt)
        self.assertIn(str(slice_artifact_dir / "copilot-home"), prompt)
        self.assertIn('run_dir="$(python3 ', prompt)
        self.assertIn('launch --run-dir "$run_dir"', prompt)
        self.assertIn("Embedded ai-orchestrator instructions:", prompt)
        self.assertIn("Deterministic Worker Contract", prompt)
        self.assertIn("worker-evidence.md", prompt)
        self.assertIn("Required worker tool(s) for this run: none configured for this run", prompt)

    def test_prompt_rendering_states_configured_worker_tools_authoritatively(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(state, plan_slice, slice_artifact_dir, run_json, ("codex",))
        self.assertIn("Required worker tool(s) for this run: codex", prompt)
        self.assertIn("Every configured tool is required to complete", prompt)

    def test_prompt_rendering_states_worker_model_and_effort(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            ("codex",),
            "gpt-5.5",
            "low",
        )
        self.assertIn("Required worker model for this run: gpt-5.5", prompt)
        self.assertIn("Required worker effort for this run: low", prompt)
        self.assertIn('"model": "gpt-5.5"', prompt)
        self.assertIn('"effort": "low"', prompt)
        self.assertIn("Do not construct or invoke a worker harness command yourself", prompt)

    def test_prompt_rendering_embeds_ai_orchestrator_instead_of_worker_flag_guidance(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            ("claude", "copilot"),
            "some-model",
            "medium",
        )
        self.assertIn("BEGIN EMBEDDED SKILL FILE:", prompt)
        self.assertIn("name: ai-orchestrator", prompt)
        self.assertIn('"model": "some-model"', prompt)
        self.assertIn('"effort": "medium"', prompt)
        self.assertNotIn("Worker model/effort guidance:", prompt)

    def test_repair_prompt_covers_every_repairable_signature(self):
        # Every repairable signature must render a complete prompt (no
        # KeyError/IndexError from stray braces) that states the slice is not
        # accepted, quotes the gate reason, re-anchors the authorized surface,
        # and repeats the invariant instructions.
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        from mc_lib.gates import REPAIRABLE_SIGNATURES

        # One distinctive stanza marker per repairable signature, so a wrong
        # stanza selection cannot pass on the shared invariants alone.
        stanza_markers = {
            "validation": "Fix only the validation gap",
            "drift": "Fix only the drift audit gap",
            "review": "Fix only the code review gap",
            "worker-evidence": "Fix only the worker evidence gap",
            "unauthorized-files": "restore-only",
            "changed-files-mismatch": "No file edits are needed",
            "result-malformed": "valid JSON matching the required schema",
            "commit-missing": "commit skill",
            "dirty-worktree": "uncommitted changes outside `.ai-mc/`",
            "orchestrator-repairable": "You reported status `repairable` yourself",
        }
        self.assertEqual(set(stanza_markers), set(REPAIRABLE_SIGNATURES))

        for signature in sorted(REPAIRABLE_SIGNATURES):
            gate = mc.GateDecision(
                "repairable",
                f"gate reason for {signature} with literal {{braces}} kept",
                None,
                ("README.md",),
                signature=signature,
            )
            prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head="a" * 40)
            self.assertIn("NOT accepted", prompt, signature)
            self.assertIn(f"gate reason for {signature} with literal {{braces}} kept", prompt)
            self.assertIn(f"category: {signature}", prompt)
            self.assertIn(stanza_markers[signature], prompt, signature)
            self.assertIn("- README.md", prompt)
            self.assertIn("Do not change any other file.", prompt)
            self.assertIn("orchestrator-result.json", prompt)
            self.assertIn("git rev-parse HEAD", prompt)
            self.assertIn("Slice 1", prompt)

    def test_repair_prompt_worker_evidence_preserves_existing_work(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = mc.GateDecision(
            "repairable",
            "required worker tool(s) (opencode) were never actually invoked",
            None,
            ("README.md",),
            signature="worker-evidence",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head="a" * 40)
        self.assertIn("do NOT re-implement", prompt)
        self.assertIn("worker evidence", prompt)
        self.assertIn("were never actually invoked", prompt)

    def test_repair_prompt_unauthorized_files_is_restore_only(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        before = "b" * 40
        gate = mc.GateDecision(
            "repairable",
            "unauthorized changed files: EVIL.md",
            None,
            ("EVIL.md", "README.md"),
            signature="unauthorized-files",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head=before)
        self.assertIn("OUTSIDE your authorized surface: EVIL.md", prompt)
        self.assertIn(f"git checkout {before} -- EVIL.md", prompt)
        self.assertIn("touch nothing else", prompt)
        # The authorized file must not be named in the restore command.
        self.assertNotIn(f"git checkout {before} -- EVIL.md README.md", prompt)

    def test_repair_prompt_unauthorized_files_quotes_awkward_paths(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        before = "c" * 40
        gate = mc.GateDecision(
            "repairable",
            "unauthorized changed files: bad name.md, glob*.md",
            None,
            ("bad name.md", "glob*.md"),
            signature="unauthorized-files",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate, before_head=before)
        # Paths with spaces or metacharacters must survive a literal copy of
        # the restore command as single arguments.
        self.assertIn(f"git checkout {before} -- 'bad name.md' 'glob*.md'", prompt)

    def test_repair_prompt_changed_files_mismatch_needs_no_edits(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = mc.GateDecision(
            "repairable",
            "orchestrator changed_files does not match git evidence",
            None,
            ("README.md",),
            signature="changed-files-mismatch",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate)
        self.assertIn("No file edits are needed", prompt)
        self.assertIn("exactly match the actual diff: README.md", prompt)

    def test_repair_prompt_dirty_worktree_lists_meaningful_status(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "git-status-after.txt").write_text("M  README.md\n?? .ai-mc/scratch.txt\n", encoding="utf-8")
        gate = mc.GateDecision(
            "repairable",
            "post-commit worktree is dirty outside .ai-mc/",
            None,
            ("README.md",),
            signature="dirty-worktree",
        )
        prompt = mc_runtime.render_repair_prompt(plan_slice, artifact, gate)
        self.assertIn("M  README.md", prompt)
        self.assertNotIn(".ai-mc/scratch.txt", prompt)

    def test_git_status_text_preserves_leading_space_on_first_line(self):
        # `git status --short` is positional: " M file" (unstaged modify)
        # starts with a meaningful space. A stripped read shifted the first
        # line's path parse by one character ("EADME.md").
        self.prepare_committed_repo()
        (self.repo / "seed.txt").write_text("modified but unstaged\n", encoding="utf-8")
        status_text = mc.git_status_text(self.repo)
        self.assertTrue(status_text.startswith(" M "), repr(status_text.splitlines()[0]))
        self.assertEqual(mc.status_changed_files(status_text), {"seed.txt"})

    def test_repair_prompt_fails_closed_on_unknown_signature(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        artifact = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001"
        artifact.mkdir(parents=True, exist_ok=True)
        gate = mc.GateDecision("repairable", "reason", None, (), signature="mystery")
        with self.assertRaisesRegex(mc.McError, "no repair stanza"):
            mc_runtime.render_repair_prompt(plan_slice, artifact, gate)

    def test_repair_template_does_not_change_main_prompt_template(self):
        # The repair block is a second fenced template in the same reference
        # file; the main loader must still pick the original block.
        template = mc.load_prompt_template()
        self.assertIn("You are the slice orchestrator for Master Controller.", template)
        self.assertNotIn("NOT accepted", template)
        repair = mc_runtime.load_repair_template()
        self.assertIn("NOT accepted", repair)
        self.assertNotIn("Worker helper sequence", repair)

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

    def test_observe_without_current_slice_returns_snapshot_and_event(self):
        state = self.init_run()
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=None,
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(mc.observe(args), 0)

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
        fake_adapter.detect_activity.return_value = {"running": True, "active": True, "capture": "pane text"}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            harness_command=None,
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(mc.observe(args), 0)

        snapshot = json.loads(output.getvalue())
        self.assertEqual(snapshot["process"]["running"], True)
        self.assertEqual(snapshot["pane"]["tail"], "pane text")
        self.assertTrue((artifact / "pane-capture-live-latest.txt").exists())
        self.assertTrue((artifact / "observation-latest.json").exists())

    def test_send_records_literal_text_for_current_session(self):
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
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "ready"}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            text="You were interrupted. Continue.",
            reason="resume after reset",
            harness_command=None,
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.send(args), 0)
        fake_adapter.send_literal.assert_called_once_with("mc_test_slice-001_a1", "You were interrupted. Continue.")
        records = [
            json.loads(line)
            for line in (self.repo / state["operational_events_path"]).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[-1]["kind"], "send")
        self.assertEqual(records[-1]["text"], "You were interrupted. Continue.")

    def test_send_refuses_hard_prompt_from_observation(self):
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
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Approve this action"}
        fake_adapter.detect_hard_prompt.return_value = {"present": True, "kinds": ["approval_prompt"], "markers": ["Approve this action"]}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            text="continue",
            reason="test",
            harness_command=None,
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )
        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            with self.assertRaisesRegex(mc.McError, "hard prompt"):
                mc.send(args)
        fake_adapter.send_literal.assert_not_called()

    def test_hard_prompt_detection_ignores_mc_safety_text(self):
        safety_text = (
            "Commit creation is authorized only after validation. "
            "Do not push, open a PR, release, deploy, change dependencies/licenses, "
            "request secrets, or perform destructive actions unless explicitly authorized."
        )

        hard_prompt = mc.TmuxHarnessAdapter.detect_hard_prompt(safety_text)
        self.assertFalse(hard_prompt["present"])

        hints = mc.extract_operational_hints(safety_text, process_running=True, result_exists=False)
        self.assertFalse(any(hint["kind"] == "external_side_effect_request" for hint in hints))

    def test_full_rendered_orchestrator_prompt_triggers_no_hard_prompt_or_hard_stop_hint(self):
        # render_orchestrator_prompt embeds the complete ai-orchestrator skill
        # bundle (SKILL.md plus every linked Markdown resource, including each
        # model reference file). A doc phrase anywhere in that bundle that
        # happens to collide with a HARD_PROMPT_MARKERS substring would make
        # the repair-send guard and _raise_on_hard_stop_hints refuse delivery
        # on almost every run, since the embedded bundle stays in tmux
        # scrollback for most of a slice's life. Regression for the
        # `copilot.md` "Allow access to all URLs..." / permission_prompt
        # "allow access" collision found in review.
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        plan_slice = mc.parse_plan(self.plan)[0]
        slice_artifact_dir = run_json.parent / "slices" / "slice-001"
        prompt = mc.render_orchestrator_prompt(
            state,
            plan_slice,
            slice_artifact_dir,
            run_json,
            ("claude", "codex", "copilot", "opencode"),
            "some-model",
            "medium",
        )
        self.assertIn("BEGIN EMBEDDED SKILL FILE:", prompt)

        hard_prompt = mc.TmuxHarnessAdapter.detect_hard_prompt(prompt)
        self.assertFalse(hard_prompt["present"], hard_prompt.get("kinds"))

        hints = mc.extract_operational_hints(prompt, process_running=True, result_exists=False)
        hard_stop_hints = [hint["kind"] for hint in hints if hint.get("hard_stop")]
        self.assertEqual(hard_stop_hints, [])

    def test_hard_prompt_detection_keeps_external_side_effect_prompts(self):
        prompt = "Approve deploy to production? [y/n]"

        hard_prompt = mc.TmuxHarnessAdapter.detect_hard_prompt(prompt)
        self.assertTrue(hard_prompt["present"])
        self.assertIn("external_side_effect_request", hard_prompt["kinds"])

        hints = mc.extract_operational_hints(prompt, process_running=True, result_exists=False)
        external = next(hint for hint in hints if hint["kind"] == "external_side_effect_request")
        self.assertTrue(external["hard_stop"])

    def test_operational_hints_ignore_instructional_timeout_flags(self):
        text = 'Use worker_jobs.py wait --run-dir "$run_dir" --label check --timeout 300.'
        hints = mc.extract_operational_hints(text, process_running=True, result_exists=False)
        self.assertFalse(any(hint["kind"] == "network_transient" for hint in hints))

        real_error = mc.extract_operational_hints(
            "Network error: request timed out while contacting the provider.", process_running=True, result_exists=False
        )
        self.assertTrue(any(hint["kind"] == "network_transient" for hint in real_error))

    def test_operational_hints_parse_rolling_limit_duration(self):
        now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone(timedelta(hours=10)))

        hints = mc.extract_operational_hints(
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

        hints = mc.extract_operational_hints(
            "Session limit reached and will reset at 12:10AM.",
            process_running=True,
            result_exists=False,
            now=now,
        )

        usage = next(hint for hint in hints if hint["kind"] == "usage_limit")
        self.assertEqual(usage["subtype"], "rolling_window")
        self.assertFalse(usage["hard_stop"])
        self.assertEqual(usage["reset_at"], "2026-07-06T00:10:00+10:00")

        utc_hints = mc.extract_operational_hints(
            "Usage limit reached and will reset at 14:30 UTC.",
            process_running=True,
            result_exists=False,
            now=datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc),
        )
        utc_usage = next(hint for hint in utc_hints if hint["kind"] == "usage_limit")
        self.assertEqual(utc_usage["reset_at"], "2026-07-05T14:30:00+00:00")

    def test_operational_hints_prefer_relative_duration_over_absolute_time(self):
        now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone(timedelta(hours=10)))

        hints = mc.extract_operational_hints(
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
                hints = mc.extract_operational_hints(text, process_running=True, now=datetime(2026, 7, 5, tzinfo=timezone.utc))
                usage = next(hint for hint in hints if hint["kind"] == "usage_limit" and hint["subtype"] == subtype)
                self.assertTrue(usage["hard_stop"])
                self.assertEqual(usage["recovery_guidance"], "stop-for-user")

    def test_operational_hints_surface_sub_cap_weekly_usage_warning_without_blocking(self):
        text = (
            "You've used 91% of your weekly limit · resets Jul 9 at 8am (Australia/Sydney). "
            "Until July 7, you can use up to 50% of your plan's weekly usage limit on Fable 5. "
            "If you hit your limit, you can continue on Fable 5 with usage credits."
        )

        hints = mc.extract_operational_hints(text, process_running=True, result_exists=False)

        usage = next(hint for hint in hints if hint["kind"] == "usage_limit")
        self.assertEqual(usage["subtype"], "warning")
        self.assertFalse(usage["hard_stop"])
        self.assertEqual(usage["recovery_guidance"], "continue-with-observation")

    def test_operational_hints_classify_service_unavailable_and_ambiguous_absolute_reset(self):
        now = datetime(2026, 7, 5, 0, 10, tzinfo=timezone(timedelta(hours=10)))

        service = mc.extract_operational_hints(
            "Service unavailable. Please try again later in 10 minutes.",
            process_running=True,
            now=now,
        )
        service_hint = next(hint for hint in service if hint["kind"] == "service_unavailable")
        self.assertFalse(service_hint["hard_stop"])
        self.assertEqual(service_hint["retry_after_seconds"], 600)

        ambiguous = mc.extract_operational_hints(
            "Session limit reached and will reset at 11:55pm.",
            process_running=True,
            now=now,
            max_single_pause_seconds=21600,
        )
        usage = next(hint for hint in ambiguous if hint["kind"] == "usage_limit")
        self.assertEqual(usage["subtype"], "unknown_limit")
        self.assertTrue(usage["hard_stop"])

    def test_operational_hints_distinguish_live_and_exited_rolling_limit_guidance(self):
        now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
        text = "Usage limit reached. Try again in 1 hour."

        live = mc.extract_operational_hints(text, process_running=True, result_exists=False, now=now)
        exited = mc.extract_operational_hints(text, process_running=False, result_exists=False, now=now)
        ready = mc.extract_operational_hints(text, process_running=False, result_exists=True, now=now)

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
        fake_adapter.detect_activity.return_value = {"running": True, "active": False, "capture": "Weekly usage limit reached."}
        fake_adapter.detect_hard_prompt.return_value = {"present": False, "kinds": [], "markers": []}
        args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            text="continue",
            reason="test",
            harness_command=None,
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )

        with mock.patch.object(mc_observation, "TmuxHarnessAdapter", return_value=fake_adapter):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(mc.observe(args), 0)
            snapshot = json.loads(output.getvalue())
            self.assertEqual(snapshot["operational_hints"][0]["kind"], "usage_limit")
            self.assertTrue(snapshot["operational_hints"][0]["hard_stop"])
            with self.assertRaisesRegex(mc.McError, "hard-stop operational hint"):
                mc.send(args)
        fake_adapter.send_literal.assert_not_called()

    def write_gate_result(self, artifact, *, changed_files, validation_result="pass", drift="PASS", review="PASS", commit_hash=None):
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "validation-summary.md").write_text("validation\n", encoding="utf-8")
        (artifact / "drift-audit.md").write_text("drift\n", encoding="utf-8")
        (artifact / "code-review.md").write_text("review\n", encoding="utf-8")
        result = {
            "schema_version": 1,
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
        }
        (artifact / "orchestrator-result.json").write_text(json.dumps(result), encoding="utf-8")

    def write_gate_result_data(self, artifact, result):
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "validation-summary.md").write_text("validation\n", encoding="utf-8")
        (artifact / "drift-audit.md").write_text("drift\n", encoding="utf-8")
        (artifact / "code-review.md").write_text("review\n", encoding="utf-8")
        (artifact / "orchestrator-result.json").write_text(json.dumps(result), encoding="utf-8")

    def prepare_committed_repo(self):
        configure_git_identity(self.repo)
        self.plan.write_text(self.plan.read_text(encoding="utf-8"), encoding="utf-8")
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        commit_all(self.repo)

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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode", "codex")
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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
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

    def test_reconcile_repairs_failed_slice_after_commit_hash_evidence_mismatch(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        after = git(self.repo, "rev-parse", "HEAD")
        artifact = run_dir / "slices" / "slice-001"
        self.write_gate_result(artifact, changed_files=["README.md"], commit_hash="0" * 40)
        state["slices"].append(
            {
                "slice_id": "Slice 1",
                "title": "First Slice",
                "status": "fail",
                "started_at": "2026-01-01T00:00:00Z",
                "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
                "changed_files": ["README.md"],
                "validation": [{"command": "test", "result": "pass", "notes": ""}],
                "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
                "code_review": {"verdict": "PASS", "path": "code-review.md"},
                "commit": {"requested": True, "created": True, "hash": "0" * 40},
                "next_action": "",
                "blockers": [],
                "gate_reason": "reported commit is not the current HEAD",
            }
        )
        state["status"] = "failed"
        state["stop_reason"] = "reported commit is not the current HEAD"
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")

        args = argparse.Namespace(repo=str(self.repo), run="current")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.reconcile(args), 0)

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
            self.assertEqual(mc.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(run_args), 0)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["supervision"]["mode"], "deterministic-batch")
        self.assertEqual(state["slices"][0]["status"], "pass")
        self.assertEqual(state["slices"][0]["changed_files"], ["README.md"])
        slice_dir = (self.repo / ".ai-mc" / "current").resolve() / "slices" / "slice-001"
        self.assertTrue((slice_dir / "pane-capture.txt").exists())
        self.assertTrue((slice_dir / "pane-capture-live-latest.txt").exists())
        activity_path = slice_dir / "activity-attempt-1.jsonl"
        self.assertTrue(activity_path.exists())
        activity = json.loads(activity_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(set(activity), {"active", "checked_at", "running"})
        # First-attempt-pass guardrail: exactly one session, no repair
        # artifacts, and the pre-repair-loop slice-entry shape.
        self.assertFalse((slice_dir / "activity-attempt-2.jsonl").exists())
        self.assertFalse((slice_dir / "repair-prompt.md").exists())
        self.assertFalse((slice_dir / "repair-prompt-repair-1.md").exists())
        self.assertFalse((slice_dir / "orchestrator-result-repair-1.json").exists())
        self.assertFalse((slice_dir / "pane-capture-repair-1.txt").exists())
        self.assertEqual(
            set(state["slices"][0]),
            {
                "slice_id", "title", "status", "started_at", "completed_at", "artifact_dir",
                "before_head", "changed_files", "validation", "drift_audit", "code_review",
                "commit", "next_action", "blockers", "gate_reason", "worker_tools", "worker_policy",
            },
        )

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

    def _write_failing_validation_result(self, artifact, slice_id="Slice 1"):
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "orchestrator-result.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
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
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": mc.utc_now(),
            "before_head": git(self.repo, "rev-parse", "HEAD"),
            "worker_tools": [],
            "pause": None,
        }
        if repair is not None:
            current["repair"] = repair
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
            worker_tools="",
            allow_profile_command=False,
            allow_unattended_default=False,
            harness_model=None,
        )

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

    def test_run_next_refuses_while_current_slice_is_active(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
        with self.assertRaisesRegex(mc.McError, "active current slice"):
            mc.run_next(run_args)
        run_args.scope = "remaining"
        with self.assertRaisesRegex(mc.McError, "active current slice"):
            mc.run_remaining(run_args)

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

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_remaining_completes_two_toy_slices(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake_harness.py"
        write_fake_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
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
            self.assertEqual(mc.run_remaining(run_args), 0)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "complete")
        self.assertEqual([entry["status"] for entry in state["slices"]], ["pass", "pass"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_blocks_when_session_exits_without_result(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "no_result_harness.py"
        write_no_result_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(run_args), 2)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("orchestrator result missing", state["stop_reason"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_times_out_hanging_session_with_evidence(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "hanging_harness.py"
        write_hanging_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=3,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(run_args), 2)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        slice_dir = run_dir / "slices" / "slice-001"
        self.assertEqual(state["status"], "blocked")
        self.assertIn("timeout waiting for orchestrator-result.json", state["stop_reason"])
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
            self.assertEqual(mc.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(run_args), 0)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["slices"][0]["status"], "pass")
        self.assertTrue((run_dir / "slices" / "slice-001" / "activity-attempt-2.jsonl").exists())
        # The dead-session relaunch consumed one repair round but was not a
        # circuit-breaker step: the breaker state stays untouched.
        self.assertEqual(state["slices"][0]["repair"]["round"], 1)
        self.assertEqual(state["slices"][0]["repair"]["last_signature"], "")
        self.assertEqual(state["slices"][0]["repair"]["session_generation"], 2)

    def _run_next_args(self, harness, timeout_seconds=20):
        return argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=timeout_seconds,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_repairs_in_session_without_new_session(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "in_session_repair.py"
        write_in_session_repair_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(self._run_next_args(harness)), 0)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
        archived = json.loads((slice_dir / "orchestrator-result-repair-1.json").read_text(encoding="utf-8"))
        self.assertEqual(archived["validation"], [])
        self.assertTrue((slice_dir / "repair-prompt-repair-1.md").exists())
        self.assertTrue((slice_dir / "pane-capture-repair-1.txt").exists())
        final = json.loads((slice_dir / "orchestrator-result.json").read_text(encoding="utf-8"))
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
            self.assertEqual(mc.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(self._run_next_args(harness)), 2)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
            self.assertTrue((slice_dir / f"orchestrator-result-repair-{round_number}.json").exists())
            self.assertTrue((slice_dir / f"pane-capture-repair-{round_number}.txt").exists())
            self.assertTrue((slice_dir / f"git-status-repair-{round_number}.txt").exists())
        self.assertTrue((slice_dir / "repair-prompt-repair-1.md").exists())
        self.assertFalse((slice_dir / "orchestrator-result-repair-3.json").exists())
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
        plan_slice = mc.parse_plan(self.plan)[0]
        prompt_path = self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001" / "repair-prompt-repair-1.md"
        message = mc_runner._repair_delivery_message(plan_slice, prompt_path)
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
            self.assertEqual(mc.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(self._run_next_args(harness, timeout_seconds=30)), 2)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
        harness = Path(self.tmp.name) / "hard_prompt_at_repair.py"
        write_hard_prompt_at_repair_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(self._run_next_args(harness)), 2)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
            self.assertEqual(mc.init_run(args), 0)
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        state = json.loads(run_json.read_text(encoding="utf-8"))
        self.assertEqual(state["policy"]["max_repair_attempts"], 3)
        state["policy"]["max_repair_attempts"] = 0
        run_json.write_text(json.dumps(state), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(self._run_next_args(harness)), 2)
        state = json.loads(run_json.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertIn("repair budget exhausted", state["stop_reason"])
        slice_dir = (self.repo / ".ai-mc" / "current").resolve() / "slices" / "slice-001"
        self.assertFalse((slice_dir / "repair-prompt-repair-1.md").exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_integrity_gate_stops_immediately_without_repair(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "wrong_slice_id.py"
        write_wrong_slice_id_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(self._run_next_args(harness)), 2)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        slice_dir = run_dir / "slices" / "slice-001"
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("slice_id does not match", state["stop_reason"])
        self.assertNotIn("repair", state["slices"][0])
        self.assertFalse((slice_dir / "repair-prompt-repair-1.md").exists())
        self.assertFalse((slice_dir / "orchestrator-result-repair-1.json").exists())
        self.assertFalse((slice_dir / "activity-attempt-2.jsonl").exists())

    def test_run_remaining_stops_on_approval_needed_second_slice(self):
        write_plan(self.plan)
        text = self.plan.read_text(encoding="utf-8").replace(
            "Approval needed before implementation: no.\n\n### Validation Plan\n- Commands to run:\n  - git diff --check\n\n### Rollback Path\n- Revert CHANGELOG.md.",
            "Approval needed before implementation: yes.\n\n### Validation Plan\n- Commands to run:\n  - git diff --check\n\n### Rollback Path\n- Revert CHANGELOG.md.",
        )
        self.plan.write_text(text, encoding="utf-8")
        state = self.init_run()
        state["slices"].append({"slice_id": "Slice 1", "status": "pass"})
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
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
            self.assertEqual(mc.run_remaining(run_args), 2)
        stopped = json.loads(run_json.read_text(encoding="utf-8"))
        self.assertEqual(stopped["status"], "needs-human")
        self.assertIn("approval", stopped["stop_reason"])

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

    def test_stop_records_cancelled_state(self):
        self.init_run()
        args = argparse.Namespace(repo=str(self.repo), run="current", reason="test stop", harness_command=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.stop(args), 0)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["stop_reason"], "test stop")

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

    def test_rendered_prompt_states_claude_worker_auth_policy(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        state = self.init_run()
        artifact_dir = Path("/tmp/artifacts")
        run_json = Path("/tmp/run.json")
        prompt = mc.render_orchestrator_prompt(state, plan_slice, artifact_dir, run_json, ("claude",))
        self.assertIn("Required worker tool(s) for this run: claude", prompt)
        self.assertIn("Worker auth policy:", prompt)
        self.assertIn("MC does not set CLAUDE_CONFIG_DIR", prompt)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", prompt)

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


    # --- Review fixes: fail-closed parsing -------------------------------

    def test_approval_free_text_blocks(self):
        for value in ["not yet decided", "none", "maybe later"]:
            write_plan(self.plan, approval=value)
            plan_slice = mc.parse_plan(self.plan)[0]
            self.assertIsNone(plan_slice.approval_needed, value)
            runnable, reasons = mc.eligibility(plan_slice)
            self.assertFalse(runnable, value)
            self.assertIn("approval-needed risk flag is missing or unclear", reasons)

    def test_approval_exact_no_runs(self):
        write_plan(self.plan, approval="no")
        self.assertFalse(mc.parse_plan(self.plan)[0].approval_needed)

    def test_authorized_files_ignores_stray_bullet(self):
        plan_slice = mc.PlanSlice(
            1,
            "t",
            "",
            {
                "Authorized Surface": (
                    "- Files allowed to change:\n"
                    "  - README.md\n"
                    "- Note: be careful in this area\n"
                    "- Tests allowed or expected to change: none."
                )
            },
        )
        self.assertEqual(plan_slice.authorized_files, ["README.md"])

    def test_is_authorized_path_glob_is_segment_aware(self):
        self.assertTrue(mc.is_authorized_path("a.md", ["*.md"]))
        self.assertFalse(mc.is_authorized_path("deep/a.md", ["*.md"]))
        self.assertTrue(mc.is_authorized_path("deep/a.md", ["**/*.md"]))
        self.assertTrue(mc.is_authorized_path("src/a.py", ["src/*.py"]))
        self.assertFalse(mc.is_authorized_path("src/deep/a.py", ["src/*.py"]))

    def test_normalize_authorized_entry_strips_backtick_with_trailing_annotation(self):
        # Regression: entries like "`file.py` (new file)" were previously
        # normalized to "file.py` (new file)" because str.strip("`") only
        # trims from the very ends of the string, so a closing backtick
        # followed by an annotation was never removed.
        self.assertEqual(mc.normalize_authorized_entry("`nilakantha.py` (new file)"), "nilakantha.py")
        self.assertEqual(
            mc.normalize_authorized_entry(
                "`tests/__init__.py` (new file, only if required for test discovery; must stay empty)"
            ),
            "tests/__init__.py",
        )
        self.assertEqual(mc.normalize_authorized_entry("`*.md`"), "*.md")
        self.assertEqual(mc.normalize_authorized_entry("pi_calculator.py"), "pi_calculator.py")
        self.assertTrue(mc.is_authorized_path("nilakantha.py", ["`nilakantha.py` (new file)"]))

    # --- Review fixes: fail-closed gate ----------------------------------

    def _commit_readme_change(self):
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README.md").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "Good change")
        return before, git(self.repo, "rev-parse", "HEAD")

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

    def test_init_writes_self_ignoring_gitignore(self):
        self.init_run()
        gitignore = self.repo / ".ai-mc" / ".gitignore"
        self.assertTrue(gitignore.exists())
        self.assertEqual(gitignore.read_text(encoding="utf-8"), "*\n")

    def test_init_records_plan_digest(self):
        state = self.init_run()
        self.assertEqual(state["plan"]["sha256"], mc.plan_digest(self.plan))

    def test_verify_plan_unchanged_stops_on_edit(self):
        state = self.init_run()
        self.plan.write_text(self.plan.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
        with self.assertRaisesRegex(mc.McError, "plan file changed"):
            mc.verify_plan_unchanged(state, self.plan)

    def test_run_remaining_verifies_plan_before_completion_check(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        state["slices"].append({"slice_id": "Slice 1", "status": "pass"})
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
        with self.assertRaisesRegex(mc.McError, "plan file changed"):
            mc.run_remaining(args)

    def test_reconcile_verifies_plan_before_gate_recheck(self):
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["slices"].append(
            {
                "slice_id": "Slice 1",
                "title": "First Slice",
                "status": "fail",
                "started_at": "2026-01-01T00:00:00Z",
                "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
                "before_head": None,
            }
        )
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        self.plan.write_text(self.plan.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), run="current")
        with self.assertRaisesRegex(mc.McError, "plan file changed"):
            mc.reconcile(args)

    def test_init_rejects_duplicate_slice_numbers(self):
        dup = self.repo / "dup.md"
        dup.write_text("# Plan\n\n## Slice 1: A\n\n## Slice 1: B\n", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), plan=str(dup), harness="codex", worktree_root=None)
        with self.assertRaisesRegex(mc.McError, "duplicate slice numbers"):
            mc.init_run(args)

    def test_tool_homes_marked_sensitive(self):
        self.assertIn("tool-homes", mc.SENSITIVE_ARTIFACT_NAMES)

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
            self.assertEqual(mc.run_next(run_args), 2)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "needs-human")
        self.assertIn("branch changed since init", state["stop_reason"])

    def test_normalize_stop_status_maps_fail_and_unknown(self):
        self.assertEqual(mc.normalize_stop_status("fail"), "failed")
        self.assertEqual(mc.normalize_stop_status("weird"), "blocked")
        self.assertEqual(mc.normalize_stop_status("needs-human"), "needs-human")
        self.assertEqual(mc.normalize_stop_status("blocked"), "blocked")

    def test_slice_entry_records_before_head(self):
        gate = mc.GateDecision("pass", "ok", {"changed_files": []}, ())
        entry = mc.slice_entry_from_gate(self.repo, mc.parse_plan(self.plan)[0], self.repo / "art", "2026-01-01T00:00:00Z", gate, "abc123")
        self.assertEqual(entry["before_head"], "abc123")

    def test_reconcile_uses_recorded_before_head(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        artifact = run_dir / "slices" / "slice-001"
        artifact.mkdir(parents=True)
        state["slices"].append(
            {
                "slice_id": "Slice 1",
                "title": "First Slice",
                "status": "fail",
                "started_at": "2026-01-01T00:00:00Z",
                "artifact_dir": str(artifact.relative_to(self.repo.resolve())),
                "before_head": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "commit": {"requested": True, "created": True, "hash": "0" * 40},
            }
        )
        state["status"] = "failed"
        (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
        captured = {}

        def fake_gate(repo, run_state, plan_slice, art, before, after, status, worker_tools=()):
            captured["before"] = before
            return mc.GateDecision("fail", "still bad", {"changed_files": []}, ())

        args = argparse.Namespace(repo=str(self.repo), run="current")
        with mock.patch.object(mc_commands, "verify_gate", fake_gate):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.reconcile(args), 2)
        self.assertEqual(captured["before"], "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    # --- Review fixes: harness readiness / launch parity -----------------

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

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for runtime test")
    def test_run_next_blocks_on_unexpected_gate_exception(self):
        self.prepare_committed_repo()
        harness = Path(self.tmp.name) / "fake_harness.py"
        write_fake_harness(harness)
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with mock.patch.object(mc_runner, "verify_gate", side_effect=ValueError("boom")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.run_next(run_args), 2)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
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
            self.assertEqual(mc.init_run(args), 0)
        run_args = argparse.Namespace(
            repo=str(self.repo),
            run="current",
            dry_run=False,
            timeout_seconds=10,
            poll_seconds=0.1,
            harness_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(harness))}",
        )
        with mock.patch.object(mc_runner, "verify_gate", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mc.run_next(run_args), 2)
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["stop_reason"], "interrupted by user")
        self.assertIsNone(state["current_slice"])

    # --- Approval-gated slices (approve / --assume-complete) --------------

    def test_approve_command_clears_explicit_yes_gate(self):
        write_plan(self.plan, approval="yes")
        self.prepare_committed_repo()
        state = self.init_run()
        plan_slice = mc.parse_plan(self.plan)[0]
        runnable, reasons = mc.eligibility(plan_slice, mc.approved_slice_ids(state))
        self.assertFalse(runnable)
        self.assertTrue(any("approve command" in reason for reason in reasons))

        approve_args = argparse.Namespace(repo=str(self.repo), run="current", slice="Slice 1", reason="risk reviewed")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.approve_slice(approve_args), 0)

        updated = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertIn("Slice 1", updated["approvals"])
        self.assertEqual(updated["approvals"]["Slice 1"]["reason"], "risk reviewed")
        runnable, reasons = mc.eligibility(plan_slice, mc.approved_slice_ids(updated))
        self.assertTrue(runnable, reasons)
        events_path = self.repo / updated["operational_events_path"]
        records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(record.get("kind") == "approval" and record.get("slice_id") == "Slice 1" for record in records))

        dry_args = argparse.Namespace(repo=str(self.repo), run="current", dry_run=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(dry_args), 0)

    def test_approve_rejects_non_gated_slice(self):
        self.prepare_committed_repo()
        self.init_run()
        approve_args = argparse.Namespace(repo=str(self.repo), run="current", slice="Slice 1", reason="")
        with self.assertRaisesRegex(mc.McError, "not approval-gated"):
            mc.approve_slice(approve_args)

    def test_approval_does_not_clear_unclear_flag(self):
        write_plan(self.plan, approval="not yet decided")
        plan_slice = mc.parse_plan(self.plan)[0]
        runnable, reasons = mc.eligibility(plan_slice, {"Slice 1"})
        self.assertFalse(runnable)
        self.assertTrue(any("missing or unclear" in reason for reason in reasons))

    def test_init_assume_complete_adopts_prior_slices(self):
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            assume_complete="Slice 1",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["slices"]), 1)
        entry = state["slices"][0]
        self.assertEqual(entry["slice_id"], "Slice 1")
        self.assertEqual(entry["status"], "assumed-complete")
        self.assertIn("operator attested", entry["gate_reason"])
        candidate = mc.next_slice(mc.parse_plan(self.plan), state)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.slice_id, "Slice 2")

    def test_init_assume_complete_rejects_unknown_slice(self):
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            assume_complete="Slice 99",
        )
        with self.assertRaisesRegex(mc.McError, "not in the plan"):
            mc.init_run(args)

    def test_init_policy_flags_are_recorded(self):
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            max_repair_attempts=1,
            no_commit_required=True,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["policy"]["max_repair_attempts"], 1)
        self.assertFalse(state["policy"]["commit_required"])
        self.assertEqual(state["approvals"], {})

    # --- Gate hardening (validation artifact, worker success) -------------

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
            self.repo, state, mc.parse_plan(self.plan)[0], artifact, before, after, mc.git_status_text(self.repo), ("opencode",)
        )
        self.assertEqual(decision.status, "repairable")
        self.assertEqual(decision.signature, "worker-evidence")
        self.assertIn("never completed successfully", decision.reason)

    # --- Send guards and event-log behavior --------------------------------

    def test_send_rejects_multiline_text(self):
        self.prepare_committed_repo()
        self.init_run()
        args = argparse.Namespace(repo=str(self.repo), run="current", text="line one\nline two", reason="test")
        with self.assertRaisesRegex(mc.McError, "single line"):
            mc.send(args)

    def test_send_literal_rejects_multiline_text(self):
        adapter = mc.TmuxHarnessAdapter("codex", "python fake.py")
        with self.assertRaisesRegex(mc.McError, "single line"):
            adapter.send_literal("some-session", "line one\nline two")

    def test_event_counter_seeds_from_existing_log(self):
        state = self.init_run()
        event_path = self.repo / state["operational_events_path"]
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text(
            "\n".join(json.dumps({"event_id": f"op-{n:04d}", "kind": "observation"}) for n in (1, 2, 3)) + "\n",
            encoding="utf-8",
        )
        record = mc.append_operational_event(self.repo, state, {"kind": "manual_note", "status": "recorded"})
        self.assertEqual(record["event_id"], "op-0004")
        counter_path = event_path.with_name(event_path.name + ".counter")
        self.assertEqual(counter_path.read_text(encoding="utf-8").strip(), "4")

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

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for orphan detection test")
    def test_status_warns_when_active_session_is_gone(self):
        self.prepare_committed_repo()
        state = self.init_run()
        run_dir = (self.repo / ".ai-mc" / "current").resolve()
        state["status"] = "running"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "Toy",
            "artifact_dir": f".ai-mc/runs/{state['run_id']}/slices/slice-001",
            "tmux_session": "mc_no_such_session_xyz",
            "attempt": 1,
            "started_at": mc.utc_now(),
            "before_head": None,
            "pause": None,
            "worker_tools": [],
            "repair": mc_state.default_repair_state(),
        }
        (run_dir / "run.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(mc.status(argparse.Namespace(repo=str(self.repo), run="current")), 0)
        output = buffer.getvalue()
        self.assertIn("WARNING", output)
        self.assertIn("mc_no_such_session_xyz", output)

    # --- Cross-skill dependency contract ---------------------------------

    def test_worker_jobs_module_exposes_claude_project_root(self):
        module = mc.worker_jobs_module()
        self.assertTrue(hasattr(module, "claude_project_root"))


if __name__ == "__main__":
    unittest.main()

"""Shared fixtures for the PM Stage 1 test suite.

Provides `PmTestCase`: a temp git repo per test, a valid-minimal-plan
writer (parameterizable per slice), an in-process CLI runner, and a
run-creation helper built on `state.create_run`. Kept deliberately minimal
— there is no fake harness here; Stage 1 has no session lifecycle to fake.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import cli  # noqa: E402
from pm_lib import plan as plan_mod  # noqa: E402
from pm_lib import state as state_mod  # noqa: E402


def render_slice(
    number: int,
    *,
    title: str | None = None,
    files: list[str] | None = ("a.py",),
    approval: str = "no",
    audit: str = "no",
    risky: str = "none",
    intended: str = "Do the thing.",
    acceptance: str = "It works.",
    non_goals: str = "Nothing else.",
    validation: str = "Run the tests.",
    rollback: str = "git revert.",
) -> str:
    """Render one '## Slice N: ...' block in the canonical plan shape.

    File entries are written as indented sub-bullets under a "- Files
    allowed to change:" bullet, matching the shape `PlanSlice.authorized_files`
    parses (sibling column-0 bullets stop the capture; that is deliberately
    exercised by tests, not by this helper).
    """
    title = title or f"Slice {number} title"
    if files:
        file_lines = "\n".join(f"  - {f}" for f in files)
    else:
        file_lines = "  - none."
    return f"""## Slice {number}: {title}

### Intended Change
{intended}

### Acceptance Criteria
{acceptance}

### Authorized Surface
- Files allowed to change:
{file_lines}
- Functions/classes/components allowed to change: none.
- Tests allowed or expected to change: none.

### Explicit Non-Goals
{non_goals}

### Risk Flags
- Risky surfaces touched: {risky}.
- Approval needed before implementation: {approval}.
- Independent audit required: {audit}.

### Validation Plan
{validation}

### Rollback Path
{rollback}

"""


class PmTestCase(unittest.TestCase):
    """Base test case: a temp git repo, plan-writing and CLI-running helpers."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "pm-test@example.com")
        self._git("config", "user.name", "PM Test")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-q", "-m", "initial commit")

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            capture_output=True,
        )

    def write_plan(self, path: Path | None = None, *, slices: list[dict] | None = None) -> Path:
        """Write a valid minimal plan. `slices` is a list of render_slice() kwargs dicts."""
        if slices is None:
            slices = [{}]
        if path is None:
            path = self.repo / "plan.md"
        body = "# Test Plan\n\n" + "".join(
            render_slice(index + 1, **overrides) for index, overrides in enumerate(slices)
        )
        path.write_text(body, encoding="utf-8")
        return path

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        """Run cli.main(argv) in-process; returns (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
        except SystemExit as exc:
            code = 0 if exc.code is None else (exc.code if isinstance(exc.code, int) else 1)
        return code, out.getvalue(), err.getvalue()

    def make_run(
        self,
        *,
        plan_path: Path,
        branch: str = "main",
        harness: dict | None = None,
        reviewer: dict | None = None,
        policy: dict | None = None,
        slice_statuses: dict[str, str] | None = None,
        run_id: str | None = None,
    ):
        """Parse `plan_path` and create a run via state.create_run.

        `slice_statuses` maps slice id -> status ("attested" is the only
        status allowed at creation time; pending slices default to None).
        Returns (state, token, run_dir).
        """
        slices = plan_mod.parse_plan(plan_path)
        statuses = slice_statuses or {}
        entries = [
            {
                "id": s.slice_id,
                "title": s.title,
                "status": statuses.get(s.slice_id),
                "risk": s.plan_risk,
                "plan_risk": s.plan_risk,
                "commit": None,
                "attempts": 0,
            }
            for s in slices
        ]
        return state_mod.create_run(
            self.repo,
            plan_path=plan_path,
            plan_sha256=plan_mod.plan_digest(plan_path),
            slice_count=len(slices),
            branch=branch,
            harness=harness if harness is not None else {"name": "fake", "model": None, "effort": None},
            reviewer=reviewer if reviewer is not None else {"tools": [], "model": None, "effort": None},
            policy=policy if policy is not None else {"max_attempts": 3, "commit_required": True},
            slices=entries,
            run_id=run_id,
        )

    def set_current_slice(
        self,
        state: dict,
        token: str,
        run_dir: Path,
        *,
        slice_id: str,
        before_head: str | None,
        artifact_dir: Path | None = None,
        **overrides,
    ) -> dict:
        """Set `state['current_slice']` (Stage 2 floor tests) and persist it.

        `overrides` lets a test add extra current_slice fields (e.g.
        `attempts`); `before_head` and `artifact_dir` cover the fields the
        floor reads directly. Returns the freshly loaded, persisted state.
        """
        current = {
            "id": slice_id,
            "artifact_dir": str(artifact_dir) if artifact_dir is not None else "",
            "before_head": before_head,
        }
        current.update(overrides)
        updated = dict(state)
        updated["current_slice"] = current
        state_mod.save_state(run_dir, updated, token)
        return state_mod.load_state(run_dir, token)

    def record_approval(
        self, state: dict, token: str, run_dir: Path, *, slice_id: str, reason: str = "approved for test"
    ) -> dict:
        """Record a human approval for `slice_id` (Stage 2 floor tests) and persist it."""
        updated = dict(state)
        approvals = dict(updated.get("approvals") or {})
        approvals[slice_id] = {"at": "2026-01-01T00:00:00Z", "reason": reason}
        updated["approvals"] = approvals
        state_mod.save_state(run_dir, updated, token)
        return state_mod.load_state(run_dir, token)

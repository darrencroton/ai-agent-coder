"""Shared fixtures for the PM Stage 1 test suite.

Provides `PmTestCase`: a temp git repo per test, a valid-minimal-plan writer
(parameterizable per slice), an in-process CLI runner, a run-creation helper
built on `state.create_run`, and fake harness writers for process-lifecycle
tests.
"""

from __future__ import annotations

import io
import os
import re
import stat
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

# Stage 3 additions -----------------------------------------------------------
#
# Most Stage 3 commands (status/approve/start-slice/observe/finalize/stop)
# resolve their repo from the controller's cwd, not a --repo flag
# (only init/check-plan take one) — `run_cli_in_repo` runs a CLI call with
# cwd temporarily set to the test repo. `write_fake_harness` builds a tiny
# `sh` script standing in for a coding CLI in the headless process-lifecycle
# tests (the retained fake-harness pattern, replacement-ledger §9.1/§9.3).
# `parse_init_output` extracts the run id and one-time-printed token from
# `init`'s stdout so later commands in the same test can use them.

_RUN_ID_RE = re.compile(r"^run id:\s*(?P<run_id>\S+)", re.MULTILINE)
_TOKEN_RE = re.compile(r"^PM_RUN_TOKEN=(?P<token>\S+)$", re.MULTILINE)


def parse_init_output(stdout: str) -> tuple[str, str]:
    """Extract (run_id, token) from `init`'s stdout. Fails loudly if either
    is absent — a silent None would surface as a confusing failure much
    later in whatever test called this."""
    run_id_match = _RUN_ID_RE.search(stdout)
    token_match = _TOKEN_RE.search(stdout)
    if not run_id_match or not token_match:
        raise AssertionError(f"could not parse run id/token from init output:\n{stdout}")
    return run_id_match.group("run_id"), token_match.group("token")


def write_fake_harness(path: Path, body: str) -> Path:
    """Write an executable `sh` script fake harness at `path`.

    `body` is arbitrary shell; the caller composes it per scenario (reading
    $PM_RESULT_PATH / $PM_SLICE_ID / $PM_SLICE_ARTIFACT_DIR, sleeping,
    optionally committing, writing result.json). No real coding CLI is
    ever invoked in this suite.
    """
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def write_headless_fake_harness(path: Path) -> Path:
    """Write the standard resumable headless fake used by lifecycle tests.

    Every turn writes ``result.json``.  A resume turn, identified by
    ``PM_DEVELOPER_RESUME_SESSION_ID``, appends its final command argument to
    ``PM_HEADLESS_FAKE_APPEND_PATH`` and commits that file when it is inside
    a git repository.
    """
    return write_fake_harness(
        path,
        r"""set -eu
turn_text="${1:-}"
if [ -n "${PM_DEVELOPER_RESUME_SESSION_ID:-}" ]; then
    echo "HEADLESS_FAKE_RESUME:${PM_DEVELOPER_RESUME_SESSION_ID}"
    if [ -n "${PM_HEADLESS_FAKE_APPEND_PATH:-}" ]; then
        printf '%s\n' "$turn_text" >> "$PM_HEADLESS_FAKE_APPEND_PATH"
        git add "$PM_HEADLESS_FAKE_APPEND_PATH"
        git commit -q -m "Headless fake resume"
    fi
else
    echo "HEADLESS_FAKE_LAUNCH"
fi
printf '{"status":"complete","summary":"headless fake completed"}\n' > "$PM_RESULT_PATH"
""",
    )


# Shared fake-harness bodies. These are the scenario fragments both lifecycle
# suites (`test_slice_ops`, `test_finalize`) need, so they live here rather than
# being defined twice — a duplicated fake that drifts between the two files
# would make the same scenario mean two different things.


def write_result_cmd(status: str = "done", summary: str = "did the work") -> str:
    """A shell fragment writing a minimal valid `result.json` to $PM_RESULT_PATH."""
    return (
        "printf '{\"slice\": \"%s\", \"status\": \"" + status + "\", \"summary\": \"" + summary + "\"}\\n' "
        '"$PM_SLICE_ID" > "$PM_RESULT_PATH"'
    )


def idle_body(*, sleep_seconds: float = 30.0) -> str:
    """A fake harness that produces one line of output then idles until killed."""
    return f"echo FAKE_HARNESS_WORKING\nsleep {sleep_seconds}"


def commit_and_result_body(
    repo: Path,
    *,
    authorized_file: str = "a.py",
    unauthorized_file: str | None = None,
    delay: float = 0.0,
    tail_sleep: float = 0.0,
) -> str:
    """Commit the authorized change (optionally also touch an unauthorized
    file), write result.json, then optionally idle briefly."""
    lines = ["turn_text=\"${1:-}\"", "echo FAKE_HARNESS_WORKING"]
    if delay:
        lines.append(f"sleep {delay}")
    lines.append(f'echo "authorized change" >> "{repo}/{authorized_file}"')
    lines.append(f'git -C "{repo}" add "{authorized_file}"')
    if unauthorized_file:
        lines.append(f'echo "oops" >> "{repo}/{unauthorized_file}"')
        lines.append(f'git -C "{repo}" add "{unauthorized_file}"')
    lines.append(f'git -C "{repo}" commit -q -m "slice work"')
    lines.append(write_result_cmd())
    if tail_sleep:
        lines.append(f"sleep {tail_sleep}")
    return "\n".join(lines)


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

    def run_cli_in_repo(self, argv: list[str]) -> tuple[int, str, str]:
        """Like `run_cli`, but with cwd set to `self.repo` for the call's
        duration. Stage 3's non-init commands resolve their repo from the
        controller's cwd (`git_ops.resolve_repo(Path.cwd())`), matching an
        operator running `pm` from inside the working tree."""
        previous = os.getcwd()
        os.chdir(self.repo)
        try:
            return self.run_cli(argv)
        finally:
            os.chdir(previous)

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

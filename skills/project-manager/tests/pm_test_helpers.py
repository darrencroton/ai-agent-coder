"""Shared fixtures for the PM test suite.

Provides `PlanTestCase` (a plain temp directory) and `PmTestCase` (that plus
a temp git repo), a valid-minimal-plan writer (parameterizable per slice), an
in-process CLI runner, a run-creation helper built on `state.create_run`, and
the fake-harness script builders the tmux-gated modules share.

Importing this module also confines every tmux call this suite makes — the
production ones through `pm_lib.sessions` and any the tests issue directly —
to a private tmux server. See `TEST_TMUX_SOCKET`.
"""

from __future__ import annotations

import atexit
import io
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import cli  # noqa: E402
from pm_lib import plan as plan_mod  # noqa: E402
from pm_lib import sessions as sessions_mod  # noqa: E402
from pm_lib import state as state_mod  # noqa: E402

# --- tmux isolation ----------------------------------------------------------
#
# Every tmux-touching test drives a private tmux server, named per test
# process, rather than the developer's or CI runner's default one. Three
# reasons, all of them observed rather than hypothetical:
#
# 1. A session sweep is prefix- or shape-matched over the WHOLE server. A
#    stray match, or a bare `stop --scavenge` sweep, reaches sessions the
#    suite did not create — including the terminal the engineer is running
#    the tests from.
# 2. `TestStartSessionStripsInheritedToken` sets a tmux SERVER-global
#    variable; on a shared server that mutates the caller's environment.
# 3. Two test processes on one server collide by session name, which is what
#    blocked running the suite in parallel.
#
# `-L` (via PM_TMUX_SOCKET) is the mechanism, not TMUX_TMPDIR: tmux ignores
# TMUX_TMPDIR whenever $TMUX is already set, so a suite run from inside tmux
# would silently fall back to the caller's real server.
TEST_TMUX_SOCKET = f"pm-tests-{os.getpid()}"
os.environ["PM_TMUX_SOCKET"] = TEST_TMUX_SOCKET


def tmux_argv(*args: str) -> list[str]:
    """`tmux` argv pinned to this process's private server.

    Built here from the frozen `TEST_TMUX_SOCKET` rather than delegating to
    `sessions.tmux_argv`, which reads the mutable `PM_TMUX_SOCKET` at call
    time. A test that cleared or repointed that variable would otherwise send
    these calls — including the teardown `kill-server` below — to the caller's
    default tmux server. That is not a theoretical failure: an earlier
    isolation attempt here did exactly that (via `TMUX_TMPDIR`, which tmux
    ignores when `$TMUX` is set) and destroyed a live operator's sessions.
    """
    return ["tmux", "-L", TEST_TMUX_SOCKET, *args]


@atexit.register
def _kill_test_tmux_server() -> None:
    """Tear this process's private server down when the process exits.

    Always `-L`-scoped to the frozen socket, never a bare `tmux kill-server`.
    Best effort by construction: tmux may be absent (the tmux-gated tests
    self-skip, but this still runs) or the server may already be gone.
    """
    try:
        subprocess.run(tmux_argv("kill-server"), check=False, capture_output=True)
    except OSError:
        pass

# --- CLI-driving helpers -----------------------------------------------------

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


# --- fake harness script builders --------------------------------------------
#
# Shared by every tmux-gated module. These were duplicated per module, which
# let the same fixture drift into several slightly different versions of
# itself — the trigger-gated variants in particular exist precisely because
# the fixed-delay ones were subtly wrong, and a copy that kept the old shape
# kept the old race.
#
# Fixtures come in two families:
#
# * Fixed-delay, for events that only need to happen "soon" and that nothing
#   is timed against (a harness that commits and writes its result).
# * TRIGGER-GATED, for events a test must place at a specific moment relative
#   to something it is measuring. A launch-relative delay cannot do this:
#   `start-slice` takes several seconds (readiness settle plus two 1s
#   `send_prompt` settles), so the event either races the launch or has to be
#   padded until it is slow as well as racy.
#
# All of them must come up with a pane clear of dialog markers:
# `send_prompt` refuses to inject the launch pointer into a visible
# credential/trust/approval dialog, so a harness that printed a marker
# before injection would fail `start-slice` and never reach the behaviour
# under test.

_READY = "echo FAKE_HARNESS_READY"


def trigger_wait(trigger_path: Path) -> str:
    """Shell that blocks until `trigger_path` exists."""
    return f'while [ ! -f "{trigger_path}" ]; do sleep 0.05; done'


def result_heredoc(status: str = "done", summary: str = "did the work") -> str:
    return (
        'cat > "$PM_RESULT_PATH" <<EOF\n'
        '{"slice": "$PM_SLICE_ID", "status": "' + status + '", "summary": "' + summary + '"}\n'
        "EOF"
    )


def commit_and_result_script(
    repo: Path,
    *,
    authorized_file: str = "a.py",
    unauthorized_file: str | None = None,
    delay: float = 1.0,
    tail_sleep: float = 3.0,
) -> str:
    """Echoes readiness, waits, commits (optionally touching an unauthorized
    file too), writes result.json, then idles briefly before exiting."""
    lines = [
        _READY,
        f"sleep {delay}",
        f'echo "authorized change" >> "{repo}/{authorized_file}"',
        f'git -C "{repo}" add "{authorized_file}"',
    ]
    if unauthorized_file:
        lines.append(f'echo "oops" >> "{repo}/{unauthorized_file}"')
        lines.append(f'git -C "{repo}" add "{unauthorized_file}"')
    lines.append(f'git -C "{repo}" commit -q -m "slice work"')
    lines.append(result_heredoc())
    lines.append(f"sleep {tail_sleep}")
    return "\n".join(lines)


def result_only_script(*, delay: float = 0.5, tail_sleep: float = 3.0) -> str:
    return "\n".join([_READY, f"sleep {delay}", result_heredoc(), f"sleep {tail_sleep}"])


def idle_script(*, sleep_seconds: float = 30.0) -> str:
    return f"{_READY}\nsleep {sleep_seconds}"


def stdin_draining_idle_script() -> str:
    """Actively reads (and echoes) stdin, unlike a bare `sleep`. Injected text
    would otherwise sit unread in the pty's canonical-mode input queue and, if
    it accumulates, silently drop a *later* `send_line` steer — the same reason
    a real coding CLI (which does read stdin) doesn't hit this."""
    return f"{_READY}\nexec cat -"


def trigger_gated_credential_prompt_script(trigger_path: Path) -> str:
    """Comes up clean; reveals a credential prompt once `trigger_path` exists.

    The watcher is backgrounded and `cat -` runs in the foreground, so stdin is
    both drained and echoed: a `send_line` steer shows up in the pane and never
    accumulates unread.
    """
    return (
        f"{_READY}\n"
        f"( {trigger_wait(trigger_path)}; echo 'Enter API key to continue' ) &\n"
        "exec cat -"
    )


def trigger_gated_result_script(trigger_path: Path, *, tail_sleep: float = 10.0) -> str:
    """Writes result.json once `trigger_path` exists, then idles."""
    return (
        f"{_READY}\n"
        "cat - >/dev/null &\n"
        f"{trigger_wait(trigger_path)}\n"
        f"{result_heredoc()}\n"
        f"sleep {tail_sleep}"
    )


def trigger_gated_exit_script(trigger_path: Path) -> str:
    """Stays alive until `trigger_path` exists, then exits, so the session's
    death is timed by the test rather than by a launch-relative clock."""
    return f"{_READY}\ncat - >/dev/null &\n{trigger_wait(trigger_path)}"


def trigger_gated_churn_script(trigger_path: Path) -> str:
    """Holds the pane still until `trigger_path` exists, then changes it every
    0.3s forever. Never writes result.json and never prints a dialog marker.

    The quiet period is load-bearing. Readiness for a non-codex/opencode
    harness is inferred from the pane HOLDING STILL for 1.5s
    (`sessions._wait_stable_pane_ready`), so a harness churning from launch can
    never be detected ready: `wait_until_ready` burns its full 60s deadline and
    gives up — non-fatally, so a test stays green while spending 60s a run on
    nothing. Gating on a trigger rather than a fixed sleep means the quiet
    period cannot silently become too short if the process is descheduled.
    """
    return (
        f"{_READY}\n"
        "cat - >/dev/null &\n"
        f"{trigger_wait(trigger_path)}\n"
        "i=0\n"
        "while true; do\n"
        "  i=$((i+1))\n"
        "  echo tick-$i\n"
        "  sleep 0.3\n"
        "done"
    )


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


class PlanTestCase(unittest.TestCase):
    """A plain temp directory, plus the plan-writing and CLI-running helpers.

    For tests whose `repo` is only ever a filesystem location — plan parsing,
    surface lint, report rendering. `PmTestCase` adds a real git repository on
    top, and that costs roughly 165ms per test (six git subprocesses) against
    2ms here. Paid across a module of pure parser tests it dominates the
    module's entire runtime, so the git fixture belongs only to tests that
    actually read git state.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")

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
        duration. Every command except init/check-plan resolves its repo
        from the controller's cwd (`git_ops.resolve_repo(Path.cwd())`),
        matching an operator running `pm` from inside the working tree."""
        previous = os.getcwd()
        os.chdir(self.repo)
        try:
            return self.run_cli(argv)
        finally:
            os.chdir(previous)


class PmTestCase(PlanTestCase):
    """`PlanTestCase` plus a real temp git repository at `self.repo`.

    Required by anything that reads or writes git state: run creation (state
    lives under the worktree's git dir), the floor's git facts, and every
    tmux-gated lifecycle test.
    """

    def setUp(self) -> None:
        super().setUp()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "pm-test@example.com")
        self._git("config", "user.name", "PM Test")
        self._git("add", "README.md")
        self._git("commit", "-q", "-m", "initial commit")

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            capture_output=True,
        )

    def _wait_for(self, predicate, timeout: float = 15.0, interval: float = 0.3) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

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
            policy=policy if policy is not None else {"max_attempts": 3},
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
        """Set `state['current_slice']` and persist it.

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
        """Record a human approval for `slice_id` and persist it."""
        updated = dict(state)
        approvals = dict(updated.get("approvals") or {})
        approvals[slice_id] = {"at": "2026-01-01T00:00:00Z", "reason": reason}
        updated["approvals"] = approvals
        state_mod.save_state(run_dir, updated, token)
        return state_mod.load_state(run_dir, token)


class TmuxRunTestCase(PmTestCase):
    """Base for the tmux-gated lifecycle modules: a feature-branch repo, a
    session reaper, and the `init` / wait helpers those tests all need.

    Subclasses add their own cleanups (a reviewer-subprocess reaper, say) on
    top of `setUp`; they must not re-implement what is here, which is how these
    helpers previously drifted into several near-copies of themselves.
    """

    def setUp(self) -> None:
        super().setUp()
        # Operate on a dedicated feature branch, as a real run does — the
        # implicit-current-branch init path refuses main/master.
        self._git("checkout", "-q", "-b", "pm-work")
        self._sessions_to_reap: list[str] = []
        self.addCleanup(self._reap_sessions)

    def _reap_sessions(self) -> None:
        for name in self._sessions_to_reap:
            sessions_mod.force_stop(name)

    def _track_current_session(self, run_id: str, token: str) -> str | None:
        """Register the run's live session for cleanup and return its name."""
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        state = state_mod.load_state(run_dir, token)
        current = state.get("current_slice") or {}
        session = current.get("tmux_session")
        if session:
            self._sessions_to_reap.append(session)
        return session

    def _wait_for_result(self, run_id: str, token: str, *, timeout: float = 15.0) -> bool:
        run_dir = state_mod.resolve_run_dir(self.repo, run_id)
        state = state_mod.load_state(run_dir, token)
        artifact_dir = Path(state["current_slice"]["artifact_dir"])
        return self._wait_for(lambda: (artifact_dir / "result.json").is_file(), timeout=timeout)

    def _plan_path(self) -> Path:
        # Deliberately outside self.repo: an untracked plan.md inside the
        # worktree is itself an unclean worktree entry, tripping init's
        # clean-worktree preflight (and the floor's surface/cleanliness facts)
        # for reasons unrelated to the behaviour under test. The real CLI takes
        # an arbitrary --plan path, so this is faithful, not merely convenient.
        return self.repo.parent / "plan.md"

    def _init(self, plan_path: Path, harness_script: Path, *, extra: list[str] | None = None) -> tuple[int, str, str]:
        argv = [
            "init",
            "--repo", str(self.repo),
            "--plan", str(plan_path),
            "--harness", "fake",
            "--harness-command", str(harness_script),
        ]
        if extra:
            argv += extra
        return self.run_cli_in_repo(argv)

"""Protected behaviours: the campaign child commands.

`launch-child`, `wait-child`, `answer-child` and `stop-child` drive a child
PM in its own git worktree and in a tmux session on a private socket. The
child here is a fake shell harness passed through `--harness-command`; no
real coding CLI is ever launched.

Input refusals, manifest errors, names and the pointer template need no
tmux (`sessions.kill_server` itself is tested in test_sessions). A refusal
checked after the tmux
precondition, and everything that drives a real pane or server, is gated on
tmux and runs its children on a dedicated socket
(`pm-tests-<pid>-child`, or a launch's own default socket) and kills that
server in cleanup through `sessions.kill_server`, so no per-session reaping
is needed. Worktrees, prompt files and the fake's records all sit under
`self.repo.parent`, outside the repository under test.

A sandboxed launch (`--sandbox`) refuses its inputs without `srt`; the tests
that start a sandboxed server need `srt`, `tmux` and macOS (`requires_srt`)
and each runs its child on a `<TEST_TMUX_SOCKET>-sb*` socket of its own,
torn down in cleanup through `sandbox.stop_server` when a record exists.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Imported for its side effect as much as its helpers: pm_test_helpers pins
# PM_TMUX_SOCKET, which every command here must leave exactly as it found it.
from pm_test_helpers import (
    SANDBOX_TEST_SOCKET_PREFIX,
    TEST_TMUX_SOCKET,
    PmTestCase,
    make_linked_worktree,
    minimal_base_policy,
    register_sandbox_socket,
    requires_srt,
    sandbox_probe_child_script,
    trigger_wait,
    write_base_policy,
    write_fake_harness,
)

from pm_lib import PmError
from pm_lib import campaign
from pm_lib import cli
from pm_lib import git_ops
from pm_lib import prompts
from pm_lib import sandbox
from pm_lib import sessions
from pm_lib import slice_ops
from pm_lib import state as state_mod

_HAS_TMUX = shutil.which("tmux") is not None
CHILD_SOCKET = f"{TEST_TMUX_SOCKET}-child"
_QUESTION_TEXT = "Which node order?\nA: data first\nB: model first\nRecommendation: A"
_RESULT_JSON = '{"status": "complete", "run_id": null, "summary": "fake child finished"}'


def _alive(pid: int) -> bool:
    """Whether `pid` is still executing: gone, or a zombie awaiting its reaper, counts as dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], text=True, capture_output=True, check=False).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


def _child_tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-L", socket, *args], check=False, text=True, capture_output=True)


class CampaignTestCase(PmTestCase):
    """A repository with two commits, a launcher prompt file, a records
    directory, and the launch-child argv builder."""

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "README.md").write_text("hello again\n", encoding="utf-8")
        self._git("commit", "-q", "-am", "second commit")
        self.base_commit = self._git("rev-parse", "HEAD~1").stdout.strip()
        self.prompt_file = self.repo.parent / "launcher-src.md"
        self.prompt_file.write_bytes("# Launcher\n\nRun the node plan.\r\nNon-ASCII: é\n".encode("utf-8"))
        self.records = self.repo.parent / "records"
        self.records.mkdir()
        self.worktree = self.repo.parent / "node-wt"
        self.addCleanup(sessions.kill_server, CHILD_SOCKET)

    def launch_argv(self, harness: Path, *, branch: str = "node-1", socket: str | None = CHILD_SOCKET) -> list[str]:
        argv = [
            "launch-child",
            "--repo",
            str(self.repo),
            "--base",
            "HEAD~1",
            "--branch",
            branch,
            "--worktree",
            str(self.worktree),
            "--prompt-file",
            str(self.prompt_file),
            "--harness",
            "fake",
            "--harness-command",
            str(harness),
        ]
        if socket is not None:
            argv += ["--socket", socket]
        return argv

    def branches(self) -> str:
        return self._git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").stdout

    def worktrees(self) -> str:
        return self._git("worktree", "list", "--porcelain").stdout

    def assert_socket_untouched(self) -> None:
        self.assertEqual(os.environ.get("PM_TMUX_SOCKET"), TEST_TMUX_SOCKET)

    def _write_child_runs(self) -> subprocess.Popen:
        """A readable run recording a live reviewer process group, and an unreadable one."""
        reviewer = subprocess.Popen(["sleep", "120"], start_new_session=True)
        self.addCleanup(reviewer.wait)
        self.addCleanup(reviewer.kill)
        root = state_mod.state_root(self.worktree)
        (root / "run-good").mkdir(parents=True)
        (root / "run-good" / "run.json").write_text(
            json.dumps(
                {
                    "status": "stopped",
                    "stop_reason": "budget",
                    "current_slice": {"id": "Slice 1", "reviewer_pids": [reviewer.pid]},
                }
            ),
            encoding="utf-8",
        )
        (root / "run-bad").mkdir()
        (root / "run-bad" / "run.json").write_text("{torn", encoding="utf-8")
        return reviewer


# --- pure helpers: no tmux, no git -------------------------------------------


class TestChildNames(unittest.TestCase):
    def test_a_branch_with_slash_and_dot_yields_names_with_neither(self) -> None:
        session, socket = campaign.child_names("feat/x.y:z", "0a1b2c")
        self.assertEqual(session, "campaign-feat-x-y-z-0a1b2c")
        self.assertEqual(socket, "pm-child-feat-x-y-z-0a1b2c")
        for name in (session, socket):
            self.assertNotIn("/", name)
            self.assertNotIn(".", name)
            self.assertNotIn(":", name)

    def test_an_explicit_socket_is_used_verbatim_and_the_session_is_never_pm(self) -> None:
        session, socket = campaign.child_names("pm-x", "abcdef", "my-socket")
        self.assertEqual(socket, "my-socket")
        self.assertTrue(session.startswith("campaign-"))

    def test_a_long_branch_is_cut_to_32_characters_and_both_names_share_the_suffix(self) -> None:
        session, socket = campaign.child_names("campaign/" + "n" * 80, "0a1b2c")
        component = "campaign-" + "n" * 23
        self.assertEqual(session, f"campaign-{component}-0a1b2c")
        self.assertEqual(socket, f"pm-child-{component}-0a1b2c")

    def test_socket_name_rule(self) -> None:
        for bad in ("", "   ", "a b", "a\tb", "a/b", "default", "a" * 65):
            with self.subTest(socket=bad), self.assertRaises(PmError):
                campaign.check_socket_name(bad, what="--socket")
        for good in ("pm-child-ok_1", "a" * 64, "default-2"):
            campaign.check_socket_name(good, what="--socket")


class TestChildLaunchPointer(unittest.TestCase):
    def test_the_default_pointer_is_one_line_naming_the_launcher(self) -> None:
        launcher = Path("/tmp/wt/.pm/campaign/launcher.md")
        pointer = prompts.render_child_launch_pointer(launcher)
        self.assertEqual(
            pointer,
            "You are a Project Manager launched by a Campaign Manager. Read your launcher at "
            f"{launcher} now, in full, before doing anything else, then follow it exactly.",
        )
        self.assertNotIn("\n", pointer)

    def test_the_pointer_is_sourced_from_the_child_launch_pointer_section(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "developer-prompt.md"
        path.write_text(
            "# Template\n\n```md\nmain\n```\n\n## Launch Pointer\n\n```md\nwrong {prompt_path}\n```\n\n"
            "## Child Launch Pointer\n\n```md\nCHILD {launcher_path}\n```\n",
            encoding="utf-8",
        )
        pointer = prompts.render_child_launch_pointer(Path("/x/launcher.md"), reference_path=path)
        self.assertEqual(pointer, "CHILD /x/launcher.md")


class TestCampaignParsers(unittest.TestCase):
    _EXPECTED_FLAGS = {
        "launch-child": {
            "--repo",
            "--base",
            "--branch",
            "--worktree",
            "--prompt-file",
            "--harness",
            "--model",
            "--effort",
            "--socket",
            "--harness-command",
            "--setup",
            "--sandbox",
        },
        "wait-child": {"--worktree", "--wait", "--seen-marker"},
        "answer-child": {"--worktree", "--text", "--redeliver"},
        "stop-child": {"--worktree", "--reason"},
    }

    def test_each_campaign_command_takes_exactly_its_flags_and_never_run_or_token(self) -> None:
        subparsers = next(
            action for action in cli.build_parser()._actions if isinstance(action, argparse._SubParsersAction)
        )
        for name, expected in self._EXPECTED_FLAGS.items():
            with self.subTest(command=name):
                flags = {
                    option
                    for action in subparsers.choices[name]._actions
                    for option in action.option_strings
                    if option not in ("-h", "--help")
                }
                self.assertEqual(flags, expected)
                self.assertNotIn("--run", flags)
                self.assertNotIn("--token", flags)


# --- refusals and manifest errors: no tmux needed ----------------------------


class TestLaunchChildPreconditions(CampaignTestCase):
    """Each refusal exits 2 with `pm: error:` and creates no branch, no
    worktree and no path, leaving anything pre-existing untouched."""

    def setUp(self) -> None:
        super().setUp()
        self.harness = write_fake_harness(self.repo.parent / "fake-child.sh", "echo FAKE_HARNESS_READY\nsleep 30")

    def assert_refused(self, argv: list[str], fragment: str) -> str:
        branches, worktrees = self.branches(), self.worktrees()
        code, out, err = self.run_cli(argv)
        self.assertEqual(code, 2, out + err)
        self.assertIn("pm: error:", err)
        self.assertIn(fragment, err)
        self.assertEqual(self.branches(), branches)
        self.assertEqual(self.worktrees(), worktrees)
        self.assert_socket_untouched()
        return err

    def test_an_existing_branch_is_refused_and_left_where_it_was(self) -> None:
        self._git("branch", "node-1", "HEAD")
        self.assert_refused(self.launch_argv(self.harness), "already exists")
        self.assertFalse(self.worktree.exists())

    def test_an_existing_path_is_refused_and_left_untouched(self) -> None:
        self.worktree.mkdir()
        (self.worktree / "keep.txt").write_text("mine\n", encoding="utf-8")
        self.assert_refused(self.launch_argv(self.harness), "already exists")
        self.assertEqual(os.listdir(self.worktree), ["keep.txt"])
        self.assertEqual((self.worktree / "keep.txt").read_text(encoding="utf-8"), "mine\n")

    def test_a_base_that_tracks_handoff_is_refused(self) -> None:
        (self.repo / "HANDOFF.md").write_text("# Handoff\n", encoding="utf-8")
        self._git("add", "HANDOFF.md")
        self._git("commit", "-q", "-m", "track a handoff")
        argv = self.launch_argv(self.harness)
        argv[argv.index("--base") + 1] = "HEAD"
        self.assert_refused(argv, "the base commit tracks HANDOFF.md")
        self.assertFalse(self.worktree.exists())

    def test_an_unresolvable_base_is_refused(self) -> None:
        argv = self.launch_argv(self.harness)
        argv[argv.index("--base") + 1] = "no-such-ref"
        self.assert_refused(argv, "does not resolve to a commit")
        self.assertFalse(self.worktree.exists())

    def test_an_unresolvable_repo_is_refused(self) -> None:
        argv = self.launch_argv(self.harness)
        argv[argv.index("--repo") + 1] = str(self.repo.parent / "missing-repo")
        self.assert_refused(argv, "does not exist")
        self.assertFalse(self.worktree.exists())

    def test_a_blank_or_unreadable_prompt_file_is_refused(self) -> None:
        self.prompt_file.write_text("  \n\t\n", encoding="utf-8")
        self.assert_refused(self.launch_argv(self.harness), "blank")
        self.prompt_file.unlink()
        self.assert_refused(self.launch_argv(self.harness), "could not be read")
        self.assertFalse(self.worktree.exists())

    def test_a_bad_explicit_socket_is_refused(self) -> None:
        for socket, fragment in (
            ("a/b", "'/'"),
            ("a b", "whitespace"),
            ("", "non-blank"),
            ("  ", "non-blank"),
            ("default", "must not be 'default'"),
            ("a" * 65, "at most 64 characters"),
        ):
            with self.subTest(socket=socket):
                self.assert_refused(self.launch_argv(self.harness, socket=socket), fragment)
                self.assertFalse(self.worktree.exists())

    def test_an_unsupported_harness_without_a_command_is_refused(self) -> None:
        argv = self.launch_argv(self.harness)
        del argv[argv.index("--harness-command") : argv.index("--harness-command") + 2]
        self.assert_refused(argv, "not supported")
        self.assertFalse(self.worktree.exists())

    @unittest.skipUnless(_HAS_TMUX, "the tmux precondition is checked first")
    def test_an_executable_not_on_path_is_refused(self) -> None:
        argv = self.launch_argv(self.harness)
        argv[argv.index("--harness-command") + 1] = str(self.repo.parent / "no-such-harness")
        self.assert_refused(argv, "not found on PATH")
        self.assertFalse(self.worktree.exists())

    @unittest.skipUnless(_HAS_TMUX, "the tmux precondition is checked first")
    def test_an_explicitly_blank_harness_command_is_refused_not_replaced(self) -> None:
        # `--harness fake` on purpose: were a blank override ever treated as
        # omitted again, this refuses as an unsupported harness rather than
        # composing — and launching — a real CLI that may be on PATH.
        argv = self.launch_argv(self.harness)
        for blank in ("", "   "):
            with self.subTest(command=blank):
                argv[argv.index("--harness-command") + 1] = blank
                self.assert_refused(argv, "harness command is blank")
                self.assertFalse(self.worktree.exists())

    def test_a_dangling_symlink_at_the_worktree_path_is_refused_and_left_unchanged(self) -> None:
        target = self.repo.parent / "missing-target"
        self.worktree.symlink_to(target)
        self.assert_refused(self.launch_argv(self.harness), "already exists")
        self.assertTrue(self.worktree.is_symlink())
        self.assertEqual(os.readlink(self.worktree), str(target))
        self.assertFalse(target.exists())

    def test_a_composed_command_whose_executable_is_not_on_path_is_refused(self) -> None:
        # PATH holds only git and a stand-in tmux, which the earlier
        # preconditions need, so the composed codex command cannot find its
        # executable.
        bin_dir = self.repo.parent / "bin-git-only"
        bin_dir.mkdir()
        (bin_dir / "git").symlink_to(shutil.which("git"))
        write_fake_harness(bin_dir / "tmux", "exit 1")
        argv = self.launch_argv(self.harness)
        del argv[argv.index("--harness-command") : argv.index("--harness-command") + 2]
        argv[argv.index("--harness") + 1] = "codex"
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            self.assert_refused(argv, "harness executable not found on PATH: codex")
        self.assertFalse(self.worktree.exists())

    @unittest.skipUnless(_HAS_TMUX, "tmux is required to start a server on the socket")
    def test_a_socket_whose_server_hosts_a_session_is_refused_and_the_session_survives(self) -> None:
        socket = f"{TEST_TMUX_SOCKET}-occupied"
        self.addCleanup(sessions.kill_server, socket)
        started = _child_tmux(socket, "new-session", "-d", "-s", "bystander", "sleep 120")
        self.assertEqual(started.returncode, 0, started.stderr)
        err = self.assert_refused(self.launch_argv(self.harness, socket=socket), "a child needs a private server")
        self.assertIn(socket, err)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(_child_tmux(socket, "has-session", "-t", "bystander").returncode, 0)

    def test_tmux_absent_is_refused_before_anything_is_created(self) -> None:
        real_which = shutil.which

        def which(name: str, *args, **kwargs) -> str | None:
            return None if name == "tmux" else real_which(name, *args, **kwargs)

        with mock.patch.object(campaign.shutil, "which", side_effect=which):
            self.assert_refused(self.launch_argv(self.harness), "tmux is required to launch a child; install it")
        self.assertFalse(self.worktree.exists())

    @unittest.skipUnless(_HAS_TMUX, "the tmux precondition is checked first")
    def test_a_socket_whose_server_cannot_be_listed_is_refused(self) -> None:
        error = PmError(f"could not list sessions on tmux socket {CHILD_SOCKET!r}: File name too long")
        with mock.patch.object(campaign.sessions, "list_sessions_on", side_effect=error):
            self.assert_refused(self.launch_argv(self.harness), "File name too long")
        self.assertFalse(self.worktree.exists())

    @unittest.skipUnless(_HAS_TMUX, "the tmux precondition is checked first")
    def test_a_failed_worktree_add_reports_what_git_left_behind(self) -> None:
        real_git = campaign.git_ops.git

        def git(repo: Path, *args: str) -> str:
            if "worktree" not in args:
                return real_git(repo, *args)
            # A partial failure: the branch and the directory exist, then git fails.
            real_git(repo, "branch", "node-1", self.base_commit)
            self.worktree.mkdir()
            raise PmError("simulated worktree failure")

        with mock.patch.object(campaign.git_ops, "git", side_effect=git):
            code, out, err = self.run_cli(self.launch_argv(self.harness))
        self.assertEqual(code, 2, out + err)
        worktree = self.worktree.resolve()
        self.assertIn(
            "git worktree add failed: simulated worktree failure; left behind: branch node-1 exists "
            f"(git branch -D node-1); path {worktree} exists (git worktree remove {worktree}, or remove it by hand)",
            err,
        )

    @unittest.skipUnless(_HAS_TMUX, "the tmux precondition is checked first")
    def test_a_failure_after_the_worktree_exists_names_the_branch_and_worktree(self) -> None:
        failure = PmError("tmux start failed: boom")
        with mock.patch.object(campaign.sessions, "start_session", side_effect=failure):
            code, out, err = self.run_cli(self.launch_argv(self.harness))
        self.assertEqual(code, 2, out + err)
        self.assertIn("pm: error:", err)
        self.assertIn("boom", err)
        self.assertIn("branch node-1", err)
        self.assertIn(str(self.worktree.resolve()), err)
        self.assertTrue(self.worktree.is_dir())
        self.assertIn("refs/heads/node-1", self.branches())
        self.assertFalse(campaign.manifest_path(self.worktree).exists(), "no session, so no manifest")
        self.assert_socket_untouched()

    def test_a_blank_or_unreadable_setup_script_is_refused_before_anything_is_created(self) -> None:
        blank = self.repo.parent / "setup-blank.sh"
        blank.write_text("\n", encoding="utf-8")
        self.assert_refused(self.launch_argv(self.harness) + ["--setup", str(blank)], "setup script is blank")
        self.assert_refused(
            self.launch_argv(self.harness) + ["--setup", str(self.repo.parent / "absent.sh")],
            "setup script could not be read",
        )

    def _commit_ignoring(self, *patterns: str) -> None:
        (self.repo / ".gitignore").write_text("".join(f"{p}\n" for p in patterns), encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-q", "-m", "ignore scaffolding")

    def _setup_argv(self, script: Path) -> list[str]:
        argv = self.launch_argv(self.harness) + ["--setup", str(script)]
        argv[argv.index("--base") + 1] = "HEAD"
        return argv

    def test_a_setup_script_runs_in_the_new_worktree_before_the_session_starts(self) -> None:
        self._commit_ignoring("setup-cwd.txt", "link.md")
        script = self.repo.parent / "setup.sh"
        script.write_text(
            'pwd -P > setup-cwd.txt\necho scaffolded\nln -s "$PWD/README.md" link.md\n'
            'if [ -n "${PM_RUN_TOKEN+x}" ]; then echo token-leaked >&2; fi\n'
            'if [ -n "${CLAUDE_CODE_MESSAGING_TOKEN+x}" ]; then echo messaging-token-leaked >&2; fi\n'
            'echo "bedrock=${CLAUDE_CODE_USE_BEDROCK-unset}"\n',
            encoding="utf-8",
        )
        caller = {
            "PM_RUN_TOKEN": "caller-token-must-not-leak",
            "CLAUDE_CODE_MESSAGING_TOKEN": "messaging-token-must-not-leak",
            "CLAUDE_CODE_USE_BEDROCK": "1",
        }
        with (
            mock.patch.dict(os.environ, caller),
            mock.patch.object(campaign, "_start_child_session") as start,
        ):
            code, out, err = self.run_cli(self._setup_argv(script))
        self.assertEqual(code, 0, out + err)
        worktree = self.worktree.resolve()
        start.assert_called_once()
        self.assertEqual((worktree / "setup-cwd.txt").read_text(encoding="utf-8").strip(), str(worktree))
        self.assertTrue((worktree / "link.md").is_symlink())
        log = (campaign.campaign_dir(worktree) / "setup.log").read_text(encoding="utf-8")
        self.assertIn("scaffolded", log)
        self.assertNotIn("token-leaked", log)
        self.assertNotIn("messaging-token-leaked", log)
        self.assertIn("bedrock=1", log)

    def test_a_setup_script_that_dirties_the_worktree_fails_the_launch(self) -> None:
        script = self.repo.parent / "setup-dirty.sh"
        script.write_text("echo stray > stray.txt\n", encoding="utf-8")
        with mock.patch.object(campaign.sessions, "start_session") as start:
            code, out, err = self.run_cli(self._setup_argv(script))
        self.assertEqual(code, 2, out + err)
        start.assert_not_called()
        self.assertIn("left the worktree dirty outside .pm/", err)
        self.assertIn("stray.txt", err)
        self.assertIn(f"branch node-1 and worktree {self.worktree.resolve()} are left behind", err)

    def test_a_hung_setup_script_is_killed_with_everything_it_started(self) -> None:
        pid_file = self.repo.parent / "sleeper.pid"
        script = self.repo.parent / "setup-hang.sh"
        script.write_text(f'sleep 300 &\necho $! > "{pid_file}"\nwait\n', encoding="utf-8")
        with (
            mock.patch.object(campaign, "SETUP_TIMEOUT_SECONDS", 1),
            mock.patch.object(campaign.sessions, "start_session") as start,
        ):
            code, out, err = self.run_cli(self._setup_argv(script))
        self.assertEqual(code, 2, out + err)
        start.assert_not_called()
        self.assertIn("did not finish in 1s", err)
        self.assertIn("process group was killed", err)
        sleeper = int(pid_file.read_text(encoding="utf-8").strip())
        self.assertTrue(self._wait_for(lambda: not _alive(sleeper)), "the script's own child must be dead too")
        self.assertTrue((campaign.campaign_dir(self.worktree.resolve()) / "setup.log").is_file())

    def test_an_interrupted_setup_wait_kills_the_script_group_and_re_raises(self) -> None:
        pid_file = self.repo.parent / "sleeper.pid"
        script = self.repo.parent / "setup-hang.sh"
        script.write_text(f'sleep 300 &\necho $! > "{pid_file}"\nwait\n', encoding="utf-8")

        def interrupt(process: subprocess.Popen) -> int:
            # The shell creates the file before `echo` fills it: wait for a pid, not a path.
            self.assertTrue(self._wait_for(lambda: pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()))
            raise KeyboardInterrupt

        with (
            mock.patch.object(campaign, "_wait_for_setup", side_effect=interrupt),
            mock.patch.object(campaign.sessions, "start_session") as start,
            self.assertRaises(KeyboardInterrupt),
        ):
            campaign.launch_child(
                repo=self.repo,
                base="HEAD",
                branch="node-1",
                worktree=self.worktree,
                prompt_file=self.prompt_file,
                harness="fake",
                harness_command=str(self.harness),
                socket=CHILD_SOCKET,
                setup=script,
            )
        start.assert_not_called()
        sleeper = int(pid_file.read_text(encoding="utf-8").strip())
        self.assertTrue(self._wait_for(lambda: not _alive(sleeper)), "the interrupted script's child must be dead")

    def test_a_failing_setup_script_fails_the_launch_before_any_session_and_names_the_leftovers(self) -> None:
        script = self.repo.parent / "setup-fail.sh"
        script.write_text("echo partial > partial.txt\necho 'no data volume' >&2\nexit 3\n", encoding="utf-8")
        with mock.patch.object(campaign.sessions, "start_session") as start:
            code, out, err = self.run_cli(self._setup_argv(script))
        self.assertEqual(code, 2, out + err)
        start.assert_not_called()
        self.assertIn("worktree setup", err)
        self.assertIn("exited 3", err)
        self.assertIn("no data volume", err)
        self.assertIn(f"branch node-1 and worktree {self.worktree.resolve()} are left behind", err)
        self.assertTrue((self.worktree / "partial.txt").is_file())
        self.assertIn("no data volume", (campaign.campaign_dir(self.worktree.resolve()) / "setup.log").read_text())
        self.assertFalse(campaign.manifest_path(self.worktree).exists(), "no session, so no manifest")
        self.assert_socket_untouched()

    @unittest.skipUnless(_HAS_TMUX, "the tmux precondition is checked first")
    def test_an_ignore_rule_overriding_the_handoff_exclusion_fails_setup_naming_the_leftovers(self) -> None:
        (self.repo / ".gitignore").write_text("!HANDOFF.md\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-q", "-m", "un-ignore handoffs")
        argv = self.launch_argv(self.harness)
        argv[argv.index("--base") + 1] = "HEAD"
        code, out, err = self.run_cli(argv)
        self.assertEqual(code, 2, out + err)
        self.assertIn("HANDOFF.md is not ignored", err)
        self.assertIn(f"branch node-1 and worktree {self.worktree.resolve()} are left behind", err)


class TestManifestErrors(CampaignTestCase):
    def test_missing_or_unreadable_child_json_exits_2_naming_the_file(self) -> None:
        self.worktree.mkdir()
        manifest = campaign.manifest_path(self.worktree.resolve())
        commands = (
            ["wait-child", "--worktree", str(self.worktree)],
            ["answer-child", "--worktree", str(self.worktree), "--text", "A"],
            ["stop-child", "--worktree", str(self.worktree), "--reason", "test"],
        )
        # Every field present, so the socket rule is what refuses the last one.
        slashed = {key: "x" for key in campaign._MANIFEST_FIELDS} | {"socket": "a/b"}
        cases = ((None, ""), ("{not json", ""), ('["a list"]', ""), (json.dumps(slashed), "'/'"))
        for content, fragment in cases:
            if content is not None:
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(content, encoding="utf-8")
            for argv in commands:
                with self.subTest(content=content, command=argv[0]):
                    code, out, err = self.run_cli(argv)
                    self.assertEqual(code, 2, out + err)
                    self.assertIn("pm: error:", err)
                    self.assertIn(str(manifest), err)
                    self.assertIn(fragment, err)
                    self.assert_socket_untouched()

    def _write_manifest(self, session: str) -> None:
        directory = campaign.campaign_dir(self.worktree)
        directory.mkdir(parents=True)
        manifest = {
            "socket": CHILD_SOCKET,
            "session": session,
            "branch": "node-1",
            "base": self.base_commit,
            "worktree": str(self.worktree),
            "launcher": str(directory / "launcher.md"),
            "launched_at": "2026-10-23T00:00:00Z",
        }
        (directory / "child.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_wait_child_exits_0_with_no_session_and_no_state_root_and_restores_an_unset_socket(self) -> None:
        self.worktree.mkdir()
        self._write_manifest("campaign-node-1-000000")
        previous = os.environ.pop("PM_TMUX_SOCKET")
        try:
            # The ceiling keeps git from finding a repository enclosing the
            # temp directory (TMPDIR may itself sit inside one).
            with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.repo.parent)}):
                code, out, err = self.run_cli(["wait-child", "--worktree", str(self.worktree), "--wait", "5"])
            self.assertNotIn("PM_TMUX_SOCKET", os.environ)
        finally:
            os.environ["PM_TMUX_SOCKET"] = previous
        self.assertEqual(code, 0, err)
        self.assertIn("session running: False", out)
        self.assertIn("child runs: state root unavailable", out)
        self.assertIn("--- pane tail ---", out)

    def test_a_path_inside_another_repository_never_reads_that_repositorys_runs(self) -> None:
        # A plain directory inside the repository under test: git would
        # resolve it to the repository, whose state root holds a real run.
        self.make_run(plan_path=self.write_plan(self.repo.parent / "plan.md"))
        self.worktree = self.repo / "inner"
        self.worktree.mkdir()
        self._write_manifest("campaign-node-1-000000")
        code, out, err = self.run_cli(["wait-child", "--worktree", str(self.worktree)])
        self.assertEqual(code, 0, err)
        self.assertIn("is not the root of a git worktree", out)
        self.assertNotIn("child run ", out)

    @unittest.skipUnless(_HAS_TMUX, "tmux is required to host a bystander session")
    def test_stop_child_refuses_a_server_hosting_a_bystander_and_kills_nothing(self) -> None:
        self.worktree.mkdir()
        self._write_manifest("campaign-node-1-000000")
        started = _child_tmux(CHILD_SOCKET, "new-session", "-d", "-s", "bystander", "sleep 120")
        self.assertEqual(started.returncode, 0, started.stderr)
        # The right mark, so the bystander rule is what refuses.
        sessions.set_server_environment(CHILD_SOCKET, "PM_CHILD_WORKTREE", str(self.worktree.resolve()))
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.worktree), "--reason", "test"])
        self.assertEqual(code, 2, out + err)
        self.assertIn(
            f"tmux server {CHILD_SOCKET} hosts sessions this child does not own: bystander; "
            "refusing to kill the server",
            err,
        )
        self.assertEqual(_child_tmux(CHILD_SOCKET, "has-session", "-t", "bystander").returncode, 0)
        self.assertFalse((campaign.campaign_dir(self.worktree) / "pane.txt").exists())
        self.assert_socket_untouched()

    @unittest.skipUnless(_HAS_TMUX, "tmux is required to host another child's server")
    def test_stop_child_refuses_a_server_launched_for_another_worktree_and_kills_nothing(self) -> None:
        self.worktree.mkdir()
        self._write_manifest("campaign-node-1-000000")
        started = _child_tmux(CHILD_SOCKET, "new-session", "-d", "-s", "campaign-other-000000", "sleep 120")
        self.assertEqual(started.returncode, 0, started.stderr)
        other = str(self.repo.parent / "other-wt")
        sessions.set_server_environment(CHILD_SOCKET, "PM_CHILD_WORKTREE", other)
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.worktree), "--reason", "test"])
        self.assertEqual(code, 2, out + err)
        self.assertIn(
            f"tmux server {CHILD_SOCKET} was not launched for {self.worktree.resolve()} "
            f"(it carries PM_CHILD_WORKTREE={other}); refusing to kill the server",
            err,
        )
        self.assertEqual(_child_tmux(CHILD_SOCKET, "has-session", "-t", "campaign-other-000000").returncode, 0)
        self.assertFalse((campaign.campaign_dir(self.worktree) / "pane.txt").exists())
        self.assert_socket_untouched()

    @unittest.skipUnless(_HAS_TMUX, "tmux is required to host the marked server")
    def test_stop_child_keeps_the_reviewer_groups_when_the_post_kill_probe_fails(self) -> None:
        # The repository is the worktree here: only a git worktree has a state root to read runs from.
        self.worktree = self.repo
        self._write_manifest("campaign-node-1-000000")
        reviewer = self._write_child_runs()
        started = _child_tmux(CHILD_SOCKET, "new-session", "-d", "-s", "campaign-node-1-000000", "sleep 120")
        self.assertEqual(started.returncode, 0, started.stderr)
        sessions.set_server_environment(CHILD_SOCKET, "PM_CHILD_WORKTREE", str(self.worktree.resolve()))
        with mock.patch.object(campaign.sessions, "kill_server", side_effect=PmError("probe failed")):
            code, out, err = self.run_cli(["stop-child", "--worktree", str(self.worktree), "--reason", "test"])
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"reviewer process groups the child recorded: {reviewer.pid};", out)
        self.assertIn(f"tmux server {CHILD_SOCKET} could not be confirmed killed (probe failed)", out)
        self.assertNotIn("killed tmux server", out)

    @unittest.skipUnless(_HAS_TMUX, "tmux is required for a pane to capture")
    def test_wait_child_reports_a_pane_live_file_that_could_not_be_written(self) -> None:
        self.worktree.mkdir()
        self._write_manifest("campaign-node-1-000000")
        started = _child_tmux(CHILD_SOCKET, "new-session", "-d", "-s", "campaign-node-1-000000", "sleep 120")
        self.assertEqual(started.returncode, 0, started.stderr)
        # A directory where the file goes: the write raises IsADirectoryError.
        (campaign.campaign_dir(self.worktree) / "pane-live.txt").mkdir()
        code, out, err = self.run_cli(["wait-child", "--worktree", str(self.worktree)])
        self.assertEqual(code, 0, out + err)
        self.assertIn("pane-live: not written (", out)

    @unittest.skipUnless(_HAS_TMUX, "stop-child lists the manifest's server first")
    def test_stop_child_reports_a_pane_capture_that_could_not_be_written(self) -> None:
        self.worktree.mkdir()
        self._write_manifest("campaign-node-1-000000")
        with mock.patch.object(campaign.sessions, "capture_text", side_effect=OSError("disk full")):
            code, out, err = self.run_cli(["stop-child", "--worktree", str(self.worktree), "--reason", "test"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("pane: not captured (disk full)", out)
        self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)
        # A link planted at pane.txt: the dead session's presence check is
        # refused, reported as the capture's error, and the stop completes.
        victim = self.repo.parent / "victim.txt"
        victim.write_text("operator\n", encoding="utf-8")
        (campaign.campaign_dir(self.worktree) / "pane.txt").symlink_to(victim)
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.worktree), "--reason", "again"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("pane: not captured (refused ", out)
        self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)
        self.assertEqual(victim.read_text(encoding="utf-8"), "operator\n")

    def test_answer_child_refuses_with_no_live_session_leaving_the_files_unchanged(self) -> None:
        self.worktree.mkdir()
        self._write_manifest("campaign-node-1-000000")
        question = campaign.campaign_dir(self.worktree) / "cm-question.md"
        question.write_text("which?\n", encoding="utf-8")
        before = sorted(os.listdir(question.parent))
        code, out, err = self.run_cli(["answer-child", "--worktree", str(self.worktree), "--text", "A"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("no live child session", err)
        self.assertEqual(sorted(os.listdir(question.parent)), before)
        self.assert_socket_untouched()


class TestExcludeHandoff(PmTestCase):
    def test_handoff_is_listed_once_however_often_the_helper_runs(self) -> None:
        # The main worktree: git names its exclude file by a relative path.
        campaign._exclude_handoff(self.repo)
        campaign._exclude_handoff(self.repo)
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        # Anchored: only the root HANDOFF.md is hidden from the floor.
        self.assertEqual(exclude.splitlines().count("/HANDOFF.md"), 1)
        self.assertNotIn("HANDOFF.md", exclude.splitlines())


# --- live children: tmux required ---------------------------------------------


@unittest.skipUnless(_HAS_TMUX, "tmux is required to drive a real child session")
class TestChildLifecycle(CampaignTestCase):
    """One fake child from launch to stop: it records its cwd, socket, token
    presence and every input line; asks one question once triggered; and
    writes its completion file once the answer exists, then idles."""

    def _fake_child(self, trigger: Path) -> Path:
        records = self.records
        body = "\n".join(
            [
                "echo FAKE_HARNESS_READY",
                f'pwd -P > "{records}/cwd"',
                f'printf \'%s\' "$PM_TMUX_SOCKET" > "{records}/socket"',
                f'if [ -n "${{PM_RUN_TOKEN+x}}" ]; then echo set; else echo unset; fi > "{records}/token"',
                f'printf \'%s\\n%s\' "$PM_CM_QUESTION_PATH" "$PM_CHILD_RESULT_PATH" > "{records}/paths"',
                "(",
                f"  {trigger_wait(trigger)}",
                "  printf '" + _QUESTION_TEXT.replace("\n", "\\n") + '\\n\' > "$PM_CM_QUESTION_PATH"',
                '  while [ ! -f "$(dirname "$PM_CM_QUESTION_PATH")/cm-answer-1.md" ]; do sleep 0.05; done',
                f"  echo '{_RESULT_JSON}' > \"$PM_CHILD_RESULT_PATH\"",
                ") &",
                f'while IFS= read -r line; do printf \'%s\\n\' "$line" >> "{records}/input"; done',
            ]
        )
        return write_fake_harness(self.repo.parent / "fake-child.sh", body)

    def _input_lines(self) -> list[str]:
        path = self.records / "input"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_launch_question_answer_result_and_stop(self) -> None:
        trigger = self.repo.parent / "question-trigger"
        harness = self._fake_child(trigger)
        os.environ["PM_RUN_TOKEN"] = "caller-token-must-not-leak"
        try:
            code, out, err = self.run_cli(self.launch_argv(harness))
            caller_token = os.environ.get("PM_RUN_TOKEN")
        finally:
            del os.environ["PM_RUN_TOKEN"]
        self.assertEqual(code, 0, out + err)
        self.assertEqual(caller_token, "caller-token-must-not-leak", "the caller's token must be restored")
        self.assert_socket_untouched()
        # tmux copies the starting process's environment into a new server's
        # global environment; the token must not be there either.
        server_env = _child_tmux(CHILD_SOCKET, "show-environment", "-g", "PM_RUN_TOKEN")
        self.assertNotIn("caller-token-must-not-leak", server_env.stdout)
        self.assertNotIn("PM_RUN_TOKEN=", server_env.stdout)
        self.assertNotIn("caller-token-must-not-leak", _child_tmux(CHILD_SOCKET, "show-environment", "-g").stdout)
        worktree = self.worktree.resolve()
        directory = campaign.campaign_dir(worktree)
        launcher = directory / "launcher.md"

        # Branch at REF, linked worktree on it, ignore file, launcher copy, manifest.
        self.assertEqual(self._git("rev-parse", "refs/heads/node-1").stdout.strip(), self.base_commit)
        self.assertIn(f"worktree {worktree}\nHEAD {self.base_commit}\nbranch refs/heads/node-1\n", self.worktrees())
        self.assertTrue((worktree / ".pm" / ".gitignore").is_file())
        self.assertEqual(launcher.read_bytes(), self.prompt_file.read_bytes())
        manifest = json.loads((directory / "child.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest), {*campaign._MANIFEST_FIELDS, "dossier_commit", "node_dir"})
        # The launcher sits in no git repository: nothing is refused and both are null.
        self.assertIsNone(manifest["dossier_commit"])
        self.assertIsNone(manifest["node_dir"])
        self.assertIn(f"\ngit dir: {git_ops.worktree_git_dir(worktree)}\n", out)
        self.assertIn("\nnode dir: none (the launcher lies in no git repository", out)
        self.assertEqual(manifest["socket"], CHILD_SOCKET)
        self.assertEqual(manifest["branch"], "node-1")
        self.assertEqual(manifest["base"], self.base_commit)
        self.assertEqual(manifest["worktree"], str(worktree))
        self.assertEqual(manifest["launcher"], str(launcher))
        session = manifest["session"]
        self.assertRegex(session, r"^campaign-node-1-[0-9a-f]{6}$")
        self.assertIn(
            f"child launched: session={session} socket={CHILD_SOCKET} worktree={worktree} launcher={launcher}", out
        )
        # The server carries launch-child's ownership mark, which stop-child verifies.
        self.assertEqual(sessions.server_environment(CHILD_SOCKET, "PM_CHILD_WORKTREE"), str(worktree))
        # HANDOFF.md is excluded in the file the linked worktree's git reads.
        (worktree / "HANDOFF.md").write_text("# Handoff\n", encoding="utf-8")
        status = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"], check=True, text=True, capture_output=True
        )
        self.assertEqual(status.stdout, "")

        # The session lives on the child's private server, not the caller's.
        self.assertEqual(_child_tmux(CHILD_SOCKET, "has-session", "-t", session).returncode, 0)
        self.assertFalse(sessions.session_exists(session))

        # What the fake recorded about its environment, and the pointer it read.
        self.assertTrue(self._wait_for(lambda: (self.records / "paths").exists()))
        self.assertEqual((self.records / "cwd").read_text(encoding="utf-8").strip(), str(worktree))
        self.assertEqual((self.records / "socket").read_text(encoding="utf-8"), CHILD_SOCKET)
        self.assertEqual((self.records / "token").read_text(encoding="utf-8").strip(), "unset")
        self.assertEqual(
            (self.records / "paths").read_text(encoding="utf-8").splitlines(),
            [str(directory / "cm-question.md"), str(directory / "child-result.json")],
        )
        pointer = prompts.render_child_launch_pointer(launcher)
        self.assertTrue(self._wait_for(lambda: pointer in self._input_lines()), self._input_lines())

        # No question yet: answer-child refuses and changes nothing.
        code, out, err = self.run_cli(["answer-child", "--worktree", str(worktree), "--text", "A"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("no open child question", err)
        self.assertEqual(list(directory.glob("cm-*")), [])
        self.assert_socket_untouched()

        # (b) The question ends the wait early and is printed.
        threading.Timer(1.0, trigger.write_text, args=("go\n",), kwargs={"encoding": "utf-8"}).start()
        start = time.monotonic()
        code, out, err = self.run_cli(["wait-child", "--worktree", str(worktree), "--wait", "30"])
        self.assertLess(time.monotonic() - start, 20.0)
        self.assertEqual(code, 0, err)
        fenced = "".join(f"| {line}\n" for line in _QUESTION_TEXT.splitlines())
        self.assertIn("question present: yes\n--- question ---\n" + fenced + "--- end question ---\n", out)
        self.assertIn("result present: False", out)
        self.assertNotIn("note:", out)
        self.assert_socket_untouched()

        # Blank text is refused with the question still open.
        code, out, err = self.run_cli(["answer-child", "--worktree", str(worktree), "--text", "  "])
        self.assertEqual(code, 2, out + err)
        self.assertIn("non-empty", err)
        self.assertEqual(sorted(p.name for p in directory.glob("cm-*")), ["cm-question.md"])

        # The answer goes through the shared helper with the cm- prefix.
        with mock.patch.object(campaign.slice_ops, "deliver_answer", wraps=slice_ops.deliver_answer) as deliver:
            code, out, err = self.run_cli(["answer-child", "--worktree", str(worktree), "--text", "A, data first"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(deliver.call_args.args[:4], (worktree, directory, "cm-", session))
        answer_path = directory / "cm-answer-1.md"
        self.assertEqual(answer_path.read_text(encoding="utf-8"), "A, data first")
        self.assertTrue((directory / "cm-question-1.md").is_file())
        self.assertFalse((directory / "cm-question.md").exists())
        self.assertIn(f"answered: {answer_path}", out)
        self.assertIn(f"question archived: {directory / 'cm-question-1.md'}", out)
        self.assertIn("delivery: confirmed\n", out)
        answer_pointer = prompts.render_answer_pointer(answer_path)
        self.assertTrue(self._wait_for(lambda: answer_pointer in self._input_lines()), self._input_lines())
        self.assert_socket_untouched()

        # The completion file ends the next wait early, with its status.
        start = time.monotonic()
        code, out, err = self.run_cli(["wait-child", "--worktree", str(worktree), "--wait", "30"])
        self.assertLess(time.monotonic() - start, 20.0)
        self.assertEqual(code, 0, err)
        self.assertIn("result present: True (status=complete; unverified: written by the child)", out)
        self.assertIn("question present: no", out)

        # With the completion file in, a fresh question cannot be answered.
        (directory / "cm-question.md").write_text("late?\n", encoding="utf-8")
        before = sorted(p.name for p in directory.glob("cm-*"))
        code, out, err = self.run_cli(["answer-child", "--worktree", str(worktree), "--text", "B"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("completion file", err)
        self.assertEqual(sorted(p.name for p in directory.glob("cm-*")), before)
        (directory / "cm-question.md").unlink()

        # Tokenless run lines, the pane-live capture and the tail.
        reviewer = self._write_child_runs()
        code, out, err = self.run_cli(["wait-child", "--worktree", str(worktree)])
        self.assertEqual(code, 0, err)
        self.assertIn("child run run-bad: status=unreadable stop_reason=None (unverified: tokenless read)", out)
        self.assertIn("child run run-good: status=stopped stop_reason=budget (unverified: tokenless read)", out)
        self.assertIn("FAKE_HARNESS_READY", (directory / "pane-live.txt").read_text(encoding="utf-8"))
        tail = out.split("--- pane tail ---\n", 1)[1]
        self.assertIn("FAKE_HARNESS_READY", tail)
        self.assertLessEqual(len(tail.splitlines()), 40)
        self.assert_socket_untouched()

        # (d) stop-child: full pane, reviewer groups printed and left alone, server gone.
        code, out, err = self.run_cli(["stop-child", "--worktree", str(worktree), "--reason", "node judged"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"reviewer process groups the child recorded: {reviewer.pid};", out)
        self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)
        self.assertIn("reason: node judged", out)
        pane = (directory / "pane.txt").read_text(encoding="utf-8")
        self.assertIn("FAKE_HARNESS_READY", pane)
        self.assertNotEqual(_child_tmux(CHILD_SOCKET, "has-session", "-t", session).returncode, 0)
        self.assertNotEqual(_child_tmux(CHILD_SOCKET, "list-sessions").returncode, 0)
        self.assertIsNone(reviewer.poll(), "stop-child must never signal a recorded reviewer")
        self.assert_socket_untouched()

        # A re-run keeps the first capture.
        code, out, err = self.run_cli(["stop-child", "--worktree", str(worktree), "--reason", "again"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("kept from an earlier capture", out)
        self.assertEqual((directory / "pane.txt").read_text(encoding="utf-8"), pane)

    def test_default_socket_and_names_for_a_branch_with_slash_and_dot(self) -> None:
        harness = write_fake_harness(self.repo.parent / "fake-child.sh", "echo FAKE_HARNESS_READY\nexec cat -")
        code, out, err = self.run_cli(self.launch_argv(harness, branch="feat/x.y", socket=None))
        self.assertEqual(code, 0, out + err)
        manifest = campaign.load_manifest(self.worktree.resolve())
        socket, session = manifest["socket"], manifest["session"]
        self.addCleanup(sessions.kill_server, socket)
        match = re.fullmatch(r"pm-child-feat-x-y-([0-9a-f]{6})", socket)
        self.assertIsNotNone(match, socket)
        self.assertEqual(session, f"campaign-feat-x-y-{match.group(1)}")
        self.assertEqual(_child_tmux(socket, "has-session", "-t", session).returncode, 0)
        self.assert_socket_untouched()
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.worktree), "--reason", "done"])
        self.assertEqual(code, 0, out + err)
        self.assertNotEqual(_child_tmux(socket, "list-sessions").returncode, 0)

    def test_a_failure_after_the_session_started_kills_the_server_and_keeps_the_manifest(self) -> None:
        # Readiness fails closed on a trust prompt while the session is alive.
        harness = write_fake_harness(
            self.repo.parent / "fake-child.sh", "echo 'Do you trust the files in this folder?'\nsleep 60"
        )
        code, out, err = self.run_cli(self.launch_argv(harness))
        self.assertEqual(code, 2, out + err)
        self.assertIn("pm: error:", err)
        self.assertIn("trust prompt", err)
        self.assertIn("branch node-1", err)
        self.assertIn(str(self.worktree.resolve()), err)
        self.assertNotEqual(_child_tmux(CHILD_SOCKET, "list-sessions").returncode, 0)
        self.assertTrue(campaign.manifest_path(self.worktree.resolve()).is_file())
        pane_path = campaign.campaign_dir(self.worktree.resolve()) / "pane.txt"
        self.assertIn(f"its pane was captured to {pane_path}", err)
        self.assertIn("Do you trust the files in this folder?", pane_path.read_text(encoding="utf-8"))
        self.assert_socket_untouched()
        # The kept manifest lets stop-child run again.
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.worktree), "--reason", "failed launch"])
        self.assertEqual(code, 0, out + err)

    def test_a_send_failure_after_the_manifest_is_written_kills_the_server_and_keeps_the_evidence(self) -> None:
        harness = write_fake_harness(self.repo.parent / "fake-child.sh", "echo FAKE_HARNESS_READY\nexec cat -")
        with mock.patch.object(campaign.sessions, "send_prompt", side_effect=PmError("send failed: boom")):
            code, out, err = self.run_cli(self.launch_argv(harness))
        self.assertEqual(code, 2, out + err)
        self.assertIn("boom", err)
        self.assertIn("branch node-1", err)
        self.assertNotEqual(_child_tmux(CHILD_SOCKET, "list-sessions").returncode, 0)
        worktree = self.worktree.resolve()
        self.assertTrue(campaign.manifest_path(worktree).is_file())
        pane_path = campaign.campaign_dir(worktree) / "pane.txt"
        self.assertIn(f"its pane was captured to {pane_path}", err)
        self.assertIn("FAKE_HARNESS_READY", pane_path.read_text(encoding="utf-8"))
        self.assert_socket_untouched()

    def test_wait_child_returns_on_a_marker_and_on_death_but_not_on_pane_change(self) -> None:
        churn, marker, clear, die = (self.repo.parent / name for name in ("churn", "marker", "clear", "die"))
        body = "\n".join(
            [
                "echo FAKE_HARNESS_READY",
                trigger_wait(churn),
                f'n=0; while [ ! -f "{marker}" ]; do n=$((n+1)); echo "tick $n"; sleep 0.3; done',
                "echo 'Enter API key to continue'",
                trigger_wait(clear),
                # Scroll the marker off the visible pane, then exit on the last trigger.
                "i=0; while [ $i -lt 80 ]; do echo; i=$((i+1)); done",
                "echo CLEARED",
                trigger_wait(die),
            ]
        )
        harness = write_fake_harness(self.repo.parent / "fake-child.sh", body)
        code, out, err = self.run_cli(self.launch_argv(harness))
        self.assertEqual(code, 0, out + err)
        argv = ["wait-child", "--worktree", str(self.worktree)]

        # A changing pane alone never ends the wait.
        churn.write_text("go\n", encoding="utf-8")
        start = time.monotonic()
        code, out, err = self.run_cli([*argv, "--wait", "5"])
        self.assertGreaterEqual(time.monotonic() - start, 4.5)
        self.assertEqual(code, 0, err)
        self.assertIn("session running: True", out)
        self.assertIn("dialog marker: clear", out)
        self.assertIn("note: this wait returned no signal", out)

        # A dialog marker on the visible pane does.
        threading.Timer(1.0, marker.write_text, args=("go\n",), kwargs={"encoding": "utf-8"}).start()
        start = time.monotonic()
        code, out, err = self.run_cli([*argv, "--wait", "30"])
        self.assertLess(time.monotonic() - start, 20.0)
        self.assertEqual(code, 0, err)
        self.assertIn("dialog marker: credential_prompt", out)
        self.assertNotIn("note:", out)

        # So does the session's death, once the marker has scrolled away.
        clear.write_text("go\n", encoding="utf-8")
        self.assertTrue(
            self._wait_for(lambda: "dialog marker: clear" in self.run_cli(argv)[1]), "the marker never scrolled away"
        )
        threading.Timer(1.0, die.write_text, args=("go\n",), kwargs={"encoding": "utf-8"}).start()
        start = time.monotonic()
        code, out, err = self.run_cli([*argv, "--wait", "30"])
        self.assertLess(time.monotonic() - start, 20.0)
        self.assertEqual(code, 0, err)
        self.assertIn("session running: False", out)
        self.assert_socket_untouched()


# --- sandboxed children ----------------------------------------------------------


class SandboxLaunchTestCase(CampaignTestCase):
    """A dossier holding `brief.md` and `sandbox.json`, with the launcher
    under `nodes/n1/` (the dossier is a git repository, and the brief at its
    root governs the launcher), and an idle fake child."""

    def setUp(self) -> None:
        super().setUp()
        self.dossier = self.repo.parent / "dossier"
        (self.dossier / "nodes" / "n1").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.dossier)], check=True)
        self.prompt_file = self.dossier / "nodes" / "n1" / "launcher.md"
        self.prompt_file.write_text("# Launcher\n\nRun the node plan.\n", encoding="utf-8")
        self.commit_dossier()
        self.policy = write_base_policy(self.dossier / "sandbox.json")
        self.harness = write_fake_harness(self.repo.parent / "fake-child.sh", "echo FAKE_HARNESS_READY\nexec cat -")

    def commit_dossier(self) -> str:
        """Commit everything in the dossier (a launch refuses an uncommitted node directory); its HEAD."""
        git = ["git", "-C", str(self.dossier), "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "dossier"], check=True)
        return subprocess.run([*git, "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()

    def write_brief(self, section: str | None) -> Path:
        """`brief.md` in the dossier, with `section` as its `## Sandbox` body (none when None)."""
        text = "# Brief: t\n\n## Worktree setup\n- none\n"
        if section is not None:
            text += f"\n## Sandbox\n{section}\n"
        text += "\n## Budgets\n- max_parallel: 1\n"
        path = self.dossier / "brief.md"
        path.write_text(text, encoding="utf-8")
        return path

    def pin(self, policy: Path) -> str:
        """A `## Sandbox` body pinning `policy`'s SHA-256."""
        return f"- `sandbox.json` in this dossier, SHA-256 `{hashlib.sha256(policy.read_bytes()).hexdigest()}`."

    def control_dir(self, socket: str) -> Path:
        return sandbox.control_dir(Path(os.path.realpath(self.repo / ".git")), socket)

    def sandbox_tmpdir(self, socket: str) -> Path:
        return Path(os.path.realpath(tempfile.gettempdir())) / f"{sandbox.TMPDIR_PREFIX}{socket}"

    def without_srt(self) -> contextlib.ExitStack:
        """Patches under which a sandboxed launch passes the `srt` and macOS
        preconditions on any host, for tests that stop before a server starts."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(campaign.sandbox_mod, "check_binaries"))
        stack.enter_context(mock.patch.object(campaign.sandbox_mod, "srt_version", return_value="0.0.81-test"))
        stack.enter_context(mock.patch.object(campaign.platform, "system", return_value="Darwin"))
        return stack

    def clean_sandbox_dirs(self, socket: str) -> None:
        """Remove `socket`'s temp dir and the control root once the test ends."""
        self.addCleanup(shutil.rmtree, self.sandbox_tmpdir(socket), True)
        self.addCleanup(shutil.rmtree, sandbox.control_root(Path(os.path.realpath(self.repo / ".git"))), True)


class TestSandboxLaunchRefusals(SandboxLaunchTestCase):
    """Each refusal exits 2 before any branch, worktree, control directory
    or temp directory exists."""

    def assert_refused(self, argv: list[str], *fragments: str) -> str:
        branches, worktrees = self.branches(), self.worktrees()
        code, out, err = self.run_cli(argv)
        self.assertEqual(code, 2, out + err)
        self.assertIn("pm: error:", err)
        for fragment in fragments:
            self.assertIn(fragment, err)
        self.assertEqual(self.branches(), branches)
        self.assertEqual(self.worktrees(), worktrees)
        self.assertFalse(self.worktree.exists())
        self.assertFalse(sandbox.control_root(self.repo / ".git").exists())
        self.assertFalse(self.sandbox_tmpdir(CHILD_SOCKET).exists())
        self.assert_socket_untouched()
        return err

    def test_a_base_policy_that_fails_validation_is_refused_naming_the_file_and_the_reason(self) -> None:
        policy = minimal_base_policy()
        policy["allowAppleEvents"] = True
        write_base_policy(self.policy, policy)
        self.assert_refused(
            self.launch_argv(self.harness) + ["--sandbox", str(self.policy)], str(self.policy), "allowAppleEvents"
        )

    def test_an_empty_sandbox_value_is_refused_not_taken_for_no_flag(self) -> None:
        # No brief pins a policy here, so an empty value read as absent would launch unsandboxed.
        for value in ("", "  "):
            with self.subTest(value=value):
                self.assert_refused(
                    self.launch_argv(self.harness) + ["--sandbox", value], "--sandbox needs an srt base policy file"
                )

    def test_a_pinned_brief_refuses_a_launch_without_sandbox_quoting_the_pin(self) -> None:
        pin = hashlib.sha256(self.policy.read_bytes()).hexdigest()
        brief = self.write_brief(self.pin(self.policy))
        self.assert_refused(self.launch_argv(self.harness), f"SHA-256 `{pin}`", str(brief.resolve()), "--sandbox")

    def test_a_pinned_brief_refuses_a_policy_with_another_sha256_quoting_the_pin(self) -> None:
        pin = hashlib.sha256(self.policy.read_bytes()).hexdigest()
        self.write_brief(self.pin(self.policy))
        other = minimal_base_policy()
        other["network"]["allowedDomains"] = ["example.com"]
        other_path = write_base_policy(self.repo.parent / "other-sandbox.json", other)
        other_sha = hashlib.sha256(other_path.read_bytes()).hexdigest()
        self.assert_refused(self.launch_argv(self.harness) + ["--sandbox", str(other_path)], f"pins `{pin}`", other_sha)

    def test_a_sandbox_section_naming_neither_a_policy_nor_none_is_refused(self) -> None:
        brief = self.write_brief("- to be decided")
        self.assert_refused(
            self.launch_argv(self.harness), "names neither sandbox.json nor none", f"(in {brief.resolve()})"
        )
        self.write_brief("- `sandbox.json` in this dossier, unpinned.")
        self.assert_refused(self.launch_argv(self.harness), "without its pin", f"(in {brief.resolve()})")

    @unittest.skipUnless(_HAS_TMUX, "the tmux precondition is checked first")
    def test_srt_absent_from_path_is_refused_before_anything_is_created(self) -> None:
        real_which = shutil.which

        def which(name: str, *args, **kwargs) -> str | None:
            return None if name == "srt" else real_which(name, *args, **kwargs)

        with mock.patch.object(campaign.shutil, "which", side_effect=which):
            self.assert_refused(self.launch_argv(self.harness) + ["--sandbox", str(self.policy)], "srt is required")

    @unittest.skipUnless(_HAS_TMUX, "the preconditions list the socket's server")
    def test_a_brief_without_a_pin_leaves_sandbox_optional(self) -> None:
        for number, section in enumerate((None, "- none")):
            with self.subTest(section=section):
                self.write_brief(section)
                self.worktree = self.repo.parent / f"node-wt-{number}"
                with mock.patch.object(campaign, "_start_child_session") as start:
                    code, out, err = self.run_cli(self.launch_argv(self.harness, branch=f"node-{number}"))
                self.assertEqual(code, 0, out + err)
                start.assert_called_once()
                launch = start.call_args.args[0]
                self.assertIsNone(launch.sandbox)
                self.assertIsNone(launch.control_dir)
                self.assertIn(f"watch: tmux -L {CHILD_SOCKET} attach -t {launch.session}", out)


@unittest.skipUnless(_HAS_TMUX, "tmux is required to drive a real child session")
class TestUnsandboxedLaunchUnchanged(CampaignTestCase):
    def test_no_control_dir_no_sandbox_key_and_a_plain_watch_line_and_stop_output(self) -> None:
        harness = write_fake_harness(self.repo.parent / "fake-child.sh", "echo FAKE_HARNESS_READY\nexec cat -")
        code, out, err = self.run_cli(self.launch_argv(harness))
        self.assertEqual(code, 0, out + err)
        worktree = self.worktree.resolve()
        manifest = json.loads(campaign.manifest_path(worktree).read_text(encoding="utf-8"))
        self.assertEqual(list(manifest), [*campaign._MANIFEST_FIELDS, "dossier_commit", "node_dir"])
        session = manifest["session"]
        self.assertIn(f"\nwatch: tmux -L {CHILD_SOCKET} attach -t {session}\n", out)
        self.assertNotIn("sandbox", out)
        self.assertFalse(sandbox.control_root(self.repo / ".git").exists())
        self.assertIsNone(sessions.server_environment(CHILD_SOCKET, "SANDBOX_RUNTIME"))
        code, out, err = self.run_cli(["stop-child", "--worktree", str(worktree), "--reason", "done"])
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("sandbox", out)
        self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)


class TestLaunchChildNodeDirectory(SandboxLaunchTestCase):
    """The launcher's directory in the dossier must be committed; the launch records where and at what commit."""

    def assert_dirty_refusal(self, *fragments: str) -> None:
        branches, worktrees = self.branches(), self.worktrees()
        harness = write_fake_harness(self.repo.parent / "fake-child.sh", "echo FAKE_HARNESS_READY\nsleep 30")
        code, out, err = self.run_cli(self.launch_argv(harness))
        self.assertEqual(code, 2, out + err)
        self.assertIn("uncommitted or untracked files", err)
        for fragment in fragments:
            self.assertIn(fragment, err)
        self.assertEqual(self.branches(), branches)
        self.assertEqual(self.worktrees(), worktrees)
        self.assertFalse(self.worktree.exists(), "nothing may be created before the refusal")
        self.assert_socket_untouched()

    def test_an_untracked_file_in_the_node_directory_is_refused_by_name(self) -> None:
        (self.prompt_file.parent / "notes.md").write_text("draft\n", encoding="utf-8")
        self.assert_dirty_refusal("?? notes.md")

    def test_a_modified_launcher_is_refused_by_name(self) -> None:
        self.prompt_file.write_text("# Launcher\n\nEdited after the commit.\n", encoding="utf-8")
        self.assert_dirty_refusal(" M launcher.md")

    def test_a_dirty_file_outside_the_node_directory_is_not_this_directorys_business(self) -> None:
        (self.dossier / "nodes" / "n2").mkdir()
        (self.dossier / "nodes" / "n2" / "draft.md").write_text("draft\n", encoding="utf-8")
        (self.dossier / "scratch.md").write_text("draft\n", encoding="utf-8")
        head = subprocess.run(
            ["git", "-C", str(self.dossier), "rev-parse", "HEAD"], check=True, text=True, capture_output=True
        ).stdout.strip()
        self.assertEqual(campaign._committed_node_dir(self.prompt_file), (self.prompt_file.resolve().parent, head))

    def test_an_ignored_launcher_is_refused_because_status_never_lists_it(self) -> None:
        (self.dossier / ".gitignore").write_text("launcher.md\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.dossier), "rm", "-q", "--cached", str(self.prompt_file)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.dossier),
                "-c",
                "user.email=t@x",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "-am",
                "ignore",
            ],
            check=True,
        )
        with self.assertRaises(PmError) as caught:
            campaign._committed_node_dir(self.prompt_file)
        self.assertIn("is not tracked", str(caught.exception))

    def test_a_launcher_in_no_repository_records_nothing_and_is_not_refused(self) -> None:
        self.assertEqual(campaign._committed_node_dir(self.repo.parent / "launcher-src.md"), (None, None))

    @unittest.skipUnless(_HAS_TMUX, "tmux is required to drive a real child session")
    def test_a_clean_node_directory_launches_and_records_the_dossier_commit_and_node_dir(self) -> None:
        head = self.commit_dossier()
        harness = write_fake_harness(self.repo.parent / "fake-child.sh", "echo FAKE_HARNESS_READY\nexec cat -")
        code, out, err = self.run_cli(self.launch_argv(harness))
        self.assertEqual(code, 0, out + err)
        worktree = self.worktree.resolve()
        node_dir = self.prompt_file.resolve().parent
        manifest = json.loads(campaign.manifest_path(worktree).read_text(encoding="utf-8"))
        self.assertEqual(manifest["dossier_commit"], head)
        self.assertEqual(manifest["node_dir"], str(node_dir))
        self.assertIn(f"\ngit dir: {git_ops.worktree_git_dir(worktree)}\n", out)
        self.assertIn(f"\nnode dir: {node_dir} at dossier commit {head}\n", out)
        self.assertEqual(campaign.load_manifest(worktree)["session"], manifest["session"])
        code, out, err = self.run_cli(["stop-child", "--worktree", str(worktree), "--reason", "done"])
        self.assertEqual(code, 0, out + err)


@unittest.skipUnless(_HAS_TMUX, "tmux is required to drive a real child session")
class TestWaitChildSeenMarker(CampaignTestCase):
    """`--seen-marker` suppresses only the named literal, which is still printed on return."""

    def test_a_seen_marker_does_not_end_the_wait_but_another_marker_does(self) -> None:
        transcript, credential = self.repo.parent / "transcript", self.repo.parent / "credential"
        body = "\n".join(
            [
                "echo FAKE_HARNESS_READY",
                trigger_wait(transcript),
                "echo 'Showing detailed transcript'",
                trigger_wait(credential),
                "echo 'Enter API key to continue'",
                "sleep 60",
            ]
        )
        code, out, err = self.run_cli(self.launch_argv(write_fake_harness(self.repo.parent / "fake-child.sh", body)))
        self.assertEqual(code, 0, out + err)
        argv = ["wait-child", "--worktree", str(self.worktree)]
        seen = ["--seen-marker", "Showing detailed transcript"]

        # The transcript view ends a wait, and is named.
        transcript.write_text("go\n", encoding="utf-8")
        code, out, err = self.run_cli([*argv, "--wait", "30"])
        self.assertEqual(code, 0, err)
        self.assertIn("dialog marker: transcript_view (matched 'Showing detailed transcript')", out)
        self.assertNotIn("note:", out)

        # Named as seen, it no longer does: the wait runs out, still printing the marker.
        start = time.monotonic()
        code, out, err = self.run_cli([*argv, *seen, "--wait", "4"])
        self.assertGreaterEqual(time.monotonic() - start, 3.5, "a seen marker must not end the wait")
        self.assertIn("dialog marker: transcript_view (matched 'Showing detailed transcript')", out)
        self.assertIn("note: this wait returned no signal", out)

        # A different literal in the same pane ends it.
        threading.Timer(1.0, credential.write_text, args=("go\n",), kwargs={"encoding": "utf-8"}).start()
        start = time.monotonic()
        code, out, err = self.run_cli([*argv, *seen, "--wait", "30"])
        self.assertLess(time.monotonic() - start, 20.0)
        self.assertIn("credential_prompt", out)
        self.assertIn("(matched 'Enter API key'; 'Showing detailed transcript')", out)
        self.assertNotIn("note:", out)
        self.assert_socket_untouched()


@unittest.skipUnless(_HAS_TMUX, "tmux is required to drive a real child session")
class TestAnswerChildDelivery(CampaignTestCase):
    """A child whose pane never reacts (no tty echo) reads as an unconfirmed delivery,
    which `answer-child --redeliver` can then repeat."""

    def setUp(self) -> None:
        super().setUp()
        body = "\n".join(
            [
                "echo FAKE_HARNESS_READY",
                "stty -echo",
                f'while IFS= read -r line; do printf \'%s\\n\' "$line" >> "{self.records}/input"; done',
            ]
        )
        harness = write_fake_harness(self.repo.parent / "fake-child.sh", body)
        code, out, err = self.run_cli(self.launch_argv(harness))
        self.assertEqual(code, 0, out + err)
        self.worktree = self.worktree.resolve()
        self.directory = campaign.campaign_dir(self.worktree)
        self.argv = ["answer-child", "--worktree", str(self.worktree)]

    def inputs(self) -> list[str]:
        path = self.records / "input"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def pointer(self, number: int) -> str:
        return prompts.render_answer_pointer(self.directory / f"cm-answer-{number}.md")

    def files(self) -> list[str]:
        return sorted(path.name for path in self.directory.glob("cm-*"))

    def test_a_pane_that_does_not_react_is_unconfirmed_and_redeliver_resends_the_pointer(self) -> None:
        (self.directory / "cm-question.md").write_text("which?\n", encoding="utf-8")
        code, out, err = self.run_cli([*self.argv, "--text", "A"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(
            "delivery: unconfirmed (the pane did not change after Enter; re-check with observe/wait-child, "
            "then --redeliver if the child did not react)",
            out,
        )
        self.assertTrue(self._wait_for(lambda: self.inputs().count(self.pointer(1)) == 1), self.inputs())
        files = self.files()

        code, out, err = self.run_cli([*self.argv, "--redeliver"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"redelivered: {self.directory / 'cm-answer-1.md'}", out)
        self.assertIn("delivery: unconfirmed", out)
        self.assertTrue(self._wait_for(lambda: self.inputs().count(self.pointer(1)) == 2), self.inputs())
        self.assertEqual(self.files(), files, "a re-send writes nothing")
        self.assert_socket_untouched()

    def test_redeliver_sends_the_highest_numbered_answer_numerically(self) -> None:
        for number in (2, 10):
            (self.directory / f"cm-answer-{number}.md").write_text("x", encoding="utf-8")
        code, out, err = self.run_cli([*self.argv, "--redeliver"])
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self._wait_for(lambda: self.pointer(10) in self.inputs()), self.inputs())
        self.assertNotIn(self.pointer(2), self.inputs())

    def test_redeliver_refuses_with_no_answer(self) -> None:
        code, out, err = self.run_cli([*self.argv, "--redeliver"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("no answer to re-send", err)
        self.assertEqual(self.inputs().count(self.pointer(1)), 0)

    def test_redeliver_refuses_while_a_newer_question_is_open(self) -> None:
        (self.directory / "cm-answer-1.md").write_text("x", encoding="utf-8")
        (self.directory / "cm-question.md").write_text("and then?\n", encoding="utf-8")
        code, out, err = self.run_cli([*self.argv, "--redeliver"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("a newer question is waiting", err)
        time.sleep(1.0)
        self.assertNotIn(self.pointer(1), self.inputs())

    def test_redeliver_refuses_once_the_child_has_written_its_result(self) -> None:
        (self.directory / "cm-answer-1.md").write_text("x", encoding="utf-8")
        (self.directory / "child-result.json").write_text(_RESULT_JSON, encoding="utf-8")
        code, out, err = self.run_cli([*self.argv, "--redeliver"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("completion file", err)
        time.sleep(1.0)
        self.assertNotIn(self.pointer(1), self.inputs())

    def test_text_and_redeliver_are_exclusive_and_one_is_required(self) -> None:
        for extra in (["--text", "A", "--redeliver"], []):
            with self.subTest(extra=extra):
                code, _out, err = self.run_cli([*self.argv, *extra])
                self.assertEqual(code, 2, err)
        with self.assertRaises(PmError):
            campaign.answer_child(self.worktree, text="A", redeliver=True)
        with self.assertRaises(PmError):
            campaign.answer_child(self.worktree)


@unittest.skipUnless(_HAS_TMUX, "tmux is required to host the marked server")
class TestStopChildSandboxRecord(SandboxLaunchTestCase):
    def test_a_record_naming_another_worktree_is_refused_and_the_manifest_control_dir_is_ignored(self) -> None:
        worktree = make_linked_worktree(self.repo, self.worktree, "node-1").resolve()
        session = "campaign-node-1-000000"
        control = self.control_dir(CHILD_SOCKET)
        self.addCleanup(shutil.rmtree, control.parent, True)
        other = str(self.repo.parent / "other-wt")
        sandbox.write_record(
            control, worktree=other, socket=CHILD_SOCKET, tmpdir=str(self.sandbox_tmpdir(CHILD_SOCKET)), srt_pid=None
        )
        # A decoy the manifest names, holding a record that would pass: never read.
        decoy = self.repo.parent / "decoy" / "pm-sandbox" / CHILD_SOCKET
        sandbox.write_record(decoy, worktree=str(worktree), socket=CHILD_SOCKET, tmpdir="/nowhere", srt_pid=None)
        directory = campaign.campaign_dir(worktree)
        directory.mkdir(parents=True)
        manifest = {
            "socket": CHILD_SOCKET,
            "session": session,
            "branch": "node-1",
            "base": self.base_commit,
            "worktree": str(worktree),
            "launcher": str(directory / "launcher.md"),
            "launched_at": "2026-10-25T00:00:00Z",
            "sandbox": {"policy": "x", "policy_sha256": "0" * 64, "socket": CHILD_SOCKET, "control_dir": str(decoy)},
        }
        campaign.manifest_path(worktree).write_text(json.dumps(manifest), encoding="utf-8")
        started = _child_tmux(CHILD_SOCKET, "new-session", "-d", "-s", session, "sleep 120")
        self.assertEqual(started.returncode, 0, started.stderr)
        # The right mark, so the record is what refuses.
        sessions.set_server_environment(CHILD_SOCKET, "PM_CHILD_WORKTREE", str(worktree))

        code, out, err = self.run_cli(["stop-child", "--worktree", str(worktree), "--reason", "test"])
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"the sandbox record in {control} names worktree {other!r}", err)
        self.assertIn("refusing to kill the server", err)
        self.assertEqual(_child_tmux(CHILD_SOCKET, "has-session", "-t", session).returncode, 0)
        self.assertTrue((control / "record.json").is_file())
        self.assertTrue((decoy / "record.json").is_file())
        self.assertFalse((directory / "pane.txt").exists())
        self.assert_socket_untouched()


@unittest.skipUnless(_HAS_TMUX, "the preconditions list the socket's server")
class TestSandboxLaunchHostRefusals(SandboxLaunchTestCase):
    """The host-state refusals of a sandboxed launch, and its control
    directory acquisition, on any host (`srt` and macOS checks patched)."""

    def setUp(self) -> None:
        super().setUp()
        self.clean_sandbox_dirs(CHILD_SOCKET)
        self.argv = self.launch_argv(self.harness) + ["--sandbox", str(self.policy)]

    def assert_refused_creating_nothing(self, *fragments: str) -> None:
        branches, worktrees = self.branches(), self.worktrees()
        with self.without_srt():
            code, out, err = self.run_cli(self.argv)
        self.assertEqual(code, 2, out + err)
        for fragment in fragments:
            self.assertIn(fragment, err)
        self.assertEqual(self.branches(), branches)
        self.assertEqual(self.worktrees(), worktrees)
        self.assertFalse(self.worktree.exists())

    def test_a_server_already_answering_on_the_socket_is_refused(self) -> None:
        with mock.patch.object(campaign.sessions, "server_running", return_value=True):
            self.assert_refused_creating_nothing(f"a tmux server already answers on socket {CHILD_SOCKET!r}")
        self.assertFalse(sandbox.control_root(self.repo / ".git").exists())
        self.assertFalse(self.sandbox_tmpdir(CHILD_SOCKET).exists())

    def test_an_existing_control_dir_is_refused_and_left_intact(self) -> None:
        control = self.control_dir(CHILD_SOCKET)
        control.mkdir(parents=True)
        (control / "record.json").write_text("another child's\n", encoding="utf-8")
        self.assert_refused_creating_nothing(f"sandbox control dir already exists: {control}", "stop-child")
        self.assertEqual((control / "record.json").read_text(encoding="utf-8"), "another child's\n")
        self.assertFalse(self.sandbox_tmpdir(CHILD_SOCKET).exists())

    def test_an_unsandboxed_launch_refuses_an_existing_control_dir_for_its_socket(self) -> None:
        control = self.control_dir(CHILD_SOCKET)
        control.mkdir(parents=True)
        (control / "record.json").write_text("an earlier child's\n", encoding="utf-8")
        self.argv = self.launch_argv(self.harness)
        self.assert_refused_creating_nothing(f"sandbox control dir already exists: {control}", "remove it by hand")
        self.assertEqual((control / "record.json").read_text(encoding="utf-8"), "an earlier child's\n")

    def test_an_existing_temp_dir_is_refused_and_left_intact(self) -> None:
        tmpdir = self.sandbox_tmpdir(CHILD_SOCKET)
        tmpdir.mkdir()
        (tmpdir / "keep").write_text("x\n", encoding="utf-8")
        self.assert_refused_creating_nothing("sandbox temp dir already exists: ", tmpdir.name)
        self.assertTrue((tmpdir / "keep").is_file())
        self.assertFalse(self.control_dir(CHILD_SOCKET).exists())

    def test_a_control_dir_created_by_a_rival_after_the_checks_is_never_removed(self) -> None:
        control = self.control_dir(CHILD_SOCKET)

        def rival_launch(launch: campaign._ChildLaunch) -> None:
            # A rival on the same socket acquires the directory while this launch runs its setup.
            control.mkdir(parents=True)
            (control / "record.json").write_text("the rival's record\n", encoding="utf-8")
            (control / "policy.json").write_text("{}\n", encoding="utf-8")

        with self.without_srt(), mock.patch.object(campaign, "_run_worktree_setup", side_effect=rival_launch):
            code, out, err = self.run_cli(self.argv)
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"sandbox control dir already exists: {control}", err)
        self.assertIn("it was left untouched", err)
        self.assertIn("are left behind", err)
        self.assertNotIn("removed sandbox", err)
        self.assertEqual(sorted(path.name for path in control.iterdir()), ["policy.json", "record.json"])
        self.assertEqual((control / "record.json").read_text(encoding="utf-8"), "the rival's record\n")
        self.assertFalse(self.sandbox_tmpdir(CHILD_SOCKET).exists())
        self.assertFalse(sessions.server_running(CHILD_SOCKET))

    def test_an_interrupt_inside_start_server_kills_the_server_it_left_and_removes_the_dirs(self) -> None:
        control = self.control_dir(CHILD_SOCKET)
        tmpdir = self.sandbox_tmpdir(CHILD_SOCKET)

        def interrupted_start(*, socket: str, **_kwargs) -> tuple[int, int]:
            # srt has brought a server up, and the pid is never handed back.
            started = _child_tmux(socket, "new-session", "-d", "-s", "srt-started", "sleep 120")
            self.assertEqual(started.returncode, 0, started.stderr)
            raise KeyboardInterrupt

        with self.without_srt(), mock.patch.object(campaign.sandbox_mod, "start_server", side_effect=interrupted_start):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.run_cli(self.argv)
        self.assertFalse(sessions.server_running(CHILD_SOCKET))
        self.assertFalse(control.exists())
        self.assertFalse(tmpdir.exists())
        notes = "\n".join(getattr(raised.exception, "__notes__", []))
        self.assertIn(f"the tmux server the interrupted start left on socket {CHILD_SOCKET} was killed", notes)
        self.assertIn(f"removed sandbox temp dir {tmpdir}", notes)
        self.assertIn(f"removed sandbox control dir {control}", notes)
        self.assertIn("are left behind", notes)

    def test_base_policy_warnings_print_as_warning_lines(self) -> None:
        policy = minimal_base_policy()
        policy["enableWeakerNetworkIsolation"] = True
        write_base_policy(self.policy, policy)
        with self.without_srt(), mock.patch.object(campaign, "_start_child_session"):
            code, out, err = self.run_cli(self.argv)
        self.assertEqual(code, 0, out + err)
        warnings = [line for line in out.splitlines() if line.startswith("WARNING: ")]
        self.assertEqual(len(warnings), 1, out)
        self.assertIn("enableWeakerNetworkIsolation", warnings[0])
        self.assertNotIn("note:", out)

    def test_a_sandboxed_launch_composes_the_sandboxed_harness_command(self) -> None:
        """A composed codex child gets the profile's sandboxed variant
        (`--no-daemon`): `sandboxed=True` reaches `launch_command`."""
        fake_bin = self.repo.parent / "fake-bin"
        fake_bin.mkdir()
        write_fake_harness(fake_bin / "codex", "exec cat -")
        argv = list(self.argv)
        at = argv.index("--harness")
        argv[at : at + 4] = ["--harness", "codex"]  # no --harness-command: the profile composes it
        with (
            self.without_srt(),
            mock.patch.dict(os.environ, {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}),
            mock.patch.object(campaign.profiles, "launch_command", wraps=campaign.profiles.launch_command) as compose,
            mock.patch.object(campaign, "_start_child_session") as start,
        ):
            code, out, err = self.run_cli(argv)
        self.assertEqual(code, 0, out + err)
        compose.assert_called_once()
        self.assertIs(compose.call_args.kwargs["sandboxed"], True)
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0].command.split()[-1], "--no-daemon")


@unittest.skipUnless(_HAS_TMUX, "tmux is required to host the marked server")
class TestStopChildSandboxTeardown(SandboxLaunchTestCase):
    """stop-child's sandbox teardown outcomes on a marked plain server
    standing in for a sandboxed one: every way the sandbox is left up exits
    2 with its line printed."""

    def setUp(self) -> None:
        super().setUp()
        self.clean_sandbox_dirs(CHILD_SOCKET)
        self.child = make_linked_worktree(self.repo, self.worktree, "node-1").resolve()
        self.session = "campaign-node-1-000000"
        self.control = self.control_dir(CHILD_SOCKET)
        self.tmpdir = self.sandbox_tmpdir(CHILD_SOCKET)
        self.control.mkdir(parents=True)
        self.tmpdir.mkdir()
        directory = campaign.campaign_dir(self.child)
        directory.mkdir(parents=True)
        manifest = {
            "socket": CHILD_SOCKET,
            "session": self.session,
            "branch": "node-1",
            "base": self.base_commit,
            "worktree": str(self.child),
            "launcher": str(directory / "launcher.md"),
            "launched_at": "2026-10-25T00:00:00Z",
            "sandbox": {"policy": "x", "policy_sha256": "0" * 64, "socket": CHILD_SOCKET, "control_dir": "/ignored"},
        }
        campaign.manifest_path(self.child).write_text(json.dumps(manifest), encoding="utf-8")
        started = _child_tmux(CHILD_SOCKET, "new-session", "-d", "-s", self.session, "sleep 120")
        self.assertEqual(started.returncode, 0, started.stderr)
        sessions.set_server_environment(CHILD_SOCKET, "PM_CHILD_WORKTREE", str(self.child))

    def write_record(self) -> None:
        sandbox.write_record(
            self.control, worktree=str(self.child), socket=CHILD_SOCKET, tmpdir=str(self.tmpdir), srt_pid=None
        )

    def stop(self) -> tuple[int, str]:
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.child), "--reason", "test"])
        self.assertEqual(err, "")
        return code, out

    def test_a_surviving_srt_keeps_the_record_and_every_run_exits_2_until_it_has_gone(self) -> None:
        # A process of this test's own stands in for an srt that outlives its server.
        srt = subprocess.Popen(["sleep", "60"])
        self.addCleanup(srt.wait)
        self.addCleanup(srt.kill)
        sandbox.write_record(
            self.control, worktree=str(self.child), socket=CHILD_SOCKET, tmpdir=str(self.tmpdir), srt_pid=srt.pid
        )
        with mock.patch.object(sandbox, "SRT_EXIT_WAIT_SECONDS", 0.2):
            code, out = self.stop()
            self.assertEqual(code, 2, out)
            self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)
            self.assertIn(f"\nsrt still running: {srt.pid}\n", out)
            self.assertTrue((self.control / "record.json").is_file())
            # The record survived the first run, so a re-run still sees the srt.
            code, out = self.stop()
            self.assertEqual(code, 2, out)
            self.assertIn(f"\nsrt still running: {srt.pid}\n", out)
        srt.kill()
        srt.wait()
        code, out = self.stop()
        self.assertEqual(code, 0, out)
        self.assertIn(f"removed sandbox control dir {self.control}\n", out)
        self.assertFalse(self.control.exists())

    def test_a_stop_under_another_tmpdir_removes_the_control_dir_and_leaves_the_temp_dir_for_a_rerun(self) -> None:
        """A `TMPDIR` other than the launch's: the server killed and the
        control dir removed, the temp dir left with a note and exit 0 (a
        retry cannot fix it); a re-run under the launch's `TMPDIR` removes
        it."""
        self.write_record()
        other = self.repo.parent / "other-tmp"
        other.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(other)):
            code, out = self.stop()
        self.assertEqual(code, 0, out)
        self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)
        self.assertIn(f"left sandbox temp dir {self.tmpdir} in place", out)
        self.assertIn("TMPDIR differs from the one the run's sandbox was set up under", out)
        self.assertNotIn("refusing to remove", out)
        self.assertFalse(self.control.exists())
        self.assertTrue(self.tmpdir.is_dir())
        code, out = self.stop()
        self.assertEqual(code, 0, out)
        self.assertIn(f"removed sandbox temp dir {self.tmpdir}\n", out)
        self.assertFalse(self.tmpdir.exists())

    def test_a_teardown_that_raises_exits_2_naming_the_failure(self) -> None:
        self.write_record()
        failure = PmError(f"could not remove sandbox control dir {self.control}: busy")
        with mock.patch.object(campaign.sandbox_mod, "stop_server", side_effect=failure):
            code, out = self.stop()
        self.assertEqual(code, 2, out)
        self.assertIn(f"sandbox: not torn down (could not remove sandbox control dir {self.control}: busy)\n", out)

    def test_an_unconfirmed_kill_skips_the_teardown_and_exits_2(self) -> None:
        self.write_record()
        with (
            mock.patch.object(campaign.sessions, "kill_server", return_value=[self.session]),
            mock.patch.object(campaign.sandbox_mod, "stop_server") as stop,
        ):
            code, out = self.stop()
        self.assertEqual(code, 2, out)
        stop.assert_not_called()
        self.assertIn(f"tmux server {CHILD_SOCKET} still has sessions: {self.session}", out)
        self.assertIn("sandbox: not torn down (the server was not confirmed gone", out)
        self.assertTrue((self.control / "record.json").is_file())
        self.assertTrue(self.tmpdir.is_dir())

    def test_a_control_dir_whose_record_cannot_be_read_is_stopped_and_left_for_removal_by_hand(self) -> None:
        (self.control / "record.json").write_text("not json\n", encoding="utf-8")
        code, out = self.stop()
        self.assertEqual(code, 2, out)
        self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)
        self.assertFalse(sessions.server_running(CHILD_SOCKET))
        self.assertTrue((campaign.campaign_dir(self.child) / "pane.txt").is_file())
        self.assertIn(f"sandbox: not torn down (no readable sandbox record in {self.control} (", out)
        self.assertIn(f"remove it by hand: rm -rf {self.control})\n", out)
        self.assertTrue((self.control / "record.json").is_file())
        self.assertTrue(self.tmpdir.is_dir())
        # A re-run is not blocked by it either.
        code, out = self.stop()
        self.assertEqual(code, 2, out)
        self.assertIn(f"rm -rf {self.control})\n", out)

    def test_a_record_less_control_dir_is_torn_down_from_what_the_launch_created(self) -> None:
        # A teardown that failed partway, or a launch that never wrote the record: the log alone.
        (self.control / "srt.log").write_text("log\n", encoding="utf-8")
        code, out = self.stop()
        self.assertEqual(code, 0, out)
        self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)
        self.assertIn(f"removed sandbox temp dir {self.tmpdir}\n", out)
        self.assertIn(f"removed sandbox control dir {self.control}\n", out)
        self.assertFalse(self.control.exists())
        self.assertFalse(self.tmpdir.exists())
        # A re-run reports both already gone.
        code, out = self.stop()
        self.assertEqual(code, 0, out)
        self.assertIn(f"sandbox temp dir already gone: {self.tmpdir}\n", out)
        self.assertIn(f"sandbox control dir already gone: {self.control}\n", out)

    def test_an_unsandboxed_child_meeting_a_leftover_record_is_refused_naming_the_remedy(self) -> None:
        manifest = json.loads(campaign.manifest_path(self.child).read_text(encoding="utf-8"))
        del manifest["sandbox"]
        campaign.manifest_path(self.child).write_text(json.dumps(manifest), encoding="utf-8")
        other = str(self.repo.parent / "earlier-wt")
        sandbox.write_record(self.control, worktree=other, socket=CHILD_SOCKET, tmpdir=str(self.tmpdir), srt_pid=None)
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.child), "--reason", "test"])
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"names worktree {other!r}", err)
        self.assertIn("this child's manifest records no sandbox", err)
        self.assertIn(f"rm -rf {self.control}) and re-run stop-child", err)
        self.assertEqual(_child_tmux(CHILD_SOCKET, "has-session", "-t", self.session).returncode, 0)
        # A sandboxed manifest's refusal names no such remedy: the record may be its own server's.
        manifest["sandbox"] = {"policy": "x", "policy_sha256": "0" * 64, "socket": CHILD_SOCKET, "control_dir": "/x"}
        campaign.manifest_path(self.child).write_text(json.dumps(manifest), encoding="utf-8")
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.child), "--reason", "test"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("refusing to kill the server\n", err)
        self.assertNotIn("rm -rf", err)

    def test_a_record_less_control_dir_still_requires_the_server_mark(self) -> None:
        sessions.set_server_environment(CHILD_SOCKET, "PM_CHILD_WORKTREE", str(self.repo.parent / "other-wt"))
        code, out, err = self.run_cli(["stop-child", "--worktree", str(self.child), "--reason", "test"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("was not launched for", err)
        self.assertEqual(_child_tmux(CHILD_SOCKET, "has-session", "-t", self.session).returncode, 0)
        self.assertTrue(self.control.is_dir())


class TestStopChildSocketNamesTheSandboxRefuses(CampaignTestCase):
    """A manifest without a sandbox block whose socket the sandbox's naming
    rules refuse: never sandboxed, so stopped as an unsandboxed child."""

    def test_an_unsandboxed_child_on_such_a_socket_is_stopped_and_a_sandboxed_one_refused(self) -> None:
        socket = "pm-x\x01y"
        with self.assertRaises(PmError):
            sandbox.control_dir(self.repo / ".git", socket)
        child = make_linked_worktree(self.repo, self.worktree, "node-1").resolve()
        directory = campaign.campaign_dir(child)
        directory.mkdir(parents=True)
        manifest = {
            "socket": socket,
            "session": "campaign-node-1-000000",
            "branch": "node-1",
            "base": self.base_commit,
            "worktree": str(child),
            "launcher": str(directory / "launcher.md"),
            "launched_at": "2026-10-25T00:00:00Z",
        }
        campaign.manifest_path(child).write_text(json.dumps(manifest), encoding="utf-8")
        tmux = {
            "list_sessions_on": mock.DEFAULT,
            "server_environment": mock.DEFAULT,
            "session_exists": mock.DEFAULT,
            "capture_text": mock.DEFAULT,
            "kill_server": mock.DEFAULT,
        }
        with mock.patch.multiple(campaign.sessions, **tmux) as fakes:
            fakes["list_sessions_on"].return_value = [manifest["session"]]
            fakes["server_environment"].return_value = str(child)
            fakes["session_exists"].return_value = True
            fakes["capture_text"].return_value = "pane\n"
            fakes["kill_server"].return_value = []
            outcome = campaign.stop_child(child)
            fakes["kill_server"].assert_called_once_with(socket)
            self.assertTrue(outcome.server_gone)
            self.assertTrue(outcome.sandbox_stopped)
            self.assertEqual(outcome.sandbox_notes, [])

            manifest["sandbox"] = {"policy": "x", "policy_sha256": "0" * 64, "socket": socket, "control_dir": "/x"}
            campaign.manifest_path(child).write_text(json.dumps(manifest), encoding="utf-8")
            fakes["kill_server"].reset_mock()
            with self.assertRaises(PmError) as raised:
                campaign.stop_child(child)
            self.assertIn("invalid tmux socket name for a sandbox", str(raised.exception))
            fakes["kill_server"].assert_not_called()


class TestStopChildTempDirLookup(CampaignTestCase):
    """The conventional sandbox temp path is looked up only for a sandboxed
    child with no record, and a lookup that fails keeps the stop outcome."""

    def setUp(self) -> None:
        super().setUp()
        self.child = make_linked_worktree(self.repo, self.worktree, "node-1").resolve()
        directory = campaign.campaign_dir(self.child)
        directory.mkdir(parents=True)
        self.manifest = {
            "socket": CHILD_SOCKET,
            "session": "campaign-node-1-000000",
            "branch": "node-1",
            "base": self.base_commit,
            "worktree": str(self.child),
            "launcher": str(directory / "launcher.md"),
            "launched_at": "2026-10-25T00:00:00Z",
        }

    def stop_with_no_usable_temp_dir(self) -> tuple[campaign.StopChildOutcome, mock.MagicMock, mock.MagicMock]:
        campaign.manifest_path(self.child).write_text(json.dumps(self.manifest), encoding="utf-8")
        tmux = dict.fromkeys(
            ("list_sessions_on", "server_environment", "session_exists", "capture_text", "kill_server"), mock.DEFAULT
        )
        failure = FileNotFoundError("No usable temporary directory found")
        with (
            mock.patch.multiple(campaign.sessions, **tmux) as fakes,
            mock.patch.object(tempfile, "gettempdir", side_effect=failure) as gettempdir,
        ):
            fakes["list_sessions_on"].return_value = [self.manifest["session"]]
            fakes["server_environment"].return_value = str(self.child)
            fakes["session_exists"].return_value = True
            fakes["capture_text"].return_value = "pane\n"
            fakes["kill_server"].return_value = []
            outcome = campaign.stop_child(self.child)
        return outcome, gettempdir, fakes["kill_server"]

    def test_an_unsandboxed_stop_never_looks_up_the_temp_dir(self) -> None:
        outcome, gettempdir, kill_server = self.stop_with_no_usable_temp_dir()
        gettempdir.assert_not_called()
        kill_server.assert_called_once_with(CHILD_SOCKET)
        self.assertTrue(outcome.server_gone)
        self.assertTrue(outcome.sandbox_stopped)
        self.assertEqual(outcome.sandbox_notes, [])

    def test_a_failed_lookup_is_a_reported_sandbox_error_and_the_stop_outcome_is_kept(self) -> None:
        # A sandboxed manifest whose control directory is already gone.
        self.manifest["sandbox"] = {
            "policy": "x",
            "policy_sha256": "0" * 64,
            "socket": CHILD_SOCKET,
            "control_dir": "/x",
        }
        outcome, gettempdir, kill_server = self.stop_with_no_usable_temp_dir()
        gettempdir.assert_called()
        kill_server.assert_called_once_with(CHILD_SOCKET)
        self.assertTrue(outcome.server_gone)
        self.assertFalse(outcome.sandbox_stopped)
        self.assertIn("no temp directory is usable (No usable temporary directory found)", outcome.sandbox_error)


_SANDBOX_SOCKET_NUMBERS = iter(range(1, 1000))
SANDBOX_PROBE = Path(__file__).resolve().parents[1] / "references" / "sandbox-probe.sh"


class TestSandboxProbeFile(unittest.TestCase):
    def test_the_probe_names_exactly_the_variables_the_toolkit_keeps_out_of_a_seat(self) -> None:
        text = SANDBOX_PROBE.read_text(encoding="utf-8")
        listed = re.search(r'^DROPPED="([^"]*)"', text, re.M).group(1).split()
        self.assertEqual(listed, list(sessions.SEAT_ENV_DROPPED))
        self.assertEqual(subprocess.run(["sh", "-n", str(SANDBOX_PROBE)]).returncode, 0)


@requires_srt
class TestSandboxedChild(SandboxLaunchTestCase):
    """A child launched with `--sandbox` on a socket of its own, under the
    minimal base policy (nothing beyond the derived grants)."""

    def setUp(self) -> None:
        super().setUp()
        self.socket = register_sandbox_socket(f"{SANDBOX_TEST_SOCKET_PREFIX}-child{next(_SANDBOX_SOCKET_NUMBERS)}")
        self.control = self.control_dir(self.socket)
        self.tmpdir = self.sandbox_tmpdir(self.socket)
        self.worktree_real = Path(os.path.realpath(self.repo.parent)) / "node-wt"
        self.addCleanup(self._teardown_sandbox)

    def _teardown_sandbox(self) -> None:
        try:
            record = sandbox.read_record(self.control)
            sandbox.stop_server(socket=self.socket, record=record)
        except PmError:
            pass
        sessions.kill_server(self.socket)
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.control.parent, ignore_errors=True)

    def sandbox_argv(self, harness: Path) -> list[str]:
        return self.launch_argv(harness, socket=self.socket) + ["--sandbox", str(self.policy)]

    def assert_torn_down(self) -> None:
        self.assertFalse(sessions.server_running(self.socket))
        self.assertFalse(self.tmpdir.exists())
        self.assertFalse(self.control.exists())

    def test_a_sandboxed_launch_confines_the_child_and_stop_child_tears_it_down(self) -> None:
        # The brief pins the policy, so the launch also shows a matching pin is accepted.
        self.write_brief(self.pin(self.policy))
        pm_dir = self.worktree_real / ".pm"
        probes = pm_dir / "probes.txt"
        leak = self.worktree_real.parent / "leak"
        harness = write_fake_harness(
            self.repo.parent / "fake-child.sh",
            sandbox_probe_child_script(
                out=probes, leak=leak, control_probe=self.control / "x", inside=pm_dir / "inside"
            ),
        )
        with mock.patch.dict(os.environ, {"PM_RUN_TOKEN": "caller-token-must-not-leak"}):
            code, out, err = self.run_cli(self.sandbox_argv(harness))
        self.assertEqual(code, 0, out + err)
        self.assert_socket_untouched()
        worktree = self.worktree.resolve()
        self.assertEqual(worktree, self.worktree_real)

        # The server: sandboxed, and marked as this child's.
        self.assertEqual(sessions.server_environment(self.socket, "SANDBOX_RUNTIME"), "1")
        self.assertEqual(sessions.server_environment(self.socket, "PM_CHILD_WORKTREE"), str(worktree))
        # The effective policy, the record and the manifest's sandbox block.
        policy = json.loads((self.control / "policy.json").read_text(encoding="utf-8"))
        self.assertIn({"path": str(worktree), "literal": True}, policy["filesystem"]["allowWrite"])
        self.assertIn({"path": str(worktree / ".git"), "literal": True}, policy["filesystem"]["denyWrite"])
        self.assertIn(
            {"path": str(self.prompt_file.resolve().parent), "literal": True}, policy["filesystem"]["allowRead"]
        )
        record = sandbox.read_record(self.control)
        self.assertEqual(record["worktree"], str(worktree))
        manifest = json.loads(campaign.manifest_path(worktree).read_text(encoding="utf-8"))
        self.assertEqual(list(manifest), [*campaign._MANIFEST_FIELDS, "dossier_commit", "node_dir", "sandbox"])
        block = manifest["sandbox"]
        self.assertEqual(set(block), {"policy", "policy_sha256", "socket", "control_dir"})
        for key, value in block.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)
                self.assertTrue(value)
        self.assertEqual(block["control_dir"], str(self.control))
        self.assertEqual(block["policy_sha256"], hashlib.sha256(self.policy.read_bytes()).hexdigest())
        # The attach policy writes nothing, and the watch line attaches under it.
        attach = json.loads((self.control / "attach-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(attach["filesystem"]["allowWrite"], [])
        session = manifest["session"]
        self.assertIn(
            f"\nwatch: srt -s {self.control}/attach-policy.json tmux -L {self.socket} attach -t {session}\n", out
        )

        # What the fake child found from inside.
        self.assertTrue(self._wait_for(lambda: probes.is_file() and "done" in probes.read_text(encoding="utf-8")))
        found = dict(line.split("=", 1) for line in probes.read_text(encoding="utf-8").splitlines() if "=" in line)
        self.assertNotEqual(found["leak_rc"], "0")
        self.assertEqual(found["leak_exists"], "no")
        self.assertFalse(leak.exists())
        self.assertNotEqual(found["control_rc"], "0")
        self.assertFalse((self.control / "x").exists())
        self.assertEqual(found["inside_rc"], "0")
        self.assertTrue((pm_dir / "inside").is_file())
        self.assertEqual(found["tmpdir"], record["tmpdir"])
        self.assertEqual(found["token"], "unset")

        # stop-child: the server, srt, the temp and control directories all go.
        srt_pid = record["srt_pid"]
        code, out, err = self.run_cli(["stop-child", "--worktree", str(worktree), "--reason", "node judged"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"killed tmux server {self.socket}", out)
        self.assertIn(f"removed sandbox temp dir {record['tmpdir']}\n", out)
        self.assertIn(f"removed sandbox control dir {self.control}\n", out)
        self.assert_torn_down()
        with self.assertRaises(ProcessLookupError):
            os.kill(srt_pid, 0)

        # A re-run reports both directories already gone.
        code, out, err = self.run_cli(["stop-child", "--worktree", str(worktree), "--reason", "again"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"sandbox temp dir already gone: {record['tmpdir']}\n", out)
        self.assertIn(f"sandbox control dir already gone: {self.control}\n", out)

    def test_the_toolkits_boundary_probe_passes_inside_a_sandboxed_child(self) -> None:
        pm_dir = self.worktree_real / ".pm"
        out_dir, status = pm_dir / "probe", pm_dir / "probe-status"
        harness = write_fake_harness(
            self.repo.parent / "fake-child.sh",
            "\n".join(
                [
                    "echo FAKE_HARNESS_READY",
                    f'mkdir -p "{pm_dir}"; sh "{SANDBOX_PROBE}" "{out_dir}" >/dev/null 2>&1; echo $? > "{status}"',
                    "exec cat -",
                ]
            ),
        )
        subprocess.run(["git", "-C", str(self.dossier), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.dossier),
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "d",
            ],
            check=True,
        )
        with mock.patch.dict(os.environ, {"PM_RUN_TOKEN": "caller-token-must-not-leak"}):
            code, out, err = self.run_cli(self.sandbox_argv(harness))
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self._wait_for(lambda: status.is_file() and status.read_text(encoding="utf-8").strip()))
        rows = [line.split("\t") for line in (out_dir / "results.tsv").read_text(encoding="utf-8").splitlines()[1:]]
        checked = [row for row in rows if row[0] in {"ALLOW", "DENY"}]
        self.assertEqual([row for row in checked if row[3] != "ok"], [], "every ALLOW and DENY row holds in a sandbox")
        self.assertGreaterEqual({row[0] for row in checked}, {"ALLOW", "DENY"})
        self.assertGreaterEqual(len(checked), 10)
        self.assertEqual(status.read_text(encoding="utf-8").strip(), "0")

    def test_a_server_failing_verification_is_torn_down_and_the_leftovers_named(self) -> None:
        # Denying the worktree's .pm/ fails the positive probe.
        policy = minimal_base_policy()
        policy["filesystem"]["denyWrite"] = [str(self.worktree_real / ".pm")]
        write_base_policy(self.policy, policy)
        code, out, err = self.run_cli(self.sandbox_argv(self.harness))
        self.assertEqual(code, 2, out + err)
        self.assertIn("child sandbox did not start (positive probe failed", err)
        self.assertIn(f"removed sandbox temp dir {self.tmpdir}", err)
        self.assertIn(f"removed sandbox control dir {self.control}", err)
        self.assertIn(f"branch node-1 and worktree {self.worktree_real} are left behind", err)
        self.assertTrue(self.worktree_real.is_dir())
        self.assertIn("refs/heads/node-1", self.branches())
        self.assertFalse(campaign.manifest_path(self.worktree_real).exists(), "no session, so no manifest")
        self.assert_torn_down()
        self.assert_socket_untouched()

    def test_a_session_ending_before_readiness_tears_the_sandbox_down(self) -> None:
        harness = write_fake_harness(self.repo.parent / "fake-child.sh", "exit 3")
        code, out, err = self.run_cli(self.sandbox_argv(harness))
        self.assertEqual(code, 2, out + err)
        self.assertIn("pm: error:", err)
        self.assertIn(f"removed sandbox temp dir {self.tmpdir}", err)
        self.assertIn(f"removed sandbox control dir {self.control}", err)
        self.assertIn(f"branch node-1 and worktree {self.worktree_real} are left behind", err)
        self.assert_torn_down()
        self.assert_socket_untouched()


if __name__ == "__main__":
    unittest.main()

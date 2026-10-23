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
"""

from __future__ import annotations

import argparse
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
from pm_test_helpers import TEST_TMUX_SOCKET, PmTestCase, trigger_wait, write_fake_harness

from pm_lib import PmError
from pm_lib import campaign
from pm_lib import cli
from pm_lib import prompts
from pm_lib import sessions
from pm_lib import slice_ops
from pm_lib import state as state_mod

_HAS_TMUX = shutil.which("tmux") is not None
CHILD_SOCKET = f"{TEST_TMUX_SOCKET}-child"
_QUESTION_TEXT = "Which node order?\nA: data first\nB: model first\nRecommendation: A"
_RESULT_JSON = '{"status": "complete", "run_id": null, "summary": "fake child finished"}'


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
        },
        "wait-child": {"--worktree", "--wait"},
        "answer-child": {"--worktree", "--text"},
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
        with mock.patch.object(campaign.sessions, "capture_to", side_effect=OSError("disk full")):
            code, out, err = self.run_cli(["stop-child", "--worktree", str(self.worktree), "--reason", "test"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("pane: not captured (disk full)", out)
        self.assertIn(f"killed tmux server {CHILD_SOCKET}", out)

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
        self.assertEqual(set(manifest), set(campaign._MANIFEST_FIELDS))
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
        self.assertEqual(deliver.call_args.args[:3], (directory, "cm-", session))
        answer_path = directory / "cm-answer-1.md"
        self.assertEqual(answer_path.read_text(encoding="utf-8"), "A, data first")
        self.assertTrue((directory / "cm-question-1.md").is_file())
        self.assertFalse((directory / "cm-question.md").exists())
        self.assertIn(f"answered: {answer_path}", out)
        self.assertIn(f"question archived: {directory / 'cm-question-1.md'}", out)
        answer_pointer = prompts.render_answer_pointer(answer_path)
        self.assertTrue(self._wait_for(lambda: answer_pointer in self._input_lines()), self._input_lines())
        self.assert_socket_untouched()

        # The completion file ends the next wait early, with its status.
        start = time.monotonic()
        code, out, err = self.run_cli(["wait-child", "--worktree", str(worktree), "--wait", "30"])
        self.assertLess(time.monotonic() - start, 20.0)
        self.assertEqual(code, 0, err)
        self.assertIn("result present: True (status=complete)", out)
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


if __name__ == "__main__":
    unittest.main()

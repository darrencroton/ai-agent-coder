"""Protected behaviours: the `srt` sandbox mechanics in `pm_lib.sandbox` and
the server-scoped `sessions` helpers they rest on.

Policy loading, derivation, the binaries check, the record, the temp dir and
`wrap_command` run without `srt`. The `sessions` helpers run with tmux alone.
Everything that starts a real sandboxed server is gated on `srt`, `tmux` and
macOS (`requires_srt`), and each such test drives a `<TEST_TMUX_SOCKET>-sb*`
socket of its own, never the suite's shared server.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Imported for its side effect as much as its helpers: pm_test_helpers pins
# PM_TMUX_SOCKET and the ledger directory for this process.
from pm_test_helpers import (  # noqa: E402
    TEST_TMUX_SOCKET,
    PmTestCase,
    make_linked_worktree,
    minimal_base_policy,
    requires_srt,
    write_base_policy,
    write_fake_harness,
)

from pm_lib import PmError  # noqa: E402
from pm_lib import ledger  # noqa: E402
from pm_lib import sandbox  # noqa: E402
from pm_lib import sessions  # noqa: E402

_HAS_TMUX = shutil.which("tmux") is not None
_SOCKET_NUMBERS = itertools.count(1)


def _sandbox_socket() -> str:
    """A fresh `<TEST_TMUX_SOCKET>-sb<N>` socket for one test."""
    return f"{TEST_TMUX_SOCKET}-sb{next(_SOCKET_NUMBERS)}"


def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(path))


def _literal(path: Path | str) -> dict:
    return {"path": str(path), "literal": True}


def _socket_grant(socket: str) -> str:
    path = sessions.socket_path(socket)
    return str(_real(path.parent) / path.name)


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = _real(self._tmp.name)


# --- load_base_policy ------------------------------------------------------------


class TestLoadBasePolicy(TempDirTestCase):
    def _write(self, policy) -> Path:
        return write_base_policy(self.tmp / "sandbox.json", policy)

    def _policy_with_write(self, *entries) -> dict:
        policy = minimal_base_policy()
        policy["filesystem"]["allowWrite"] = list(entries)
        return policy

    def assertRefused(self, path: Path, *fragments: str) -> None:
        with self.assertRaises(PmError) as ctx:
            sandbox.load_base_policy(path)
        message = str(ctx.exception)
        self.assertIn(str(path), message)
        for fragment in fragments:
            self.assertIn(fragment, message)

    def test_returns_the_parsed_policy_and_no_warnings(self) -> None:
        policy = minimal_base_policy()
        policy["filesystem"]["allowWrite"] = ["~/.codex/logs_*", str(self.tmp / "state"), _literal("~/.copilot")]
        loaded, warnings = sandbox.load_base_policy(self._write(policy))
        self.assertEqual(loaded, policy)
        self.assertEqual(warnings, [])

    def test_an_unreadable_file_is_refused(self) -> None:
        self.assertRefused(self.tmp / "absent.json", "unreadable")

    def test_a_non_json_file_is_refused(self) -> None:
        path = self.tmp / "sandbox.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertRefused(path, "not valid JSON")

    def test_a_non_object_top_level_is_refused(self) -> None:
        self.assertRefused(self._write([minimal_base_policy()]), "not a JSON object")

    def test_a_missing_network_or_filesystem_object_is_refused(self) -> None:
        for key in ("network", "filesystem"):
            with self.subTest(key=key):
                policy = minimal_base_policy()
                del policy[key]
                self.assertRefused(self._write(policy), repr(key))
                policy[key] = []
                self.assertRefused(self._write(policy), repr(key))

    def test_options_that_defeat_the_boundary_are_refused(self) -> None:
        cases = (
            (("network", "allowAllUnixSockets"), "allowAllUnixSockets"),
            ((None, "allowAppleEvents"), "allowAppleEvents"),
            (("filesystem", "disabled"), "filesystem.disabled"),
        )
        for (section, key), fragment in cases:
            with self.subTest(key=key):
                policy = minimal_base_policy()
                (policy[section] if section else policy)[key] = True
                self.assertRefused(self._write(policy), fragment)

    def test_a_relative_write_entry_is_refused(self) -> None:
        # `~other` is relative to srt, which expands only the caller's `~`.
        for entry in ("state", "./state", "~other/state", _literal("state")):
            with self.subTest(entry=entry):
                self.assertRefused(self._write(self._policy_with_write(entry)), "relative")

    def test_a_malformed_write_entry_is_refused(self) -> None:
        for entry in (42, {"path": "/x"}, {"path": "/x", "literal": False}):
            with self.subTest(entry=entry):
                self.assertRefused(self._write(self._policy_with_write(entry)), "allowWrite entry")

    def test_a_grant_of_the_filesystem_root_is_refused(self) -> None:
        for entry in ("/", "//", _literal("/")):
            with self.subTest(entry=entry):
                self.assertRefused(self._write(self._policy_with_write(entry)), "the filesystem root")

    def test_a_grant_of_the_home_directory_is_refused_however_spelled(self) -> None:
        link = self.tmp / "home-link"
        link.symlink_to(Path.home())
        for entry in ("~", "~/", "~/.", str(Path.home()), str(link), _literal("~"), "~/*"):
            with self.subTest(entry=entry):
                self.assertRefused(self._write(self._policy_with_write(entry)), "the home directory")

    def test_a_grant_of_the_toolkit_checkout_is_refused(self) -> None:
        root = sandbox.toolkit_root()
        self.assertTrue((root / "skills" / "project-manager").is_dir(), root)
        self.assertRefused(self._write(self._policy_with_write(str(root))), "the toolkit checkout")

    def test_a_grant_of_the_tmux_socket_directory_is_refused(self) -> None:
        directory = sessions.socket_path("x").parent
        self.assertRefused(self._write(self._policy_with_write(str(directory))), "the tmux socket directory")

    def test_the_tmux_socket_directory_honours_tmux_tmpdir(self) -> None:
        with mock.patch.dict(os.environ, {"TMUX_TMPDIR": str(self.tmp)}):
            directory = self.tmp / f"tmux-{os.getuid()}"
            self.assertRefused(self._write(self._policy_with_write(str(directory))), "the tmux socket directory")

    def test_weaker_network_isolation_and_local_binding_each_warn(self) -> None:
        policy = minimal_base_policy()
        policy["enableWeakerNetworkIsolation"] = True
        policy["network"]["allowLocalBinding"] = True
        loaded, warnings = sandbox.load_base_policy(self._write(policy))
        self.assertEqual(loaded, policy)
        self.assertEqual(len(warnings), 2)
        self.assertIn("enableWeakerNetworkIsolation", warnings[0])
        self.assertIn("trustd", warnings[0])
        self.assertIn("allowLocalBinding", warnings[1])
        self.assertIn("loopback", warnings[1])


# --- derived policies --------------------------------------------------------------


class DerivationTestCase(PmTestCase):
    """A repository with a linked worktree on `node/x`."""

    def setUp(self) -> None:
        super().setUp()
        self.branch = "node/x"
        self.worktree = _real(make_linked_worktree(self.repo, self.repo.parent / "wt", self.branch))
        self.common = _real(self.repo / ".git")
        self.git_dir = self.common / "worktrees" / "wt"
        self.tmpdir = _real(self.repo.parent) / "pm-sandbox-test"
        self.tmpdir.mkdir()
        self.control_root = sandbox.control_root(self.common)
        self.socket = f"{TEST_TMUX_SOCKET}-derive"

    def derive(self, base: dict | None = None, *, worktree: Path | None = None, **overrides) -> dict:
        arguments = dict(
            worktree=worktree or self.worktree,
            branch=self.branch,
            socket=self.socket,
            tmpdir=self.tmpdir,
            control_root=self.control_root,
            pm_inside=False,
        )
        arguments.update(overrides)
        return sandbox.derive_policy(minimal_base_policy() if base is None else base, **arguments)

    def escape_denies(self) -> list[dict]:
        return [
            _literal(self.worktree / ".git"),
            _literal(self.git_dir / "commondir"),
            _literal(self.git_dir / "gitdir"),
            _literal(self.git_dir / "config.worktree"),
            _literal(self.git_dir / "hooks"),
        ]

    def always_denies(self, control_root: Path | None = None) -> list[dict]:
        return [
            _literal(self.common / "config"),
            _literal(self.common / "hooks"),
            _literal(control_root or self.control_root),
            _literal("/tmp/claude"),
            _literal("/private/tmp/claude"),
        ]


class TestDerivePolicy(DerivationTestCase):
    def test_a_linked_worktree_gets_exact_common_dir_grants_and_the_escape_denies(self) -> None:
        base = minimal_base_policy()
        base["filesystem"]["allowWrite"] = ["~/.codex/sessions"]
        base["filesystem"]["denyWrite"] = ["~/.codex/config.toml"]
        policy = self.derive(base)
        heads = self.common / "refs" / "heads"
        logs = self.common / "logs" / "refs" / "heads"
        self.assertEqual(
            policy["filesystem"]["allowWrite"],
            [
                "~/.codex/sessions",
                str(self.worktree),
                str(self.git_dir),
                str(self.common / "objects"),
                _literal(heads / "node" / "x"),
                _literal(heads / "node" / "x.lock"),
                _literal(logs / "node" / "x"),
                _literal(logs / "node" / "x.lock"),
                _literal(self.common / "packed-refs.lock"),
                _socket_grant(self.socket),
                str(self.tmpdir),
            ],
        )
        self.assertEqual(
            policy["filesystem"]["denyWrite"],
            ["~/.codex/config.toml", *self.always_denies(), *self.escape_denies(), _literal(self.git_dir / "pm")],
        )

    def test_the_state_root_deny_is_mode_b_only_and_the_ledger_grant_mode_c_only(self) -> None:
        outside = self.derive(pm_inside=False)
        inside = self.derive(pm_inside=True)
        ledger_grant = str(_real(ledger.ledger_dir()))
        self.assertIn(_literal(self.git_dir / "pm"), outside["filesystem"]["denyWrite"])
        self.assertNotIn(ledger_grant, outside["filesystem"]["allowWrite"])
        self.assertNotIn(_literal(self.git_dir / "pm"), inside["filesystem"]["denyWrite"])
        self.assertEqual(inside["filesystem"]["allowWrite"][-1], ledger_grant)
        for policy in (outside, inside):
            for deny in self.escape_denies():
                self.assertIn(deny, policy["filesystem"]["denyWrite"])

    def test_a_plain_checkout_gets_no_common_dir_grants_and_no_escape_denies(self) -> None:
        policy = self.derive(worktree=self.repo, branch="main")
        repo = _real(self.repo)
        self.assertEqual(
            policy["filesystem"]["allowWrite"],
            [str(repo), str(repo / ".git"), _socket_grant(self.socket), str(self.tmpdir)],
        )
        self.assertEqual(policy["filesystem"]["denyWrite"], [*self.always_denies(), _literal(repo / ".git" / "pm")])

    def test_reads_socket_pty_and_required_lists(self) -> None:
        base = {"network": {}, "filesystem": {"allowRead": ["/opt/data"]}, "allowPty": False}
        extra = self.repo.parent / "launcher"
        policy = self.derive(base, extra_read=[extra])
        self.assertEqual(
            policy["filesystem"]["allowRead"],
            ["/opt/data", str(self.worktree), str(self.common), str(sandbox.toolkit_root()), str(extra)],
        )
        self.assertEqual(policy["network"]["allowUnixSockets"], [_socket_grant(self.socket)])
        self.assertIs(policy["allowPty"], True)
        for key in ("allowedDomains", "deniedDomains"):
            self.assertEqual(policy["network"][key], [])
        self.assertEqual(policy["filesystem"]["denyRead"], [])
        self.assertIsInstance(policy["filesystem"]["denyWrite"], list)

    def test_the_socket_is_granted_under_its_real_directory(self) -> None:
        # srt resolves only paths that exist and the socket does not yet,
        # so the `/tmp` spelling would match nothing under Seatbelt.
        policy = self.derive()
        grant = _socket_grant(self.socket)
        self.assertEqual(Path(grant).name, self.socket)
        self.assertEqual(Path(grant).parent, _real(sessions.socket_path(self.socket).parent))
        self.assertIn(grant, policy["filesystem"]["allowWrite"])
        self.assertIn(grant, policy["network"]["allowUnixSockets"])

    def test_the_base_is_not_mutated(self) -> None:
        base = minimal_base_policy()
        base["network"]["allowUnixSockets"] = ["/var/run/x.sock"]
        snapshot = copy.deepcopy(base)
        self.derive(base, pm_inside=True)
        sandbox.derive_reviewer_policy(base, worktree=self.worktree, tmpdir=self.tmpdir, control_root=self.control_root)
        sandbox.derive_attach_policy(base, socket=self.socket, control_root=self.control_root)
        self.assertEqual(base, snapshot)

    def test_a_base_grant_covering_reaching_into_or_above_the_repository_is_refused(self) -> None:
        cases = (
            (str(self.worktree), "is the worktree"),
            (str(self.worktree / "src"), "is inside the worktree"),
            (str(self.repo.parent), "is an ancestor of the worktree"),
            (str(self.common), "the common git dir"),
            (_literal(self.common / "objects"), "is inside the common git dir"),
            (str(self.repo.parent / "*"), "is an ancestor of the worktree"),
        )
        for entry, fragment in cases:
            with self.subTest(entry=entry):
                base = minimal_base_policy()
                base["filesystem"]["allowWrite"] = [entry]
                with self.assertRaises(PmError) as ctx:
                    self.derive(base)
                self.assertIn(fragment, str(ctx.exception))
                with self.assertRaises(PmError):
                    sandbox.derive_reviewer_policy(
                        base, worktree=self.worktree, tmpdir=self.tmpdir, control_root=self.control_root
                    )


class TestDeriveReviewerAndAttachPolicies(DerivationTestCase):
    def test_the_reviewer_writes_only_its_temp_dir_and_reaches_no_tmux_socket(self) -> None:
        base = minimal_base_policy()
        base["filesystem"]["allowWrite"] = ["~/.codex/sessions"]
        extra = self.repo.parent / "plan-dir"
        policy = sandbox.derive_reviewer_policy(
            base, worktree=self.worktree, tmpdir=self.tmpdir, control_root=self.control_root, extra_read=[extra]
        )
        self.assertEqual(policy["filesystem"]["allowWrite"], ["~/.codex/sessions", str(self.tmpdir)])
        self.assertEqual(policy["filesystem"]["denyWrite"], [*self.always_denies(), *self.escape_denies()])
        self.assertEqual(
            policy["filesystem"]["allowRead"],
            [str(self.worktree), str(self.common), str(sandbox.toolkit_root()), str(extra)],
        )
        self.assertIs(policy["allowPty"], True)
        self.assertNotIn("allowUnixSockets", policy["network"])

        base["network"]["allowUnixSockets"] = ["/var/run/x.sock"]
        policy = sandbox.derive_reviewer_policy(
            base, worktree=self.worktree, tmpdir=self.tmpdir, control_root=self.control_root
        )
        self.assertEqual(policy["network"]["allowUnixSockets"], ["/var/run/x.sock"])

    def test_the_attach_client_can_write_nothing(self) -> None:
        base = minimal_base_policy()
        base["network"]["allowedDomains"] = ["api.anthropic.com"]
        base["network"]["allowLocalBinding"] = False
        base["filesystem"]["denyRead"] = ["~/.ssh"]
        base["filesystem"]["allowRead"] = ["~/.ssh/known_hosts"]
        base["filesystem"]["allowWrite"] = ["~/.codex/sessions"]
        base["filesystem"]["denyWrite"] = ["~/.claude/settings.json"]
        base["enableWeakerNetworkIsolation"] = True
        policy = sandbox.derive_attach_policy(base, socket=self.socket, control_root=self.control_root)
        self.assertEqual(
            policy,
            {
                "network": {
                    "allowedDomains": ["api.anthropic.com"],
                    "deniedDomains": [],
                    "allowLocalBinding": False,
                    "allowUnixSockets": [_socket_grant(self.socket)],
                },
                "filesystem": {
                    "denyRead": ["~/.ssh"],
                    "allowRead": ["~/.ssh/known_hosts"],
                    "allowWrite": [],
                    "denyWrite": [
                        "~/.claude/settings.json",
                        _literal(self.control_root),
                        _literal("/tmp/claude"),
                        _literal("/private/tmp/claude"),
                        _literal("~/.npm/_logs"),
                        _literal("~/.claude/debug"),
                    ],
                },
                "allowPty": True,
            },
        )


# --- check_binaries and srt_version -------------------------------------------------


class TestCheckBinaries(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A fake srt package: bin/srt -> lib/sandbox-runtime/dist/cli.js, the
        # package.json one directory above the entry point, as installed.
        self.package = self.tmp / "lib" / "sandbox-runtime"
        (self.package / "dist").mkdir(parents=True)
        (self.package / "package.json").write_text("{}", encoding="utf-8")
        entry = self.package / "dist" / "cli.js"
        entry.write_text("", encoding="utf-8")
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "srt").symlink_to(entry)
        self.node = self.tmp / "cellar" / "node" / "bin" / "node"
        self.node.parent.mkdir(parents=True)
        self.node.write_text("", encoding="utf-8")
        found = {"srt": str(self.tmp / "bin" / "srt"), "node": str(self.node)}
        patcher = mock.patch.object(sandbox.shutil, "which", side_effect=found.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _policy(self, *entries) -> dict:
        policy = minimal_base_policy()
        policy["filesystem"]["allowWrite"] = list(entries)
        return policy

    def test_the_package_root_is_the_directory_holding_package_json_above_the_real_entry_point(self) -> None:
        self.assertEqual(sandbox.srt_package_root(), self.package)

    def test_unrelated_grants_pass(self) -> None:
        sandbox.check_binaries(self._policy(str(self.tmp / "state"), "~/.codex/sessions"))

    def test_a_grant_on_above_or_inside_the_srt_package_is_refused(self) -> None:
        for entry in (
            str(self.package),
            str(self.tmp / "lib"),
            str(self.package / "dist"),
            _literal(self.package / "dist" / "cli.js"),
            str(self.tmp / "lib" / "*"),
        ):
            with self.subTest(entry=entry):
                with self.assertRaises(PmError) as ctx:
                    sandbox.check_binaries(self._policy(entry))
                self.assertIn("srt's installation", str(ctx.exception))

    def test_a_grant_on_or_above_node_is_refused(self) -> None:
        for entry in (str(self.node), str(self.node.parent), str(self.tmp / "cellar")):
            with self.subTest(entry=entry):
                with self.assertRaises(PmError) as ctx:
                    sandbox.check_binaries(self._policy(entry))
                self.assertIn("the node that runs srt", str(ctx.exception))

    def test_a_missing_srt_is_refused_with_the_install_hint(self) -> None:
        with mock.patch.object(sandbox.shutil, "which", return_value=None):
            with self.assertRaises(PmError) as ctx:
                sandbox.check_binaries(self._policy())
        self.assertEqual(
            str(ctx.exception), "srt is required for a sandboxed run; install @anthropic-ai/sandbox-runtime"
        )


class TestSrtVersion(TempDirTestCase):
    def test_returns_srt_versions_output(self) -> None:
        write_fake_harness(self.tmp / "srt", "echo 9.8.7")
        with mock.patch.dict(os.environ, {"PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(sandbox.srt_version(), "9.8.7")


# --- control directory, record, temp directory -------------------------------------


class TestControlDirAndRecord(TempDirTestCase):
    def test_control_dir_is_under_the_common_git_dir(self) -> None:
        self.assertEqual(sandbox.control_dir(self.tmp, "pm-run-x"), self.tmp / "pm-sandbox" / "pm-run-x")
        for socket in ("", "a/b", ".."):
            with self.subTest(socket=socket), self.assertRaises(PmError):
                sandbox.control_dir(self.tmp, socket)

    def test_the_record_round_trips(self) -> None:
        control = sandbox.control_dir(self.tmp, "pm-run-x")
        fields = {
            "worktree": "/w",
            "branch": "node/x",
            "socket": "pm-run-x",
            "tmpdir": "/t/pm-sandbox-pm-run-x",
            "policy": "/d/sandbox.json",
            "policy_sha256": "0" * 64,
            "srt_version": "0.0.81",
            "srt_pid": 123,
            "server_pid": 456,
            "started_at": "2026-10-25T10:00:00Z",
        }
        path = sandbox.write_record(control, **fields)
        self.assertEqual(path, control / "record.json")
        self.assertEqual(sandbox.read_record(control), fields)

    def test_a_missing_or_malformed_record_is_refused(self) -> None:
        control = self.tmp / "control"
        with self.assertRaises(PmError) as ctx:
            sandbox.read_record(control)
        self.assertIn("missing", str(ctx.exception))
        control.mkdir()
        for text in ("{oops", "[1, 2]"):
            with self.subTest(text=text):
                (control / "record.json").write_text(text, encoding="utf-8")
                with self.assertRaises(PmError) as ctx:
                    sandbox.read_record(control)
                self.assertIn("malformed", str(ctx.exception))


class TestTempDir(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(tempfile, "tempdir", str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_make_creates_once_and_remove_removes_then_reports_gone(self) -> None:
        path = sandbox.make_tmpdir("pm-run-x")
        self.assertEqual(path, self.tmp / "pm-sandbox-pm-run-x")
        self.assertTrue(path.is_dir())
        (path / "nested").mkdir()
        (path / "nested" / "file").write_text("x", encoding="utf-8")
        with self.assertRaises(PmError) as ctx:
            sandbox.make_tmpdir("pm-run-x")
        self.assertIn("already exists", str(ctx.exception))
        self.assertTrue(sandbox.remove_tmpdir(path))
        self.assertFalse(path.exists())
        self.assertFalse(sandbox.remove_tmpdir(path))

    def test_remove_refuses_a_foreign_name_or_parent(self) -> None:
        foreign = self.tmp / "other-dir"
        foreign.mkdir()
        nested = self.tmp / "deeper" / "pm-sandbox-x"
        nested.mkdir(parents=True)
        for path in (foreign, nested):
            with self.subTest(path=path):
                with self.assertRaises(PmError) as ctx:
                    sandbox.remove_tmpdir(path)
                self.assertIn("refusing to remove", str(ctx.exception))
                self.assertTrue(path.is_dir())

    def test_remove_refuses_a_symlink(self) -> None:
        target = self.tmp / "target"
        target.mkdir()
        link = self.tmp / "pm-sandbox-link"
        link.symlink_to(target)
        with self.assertRaises(PmError):
            sandbox.remove_tmpdir(link)
        self.assertTrue(target.is_dir())


# --- wrap_command, inside_sandbox, start_server preconditions -----------------------


class TestWrapCommandAndDetection(unittest.TestCase):
    def test_wrap_command_sets_tmpdir_and_the_cert_bundle_when_present(self) -> None:
        with tempfile.NamedTemporaryFile() as certs, mock.patch.object(sandbox, "SSL_CERT_FILE", Path(certs.name)):
            self.assertEqual(
                sandbox.wrap_command("/c/policy.json", ["codex", "exec", "x y"], tmpdir="/t/run"),
                [
                    "srt",
                    "-s",
                    "/c/policy.json",
                    "env",
                    "TMPDIR=/t/run",
                    f"SSL_CERT_FILE={certs.name}",
                    "codex",
                    "exec",
                    "x y",
                ],
            )
        with mock.patch.object(sandbox, "SSL_CERT_FILE", Path("/nonexistent/cert.pem")):
            self.assertEqual(
                sandbox.wrap_command(Path("/c/policy.json"), ["true"], tmpdir=Path("/t/run")),
                ["srt", "-s", "/c/policy.json", "env", "TMPDIR=/t/run", "true"],
            )

    def test_inside_sandbox_reads_sandbox_runtime(self) -> None:
        with mock.patch.dict(os.environ, {"SANDBOX_RUNTIME": "1"}):
            self.assertTrue(sandbox.inside_sandbox())
        with mock.patch.dict(os.environ, {"SANDBOX_RUNTIME": "0"}):
            self.assertFalse(sandbox.inside_sandbox())
        with mock.patch.dict(os.environ):
            os.environ.pop("SANDBOX_RUNTIME", None)
            self.assertFalse(sandbox.inside_sandbox())

    def test_start_server_is_macos_only(self) -> None:
        with (
            mock.patch.object(sandbox.platform, "system", return_value="Linux"),
            mock.patch.object(sandbox.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(PmError) as ctx:
                sandbox.start_server(
                    socket="s", policy_path=Path("/p"), worktree=Path("/w"), tmpdir=Path("/t"), log_path=Path("/l")
                )
        self.assertEqual(str(ctx.exception), "sandboxing is implemented for macOS only")
        popen.assert_not_called()

    def test_start_server_refuses_a_socket_a_server_already_answers_on(self) -> None:
        with (
            mock.patch.object(sandbox.platform, "system", return_value="Darwin"),
            mock.patch.object(sandbox.sessions, "server_running", return_value=True),
            mock.patch.object(sandbox.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(PmError) as ctx:
                sandbox.start_server(
                    socket="s", policy_path=Path("/p"), worktree=Path("/w"), tmpdir=Path("/t"), log_path=Path("/l")
                )
        self.assertIn("already answers", str(ctx.exception))
        popen.assert_not_called()


# --- sessions helpers ----------------------------------------------------------------


class TestServerRunning(unittest.TestCase):
    def test_asks_the_named_socket_for_its_pid(self) -> None:
        reply = subprocess.CompletedProcess([], 0, "1234\n", "")
        with (
            mock.patch.dict(os.environ, {"PM_TMUX_SOCKET": "other"}),
            mock.patch.object(sessions.subprocess, "run", return_value=reply) as run,
        ):
            self.assertTrue(sessions.server_running("pm-run-x"))
        self.assertEqual(run.call_args.args[0], ["tmux", "-L", "pm-run-x", "display-message", "-p", "#{pid}"])

    def test_no_server_replies_are_false_and_other_failures_raise(self) -> None:
        for detail in ("no server running on /tmp/tmux-1/x", "error connecting to /x (No such file or directory)"):
            with self.subTest(detail=detail):
                reply = subprocess.CompletedProcess([], 1, "", detail)
                with mock.patch.object(sessions.subprocess, "run", return_value=reply):
                    self.assertFalse(sessions.server_running("pm-run-x"))
        reply = subprocess.CompletedProcess([], 1, "", "error connecting to /x (File name too long)")
        with mock.patch.object(sessions.subprocess, "run", return_value=reply):
            with self.assertRaises(PmError) as ctx:
                sessions.server_running("pm-run-x")
        self.assertIn("File name too long", str(ctx.exception))
        with mock.patch.object(sessions.subprocess, "run", side_effect=FileNotFoundError("tmux")):
            with self.assertRaises(PmError):
                sessions.server_running("pm-run-x")

    @unittest.skipUnless(_HAS_TMUX, "tmux is required")
    def test_a_never_started_socket_has_no_server(self) -> None:
        self.assertFalse(sessions.server_running(f"{TEST_TMUX_SOCKET}-sb-never"))


class TestRunShell(unittest.TestCase):
    def test_doubles_hashes_so_the_command_runs_as_written(self) -> None:
        reply = subprocess.CompletedProcess([], 0, "out\n", None)
        with mock.patch.object(sessions.subprocess, "run", return_value=reply) as run:
            self.assertEqual(sessions.run_shell("pm-run-x", "echo '#{pid}' ##"), (0, "out\n"))
        self.assertEqual(run.call_args.args[0], ["tmux", "-L", "pm-run-x", "run-shell", "echo '##{pid}' ####"])

    def test_raises_only_when_tmux_cannot_run(self) -> None:
        with mock.patch.object(sessions.subprocess, "run", side_effect=FileNotFoundError("tmux")):
            with self.assertRaises(PmError):
                sessions.run_shell("pm-run-x", "true")

    @unittest.skipUnless(_HAS_TMUX, "tmux is required")
    def test_a_socket_with_no_server_fails_and_starts_none(self) -> None:
        socket = f"{TEST_TMUX_SOCKET}-sb-noserver"
        self.addCleanup(sessions.kill_server, socket)
        returncode, output = sessions.run_shell(socket, "true")
        self.assertNotEqual(returncode, 0)
        self.assertFalse(sessions.server_running(socket))
        self.assertFalse(sessions.socket_path(socket).exists())

    @unittest.skipUnless(_HAS_TMUX, "tmux is required")
    def test_runs_on_a_live_server_with_its_exit_status_and_literal_hashes(self) -> None:
        socket = f"{TEST_TMUX_SOCKET}-sb-plain"
        self.addCleanup(sessions.kill_server, socket)
        with sessions.scoped_environ("PM_TMUX_SOCKET", socket):
            sessions.start_session("pm-test-sbplain-s01a0", Path(tempfile.gettempdir()), "sleep 15", {})
        self.assertEqual(sessions.run_shell(socket, "echo 'a#b #{pid}'"), (0, "a#b #{pid}\n"))
        self.assertNotEqual(sessions.run_shell(socket, "exit 3")[0], 0)


class TestStartSessionViaServer(unittest.TestCase):
    def test_issues_start_sessions_exact_new_session_through_run_shell(self) -> None:
        env = {"PM_SLICE_ID": "Slice 1", "PM_X": "a 'b' #c"}
        command = "/h/fake harness.sh --flag"
        with mock.patch.object(sessions, "_tmux_or_raise") as direct:
            sessions.start_session("pm-r-s01a0", Path("/r/repo"), command, env)
        expected = direct.call_args.args[0]
        self.assertEqual(
            expected[-1], "unset PM_RUN_TOKEN; PM_SLICE_ID='Slice 1' PM_X='a '\"'\"'b'\"'\"' #c' " + command
        )

        with mock.patch.dict(os.environ, {"PM_RUN_TOKEN": "secret", "PM_TMUX_SOCKET": "other"}):
            seen_token: list[str | None] = []

            def fake_run_shell(socket: str, shell: str) -> tuple[int, str]:
                seen_token.append(os.environ.get("PM_RUN_TOKEN"))
                return 0, ""

            with (
                mock.patch.object(sessions, "run_shell", side_effect=fake_run_shell) as run_shell,
                mock.patch.object(sessions, "_tmux_or_raise") as direct,
                mock.patch.object(sessions, "_run_tmux") as bare,
            ):
                sessions.start_session_via_server("pm-run-x", "pm-r-s01a0", Path("/r/repo"), command, env)
        socket, shell = run_shell.call_args.args
        self.assertEqual(socket, "pm-run-x")
        self.assertEqual(shlex.split(shell), ["tmux", "-L", "pm-run-x", *expected])
        self.assertEqual(seen_token, [None])
        direct.assert_not_called()
        bare.assert_not_called()

    def test_refuses_an_explicit_token_and_a_failed_run_shell(self) -> None:
        with mock.patch.object(sessions, "run_shell") as run_shell:
            with self.assertRaises(PmError):
                sessions.start_session_via_server("s", "pm-r-s01a0", Path("/r"), "true", {"PM_RUN_TOKEN": "x"})
        run_shell.assert_not_called()
        with mock.patch.object(sessions, "run_shell", return_value=(1, "error connecting to /x\n")):
            with self.assertRaises(PmError) as ctx:
                sessions.start_session_via_server("s", "pm-r-s01a0", Path("/r"), "true", {})
        self.assertIn("error connecting", str(ctx.exception))

    @unittest.skipUnless(_HAS_TMUX, "tmux is required")
    def test_a_socket_with_no_server_raises_and_leaves_no_server(self) -> None:
        socket = f"{TEST_TMUX_SOCKET}-sb-via-none"
        self.addCleanup(sessions.kill_server, socket)
        with self.assertRaises(PmError):
            sessions.start_session_via_server(socket, "pm-test-via-s01a0", Path(tempfile.gettempdir()), "sleep 5", {})
        self.assertFalse(sessions.server_running(socket))
        self.assertFalse(sessions.socket_path(socket).exists())


# --- a real sandboxed server ---------------------------------------------------------


@requires_srt
class SandboxServerTestCase(PmTestCase):
    """A linked worktree, its temp and control directories, and an effective
    policy written to the control directory, on a socket of this test's own."""

    def setUp(self) -> None:
        super().setUp()
        self.branch = "node/sb"
        self.worktree = _real(make_linked_worktree(self.repo, self.repo.parent / "wt", self.branch))
        self.common = _real(self.repo / ".git")
        self.socket = _sandbox_socket()
        self.addCleanup(self._kill_server)
        self.tmpdir = sandbox.make_tmpdir(self.socket)
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.control = sandbox.control_dir(self.common, self.socket)
        self.control.mkdir(parents=True)
        self.srt_pid: int | None = None

    def _kill_server(self) -> None:
        # Through stop_server when a server was started, so its srt is reaped.
        if self.srt_pid is None:
            sessions.kill_server(self.socket)
        else:
            record = {"worktree": str(self.worktree), "tmpdir": str(self.tmpdir), "srt_pid": self.srt_pid}
            sandbox.stop_server(socket=self.socket, record=record)
        (sessions.socket_path(self.socket).parent / f"{self.socket}.probe").unlink(missing_ok=True)

    def write_policy(self, base: dict | None = None) -> Path:
        policy = sandbox.derive_policy(
            minimal_base_policy() if base is None else base,
            worktree=self.worktree,
            branch=self.branch,
            socket=self.socket,
            tmpdir=self.tmpdir,
            control_root=sandbox.control_root(self.common),
            pm_inside=False,
        )
        sandbox.check_binaries(policy)
        path = self.control / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return path

    def start(self, policy_path: Path) -> tuple[int, int]:
        pids = sandbox.start_server(
            socket=self.socket,
            policy_path=policy_path,
            worktree=self.worktree,
            tmpdir=self.tmpdir,
            log_path=self.control / "srt.log",
        )
        self.srt_pid = pids[0]
        return pids

    def wait_for(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return predicate()


class TestSandboxedServer(SandboxServerTestCase):
    def test_a_started_server_verifies_and_stops_leaving_nothing_behind(self) -> None:
        srt_pid, server_pid = self.start(self.write_policy())
        self.assertNotEqual(srt_pid, server_pid)
        self.assertTrue(sessions.server_running(self.socket))
        sandbox.verify_server(socket=self.socket, worktree=self.worktree)
        self.assertFalse((self.worktree / ".pm").exists())
        sandbox.write_record(
            self.control,
            worktree=str(self.worktree),
            branch=self.branch,
            socket=self.socket,
            tmpdir=str(self.tmpdir),
            policy="sandbox.json",
            policy_sha256="0" * 64,
            srt_version=sandbox.srt_version(),
            srt_pid=srt_pid,
            server_pid=server_pid,
            started_at="2026-10-25T10:00:00Z",
        )
        record = sandbox.read_record(self.control)

        remaining, notes = sandbox.stop_server(socket=self.socket, record=record)
        self.assertEqual(remaining, [])
        self.assertFalse(sessions.server_running(self.socket))
        self.assertFalse(self.tmpdir.exists())
        self.assertFalse(self.control.exists())
        self.assertNotIn(f"srt still running: {srt_pid}", notes)
        self.assertIn(f"removed sandbox temp dir {self.tmpdir}", notes)
        self.assertIn(f"removed sandbox control dir {self.control}", notes)
        with self.assertRaises(ProcessLookupError):
            os.kill(srt_pid, 0)

        _remaining, notes = sandbox.stop_server(socket=self.socket, record=record)
        self.assertIn(f"sandbox temp dir already gone: {self.tmpdir}", notes)
        self.assertIn(f"sandbox control dir already gone: {self.control}", notes)

    def test_a_session_through_the_server_is_confined_and_carries_no_token(self) -> None:
        self.start(self.write_policy())
        sandbox.verify_server(socket=self.socket, worktree=self.worktree)
        out = self.worktree / "out.txt"
        leak = self.repo.parent / "leak"
        script = write_fake_harness(
            self.repo.parent / "harness.sh",
            "\n".join(
                [
                    f'echo "X=$PM_X TOKEN=${{PM_RUN_TOKEN:-ABSENT}} TMPDIR=$TMPDIR" > "{out}"',
                    f'touch "{leak}" 2>/dev/null; echo "leak_rc=$?" >> "{out}"',
                    f'touch "{self.worktree}/inside"; echo "inside_rc=$?" >> "{out}"',
                    f'git -C "{self.worktree}" branch sibling 2>/dev/null; echo "branch_rc=$?" >> "{out}"',
                    'echo done >> "' + str(out) + '"',
                    "sleep 15",
                ]
            ),
        )
        with sessions.scoped_environ("PM_RUN_TOKEN", "secret-host-token"):
            sessions.start_session_via_server(
                self.socket, "pm-test-sb-s01a0", self.worktree, str(script), {"PM_X": "a b"}
            )
        self.assertEqual(sessions.list_sessions_on(self.socket), ["pm-test-sb-s01a0"])
        self.assertTrue(self.wait_for(lambda: out.is_file() and "done" in out.read_text(encoding="utf-8")))
        lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], f"X=a b TOKEN=ABSENT TMPDIR={self.tmpdir}")
        self.assertNotEqual(lines[1], "leak_rc=0")
        self.assertEqual(lines[2], "inside_rc=0")
        self.assertNotEqual(lines[3], "branch_rc=0")
        self.assertFalse(leak.exists())
        self.assertTrue((self.worktree / "inside").is_file())
        self.assertIsNone(sessions.server_environment(self.socket, "PM_RUN_TOKEN"))

    def test_start_server_refuses_a_socket_with_a_live_server(self) -> None:
        policy_path = self.write_policy()
        srt_pid, _server_pid = self.start(policy_path)
        with self.assertRaises(PmError) as ctx:
            self.start(policy_path)
        self.assertIn("already answers", str(ctx.exception))
        os.kill(srt_pid, 0)  # the first server is untouched

    def test_srt_exiting_first_fails_the_start_quoting_its_log(self) -> None:
        # srt refuses a settings file without its required keys (exit 1).
        policy_path = self.control / "policy.json"
        policy_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(PmError) as ctx:
            self.start(policy_path)
        message = str(ctx.exception)
        self.assertIn("srt exited with status", message)
        self.assertIn("srt.log ends:", message)
        self.assertFalse(sessions.server_running(self.socket))

    def test_a_policy_denying_the_probe_directory_fails_the_positive_probe(self) -> None:
        (self.worktree / ".pm").mkdir()
        base = minimal_base_policy()
        base["filesystem"]["denyWrite"] = [str(self.worktree / ".pm")]
        self.start(self.write_policy(base))
        with self.assertRaises(PmError) as ctx:
            sandbox.verify_server(socket=self.socket, worktree=self.worktree)
        self.assertIn("positive probe", str(ctx.exception))
        self.assertFalse((self.worktree / ".pm" / "sandbox-probe").exists())


@requires_srt
class TestPlainServerFailsVerification(PmTestCase):
    def test_an_unsandboxed_server_claiming_sandbox_runtime_fails_the_negative_probe_and_is_killed(self) -> None:
        socket = _sandbox_socket()
        probe = sessions.socket_path(socket).parent / f"{socket}.probe"
        self.addCleanup(probe.unlink, missing_ok=True)
        self.addCleanup(sessions.kill_server, socket)
        with sessions.scoped_environ("PM_TMUX_SOCKET", socket):
            sessions.start_session("pm-test-plain-s01a0", self.repo, "sleep 15", {})
        sessions.set_server_environment(socket, "SANDBOX_RUNTIME", "1")
        with self.assertRaises(PmError) as ctx:
            sandbox.verify_server(socket=socket, worktree=self.repo)
        self.assertIn("negative probe", str(ctx.exception))
        self.assertIn(str(probe), str(ctx.exception))
        self.assertFalse(sessions.server_running(socket))
        self.assertFalse(probe.exists())

    def test_a_server_without_sandbox_runtime_is_refused(self) -> None:
        socket = _sandbox_socket()
        self.addCleanup(sessions.kill_server, socket)
        with sessions.scoped_environ("PM_TMUX_SOCKET", socket):
            sessions.start_session("pm-test-plain-s01a0", self.repo, "sleep 15", {})
        with self.assertRaises(PmError) as ctx:
            sandbox.verify_server(socket=socket, worktree=self.repo)
        self.assertIn("SANDBOX_RUNTIME=1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

"""Protected behaviours: the `srt` sandbox mechanics in `pm_lib.sandbox` and
the server-scoped `sessions` helpers they rest on.

Policy loading, derivation, the binaries check, the record, the temp dir and
`wrap_command` run without `srt`, and so do the start's process shape and
deadline and the teardown's record checks, against a fake `srt` (and a fake
`tmux` where the probe itself is under test), and the bound on
`verify_server`'s probes. The `sessions` helpers run with tmux alone.
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
    register_sandbox_socket,
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
    """A fresh `<TEST_TMUX_SOCKET>-sb<N>` socket for one test, registered
    for the helpers' `atexit` sweep."""
    return register_sandbox_socket(f"{TEST_TMUX_SOCKET}-sb{next(_SOCKET_NUMBERS)}")


def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(path))


def _literal(path: Path | str) -> dict:
    return {"path": str(path), "literal": True}


def _socket_grant(socket: str) -> str:
    path = sessions.socket_path(socket)
    return str(_real(path.parent) / path.name)


def _case_variant(test: unittest.TestCase, path: Path | str) -> str:
    """`path` with its letters' case swapped; skips `test` unless that
    spelling names the same file (a case-insensitive volume)."""
    variant = str(path).swapcase()
    if variant == str(path) or not os.path.exists(variant) or not os.path.samefile(variant, path):
        test.skipTest(f"{path} is not on a case-insensitive volume")
    return variant


# A NUL, a control character and a lone surrogate, each inside a path.
_BAD_PATHS = ("/tmp/a\x00b", "/tmp/a\x1bb", "/tmp/a\ud800b")

# (section, key) of every list `srt` reads from a policy.
_POLICY_LISTS = (
    *(("network", key) for key in ("allowedDomains", "deniedDomains", "allowUnixSockets")),
    *(("filesystem", key) for key in ("denyRead", "allowRead", "allowWrite", "denyWrite")),
)


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

    def test_a_grant_above_or_inside_the_toolkit_checkout_is_refused_but_one_inside_home_is_not(self) -> None:
        root = sandbox.toolkit_root()
        if _real(root.parent) == _real(Path.home()):
            self.skipTest("the toolkit checkout sits directly in the home directory")
        for entry, relation in ((root.parent, "is an ancestor of"), (root / "skills", "is inside")):
            with self.subTest(entry=entry):
                self.assertRefused(self._write(self._policy_with_write(str(entry))), "the toolkit checkout", relation)
        # Home is refused by equality alone: a grant inside it is normal.
        self.assertRefused(self._write(self._policy_with_write("~")), "the home directory")
        sandbox.load_base_policy(self._write(self._policy_with_write("~/.codex/sessions")))

    def test_a_grant_of_the_tmux_socket_directory_is_refused(self) -> None:
        directory = sessions.socket_path("x").parent
        self.assertRefused(self._write(self._policy_with_write(str(directory))), "the tmux socket directory")

    def test_a_grant_above_or_inside_the_tmux_socket_directory_is_refused(self) -> None:
        directory = sessions.socket_path("x").parent
        for entry, relation in ((directory.parent, "is an ancestor of"), (directory / "inner", "is inside")):
            with self.subTest(entry=entry):
                self.assertRefused(
                    self._write(self._policy_with_write(str(entry))), "the tmux socket directory", relation
                )

    def test_an_unix_socket_entry_reaching_the_tmux_socket_directory_is_refused(self) -> None:
        directory = sessions.socket_path("x").parent
        for entry in (str(directory), str(directory / "default"), _literal(directory / "default")):
            with self.subTest(entry=entry):
                policy = minimal_base_policy()
                policy["network"]["allowUnixSockets"] = [entry]
                self.assertRefused(self._write(policy), "network.allowUnixSockets entry", "the tmux socket directory")
        # A relative or malformed entry is refused as an allowWrite one is.
        for entry, fragment in (("sockets/x.sock", "relative"), (42, "network.allowUnixSockets entry")):
            with self.subTest(entry=entry):
                policy = minimal_base_policy()
                policy["network"]["allowUnixSockets"] = [entry]
                self.assertRefused(self._write(policy), fragment)
        # An unrelated socket stays allowed.
        policy = minimal_base_policy()
        policy["network"]["allowUnixSockets"] = [str(self.tmp / "agent.sock")]
        sandbox.load_base_policy(self._write(policy))

    def test_the_tmux_socket_directory_honours_tmux_tmpdir(self) -> None:
        with mock.patch.dict(os.environ, {"TMUX_TMPDIR": str(self.tmp)}):
            directory = self.tmp / f"tmux-{os.getuid()}"
            self.assertRefused(self._write(self._policy_with_write(str(directory))), "the tmux socket directory")

    def test_a_case_variant_of_each_protected_target_is_refused(self) -> None:
        # srt enforces a grant spelled with other letter case on a
        # case-insensitive volume, so the refusal judges identity.
        directory = sessions.socket_path("x").parent
        if not directory.is_dir():
            self.skipTest(f"the tmux socket directory {directory} does not exist")
        cases = (
            (Path.home(), "the home directory"),
            (sandbox.toolkit_root(), "the toolkit checkout"),
            (_real(directory), "the tmux socket directory"),
        )
        for target, fragment in cases:
            with self.subTest(target=target):
                variant = _case_variant(self, target)
                for entry in (variant, _literal(variant)):
                    self.assertRefused(self._write(self._policy_with_write(entry)), fragment)

    def test_a_write_entry_holding_a_nul_control_or_surrogate_character_is_refused(self) -> None:
        for text in _BAD_PATHS:
            for entry in (text, _literal(text)):
                with self.subTest(entry=entry):
                    self.assertRefused(self._write(self._policy_with_write(entry)), "lone-surrogate character")

    def test_a_policy_path_no_file_can_have_is_refused(self) -> None:
        for name in ("a\x00b.json", "a\ud800b.json"):
            with self.subTest(name=name):
                with self.assertRaises(PmError) as ctx:
                    sandbox.load_base_policy(self.tmp / name)
                self.assertIn("unreadable", str(ctx.exception))

    def test_a_non_list_policy_list_is_refused(self) -> None:
        for section, key in _POLICY_LISTS:
            for value in (None, "/var/run/x.sock", {"path": "/var/run/x.sock"}):
                with self.subTest(key=key, value=value):
                    policy = minimal_base_policy()
                    policy[section][key] = value
                    self.assertRefused(self._write(policy), f"{section}.{key} is not a list")

    def test_a_case_variant_of_a_socket_directory_not_yet_created_is_refused(self) -> None:
        # On a fresh host the directory appears only when start_server's
        # host probe runs, after the base policy was loaded; srt then
        # enforces the variant as the directory, so it is refused before.
        for directory, variant in (("tmux-absent", "TMUX-Absent"), ("tmux-\u00e9", "TMUX-E\u0301")):
            with self.subTest(directory=directory):
                absent = self.tmp / directory
                with mock.patch.object(
                    sandbox.sessions, "socket_path", side_effect=lambda name, parent=absent: parent / name
                ):
                    self.assertRefused(
                        self._write(self._policy_with_write(str(self.tmp / variant))), "the tmux socket directory"
                    )
                    self.assertFalse(absent.exists())
                    sandbox.load_base_policy(self._write(self._policy_with_write(str(self.tmp / "tmux-other"))))

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

    def pointer_denies(self, git_dir: Path) -> list[dict]:
        return [
            _literal(git_dir / "commondir"),
            _literal(git_dir / "gitdir"),
            _literal(git_dir / "config.worktree"),
        ]

    def escape_denies(self) -> list[dict]:
        """A linked worktree's: the pointer files, then `.git` and `hooks`."""
        return [
            *self.pointer_denies(self.git_dir),
            _literal(self.worktree / ".git"),
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
                _literal(self.worktree),
                _literal(self.git_dir),
                _literal(self.common / "objects"),
                _literal(heads / "node" / "x"),
                _literal(heads / "node" / "x.lock"),
                _literal(logs / "node" / "x"),
                _literal(logs / "node" / "x.lock"),
                _literal(self.common / "packed-refs.lock"),
                _literal(_socket_grant(self.socket)),
                _literal(self.tmpdir),
            ],
        )
        self.assertEqual(
            policy["filesystem"]["denyWrite"],
            ["~/.codex/config.toml", *self.always_denies(), *self.escape_denies(), _literal(self.git_dir / "pm")],
        )

    def test_the_state_root_deny_is_mode_b_only_and_the_ledger_grant_mode_c_only(self) -> None:
        outside = self.derive(pm_inside=False)
        inside = self.derive(pm_inside=True)
        ledger_grant = _literal(_real(ledger.ledger_dir()))
        self.assertIn(_literal(self.git_dir / "pm"), outside["filesystem"]["denyWrite"])
        self.assertNotIn(ledger_grant, outside["filesystem"]["allowWrite"])
        self.assertNotIn(_literal(self.git_dir / "pm"), inside["filesystem"]["denyWrite"])
        self.assertEqual(inside["filesystem"]["allowWrite"][-1], ledger_grant)
        for policy in (outside, inside):
            for deny in self.escape_denies():
                self.assertIn(deny, policy["filesystem"]["denyWrite"])

    def test_a_plain_checkout_gets_no_common_dir_grants_but_the_pointer_file_and_worktrees_denies(self) -> None:
        # git honours `$GIT_DIR/commondir` in a plain checkout too, so the
        # pointer files are denied there; `.git` is a directory and its
        # `hooks` is already denied as the common dir's, so neither is added.
        policy = self.derive(worktree=self.repo, branch="main")
        repo = _real(self.repo)
        self.assertEqual(
            policy["filesystem"]["allowWrite"],
            [_literal(repo), _literal(repo / ".git"), _literal(_socket_grant(self.socket)), _literal(self.tmpdir)],
        )
        self.assertEqual(
            policy["filesystem"]["denyWrite"],
            [
                *self.always_denies(),
                *self.pointer_denies(repo / ".git"),
                _literal(repo / ".git" / "worktrees"),
                _literal(repo / ".git" / "pm"),
            ],
        )
        self.assertNotIn(_literal(repo / ".git"), policy["filesystem"]["denyWrite"])
        self.assertNotIn(_literal(repo / ".git" / "modules"), policy["filesystem"]["denyWrite"])
        self.assertEqual(
            policy["filesystem"]["denyWrite"].count(_literal(repo / ".git" / "hooks")), 1, "only the common dir's"
        )

    def test_a_linked_worktree_does_not_deny_the_common_worktrees_directory(self) -> None:
        policy = self.derive()
        self.assertNotIn(_literal(self.common / "worktrees"), policy["filesystem"]["denyWrite"])

    def test_reads_socket_pty_and_required_lists(self) -> None:
        base = {"network": {}, "filesystem": {"allowRead": ["/opt/data"]}, "allowPty": False}
        extra = self.repo.parent / "launcher"
        policy = self.derive(base, extra_read=[extra])
        self.assertEqual(
            policy["filesystem"]["allowRead"],
            [
                "/opt/data",
                _literal(self.worktree),
                _literal(self.common),
                _literal(sandbox.toolkit_root()),
                _literal(extra),
            ],
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
        self.assertIn(_literal(grant), policy["filesystem"]["allowWrite"])
        # allowUnixSockets takes plain strings, compiled by srt as a subpath.
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

    def test_a_worktree_path_with_glob_characters_gets_literal_entries_only(self) -> None:
        # srt reads any string holding * ? [ ] as a pattern too, so a plain
        # grant of `wt[1]` would also grant a sibling `wt1`.
        worktree = _real(make_linked_worktree(self.repo, self.repo.parent / "wt[1]", "node/glob"))
        git_dir = sandbox.git_ops.worktree_git_dir(worktree)  # git names it `wt-1`
        policy = self.derive(worktree=worktree, branch="node/glob", extra_read=[self.repo.parent / "launch*"])
        for key in ("allowWrite", "allowRead", "denyWrite"):
            for entry in policy["filesystem"][key]:
                with self.subTest(key=key, entry=entry):
                    self.assertIsInstance(entry, dict)
                    self.assertIs(entry["literal"], True)
        self.assertEqual(policy["filesystem"]["allowWrite"][:2], [_literal(worktree), _literal(git_dir)])
        self.assertEqual(policy["filesystem"]["allowRead"][0], _literal(worktree))
        reviewer = sandbox.derive_reviewer_policy(
            minimal_base_policy(), worktree=worktree, tmpdir=self.tmpdir, control_root=self.control_root
        )
        for key in ("allowWrite", "allowRead", "denyWrite"):
            for entry in reviewer["filesystem"][key]:
                with self.subTest(policy="reviewer", key=key, entry=entry):
                    self.assertIs(entry["literal"], True)

    def test_a_case_variant_of_the_repository_is_refused(self) -> None:
        cases = (
            (_case_variant(self, self.worktree), "is the worktree"),
            (_case_variant(self, self.worktree) + "/src", "is inside the worktree"),
            (_case_variant(self, self.repo.parent), "is an ancestor of the worktree"),
            (_case_variant(self, self.common), "is the common git dir"),
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

    def test_a_malformed_base_raises_pm_error_from_every_derivation(self) -> None:
        derivations = {
            "server": self.derive,
            "reviewer": lambda base: sandbox.derive_reviewer_policy(
                base, worktree=self.worktree, tmpdir=self.tmpdir, control_root=self.control_root
            ),
            "attach": lambda base: sandbox.derive_attach_policy(
                base, socket=self.socket, control_root=self.control_root
            ),
        }
        # (base, the fragment its PmError names, the derivations it reaches).
        cases = []
        for section, key in _POLICY_LISTS:
            for value in (None, "/var/run/x.sock", {"path": "/var/run/x.sock"}):
                base = minimal_base_policy()
                base[section][key] = value
                cases.append((base, f"{section}.{key} is not a list", tuple(derivations)))
        for section in ("network", "filesystem"):
            base = minimal_base_policy()
            base[section] = ["not", "an", "object"]
            cases.append((base, f"{section} is not an object", tuple(derivations)))
        for text in _BAD_PATHS:
            base = minimal_base_policy()
            base["filesystem"]["allowWrite"] = [text]
            # The attach policy carries no write grant, so it never reads them.
            cases.append((base, "lone-surrogate character", ("server", "reviewer")))
        for base, fragment, names in cases:
            for name in names:
                with self.subTest(base=base, derivation=name):
                    with self.assertRaises(PmError) as ctx:
                        derivations[name](base)
                    self.assertIn(fragment, str(ctx.exception))


class TestDeriveReviewerAndAttachPolicies(DerivationTestCase):
    def test_the_reviewer_writes_only_its_temp_dir_and_reaches_no_tmux_socket(self) -> None:
        base = minimal_base_policy()
        base["filesystem"]["allowWrite"] = ["~/.codex/sessions"]
        extra = self.repo.parent / "plan-dir"
        policy = sandbox.derive_reviewer_policy(
            base, worktree=self.worktree, tmpdir=self.tmpdir, control_root=self.control_root, extra_read=[extra]
        )
        self.assertEqual(policy["filesystem"]["allowWrite"], ["~/.codex/sessions", _literal(self.tmpdir)])
        self.assertEqual(policy["filesystem"]["denyWrite"], [*self.always_denies(), *self.escape_denies()])
        self.assertEqual(
            policy["filesystem"]["allowRead"],
            [_literal(self.worktree), _literal(self.common), _literal(sandbox.toolkit_root()), _literal(extra)],
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
        self._which = mock.patch.object(sandbox.shutil, "which", side_effect=found.get)
        self._which.start()
        self.addCleanup(self._which.stop)

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

    def test_a_grant_on_or_above_the_launcher_directories_is_refused(self) -> None:
        # `bin/srt` is a symlink into the package and `PATH` names it, so a
        # grant on `bin` replaces the symlink the host runs next, though
        # neither real path is granted. Each launcher sits in a directory of
        # its own, so each is judged separately.
        self._which.stop()
        srt_bin, node_bin = self.tmp / "launch-srt", self.tmp / "launch-node"
        srt_bin.mkdir()
        node_bin.mkdir()
        (self.package / "dist" / "cli.js").chmod(0o755)
        (srt_bin / "srt").symlink_to(self.package / "dist" / "cli.js")
        self.node.chmod(0o755)
        (node_bin / "node").symlink_to(self.node)
        for entry, fragment in (
            (str(srt_bin), "the srt launcher"),
            (str(node_bin), "the node launcher"),
        ):
            with self.subTest(entry=entry):
                with mock.patch.dict(os.environ, {"PATH": f"{srt_bin}{os.pathsep}{node_bin}{os.pathsep}/usr/bin"}):
                    with self.assertRaises(PmError) as ctx:
                        sandbox.check_binaries(self._policy(entry))
                self.assertIn(fragment, str(ctx.exception))
        with mock.patch.dict(os.environ, {"PATH": f"{srt_bin}{os.pathsep}{node_bin}{os.pathsep}/usr/bin"}):
            sandbox.check_binaries(self._policy(str(self.tmp / "state")))

    def test_a_case_variant_of_the_srt_package_or_node_is_refused(self) -> None:
        cases = (
            (_case_variant(self, self.package), "srt's installation"),
            (_case_variant(self, self.package / "dist"), "srt's installation"),
            (_case_variant(self, self.node), "the node that runs srt"),
            (_case_variant(self, self.node.parent), "the node that runs srt"),
        )
        for entry, fragment in cases:
            with self.subTest(entry=entry):
                with self.assertRaises(PmError) as ctx:
                    sandbox.check_binaries(self._policy(entry))
                self.assertIn(fragment, str(ctx.exception))

    def test_a_write_entry_holding_a_nul_control_or_surrogate_character_is_refused(self) -> None:
        for text in _BAD_PATHS:
            with self.subTest(text=text):
                with self.assertRaises(PmError) as ctx:
                    sandbox.check_binaries(self._policy(text))
                self.assertIn("lone-surrogate character", str(ctx.exception))

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
        self.assertEqual(sandbox.read_record(control), {**fields, "control": str(control)})

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


# --- the server lifecycle without srt -------------------------------------------------


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class TestStartServerWithFakes(TempDirTestCase):
    """`start_server`'s process shape and its deadline, against a fake `srt`
    (a process group holding one long-lived child) and, where the probe
    itself is under test, a fake `tmux`, both first on PATH."""

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.child_pid_file = self.tmp / "child.pid"
        self.started = self.tmp / "started"
        write_fake_harness(
            self.bin / "srt",
            "\n".join(
                [
                    f'sleep 60 & echo $! > "{self.child_pid_file}"',
                    f'touch "{self.started}"',
                    "echo 'fake srt: waiting'",
                    "wait",
                ]
            ),
        )
        self.log = self.tmp / "srt.log"
        for patcher in (
            mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}),
            mock.patch.object(sandbox.platform, "system", return_value="Darwin"),
            mock.patch.object(sandbox, "START_DEADLINE_SECONDS", 1.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_tmux(self, after_start: str) -> None:
        """A `tmux` that reports no server until the fake srt has started,
        then runs `after_start`."""
        write_fake_harness(
            self.bin / "tmux",
            "\n".join(
                [
                    f'if [ -e "{self.started}" ]; then {after_start}; fi',
                    "echo 'no server running on /tmp/fake' >&2",
                    "exit 1",
                ]
            ),
        )

    def start(self) -> str:
        started_before = dict(sandbox._STARTED)
        began = time.monotonic()
        with self.assertRaises(PmError) as ctx:
            sandbox.start_server(
                socket="pm-run-x",
                policy_path=self.tmp / "p.json",
                worktree=self.tmp,
                tmpdir=self.tmp,
                log_path=self.log,
            )
        self.assertLess(time.monotonic() - began, 1.0 + 3.0)
        message = str(ctx.exception)
        self.assertIn("fake srt: waiting", message)  # the log is quoted
        child = int(self.child_pid_file.read_text(encoding="utf-8"))
        self.assertTrue(_wait_gone(child), "the process group was not killed")
        self.assertEqual(sandbox._STARTED, started_before)
        return message

    def test_a_server_that_never_answers_fails_at_the_deadline(self) -> None:
        self.fake_tmux(":")
        self.assertIn("the server did not answer within 1 s", self.start())

    def test_a_hung_probe_expires_at_the_deadline(self) -> None:
        # `exec`, so the timeout's kill reaches the process holding the pipe.
        self.fake_tmux("exec sleep 30")
        message = self.start()
        self.assertIn("the server did not answer within 1 s", message)
        self.assertIn("no reply within", message)

    def test_an_answer_after_the_deadline_is_refused(self) -> None:
        calls: list[float | None] = []

        def late_server_running(socket: str, timeout: float | None = None) -> bool:
            calls.append(timeout)
            if len(calls) == 1:
                return False  # the pre-start check
            time.sleep(1.2)
            return True

        with (
            mock.patch.object(sandbox.sessions, "server_running", side_effect=late_server_running),
            mock.patch.object(sandbox, "_server_pid", return_value=4242),
        ):
            self.assertIn("the server did not answer within 1 s", self.start())
        self.assertTrue(all(timeout is not None and timeout <= 10.0 for timeout in calls[1:]), calls)

    def test_an_interrupt_during_the_wait_kills_the_group_and_propagates(self) -> None:
        processes: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def spy(argv, *args, **kwargs):
            process = real_popen(argv, *args, **kwargs)
            processes.append(process)
            return process

        def interrupted(socket: str, timeout: float | None = None) -> bool:
            if not processes:
                return False  # the pre-start check
            deadline = time.monotonic() + 5.0
            while not self.started.exists() and time.monotonic() < deadline:  # the child pid is written
                time.sleep(0.05)
            raise KeyboardInterrupt

        started_before = dict(sandbox._STARTED)
        with (
            mock.patch.object(sandbox.subprocess, "Popen", side_effect=spy),
            mock.patch.object(sandbox.sessions, "server_running", side_effect=interrupted),
        ):
            with self.assertRaises(KeyboardInterrupt):
                sandbox.start_server(
                    socket="pm-run-x",
                    policy_path=self.tmp / "p.json",
                    worktree=self.tmp,
                    tmpdir=self.tmp,
                    log_path=self.log,
                )
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode, "srt was not reaped")
        self.assertTrue(_wait_gone(processes[0].pid), "srt is still running")
        child = int(self.child_pid_file.read_text(encoding="utf-8"))
        self.assertTrue(_wait_gone(child), "the process group was not killed")
        self.assertEqual(sandbox._STARTED, started_before)

    def test_a_server_that_answers_starts_in_its_own_session_without_the_token(self) -> None:
        self.fake_tmux("echo 5151; exit 0")
        worktree = self.tmp / "wt"
        worktree.mkdir()
        self.log.write_text("earlier run\n", encoding="utf-8")
        real_popen = subprocess.Popen
        srt_calls: list[tuple[list[str], dict, str | None, subprocess.Popen]] = []

        def spy(argv, *args, **kwargs):
            process = real_popen(argv, *args, **kwargs)
            if argv[0] == "srt":
                srt_calls.append((argv, kwargs, os.environ.get("PM_RUN_TOKEN"), process))
                self.addCleanup(sandbox._STARTED.pop, process.pid, None)
                self.addCleanup(sandbox._kill_group, process)
            return process

        # The class's 1 s deadline is for the failure tests; a first exec of
        # the fresh fake scripts can take longer than that on a loaded host.
        with (
            mock.patch.object(sandbox, "START_DEADLINE_SECONDS", 10.0),
            mock.patch.dict(os.environ, {"PM_RUN_TOKEN": "tok-1"}),
            mock.patch.object(sandbox.subprocess, "Popen", side_effect=spy),
        ):
            srt_pid, server_pid = sandbox.start_server(
                socket="pm-run-x",
                policy_path=self.tmp / "p.json",
                worktree=worktree,
                tmpdir=self.tmp,
                log_path=self.log,
            )

        self.assertEqual(len(srt_calls), 1)
        argv, kwargs, token_during_call, process = srt_calls[0]
        self.assertEqual(argv[:4], ["srt", "-s", str(self.tmp / "p.json"), "env"])
        self.assertEqual(argv[-4:], ["tmux", "-L", "pm-run-x", "-D"])
        self.assertEqual(kwargs["cwd"], str(worktree))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], kwargs["stderr"])
        self.assertEqual(kwargs["stdout"].name, str(self.log))
        self.assertEqual(kwargs["stdout"].mode, "ab")
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(token_during_call, "tok-1")
        self.assertNotIn("PM_RUN_TOKEN", kwargs["env"])
        self.assertEqual(kwargs["env"]["PATH"], os.environ["PATH"])
        self.assertTrue(self.log.read_text(encoding="utf-8").startswith("earlier run\n"))
        self.assertEqual((srt_pid, server_pid), (process.pid, 5151))
        self.assertIs(sandbox._STARTED[srt_pid], process)


# Marks a field `TestStopServerRecord.record` leaves out.
_ABSENT = object()


class TestStopServerRecord(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(tempfile, "tempdir", str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.socket = "pm-run-x"
        self.tmpdir = sandbox.make_tmpdir(self.socket)
        self.control = sandbox.control_dir(self.tmp / "common", self.socket)
        self.control.mkdir(parents=True)

    def record(self, **overrides) -> dict:
        record = {"srt_pid": None, "tmpdir": str(self.tmpdir), "control": str(self.control)}
        record.update(overrides)
        return {key: value for key, value in record.items() if value is not _ABSENT}

    def test_the_recorded_control_dir_is_used_without_the_worktree(self) -> None:
        record = self.record(worktree=str(self.tmp / "gone"))
        with (
            mock.patch.object(sandbox.sessions, "kill_server", return_value=[]),
            mock.patch.object(sandbox, "_common_dir_of", side_effect=AssertionError("git was asked")),
        ):
            outcome = sandbox.stop_server(socket=self.socket, record=record)
        self.assertEqual(outcome.remaining, [])
        self.assertEqual(outcome.control_state, "removed")
        self.assertIn(f"removed sandbox control dir {self.control}", outcome.notes)
        self.assertFalse(self.control.exists())
        self.assertFalse(self.tmpdir.exists())

    def test_a_srt_that_outlives_the_wait_keeps_the_record_for_a_retry(self) -> None:
        with (
            mock.patch.object(sandbox.sessions, "kill_server", return_value=[]),
            mock.patch.object(sandbox, "_pid_alive", return_value=True),
            mock.patch.object(sandbox, "SRT_EXIT_WAIT_SECONDS", 0.2),
        ):
            outcome = sandbox.stop_server(socket=self.socket, record=self.record(srt_pid=999999))
        self.assertTrue(outcome.srt_alive)
        self.assertEqual(outcome.control_state, "kept")
        self.assertEqual(
            outcome.left, ["srt still running: 999999", f"sandbox control dir {self.control} is still there"]
        )
        self.assertIn("srt still running: 999999", outcome.notes)
        self.assertIn(f"removed sandbox temp dir {self.tmpdir}", outcome.notes)
        self.assertTrue(self.control.is_dir())

    def test_a_missing_or_malformed_field_is_refused_before_anything_is_stopped(self) -> None:
        linked = self.tmp / "pm-sandbox-linked"
        linked.symlink_to(self.tmpdir)
        regular = self.tmp / "pm-sandbox-file"
        regular.write_text("x", encoding="utf-8")
        cases = (
            (self.record(srt_pid=_ABSENT), "srt_pid is missing"),
            (self.record(srt_pid="123"), "srt_pid is not a pid"),
            (self.record(srt_pid=True), "srt_pid is not a pid"),
            (self.record(srt_pid=-1), "srt_pid is not a pid"),
            (self.record(srt_pid=2**31), "srt_pid is not a pid"),
            (self.record(srt_pid=2**100), "srt_pid is not a pid"),
            (self.record(tmpdir=_ABSENT), "tmpdir is missing"),
            (self.record(tmpdir=None), "tmpdir is null"),
            (self.record(tmpdir=7), "tmpdir is not a path"),
            (self.record(tmpdir=str(self.tmp / "elsewhere")), "refusing to remove"),
            (self.record(tmpdir=str(linked)), f"refusing to remove {linked}: not a directory"),
            (self.record(tmpdir=str(regular)), f"refusing to remove {regular}: not a directory"),
            (self.record(control="pm-sandbox/pm-run-x"), "refusing to remove sandbox control dir"),
            (self.record(control=str(self.tmp / "pm-sandbox" / "other")), "refusing to remove sandbox control dir"),
            (self.record(control=str(self.tmp / "elsewhere" / "pm-run-x")), "refusing to remove sandbox control dir"),
            (self.record(control="/x/pm-sandbox/a\x00b"), "control is not a path"),
            (self.record(control=_ABSENT), "neither control nor worktree"),
            (self.record(control=_ABSENT, worktree=["w"]), "worktree is not a path"),
            ([], "not an object"),
        )
        for record, fragment in cases:
            with self.subTest(record=record):
                with (
                    mock.patch.object(sandbox.sessions, "kill_server") as kill,
                    mock.patch.object(sandbox, "_pid_alive") as alive,
                    mock.patch.object(sandbox.shutil, "rmtree") as rmtree,
                ):
                    with self.assertRaises(PmError) as ctx:
                        sandbox.stop_server(socket=self.socket, record=record)
                self.assertIn(fragment, str(ctx.exception))
                for double in (kill, alive, rmtree):
                    double.assert_not_called()
                self.assertTrue(self.tmpdir.is_dir())
                self.assertTrue(self.control.is_dir())
                self.assertTrue(linked.is_symlink())
                self.assertTrue(regular.is_file())

    def test_a_control_path_that_is_not_a_directory_is_noted_as_refused_not_gone(self) -> None:
        target = self.tmp / "target"
        target.mkdir()
        shutil.rmtree(self.control)
        for make in (lambda: self.control.symlink_to(target), lambda: self.control.write_text("x", encoding="utf-8")):
            with self.subTest(make=make):
                if not os.path.lexists(self.tmpdir):
                    self.tmpdir.mkdir()
                make()
                with mock.patch.object(sandbox.sessions, "kill_server", return_value=[]):
                    outcome = sandbox.stop_server(socket=self.socket, record=self.record())
                notes = outcome.notes
                self.assertEqual(outcome.control_state, "refused")
                self.assertIn(
                    f"refused to remove sandbox control dir {self.control}: not a directory; it was left in place",
                    notes,
                )
                self.assertFalse(any("already gone" in note and str(self.control) in note for note in notes), notes)
                self.assertTrue(os.path.lexists(self.control))
                self.assertTrue(target.is_dir())
                self.control.unlink()


class TestVerifyServerNegativeProbeCleanup(unittest.TestCase):
    def test_a_failed_kill_is_appended_to_the_probe_error_not_raised_in_its_place(self) -> None:
        with (
            mock.patch.object(sandbox.sessions, "server_environment", return_value="1"),
            mock.patch.object(sandbox.sessions, "run_shell", return_value=(0, "")),
            mock.patch.object(sandbox.sessions, "kill_server", side_effect=PmError("could not kill: boom")),
        ):
            with self.assertRaises(PmError) as ctx:
                sandbox.verify_server(socket=f"{TEST_TMUX_SOCKET}-sb-unconfined", worktree=Path("/w"))
        message = str(ctx.exception)
        self.assertIn("negative probe failed", message)
        self.assertIn("killing the server failed too: could not kill: boom", message)
        self.assertNotIn("the server was killed", message)


class TestVerifyServerProbeTimeouts(TempDirTestCase):
    socket = f"{TEST_TMUX_SOCKET}-sb-verify-timeouts"

    def test_both_probes_are_bounded(self) -> None:
        calls: list[tuple[str, float | None]] = []

        def run_shell(socket: str, command: str, timeout: float | None = None) -> tuple[int, str]:
            calls.append((command, timeout))
            if len(calls) == 1:
                return 1, "touch: Operation not permitted"  # the negative probe, refused as it must be
            (self.tmp / ".pm" / "sandbox-probe").touch()
            return 0, ""

        with (
            mock.patch.object(sandbox.sessions, "server_environment", return_value="1"),
            mock.patch.object(sandbox.sessions, "run_shell", side_effect=run_shell),
        ):
            sandbox.verify_server(socket=self.socket, worktree=self.tmp)
        self.assertEqual([timeout for _command, timeout in calls], [sandbox.PROBE_TIMEOUT_SECONDS] * 2, calls)
        self.assertFalse((self.tmp / ".pm").exists())

    def test_a_probe_that_does_not_return_fails_verification(self) -> None:
        # 0 wedges the environment probe, 1 the negative and 2 the positive
        # `run-shell`. A wedged negative probe gets the same cleanup as one
        # that succeeds: its file removed and the server killed.
        for wedged in (0, 1, 2):
            with self.subTest(wedged=wedged):
                calls = itertools.count(1)

                def server_environment(socket: str, name: str, timeout: float | None = None, wedged=wedged):
                    if wedged == 0 and timeout is not None:
                        raise PmError(f"could not read {name} on tmux socket {socket!r}: no reply within {timeout:g} s")
                    return "1"

                def run_shell(socket: str, command: str, timeout: float | None = None, wedged=wedged, calls=calls):
                    if next(calls) == wedged:
                        if wedged == 1:
                            Path(shlex.split(command)[-1]).touch()  # it ran, then wedged
                        raise PmError(f"run-shell on tmux socket {socket!r} did not return within {timeout:g} s")
                    return 1, ""

                with (
                    mock.patch.object(sandbox.sessions, "server_environment", side_effect=server_environment),
                    mock.patch.object(sandbox.sessions, "run_shell", side_effect=run_shell),
                    mock.patch.object(sandbox.sessions, "socket_path", return_value=self.tmp / self.socket),
                    mock.patch.object(sandbox.sessions, "kill_server", return_value=[]) as kill_server,
                ):
                    with self.assertRaises(PmError) as ctx:
                        sandbox.verify_server(socket=self.socket, worktree=self.tmp)
                message = str(ctx.exception)
                self.assertIn("within 10 s", message)
                self.assertFalse((self.tmp / ".pm").exists())
                if wedged == 1:
                    self.assertIn("negative probe could not run", message)
                    self.assertIn("the server was killed", message)
                    kill_server.assert_called_once_with(self.socket)
                    self.assertFalse((self.tmp / f"{self.socket}.probe").exists())
                else:
                    kill_server.assert_not_called()


class TestSocketNamesAtTheEntryPoints(TempDirTestCase):
    def test_an_unusable_socket_name_is_refused_before_tmux_is_reached(self) -> None:
        entry_points = (
            lambda socket: sandbox.start_server(
                socket=socket, policy_path=Path("/p"), worktree=Path("/w"), tmpdir=Path("/t"), log_path=Path("/l")
            ),
            lambda socket: sandbox.verify_server(socket=socket, worktree=Path("/w")),
            lambda socket: sandbox.stop_server(socket=socket, record={}),
            lambda socket: sandbox.control_dir(self.tmp, socket),
            lambda socket: sandbox.make_tmpdir(socket),
        )
        with (
            mock.patch.object(sandbox.sessions, "server_running") as running,
            mock.patch.object(sandbox.sessions, "server_environment") as environment,
            mock.patch.object(sandbox.sessions, "kill_server") as kill,
            mock.patch.object(sandbox.subprocess, "Popen") as popen,
        ):
            for socket in ("", "..", "a/b", "a\x00b", "a\x1bb", "a\ud800b"):
                for entry_point in entry_points:
                    with self.subTest(socket=socket), self.assertRaises(PmError) as ctx:
                        entry_point(socket)
                    self.assertIn("invalid tmux socket name", str(ctx.exception))
        for double in (running, environment, kill, popen):
            double.assert_not_called()


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

    def test_a_timeout_bounds_the_probe(self) -> None:
        reply = subprocess.CompletedProcess([], 0, "1\n", "")
        with mock.patch.object(sessions.subprocess, "run", return_value=reply) as run:
            sessions.server_running("pm-run-x")
            self.assertIsNone(run.call_args.kwargs["timeout"])
            sessions.server_running("pm-run-x", timeout=2.5)
            self.assertEqual(run.call_args.kwargs["timeout"], 2.5)
        expired = subprocess.TimeoutExpired(["tmux"], 0.5)
        with mock.patch.object(sessions.subprocess, "run", side_effect=expired):
            with self.assertRaises(PmError) as ctx:
                sessions.server_running("pm-run-x", timeout=0.5)
        self.assertIn("no reply within 0.5 s", str(ctx.exception))

    @unittest.skipUnless(_HAS_TMUX, "tmux is required")
    def test_a_never_started_socket_has_no_server(self) -> None:
        self.assertFalse(sessions.server_running(register_sandbox_socket(f"{TEST_TMUX_SOCKET}-sb-never")))


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

    def test_a_timeout_bounds_the_command(self) -> None:
        expired = subprocess.TimeoutExpired(["tmux"], 0.5)
        with mock.patch.object(sessions.subprocess, "run", side_effect=expired) as run:
            with self.assertRaises(PmError) as ctx:
                sessions.run_shell("pm-run-x", "sleep 9", timeout=0.5)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.5)
        self.assertIn("did not return within 0.5 s", str(ctx.exception))

    def test_output_that_is_not_utf_8_is_replaced_not_raised(self) -> None:
        # A tmux client printing a raw 0xff byte; the real tmux 3.5a escapes
        # job output (see the live test below), so a fake prints it here.
        with tempfile.TemporaryDirectory() as directory:
            write_fake_harness(Path(directory) / "tmux", "printf 'a\\377b\\n'")
            with mock.patch.dict(os.environ, {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}):
                self.assertEqual(sessions.run_shell("pm-run-x", "true"), (0, "a�b\n"))

    @unittest.skipUnless(_HAS_TMUX, "tmux is required")
    def test_a_socket_with_no_server_fails_and_starts_none(self) -> None:
        socket = register_sandbox_socket(f"{TEST_TMUX_SOCKET}-sb-noserver")
        self.addCleanup(sessions.kill_server, socket)
        returncode, output = sessions.run_shell(socket, "true")
        self.assertNotEqual(returncode, 0)
        self.assertFalse(sessions.server_running(socket))
        self.assertFalse(sessions.socket_path(socket).exists())

    @unittest.skipUnless(_HAS_TMUX, "tmux is required")
    def test_runs_on_a_live_server_with_its_exit_status_and_literal_hashes(self) -> None:
        socket = register_sandbox_socket(f"{TEST_TMUX_SOCKET}-sb-plain")
        self.addCleanup(sessions.kill_server, socket)
        with sessions.scoped_environ("PM_TMUX_SOCKET", socket):
            sessions.start_session("pm-test-sbplain-s01a0", Path(tempfile.gettempdir()), "sleep 15", {})
        self.assertEqual(sessions.run_shell(socket, "echo 'a#b #{pid}'"), (0, "a#b #{pid}\n"))
        self.assertNotEqual(sessions.run_shell(socket, "exit 3")[0], 0)
        # tmux 3.5a writes a non-UTF-8 job byte as the escape `\377`; either
        # way run_shell returns rather than raising.
        returncode, output = sessions.run_shell(socket, "printf '\\377'")
        self.assertEqual(returncode, 0)
        self.assertIn(output.strip(), ("\\377", "�"))
        began = time.monotonic()
        with self.assertRaises(PmError) as ctx:
            sessions.run_shell(socket, "sleep 5", timeout=0.5)
        self.assertLess(time.monotonic() - began, 3.0)
        self.assertIn("did not return within 0.5 s", str(ctx.exception))


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
        socket = register_sandbox_socket(f"{TEST_TMUX_SOCKET}-sb-via-none")
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

    worktree_name = "wt"

    def setUp(self) -> None:
        super().setUp()
        self.branch = "node/sb"
        self.worktree = _real(make_linked_worktree(self.repo, self.repo.parent / self.worktree_name, self.branch))
        self.common = _real(self.repo / ".git")
        self.git_dir = self.common / "worktrees" / self.worktree_name
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

    def run_in_pane(self, *lines: str) -> list[str]:
        """Run shell `lines` in a session started through the server (each
        may append to "$OUT"); return what they wrote, once done."""
        out = self.worktree / "pane-out.txt"  # the worktree is all a pane can write
        script = write_fake_harness(
            self.repo.parent / "pane.sh", "\n".join([f'OUT="{out}"', *lines, 'echo done >> "$OUT"', "sleep 15"])
        )
        sessions.start_session_via_server(self.socket, "pm-test-sbpane-s01a0", self.worktree, str(script), {})
        self.assertTrue(self.wait_for(lambda: out.is_file() and "done" in out.read_text(encoding="utf-8")))
        return out.read_text(encoding="utf-8").splitlines()


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

        outcome = sandbox.stop_server(socket=self.socket, record=record)
        self.assertEqual(outcome.remaining, [])
        self.assertFalse(outcome.srt_alive)
        self.assertEqual(outcome.left, [])
        self.assertFalse(sessions.server_running(self.socket))
        self.assertFalse(self.tmpdir.exists())
        self.assertFalse(self.control.exists())
        self.assertNotIn(f"srt still running: {srt_pid}", outcome.notes)
        self.assertIn(f"removed sandbox temp dir {self.tmpdir}", outcome.notes)
        self.assertIn(f"removed sandbox control dir {self.control}", outcome.notes)
        with self.assertRaises(ProcessLookupError):
            os.kill(srt_pid, 0)

        outcome = sandbox.stop_server(socket=self.socket, record=record)
        self.assertEqual((outcome.tmpdir_state, outcome.control_state), ("gone", "gone"))
        self.assertIn(f"sandbox temp dir already gone: {self.tmpdir}", outcome.notes)
        self.assertIn(f"sandbox control dir already gone: {self.control}", outcome.notes)

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

    def test_a_pane_cannot_write_the_common_config_or_hooks(self) -> None:
        # The escape denies are the assumption whose failure would be worst:
        # a writable config or hook runs code in the host's next git command.
        config = self.common / "config"
        before = config.read_bytes()
        hook = self.common / "hooks" / "post-checkout"
        self.start(self.write_policy())
        sandbox.verify_server(socket=self.socket, worktree=self.worktree)
        pointer = self.git_dir / "commondir"
        pointer_before = pointer.read_bytes()
        lines = self.run_in_pane(
            f'echo "[x]" >> "{config}" 2>/dev/null; echo "config_rc=$?" >> "$OUT"',
            f'touch "{hook}" 2>/dev/null; echo "hooks_rc=$?" >> "$OUT"',
            f'echo "/tmp" > "{pointer}" 2>/dev/null; echo "commondir_rc=$?" >> "$OUT"',
        )
        self.assertNotIn("config_rc=0", lines)
        self.assertNotIn("hooks_rc=0", lines)
        self.assertNotIn("commondir_rc=0", lines)
        self.assertIn("done", lines)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(hook.exists())
        self.assertEqual(pointer.read_bytes(), pointer_before)

    def test_the_config_deny_holds_in_a_plain_checkout_whose_git_dir_is_granted(self) -> None:
        # Here the whole git dir is granted, and `allowGitConfig` drops srt's
        # own cwd-anchored `.git/config` deny, so only the toolkit's deny
        # stands between a pane and the config.
        config = self.common / "config"
        before = config.read_bytes()
        base = minimal_base_policy()
        base["filesystem"]["allowGitConfig"] = True
        policy = sandbox.derive_policy(
            base,
            worktree=self.repo,
            branch="main",
            socket=self.socket,
            tmpdir=self.tmpdir,
            control_root=sandbox.control_root(self.common),
            pm_inside=False,
        )
        policy_path = self.control / "plain-policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        self.srt_pid, _server_pid = sandbox.start_server(
            socket=self.socket,
            policy_path=policy_path,
            worktree=self.repo,
            tmpdir=self.tmpdir,
            log_path=self.control / "srt.log",
        )
        sandbox.verify_server(socket=self.socket, worktree=self.repo)
        returncode, _output = sessions.run_shell(self.socket, f'echo "[x]" >> "{config}"')
        self.assertNotEqual(returncode, 0)
        self.assertEqual(config.read_bytes(), before)
        # git honours `.git/commondir` here too; the deny holds although the
        # file does not exist (git never writes it in a plain repository).
        pointer = self.common / "commondir"
        returncode, _output = sessions.run_shell(self.socket, f'echo "/tmp" > "{pointer}"')
        self.assertNotEqual(returncode, 0)
        self.assertFalse(pointer.exists())
        returncode, _output = sessions.run_shell(self.socket, f'touch "{_real(self.repo)}/inside"')
        self.assertEqual(returncode, 0)  # the git dir's own checkout is writable

    def test_a_policy_denying_the_probe_directory_fails_the_positive_probe(self) -> None:
        (self.worktree / ".pm").mkdir()
        base = minimal_base_policy()
        base["filesystem"]["denyWrite"] = [str(self.worktree / ".pm")]
        self.start(self.write_policy(base))
        with self.assertRaises(PmError) as ctx:
            sandbox.verify_server(socket=self.socket, worktree=self.worktree)
        self.assertIn("positive probe", str(ctx.exception))
        self.assertFalse((self.worktree / ".pm" / "sandbox-probe").exists())


class TestSandboxedServerAtAGlobPath(SandboxServerTestCase):
    worktree_name = "wt[1]"

    def test_a_sibling_matching_the_worktree_as_a_pattern_stays_unwritable(self) -> None:
        sibling = self.repo.parent / "wt1"
        sibling.write_text("original\n", encoding="utf-8")
        self.start(self.write_policy())
        sandbox.verify_server(socket=self.socket, worktree=self.worktree)
        lines = self.run_in_pane(
            f'echo x >> "{sibling}" 2>/dev/null; echo "sibling_rc=$?" >> "$OUT"',
            f'touch "{self.worktree}/inside"; echo "inside_rc=$?" >> "$OUT"',
        )
        self.assertIn("inside_rc=0", lines)
        self.assertNotIn("sibling_rc=0", lines)
        self.assertEqual(sibling.read_text(encoding="utf-8"), "original\n")


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

"""`campaign-preflight`: the mechanical half of a campaign's reconcile."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pm_test_helpers import PmTestCase, minimal_base_policy, write_base_policy

from pm_lib import PmError
from pm_lib import preflight
from pm_lib import sandbox

TEMPLATE = Path(__file__).resolve().parent.parent / "references" / "dossier-template.md"
POLICY = json.dumps({"schema": 1, "protected_paths": ["instruments"]}) + "\n"


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class PreflightTestCase(PmTestCase):
    """A product repository with a committed policy file and an integration
    branch, a dossier repository with a brief that pins both, and a setup
    script that creates one git-ignored file."""

    def setUp(self) -> None:
        super().setUp()
        (self.repo / ".gitignore").write_text("scaffold\n", encoding="utf-8")
        (self.repo / "campaign-policy.json").write_text(POLICY, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "policy and ignore")
        self.base = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("branch", "integration/t", self.base)
        self.parent = self.repo.parent / "worktrees"
        self.parent.mkdir()
        self.dossier = self.repo.parent / "dossier"
        self.dossier.mkdir()
        self.setup = "ln -s README.md scaffold\n"
        (self.dossier / "setup.sh").write_text(self.setup, encoding="utf-8")
        self.write_brief()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.dossier)], check=True)
        self._commit_dossier()

    def write_brief(self, *, policy_sha: str | None = None, setup_sha: str | None = None, extra: str = "") -> None:
        policy_sha = policy_sha or _sha(POLICY)
        setup_sha = setup_sha or _sha(self.setup)
        (self.dossier / "brief.md").write_text(
            "# Brief: t\n\n## Pinned references\n"
            f"- Campaign base: `main` at `{self.base}`.\n"
            "## Allowed paths\n"
            f"- Node worktree parent: `{self.parent}`.\n"
            f"- Protected-paths policy file: `campaign-policy.json`, SHA-256 `{policy_sha}`.\n"
            f"## Worktree setup\n- `setup.sh` in this dossier, SHA-256 `{setup_sha}`.\n"
            f"## Budgets\n- max_parallel: 1{extra}\n",
            encoding="utf-8",
        )

    def _commit_dossier(self) -> None:
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
                "-q",
                "-m",
                "brief",
            ],
            check=True,
        )

    def preflight(self, *extra: str) -> tuple[int, str, str]:
        """Run the command with tmux stubbed present, so the machine's tmux
        does not decide a test about the brief."""
        argv = [
            "campaign-preflight",
            "--dossier",
            str(self.dossier),
            "--repo",
            str(self.repo),
            "--integration-branch",
            "integration/t",
        ]
        real_which = preflight.shutil.which
        with mock.patch.object(
            preflight.shutil, "which", side_effect=lambda n: "/usr/bin/tmux" if n == "tmux" else real_which(n)
        ):
            return self.run_cli(argv + list(extra))

    def failures(self, out: str) -> list[str]:
        return [line[6:] for line in out.splitlines() if line.startswith("FAIL  ")]


class TestPreflight(PreflightTestCase):
    def test_a_sound_campaign_passes_with_the_cm_files_mid_edit_and_the_trial_leaves_no_worktree(self) -> None:
        checkout = self.repo.parent / "integration"
        self._git("worktree", "add", "-q", str(checkout), "integration/t")
        (self.dossier / "work.md").write_text("## Now\n- mid-edit, uncommitted\n", encoding="utf-8")
        digest = hashlib.sha256((self.dossier / "brief.md").read_bytes()).hexdigest()
        code, out, err = self.preflight("--digest", digest, "--worktree", str(checkout))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.failures(out), [])
        self.assertIn("brief.md matches the owner's digest", out)
        self.assertIn(f"integration checkout {checkout} is on integration/t at its head", out)
        self.assertIn("setup.sh runs in a fresh worktree and leaves it clean", out)
        self.assertIn("note  trust:", out)
        self.assertEqual(self._git("worktree", "list", "--porcelain").stdout.count("worktree "), 2)
        self.assertNotIn("scaffold", self._git("status", "--short").stdout)

    def test_the_templates_own_brief_fails_the_placeholder_check(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        block = re.search(r"## `brief.md`\n\n```md\n(.*?)```", text, re.S).group(1)
        found = preflight.placeholders(block)
        self.assertIn("<campaign id>", found)
        self.assertIn("<spend limit>", found)
        self.assertEqual(preflight.placeholders("branches `campaign/<id>/<node>` only"), [])
        self.assertEqual(preflight.placeholders("- Campaign-wide: 12 nodes; `<SPEND_LIMIT>`."), ["<SPEND_LIMIT>"])

    def test_each_pin_mismatch_is_its_own_failure_and_an_unverified_script_is_never_run(self) -> None:
        self.write_brief(policy_sha="0" * 64, setup_sha="1" * 64, extra="\n- Spend: <SPEND_LIMIT>")
        self._commit_dossier()
        code, out, err = self.preflight("--digest", "f" * 64)
        self.assertEqual(code, 1, out + err)
        failed = self.failures(out)
        self.assertIn("brief.md has no placeholders", failed)
        self.assertIn("brief.md matches the owner's digest", failed)
        self.assertIn("policy file at the integration head matches its pin", failed)
        self.assertIn("setup.sh matches its pin", failed)
        self.assertIn("<SPEND_LIMIT>", out)
        self.assertNotIn("setup.sh runs in a fresh worktree", out)
        self.assertEqual(len(failed), 4, failed)

    def test_a_digest_mismatch_alone_skips_the_setup_trial(self) -> None:
        code, out, err = self.preflight("--digest", "f" * 64)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.failures(out), ["brief.md matches the owner's digest", "setup.sh trial"])
        self.assertIn("skipped", out)

    def test_an_edited_brief_and_an_absent_worktree_parent_fail_and_no_digest_is_a_note(self) -> None:
        (self.dossier / "brief.md").write_text((self.dossier / "brief.md").read_text() + "\nedited\n", encoding="utf-8")
        os.rmdir(self.parent)
        code, out, err = self.preflight()
        self.assertEqual(code, 1, out + err)
        failed = self.failures(out)
        self.assertIn("brief.md is committed and unchanged", failed)
        self.assertTrue(any(f.startswith("node worktree parent") for f in failed), failed)
        self.assertIn("note  brief digest", out)

    def test_a_brief_with_no_setup_script_passes_and_a_historical_pin_is_not_read(self) -> None:
        brief = (self.dossier / "brief.md").read_text(encoding="utf-8")
        brief = brief.replace(f"- `setup.sh` in this dossier, SHA-256 `{_sha(self.setup)}`.", "- none")
        brief = brief.replace(
            "- Protected-paths policy file:",
            f"- Previous protected-paths policy file: `old.json`, SHA-256 `{'2' * 64}`.\n- Protected-paths policy file:",
        )
        (self.dossier / "brief.md").write_text(brief, encoding="utf-8")
        (self.dossier / "setup.sh").unlink()
        self._commit_dossier()
        code, out, err = self.preflight()
        self.assertEqual(code, 0, out + err)
        self.assertIn("ok    Worktree setup: none", out)
        self.assertIn("ok    policy file at the integration head matches its pin", out)
        self.assertIn("no owner digest was compared", out)

    def test_a_setup_script_that_dirties_the_worktree_fails_with_its_log_and_the_trial_is_removed(self) -> None:
        self.setup = "echo stray > stray.txt\necho scaffolding noise >&2\n"  # exits 0; the dirty tree is the failure
        (self.dossier / "setup.sh").write_text(self.setup, encoding="utf-8")
        self.write_brief()
        self._commit_dossier()
        code, out, err = self.preflight()
        self.assertEqual(code, 1, out + err)
        self.assertIn("setup.sh runs in a fresh worktree and leaves it clean", self.failures(out))
        self.assertIn("stray.txt", out)
        self.assertIn("scaffolding noise", out, "the log tail survives the trial's removal")
        self.assertEqual(self._git("worktree", "list", "--porcelain").stdout.count("worktree "), 1)

    def test_a_handoff_tracked_at_the_head_a_branch_off_the_base_and_a_missing_setup_section_fail(self) -> None:
        (self.repo / "HANDOFF.md").write_text("# h\n", encoding="utf-8")
        self._git("add", "HANDOFF.md")
        self._git("commit", "-q", "-m", "track handoff")
        self._git("branch", "-f", "integration/t", "HEAD")  # the head tracks HANDOFF.md and is not on the pinned base
        self._git("checkout", "-q", "--detach", "HEAD~1")
        self._git("commit", "-q", "--allow-empty", "-m", "another base")
        self.base = self._git("rev-parse", "HEAD").stdout.strip()
        self.write_brief()
        brief = (self.dossier / "brief.md").read_text(encoding="utf-8").replace("## Worktree setup", "## Scaffolding")
        (self.dossier / "brief.md").write_text(brief, encoding="utf-8")
        self._commit_dossier()
        code, out, err = self.preflight("--worktree", str(self.repo.parent / "absent"))
        self.assertEqual(code, 1, out + err)
        failed = self.failures(out)
        self.assertIn("integration head does not track HANDOFF.md", failed)
        self.assertIn("integration head descends from the base", failed)
        self.assertIn("brief has a Worktree setup section", failed)
        self.assertTrue(any(f.startswith("integration checkout") for f in failed), failed)

    def test_the_command_takes_no_run_or_token(self) -> None:
        import argparse

        from pm_lib import cli

        subparsers = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        flags = {o for a in subparsers.choices["campaign-preflight"]._actions for o in a.option_strings}
        self.assertFalse(flags & {"--run", "--token"}, flags)


class TestBriefSandboxPin(unittest.TestCase):
    """`sandbox.brief_sandbox_pin` and `sandbox.find_brief`, which launch-child and preflight share."""

    PIN = "a" * 64

    def test_no_section_and_none_are_no_pin_and_a_pinned_policy_returns_its_digest(self) -> None:
        self.assertIsNone(sandbox.brief_sandbox_pin("# Brief\n\n## Worktree setup\n- none\n"))
        self.assertIsNone(sandbox.brief_sandbox_pin("## Sandbox\n- none\n\n## Budgets\n- sandbox.json: later\n"))
        pinned = f"## Sandbox\n- `sandbox.json` in this dossier, SHA-256 `{self.PIN}`.\n## Budgets\n"
        self.assertEqual(sandbox.brief_sandbox_pin(pinned), self.PIN)

    def test_a_policy_named_without_a_pin_or_a_section_naming_neither_is_refused(self) -> None:
        for section, fragment in (
            ("- `sandbox.json` in this dossier.", "without its pin"),
            ("- `sandbox.json`, SHA-256 `ABC`.", "without its pin"),
            ("- to be decided", "names neither"),
        ):
            with self.subTest(section=section), self.assertRaises(PmError) as raised:
                sandbox.brief_sandbox_pin(f"## Sandbox\n{section}\n")
            self.assertIn(fragment, str(raised.exception))

    def test_find_brief_walks_up_to_the_nearest_brief(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(os.path.realpath(directory.name))
        launcher = root / "dossier" / "nodes" / "n1" / "launcher.md"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("# Launcher\n", encoding="utf-8")
        (root / "dossier" / "brief.md").write_text("# Brief\n", encoding="utf-8")
        self.assertEqual(sandbox.find_brief(launcher), root / "dossier" / "brief.md")
        (root / "dossier" / "nodes" / "brief.md").write_text("# Nearer\n", encoding="utf-8")
        self.assertEqual(sandbox.find_brief(launcher), root / "dossier" / "nodes" / "brief.md")


class TestPreflightSandbox(PreflightTestCase):
    """The brief's `## Sandbox` section: `none`, or a pinned `sandbox.json`
    checked against its pin, `load_base_policy` and `srt` on PATH. A fake
    `srt` first on PATH makes the version line machine-independent."""

    def setUp(self) -> None:
        super().setUp()
        fake_bin = self.repo.parent / "fake-bin"
        fake_bin.mkdir()
        srt = fake_bin / "srt"
        srt.write_text("#!/bin/sh\necho 0.0.81-test\n", encoding="utf-8")
        srt.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.policy = minimal_base_policy()

    def pin_sandbox(self, *, policy: dict | None = None, sha: str | None = None, section: str | None = None) -> None:
        """Write `sandbox.json` when `policy` is given, add a `## Sandbox` section to the brief (`section`, or a
        pin of `sha` or the file's own digest), and commit the dossier."""
        path = self.dossier / "sandbox.json"
        if policy is not None:
            write_base_policy(path, policy)
        if section is None:
            digest = sha or hashlib.sha256(path.read_bytes()).hexdigest()
            section = f"- `sandbox.json` in this dossier, SHA-256 `{digest}`."
        brief = self.dossier / "brief.md"
        brief.write_text(brief.read_text(encoding="utf-8") + f"## Sandbox\n{section}\n", encoding="utf-8")
        self._commit_dossier()

    def run_preflight(self, *, srt_present: bool = True) -> tuple[int, str]:
        real_which = shutil.which

        def which(name: str, *args, **kwargs) -> str | None:
            if name == "tmux":
                return "/usr/bin/tmux"
            if name == "srt" and not srt_present:
                return None
            return real_which(name, *args, **kwargs)

        argv = ["campaign-preflight", "--dossier", str(self.dossier), "--repo", str(self.repo)]
        with mock.patch.object(preflight.shutil, "which", side_effect=which):
            code, out, err = self.run_cli(argv + ["--integration-branch", "integration/t"])
        self.assertEqual(err, "")
        return code, out

    def assert_failed_with_instruction(self, out: str, name: str, fragment: str) -> None:
        lines = out.splitlines()
        self.assertIn(f"FAIL  {name}", lines, out)
        instruction = lines[lines.index(f"FAIL  {name}") + 1]
        self.assertTrue(instruction.startswith("      -> "), instruction)
        self.assertIn(fragment, instruction)

    def test_a_brief_without_a_sandbox_section_or_with_none_passes_with_sandbox_none(self) -> None:
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertIn("ok    sandbox: none\n", out)
        self.assertNotRegex(out, r"(?m)^(ok|FAIL) +srt")
        self.pin_sandbox(section="- none")
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertIn("ok    sandbox: none\n", out)
        self.assertNotRegex(out, r"(?m)^(ok|FAIL) +srt")

    def test_a_pinned_valid_policy_passes_and_each_warning_is_a_note(self) -> None:
        self.policy["enableWeakerNetworkIsolation"] = True
        self.pin_sandbox(policy=self.policy)
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.failures(out), [])
        for line in (
            "ok    sandbox.json is committed and unchanged",
            "ok    sandbox.json matches its pin",
            "ok    sandbox.json is a valid sandbox policy",
            "ok    srt 0.0.81-test on PATH",
        ):
            self.assertIn(f"{line}\n", out)
        notes = [line for line in out.splitlines() if line.startswith("note  sandbox policy")]
        self.assertEqual(len(notes), 1, out)
        self.assertIn("enableWeakerNetworkIsolation", notes[0])

    def test_a_pinned_policy_missing_from_the_dossier_fails(self) -> None:
        self.pin_sandbox(sha="0" * 64)
        code, out = self.run_preflight()
        self.assertEqual(code, 1, out)
        self.assert_failed_with_instruction(out, "sandbox.json is in the dossier", "commit it beside brief.md")

    def test_a_policy_that_does_not_match_its_pin_fails_and_is_not_validated(self) -> None:
        self.pin_sandbox(policy=self.policy, sha="0" * 64)
        code, out = self.run_preflight()
        self.assertEqual(code, 1, out)
        self.assert_failed_with_instruction(out, "sandbox.json matches its pin", "question to the owner")
        self.assertNotIn("sandbox.json is a valid sandbox policy", out)

    def test_an_invalid_pinned_policy_fails_naming_the_reason(self) -> None:
        self.policy["allowAppleEvents"] = True
        self.pin_sandbox(policy=self.policy)
        code, out = self.run_preflight()
        self.assertEqual(code, 1, out)
        self.assert_failed_with_instruction(out, "sandbox.json is a valid sandbox policy", "allowAppleEvents")

    def test_srt_absent_fails_when_a_policy_is_pinned(self) -> None:
        self.pin_sandbox(policy=self.policy)
        code, out = self.run_preflight(srt_present=False)
        self.assertEqual(code, 1, out)
        self.assert_failed_with_instruction(out, "srt on PATH", "srt is required")

    def test_a_changed_policy_fails_the_owner_file_check_and_a_malformed_section_fails(self) -> None:
        self.pin_sandbox(policy=self.policy)
        (self.dossier / "sandbox.json").write_text("{}\n", encoding="utf-8")
        code, out = self.run_preflight()
        self.assertEqual(code, 1, out)
        self.assertIn("sandbox.json is committed and unchanged", self.failures(out))
        brief = self.dossier / "brief.md"
        unpinned = brief.read_text(encoding="utf-8").replace(
            "- `sandbox.json` in this dossier, SHA-256 `", "- `sandbox.json`, digest `"
        )
        brief.write_text(unpinned, encoding="utf-8")
        code, out = self.run_preflight()
        self.assertEqual(code, 1, out)
        self.assert_failed_with_instruction(
            out, "brief's Sandbox section names sandbox.json or none", "without its pin"
        )


if __name__ == "__main__":
    unittest.main()

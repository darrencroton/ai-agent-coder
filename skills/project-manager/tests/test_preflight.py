"""`campaign-preflight`: the mechanical half of a campaign's reconcile."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pm_test_helpers import PmTestCase

from pm_lib import preflight

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


if __name__ == "__main__":
    unittest.main()

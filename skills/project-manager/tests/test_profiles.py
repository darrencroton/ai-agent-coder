"""Protected behaviours: harness launch-command composition and model inventory.

Pins the five harness profiles (target-design/replacement-ledger §9.1 — the
observed executables and override flags are sanctioned operational data;
the composing code is written fresh):

- `HARNESS_PROFILES` has exactly the five supported harnesses: codex,
  claude, copilot, opencode, qwen, each with an `executable` matching its
  harness name (`slice_ops.init` resolves that key on PATH).
- An unknown harness name raises `PmError` naming all five supported harnesses.
- `compose_headless_command` composes the frozen Developer and Reviewer
  one-shot forms.
- Model overrides: codex/opencode use `-m <model>`; claude/copilot/qwen use
  `--model <model>` on their headless commands.
- Effort overrides: codex composes `-c model_reasoning_effort="<effort>"`;
  claude/copilot use `--effort <effort>`; opencode uses `--variant <effort>`
  (the CLI's own "provider-specific reasoning effort" control); qwen has no
  headless effort mechanism, so an effort request fails closed with a
  `PmError` at compose time (never silently dropped, never a broken launch
  command). Every profile carrying an effort mechanism must actually emit it
  in both modes — pinned table-driven, because the composer once passed a
  throwaway list to the effort appender, which would turn a newly-added
  effort flag into a silent no-op.
- `compose_resume_command` composes the frozen Developer resume form for all
  five harnesses and preserves Codex's linked-worktree git access.
- `query_model_identity` returns `None` for codex/claude/copilot/qwen (no
  inventory contract). For opencode it runs `opencode models <provider>
  --verbose` (provider = text before the first `/` in the model id) and:
  parses the verbose-JSON display-name metadata following the matched
  model line when found; fails closed (`PmError`) when the query process
  exits non-zero, when the requested model id is absent from the
  inventory output, and when the JSON metadata following the model line is
  malformed or missing a non-empty `name` field.
- `parse_reviewer_tools` splits a comma-separated string, lowercases and
  strips each entry, and returns an empty tuple for `None`/empty input.
"""

from __future__ import annotations

import subprocess
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import sys

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import PmError
from pm_lib import profiles
import verify_harness_argv


class TestHarnessProfileTable(unittest.TestCase):
    def test_exactly_five_supported_harnesses(self) -> None:
        expected = ("codex", "claude", "copilot", "opencode", "qwen")
        self.assertEqual(profiles.SUPPORTED_HARNESSES, expected)
        self.assertEqual(set(profiles.HARNESS_PROFILES), set(expected))

    def test_executable_names_match_the_harness_names(self) -> None:
        # `slice_ops.init` resolves this key on PATH before accepting a run,
        # so a renamed or missing entry would break launch pre-flight.
        for harness in profiles.SUPPORTED_HARNESSES:
            with self.subTest(harness=harness):
                self.assertEqual(profiles.HARNESS_PROFILES[harness]["executable"], harness)

    def test_every_effort_mechanism_in_the_table_reaches_the_composed_argv(self) -> None:
        # Regression guard for a real trap: the composer used to pass a
        # THROWAWAY list to `_append_headless_effort` for the harnesses that had
        # no effort mechanism, purely to make it raise. Adding an effort flag to
        # such a profile would then append it to the discarded list and the flag
        # would never reach the launch command — configured-looking and inert.
        # This walks the table rather than naming harnesses, so it holds for any
        # profile a later slice gives an effort mechanism.
        for harness, profile in profiles.HARNESS_PROFILES.items():
            effort_flag = profile.get("effort_flag")
            effort_config_key = profile.get("effort_config_key")
            for mode in ("developer", "reviewer"):
                with self.subTest(harness=harness, mode=mode):
                    if not effort_flag and not effort_config_key:
                        with self.assertRaises(PmError):
                            profiles.compose_headless_command(
                                harness, "POINTER", mode=mode, repo=Path("/repo"), effort="high"
                            )
                        continue
                    command = profiles.compose_headless_command(
                        harness, "POINTER", mode=mode, repo=Path("/repo"), effort="high"
                    )
                    if effort_flag:
                        self.assertIn(effort_flag, command)
                        self.assertEqual(command[command.index(effort_flag) + 1], "high")
                    else:
                        self.assertIn(f'{effort_config_key}="high"', command)

    def test_unknown_harness_error_names_every_supported_harness(self) -> None:
        with self.assertRaises(PmError) as ctx:
            profiles.compose_headless_command("gemini", "POINTER", mode="developer", repo=Path("/repo"))
        message = str(ctx.exception)
        for name in profiles.SUPPORTED_HARNESSES:
            self.assertIn(name, message)


class TestHarnessArgvVerifier(unittest.TestCase):
    def test_nonzero_help_exit_is_inconclusive(self) -> None:
        error = subprocess.CalledProcessError(2, ["tool", "--help"], stderr="help failed")
        with mock.patch.object(verify_harness_argv, "verify", side_effect=error):
            with mock.patch("builtins.print") as print_mock:
                result = verify_harness_argv.main(["--harness", "codex"])
        self.assertEqual(result, 2)
        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("INCONCLUSIVE", output)
        self.assertIn("help failed", output)

    def test_composed_variants_cover_flags_without_redundant_combinations(self) -> None:
        commands = verify_harness_argv._composed("codex")
        self.assertEqual(sum(kind == "launch" for kind, _command in commands), 4)
        launch_flags = {
            flag
            for kind, command in commands
            if kind == "launch"
            for flag in verify_harness_argv._flags(command)
        }
        self.assertIn("-m", launch_flags)
        self.assertIn("-c", launch_flags)

        qwen_commands = verify_harness_argv._composed("qwen")
        self.assertEqual(sum(kind == "launch" for kind, _command in qwen_commands), 2)
        self.assertTrue(
            any("--model" in command for kind, command in qwen_commands if kind == "launch")
        )


class TestComposeHeadlessDeveloperCommand(unittest.TestCase):
    _repo = Path("/repo")
    _git_dir = Path("/repo/.git")
    _session_id = "11111111-1111-1111-1111-111111111111"

    def test_claude(self) -> None:
        self.assertEqual(
            profiles.compose_headless_command(
                "claude", "POINTER", mode="developer", repo=self._repo,
                model="opus", effort="medium", session_id=self._session_id,
            ),
            [
                "claude", "-p", "POINTER", "--model", "opus", "--effort", "medium",
                "--permission-mode", "acceptEdits", "--session-id", self._session_id, "--add-dir", "/repo",
            ],
        )

    def test_codex_including_linked_worktree_git_access(self) -> None:
        self.assertEqual(
            profiles.compose_headless_command(
                "codex", "POINTER", mode="developer", repo=self._repo,
                model="gpt-5", effort="high", git_access_dir=self._git_dir,
            ),
            [
                "codex", "exec", "POINTER", "-m", "gpt-5", "-c", 'model_reasoning_effort="high"',
                "--sandbox", "workspace-write", "--skip-git-repo-check", "-C", "/repo", "--add-dir", "/repo/.git",
            ],
        )

    def test_copilot(self) -> None:
        self.assertEqual(
            profiles.compose_headless_command(
                "copilot", "POINTER", mode="developer", repo=self._repo,
                model="gpt-5", effort="high", session_id=self._session_id,
            ),
            [
                "copilot", "-p", "POINTER", "--model", "gpt-5", "--effort", "high",
                "--allow-all-tools", "--autopilot", "--session-id", self._session_id, "--add-dir", "/repo",
            ],
        )

    def test_opencode(self) -> None:
        self.assertEqual(
            profiles.compose_headless_command(
                "opencode", "POINTER", mode="developer", repo=self._repo,
                model="local/model", effort="high",
            ),
            ["opencode", "run", "POINTER", "-m", "local/model", "--variant", "high", "--agent", "build", "--auto", "--dir", "/repo"],
        )

    def test_opencode_without_effort_omits_the_variant_flag(self) -> None:
        self.assertEqual(
            profiles.compose_headless_command("opencode", "POINTER", mode="developer", repo=self._repo, model="local/model"),
            ["opencode", "run", "POINTER", "-m", "local/model", "--agent", "build", "--auto", "--dir", "/repo"],
        )

    def test_qwen(self) -> None:
        self.assertEqual(
            profiles.compose_headless_command("qwen", "POINTER", mode="developer", repo=self._repo, model="qwen-max"),
            ["qwen", "--prompt", "POINTER", "--model", "qwen-max", "--sandbox", "--output-format", "text"],
        )

    def test_qwen_effort_fails_closed(self) -> None:
        # Qwen Code's option list carries nothing reasoning-related, so an
        # effort request must raise rather than launch a command that silently
        # ignores it.
        with self.assertRaises(PmError) as ctx:
            profiles.compose_headless_command("qwen", "POINTER", mode="developer", repo=self._repo, effort="high")
        self.assertIn("headless", str(ctx.exception))

    def test_invalid_mode_and_unknown_harness_fail_closed(self) -> None:
        with self.assertRaises(PmError):
            profiles.compose_headless_command("claude", "POINTER", mode="invalid", repo=self._repo)
        with self.assertRaises(PmError):
            profiles.compose_headless_command("not-a-harness", "POINTER", mode="developer", repo=self._repo)


class TestComposeHeadlessReviewerCommand(unittest.TestCase):
    _repo = Path("/repo")

    def test_reviewer_shapes_preserve_existing_commands(self) -> None:
        cases = [
            (
                "codex", {"model": "gpt-5", "effort": "high"},
                ["codex", "exec", "PROMPT", "-m", "gpt-5", "-c", 'model_reasoning_effort="high"', "--sandbox", "read-only", "--skip-git-repo-check", "-C", "/repo"],
            ),
            (
                "claude", {"model": "opus", "effort": "high"},
                ["claude", "-p", "PROMPT", "--model", "opus", "--effort", "high", "--permission-mode", "plan", "--output-format", "text", "--add-dir", "/repo"],
            ),
            (
                "copilot", {"model": "gpt-5", "effort": "high"},
                ["copilot", "--model", "gpt-5", "--effort", "high", "-p", "PROMPT", "--allow-all-tools", "--autopilot", "--silent", "--add-dir", "/repo"],
            ),
            (
                "opencode", {"model": "my-model", "effort": "high"},
                ["opencode", "run", "PROMPT", "-m", "my-model", "--variant", "high", "--agent", "plan", "--auto", "--dir", "/repo"],
            ),
            (
                "qwen", {"model": "qwen-max"},
                ["qwen", "--prompt", "PROMPT", "--model", "qwen-max", "--sandbox", "--output-format", "text"],
            ),
        ]
        for harness, kwargs, expected in cases:
            with self.subTest(harness=harness):
                self.assertEqual(
                    profiles.compose_headless_command(harness, "PROMPT", mode="reviewer", repo=self._repo, **kwargs), expected
                )

    def test_opencode_without_effort_omits_the_variant_flag(self) -> None:
        # The mode-specific mirror of the developer assertion: the table-walking
        # guard in TestHarnessProfileTable only ever composes WITH an effort, so
        # without this a reviewer-only change that started emitting a default
        # `--variant` when no effort was requested would go unnoticed.
        self.assertEqual(
            profiles.compose_headless_command("opencode", "PROMPT", mode="reviewer", repo=self._repo, model="my-model"),
            ["opencode", "run", "PROMPT", "-m", "my-model", "--agent", "plan", "--auto", "--dir", "/repo"],
        )

    def test_qwen_effort_fails_closed(self) -> None:
        with self.assertRaises(PmError) as ctx:
            profiles.compose_headless_command("qwen", "PROMPT", mode="reviewer", repo=self._repo, effort="high")
        self.assertIn("headless", str(ctx.exception))


class TestComposeResumeCommand(unittest.TestCase):
    _repo = Path("/repo")
    _git_dir = Path("/repo/.git")
    _session_id = "session-123"

    def test_frozen_resume_shapes(self) -> None:
        cases = [
            (
                "claude", {},
                ["claude", "-p", "CORRECTION", "--resume", self._session_id, "--permission-mode", "acceptEdits", "--add-dir", "/repo"],
            ),
            (
                "codex", {"git_access_dir": self._git_dir},
                [
                    "codex", "exec", "resume", self._session_id, "CORRECTION",
                    "-c", 'sandbox_mode="workspace-write"',
                    "-c", 'sandbox_workspace_write.writable_roots=["/repo/.git"]',
                    "--skip-git-repo-check",
                ],
            ),
            (
                "copilot", {},
                ["copilot", "-p", "CORRECTION", "--resume=session-123", "--allow-all-tools", "--autopilot", "--add-dir", "/repo"],
            ),
            (
                "opencode", {},
                ["opencode", "run", "CORRECTION", "--session", self._session_id, "--agent", "build", "--auto", "--dir", "/repo"],
            ),
            (
                "qwen", {},
                ["qwen", "--prompt", "CORRECTION", "--resume", self._session_id, "--sandbox", "--output-format", "text"],
            ),
        ]
        for harness, kwargs, expected in cases:
            with self.subTest(harness=harness):
                self.assertEqual(
                    profiles.compose_resume_command(harness, "CORRECTION", session_id=self._session_id, repo=self._repo, **kwargs), expected
                )

    def test_empty_session_id_fails_closed(self) -> None:
        with self.assertRaises(PmError):
            profiles.compose_resume_command("codex", "CORRECTION", session_id="", repo=self._repo)

    def test_unknown_harness_and_omitted_codex_git_access_fail_or_compose_as_expected(self) -> None:
        with self.assertRaises(PmError):
            profiles.compose_resume_command("not-a-harness", "CORRECTION", session_id=self._session_id, repo=self._repo)
        command = profiles.compose_resume_command("codex", "CORRECTION", session_id=self._session_id, repo=self._repo)
        self.assertNotIn("--add-dir", command)
        # No git access requested means no writable-roots override at all,
        # rather than an empty array that would narrow the sandbox.
        self.assertEqual(
            command,
            [
                "codex", "exec", "resume", self._session_id, "CORRECTION",
                "-c", 'sandbox_mode="workspace-write"',
                "--skip-git-repo-check",
            ],
        )

    def test_codex_resume_omits_every_flag_codex_exec_resume_rejects(self) -> None:
        """`codex exec resume` accepts only --skip-git-repo-check of the launch
        turn's four flags; the other three are argument-parsing errors, which
        made `finalize --steer` against a codex Developer impossible. The
        capabilities survive as -c config overrides, verified against the
        installed CLI rather than inferred from the launch shape."""
        for kwargs in ({}, {"git_access_dir": self._git_dir}):
            with self.subTest(git_access_dir=bool(kwargs)):
                command = profiles.compose_resume_command(
                    "codex", "CORRECTION", session_id=self._session_id, repo=self._repo, **kwargs
                )
                for rejected in ("--sandbox", "-C", "--add-dir"):
                    self.assertNotIn(rejected, command)
                self.assertIn("--skip-git-repo-check", command)
                self.assertIn('sandbox_mode="workspace-write"', command)

    def test_codex_resume_writable_roots_value_is_escaped_toml(self) -> None:
        """The writable-roots value is generated, not concatenated, so a path
        containing a quote or backslash cannot break out of the override."""
        command = profiles.compose_resume_command(
            "codex", "CORRECTION", session_id=self._session_id, repo=self._repo,
            git_access_dir=Path('/repo/we"ird\\path/.git'),
        )
        self.assertIn(r'sandbox_workspace_write.writable_roots=["/repo/we\"ird\\path/.git"]', command)

    def test_codex_resume_writable_roots_survives_a_non_bmp_path(self) -> None:
        """A non-BMP character must be emitted literally, not as a `\\uXXXX`
        surrogate pair: TOML rejects surrogates as not being Unicode scalar
        values, so the escaped form would make codex fail in its config parser
        for any worktree whose path contains an emoji."""
        command = profiles.compose_resume_command(
            "codex", "CORRECTION", session_id=self._session_id, repo=self._repo,
            git_access_dir=Path("/repo/wt-\U0001f600/.git"),
        )
        value = next(part for part in command if part.startswith("sandbox_workspace_write."))
        self.assertIn("\U0001f600", value)
        self.assertNotIn("\\u", value)
        # It must be parseable as the TOML it claims to be.
        self.assertEqual(
            tomllib.loads(value)["sandbox_workspace_write"]["writable_roots"],
            ["/repo/wt-\U0001f600/.git"],
        )

    def test_non_codex_resume_shapes_are_untouched_by_the_codex_fix(self) -> None:
        """The F1 repair is codex-resume-only: the other four harnesses keep
        their --add-dir/--dir style access exactly as Slice 2 froze it."""
        for harness, expected_access in (
            ("claude", ["--add-dir", "/repo"]),
            ("copilot", ["--add-dir", "/repo"]),
            ("opencode", ["--dir", "/repo"]),
        ):
            with self.subTest(harness=harness):
                command = profiles.compose_resume_command(
                    harness, "CORRECTION", session_id=self._session_id, repo=self._repo
                )
                self.assertEqual(command[-2:], expected_access)
                self.assertNotIn("-c", command)


class TestQueryModelIdentityNoInventory(unittest.TestCase):
    def test_codex_claude_copilot_qwen_have_no_inventory_contract(self) -> None:
        for harness in ("codex", "claude", "copilot", "qwen"):
            with self.subTest(harness=harness):
                self.assertIsNone(profiles.query_model_identity(harness, "some-model"))

    def test_unknown_harness_fails_closed(self) -> None:
        with self.assertRaises(PmError):
            profiles.query_model_identity("gemini", "some-model")


class TestQueryModelIdentityOpencode(unittest.TestCase):
    def _mock_result(self, returncode: int, stdout: str = "", stderr: str = "") -> mock.Mock:
        result = mock.Mock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    def test_found_with_display_name(self) -> None:
        stdout = 'local/qwen3.6-35b\n{"name": "Qwen 3.6 35B Instruct", "context": 32000}\n'
        with mock.patch.object(profiles.subprocess, "run", return_value=self._mock_result(0, stdout)) as run:
            identity = profiles.query_model_identity("opencode", "local/qwen3.6-35b")
        self.assertEqual(identity["requested"], "local/qwen3.6-35b")
        self.assertEqual(identity["resolved_id"], "local/qwen3.6-35b")
        self.assertEqual(identity["display_name"], "Qwen 3.6 35B Instruct")
        self.assertIn("opencode models local --verbose", identity["inventory_command"])
        called_command = run.call_args[0][0]
        self.assertEqual(called_command, ["opencode", "models", "local", "--verbose"])

    def test_missing_model_fails_closed(self) -> None:
        stdout = "local/other-model\n{\"name\": \"Other\"}\n"
        with mock.patch.object(profiles.subprocess, "run", return_value=self._mock_result(0, stdout)):
            with self.assertRaises(PmError) as ctx:
                profiles.query_model_identity("opencode", "local/qwen3.6-35b")
        self.assertIn("qwen3.6-35b", str(ctx.exception))

    def test_query_failure_fails_closed(self) -> None:
        with mock.patch.object(
            profiles.subprocess, "run", return_value=self._mock_result(1, "", "no such provider")
        ):
            with self.assertRaises(PmError) as ctx:
                profiles.query_model_identity("opencode", "local/qwen3.6-35b")
        self.assertIn("no such provider", str(ctx.exception))

    def test_malformed_json_fails_closed(self) -> None:
        stdout = "local/qwen3.6-35b\nnot valid json at all\n"
        with mock.patch.object(profiles.subprocess, "run", return_value=self._mock_result(0, stdout)):
            with self.assertRaises(PmError):
                profiles.query_model_identity("opencode", "local/qwen3.6-35b")

    def test_empty_display_name_fails_closed(self) -> None:
        stdout = 'local/qwen3.6-35b\n{"name": "  "}\n'
        with mock.patch.object(profiles.subprocess, "run", return_value=self._mock_result(0, stdout)):
            with self.assertRaises(PmError):
                profiles.query_model_identity("opencode", "local/qwen3.6-35b")

    def test_provider_is_text_before_first_slash(self) -> None:
        stdout = 'anthropic/claude-x\n{"name": "Claude X"}\n'
        with mock.patch.object(profiles.subprocess, "run", return_value=self._mock_result(0, stdout)) as run:
            profiles.query_model_identity("opencode", "anthropic/claude-x")
        called_command = run.call_args[0][0]
        self.assertEqual(called_command, ["opencode", "models", "anthropic", "--verbose"])

    def test_provider_defaults_to_whole_model_when_no_slash(self) -> None:
        stdout = 'bare-model\n{"name": "Bare Model"}\n'
        with mock.patch.object(profiles.subprocess, "run", return_value=self._mock_result(0, stdout)) as run:
            profiles.query_model_identity("opencode", "bare-model")
        called_command = run.call_args[0][0]
        self.assertEqual(called_command, ["opencode", "models", "bare-model", "--verbose"])


class TestParseReviewerTools(unittest.TestCase):
    def test_comma_separated_lowercased_stripped(self) -> None:
        self.assertEqual(profiles.parse_reviewer_tools(" Copilot , CODEX ,claude"), ("copilot", "codex", "claude"))

    def test_none_and_empty_return_empty_tuple(self) -> None:
        self.assertEqual(profiles.parse_reviewer_tools(None), ())
        self.assertEqual(profiles.parse_reviewer_tools(""), ())

    def test_blank_entries_dropped(self) -> None:
        self.assertEqual(profiles.parse_reviewer_tools("codex,, ,claude"), ("codex", "claude"))


if __name__ == "__main__":
    unittest.main()

"""Protected behaviours: harness launch-command composition and model inventory.

Pins the five harness profiles (target-design/replacement-ledger §9.1 — the
observed base commands and override flags are sanctioned operational data;
the composing code is written fresh):

- `HARNESS_PROFILES` has exactly the five supported harnesses: codex,
  claude, copilot, opencode, qwen.
- `compose_command` builds each harness's base command exactly as observed,
  each at its harness's fullest autonomy: codex `codex --no-alt-screen
  --dangerously-bypass-approvals-and-sandbox`; claude `claude
  --permission-mode bypassPermissions`; copilot `copilot --allow-all
  --autopilot`; opencode `opencode --auto`; qwen `qwen --yolo`.
- Model overrides: codex/opencode use `-m <model>`; claude/copilot use
  `--model <model>`; qwen uses `-m <model>`.
- Effort overrides: codex composes `-c model_reasoning_effort="<effort>"`;
  claude/copilot use `--effort <effort>`; opencode/qwen have no effort
  mechanism, so an effort request fails closed with a `PmError` at compose
  time (never silently dropped, never a broken launch command).
- claude-only composition: a `session_id` appends `--session-id <uuid>`;
  a no-op for the other four harnesses.
- An unknown harness name raises `PmError` naming all five supported harnesses.
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

import unittest
from pathlib import Path
from unittest import mock

import sys

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import PmError
from pm_lib import profiles


class TestHarnessProfileTable(unittest.TestCase):
    def test_exactly_five_supported_harnesses(self) -> None:
        expected = ("codex", "claude", "copilot", "opencode", "qwen")
        self.assertEqual(profiles.SUPPORTED_HARNESSES, expected)
        self.assertEqual(set(profiles.HARNESS_PROFILES), set(expected))


class TestComposeCommandBaseCommands(unittest.TestCase):
    BASE_COMMANDS = {
        "codex": "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox",
        "claude": "claude --permission-mode bypassPermissions",
        "copilot": "copilot --allow-all --autopilot",
        "opencode": "opencode --auto",
        "qwen": "qwen --yolo",
    }

    def test_base_commands(self) -> None:
        for harness, expected in self.BASE_COMMANDS.items():
            with self.subTest(harness=harness):
                self.assertEqual(profiles.compose_command(harness), expected)

    def test_every_supported_harness_is_pinned(self) -> None:
        self.assertEqual(set(self.BASE_COMMANDS), set(profiles.SUPPORTED_HARNESSES))


class TestComposeCommandOverrides(unittest.TestCase):
    def test_codex_model_and_effort(self) -> None:
        composed = profiles.compose_command("codex", model="o3", effort="high")
        # shlex.join shell-quotes the -c value because it contains embedded
        # double quotes; the underlying token is still model_reasoning_effort="high".
        self.assertEqual(
            composed,
            "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox "
            "-m o3 -c 'model_reasoning_effort=\"high\"'",
        )
        self.assertIn('model_reasoning_effort="high"', composed)

    def test_claude_model_and_effort(self) -> None:
        composed = profiles.compose_command("claude", model="sonnet", effort="medium")
        self.assertEqual(composed, "claude --permission-mode bypassPermissions --model sonnet --effort medium")

    def test_copilot_model_and_effort(self) -> None:
        composed = profiles.compose_command("copilot", model="gpt-5", effort="low")
        self.assertEqual(composed, "copilot --allow-all --autopilot --model gpt-5 --effort low")

    def test_opencode_model_only(self) -> None:
        composed = profiles.compose_command("opencode", model="local/qwen3.6")
        self.assertEqual(composed, "opencode --auto -m local/qwen3.6")

    def test_opencode_effort_fails_closed(self) -> None:
        with self.assertRaises(PmError) as ctx:
            profiles.compose_command("opencode", effort="high")
        self.assertIn("opencode", str(ctx.exception))

    def test_qwen_model_only(self) -> None:
        self.assertEqual(profiles.compose_command("qwen", model="qwen/qwen3.6"), "qwen --yolo -m qwen/qwen3.6")

    def test_qwen_effort_fails_closed(self) -> None:
        with self.assertRaises(PmError) as ctx:
            profiles.compose_command("qwen", effort="high")
        self.assertIn("qwen", str(ctx.exception))


class TestComposeCommandClaudeSpecific(unittest.TestCase):
    def test_session_id_flag(self) -> None:
        composed = profiles.compose_command("claude", session_id="11111111-1111-1111-1111-111111111111")
        self.assertEqual(
            composed, "claude --permission-mode bypassPermissions --session-id 11111111-1111-1111-1111-111111111111"
        )

    def test_session_id_is_a_noop_for_other_harnesses(self) -> None:
        composed = profiles.compose_command("codex", session_id="11111111-1111-1111-1111-111111111111")
        self.assertEqual(composed, "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox")


class TestComposeCommandUnknownHarness(unittest.TestCase):
    def test_unknown_harness_fails_closed_naming_supported_harnesses(self) -> None:
        with self.assertRaises(PmError) as ctx:
            profiles.compose_command("gemini")
        message = str(ctx.exception)
        for name in ("codex", "claude", "copilot", "opencode", "qwen"):
            self.assertIn(name, message)


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

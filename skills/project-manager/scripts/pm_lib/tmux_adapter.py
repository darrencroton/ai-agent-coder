from __future__ import annotations

import re
import shlex
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .constants import KNOWN_UNATTENDED_HARNESS_COMMANDS
from .models import PmError, PlanSlice
from .process import run_command
from .runtime import slice_environment


# Directory-trust / folder-trust dialogs from the interactive TUIs. If any of
# these is on screen when PM is about to submit, a blind Enter would confirm it
# (for example auto-trusting a directory) — exactly the kind of side effect PM
# must not cause. Detecting them lets PM fail closed instead.
TRUST_PROMPT_MARKERS = (
    "Do you trust the contents of this directory",
    "Do you trust the files in this folder",
    "Do you trust the files in this directory",
)

HARD_PROMPT_MARKERS: dict[str, tuple[str, ...]] = {
    "trust_prompt": TRUST_PROMPT_MARKERS,
    "approval_prompt": (
        "Do you want to proceed?",
        "Approve this action",
        "Allow this command",
        "requires approval",
        "approval required",
    ),
    "credential_prompt": (
        "Enter API key",
        "Enter password",
        "Enter your password",
        "Login required",
        "Please log in",
        "Sign in to continue",
        "MFA",
        "two-factor",
    ),
    "permission_prompt": (
        "Permission denied",
        "Grant permission",
        "requires permission",
        "allow access",
    ),
    "external_side_effect_request": (
        "push to remote",
        "create pull request",
        "install dependency",
        "license change",
    ),
}

EXTERNAL_SIDE_EFFECT_PROMPT_RE = re.compile(
    r"\b(?:do you want to|approve|confirm|allow|permission to|shall i|should i|ready to)\b"
    r"[^.\n?]{0,120}\b(?:push(?: to remote)?|create (?:a )?(?:pull request|pr)|open (?:a )?(?:pull request|pr)|"
    r"deploy|release|publish|install (?:a )?dependenc(?:y|ies)|change (?:the )?license|license change)\b"
    r"|"
    r"\b(?:push to remote|create (?:a )?(?:pull request|pr)|open (?:a )?(?:pull request|pr)|deploy|release|publish|"
    r"install (?:a )?dependenc(?:y|ies)|license change)\b[^.\n]{0,60}(?:\?|yes/no|\[y/n\]|approve|confirm)",
    re.IGNORECASE,
)


class TmuxHarnessAdapter:
    """Single tmux-backed harness adapter for the configured command."""

    def __init__(
        self,
        harness_name: str,
        command_override: str | None = None,
        allow_unattended_default: bool = False,
        reviewer_tools: tuple[str, ...] = (),
        expected_model_display: str | None = None,
    ):
        self.harness_name = harness_name
        self.command_override = command_override
        self.allow_unattended_default = allow_unattended_default
        self.reviewer_tools = reviewer_tools
        self.expected_model_display = expected_model_display
        if command_override:
            self.command = command_override
        elif allow_unattended_default and harness_name in KNOWN_UNATTENDED_HARNESS_COMMANDS:
            self.command = KNOWN_UNATTENDED_HARNESS_COMMANDS[harness_name]
        else:
            self.command = harness_name

    def preflight(self) -> None:
        if not shutil.which("tmux"):
            raise PmError("tmux is required for runtime execution")
        if not self.command.strip():
            raise PmError("harness command is empty")
        using_known_default = self.allow_unattended_default and self.harness_name in KNOWN_UNATTENDED_HARNESS_COMMANDS
        if not self.command_override and not using_known_default:
            if self.harness_name in KNOWN_UNATTENDED_HARNESS_COMMANDS:
                raise PmError(
                    f"harness {self.harness_name!r} defaults to an interactive session that will deadlock on an "
                    "unattended approval prompt (nothing here can answer it, so the run hangs until "
                    "--timeout-seconds expires). Pass --harness-command '<full non-interactive-approval "
                    f"command>', or pass --allow-unattended-default to use the known unattended-safe default: "
                    f"{KNOWN_UNATTENDED_HARNESS_COMMANDS[self.harness_name]!r}"
                )
            raise PmError(
                f"harness {self.harness_name!r} has no known unattended-safe default command; "
                "pass --harness-command with a full non-interactive-approval command"
            )
        executable = shlex.split(self.command)[0] if self.command.strip() else ""
        if not executable:
            raise PmError("harness command is empty")
        if not shutil.which(executable):
            raise PmError(f"harness command not found: {executable}")

    def build_shell_command(self, slice_artifact_dir: Path, run_json: Path, plan_path: Path, plan_slice: PlanSlice) -> str:
        env = slice_environment(slice_artifact_dir, run_json, plan_path, plan_slice, self.harness_name, self.reviewer_tools)
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}"
            for key, value in env.items()
        )
        return f"{env_prefix} {self.command}"

    def start(self, repo: Path, session_name: str, slice_artifact_dir: Path, run_json: Path, plan_path: Path, plan_slice: PlanSlice) -> None:
        self.preflight()
        shell_command = self.build_shell_command(slice_artifact_dir, run_json, plan_path, plan_slice)
        run_command(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                str(repo),
                shell_command,
            ],
            error_prefix="tmux start failed",
        )

    def _pane_text(self, session_name: str) -> str:
        # Embedded delegation contracts and frozen slice instructions can span
        # many terminal lines.
        # A real hard prompt printed just before prompt injection must remain
        # visible to the send-time guard instead of being pushed outside a
        # short scrollback window by the pasted instructions.
        result = run_command(["tmux", "capture-pane", "-p", "-S", "-32768", "-t", session_name], allow_failure=True)
        return result.stdout if result.returncode == 0 else ""

    @staticmethod
    def _raise_on_trust_prompt(executable: str, capture: str) -> None:
        normalized = re.sub(r"\s+", " ", capture)
        for marker in TRUST_PROMPT_MARKERS:
            if marker in normalized:
                raise PmError(
                    f"{executable} directory trust prompt blocked unattended launch; trust the repo before running PM"
                )

    @staticmethod
    def detect_hard_prompt(capture: str) -> dict[str, Any]:
        """Return conservative hard-prompt flags from visible pane text."""
        # tmux inserts display-width line wraps into captured text. Normalize
        # whitespace so a prompt split between two terminal rows remains a
        # prompt rather than bypassing the send guard.
        normalized = re.sub(r"\s+", " ", capture)
        lowered = normalized.lower()
        matches: dict[str, Any] = {
            "present": False,
            "kinds": [],
            "markers": [],
        }
        for kind, markers in HARD_PROMPT_MARKERS.items():
            if kind == "external_side_effect_request":
                match = EXTERNAL_SIDE_EFFECT_PROMPT_RE.search(normalized)
                if match:
                    matches["present"] = True
                    matches["kinds"].append(kind)
                    matches["markers"].append(match.group(0).strip())
                continue
            for marker in markers:
                if marker.lower() in lowered:
                    matches["present"] = True
                    if kind not in matches["kinds"]:
                        matches["kinds"].append(kind)
                    matches["markers"].append(marker)
        return matches

    def wait_until_prompt_ready(self, session_name: str) -> None:
        command_parts = shlex.split(self.command) if self.command.strip() else []
        executable = Path(command_parts[0]).name if command_parts else ""
        if executable == "codex":
            self._wait_codex_ready(session_name)
        elif executable == "claude":
            self._wait_claude_ready(session_name)
        elif executable == "opencode":
            self._wait_opencode_ready(session_name)
        elif executable == "copilot":
            self._wait_copilot_ready(session_name)
        # Any other executable (a custom --harness-command, a non-TUI harness)
        # has no interactive readiness handshake to perform.
        if executable == "opencode" and self.expected_model_display:
            self._verify_opencode_model_display(session_name)

    def _verify_opencode_model_display(self, session_name: str) -> None:
        """Fail closed when OpenCode's resolved TUI model differs from inventory metadata."""
        capture = self._pane_text(session_name)
        expected = re.sub(r"\s+", " ", self.expected_model_display or "").strip().lower()
        observed = re.sub(r"\s+", " ", capture).lower()
        if expected and expected not in observed:
            raise PmError(
                "opencode did not display the requested model identity before prompt injection: "
                f"expected {self.expected_model_display!r}; refusing possible silent fallback"
            )

    def _wait_stable_pane_ready(self, session_name: str, executable: str, deadline: float) -> None:
        # Readiness inferred from the TUI finishing its draw: a non-empty pane
        # unchanged across a short window. Used directly for harnesses with no
        # stable ready banner (Claude Code, Copilot) and as the fallback when a
        # banner-keyed harness updates its banner text (Codex, OpenCode). A
        # directory-trust dialog is caught explicitly and fails closed.
        # Reaching the deadline still returns: send_prompt's
        # settle-and-double-submit is the backstop, and any trust dialog would
        # have appeared (and been caught) well before then.
        previous = ""
        stable_since: float | None = None
        while time.monotonic() < deadline:
            if not self.session_exists(session_name):
                raise PmError(f"{executable} session exited before the prompt could be sent")
            capture = self._pane_text(session_name)
            self._raise_on_trust_prompt(executable, capture)
            if capture.strip() and capture == previous:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 1.5:
                    time.sleep(0.5)
                    return
            else:
                stable_since = None
            previous = capture
            time.sleep(0.25)

    def _wait_banner_ready(self, session_name: str, executable: str, is_ready: Callable[[str], bool]) -> None:
        # Banner-keyed readiness with a stable-pane fallback: banner strings
        # are version-fragile (a CLI update that rewords its banner must not
        # turn every launch into a hard failure), so if the banner never
        # appears, fall back to the same drawn-and-stable heuristic used for
        # Claude/Copilot instead of raising.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if not self.session_exists(session_name):
                raise PmError(f"{executable} session exited before the prompt could be sent")
            capture = self._pane_text(session_name)
            self._raise_on_trust_prompt(executable, capture)
            if is_ready(capture):
                time.sleep(0.5)
                return
            time.sleep(0.25)
        self._wait_stable_pane_ready(session_name, executable, time.monotonic() + 10.0)

    def _wait_codex_ready(self, session_name: str) -> None:
        self._wait_banner_ready(session_name, "codex", lambda capture: "OpenAI Codex" in capture and "›" in capture)

    def _wait_claude_ready(self, session_name: str) -> None:
        # Claude Code has no single stable "ready" banner PM can key on.
        self._wait_stable_pane_ready(session_name, "claude", time.monotonic() + 20.0)

    def _wait_opencode_ready(self, session_name: str) -> None:
        # Confirmed by reproduction: a freshly launched `opencode --auto`
        # session shows a stable "Ask anything..." composer placeholder while
        # idle. That text disappears once a prompt is in flight, so it is a
        # reliable one-shot ready marker for the first send.
        self._wait_banner_ready(session_name, "opencode", lambda capture: "Ask anything" in capture)

    def _wait_copilot_ready(self, session_name: str) -> None:
        # Copilot's footer text changes between states ("autopilot · /
        # commands" before any prompt, "/ commands · ? help" after), so there
        # is no single stable banner string to key on the way codex has.
        self._wait_stable_pane_ready(session_name, "copilot", time.monotonic() + 20.0)

    def send_prompt(self, session_name: str, prompt_path: Path) -> None:
        buffer_name = f"{session_name}_prompt"
        self.wait_until_prompt_ready(session_name)
        run_command(["tmux", "load-buffer", "-b", buffer_name, str(prompt_path)], error_prefix="tmux prompt load failed")
        run_command(["tmux", "paste-buffer", "-b", buffer_name, "-t", session_name], error_prefix="tmux prompt paste failed")
        run_command(["tmux", "delete-buffer", "-b", buffer_name], allow_failure=True)
        # Confirmed by reproduction: submitting immediately races the harness
        # TUI's own paste handling. A single C-m sent right after paste-buffer
        # can be consumed finalizing the pasted multi-line block instead of
        # submitting it, leaving the prompt sitting unsent (composer at "0
        # tok") until PM's timeout fires -- there is no approval prompt to
        # detect, just a message that was never actually sent. A second C-m
        # after the TUI settles reliably submits it. Both sends tolerate a
        # session that has already ended (a fast-finishing harness can exit
        # before either fires) -- that is a normal completion path the result
        # /activity checks below handle, not a send_prompt failure.
        time.sleep(1.0)
        run_command(["tmux", "send-keys", "-t", session_name, "C-m"], allow_failure=True)
        time.sleep(1.0)
        run_command(["tmux", "send-keys", "-t", session_name, "C-m"], allow_failure=True)

    def send_literal(self, session_name: str, text: str) -> None:
        if "\n" in text or "\r" in text:
            # Literal keystrokes into a TUI: the first newline would submit a
            # partial message (the same race send_prompt's double-Enter
            # discipline exists for). Multi-line content belongs in a file the
            # session is pointed at, the way repair prompts are delivered.
            raise PmError("send text must be a single line; write multi-line content to a file and send a one-line pointer")
        if not self.session_exists(session_name):
            raise PmError(f"tmux session is not running: {session_name}")
        capture = self._pane_text(session_name)
        hard_prompt = self.detect_hard_prompt(capture)
        if hard_prompt["present"]:
            raise PmError(
                "refusing to send into hard prompt on screen: "
                + ", ".join(str(kind) for kind in hard_prompt["kinds"])
            )
        # "--" ends tmux option parsing so a literal text that begins with "-"
        # cannot be misread as a send-keys flag.
        run_command(["tmux", "send-keys", "-t", session_name, "-l", "--", text], error_prefix="tmux literal send failed")
        # Keep the same settle-and-resubmit discipline as send_prompt. This
        # avoids the known single-Enter race in Codex/Claude TUIs while still
        # using literal tmux input rather than shell evaluation.
        time.sleep(1.0)
        run_command(["tmux", "send-keys", "-t", session_name, "C-m"], allow_failure=True)
        time.sleep(1.0)
        run_command(["tmux", "send-keys", "-t", session_name, "C-m"], allow_failure=True)

    def capture(self, session_name: str, destination: Path) -> None:
        if not shutil.which("tmux"):
            destination.write_text("tmux was unavailable during capture\n", encoding="utf-8")
            return
        result = run_command(["tmux", "capture-pane", "-p", "-S", "-32768", "-t", session_name], allow_failure=True)
        if result.returncode == 0:
            destination.write_text(result.stdout, encoding="utf-8")
        else:
            destination.write_text("tmux pane was unavailable during capture\n", encoding="utf-8")

    def session_exists(self, session_name: str) -> bool:
        if not shutil.which("tmux"):
            return False
        return run_command(["tmux", "has-session", "-t", session_name], allow_failure=True).returncode == 0

    def sessions_with_prefix(self, prefix: str) -> list[str]:
        if not shutil.which("tmux"):
            return []
        result = run_command(["tmux", "list-sessions", "-F", "#{session_name}"], allow_failure=True)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip().startswith(prefix)]

    def detect_activity(self, session_name: str, previous_capture: str) -> dict[str, Any]:
        if not self.session_exists(session_name):
            return {"running": False, "active": False, "capture": ""}
        result = run_command(["tmux", "capture-pane", "-p", "-S", "-32768", "-t", session_name], allow_failure=True)
        capture = result.stdout if result.returncode == 0 else ""
        return {"running": True, "active": capture != previous_capture, "capture": capture}

    def request_stop(self, session_name: str) -> None:
        if self.session_exists(session_name):
            run_command(["tmux", "send-keys", "-t", session_name, "C-c"], allow_failure=True)

    def force_stop(self, session_name: str) -> None:
        if self.session_exists(session_name):
            run_command(["tmux", "kill-session", "-t", session_name], allow_failure=True)

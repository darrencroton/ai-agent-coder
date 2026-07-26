"""Harness launch profiles: composed commands and model-inventory queries.

The recorded executables and flags are observed operational data; the code
composing them is independent.

There is exactly one composed path — this module's profile table — plus an
explicit ``--harness-command`` override at the CLI layer for fake harnesses
and unsupported setups. This module does not implement that override; it only
composes the profile-table path and fails closed for any harness name outside
the table.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import PmError

SUPPORTED_HARNESSES: tuple[str, ...] = ("codex", "claude", "copilot", "opencode", "qwen")

HARNESS_PROFILES: dict[str, dict[str, Any]] = {
    "codex": {
        "executable": "codex",
        "model_flag": "-m",
        "effort_config_key": "model_reasoning_effort",
    },
    "claude": {
        "executable": "claude",
        "model_flag": "--model",
        "effort_flag": "--effort",
    },
    "copilot": {
        "executable": "copilot",
        "model_flag": "--model",
        "effort_flag": "--effort",
    },
    "opencode": {
        "executable": "opencode",
        "model_flag": "-m",
        # `opencode run --variant` is documented by the CLI as "model variant
        # (provider-specific reasoning effort, e.g., high, max, minimal)", so it
        # is OpenCode's reasoning-effort control and `--effort` maps onto it.
        # Values are provider-specific and passed through verbatim, exactly as
        # for the other harnesses' effort flags.
        "effort_flag": "--variant",
        "model_inventory_command": ["opencode", "models", "{provider}", "--verbose"],
    },
    "qwen": {
        "executable": "qwen",
        "model_flag": "-m",
        "headless_model_flag": "--model",
        # Qwen Code exposes no reasoning-effort flag. An effort request
        # therefore fails closed through _append_headless_effort.
    },
}


def _unknown_harness_error(harness: str) -> PmError:
    supported = ", ".join(SUPPORTED_HARNESSES)
    return PmError(f"no PM harness profile is defined for {harness!r}; supported harnesses: {supported}")


def _append_headless_model(command: list[str], profile: dict[str, Any], model: str | None) -> None:
    if model:
        command.extend([profile.get("headless_model_flag", profile["model_flag"]), model])


def _append_headless_effort(command: list[str], profile: dict[str, Any], effort: str | None, harness: str) -> None:
    """Append the harness's reasoning-effort override, or fail closed.

    Qwen Code exposes no effort/reasoning flag on its tested headless command,
    so an effort request raises rather than being silently dropped or turned
    into a broken launch command. Every call site passes the real ``command``:
    a throwaway list would turn a table entry that *does* carry an effort flag
    into a silent no-op that still looks configured.
    """
    if not effort:
        return
    effort_flag = profile.get("effort_flag")
    effort_config_key = profile.get("effort_config_key")
    if effort_flag:
        command.extend([effort_flag, effort])
        return
    if effort_config_key:
        command.extend(["-c", f'{effort_config_key}="{effort}"'])
        return
    raise PmError(
        f"{harness}'s tested headless command has no effort/reasoning flag; "
        "omit --effort for this harness"
    )


def compose_headless_command(
    harness: str,
    pointer: str,
    *,
    mode: str,
    repo: Path,
    model: str | None = None,
    effort: str | None = None,
    session_id: str | None = None,
    git_access_dir: Path | None = None,
) -> list[str]:
    """Compose a one-shot headless launch for the Developer or Reviewer.

    This is the single launch composer for both seats.  The returned argv is
    passed directly to ``Popen`` so prompt, model, and path values never
    require a second round of shell parsing.

    ``session_id`` binds Claude and Copilot Developer launches to the session
    that a later headless resume must use.  Codex, OpenCode, and Qwen discover
    their launch-bound identifiers after completion, so the value is unused
    for those harnesses.  ``git_access_dir`` is the Codex-only git directory
    that must accompany launches from a linked worktree.
    """
    profile = HARNESS_PROFILES.get(harness)
    if profile is None:
        raise _unknown_harness_error(harness)
    if mode not in {"developer", "reviewer"}:
        raise PmError(f"headless mode must be 'developer' or 'reviewer', got {mode!r}")

    repo_str = str(repo)
    if mode == "reviewer":
        if harness == "codex":
            command = ["codex", "exec", pointer]
            _append_headless_model(command, profile, model)
            _append_headless_effort(command, profile, effort, harness)
            command.extend(["--sandbox", "read-only", "--skip-git-repo-check", "-C", repo_str])
            return command
        if harness == "claude":
            command = ["claude", "-p", pointer]
            _append_headless_model(command, profile, model)
            _append_headless_effort(command, profile, effort, harness)
            command.extend(["--permission-mode", "plan", "--output-format", "text", "--add-dir", repo_str])
            return command
        if harness == "copilot":
            command = ["copilot"]
            _append_headless_model(command, profile, model)
            _append_headless_effort(command, profile, effort, harness)
            command.extend(["-p", pointer, "--allow-all-tools", "--autopilot", "--silent", "--add-dir", repo_str])
            return command
        if harness == "opencode":
            command = ["opencode", "run", pointer]
            _append_headless_model(command, profile, model)
            _append_headless_effort(command, profile, effort, harness)
            command.extend(["--agent", "plan", "--auto", "--dir", repo_str])
            return command
        command = ["qwen", "--prompt", pointer]
        _append_headless_model(command, profile, model)
        _append_headless_effort(command, profile, effort, harness)
        command.extend(["--sandbox", "--output-format", "text"])
        return command

    if harness == "codex":
        command = ["codex", "exec", pointer]
        _append_headless_model(command, profile, model)
        _append_headless_effort(command, profile, effort, harness)
        command.extend(["--sandbox", "workspace-write", "--skip-git-repo-check", "-C", repo_str])
        if git_access_dir is not None:
            command.extend(["--add-dir", str(git_access_dir)])
        return command
    if harness == "claude":
        command = ["claude", "-p", pointer]
        _append_headless_model(command, profile, model)
        _append_headless_effort(command, profile, effort, harness)
        command.extend(["--permission-mode", "acceptEdits"])
        if session_id:
            command.extend(["--session-id", session_id])
        command.extend(["--add-dir", repo_str])
        return command
    if harness == "copilot":
        command = ["copilot", "-p", pointer]
        _append_headless_model(command, profile, model)
        _append_headless_effort(command, profile, effort, harness)
        command.extend(["--allow-all-tools", "--autopilot"])
        if session_id:
            command.extend(["--session-id", session_id])
        command.extend(["--add-dir", repo_str])
        return command
    if harness == "opencode":
        command = ["opencode", "run", pointer]
        _append_headless_model(command, profile, model)
        _append_headless_effort(command, profile, effort, harness)
        command.extend(["--agent", "build", "--auto", "--dir", repo_str])
        return command
    command = ["qwen", "--prompt", pointer]
    _append_headless_model(command, profile, model)
    _append_headless_effort(command, profile, effort, harness)
    command.extend(["--sandbox", "--output-format", "text"])
    return command


def compose_resume_command(
    harness: str,
    correction: str,
    *,
    session_id: str,
    repo: Path,
    git_access_dir: Path | None = None,
) -> list[str]:
    """Compose a Developer's next, resumptive headless turn.

    A caller must first quiesce the preceding process and supply the
    launch-bound ``session_id``.  Custom ``--harness-command`` overrides are
    intentionally handled by the lifecycle layer: their resume protocol
    re-runs the override with ``PM_DEVELOPER_RESUME_SESSION_ID`` set.
    """
    if harness not in HARNESS_PROFILES:
        raise _unknown_harness_error(harness)
    if not session_id:
        raise PmError("cannot compose a headless resume without a captured session id")

    repo_str = str(repo)
    if harness == "claude":
        return [
            "claude", "-p", correction, "--resume", session_id,
            "--permission-mode", "acceptEdits", "--add-dir", repo_str,
        ]
    if harness == "codex":
        command = [
            "codex", "exec", "resume", session_id, correction,
            "--sandbox", "workspace-write", "--skip-git-repo-check", "-C", repo_str,
        ]
        if git_access_dir is not None:
            command.extend(["--add-dir", str(git_access_dir)])
        return command
    if harness == "copilot":
        return [
            "copilot", "-p", correction, f"--resume={session_id}",
            "--allow-all-tools", "--autopilot", "--add-dir", repo_str,
        ]
    if harness == "opencode":
        return ["opencode", "run", correction, "--session", session_id, "--agent", "build", "--auto", "--dir", repo_str]
    return ["qwen", "--prompt", correction, "--resume", session_id, "--sandbox", "--output-format", "text"]


def query_model_identity(harness: str, model: str) -> dict[str, str] | None:
    """Resolve an exact model id through a harness-owned inventory when available.

    ``None`` means the profile has no queryable inventory contract (codex,
    claude, copilot, qwen). A configured inventory (opencode) is fail-closed: a
    failed query, a model id absent from the inventory, or unparseable/empty
    display-name metadata all raise ``PmError`` rather than letting the
    harness silently select a different model.
    """
    profile = HARNESS_PROFILES.get(harness)
    if profile is None:
        raise _unknown_harness_error(harness)
    command_template = profile.get("model_inventory_command")
    if not command_template:
        return None

    provider = model.split("/", 1)[0] if "/" in model else model
    command = [str(part).format(provider=provider) for part in command_template]
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or f"exit {result.returncode}"
        raise PmError(f"{harness} model inventory query failed: {detail}")

    lines = (result.stdout or "").splitlines()
    try:
        model_line = next(index for index, line in enumerate(lines) if line.strip() == model)
    except StopIteration as exc:
        raise PmError(
            f"requested {harness} model {model!r} is not present in the harness model inventory; "
            "use the exact configured model id"
        ) from exc

    remainder = "\n".join(lines[model_line + 1 :]).lstrip()
    try:
        metadata, _ = json.JSONDecoder().raw_decode(remainder)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PmError(f"could not parse {harness} model metadata for {model!r}") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"].strip():
        raise PmError(f"{harness} model metadata has no display name for {model!r}")

    return {
        "requested": model,
        "resolved_id": model,
        "display_name": metadata["name"].strip(),
        "inventory_command": shlex.join(command),
    }


def parse_reviewer_tools(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(tool.strip().lower() for tool in value.split(",") if tool.strip())

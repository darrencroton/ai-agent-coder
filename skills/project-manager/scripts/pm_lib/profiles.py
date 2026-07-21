"""Harness launch profiles: composed commands and model-inventory queries.

The recorded marker/readiness strings, base commands, and flags are observed
operational data; the code composing them is independent.

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
        "base_command": ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "never"],
        "model_flag": "-m",
        "effort_config_key": "model_reasoning_effort",
        "reviewer_network_flag": ["-c", "sandbox_workspace_write.network_access=true"],
        "commit_git_access_flag": "--add-dir",
    },
    "claude": {
        "base_command": ["claude", "--permission-mode", "auto"],
        "model_flag": "--model",
        "effort_flag": "--effort",
        "session_id_flag": "--session-id",
    },
    "copilot": {
        "base_command": ["copilot", "--allow-all-tools", "--autopilot"],
        "model_flag": "--model",
        "effort_flag": "--effort",
    },
    "opencode": {
        "base_command": ["opencode", "--auto"],
        "model_flag": "-m",
        # No effort_flag and no effort_config_key: the interactive TUI this
        # profile launches has no reasoning-effort flag, so an effort request
        # fails closed at compose time (see _append_effort below) instead of
        # launching a broken command.
        "model_inventory_command": ["opencode", "models", "{provider}", "--verbose"],
    },
    "qwen": {
        "base_command": ["qwen"],
        "model_flag": "-m",
        # Qwen Code's interactive command exposes no reasoning-effort flag.
        # An effort request therefore fails closed through _append_effort.
    },
}


def _unknown_harness_error(harness: str) -> PmError:
    supported = ", ".join(SUPPORTED_HARNESSES)
    return PmError(f"no PM harness profile is defined for {harness!r}; supported harnesses: {supported}")


def _append_model(command: list[str], profile: dict[str, Any], model: str | None) -> None:
    if not model:
        return
    command.extend([profile["model_flag"], model])


def _append_effort(command: list[str], profile: dict[str, Any], effort: str | None, harness: str) -> None:
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
        f"harness profile {harness!r} has no effort override for its interactive launch command; "
        "omit --effort for this harness"
    )


def compose_command(
    harness: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    reviewer_network: bool = False,
    git_access_dir: Path | None = None,
    session_id: str | None = None,
) -> str:
    """Compose one harness's launch command from the profile table.

    Only the codex profile applies ``reviewer_network`` and
    ``git_access_dir`` (its reviewer-network sandbox flag and commit
    git-directory access flag); only the claude profile applies
    ``session_id`` (its transcript-capture flag). Passing those keyword
    arguments for a different harness is silently a no-op rather than an
    error — Stage 3's caller composes per-slice, and not every harness has
    an equivalent flag.
    """
    profile = HARNESS_PROFILES.get(harness)
    if profile is None:
        raise _unknown_harness_error(harness)

    command = list(profile["base_command"])
    _append_model(command, profile, model)
    _append_effort(command, profile, effort, harness)

    if harness == "codex":
        if reviewer_network:
            command.extend(profile["reviewer_network_flag"])
        if git_access_dir is not None:
            command.extend([profile["commit_git_access_flag"], str(git_access_dir)])
    if harness == "claude" and session_id:
        command.extend([profile["session_id_flag"], session_id])

    return shlex.join(command)


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

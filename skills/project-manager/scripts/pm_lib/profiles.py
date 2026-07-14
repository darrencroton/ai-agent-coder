from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any

from .constants import HARNESS_PROFILES
from .git_ops import git_access_path
from .models import PmError
from .process import run_command


_RUN_LAUNCH_STRING_FIELDS = (
    "harness_command",
    "harness_model",
    "harness_effort",
    "reviewer_model",
    "reviewer_effort",
)
_RUN_LAUNCH_BOOLEAN_FIELDS = ("allow_profile_command", "allow_unattended_default")


def parse_reviewer_tools(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(tool.strip().lower() for tool in value.split(",") if tool.strip())


def _launch_config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        **{field: getattr(args, field, None) for field in _RUN_LAUNCH_STRING_FIELDS},
        "reviewer_tools": list(parse_reviewer_tools(getattr(args, "reviewer_tools", None))),
        **{field: bool(getattr(args, field, False)) for field in _RUN_LAUNCH_BOOLEAN_FIELDS},
    }


def _validated_run_launch_config(state: dict[str, Any]) -> dict[str, Any] | None:
    harness = state.get("harness") if isinstance(state.get("harness"), dict) else {}
    config = harness.get("launch_config")
    if config is None:
        return None
    if not isinstance(config, dict):
        raise PmError("run-level harness.launch_config is malformed")
    for field in _RUN_LAUNCH_STRING_FIELDS:
        value = config.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise PmError(f"run-level harness.launch_config.{field} must be null or a non-empty string")
    reviewer_tools = config.get("reviewer_tools")
    if not isinstance(reviewer_tools, list) or not all(isinstance(tool, str) and tool for tool in reviewer_tools):
        raise PmError("run-level harness.launch_config.reviewer_tools must be a list of non-empty strings")
    for field in _RUN_LAUNCH_BOOLEAN_FIELDS:
        if not isinstance(config.get(field), bool):
            raise PmError(f"run-level harness.launch_config.{field} must be a boolean")
    return config


def effective_run_launch_args(args: argparse.Namespace, state: dict[str, Any]) -> argparse.Namespace:
    """Apply the immutable run-level launch contract to one CLI invocation.

    Omitted flags inherit the first slice's configuration. Supplying a
    different value is a reconfiguration attempt and fails closed; model/tool
    changes require a new run so every slice has one auditable identity.
    """
    persisted = _validated_run_launch_config(state)
    if persisted is None:
        return args
    values = dict(vars(args))
    for field in _RUN_LAUNCH_STRING_FIELDS:
        supplied = values.get(field)
        expected = persisted.get(field)
        if supplied is not None and supplied != expected:
            raise PmError(
                f"{field.replace('_', '-')} differs from the frozen run launch configuration; initialize a new run to reconfigure models or commands"
            )
        values[field] = expected
    supplied_tools = parse_reviewer_tools(values.get("reviewer_tools"))
    expected_tools = tuple(str(tool) for tool in persisted.get("reviewer_tools", ()))
    if supplied_tools and supplied_tools != expected_tools:
        raise PmError(
            "reviewer-tools differs from the frozen run launch configuration; initialize a new run to reconfigure reviewers"
        )
    values["reviewer_tools"] = ",".join(expected_tools)
    for field in _RUN_LAUNCH_BOOLEAN_FIELDS:
        supplied = bool(values.get(field))
        expected = bool(persisted.get(field))
        if supplied and not expected:
            raise PmError(
                f"{field.replace('_', '-')} differs from the frozen run launch configuration; initialize a new run to reconfigure launch policy"
            )
        values[field] = expected
    return argparse.Namespace(**values)


def freeze_run_launch_config(args: argparse.Namespace, state: dict[str, Any]) -> argparse.Namespace:
    """Freeze the first slice's complete launch configuration for the run."""
    persisted = _validated_run_launch_config(state)
    if persisted is None:
        state.setdefault("harness", {})["launch_config"] = _launch_config_from_args(args)
        return args
    return effective_run_launch_args(args, state)


def query_profile_model_identity(harness_name: str, model: str) -> dict[str, str] | None:
    """Resolve an exact model id through a harness-owned inventory when available.

    ``None`` means the profile has no queryable inventory contract. A configured
    inventory is fail-closed: a failed query, typo, alias, or unparseable identity
    is rejected before the harness can silently select a different model.
    """
    profile = HARNESS_PROFILES.get(harness_name) or {}
    command_template = profile.get("model_inventory_command")
    if not command_template:
        return None
    provider = model.split("/", 1)[0] if "/" in model else model
    command = [str(part).format(provider=provider) for part in command_template]
    result = run_command(command, allow_failure=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise PmError(f"{harness_name} model inventory query failed: {detail}")

    lines = result.stdout.splitlines()
    try:
        model_line = next(index for index, line in enumerate(lines) if line.strip() == model)
    except StopIteration as exc:
        raise PmError(
            f"requested {harness_name} model {model!r} is not present in the harness model inventory; "
            "use the exact configured model id"
        ) from exc

    display_name = model
    if profile.get("model_inventory_verbose_json"):
        remainder = "\n".join(lines[model_line + 1 :]).lstrip()
        try:
            metadata, _ = json.JSONDecoder().raw_decode(remainder)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PmError(f"could not parse {harness_name} model metadata for {model!r}") from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"].strip():
            raise PmError(f"{harness_name} model metadata has no display name for {model!r}")
        display_name = metadata["name"].strip()
    return {
        "requested": model,
        "resolved_id": model,
        "display_name": display_name,
        "inventory_command": shlex.join(command),
    }


def _append_model_override(command: list[str], profile: dict[str, Any], model: str | None, tool_name: str) -> None:
    if not model:
        return
    model_flag = profile.get("model_flag")
    model_config_key = profile.get("model_config_key")
    if model_flag:
        command.extend([str(model_flag), model])
        return
    if model_config_key:
        command.extend(["-c", f'{model_config_key}="{model}"'])
        return
    raise PmError(f"harness profile {tool_name!r} does not support PM-composed model overrides")


def _append_effort_override(command: list[str], profile: dict[str, Any], effort: str | None, tool_name: str) -> None:
    if not effort:
        return
    effort_flag = profile.get("effort_flag")
    effort_config_key = profile.get("effort_config_key")
    if effort_flag:
        command.extend([str(effort_flag), effort])
        return
    if effort_config_key:
        command.extend(["-c", f'{effort_config_key}="{effort}"'])
        return
    raise PmError(f"harness profile {tool_name!r} does not support PM-composed effort overrides")


def profile_command(
    harness_name: str,
    repo: Path,
    state: dict[str, Any],
    reviewer_tools: tuple[str, ...],
    developer_session_id: str | None = None,
    harness_model: str | None = None,
    harness_effort: str | None = None,
) -> str:
    profile = HARNESS_PROFILES.get(harness_name)
    if not profile:
        raise PmError(f"no PM harness profile is defined for {harness_name!r}")
    command = list(profile.get("base_command") or [])
    if not command:
        raise PmError(f"harness profile {harness_name!r} has no base command")

    _append_model_override(command, profile, harness_model, harness_name)
    _append_effort_override(command, profile, harness_effort, harness_name)

    if harness_name == "codex":
        if reviewer_tools:
            command.extend(profile["reviewer_network_flag"])
        if state.get("policy", {}).get("commit_required", True):
            command.extend([profile["commit_git_access_flag"], str(git_access_path(repo))])
    if harness_name == "claude" and developer_session_id:
        # Pins the session transcript to a deterministic path under
        # ~/.claude/projects/<repo-slug>/<session_id>.jsonl so PM can capture
        # it as a full-fidelity artifact after the run (see
        # capture_developer_transcript). Claude Code's interactive TUI
        # collapses verbose tool output behind "ctrl+o to expand" in the tmux
        # pane capture; this transcript is not subject to that collapsing.
        command.extend(["--session-id", developer_session_id])
    return shlex.join(command)


def resolve_harness_command(
    args: argparse.Namespace,
    repo: Path,
    state: dict[str, Any],
    developer_session_id: str | None = None,
) -> str | None:
    if getattr(args, "harness_model", None) and not getattr(args, "allow_profile_command", False):
        raise PmError("--harness-model is only supported with --allow-profile-command")
    if getattr(args, "harness_effort", None) and not getattr(args, "allow_profile_command", False):
        raise PmError("--harness-effort is only supported with --allow-profile-command")
    if getattr(args, "harness_command", None):
        return args.harness_command
    if getattr(args, "allow_profile_command", False):
        return profile_command(
            state["harness"]["name"],
            repo,
            state,
            parse_reviewer_tools(getattr(args, "reviewer_tools", None)),
            developer_session_id,
            getattr(args, "harness_model", None),
            getattr(args, "harness_effort", None),
        )
    return None


def effective_launch_args(args: argparse.Namespace, state: dict[str, Any]) -> argparse.Namespace:
    """Fill omitted per-invocation launch flags from the active slice snapshot."""
    values = dict(vars(effective_run_launch_args(args, state)))
    current = state.get("current_slice") if isinstance(state.get("current_slice"), dict) else None
    persisted = current.get("launch_config") if current and isinstance(current.get("launch_config"), dict) else {}
    for field in _RUN_LAUNCH_STRING_FIELDS:
        if not values.get(field) and persisted.get(field):
            values[field] = persisted[field]
    if not parse_reviewer_tools(values.get("reviewer_tools")) and persisted.get("reviewer_tools"):
        values["reviewer_tools"] = ",".join(str(tool) for tool in persisted["reviewer_tools"])
    for field in ("allow_profile_command", "allow_unattended_default"):
        if not values.get(field) and persisted.get(field):
            values[field] = True
    return argparse.Namespace(**values)


def resolve_current_harness_command(
    args: argparse.Namespace,
    repo: Path,
    state: dict[str, Any],
    developer_session_id: str | None = None,
) -> str | None:
    return resolve_harness_command(effective_launch_args(args, state), repo, state, developer_session_id)


def current_allow_unattended_default(args: argparse.Namespace, state: dict[str, Any]) -> bool:
    return bool(getattr(effective_launch_args(args, state), "allow_unattended_default", False))

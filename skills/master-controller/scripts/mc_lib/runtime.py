from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .constants import REQUIRED_AUDIT_SKILLS, SENSITIVE_ARTIFACT_NAMES, WORKER_CREDENTIAL_HOMES
from .git_ops import meaningful_status_lines, normalize_authorized_entry, unauthorized_files
from .models import GateDecision, McError, PlanSlice


_WORKER_JOBS_MODULE: Any = None


def environment_preflight() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": sys.executable,
        "python_version": platform.python_version(),
        "git": shutil.which("git"),
        "tmux": shutil.which("tmux"),
    }


def skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def result_schema_path() -> Path:
    return skill_root() / "references" / "run-state-schema.md"


def worker_jobs_path() -> Path:
    return skill_root().parent / "ai-orchestrator" / "scripts" / "worker_jobs.py"


def _excerpt(text: str, start: int, end: int, context: int = 120) -> str:
    lower = max(0, start - context)
    upper = min(len(text), end + context)
    return re.sub(r"\s+", " ", text[lower:upper]).strip()


def _parse_duration_seconds(text: str) -> int | None:
    lowered = text.lower()
    total = 0
    matched = False
    for pattern, multiplier in (
        (r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", 3600),
        (r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b", 60),
        (r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", 1),
    ):
        for match in re.finditer(pattern, lowered):
            total += int(float(match.group(1)) * multiplier)
            matched = True
    if matched:
        return max(1, total)
    return None


def _parse_absolute_reset_at(text: str, now: datetime, max_single_pause_seconds: int) -> tuple[datetime | None, bool]:
    local_now = now if now.tzinfo is not None else now.astimezone()
    timezone_match = re.search(
        r"\b(?:reset|resets|resetting|try again|available again|resume)\b[^.\n]{0,80}?\b(?:at|after)\s+"
        r"(?P<stamp>\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s*(?:UTC|GMT|[A-Z]{2,5}|[+-]\d{2}:?\d{2}))?)",
        text,
        flags=re.IGNORECASE,
    )
    if not timezone_match:
        return None, False
    stamp = timezone_match.group("stamp").strip()
    zone_match = re.search(r"\s*(?P<zone>UTC|GMT|[A-Z]{2,5}|[+-]\d{2}:?\d{2})$", stamp)
    zone_tz = local_now.tzinfo
    if zone_match and zone_match.group("zone") in {"AM", "PM"}:
        zone_match = None
    has_zone = zone_match is not None
    if zone_match:
        zone_token = zone_match.group("zone")
        if zone_token in {"UTC", "GMT"}:
            zone_tz = timezone.utc
        elif re.match(r"[+-]\d{2}:?\d{2}$", zone_token):
            sign = 1 if zone_token[0] == "+" else -1
            digits = zone_token[1:].replace(":", "")
            zone_tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        else:
            return None, True
    reset_now = local_now.astimezone(zone_tz)
    clock = re.match(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?", stamp, flags=re.IGNORECASE)
    if not clock:
        return None, True
    hour = int(clock.group("hour"))
    minute = int(clock.group("minute") or "0")
    ampm = (clock.group("ampm") or "").lower()
    if ampm:
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
    if hour > 23 or minute > 59:
        return None, True
    candidate = reset_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= reset_now:
        candidate += timedelta(days=1)
    wait_seconds = int((candidate - reset_now).total_seconds())
    if has_zone or 0 < wait_seconds <= max_single_pause_seconds:
        return candidate, False
    return None, True


def _reset_fields(text: str, now: datetime, max_single_pause_seconds: int) -> tuple[str | None, int | None, bool]:
    duration_scope = ""
    duration_intro = re.search(
        r"\b(?:in|after|within)\s+(?P<duration>[^.\n]{0,100})",
        text,
        flags=re.IGNORECASE,
    )
    if duration_intro:
        duration_scope = duration_intro.group("duration")
    retry_after = _parse_duration_seconds(duration_scope) if duration_scope else None
    if retry_after is not None:
        reset_at = now + timedelta(seconds=retry_after)
        return reset_at.replace(microsecond=0).isoformat(), retry_after, False
    absolute, ambiguous = _parse_absolute_reset_at(text, now, max_single_pause_seconds)
    if absolute is not None:
        return absolute.replace(microsecond=0).isoformat(), int((absolute - now.astimezone(absolute.tzinfo)).total_seconds()), False
    return None, None, ambiguous


def _hint(
    *,
    kind: str,
    subtype: str | None,
    confidence: str,
    hard_stop: bool,
    source: str,
    evidence_excerpt: str,
    now: datetime,
    reset_at: str | None = None,
    retry_after_seconds: int | None = None,
    recovery_guidance: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "confidence": confidence,
        "subtype": subtype,
        "reset_at": reset_at,
        "retry_after_seconds": retry_after_seconds,
        "hard_stop": hard_stop,
        "evidence_excerpt": evidence_excerpt,
        "source": source,
        "detected_at": now.replace(microsecond=0).isoformat(),
        "recovery_guidance": recovery_guidance,
    }


def extract_operational_hints(
    pane_text: str,
    *,
    transcript_text: str = "",
    process_running: bool = False,
    process_active: bool = False,
    result_exists: bool = False,
    now: datetime | None = None,
    max_single_pause_seconds: int = 21600,
) -> list[dict[str, Any]]:
    """Return lightweight operational hints from live harness evidence.

    These hints are intentionally advisory except for hard-stop categories. They
    give the supervising MC model compact evidence without turning Python into a
    broad natural-language decision engine.
    """
    observed_at = now if now is not None and now.tzinfo is not None else (now or datetime.now()).astimezone()
    hints: list[dict[str, Any]] = []
    sources = (("tmux-pane", pane_text or ""), ("transcript", transcript_text or ""))
    for source, text in sources:
        lowered = text.lower()
        if not lowered:
            continue
        usage_percent_match = re.search(
            r"\b(?:you(?:'ve| have)\s+used|used)\s+(\d{1,3})%\b[^.\n]{0,120}\b(?:hourly|daily|weekly|monthly|5[- ]?hour|five[- ]?hour)?\s*(?:usage\s*)?(?:limit|quota|cap)\b",
            lowered,
        )
        informational_usage_warning = bool(usage_percent_match and int(usage_percent_match.group(1)) < 100)
        conditional_limit_warning = "if you hit your limit" in lowered
        if informational_usage_warning or conditional_limit_warning:
            warning_match = usage_percent_match or re.search(r"\bif you hit your limit\b", lowered)
            if warning_match:
                hints.append(
                    _hint(
                        kind="usage_limit",
                        subtype="warning",
                        confidence="high" if usage_percent_match else "medium",
                        hard_stop=False,
                        source=source,
                        evidence_excerpt=_excerpt(text, warning_match.start(), warning_match.end()),
                        now=observed_at,
                        recovery_guidance="continue-with-observation",
                    )
                )

        for subtype, pattern in (
            ("weekly_window", r"\bweekly\b[^.\n]{0,80}\b(?:limit|quota|cap)\b|\b(?:limit|quota|cap)\b[^.\n]{0,80}\bweekly\b"),
            ("monthly_window", r"\bmonthly\b[^.\n]{0,80}\b(?:limit|quota|cap)\b|\b(?:limit|quota|cap)\b[^.\n]{0,80}\bmonthly\b"),
            (
                "account_or_billing",
                r"\b(?:account|billing|subscription|plan|credit|credits)\b[^.\n]{0,100}\b(?:limit|quota|cap|exhausted|upgrade|billing)\b",
            ),
        ):
            if informational_usage_warning or conditional_limit_warning:
                continue
            match = re.search(pattern, lowered)
            if match:
                hints.append(
                    _hint(
                        kind="usage_limit",
                        subtype=subtype,
                        confidence="high",
                        hard_stop=True,
                        source=source,
                        evidence_excerpt=_excerpt(text, match.start(), match.end()),
                        now=observed_at,
                        recovery_guidance="stop-for-user",
                    )
                )

        rolling_match = re.search(
            r"\b(?:5[- ]?hour|five[- ]?hour|rolling|session|usage)\b[^.\n]{0,140}\b(?:limit|quota|cap|reset|try again)\b|"
            r"\b(?:limit|quota|cap)\b[^.\n]{0,140}\b(?:reset|try again|in \d+|after \d+)\b",
            lowered,
        )
        if (
            rolling_match
            and not informational_usage_warning
            and not conditional_limit_warning
            and not any(h["kind"] == "usage_limit" and h["source"] == source and h["hard_stop"] for h in hints)
        ):
            # Scope reset parsing to a window around the matched limit text.
            # Scanning the whole pane let an unrelated duration phrase
            # elsewhere on screen ("completed in 5 minutes") masquerade as the
            # reset time; the window still covers the adjacent sentence
            # ("Usage limit reached. Try again in 3 hours.").
            reset_window = text[max(0, rolling_match.start() - 200): rolling_match.end() + 300]
            reset_at, retry_after, ambiguous = _reset_fields(reset_window, observed_at, max_single_pause_seconds)
            hard_stop = reset_at is None and retry_after is None
            subtype = "unknown_limit" if hard_stop or ambiguous else "rolling_window"
            if process_running and not result_exists and not hard_stop:
                guidance = "pause-until-reset-plus-buffer-then-send-continuation"
            elif result_exists:
                guidance = "finalize-slice"
            elif not process_running and not hard_stop:
                guidance = "restart-from-clean-authorized-state-or-stop-for-user"
            else:
                guidance = "stop-for-user"
            hints.append(
                _hint(
                    kind="usage_limit",
                    subtype=subtype,
                    confidence="high" if not hard_stop else "medium",
                    hard_stop=hard_stop,
                    source=source,
                    evidence_excerpt=_excerpt(text, rolling_match.start(), rolling_match.end()),
                    now=observed_at,
                    reset_at=reset_at,
                    retry_after_seconds=retry_after,
                    recovery_guidance=guidance,
                )
            )

        unknown_limit = re.search(r"\b(?:usage|session|rate|quota|limit|cap)\b[^.\n]{0,80}\b(?:reached|exceeded|exhausted)\b", lowered)
        if unknown_limit and not any(h["kind"] == "usage_limit" and h["source"] == source for h in hints):
            hints.append(
                _hint(
                    kind="usage_limit",
                    subtype="unknown_limit",
                    confidence="medium",
                    hard_stop=True,
                    source=source,
                    evidence_excerpt=_excerpt(text, unknown_limit.start(), unknown_limit.end()),
                    now=observed_at,
                    recovery_guidance="stop-for-user",
                )
            )

        explicit_service_match = re.search(r"\b(?:service unavailable|temporarily unavailable)\b", lowered)
        service_match = explicit_service_match or re.search(r"\b(?:try again later|overloaded|server error)\b", lowered)
        if service_match:
            retry_after = _parse_duration_seconds(text)
            hints.append(
                _hint(
                    kind="service_unavailable",
                    subtype="transient",
                    confidence="high" if explicit_service_match else "medium",
                    hard_stop=False,
                    source=source,
                    evidence_excerpt=_excerpt(text, service_match.start(), service_match.end()),
                    now=observed_at,
                    retry_after_seconds=retry_after,
                    recovery_guidance="bounded-retry",
                )
            )

        network_match = re.search(
            r"\b(?:network error|connection reset|econnreset|connection timed out|request timed out|network timeout|connection refused)\b",
            lowered,
        )
        if network_match:
            hints.append(
                _hint(
                    kind="network_transient",
                    subtype="transient",
                    confidence="medium",
                    hard_stop=False,
                    source=source,
                    evidence_excerpt=_excerpt(text, network_match.start(), network_match.end()),
                    now=observed_at,
                    recovery_guidance="bounded-retry",
                )
            )

        external_side_effect_pattern = (
            r"\b(?:do you want to|approve|confirm|allow|permission to|shall i|should i|ready to)\b"
            r"[^.\n?]{0,120}\b(?:push(?: to remote)?|create (?:a )?(?:pull request|pr)|open (?:a )?(?:pull request|pr)|"
            r"deploy|release|publish|install (?:a )?dependenc(?:y|ies)|change (?:the )?license|license change)\b"
            r"|"
            r"\b(?:push to remote|create (?:a )?(?:pull request|pr)|open (?:a )?(?:pull request|pr)|deploy|release|publish|"
            r"install (?:a )?dependenc(?:y|ies)|license change)\b[^.\n]{0,60}(?:\?|yes/no|\[y/n\]|approve|confirm)"
        )
        for kind, pattern in (
            ("auth_required", r"\b(?:login required|please log in|sign in|enter api key|enter password|mfa|two-factor)\b"),
            ("trust_prompt", r"\b(?:do you trust the (?:contents|files)|trust this (?:directory|folder|repo))\b"),
            ("permission_prompt", r"\b(?:permission denied|grant permission|requires permission|allow access)\b"),
            ("external_side_effect_request", external_side_effect_pattern),
        ):
            match = re.search(pattern, lowered)
            if match:
                hints.append(
                    _hint(
                        kind=kind,
                        subtype=None,
                        confidence="high",
                        hard_stop=True,
                        source=source,
                        evidence_excerpt=_excerpt(text, match.start(), match.end()),
                        now=observed_at,
                        recovery_guidance="stop-for-user",
                    )
                )

    if result_exists:
        hints.append(
            _hint(
                kind="result_ready",
                subtype=None,
                confidence="high",
                hard_stop=False,
                source="artifact",
                evidence_excerpt="orchestrator-result.json exists",
                now=observed_at,
                recovery_guidance="finalize-slice",
            )
        )
    elif not process_running:
        hints.append(
            _hint(
                kind="process_exited_without_result",
                subtype=None,
                confidence="high",
                hard_stop=True,
                source="process",
                evidence_excerpt="harness process is not running and orchestrator-result.json is absent",
                now=observed_at,
                recovery_guidance="stop-for-user-or-restart-only-from-clean-authorized-state",
            )
        )
    elif not process_active:
        hints.append(
            _hint(
                kind="idle_no_progress",
                subtype=None,
                confidence="low",
                hard_stop=False,
                source="process",
                evidence_excerpt="harness process is running but pane text did not change",
                now=observed_at,
                recovery_guidance="observe-again-before-deciding",
            )
        )
    return hints


def worker_jobs_module() -> Any:
    """Load ai-orchestrator's worker_jobs.py as a library module.

    Reused (not reimplemented) here for its session-path conventions, e.g.
    claude_project_root, which already correctly match how Claude Code and
    Codex lay out their on-disk session transcripts for worker sessions.
    """
    global _WORKER_JOBS_MODULE
    if _WORKER_JOBS_MODULE is None:
        path = worker_jobs_path()
        spec = importlib.util.spec_from_file_location("mc_worker_jobs", path)
        if spec is None or spec.loader is None:
            raise McError(f"could not load worker_jobs module from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _WORKER_JOBS_MODULE = module
    return _WORKER_JOBS_MODULE


def claude_orchestrator_transcript_path(repo: Path, session_id: str) -> Path:
    return worker_jobs_module().claude_project_root(repo.resolve()) / f"{session_id}.jsonl"


def capture_orchestrator_transcript(harness_name: str, repo: Path, session_id: str | None, slice_artifact_dir: Path) -> None:
    """Copy the orchestrator's own structured session transcript into the slice artifacts.

    This is a full-fidelity complement to pane-capture.txt, not a replacement:
    the tmux pane capture is still required to detect harness-level stuck/
    blocked states (e.g. approval or trust prompts) and to support further
    prompting mid-session; the JSONL transcript exists because Claude Code's
    interactive TUI collapses verbose tool output behind "ctrl+o to expand"
    in the pane, so exact commands/output are not always reconstructable from
    pane-capture.txt alone.
    """
    if harness_name != "claude" or not session_id:
        return
    destination = slice_artifact_dir / "orchestrator-transcript.jsonl"
    note_path = slice_artifact_dir / "orchestrator-transcript-note.txt"
    try:
        source = claude_orchestrator_transcript_path(repo, session_id)
    except McError as exc:
        note_path.write_text(f"orchestrator transcript lookup failed: {exc}\n", encoding="utf-8")
        return
    if source.exists():
        shutil.copy2(source, destination)
        note_path.unlink(missing_ok=True)
    else:
        note_path.write_text(
            "orchestrator transcript not found at expected path: "
            f"{source}\n"
            "This can happen if the launched command did not honor --session-id "
            "(e.g. a custom --harness-command without --session-id).\n",
            encoding="utf-8",
        )


def real_tool_home(env_var: str, default_dirname: str) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    return Path.home() / default_dirname


def worker_credential_source(tool: str) -> tuple[Path, str] | None:
    entry = WORKER_CREDENTIAL_HOMES.get(tool)
    if not entry:
        return None
    env_var, default_dirname, filename = entry
    return real_tool_home(env_var, default_dirname), filename


def slice_paths(slice_artifact_dir: Path) -> dict[str, Path]:
    return {
        "artifact_dir": slice_artifact_dir,
        "worker_artifact_root": slice_artifact_dir / "worker-runs",
        "tmp_dir": slice_artifact_dir / "tmp",
        "tool_home_root": slice_artifact_dir / "tool-homes",
        "copilot_home": slice_artifact_dir / "copilot-home",
        "codex_home": slice_artifact_dir / "codex-home",
        "claude_config_dir": slice_artifact_dir / "claude-config-dir",
    }


def seed_worker_credentials(paths: dict[str, Path], worker_tools: tuple[str, ...], orchestrator_harness_name: str) -> list[str]:
    warnings: list[str] = []
    home_by_tool = {"codex": "codex_home"}
    for tool, home_key in home_by_tool.items():
        if tool not in worker_tools or tool == orchestrator_harness_name:
            continue
        source = worker_credential_source(tool)
        if source is None:
            continue
        source_dir, filename = source
        source_path = source_dir / filename
        destination = paths[home_key] / filename
        if not source_path.exists():
            warnings.append(f"{tool} worker credential source not found: {source_path}")
            continue
        shutil.copy2(source_path, destination)
        os.chmod(destination, 0o600)
    return warnings


def ensure_slice_runtime_dirs(slice_artifact_dir: Path, worker_tools: tuple[str, ...] = (), orchestrator_harness_name: str = "") -> list[str]:
    paths = slice_paths(slice_artifact_dir)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return seed_worker_credentials(paths, worker_tools, orchestrator_harness_name)


def write_worker_policy(
    state: dict[str, Any],
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    worker_tools: tuple[str, ...],
    worker_model: str | None,
    worker_effort: str | None,
) -> Path:
    """Write the authoritative semantic boundary consumed by ai-orchestrator."""
    policy_path = slice_artifact_dir / "worker-policy.json"
    worker_requirement = plan_slice.sections.get("Validation Plan", "").lower()
    allowed_access = ["read-only"] if "worker" in worker_requirement and "read-only" in worker_requirement else ["read-only", "workspace-write"]
    policy = {
        "schema_version": 1,
        "run_id": str(state["run_id"]),
        "slice_id": plan_slice.slice_id,
        "plan_sha256": str(state.get("plan", {}).get("sha256") or ""),
        "repo_path": str(Path(state["repo_path"]).resolve()),
        "worker_artifact_root": str(slice_paths(slice_artifact_dir)["worker_artifact_root"]),
        "required_tools": list(worker_tools),
        "required_model": worker_model or "default",
        "required_effort": worker_effort or "default",
        "allowed_access": allowed_access,
        "allowed_roles": ["junior-worker", "senior-worker"],
        "authorized_files": [normalize_authorized_entry(entry) for entry in plan_slice.authorized_files],
        # Pre-launch companion to gates.py's finalize-time exact-match check:
        # any request whose required_skills touches drift-audit or code-review
        # must equal exactly one of these sets, not a mix or an empty list. An
        # empty required_skills stays valid (a legitimate ad hoc worker task
        # unrelated to either audit), so this cannot catch every misdraft —
        # only a request that already names a reserved skill incorrectly.
        "reserved_skill_sets": [[skill] for skill in REQUIRED_AUDIT_SKILLS] if plan_slice.independent_audit_required else [],
    }
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return policy_path


def worker_policy_snapshot(policy_path: Path) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    return {"sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(), "policy": policy}


def ai_orchestrator_embedded_instructions() -> str:
    # A slice needs the MC-specific semantic delegation contract, not the full
    # general-purpose ai-orchestrator bundle and every linked harness reference.
    # The validated launcher owns harness flags, while this compact reference is
    # the single source for the instructions a skill-less slice harness needs.
    path = skill_root().parent / "ai-orchestrator" / "references" / "mc-slice-contract.md"
    return path.read_text(encoding="utf-8").rstrip()


def slice_environment(
    slice_artifact_dir: Path,
    run_json: Path,
    plan_path: Path,
    plan_slice: PlanSlice,
    orchestrator_harness_name: str = "",
    worker_tools: tuple[str, ...] = (),
) -> dict[str, str]:
    paths = slice_paths(slice_artifact_dir)
    env = {
        "AI_ORCHESTRATOR_ARTIFACT_ROOT": str(paths["worker_artifact_root"]),
        "MC_RESULT_SCHEMA_PATH": str(result_schema_path()),
        "MC_RUN_JSON_PATH": str(run_json),
        "MC_PLAN_PATH": str(plan_path),
        "MC_SLICE_ARTIFACT_DIR": str(slice_artifact_dir),
        "MC_SLICE_ID": plan_slice.slice_id,
        "MC_SLICE_TMP_DIR": str(paths["tmp_dir"]),
        "MC_TOOL_HOME_ROOT": str(paths["tool_home_root"]),
        "MC_WORKER_ARTIFACT_ROOT": str(paths["worker_artifact_root"]),
        "MC_WORKER_JOBS_PATH": str(worker_jobs_path()),
        "MC_WORKER_POLICY_PATH": str(slice_artifact_dir / "worker-policy.json"),
        "TMPDIR": str(paths["tmp_dir"]),
    }
    # Only redirect a tool's own home when that tool is a *worker* for this
    # run, and never when it is also the orchestrator harness itself — a
    # Copilot or Codex orchestrator must keep its real config/session state.
    # Codex worker auth is currently portable via auth.json. Copilot only needs
    # a writable isolated dir (its GitHub credential lives outside ~/.copilot).
    # Claude Code subscription OAuth is not portable by copying
    # .credentials.json into CLAUDE_CONFIG_DIR, so MC deliberately leaves
    # Claude workers on the operator's normal config unless the caller supplied
    # standard Claude auth environment variables.
    if "copilot" in worker_tools and orchestrator_harness_name != "copilot":
        env["COPILOT_HOME"] = str(paths["copilot_home"])
    if "codex" in worker_tools and orchestrator_harness_name != "codex":
        env["CODEX_HOME"] = str(paths["codex_home"])
    return env


def worker_auth_policy_text(worker_tools: tuple[str, ...]) -> str:
    if not worker_tools:
        return "No worker tool is configured for this run."
    policies: list[str] = []
    if "copilot" in worker_tools:
        policies.append(
            "Copilot gets an isolated per-slice COPILOT_HOME for writable session state when Copilot is a worker "
            "and not the orchestrator."
        )
    if "codex" in worker_tools:
        policies.append("Codex gets an isolated per-slice CODEX_HOME seeded with auth.json when Codex is a worker and not the orchestrator.")
    if "claude" in worker_tools:
        policies.append(
            "Claude workers use the operator's normal Claude Code auth/config; MC does not set CLAUDE_CONFIG_DIR because "
            "copying .credentials.json into an isolated config dir is not a valid portable login. For non-interactive "
            "isolated auth, provide ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN in the environment."
        )
    unknown = [tool for tool in worker_tools if tool not in {"copilot", "codex", "claude"}]
    for tool in unknown:
        policies.append(f"{tool} uses its configured profile; no credential isolation policy is defined by MC.")
    return " ".join(policies)


def load_prompt_template() -> str:
    # The extracted template is rendered with str.format in
    # render_orchestrator_prompt, so any literal `{`/`}` added to the template
    # block in references/orchestrator-prompt.md (a JSON example, a shell
    # `${var}`) would raise at runtime. Keep placeholders as the only braces in
    # that block, or escape literals as `{{`/`}}`. The template file carries the
    # same warning for editors.
    path = skill_root() / "references" / "orchestrator-prompt.md"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```md\n(?P<template>.*?)\n```", text, flags=re.DOTALL)
    if not match:
        raise McError(f"orchestrator prompt template not found in {path}")
    return match.group("template")


def render_orchestrator_prompt(
    state: dict[str, Any],
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    run_json: Path,
    worker_tools: tuple[str, ...] = (),
    worker_model: str | None = None,
    worker_effort: str | None = None,
) -> str:
    template = load_prompt_template()
    paths = slice_paths(slice_artifact_dir)
    example_tool = worker_tools[0] if len(worker_tools) == 1 else "<one required tool; create one request per tool>"
    example_role = "senior-worker" if example_tool in {"claude", "codex"} else "junior-worker"
    request_example = {
        "schema_version": 1,
        "label": "01-<tool>-<subtask>",
        "slice_id": plan_slice.slice_id,
        "plan_sha256": str(state.get("plan", {}).get("sha256") or ""),
        "tool": example_tool,
        "model": worker_model or "default",
        "effort": worker_effort or "default",
        "role": example_role,
        "access": "read-only",
        "task": "<bounded worker task>",
        "context": "<task-specific context>",
        "required_skills": [],
        "files": [normalize_authorized_entry(entry) for entry in plan_slice.authorized_files],
        "constraints": ["<task-specific constraint>"],
        "expected_output": "<exact output contract>",
    }
    values = {
        "plan_path": state["plan_path"],
        "run_json_path": str(run_json),
        "slice_artifact_dir": str(slice_artifact_dir),
        "result_schema_path": str(result_schema_path()),
        "worker_jobs_path": str(worker_jobs_path()),
        "worker_artifact_root": str(paths["worker_artifact_root"]),
        "slice_tmp_dir": str(paths["tmp_dir"]),
        "tool_home_root": str(paths["tool_home_root"]),
        "copilot_home": str(paths["copilot_home"]),
        "codex_home": str(paths["codex_home"]),
        "claude_config_dir": str(paths["claude_config_dir"]),
        "worker_auth_policy": worker_auth_policy_text(worker_tools),
        "worker_policy_path": str(slice_artifact_dir / "worker-policy.json"),
        "worker_request_example": json.dumps(request_example, indent=2, sort_keys=True),
        "audit_skill_reminder": (
            "This slice is opt-in (`Independent audit required: yes`): the drift-audit and code-review requests "
            "must each set `required_skills` to exactly `[\"drift-audit\"]` or exactly `[\"code-review\"]` — never "
            "`[]` and never both skills in one request. A mismatched or mixed value is rejected before launch."
            if plan_slice.independent_audit_required
            else ""
        ),
        "ai_orchestrator_embedded_instructions": ai_orchestrator_embedded_instructions(),
        "worker_tools": ", ".join(worker_tools) if worker_tools else "none available for this run",
        "worker_model": worker_model or "default",
        "worker_effort": worker_effort or "default",
        "slice_id": plan_slice.slice_id,
        "slice_title": plan_slice.title,
        "intended_change": plan_slice.sections.get("Intended Change", ""),
        "acceptance_criteria": plan_slice.sections.get("Acceptance Criteria", ""),
        "authorized_surface": plan_slice.sections.get("Authorized Surface", ""),
        "explicit_non_goals": plan_slice.sections.get("Explicit Non-Goals", ""),
        "risk_flags": plan_slice.sections.get("Risk Flags", ""),
        "validation_plan": plan_slice.sections.get("Validation Plan", ""),
        "rollback_path": plan_slice.sections.get("Rollback Path", ""),
    }
    return template.format(**values).rstrip() + "\n"


def load_repair_template() -> str:
    # Same str.format constraint as load_prompt_template: only the documented
    # placeholders may appear as braces in the repair block.
    path = skill_root() / "references" / "orchestrator-prompt.md"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"## Repair Template\n.*?```md\n(?P<template>.*?)\n```", text, flags=re.DOTALL)
    if not match:
        raise McError(f"repair prompt template not found in {path}")
    return match.group("template")


# Gate signatures whose repair keeps the implementation and commit untouched
# and fixes only the named evidence/quality gap.
_EVIDENCE_GATE_LABELS = {
    "validation": "validation",
    "drift": "drift audit",
    "review": "code review",
    "worker-evidence": "worker evidence",
}


def _repair_stanza(
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    gate: GateDecision,
    before_head: str | None,
) -> str:
    signature = gate.signature
    if signature in _EVIDENCE_GATE_LABELS:
        label = _EVIDENCE_GATE_LABELS[signature]
        return (
            "Your code changes and any commit you already created are present and correct as far as MC verified; "
            f"do NOT re-implement the slice and do NOT redo work that already passed. Fix only the {label} gap "
            f"quoted above: re-run that gate properly, write its evidence artifact under the slice artifact "
            "directory, and record the passing outcome in `orchestrator-result.json`."
        )
    if signature == "unauthorized-files":
        offending = unauthorized_files(set(gate.actual_changed_files), plan_slice.authorized_files)
        start = before_head or "<the slice starting commit recorded by MC>"
        if offending:
            # shlex-quoted so a path with spaces or metacharacters stays one
            # argument when the orchestrator copies the command literally.
            restore_command = shlex.join(["git", "checkout", start, "--", *offending])
        else:
            restore_command = f"git checkout {start} -- <the files named in the gate reason>"
        return (
            "These files are OUTSIDE your authorized surface: "
            + (", ".join(offending) if offending else "(see the gate reason above)")
            + ". This repair is restore-only: restore those exact paths to their pre-slice committed content with\n\n"
            + f"    {restore_command}\n\n"
            + "and touch nothing else. Do not otherwise edit, fix, or improve anything outside your authorized files."
        )
    if signature == "changed-files-mismatch":
        actual = ", ".join(gate.actual_changed_files) if gate.actual_changed_files else "(no changed files)"
        return (
            "Your self-reported `changed_files` does not match git evidence. No file edits are needed: correct the "
            f"`changed_files` list in `orchestrator-result.json` to exactly match the actual diff: {actual}."
        )
    if signature == "commit-missing":
        return (
            "Your gates passed but the required commit was never created. use the commit skill for this slice's "
            "work only, then record the commit in `orchestrator-result.json`."
        )
    if signature == "dirty-worktree":
        status_path = slice_artifact_dir / "git-status-after.txt"
        status_lines = meaningful_status_lines(status_path.read_text(encoding="utf-8")) if status_path.is_file() else []
        listing = "\n".join(status_lines) if status_lines else "(see the gate reason above)"
        return (
            "The worktree has uncommitted changes outside `.ai-mc/` after your commit:\n\n"
            + listing
            + "\n\nResolve them within your authorized surface — commit authorized slice work or restore stray "
            "edits to their committed content — so the worktree ends clean."
        )
    if signature == "result-malformed":
        return (
            "Your `orchestrator-result.json` is unreadable or invalid (see the gate reason above). Your file edits "
            "may be fine; rewrite `orchestrator-result.json` so it is valid JSON matching the required schema, "
            "reporting this same slice honestly."
        )
    if signature == "orchestrator-repairable":
        return (
            "You reported status `repairable` yourself. Resume this same slice: complete the remaining work inside "
            "the frozen contract, re-run validation, the drift-audit skill, and the code-review skill, and write a "
            "fresh `orchestrator-result.json`."
        )
    if signature == "transient-service-unavailable":
        return (
            "MC found a current-attempt, high-confidence transient service-unavailable signal alongside your "
            "terminal self-report. Retry the interrupted operation within this same frozen slice, then complete "
            "the normal validation, audit, review, commit, and structured-result contract. Do not relax any gate."
        )
    if signature == "residual-ledger-mismatch":
        return (
            "An audit or review artifact visibly contains non-empty findings or observations while your structured "
            "`residual_findings` ledger is empty. Reconcile the reporting artifacts: fix any material slice-caused "
            "defect, and copy every legitimate non-blocking post-plan consideration into the ledger with its "
            "disposition, rationale, and suggested follow-up. Then rewrite the result without weakening a verdict."
        )
    if signature == "idle-no-progress":
        return (
            "MC observed no pane progress across the configured observation ceiling. Re-establish your current "
            "slice state from the frozen prompt and repository evidence, continue the interrupted work, and write "
            "the normal structured result only after every unchanged gate has completed."
        )
    raise McError(f"no repair stanza defined for gate signature: {signature!r}")


def render_repair_prompt(
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    gate: GateDecision,
    before_head: str | None = None,
) -> str:
    """Render a targeted in-session correction for a repairable gate failure.

    Composes only from data already on hand (the gate decision, the frozen
    slice contract, and evidence files in the slice artifact directory); it
    never re-derives or relaxes the gate.
    """
    template = load_repair_template()
    authorized = "\n".join(f"- {entry}" for entry in plan_slice.authorized_files) or "- (none parsed from the plan)"
    values = {
        "slice_id": plan_slice.slice_id,
        "slice_title": plan_slice.title,
        "gate_reason": gate.reason,
        "gate_signature": gate.signature,
        "category_stanza": _repair_stanza(plan_slice, slice_artifact_dir, gate, before_head),
        "authorized_files": authorized,
        "delegation_posture": (
            "This slice is opt-in (`Independent audit required: yes`): use separate validated read-only worker requests for "
            "`drift-audit` and `code-review`, wait for drift `PASS` before launching review, and retain both successful contracts."
            if plan_slice.independent_audit_required
            else "This slice uses the default posture: independent delegation remains preferred when a worker is available, "
            "but a local audit is accepted. In either case, drift must return `PASS` before code review starts."
        ),
        "slice_artifact_dir": str(slice_artifact_dir),
        "result_schema_path": str(result_schema_path()),
    }
    return template.format(**values).rstrip() + "\n"


def slice_dir_name(plan_slice: PlanSlice) -> str:
    return f"slice-{plan_slice.number:03d}"


def tmux_session_name(run_id_value: str, plan_slice: PlanSlice, generation: int) -> str:
    # Keyed on the session generation, which increments only when a fresh tmux
    # session is launched. In-session repair rounds share one generation, so
    # the session name (and the live session) stays constant across them.
    raw = f"mc_{run_id_value}_{slice_dir_name(plan_slice)}_a{generation}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:80]


def relative_artifact_path(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def capture_worker_runs_summary(slice_artifact_dir: Path) -> None:
    worker_root = slice_artifact_dir / "worker-runs"
    if not worker_root.exists():
        return
    runs: list[dict[str, Any]] = []
    for run_dir in sorted(path for path in worker_root.iterdir() if path.is_dir() and not path.is_symlink()):
        run_entry: dict[str, Any] = {"run_dir": str(run_dir), "workers": []}
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                run_entry["manifest"] = manifest
            except json.JSONDecodeError as exc:
                run_entry["manifest_error"] = str(exc)
        for status_path_obj in sorted(run_dir.glob("*-status.json")):
            try:
                status = json.loads(status_path_obj.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                status = {"path": str(status_path_obj), "error": str(exc)}
            run_entry["workers"].append(status)
        runs.append(run_entry)
    if not runs:
        return
    (slice_artifact_dir / "worker-runs-summary.json").write_text(json.dumps({"runs": runs}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def worker_delegation_overview(slice_artifact_dir: Path) -> list[dict[str, Any]]:
    """Per-worker delegation visibility for summaries.

    Reports every worker launch found under the slice's worker-runs tree:
    label, tool, process state/returncode, and whether the worker's output
    contains the marker its own request's expected_output contracted
    (``RESULT:`` / ``SECTION:``). Observability only — never part of gate
    acceptance. This surfaces failed-then-retried delegations (for example a
    worker that refused its task but exited 0) that the process-level
    worker-evidence gate has no reason to reject.
    """
    worker_root = slice_artifact_dir / "worker-runs"
    overview: list[dict[str, Any]] = []
    if not worker_root.exists():
        return overview
    for run_dir in sorted(path for path in worker_root.iterdir() if path.is_dir() and not path.is_symlink()):
        manifest: dict[str, Any] = {}
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                manifest = {}
        workers = manifest.get("workers") if isinstance(manifest.get("workers"), dict) else {}
        for label, entry in sorted(workers.items()):
            if not isinstance(entry, dict):
                continue
            status: dict[str, Any] = {}
            status_path = run_dir / f"{label}-status.json"
            if status_path.exists():
                try:
                    loaded = json.loads(status_path.read_text(encoding="utf-8"))
                    status = loaded if isinstance(loaded, dict) else {}
                except json.JSONDecodeError:
                    status = {}
            expected = ""
            request_path = run_dir / f"{label}-request.json"
            if request_path.exists():
                try:
                    expected = str(json.loads(request_path.read_text(encoding="utf-8")).get("expected_output") or "")
                except (json.JSONDecodeError, AttributeError):
                    expected = ""
            markers = [marker for marker in ("RESULT:", "SECTION:") if marker in expected]
            out_text = ""
            outfile = entry.get("outfile")
            candidates = [Path(outfile)] if isinstance(outfile, str) else []
            # Manifest outfile paths are absolute; fall back to the
            # conventional sibling file so archived/relocated evidence
            # still reads.
            candidates.append(run_dir / f"{label}-out.txt")
            for candidate in candidates:
                if candidate.is_file():
                    try:
                        out_text = candidate.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        out_text = ""
                    break
            marker_state = "n/a"
            if markers:
                marker_state = "present" if any(marker in out_text for marker in markers) else "absent"
            # Verbatim evidence for the reviewer, never interpretation: the
            # last non-empty output line is usually enough to tell a refusal
            # ("How would you like to proceed?") from a crash or an answer.
            output_tail = ""
            for line in reversed(out_text.splitlines()):
                stripped = line.strip()
                if stripped:
                    output_tail = stripped[:160]
                    break
            overview.append(
                {
                    "run_dir": str(run_dir),
                    "label": str(label),
                    "tool": str(entry.get("tool", "")),
                    "state": str(status.get("state", "unknown")),
                    "returncode": status.get("returncode"),
                    "contracted_marker": marker_state,
                    "output_tail": output_tail,
                }
            )
    return overview


def sensitive_artifact_dirs(run_dir: Path) -> list[Path]:
    paths: list[Path] = []
    slices_dir = run_dir / "slices"
    if not slices_dir.exists():
        return paths
    for path in slices_dir.glob("slice-*/*"):
        if path.is_dir() and path.name in SENSITIVE_ARTIFACT_NAMES:
            paths.append(path)
    return sorted(paths)

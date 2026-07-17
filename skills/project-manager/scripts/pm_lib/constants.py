from __future__ import annotations

import re
import shlex
from typing import Any


SCHEMA_VERSION = 5
PARSER_NAME = "implementation-plan-markdown-v2"
FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_SECTIONS = (
    "Intended Change",
    "Acceptance Criteria",
    "Authorized Surface",
    "Explicit Non-Goals",
    "Risk Flags",
    "Validation Plan",
    "Rollback Path",
)
# "assumed-complete" is written only by `init --assume-complete`: an operator
# attestation that a slice was already completed (and committed) before this
# run existed, e.g. under a previous run whose plan was edited to clear an
# approval gate. PM never assigns it from gate verification.
COMPLETED_SLICE_STATUSES = {"pass", "assumed-complete"}
DEVELOPER_STATUSES = {"pass", "repairable", "needs-human", "fail", "blocked"}
CONTINUATION_NOTE_CATEGORIES = {
    "decision",
    "implementation-lesson",
    "failed-approach",
    "interface-contract",
    "validation-lesson",
    "environment-tooling",
    "reviewer-lesson",
    "risk-warning",
    "future-slice-guidance",
}
MAX_CONTINUATION_NOTES = 100
MAX_CONTINUATION_FIELD_CHARS = 4000
MAX_PRIOR_SLICE_CONTEXT_BYTES = 524288
RUN_ACTIVE_STATUSES = {"initialized", "running", "paused", "resuming", "partial"}
RUN_STOP_STATUSES = {"needs-human", "blocked", "failed", "cancelled"}
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_POLL_SECONDS = 2.0
# Per-slice repair budget: how many repairable gate failures PM will steer
# (in-session nudge, fresh-session escalation, or dead-session relaunch)
# before stopping for a human. Every repair is re-verified by the complete,
# unrelaxed gate, so a generous budget can waste attempts but never accept a
# bad slice. Raised from 1 with the self-correcting repair loop.
DEFAULT_MAX_REPAIR_ATTEMPTS = 3
OPERATIONAL_EVENTS_FILENAME = "operational-events.jsonl"
DEFAULT_SUPERVISION: dict[str, Any] = {
    "mode": "deterministic-batch",
    "pause_policy": {
        "rolling_usage_limit": "wait-until-reset-plus-buffer",
        "weekly_usage_limit": "stop-for-user",
        "transient_service_unavailable": "bounded-retry",
        "unknown_operational_event": "stop-for-user",
    },
    "default_resume_prompt": "You were interrupted. Review what you were doing then continue.",
    "default_reset_buffer_seconds": 180,
    "max_single_pause_seconds": 21600,
    "max_consecutive_pauses_per_slice": 2,
    "max_cumulative_pause_seconds_per_run": 43200,
    "max_transient_retries_per_slice": 3,
    "max_observe_staleness_seconds": 600,
    "min_idle_observation_windows": 3,
    "pause_counters": {
        "consecutive_pauses_current_slice": 0,
        "cumulative_pause_seconds_run": 0,
    },
}

HARNESS_PROFILES: dict[str, dict[str, Any]] = {
    "codex": {
        "base_command": ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "never"],
        "model_flag": "-m",
        "effort_config_key": "model_reasoning_effort",
        "reviewer_network_flag": ["-c", "sandbox_workspace_write.network_access=true"],
        "commit_git_access_flag": "--add-dir",
        "notes": [
            "Use --no-alt-screen for durable tmux captures.",
            "Optional PM profile model and effort overrides are composed as -m and -c model_reasoning_effort=... .",
            "Reviewer-backed runs need sandbox network access.",
            "Commit-required runs need scoped write access to the repository git directory.",
            "When used as a reviewer (not developer), gets a per-slice CODEX_HOME seeded with a copy of the "
            "real auth.json, since Codex's home dir doubles as its credential store.",
        ],
    },
    "claude": {
        "base_command": ["claude", "--permission-mode", "auto"],
        "model_flag": "--model",
        "effort_flag": "--effort",
        "notes": [
            "Uses Claude Code's permission classifier for unattended routine actions.",
            "Optional PM profile model and effort overrides are composed while preserving --session-id transcript capture.",
            "Do not launch Claude reviewers from inside a Claude developer.",
            "As developer, launched with --session-id so PM can capture the full JSONL transcript "
            "as developer-transcript.jsonl (pane capture alone loses detail behind Claude Code's "
            "'ctrl+o to expand' collapsing).",
            "When used as a reviewer (not developer), uses the operator's normal Claude Code auth/config unless "
            "standard Claude auth environment variables are provided; copying .credentials.json into an isolated "
            "CLAUDE_CONFIG_DIR is not portable.",
        ],
    },
    "copilot": {
        "base_command": ["copilot", "--allow-all-tools", "--autopilot"],
        "model_flag": "--model",
        "effort_flag": "--effort",
        "notes": [
            "Mechanically validated as a PM developer harness: bare interactive TUI accepts tmux paste-buffer "
            "plus double-Enter prompt injection identically to codex/claude, and its directory-trust dialog text "
            "('Do you trust the files in this folder?') matches an existing generic TRUST_PROMPT_MARKERS entry, so "
            "PM already fails closed on it.",
            "Developer or Reviewer selection is a per-run operator choice, not a capability ranking in this profile.",
            "When used as a reviewer (not developer), gets a per-slice COPILOT_HOME for sandboxed session state; "
            "as developer it keeps the operator's real ~/.copilot config.",
            "Coverage gap: only the directory-trust prompt has been directly observed; other Copilot prompt classes "
            "(credential, permission-denial, external side effect) rely on the same generic keyword markers used "
            "for every harness and have not been individually triggered and confirmed.",
        ],
    },
    "opencode": {
        "base_command": ["opencode", "--auto"],
        "model_flag": "-m",
        "model_inventory_command": ["opencode", "models", "{provider}", "--verbose"],
        "model_inventory_verbose_json": True,
        "notes": [
            "Mechanically validated as a PM developer harness: bare interactive TUI shows a stable 'Ask "
            "anything...' idle placeholder as a ready marker and accepts the same tmux paste-buffer plus "
            "double-Enter prompt injection as codex/claude/copilot.",
            "No effort_flag: the interactive TUI command this profile launches has no effort/reasoning flag, "
            "so an effort request fails closed at command-compose time with "
            "a clear PmError instead of launching a broken command that exits before the prompt can be sent.",
            "Primarily backed by local/self-hosted models (see ~/.config/opencode/opencode.json, served via "
            "~/.llm/llama-server/); it may also be configured with subscription models. The operator chooses the "
            "model and role; this profile records mechanics only.",
            "Coverage gap: no opencode-specific hard-stop-prompt text (credential, permission-denial, external "
            "side effect) has been directly observed; detection relies on the same generic keyword markers used "
            "for every harness. The whitelisted-directory permission prompt implied by opencode.json's "
            "external_directory 'ask' rule has not been triggered and confirmed.",
        ],
    },
}

# User-approved exception (explicit choice via AskUserQuestion): the operator
# may opt in to a known unattended-safe launch command with
# --allow-unattended-default. Without that flag, or for any other harness
# name, PM still fails closed and requires --harness-command. Bare harness
# names otherwise resolve to an interactive session (see TmuxHarnessAdapter):
# tmux pastes the prompt and presses enter as if a human were typing, so an
# unflagged harness process may prompt for per-action approval that nothing in
# this loop can answer, silently deadlocking the run until --timeout-seconds
# expires. Derived from each profile's base_command so the two launch
# vocabularies (--allow-unattended-default and --allow-profile-command) cannot
# drift apart.
KNOWN_UNATTENDED_HARNESS_COMMANDS: dict[str, str] = {
    name: shlex.join(profile["base_command"]) for name, profile in HARNESS_PROFILES.items()
}

SENSITIVE_ARTIFACT_NAMES = {"copilot-home", "codex-home", "claude-config-dir", "tool-homes"}

# Plan-lint vocabulary for check-plan. PM's dependency/license/side-effect stop
# conditions are heuristic (pane markers plus prompt prohibitions), not diff
# inspection: a silent dependency edit inside an authorized surface would pass
# the file-authorization gate. The compensating control is plan-level — keep
# these files out of unattended authorized surfaces or approval-gate the slice —
# so check-plan warns when an authorized entry looks dependency- or
# license-shaped. Basenames are matched case-insensitively.
DEPENDENCY_SURFACE_BASENAMES = {
    "package.json",
    "pipfile",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "cargo.toml",
    "go.mod",
    "go.sum",
    "gemfile",
    "composer.json",
    "environment.yml",
    "environment.yaml",
    "flake.nix",
}
DEPENDENCY_SURFACE_PREFIXES = ("requirements",)
DEPENDENCY_SURFACE_SUFFIXES = (".lock", "-lock.json", "-lock.yaml", "-lock.yml")
LICENSE_SURFACE_PREFIXES = ("license", "copying", "notice", "patents")
# These recursive globs match every repository-relative path under the
# segment-aware authorization matcher. Root-like entries such as '.', './',
# and '/' match no changed path and are rejected as invalid plan input instead.
BROAD_SURFACE_ENTRIES = {"**", "**/*"}
TOP_LEVEL_ONLY_SURFACE_ENTRIES = {"*"}

# Reviewer-tool home directories are not interchangeable. Copilot's real GitHub
# credential lives outside ~/.copilot (gh CLI config / OS keychain), so
# redirecting COPILOT_HOME to an isolated per-slice directory only needs a
# writable dir. Codex's auth.json is portable enough for the local PM reviewer
# profile and can be copied into per-slice CODEX_HOME. Claude Code's subscription
# OAuth state is not safely reproduced by copying ~/.claude/.credentials.json
# into an isolated CLAUDE_CONFIG_DIR; use the operator's normal Claude config or
# standard Claude auth environment variables instead.
REVIEWER_CREDENTIAL_HOMES: dict[str, tuple[str, str, str]] = {
    "codex": ("CODEX_HOME", ".codex", "auth.json"),
}

# The two skills an opt-in ("Independent audit required: yes") slice must
# delegate as separate, exactly-one-skill reviewer requests (see gates.py's
# finalize-time enforcement and reviewer_contract.py's reserved_skill_sets
# pre-launch check). Single source of truth so the two layers cannot drift.
REQUIRED_AUDIT_SKILLS = ("drift-audit", "code-review")

# Detects "push / create PR / deploy / install dependency / license change"
# prompts — the external-side-effect stop condition enforced at two layers:
# tmux_adapter.detect_hard_prompt (send-time guard, whitespace-normalized
# original-case pane text) and hints.extract_operational_hints (offline
# hint extraction over already-lowercased text). One definition so a pattern
# fix can't leave either layer silently stale; IGNORECASE makes it correct
# against both a lowercased string and original-case text.
EXTERNAL_SIDE_EFFECT_PROMPT_RE = re.compile(
    r"\b(?:do you want to|approve|confirm|allow|permission to|shall i|should i|ready to)\b"
    r"[^.\n?]{0,120}\b(?:push(?: to remote)?|create (?:a )?(?:pull request|pr)|open (?:a )?(?:pull request|pr)|"
    r"deploy|release|publish|install (?:a )?dependenc(?:y|ies)|change (?:the )?license|license change)\b"
    r"|"
    r"\b(?:push to remote|create (?:a )?(?:pull request|pr)|open (?:a )?(?:pull request|pr)|deploy|release|publish|"
    r"install (?:a )?dependenc(?:y|ies)|license change)\b[^.\n]{0,60}(?:\?|yes/no|\[y/n\]|approve|confirm)",
    re.IGNORECASE,
)

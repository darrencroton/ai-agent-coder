from __future__ import annotations

import re
from typing import Any


SCHEMA_VERSION = 1
PARSER_NAME = "implementation-plan-markdown-v1"
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
# approval gate. MC never assigns it from gate verification.
COMPLETED_SLICE_STATUSES = {"pass", "committed", "complete", "assumed-complete"}
ORCHESTRATOR_STATUSES = {"pass", "repairable", "needs-human", "fail", "blocked"}
RUN_ACTIVE_STATUSES = {"initialized", "running", "paused", "resuming", "partial"}
RUN_STOP_STATUSES = {"needs-human", "blocked", "failed", "cancelled"}
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_POLL_SECONDS = 2.0
# Per-slice repair budget: how many repairable gate failures MC will steer
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
    "pause_counters": {
        "consecutive_pauses_current_slice": 0,
        "cumulative_pause_seconds_run": 0,
    },
}

# User-approved exception (explicit choice via AskUserQuestion, this session):
# the operator may opt in to these known unattended-safe launch commands
# with --allow-unattended-default. Without that flag, or for any other
# harness name, MC still fails closed and requires --harness-command. Bare
# harness names otherwise resolve to an interactive session (see
# TmuxHarnessAdapter): tmux pastes the prompt and presses enter as if a human
# were typing, so an unflagged harness process may prompt for per-action
# approval that nothing in this loop can answer, silently deadlocking the run
# until --timeout-seconds expires.
KNOWN_UNATTENDED_HARNESS_COMMANDS: dict[str, str] = {
    "codex": "codex --no-alt-screen -s workspace-write -a never",
    "claude": "claude --permission-mode auto",
    "opencode": "opencode --auto",
    "copilot": "copilot --allow-all-tools --autopilot",
}

HARNESS_PROFILES: dict[str, dict[str, Any]] = {
    "codex": {
        "roles": ["orchestrator", "senior-worker"],
        "base_command": ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "never"],
        "model_flag": "-m",
        "effort_config_key": "model_reasoning_effort",
        "worker_network_flag": ["-c", "sandbox_workspace_write.network_access=true"],
        "commit_git_access_flag": "--add-dir",
        "notes": [
            "Use --no-alt-screen for durable tmux captures.",
            "Optional MC profile model and effort overrides are composed as -m and -c model_reasoning_effort=... .",
            "Worker-backed runs need sandbox network access.",
            "Commit-required runs need scoped write access to the repository git directory.",
            "When used as a worker (not orchestrator), gets a per-slice CODEX_HOME seeded with a copy of the "
            "real auth.json, since Codex's home dir doubles as its credential store.",
        ],
    },
    "claude": {
        "roles": ["orchestrator", "senior-worker"],
        "base_command": ["claude", "--permission-mode", "auto"],
        "model_flag": "--model",
        "effort_flag": "--effort",
        "notes": [
            "Uses Claude Code's permission classifier for unattended routine actions.",
            "Optional MC profile model and effort overrides are composed while preserving --session-id transcript capture.",
            "Do not launch Claude workers from inside a Claude orchestrator.",
            "As orchestrator, launched with --session-id so MC can capture the full JSONL transcript "
            "as orchestrator-transcript.jsonl (pane capture alone loses detail behind Claude Code's "
            "'ctrl+o to expand' collapsing).",
            "When used as a worker (not orchestrator), uses the operator's normal Claude Code auth/config unless "
            "standard Claude auth environment variables are provided; copying .credentials.json into an isolated "
            "CLAUDE_CONFIG_DIR is not portable.",
        ],
    },
    "copilot": {
        "roles": ["orchestrator", "senior-worker", "junior-worker"],
        "base_command": ["copilot", "--allow-all-tools", "--autopilot"],
        "model_flag": "--model",
        "effort_flag": "--effort",
        "notes": [
            "Mechanically validated as an MC orchestrator harness: bare interactive TUI accepts tmux paste-buffer "
            "plus double-Enter prompt injection identically to codex/claude, and its directory-trust dialog text "
            "('Do you trust the files in this folder?') matches an existing generic TRUST_PROMPT_MARKERS entry, so "
            "MC already fails closed on it.",
            "Which role (orchestrator, senior worker, junior worker) fits a given task is a per-run operator/model "
            "decision based on the configured Copilot model's demonstrated capability, not a fixed property of "
            "this profile — see harness-adapter-contract.md and ai-orchestrator's role definitions.",
            "When used as a worker (not orchestrator), gets a per-slice COPILOT_HOME for sandboxed session state; "
            "as orchestrator it keeps the operator's real ~/.copilot config.",
            "Coverage gap: only the directory-trust prompt has been directly observed; other Copilot prompt classes "
            "(credential, permission-denial, external side effect) rely on the same generic keyword markers used "
            "for every harness and have not been individually triggered and confirmed.",
        ],
    },
    "opencode": {
        "roles": ["orchestrator", "senior-worker", "junior-worker"],
        "base_command": ["opencode", "--auto"],
        "model_flag": "-m",
        "effort_flag": "--variant",
        "notes": [
            "Mechanically validated as an MC orchestrator harness: bare interactive TUI shows a stable 'Ask "
            "anything...' idle placeholder as a ready marker and accepts the same tmux paste-buffer plus "
            "double-Enter prompt injection as codex/claude/copilot.",
            "Primarily backed by local/self-hosted models (see ~/.config/opencode/opencode.json, served via "
            "~/.llm/llama-server/); may also be configured with subscription models. Role fit depends entirely on "
            "the configured model's demonstrated capability, not on this profile — weak local models should stay "
            "in junior-worker or narrowly-scoped senior-worker use even though the mechanics support all roles.",
            "Coverage gap: no opencode-specific hard-stop-prompt text (credential, permission-denial, external "
            "side effect) has been directly observed; detection relies on the same generic keyword markers used "
            "for every harness. The whitelisted-directory permission prompt implied by opencode.json's "
            "external_directory 'ask' rule has not been triggered and confirmed.",
        ],
    },
}

SENSITIVE_ARTIFACT_NAMES = {"copilot-home", "codex-home", "claude-config-dir", "tool-homes"}

# Plan-lint vocabulary for check-plan. MC's dependency/license/side-effect stop
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
# Entries that authorize the entire repository defeat the point of a frozen
# surface; check-plan warns rather than blocks so a deliberate whole-repo
# doc-sweep slice remains possible, but never silently.
BROAD_SURFACE_ENTRIES = {"*", "**", "**/*", ".", "./", "/"}

# Worker-tool home directories are not interchangeable. Copilot's real GitHub
# credential lives outside ~/.copilot (gh CLI config / OS keychain), so
# redirecting COPILOT_HOME to an isolated per-slice directory only needs a
# writable dir. Codex's auth.json is portable enough for the local MC worker
# profile and can be copied into per-slice CODEX_HOME. Claude Code's subscription
# OAuth state is not safely reproduced by copying ~/.claude/.credentials.json
# into an isolated CLAUDE_CONFIG_DIR; use the operator's normal Claude config or
# standard Claude auth environment variables instead.
WORKER_CREDENTIAL_HOMES: dict[str, tuple[str, str, str]] = {
    "codex": ("CODEX_HOME", ".codex", "auth.json"),
}

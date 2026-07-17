"""Slice runtime: environment/paths, reviewer policy/credentials, and artifact capture.

Residual home for what does not belong in hints.py (operational-hint
extraction), prompts.py (developer/repair prompt rendering), or context.py
(prior-slice-context generation and integrity): environment preflight, skill
and artifact paths, reviewer credential isolation and policy binding, the
per-slice harness environment, developer transcript capture, and reviewer-run
capture/cancel utilities.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .constants import (
    REQUIRED_AUDIT_SKILLS,
    REVIEWER_CREDENTIAL_HOMES,
    SENSITIVE_ARTIFACT_NAMES,
)
from .models import PmError, PlanSlice
from .process import run_command


_REVIEWER_JOBS_MODULE: Any = None


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


def reviewer_jobs_path() -> Path:
    return skill_root().parent / "orchestrator" / "scripts" / "reviewer_jobs.py"


def reviewer_jobs_module() -> Any:
    """Load orchestrator's reviewer_jobs.py as a library module.

    Reused (not reimplemented) here for its session-path conventions, e.g.
    claude_project_root, which already correctly match how Claude Code and
    Codex lay out their on-disk session transcripts for reviewer sessions.
    """
    global _REVIEWER_JOBS_MODULE
    if _REVIEWER_JOBS_MODULE is None:
        path = reviewer_jobs_path()
        spec = importlib.util.spec_from_file_location("pm_reviewer_jobs", path)
        if spec is None or spec.loader is None:
            raise PmError(f"could not load reviewer_jobs module from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _REVIEWER_JOBS_MODULE = module
    return _REVIEWER_JOBS_MODULE


def claude_developer_transcript_path(repo: Path, session_id: str) -> Path:
    return reviewer_jobs_module().claude_project_root(repo.resolve()) / f"{session_id}.jsonl"


def capture_developer_transcript(harness_name: str, repo: Path, session_id: str | None, slice_artifact_dir: Path) -> None:
    """Copy the developer's own structured session transcript into the slice artifacts.

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
    destination = slice_artifact_dir / "developer-transcript.jsonl"
    note_path = slice_artifact_dir / "developer-transcript-note.txt"
    try:
        source = claude_developer_transcript_path(repo, session_id)
    except PmError as exc:
        note_path.write_text(f"developer transcript lookup failed: {exc}\n", encoding="utf-8")
        return
    if source.exists():
        shutil.copy2(source, destination)
        note_path.unlink(missing_ok=True)
    else:
        note_path.write_text(
            "developer transcript not found at expected path: "
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


def reviewer_credential_source(tool: str) -> tuple[Path, str] | None:
    entry = REVIEWER_CREDENTIAL_HOMES.get(tool)
    if not entry:
        return None
    env_var, default_dirname, filename = entry
    return real_tool_home(env_var, default_dirname), filename


def slice_paths(slice_artifact_dir: Path) -> dict[str, Path]:
    return {
        "artifact_dir": slice_artifact_dir,
        "reviewer_artifact_root": slice_artifact_dir / "reviewer-runs",
        "tmp_dir": slice_artifact_dir / "tmp",
        "tool_home_root": slice_artifact_dir / "tool-homes",
        "copilot_home": slice_artifact_dir / "copilot-home",
        "codex_home": slice_artifact_dir / "codex-home",
        "claude_config_dir": slice_artifact_dir / "claude-config-dir",
    }


def seed_reviewer_credentials(paths: dict[str, Path], reviewer_tools: tuple[str, ...], developer_harness_name: str) -> list[str]:
    warnings: list[str] = []
    home_by_tool = {"codex": "codex_home"}
    for tool, home_key in home_by_tool.items():
        if tool not in reviewer_tools or tool == developer_harness_name:
            continue
        source = reviewer_credential_source(tool)
        if source is None:
            continue
        source_dir, filename = source
        source_path = source_dir / filename
        destination = paths[home_key] / filename
        if not source_path.exists():
            warnings.append(f"{tool} reviewer credential source not found: {source_path}")
            continue
        shutil.copy2(source_path, destination)
        os.chmod(destination, 0o600)
    return warnings


def ensure_slice_runtime_dirs(slice_artifact_dir: Path, reviewer_tools: tuple[str, ...] = (), developer_harness_name: str = "") -> list[str]:
    paths = slice_paths(slice_artifact_dir)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return seed_reviewer_credentials(paths, reviewer_tools, developer_harness_name)


def write_reviewer_policy(
    state: dict[str, Any],
    plan_slice: PlanSlice,
    slice_artifact_dir: Path,
    reviewer_tools: tuple[str, ...],
    reviewer_model: str | None,
    reviewer_effort: str | None,
    *,
    before_head: str,
    session_generation: int,
    repair_round: int,
) -> Path:
    """Write the authoritative semantic boundary consumed by orchestrator.

    `before_head`, `session_generation`, and `repair_round` bind the policy —
    and therefore its SHA-256, which every validated launch contract must
    echo (gates.py's exact-match `policy_sha256` check) — to one specific
    slice attempt and repair round. Without this, the digest stays constant
    across repair rounds of a slice, so a Reviewer PASS obtained before a
    tree-changing repair (an unauthorized-files restore, a dirty-worktree
    cleanup) would still satisfy an opt-in independent-audit gate for final
    work the Reviewer never saw. Rewriting the policy at the start of every
    repair round (see runner.py's `_refresh_reviewer_policy_for_repair` and
    `_relaunch_fresh_session`) invalidates any launch contract minted under
    the previous digest with no new gate logic required.
    """
    policy_path = slice_artifact_dir / "reviewer-policy.json"
    policy = {
        "schema_version": 2,
        "run_id": str(state["run_id"]),
        "slice_id": plan_slice.slice_id,
        "plan_sha256": str(state.get("plan", {}).get("sha256") or ""),
        "repo_path": str(Path(state["repo_path"]).resolve()),
        "reviewer_artifact_root": str(slice_paths(slice_artifact_dir)["reviewer_artifact_root"]),
        "required_tools": list(reviewer_tools),
        "required_model": reviewer_model or "default",
        "required_effort": reviewer_effort or "default",
        # Pre-launch companion to gates.py's finalize-time exact-match check:
        # any request whose required_skills touches drift-audit or code-review
        # must equal exactly one of these sets, not a mix or an empty list. An
        # empty required_skills stays valid (a legitimate ad hoc reviewer task
        # unrelated to either audit), so this cannot catch every misdraft —
        # only a request that already names a reserved skill incorrectly.
        "reserved_skill_sets": [[skill] for skill in REQUIRED_AUDIT_SKILLS] if plan_slice.independent_audit_required else [],
        # Attempt/round binding (Finding 15): constant across repair rounds of
        # one slice attempt lineage, so `before_head` verifies cumulatively,
        # while `session_generation`/`repair_round` change every round and so
        # change the digest every round.
        "before_head": before_head,
        "session_generation": int(session_generation),
        "repair_round": int(repair_round),
    }
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return policy_path


def reviewer_policy_snapshot(policy_path: Path) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    return {"sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(), "policy": policy}


def slice_environment(
    slice_artifact_dir: Path,
    run_json: Path,
    plan_path: Path,
    plan_slice: PlanSlice,
    developer_harness_name: str = "",
    reviewer_tools: tuple[str, ...] = (),
) -> dict[str, str]:
    del run_json  # Controller state is intentionally not exposed to the harness.
    paths = slice_paths(slice_artifact_dir)
    env = {
        "ORCHESTRATOR_ARTIFACT_ROOT": str(paths["reviewer_artifact_root"]),
        "PM_RESULT_SCHEMA_PATH": str(result_schema_path()),
        "PM_PLAN_PATH": str(plan_path),
        "PM_PRIOR_SLICE_CONTEXT_PATH": str(slice_artifact_dir / "prior-slice-context.md"),
        "PM_SLICE_ARTIFACT_DIR": str(slice_artifact_dir),
        "PM_SLICE_ID": plan_slice.slice_id,
        "PM_SLICE_TMP_DIR": str(paths["tmp_dir"]),
        "PM_TOOL_HOME_ROOT": str(paths["tool_home_root"]),
        "PM_REVIEWER_ARTIFACT_ROOT": str(paths["reviewer_artifact_root"]),
        "PM_REVIEWER_JOBS_PATH": str(reviewer_jobs_path()),
        "PM_REVIEWER_POLICY_PATH": str(slice_artifact_dir / "reviewer-policy.json"),
        "TMPDIR": str(paths["tmp_dir"]),
    }
    # Only redirect a tool's own home when that tool is a *reviewer* for this
    # run, and never when it is also the developer harness itself — a
    # Copilot or Codex developer must keep its real config/session state.
    # Codex reviewer auth is currently portable via auth.json. Copilot only needs
    # a writable isolated dir (its GitHub credential lives outside ~/.copilot).
    # Claude Code subscription OAuth is not portable by copying
    # .credentials.json into CLAUDE_CONFIG_DIR, so PM deliberately leaves
    # Claude reviewers on the operator's normal config unless the caller supplied
    # standard Claude auth environment variables.
    if "copilot" in reviewer_tools and developer_harness_name != "copilot":
        env["COPILOT_HOME"] = str(paths["copilot_home"])
    if "codex" in reviewer_tools and developer_harness_name != "codex":
        env["CODEX_HOME"] = str(paths["codex_home"])
    return env


def slice_dir_name(plan_slice: PlanSlice) -> str:
    return f"slice-{plan_slice.number:03d}"


def tmux_session_name(run_id_value: str, plan_slice: PlanSlice, generation: int) -> str:
    # Keyed on the session generation, which increments only when a fresh tmux
    # session is launched. In-session repair rounds share one generation, so
    # the session name (and the live session) stays constant across them.
    raw = f"pm_{run_id_value}_{slice_dir_name(plan_slice)}_a{generation}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:80]


def relative_artifact_path(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def capture_reviewer_runs_summary(slice_artifact_dir: Path) -> None:
    reviewer_root = slice_artifact_dir / "reviewer-runs"
    if not reviewer_root.exists():
        return
    runs: list[dict[str, Any]] = []
    for run_dir in sorted(path for path in reviewer_root.iterdir() if path.is_dir() and not path.is_symlink()):
        run_entry: dict[str, Any] = {"run_dir": str(run_dir), "reviewers": []}
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
            run_entry["reviewers"].append(status)
        runs.append(run_entry)
    if not runs:
        return
    (slice_artifact_dir / "reviewer-runs-summary.json").write_text(json.dumps({"runs": runs}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cancel_reviewer_runs(slice_artifact_dir: Path) -> list[dict[str, Any]]:
    """Idempotently stop every helper-tracked reviewer for a terminal slice."""
    reviewer_root = slice_artifact_dir / "reviewer-runs"
    results: list[dict[str, Any]] = []
    if not reviewer_root.exists():
        return results
    for run_dir in sorted(path for path in reviewer_root.iterdir() if path.is_dir() and not path.is_symlink()):
        if not (run_dir / "manifest.json").is_file():
            continue
        result = run_command(
            [sys.executable, str(reviewer_jobs_path()), "cancel", "--run-dir", str(run_dir), "--json"],
            allow_failure=True,
        )
        results.append(
            {
                "run_dir": str(run_dir),
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        )
    if results:
        (slice_artifact_dir / "reviewer-cancel-summary.json").write_text(
            json.dumps({"runs": results}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return results


def cancel_run_reviewers(run_dir: Path) -> list[dict[str, Any]]:
    """Cancel tracked reviewers across every slice, including stale prior slices."""
    results: list[dict[str, Any]] = []
    slices_dir = run_dir / "slices"
    if not slices_dir.exists():
        return results
    for artifact_dir in sorted(path for path in slices_dir.glob("slice-*") if path.is_dir() and not path.is_symlink()):
        results.extend(cancel_reviewer_runs(artifact_dir))
        capture_reviewer_runs_summary(artifact_dir)
    return results


def reviewer_delegation_overview(slice_artifact_dir: Path) -> list[dict[str, Any]]:
    """Per-reviewer delegation visibility for summaries.

    Reports every reviewer launch found under the slice's reviewer-runs tree:
    label, tool, process state/returncode, and whether the reviewer's output
    contains the marker its own request's expected_output contracted
    (``RESULT:`` / ``SECTION:``). Observability only — never part of gate
    acceptance. This surfaces failed-then-retried delegations (for example a
    reviewer that refused its task but exited 0) that the process-level
    reviewer-evidence gate has no reason to reject.
    """
    reviewer_root = slice_artifact_dir / "reviewer-runs"
    overview: list[dict[str, Any]] = []
    if not reviewer_root.exists():
        return overview
    for run_dir in sorted(path for path in reviewer_root.iterdir() if path.is_dir() and not path.is_symlink()):
        manifest: dict[str, Any] = {}
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                manifest = {}
        reviewers = manifest.get("reviewers") if isinstance(manifest.get("reviewers"), dict) else {}
        for label, entry in sorted(reviewers.items()):
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

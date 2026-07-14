#!/usr/bin/env python3
"""Validate, launch, and manage tracked Reviewer runs for the orchestrator skill.

Reviewer artifacts are written to .orchestrator/runs/ in the current project
by default (override with ORCHESTRATOR_ARTIFACT_ROOT). Provides per-run
directories, semantic contract validation, deterministic harness composition,
manifest tracking, status, activity, cancel, and extract commands.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from reviewer_contract import (
    ReviewerContractError,
    REVIEWER_PROFILES,
    LABEL_RE as CONTRACT_LABEL_RE,
    compose_reviewer_command,
    compile_skill_bundle,
    load_json_object,
    render_reviewer_prompt,
    sha256_path,
    validate_contract,
)
from reviewer_sessions import (
    claude_project_root,
    extract_session_text,
    infer_tool_name,
    looks_like_codex_exec_transcript,
    resolve_session_path,
    session_activity,
    session_id_from_path,
)


ARTIFACT_ROOT_ENV = "ORCHESTRATOR_ARTIFACT_ROOT"
STATE_DIR_NAME = ".orchestrator"
RUN_SCHEMA_VERSION = 2
RUNS_DIR_NAME = "runs"
MANIFEST_NAME = "manifest.json"
MANIFEST_LOCK_NAME = ".manifest.lock"
INDEX_NAME = "index.json"
INDEX_LOCK_NAME = ".index.lock"
LABEL_RE = re.compile(r"^\d{2}-[a-z0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*(?:-r\d+)?$")
# Match line-based SECTION headers even when a model prefixes them with Markdown.
SECTION_RE = re.compile(r"^\s*(?:#+\s*)?SECTION:\s*([A-Za-z0-9_ -]+)\s*$", re.MULTILINE)
AUDIT_VERDICT_RE = re.compile(
    r"^PM_AUDIT_VERDICT:\s*(PASS WITH RISKS|PASS|FAIL|BLOCKED)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_LIBRARY_WRAPPERS: dict[int, subprocess.Popen[bytes]] = {}


class ReviewerJobsError(RuntimeError):
    """Raised for expected operational errors."""


def artifact_root_override() -> Path | None:
    override = os.environ.get(ARTIFACT_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return None


def existing_state_dir(start: Path | None = None) -> Path | None:
    base = (start or Path.cwd()).expanduser().resolve()
    for candidate in (base, *base.parents):
        state_dir = candidate / STATE_DIR_NAME
        if state_dir.exists():
            return state_dir
    return None


def default_state_dir(start: Path | None = None) -> Path:
    override = artifact_root_override()
    if override is not None:
        return override
    existing = existing_state_dir(start)
    if existing is not None:
        return existing
    return ((start or Path.cwd()).expanduser().resolve() / STATE_DIR_NAME)


def default_root(start: Path | None = None) -> Path:
    override = artifact_root_override()
    if override is not None:
        return override
    return default_state_dir(start) / RUNS_DIR_NAME


def state_dir_from_run_dir(path: Path) -> Path | None:
    resolved = path.expanduser().resolve()
    override = artifact_root_override()
    if override is not None:
        try:
            resolved.relative_to(override)
        except ValueError:
            return None
        return override
    for parent in resolved.parents:
        if parent.name != RUNS_DIR_NAME:
            continue
        state_dir = parent.parent
        if state_dir.name == STATE_DIR_NAME:
            return state_dir
    return None


def resolve_run_dir(arg: str) -> Path:
    """Resolve --run-dir; 'current' follows the .orchestrator/current symlink."""
    if arg == "current":
        current_link = default_state_dir() / "current"
        if not current_link.exists():
            raise ReviewerJobsError("No current run: .orchestrator/current symlink not found.")
        return current_link.resolve()
    return Path(arg).expanduser().resolve()


def ensure_managed_run_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    state_dir = state_dir_from_run_dir(resolved)
    if state_dir is None:
        managed_root = default_root()
        raise ReviewerJobsError(
            f"Run directory must live under helper-managed artifact root {managed_root}. "
            f"Set {ARTIFACT_ROOT_ENV} to override the root."
        )
    return resolved


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_identity(pid: int) -> str | None:
    """Return the stable process start time used to reject a reused PID.

    The command string is deliberately excluded: a freshly forked child may
    cross ``exec`` between two observations while retaining the same PID and
    start time.
    """
    if not process_running(pid):
        return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        check=False,
        capture_output=True,
        text=True,
    )
    identity = result.stdout.strip()
    return identity if result.returncode == 0 and identity else None


def tracked_wrapper_running(entry: dict[str, Any]) -> bool:
    """Confirm a manifest pid still belongs to this helper before signalling it."""
    pid = int(entry.get("pid", 0))
    if not process_running(pid):
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    command = result.stdout.strip()
    status_file = str(entry.get("status_file") or "")
    return result.returncode == 0 and str(Path(__file__).resolve()) in command and "_runner" in command and status_file in command


def tracked_child_running(entry: dict[str, Any]) -> bool:
    """Confirm the recorded child still has the identity captured at launch."""
    status_file = Path(str(entry.get("status_file") or ""))
    status = read_json(status_file) if status_file.is_file() else {}
    child_pid = int(status.get("child_pid") or 0)
    expected_identity = status.get("child_identity")
    return (
        child_pid > 0
        and isinstance(expected_identity, str)
        and bool(expected_identity)
        and process_identity(child_pid) == expected_identity
    )


def signal_tracked_child(entry: dict[str, Any], requested_signal: int) -> bool:
    """Signal only the child whose launch identity is preserved in status."""
    if not tracked_child_running(entry):
        return False
    status = read_json(Path(str(entry["status_file"])))
    child_pid = int(status["child_pid"])
    try:
        if os.getpgid(child_pid) == child_pid:
            os.killpg(child_pid, requested_signal)
        else:
            os.kill(child_pid, requested_signal)
    except ProcessLookupError:
        return False
    return True


def mark_cancelled_entry(entry: dict[str, Any], *, forced: bool, returncode: int) -> None:
    """Persist a terminal cancellation after all tracked processes are gone."""
    status_file = Path(str(entry.get("status_file") or ""))
    status = read_json(status_file) if status_file.is_file() else {}
    write_json(
        status_file,
        {
            **status,
            "label": entry.get("label"),
            "state": "cancelled",
            "finished_at": iso_now(),
            "cancel_requested": True,
            "forced": forced,
            "returncode": status.get("returncode", returncode),
        },
    )


def force_cancel_entry(entry: dict[str, Any]) -> None:
    """Kill both helper wrapper and its separately-sessioned child process."""
    permission_errors: list[str] = []
    try:
        signal_tracked_child(entry, signal.SIGKILL)
    except PermissionError as exc:
        permission_errors.append(f"child: {exc}")
    wrapper_pid = int(entry.get("pid", 0))
    if tracked_wrapper_running(entry):
        try:
            os.killpg(wrapper_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            permission_errors.append(f"wrapper: {exc}")
    if permission_errors:
        raise PermissionError("; ".join(permission_errors))
    mark_cancelled_entry(entry, forced=True, returncode=-signal.SIGKILL)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp_path.replace(path)


def normalize_command(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def normalize_section_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")




def validate_label(label: str) -> None:
    if LABEL_RE.fullmatch(label):
        return
    raise ReviewerJobsError(
        "Reviewer label must match <nn>-<tool>-<subtask-slug>[-rN] in lowercase kebab-case, "
        f"for example 01-codex-trace-login or 03-claude-review-plan-r1: {label}"
    )


def manifest_path(run_dir: Path) -> Path:
    return run_dir / MANIFEST_NAME


def manifest_lock_path(run_dir: Path) -> Path:
    return run_dir / MANIFEST_LOCK_NAME


def index_path(state_dir: Path) -> Path:
    return state_dir / INDEX_NAME


def index_lock_path(state_dir: Path) -> Path:
    return state_dir / INDEX_LOCK_NAME


@contextmanager
def hold_lock(lock_path: Path, description: str):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise ReviewerJobsError(f"Unable to lock {description}: {lock_path.parent}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def hold_manifest_lock(run_dir: Path):
    with hold_lock(manifest_lock_path(run_dir), f"manifest for run directory {run_dir}"):
        yield


@contextmanager
def hold_index_lock(state_dir: Path):
    with hold_lock(index_lock_path(state_dir), f"index for state directory {state_dir}"):
        yield


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = manifest_path(run_dir)
    if not path.exists():
        raise ReviewerJobsError(f"Run directory has no manifest: {run_dir}")
    manifest = read_json(path)
    if manifest.get("schema_version") != RUN_SCHEMA_VERSION:
        raise ReviewerJobsError(
            f"Unsupported Reviewer run manifest schema: expected {RUN_SCHEMA_VERSION}, "
            f"got {manifest.get('schema_version')!r}. Start a new .orchestrator run."
        )
    return manifest


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    write_json(manifest_path(run_dir), manifest)


def ensure_manifest(run_dir: Path) -> dict[str, Any]:
    path = manifest_path(run_dir)
    if path.exists():
        return load_manifest(run_dir)
    manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "created_at": iso_now(),
        "run_dir": str(run_dir),
        "reviewers": {},
    }
    save_manifest(run_dir, manifest)
    return manifest


def derive_run_dir(root: Path, prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / f"{prefix}-{stamp}-{os.getpid()}"


def load_index(state_dir: Path) -> list[dict[str, Any]]:
    path = index_path(state_dir)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return payload if isinstance(payload, list) else []


def save_index(state_dir: Path, index: list[dict[str, Any]]) -> None:
    write_json(index_path(state_dir), index)


def reviewer_status(entry: dict[str, Any]) -> dict[str, Any]:
    pid = int(entry.get("pid", 0))
    status_file = Path(entry["status_file"])
    outfile = Path(entry["outfile"])
    errfile = Path(entry["errfile"])
    status_payload = read_json(status_file) if status_file.exists() else {}
    status_state = status_payload.get("state")
    if status_state in {"completed", "cancelled", "failed"}:
        running = False
    else:
        running = process_running(pid)
    returncode = status_payload.get("returncode")
    if running:
        state = "running"
    elif status_state == "cancelled":
        state = "cancelled"
    elif returncode not in (None, 0):
        state = "failed"
    elif returncode == 0:
        state = status_state or "completed"
    elif status_state == "running":
        state = "stalled"
    else:
        state = status_state or "finished"
    return {
        "label": entry["label"],
        "pid": pid,
        "tool": entry.get("tool"),
        "state": state,
        "running": running,
        "outfile": str(outfile),
        "outfile_exists": outfile.exists(),
        "outfile_size": outfile.stat().st_size if outfile.exists() else 0,
        "errfile": str(errfile),
        "errfile_exists": errfile.exists(),
        "errfile_size": errfile.stat().st_size if errfile.exists() else 0,
        "returncode": returncode,
        "started_at": entry.get("started_at"),
        "finished_at": status_payload.get("finished_at"),
        "command": entry.get("command", []),
    }


def run_status_from_manifest(manifest: dict[str, Any]) -> str:
    states = {reviewer_status(entry)["state"] for entry in manifest.get("reviewers", {}).values()}
    if not states or states & {"running", "stalled", "finished"}:
        return "active"
    if "failed" in states:
        return "failed"
    if "cancelled" in states:
        return "cancelled"
    return "completed"


def sync_run_index(run_dir: Path, *, manifest: dict[str, Any] | None = None, status: str | None = None) -> None:
    state_dir = state_dir_from_run_dir(run_dir)
    if state_dir is None:
        return
    manifest = manifest if manifest is not None else load_manifest(run_dir)
    run_status = status or run_status_from_manifest(manifest)
    created_at = manifest.get("created_at") or iso_now()
    with hold_index_lock(state_dir):
        index = load_index(state_dir)
        for entry in index:
            if entry.get("run") != run_dir.name:
                continue
            entry["created_at"] = entry.get("created_at") or created_at
            entry["status"] = run_status
            break
        else:
            index.append({"created_at": created_at, "run": run_dir.name, "status": run_status})
        save_index(state_dir, index)


def find_section_blocks(text: str) -> list[tuple[str, int, int]]:
    matches = list(SECTION_RE.finditer(text))
    blocks: list[tuple[str, int, int]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        blocks.append((normalize_section_name(match.group(1)), start, end))
    return blocks


def extract_sections(text: str, names: list[str]) -> str:
    wanted = {normalize_section_name(name) for name in names}
    blocks = find_section_blocks(text)
    if not blocks:
        return text
    parts = [text[start:end].rstrip() for name, start, end in blocks if name in wanted]
    return "\n\n".join(part for part in parts if part).strip() or text


def helper_activity(entry: dict[str, Any], now: float) -> dict[str, Any]:
    latest_mtime = None
    latest_path: Path | None = None
    for path_key in ("outfile", "errfile", "status_file"):
        raw_path = entry.get(path_key)
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path)
        if not path.exists():
            continue
        path_mtime = path.stat().st_mtime
        if latest_mtime is None or path_mtime > latest_mtime:
            latest_mtime = path_mtime
            latest_path = path
    if latest_mtime is None:
        return {}
    return {
        "activity_source": "helper_files",
        "last_activity_at": datetime.fromtimestamp(latest_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_activity_age_s": max(0, int(now - latest_mtime)),
        "last_activity_path": str(latest_path) if latest_path is not None else None,
    }


def extract_best_result(entry: dict[str, Any]) -> dict[str, str]:
    status_file = Path(entry["status_file"])
    status_payload = read_json(status_file) if status_file.exists() else {}
    outfile = Path(entry["outfile"])
    if outfile.exists() and outfile.stat().st_size > 0:
        out_text = outfile.read_text()
        if out_text.strip():
            if entry.get("tool") == "codex" and status_payload.get("returncode") == 0 and looks_like_codex_exec_transcript(out_text):
                session_path = resolve_session_path(entry)
                if session_path is not None:
                    session_text = extract_session_text("codex", session_path)
                    if session_text:
                        return {
                            "source": "codex_session",
                            "source_path": str(session_path),
                            "text": session_text,
                        }
            return {
                "source": "outfile",
                "source_path": str(outfile),
                "text": out_text,
            }

    if entry.get("tool") in {"claude", "codex"} and status_payload.get("returncode") == 0:
        session_path = resolve_session_path(entry)
        if session_path is not None:
            session_text = extract_session_text(str(entry.get("tool")), session_path)
            if session_text:
                return {
                    "source": f"{entry.get('tool')}_session",
                    "source_path": str(session_path),
                    "text": session_text,
                }

    errfile = Path(entry["errfile"])
    if errfile.exists() and errfile.stat().st_size > 0:
        err_text = errfile.read_text()
        blocks = find_section_blocks(err_text)
        if blocks:
            return {
                "source": "errfile_sections",
                "source_path": str(errfile),
                "text": err_text[blocks[-1][1] :].strip(),
            }
        result_idx = err_text.rfind("RESULT:")
        if result_idx != -1:
            return {
                "source": "errfile_result",
                "source_path": str(errfile),
                "text": err_text[result_idx:].strip(),
            }
        return {
            "source": "errfile",
            "source_path": str(errfile),
            "text": err_text,
        }

    raise ReviewerJobsError(f"No output available for reviewer {entry['label']}")


def extract_best_text(entry: dict[str, Any]) -> str:
    return extract_best_result(entry)["text"]


def audit_skill_verdicts(required_skills: list[str], text: str) -> dict[str, str | None]:
    """Extract helper-owned semantic verdicts after the reviewer exits."""
    audit_skills = [skill for skill in required_skills if skill in {"drift-audit", "code-review"}]
    if not audit_skills:
        return {}
    matches = AUDIT_VERDICT_RE.findall(text)
    verdict = matches[0].upper() if len(matches) == 1 else None
    return {skill: verdict for skill in audit_skills}


def command_init(args: argparse.Namespace) -> int:
    root = default_root()
    if args.root:
        requested_root = Path(args.root).expanduser().resolve()
        if requested_root != root:
            raise ReviewerJobsError(
                f"Custom --root is no longer supported. Use {ARTIFACT_ROOT_ENV}={requested_root} "
                "to override the helper-managed artifact root."
            )
    run_dir = derive_run_dir(root, args.prefix)
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = ensure_manifest(run_dir)

    state_dir = default_state_dir()
    current_link = state_dir / "current"
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    current_link.symlink_to(run_dir.relative_to(state_dir))

    sync_run_index(run_dir, manifest=manifest, status="active")

    print(run_dir)
    return 0


def command_profiles(args: argparse.Namespace) -> int:
    del args
    print(json.dumps({"schema_version": RUN_SCHEMA_VERSION, "profiles": REVIEWER_PROFILES}, indent=2, sort_keys=True))
    return 0


def normalize_dependencies(manifest: dict[str, Any], label: str, depends_on: list[str] | None) -> list[str]:
    if not depends_on:
        return []
    reviewers = manifest.get("reviewers", {})
    normalized: list[str] = []
    seen: set[str] = set()
    for dep in depends_on:
        validate_label(dep)
        if dep == label:
            raise ReviewerJobsError(f"Reviewer '{label}' cannot depend on itself.")
        if dep not in reviewers:
            raise ReviewerJobsError(f"Unknown dependency label: {dep}")
        if dep in seen:
            continue
        seen.add(dep)
        normalized.append(dep)
    return normalized


def start_tracked_reviewer(
    run_dir: Path,
    label: str,
    command: list[str],
    *,
    cwd: Path,
    depends_on: list[str] | None = None,
    launch_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a validated reviewer command and persist its durable evidence."""
    validate_label(label)
    tool_name = infer_tool_name(command)

    with hold_manifest_lock(run_dir):
        manifest = ensure_manifest(run_dir)
        if label in manifest["reviewers"]:
            raise ReviewerJobsError(f"Reviewer label already exists in manifest: {label}")
        normalized_dependencies = normalize_dependencies(manifest, label, depends_on)

        started_at = iso_now()
        outfile = run_dir / f"{label}-out.txt"
        errfile = run_dir / f"{label}-err.txt"
        status_file = run_dir / f"{label}-status.json"
        wrapper_cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_runner",
            "--label",
            label,
            "--status-file",
            str(status_file),
            "--stdout",
            str(outfile),
            "--stderr",
            str(errfile),
            "--cwd",
            str(cwd),
            "--required-skills-json",
            json.dumps(list((launch_contract or {}).get("required_skills") or [])),
            "--",
            *command,
        ]
        process = subprocess.Popen(
            wrapper_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        if __name__ != "__main__":
            # Library callers (principally tests and PM utilities) keep the
            # Popen handle so they can reap it after an external cancel.
            _LIBRARY_WRAPPERS[process.pid] = process

        manifest["reviewers"][label] = {
            "label": label,
            "tool": tool_name,
            "pid": process.pid,
            "command": command,
            "outfile": str(outfile),
            "errfile": str(errfile),
            "status_file": str(status_file),
            "started_at": started_at,
            "cwd": str(cwd),
        }
        if normalized_dependencies:
            manifest["reviewers"][label]["depends_on"] = normalized_dependencies
        if launch_contract is not None:
            manifest["reviewers"][label]["launch_contract"] = launch_contract
        save_manifest(run_dir, manifest)
        sync_run_index(run_dir, manifest=manifest, status="active")

    if tool_name in {"claude", "codex"}:
        session_path = resolve_session_path(manifest["reviewers"][label], wait_seconds=5.0)
        if session_path is not None:
            with hold_manifest_lock(run_dir):
                manifest = ensure_manifest(run_dir)
                reviewer_entry = manifest["reviewers"].get(label)
                if reviewer_entry is not None:
                    reviewer_entry["session_path"] = str(session_path)
                    reviewer_entry["session_id"] = session_id_from_path(session_path) or session_path.stem
                    save_manifest(run_dir, manifest)

    return {
        "label": label,
        "pid": process.pid,
        "outfile": str(outfile),
        "errfile": str(errfile),
        "status_file": str(status_file),
        "run_dir": str(run_dir),
        "cwd": str(cwd),
    }


def _feedback_paths(run_dir: Path, label: str) -> tuple[Path, Path]:
    return run_dir / f"{label}-request-feedback.json", run_dir / f"{label}-request-feedback.md"


def write_contract_feedback(run_dir: Path, label: str, exc: ReviewerContractError) -> dict[str, Any]:
    payload = {
        "status": "rejected",
        "label": label,
        "issues": [issue.as_dict() for issue in exc.issues],
        "next_action": "Correct only the listed reviewer-request fields, preserve the same slice contract, then run launch again.",
    }
    json_path, markdown_path = _feedback_paths(run_dir, label)
    write_json(json_path, payload)
    lines = [
        "# Reviewer Request Rejected",
        "",
        "The reviewer was not launched. Fix the request and retry; do not substitute a raw harness command.",
        "",
    ]
    for index, issue in enumerate(exc.issues, start=1):
        lines.extend(
            [
                f"## {index}. {issue.code}: `{issue.field}`",
                "",
                issue.message,
                "",
                f"Correction: {issue.correction}",
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return payload


def command_launch(args: argparse.Namespace) -> int:
    run_dir = ensure_managed_run_dir(resolve_run_dir(args.run_dir))
    run_dir.mkdir(parents=True, exist_ok=True)
    request_path = Path(args.request).expanduser().resolve()
    policy_path = Path(args.policy).expanduser().resolve()
    label = "reviewer-request"
    try:
        policy = load_json_object(policy_path, "policy")
        request = load_json_object(request_path, "request")
        if isinstance(request.get("label"), str) and CONTRACT_LABEL_RE.fullmatch(request["label"].strip()):
            label = request["label"].strip()
        contract = validate_contract(policy, request, run_dir)
        label = contract["label"]
        prompt = render_reviewer_prompt(contract)
        command = compose_reviewer_command(contract, prompt)
    except ReviewerContractError as exc:
        payload = write_contract_feedback(run_dir, label, exc)
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    contract_artifact = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": "pass",
        "policy_path": str(policy_path),
        "policy_sha256": sha256_path(policy_path),
        "request_path": str(request_path),
        "request_sha256": sha256_path(request_path),
        "slice_id": contract["slice_id"],
        "plan_sha256": contract["plan_sha256"],
        "tool": contract["tool"],
        "model": contract["model"],
        "effort": contract["effort"],
        "role": contract["role"],
        "access": contract["access"],
        # Preserve the validated semantic purpose of the launch. PM uses this
        # to distinguish independent drift-audit evidence from code-review
        # evidence instead of accepting any successful reviewer process as proof
        # that both gates were delegated.
        "required_skills": list(contract["required_skills"]),
        "repo_path": contract["repo_path"],
        "cwd": contract["repo_path"],
    }
    request_copy = run_dir / f"{label}-request.json"
    policy_copy = run_dir / f"{label}-policy.json"
    prompt_path = run_dir / f"{label}-prompt.md"
    launch_path = run_dir / f"{label}-launch.json"
    write_json(request_copy, request)
    write_json(policy_copy, policy)
    prompt_path.write_text(prompt, encoding="utf-8")
    write_json(launch_path, {**contract_artifact, "resolved_command": command})
    launch_contract = {
        **contract_artifact,
        "policy_artifact": str(policy_copy),
        "request_artifact": str(request_copy),
        "prompt_artifact": str(prompt_path),
        "launch_artifact": str(launch_path),
    }
    result = start_tracked_reviewer(
        run_dir,
        label,
        command,
        cwd=Path(contract["repo_path"]),
        depends_on=args.depends_on,
        launch_contract=launch_contract,
    )
    print(json.dumps({**result, "launch_contract": launch_contract}, indent=2, sort_keys=True))
    return 0


def iter_selected_reviewers(manifest: dict[str, Any], label: str | None) -> list[dict[str, Any]]:
    reviewers = manifest.get("reviewers", {})
    if label is None:
        return [reviewers[key] for key in sorted(reviewers)]
    if label not in reviewers:
        raise ReviewerJobsError(f"Unknown reviewer label: {label}")
    return [reviewers[label]]


def dependency_warnings(manifest: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    """Return warning strings for invalid or incomplete reviewer dependencies."""
    deps = entry.get("depends_on") or []
    if not deps:
        return []
    ws = manifest.get("reviewers", {})
    warnings_list = []
    for dep in deps:
        dep_entry = ws.get(dep)
        if dep_entry is None:
            warnings_list.append(
                f"reviewer '{entry['label']}' depends on unknown reviewer '{dep}'"
            )
            continue
        dep_state = reviewer_status(dep_entry)["state"]
        if dep_state not in {"completed", "cancelled", "failed"}:
            warnings_list.append(
                f"reviewer '{entry['label']}' depends on '{dep}' which is {dep_state}"
            )
    return warnings_list


def command_status(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(args.run_dir)
    manifest = load_manifest(run_dir)
    entries = iter_selected_reviewers(manifest, args.label)
    statuses = [reviewer_status(entry) for entry in entries]
    for entry in entries:
        for warn in dependency_warnings(manifest, entry):
            print(f"WARNING: {warn}", file=sys.stderr)
    if args.json:
        print(json.dumps(statuses, indent=2, sort_keys=True))
        return 0
    for status in statuses:
        suffix = ""
        if status["returncode"] is not None:
            suffix = f" returncode={status['returncode']}"
        print(
            f"{status['label']}: state={status['state']} pid={status['pid']} "
            f"out={status['outfile_size']}B err={status['errfile_size']}B{suffix}"
        )
    return 0


def command_activity(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(args.run_dir)
    manifest = load_manifest(run_dir)
    entries = iter_selected_reviewers(manifest, args.label)
    for entry in entries:
        for warn in dependency_warnings(manifest, entry):
            print(f"WARNING: {warn}", file=sys.stderr)
    now = time.time()
    activity_rows: list[dict[str, Any]] = []

    for entry in entries:
        status = reviewer_status(entry)
        payload = {
            "label": status["label"],
            "state": status["state"],
            "running": status["running"],
            "tool": status["tool"],
            "outfile_size": status["outfile_size"],
            "errfile_size": status["errfile_size"],
        }

        if entry.get("tool") in {"claude", "codex"}:
            session_path = resolve_session_path(entry)
            if session_path is not None:
                session_payload = session_activity(str(entry.get("tool")), session_path)
                payload.update(session_payload)
                payload["activity_source"] = "session"
                payload["healthy"] = status["running"] and session_payload.get("session_mtime_age_s", args.max_idle + 1) <= args.max_idle
            else:
                payload["session_path"] = None
                fallback_payload = helper_activity(entry, now)
                payload.update(fallback_payload)
                if fallback_payload:
                    payload["healthy"] = status["running"] and fallback_payload.get("last_activity_age_s", args.max_idle + 1) <= args.max_idle
                else:
                    payload["healthy"] = False
        else:
            fallback_payload = helper_activity(entry, now)
            payload.update(fallback_payload)
            if fallback_payload:
                payload["healthy"] = status["running"] and payload["last_activity_age_s"] <= args.max_idle
            else:
                payload["healthy"] = False

        activity_rows.append(payload)

    if args.json:
        print(json.dumps(activity_rows, indent=2, sort_keys=True))
        return 0

    for payload in activity_rows:
        line = (
            f"{payload['label']}: state={payload['state']} healthy={'yes' if payload.get('healthy') else 'no'} "
            f"out={payload['outfile_size']}B err={payload['errfile_size']}B"
        )
        if payload.get("tool") in {"claude", "codex"} and payload.get("session_mtime_age_s") is not None:
            line += f" session_age={payload['session_mtime_age_s']}s"
        elif payload.get("last_activity_age_s") is not None:
            line += f" last_activity_age={payload['last_activity_age_s']}s"
        print(line)
        if payload.get("last_assistant_type"):
            detail = payload.get("last_assistant_detail")
            suffix = f":{detail}" if detail else ""
            print(f"  last_assistant={payload['last_assistant_type']}{suffix} at {payload['last_assistant_at']}")
        elif payload.get("last_event_type"):
            detail = payload.get("last_event_detail")
            suffix = f":{detail}" if detail else ""
            print(f"  last_event={payload['last_event_type']}{suffix} at {payload['last_event_at']}")
        elif payload.get("last_activity_at"):
            print(f"  last_activity_at={payload['last_activity_at']}")
        if payload.get("activity_source") == "helper_files" and payload.get("last_activity_path"):
            print(f"  helper_activity={payload['last_activity_path']}")
        if payload.get("session_path"):
            print(f"  session={payload['session_path']}")
    return 0


def command_wait(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(args.run_dir)
    deadline = None if args.timeout is None else time.time() + args.timeout
    while True:
        manifest = load_manifest(run_dir)
        statuses = [reviewer_status(entry) for entry in iter_selected_reviewers(manifest, args.label)]
        if not any(status["running"] for status in statuses):
            failed = [status for status in statuses if status["returncode"] not in (None, 0)]
            if args.json:
                print(json.dumps(statuses, indent=2, sort_keys=True))
            else:
                for status in statuses:
                    print(f"{status['label']}: state={status['state']} returncode={status['returncode']}")
            return 1 if failed else 0
        if deadline is not None and time.time() >= deadline:
            if args.json:
                print(json.dumps(statuses, indent=2, sort_keys=True))
            else:
                for status in statuses:
                    print(f"{status['label']}: state={status['state']} pid={status['pid']}")
            return 1
        time.sleep(args.interval)


def command_extract(args: argparse.Namespace) -> int:
    manifest = load_manifest(resolve_run_dir(args.run_dir))
    entry = iter_selected_reviewers(manifest, args.label)[0]
    result = extract_best_result(entry)
    text = result["text"]
    if args.sections:
        section_names = [part.strip() for part in args.sections.split(",") if part.strip()]
        if section_names:
            text = extract_sections(text, section_names)
    if args.json:
        print(
            json.dumps(
                {
                    "label": entry["label"],
                    "source": result["source"],
                    "source_path": result["source_path"],
                    "text": text.rstrip(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(text.rstrip())
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(args.run_dir)
    manifest = load_manifest(run_dir)
    entries = iter_selected_reviewers(manifest, args.label)

    requested_labels: set[str] = set()
    signal_errors: dict[str, str] = {}
    for entry in entries:
        label = str(entry["label"])
        wrapper_running = tracked_wrapper_running(entry)
        child_running = tracked_child_running(entry)
        if wrapper_running or child_running:
            requested_labels.add(label)
        if wrapper_running:
            try:
                os.kill(int(entry["pid"]), signal.SIGTERM)
            except PermissionError as exc:
                signal_errors[label] = f"wrapper SIGTERM denied: {exc}"
        if child_running:
            try:
                signal_tracked_child(entry, signal.SIGTERM)
            except PermissionError as exc:
                signal_errors[label] = f"child SIGTERM denied: {exc}"

    if not requested_labels:
        return 0

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        manifest = load_manifest(run_dir)
        current_entries = iter_selected_reviewers(manifest, args.label)
        if not any(tracked_wrapper_running(entry) or tracked_child_running(entry) for entry in current_entries):
            # A denied individual signal is not terminal if another owned
            # process completed cleanup and every tracked identity is gone.
            signal_errors.clear()
            for entry in current_entries:
                if str(entry["label"]) in requested_labels:
                    status_path = Path(str(entry["status_file"]))
                    status = read_json(status_path) if status_path.is_file() else {}
                    if status.get("state") not in {"completed", "cancelled", "failed"}:
                        mark_cancelled_entry(entry, forced=False, returncode=-signal.SIGTERM)
            statuses = [reviewer_status(entry) for entry in current_entries]
            if args.json:
                print(json.dumps(statuses, indent=2, sort_keys=True))
            else:
                for status in statuses:
                    print(f"{status['label']}: state={status['state']} returncode={status['returncode']}")
            return 0
        time.sleep(args.interval)

    manifest = load_manifest(run_dir)
    remaining = iter_selected_reviewers(manifest, args.label)
    for entry in remaining:
        if tracked_wrapper_running(entry) or tracked_child_running(entry):
            label = str(entry["label"])
            try:
                force_cancel_entry(entry)
                signal_errors.pop(label, None)
            except PermissionError as exc:
                signal_errors[label] = f"SIGKILL denied: {exc}"
    statuses = [reviewer_status(entry) for entry in remaining]
    if args.json:
        print(json.dumps(statuses, indent=2, sort_keys=True))
    else:
        for status in statuses:
            print(f"{status['label']}: state={status['state']} pid={status['pid']}")
    if signal_errors:
        details = "; ".join(f"{label}: {detail}" for label, detail in sorted(signal_errors.items()))
        raise ReviewerJobsError(f"Unable to cancel every selected reviewer: {details}")
    return 0 if not any(status["running"] for status in statuses) else 1


def command_runner(args: argparse.Namespace) -> int:
    command = normalize_command(args.command)
    if not command:
        raise ReviewerJobsError("Runner requires a reviewer command.")

    status_file = Path(args.status_file)
    stdout_path = Path(args.stdout)
    stderr_path = Path(args.stderr)
    status_file.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    child: subprocess.Popen[bytes] | None = None
    cancel_requested = False

    def request_cancel(signum: int, frame: Any) -> None:
        del signum, frame
        nonlocal cancel_requested
        cancel_requested = True
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            child.terminate()

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)

    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        child = subprocess.Popen(
            command,
            cwd=args.cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            close_fds=True,
            start_new_session=True,
        )
        write_json(
            status_file,
            {
                "label": args.label,
                "state": "running",
                "started_at": iso_now(),
                "child_pid": child.pid,
                "child_identity": process_identity(child.pid),
                "cwd": str(Path(args.cwd).resolve()),
            },
        )
        returncode = child.wait()
    child_identity = read_json(status_file).get("child_identity")
    try:
        required_skills = json.loads(args.required_skills_json)
    except json.JSONDecodeError:
        required_skills = []
    if not isinstance(required_skills, list) or not all(isinstance(skill, str) for skill in required_skills):
        required_skills = []
    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    skill_verdicts = audit_skill_verdicts(required_skills, stdout_text)
    final_state = "cancelled" if cancel_requested else ("completed" if returncode == 0 else "failed")
    write_json(
        status_file,
        {
            "label": args.label,
            "state": final_state,
            "started_at": read_json(status_file).get("started_at"),
            "finished_at": iso_now(),
            "child_pid": child.pid,
            "child_identity": child_identity,
            "cancel_requested": cancel_requested,
            "returncode": returncode,
            "skill_verdicts": skill_verdicts,
            "cwd": str(Path(args.cwd).resolve()),
        },
    )
    sync_run_index(status_file.parent)
    return returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviewer_jobs.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help=(
            "Create a unique run directory under the helper-managed artifact root "
            f"(override with {ARTIFACT_ROOT_ENV})."
        ),
    )
    init_parser.add_argument("--root", help=argparse.SUPPRESS)
    init_parser.add_argument("--prefix", default="run")
    init_parser.set_defaults(func=command_init)

    profiles_parser = subparsers.add_parser(
        "profiles", help="Show Reviewer harness command mechanics and factual read-only enforcement."
    )
    profiles_parser.set_defaults(func=command_profiles)

    launch_parser = subparsers.add_parser(
        "launch",
        help="Validate a semantic reviewer request against policy, compose the harness command, and start one tracked reviewer.",
    )
    launch_parser.add_argument(
        "--run-dir",
        required=True,
        help=(
            "Per-run directory created by `init`; must live under the helper-managed "
            f"artifact root (override with {ARTIFACT_ROOT_ENV})."
        ),
    )
    launch_parser.add_argument("--policy", required=True, help="PM/Developer reviewer-policy.json path.")
    launch_parser.add_argument("--request", required=True, help="Semantic reviewer-request.json path.")
    launch_parser.add_argument(
        "--depends-on",
        nargs="*",
        metavar="LABEL",
        help="Reviewer labels that must complete before this one (stored in manifest; checked by status/activity).",
    )
    launch_parser.set_defaults(func=command_launch)

    status_parser = subparsers.add_parser("status", help="Show reviewer status.")
    status_parser.add_argument("--run-dir", required=True)
    status_parser.add_argument("--label")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=command_status)

    activity_parser = subparsers.add_parser("activity", help="Show lightweight reviewer activity signals.")
    activity_parser.add_argument("--run-dir", required=True)
    activity_parser.add_argument("--label")
    activity_parser.add_argument("--max-idle", type=int, default=900)
    activity_parser.add_argument("--json", action="store_true")
    activity_parser.set_defaults(func=command_activity)

    wait_parser = subparsers.add_parser("wait", help="Wait for reviewers to finish.")
    wait_parser.add_argument("--run-dir", required=True)
    wait_parser.add_argument("--label")
    wait_parser.add_argument("--timeout", type=float)
    wait_parser.add_argument("--interval", type=float, default=2.0)
    wait_parser.add_argument("--json", action="store_true")
    wait_parser.set_defaults(func=command_wait)

    extract_parser = subparsers.add_parser("extract", help="Read the best available reviewer output.")
    extract_parser.add_argument("--run-dir", required=True)
    extract_parser.add_argument("--label", required=True)
    extract_parser.add_argument(
        "--sections",
        help="Comma-separated section names to extract from matching SECTION header lines. If omitted, print the whole final outfile.",
    )
    extract_parser.add_argument("--json", action="store_true", help="Print the extracted text plus its source metadata as JSON.")
    extract_parser.set_defaults(func=command_extract)

    cancel_parser = subparsers.add_parser("cancel", help="Ask tracked reviewers to stop and wait for status to settle.")
    cancel_parser.add_argument("--run-dir", required=True)
    cancel_parser.add_argument("--label")
    cancel_parser.add_argument("--timeout", type=float, default=10.0)
    cancel_parser.add_argument("--interval", type=float, default=0.5)
    cancel_parser.add_argument("--json", action="store_true")
    cancel_parser.set_defaults(func=command_cancel)

    runner_parser = subparsers.add_parser("_runner", help=argparse.SUPPRESS)
    runner_parser.add_argument("--label", required=True)
    runner_parser.add_argument("--status-file", required=True)
    runner_parser.add_argument("--stdout", required=True)
    runner_parser.add_argument("--stderr", required=True)
    runner_parser.add_argument("--cwd", required=True)
    runner_parser.add_argument("--required-skills-json", default="[]")
    runner_parser.add_argument("command", nargs=argparse.REMAINDER)
    runner_parser.set_defaults(func=command_runner)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except ReviewerJobsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

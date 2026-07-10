"""Validate semantic worker requests and compose deterministic harness commands."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
ACCESS_MODES = {"read-only", "workspace-write"}
WORKER_ROLES = {"junior-worker", "senior-worker"}
LABEL_RE = re.compile(r"^\d{2}-[a-z0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*(?:-r\d+)?$")
WORKER_PROFILES: dict[str, dict[str, Any]] = {
    "claude": {"roles": ["junior-worker", "senior-worker"], "access_modes": ["read-only", "workspace-write"]},
    "codex": {"roles": ["junior-worker", "senior-worker"], "access_modes": ["read-only", "workspace-write"]},
    "copilot": {"roles": ["junior-worker", "senior-worker"], "access_modes": ["workspace-write"]},
    "opencode": {"roles": ["junior-worker", "senior-worker"], "access_modes": ["read-only", "workspace-write"]},
}


@dataclass(frozen=True)
class ContractIssue:
    code: str
    field: str
    message: str
    correction: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field": self.field,
            "message": self.message,
            "correction": self.correction,
        }


class WorkerContractError(RuntimeError):
    """A worker request cannot be launched under the supplied policy."""

    def __init__(self, issues: list[ContractIssue]):
        super().__init__("; ".join(issue.message for issue in issues))
        self.issues = issues


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkerContractError(
            [ContractIssue("missing-file", label, f"{label} file does not exist: {path}", f"Create {path} as valid JSON.")]
        ) from exc
    except json.JSONDecodeError as exc:
        raise WorkerContractError(
            [
                ContractIssue(
                    "invalid-json",
                    label,
                    f"{label} is not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                    f"Fix the JSON syntax in {path}, then run launch again.",
                )
            ]
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerContractError(
            [ContractIssue("wrong-type", label, f"{label} must be a JSON object", "Replace the top-level value with an object.")]
        )
    return payload


def _required_string(payload: dict[str, Any], field: str, issues: list[ContractIssue]) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        issues.append(
            ContractIssue("missing-field", field, f"{field} must be a non-empty string", f"Set {field} to the required value.")
        )
        return ""
    return value.strip()


def _string_list(payload: dict[str, Any], field: str, issues: list[ContractIssue], *, required: bool = False) -> list[str]:
    value = payload.get(field)
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        issues.append(
            ContractIssue(
                "wrong-type",
                field,
                f"{field} must be a JSON array of non-empty strings",
                f"Set {field} to an array such as [\"value\"].",
            )
        )
        return []
    return [item.strip() for item in value]


def _validate_schema_version(payload: dict[str, Any], label: str, issues: list[ContractIssue]) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        issues.append(
            ContractIssue(
                "schema-version",
                f"{label}.schema_version",
                f"{label}.schema_version must equal {SCHEMA_VERSION}",
                f"Set schema_version to {SCHEMA_VERSION}.",
            )
        )


def _resolve_repo_file(repo: Path, value: str, field: str, issues: list[ContractIssue]) -> Path | None:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(repo)
    except (OSError, ValueError):
        issues.append(
            ContractIssue(
                "path-outside-repo",
                field,
                f"{field} path is outside the policy repository: {value}",
                f"Use a path beneath {repo}.",
            )
        )
        return None
    return resolved


def _normalize_authorized_entry(raw_entry: str) -> str:
    # Plan authors commonly write `` `path/to/file.py` (new file) `` — an
    # inline-code span followed by a trailing annotation. str.strip("`")
    # only trims from the very ends of the string, so it cannot remove a
    # closing backtick that isn't the last character. Extract the inline
    # code span explicitly when present; only fall back to raw stripping
    # for plain (non-backtick-wrapped) entries.
    stripped = raw_entry.strip()
    match = re.match(r"`([^`]+)`", stripped)
    if match:
        return match.group(1).strip().rstrip(".")
    return stripped.strip("`").rstrip(".")


def _authorized(relative_path: str, entries: list[str]) -> bool:
    for raw_entry in entries:
        entry = _normalize_authorized_entry(raw_entry)
        if entry.endswith("/") and relative_path.startswith(entry):
            return True
        if any(marker in entry for marker in ("*", "?", "[")) and PurePosixPath(relative_path).full_match(entry):
            return True
        if relative_path == entry:
            return True
    return False


def validate_contract(policy: dict[str, Any], request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Return a normalized contract or raise with field-specific corrections."""
    issues: list[ContractIssue] = []
    _validate_schema_version(policy, "policy", issues)
    _validate_schema_version(request, "request", issues)

    run_id = _required_string(policy, "run_id", issues)
    slice_id = _required_string(policy, "slice_id", issues)
    plan_sha256 = _required_string(policy, "plan_sha256", issues)
    repo_text = _required_string(policy, "repo_path", issues)
    artifact_text = _required_string(policy, "worker_artifact_root", issues)
    required_tools = _string_list(policy, "required_tools", issues, required=True)
    allowed_access = _string_list(policy, "allowed_access", issues, required=True)
    allowed_roles = _string_list(policy, "allowed_roles", issues, required=True)
    authorized_files = _string_list(policy, "authorized_files", issues, required=True)

    repo = Path(repo_text).expanduser().resolve() if repo_text else Path.cwd().resolve()
    artifact_root = Path(artifact_text).expanduser().resolve() if artifact_text else run_dir.parent.resolve()
    try:
        run_dir.resolve().relative_to(artifact_root)
    except ValueError:
        issues.append(
            ContractIssue(
                "artifact-root-mismatch",
                "run_dir",
                f"run directory {run_dir} is outside policy worker_artifact_root {artifact_root}",
                "Initialize the run through worker_jobs.py with the MC-provided artifact root, then retry.",
            )
        )

    request_slice = _required_string(request, "slice_id", issues)
    request_digest = _required_string(request, "plan_sha256", issues)
    if request_slice and slice_id and request_slice != slice_id:
        issues.append(
            ContractIssue(
                "slice-mismatch",
                "slice_id",
                f"worker request targets {request_slice}, but policy authorizes {slice_id}",
                f"Rewrite the request for {slice_id}; do not reuse a request from another slice.",
            )
        )
    if request_digest and plan_sha256 and request_digest != plan_sha256:
        issues.append(
            ContractIssue(
                "plan-digest-mismatch",
                "plan_sha256",
                "worker request plan digest does not match the frozen MC policy",
                f"Copy the exact plan_sha256 from the current worker policy ({plan_sha256}).",
            )
        )

    label = _required_string(request, "label", issues)
    if label and not LABEL_RE.fullmatch(label):
        issues.append(
            ContractIssue(
                "invalid-label",
                "label",
                f"label must match <nn>-<tool>-<subtask-slug>[-rN], got {label!r}",
                "Use lowercase letters/digits and hyphens, for example 01-opencode-check-output or 01-opencode-check-output-r1.",
            )
        )
    tool = str(request.get("tool") or "").strip()
    if not tool and len(required_tools) == 1:
        tool = required_tools[0]
    if not tool:
        issues.append(
            ContractIssue("missing-field", "tool", "tool is required when policy requires multiple tools", "Choose one tool from required_tools for this request; create one request per required tool.")
        )
    elif tool not in required_tools:
        issues.append(
            ContractIssue(
                "tool-not-authorized",
                "tool",
                f"worker tool {tool!r} is not required or authorized; required tools: {', '.join(required_tools) or '(none)'}",
                "Use a required tool or stop and report that the configured tool cannot satisfy the task.",
            )
        )
    elif tool not in WORKER_PROFILES:
        issues.append(
            ContractIssue(
                "unsupported-tool",
                "tool",
                f"no deterministic worker profile exists for {tool!r}",
                "Use a supported tool or add and test its profile before authorizing it in MC.",
            )
        )

    model = str(request.get("model") or policy.get("required_model") or "default").strip() or "default"
    effort = str(request.get("effort") or policy.get("required_effort") or "default").strip() or "default"
    required_model = str(policy.get("required_model") or "default").strip() or "default"
    required_effort = str(policy.get("required_effort") or "default").strip() or "default"
    if model != required_model:
        issues.append(
            ContractIssue(
                "model-mismatch",
                "model",
                f"worker request model {model!r} does not match required model {required_model!r}",
                f"Set model to {required_model!r}; do not silently fall back.",
            )
        )
    if effort != required_effort:
        issues.append(
            ContractIssue(
                "effort-mismatch",
                "effort",
                f"worker request effort {effort!r} does not match required effort {required_effort!r}",
                f"Set effort to {required_effort!r}; do not silently fall back.",
            )
        )

    role = _required_string(request, "role", issues)
    if role and role not in WORKER_ROLES:
        issues.append(
            ContractIssue(
                "invalid-role",
                "role",
                f"role must be one of {sorted(WORKER_ROLES)}, got {role!r}",
                "Choose junior-worker or senior-worker; workers can never be orchestrators.",
            )
        )
    elif tool in WORKER_PROFILES and role not in WORKER_PROFILES[tool]["roles"]:
        issues.append(
            ContractIssue(
                "unsupported-role",
                "role",
                f"{tool} profile does not support role {role!r}; supported roles: {', '.join(WORKER_PROFILES[tool]['roles'])}",
                "Choose a supported role or a different authorized worker tool.",
            )
        )
    if role and role not in allowed_roles:
        issues.append(
            ContractIssue(
                "role-not-authorized",
                "role",
                f"role {role!r} is outside the policy allowed_roles: {', '.join(allowed_roles)}",
                "Choose an allowed role or stop and request an explicit policy change.",
            )
        )
    access = _required_string(request, "access", issues)
    if access and access not in ACCESS_MODES:
        issues.append(
            ContractIssue(
                "invalid-access",
                "access",
                f"access must be one of {sorted(ACCESS_MODES)}, got {access!r}",
                "Use read-only for analysis/review or workspace-write for an authorized edit.",
            )
        )
    elif access and access not in allowed_access:
        issues.append(
            ContractIssue(
                "access-not-authorized",
                "access",
                f"access {access!r} is not authorized by policy; allowed: {', '.join(allowed_access)}",
                "Reduce the request to an allowed access mode or stop and ask for an explicit policy change.",
            )
        )
    elif tool in WORKER_PROFILES and access not in WORKER_PROFILES[tool]["access_modes"]:
        issues.append(
            ContractIssue(
                "unsupported-access",
                "access",
                f"{tool} profile cannot mechanically enforce {access!r}; supported access: {', '.join(WORKER_PROFILES[tool]['access_modes'])}",
                "Choose a supported access mode or a different authorized worker tool; do not rely on prompt-only restrictions.",
            )
        )

    task = _required_string(request, "task", issues)
    context = str(request.get("context") or "").strip()
    expected_output = _required_string(request, "expected_output", issues)
    skills = _string_list(request, "required_skills", issues, required=True)
    files = _string_list(request, "files", issues, required=True)
    constraints = _string_list(request, "constraints", issues, required=True)
    resolved_files = [path for index, value in enumerate(files) if (path := _resolve_repo_file(repo, value, f"files[{index}]", issues))]
    if access == "read-only":
        for index, path in enumerate(resolved_files):
            if not path.exists():
                issues.append(
                    ContractIssue(
                        "missing-input",
                        f"files[{index}]",
                        f"read-only worker input does not exist: {path}",
                        "Correct the path or create the authorized input before launching the worker.",
                    )
                )

    if access == "workspace-write" and not authorized_files:
        issues.append(
            ContractIssue(
                "missing-authorized-surface",
                "authorized_files",
                "workspace-write worker requested but policy has no authorized files",
                "Use read-only access or correct the MC plan before launching an editing worker.",
            )
        )
    if access == "workspace-write":
        for index, path in enumerate(resolved_files):
            relative = path.relative_to(repo).as_posix()
            if not _authorized(relative, authorized_files):
                issues.append(
                    ContractIssue(
                        "file-not-authorized",
                        f"files[{index}]",
                        f"workspace-write request includes {relative!r}, outside the policy authorized_files",
                        "Remove the file from this request or stop and revise the frozen plan through the planning workflow.",
                    )
                )

    if issues:
        raise WorkerContractError(issues)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "slice_id": slice_id,
        "plan_sha256": plan_sha256,
        "repo_path": str(repo),
        "worker_artifact_root": str(artifact_root),
        "authorized_files": authorized_files,
        "label": label,
        "tool": tool,
        "model": model,
        "effort": effort,
        "role": role,
        "access": access,
        "task": task,
        "context": context,
        "required_skills": skills,
        "files": [str(path) for path in resolved_files],
        "constraints": constraints,
        "expected_output": expected_output,
    }


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def compile_skill_bundle(skill_name: str) -> str:
    """Compile a skill and every local Markdown resource it directly or transitively references."""
    skill_dir = _skill_root() / skill_name
    entry = skill_dir / "SKILL.md"
    if not entry.is_file():
        raise WorkerContractError(
            [
                ContractIssue(
                    "required-skill-unavailable",
                    "required_skills",
                    f"required skill {skill_name!r} is unavailable at {entry}",
                    "Install the complete skill or remove it from required_skills only if the frozen task contract does not require it.",
                )
            ]
        )
    pending = [entry.resolve()]
    seen: set[Path] = set()
    rendered: list[str] = []
    link_re = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        try:
            path.relative_to(skill_dir.resolve())
        except ValueError as exc:
            raise WorkerContractError(
                [ContractIssue("skill-resource-outside-root", "required_skills", f"skill resource escapes its root: {path}", "Fix the skill package before launching.")]
            ) from exc
        if not path.is_file():
            raise WorkerContractError(
                [ContractIssue("skill-resource-missing", "required_skills", f"required skill resource is missing: {path}", "Restore the complete skill package before launching.")]
            )
        text = path.read_text(encoding="utf-8")
        seen.add(path)
        rendered.append(f"BEGIN EMBEDDED SKILL FILE: {path}\n{text.rstrip()}\nEND EMBEDDED SKILL FILE: {path}")
        for match in link_re.finditer(text):
            target = match.group("target").split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("#"):
                continue
            candidate = (path.parent / target).resolve()
            if candidate.suffix.lower() == ".md" or candidate.name == "SKILL.md":
                pending.append(candidate)
    return "\n\n".join(rendered)


def render_worker_prompt(contract: dict[str, Any]) -> str:
    skills = contract["required_skills"]
    skill_list = "\n".join(f"  - {name}" for name in skills) if skills else "  - none"
    files = "\n".join(f"  - {path}" for path in contract["files"]) if contract["files"] else "  - none"
    constraints = [
        f"Access mode is {contract['access']}; do not exceed it.",
        "You are a delegated worker, not the orchestrator. Do not invoke ai-orchestrator, re-delegate, commit, or make final acceptance decisions.",
        *contract["constraints"],
    ]
    constraint_text = "\n".join(f"  - {item}" for item in constraints)
    embedded = "\n\n".join(compile_skill_bundle(name) for name in skills)
    prompt = f"""WORKER MODE: Delegated worker only — no ai-orchestrator skill, no re-delegation, no commits, complete locally, report blockers.

TASK: {contract['task']}

ROLE: {contract['role']}
ACCESS: {contract['access']}

REQUIRED SKILLS:
{skill_list}

FILES:
{files}

CONTEXT:
{contract['context'] or '(none)'}

CONSTRAINTS:
{constraint_text}

RETURN:
{contract['expected_output']}
"""
    if embedded:
        prompt += f"\nEMBEDDED SKILL INSTRUCTIONS:\n{embedded}\n"
    return prompt


def compose_worker_command(contract: dict[str, Any], prompt: str) -> list[str]:
    tool = contract["tool"]
    model = contract["model"]
    effort = contract["effort"]
    access = contract["access"]
    repo = contract["repo_path"]

    if tool == "opencode":
        command = ["opencode", "run", prompt]
        if model != "default":
            command.extend(["-m", model])
        if effort != "default":
            command.extend(["--variant", effort])
        command.extend(["--agent", "plan" if access == "read-only" else "build", "--auto", "--dir", repo])
        return command

    if tool == "claude":
        command = ["claude", "-p", prompt]
        if model != "default":
            command.extend(["--model", model])
        if effort != "default":
            command.extend(["--effort", effort])
        command.extend(
            ["--permission-mode", "plan" if access == "read-only" else "acceptEdits", "--output-format", "text", "--add-dir", repo]
        )
        return command

    if tool == "codex":
        command = ["codex", "exec", prompt]
        if model != "default":
            command.extend(["-m", model])
        if effort != "default":
            command.extend(["-c", f'model_reasoning_effort="{effort}"'])
        command.extend(["--sandbox", access, "--skip-git-repo-check", "-C", repo])
        return command

    if tool == "copilot":
        command = ["copilot"]
        if model != "default":
            command.extend(["--model", model])
        if effort != "default":
            command.extend(["--effort", effort])
        command.extend(["-p", prompt, "--allow-all-tools", "--autopilot", "--silent", "--add-dir", repo])
        return command

    raise WorkerContractError(
        [
            ContractIssue(
                "unsupported-tool",
                "tool",
                f"no deterministic worker profile exists for {tool!r}",
                "Use a tool supported by ai-orchestrator or add and test a deterministic profile before launching it.",
            )
        ]
    )

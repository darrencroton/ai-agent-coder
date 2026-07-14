"""Validate semantic reviewer requests and compose deterministic harness commands."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
REVIEWER_ROLE = "reviewer"
REVIEWER_ACCESS = "read-only"
LABEL_RE = re.compile(r"^\d{2}-[a-z0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*(?:-r\d+)?$")
REVIEWER_PROFILES: dict[str, dict[str, Any]] = {
    "claude": {"read_only_enforcement": "partial-plan-mode"},
    "codex": {"read_only_enforcement": "mechanical-sandbox"},
    "copilot": {"read_only_enforcement": "prompt-enforced"},
    "opencode": {
        "read_only_enforcement": "partial-edit-tools-denied",
        "effort_override": "unsupported",
    },
}
POLICY_FIELDS = {
    "schema_version",
    "run_id",
    "slice_id",
    "plan_sha256",
    "repo_path",
    "reviewer_artifact_root",
    "required_tools",
    "required_model",
    "required_effort",
    "reserved_skill_sets",
}
REQUEST_FIELDS = {
    "schema_version",
    "label",
    "slice_id",
    "plan_sha256",
    "tool",
    "model",
    "effort",
    "task",
    "context",
    "required_skills",
    "files",
    "constraints",
    "expected_output",
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


class ReviewerContractError(RuntimeError):
    """A reviewer request cannot be launched under the supplied policy."""

    def __init__(self, issues: list[ContractIssue]):
        super().__init__("; ".join(issue.message for issue in issues))
        self.issues = issues


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewerContractError(
            [ContractIssue("missing-file", label, f"{label} file does not exist: {path}", f"Create {path} as valid JSON.")]
        ) from exc
    except json.JSONDecodeError as exc:
        raise ReviewerContractError(
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
        raise ReviewerContractError(
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


def _validate_fields(
    payload: dict[str, Any], allowed: set[str], label: str, issues: list[ContractIssue]
) -> None:
    for field in sorted(set(payload) - allowed):
        issues.append(
            ContractIssue(
                "unknown-field",
                f"{label}.{field}",
                f"{label} contains unsupported field {field!r}",
                f"Remove {field!r}; schema v{SCHEMA_VERSION} does not accept retired or extension fields.",
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


def validate_contract(policy: dict[str, Any], request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Return a normalized contract or raise with field-specific corrections."""
    issues: list[ContractIssue] = []
    _validate_schema_version(policy, "policy", issues)
    _validate_schema_version(request, "request", issues)
    _validate_fields(policy, POLICY_FIELDS, "policy", issues)
    _validate_fields(request, REQUEST_FIELDS, "request", issues)

    run_id = _required_string(policy, "run_id", issues)
    slice_id = _required_string(policy, "slice_id", issues)
    plan_sha256 = _required_string(policy, "plan_sha256", issues)
    repo_text = _required_string(policy, "repo_path", issues)
    artifact_text = _required_string(policy, "reviewer_artifact_root", issues)
    required_tools = _string_list(policy, "required_tools", issues, required=True)
    required_model = _required_string(policy, "required_model", issues)
    required_effort = _required_string(policy, "required_effort", issues)
    if not required_tools:
        issues.append(
            ContractIssue(
                "empty-field",
                "required_tools",
                "required_tools must select at least one Reviewer harness",
                "Add at least one of claude, codex, copilot, or opencode, or do not create a Reviewer launch policy.",
            )
        )
    for index, required_tool in enumerate(required_tools):
        if required_tool not in REVIEWER_PROFILES:
            issues.append(
                ContractIssue(
                    "unsupported-tool",
                    f"required_tools[{index}]",
                    f"no deterministic Reviewer profile exists for {required_tool!r}",
                    "Choose claude, codex, copilot, or opencode.",
                )
            )
    if len(set(required_tools)) != len(required_tools):
        issues.append(
            ContractIssue(
                "duplicate-value",
                "required_tools",
                "required_tools contains duplicate harness names",
                "List each selected Reviewer harness once.",
            )
        )

    repo = Path(repo_text).expanduser().resolve() if repo_text else Path.cwd().resolve()
    artifact_root = Path(artifact_text).expanduser().resolve() if artifact_text else run_dir.parent.resolve()
    try:
        run_dir.resolve().relative_to(artifact_root)
    except ValueError:
        issues.append(
            ContractIssue(
                "artifact-root-mismatch",
                "run_dir",
                f"run directory {run_dir} is outside policy reviewer_artifact_root {artifact_root}",
                "Initialize the run through reviewer_jobs.py with the PM-provided artifact root, then retry.",
            )
        )

    request_slice = _required_string(request, "slice_id", issues)
    request_digest = _required_string(request, "plan_sha256", issues)
    if request_slice and slice_id and request_slice != slice_id:
        issues.append(
            ContractIssue(
                "slice-mismatch",
                "slice_id",
                f"reviewer request targets {request_slice}, but policy authorizes {slice_id}",
                f"Rewrite the request for {slice_id}; do not reuse a request from another slice.",
            )
        )
    if request_digest and plan_sha256 and request_digest != plan_sha256:
        issues.append(
            ContractIssue(
                "plan-digest-mismatch",
                "plan_sha256",
                "reviewer request plan digest does not match the frozen PM policy",
                f"Copy the exact plan_sha256 from the current reviewer policy ({plan_sha256}).",
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
    tool = _required_string(request, "tool", issues)
    if tool and tool not in required_tools:
        issues.append(
            ContractIssue(
                "tool-not-authorized",
                "tool",
                f"reviewer tool {tool!r} is not required or authorized; required tools: {', '.join(required_tools) or '(none)'}",
                "Use a required tool or stop and report that the configured tool cannot satisfy the task.",
            )
        )
    elif tool not in REVIEWER_PROFILES:
        issues.append(
            ContractIssue(
                "unsupported-tool",
                "tool",
                f"no deterministic reviewer profile exists for {tool!r}",
                "Use a supported tool or add and test its profile before authorizing it in PM.",
            )
        )

    model = _required_string(request, "model", issues)
    effort = _required_string(request, "effort", issues)
    if model != required_model:
        issues.append(
            ContractIssue(
                "model-mismatch",
                "model",
                f"reviewer request model {model!r} does not match required model {required_model!r}",
                f"Set model to {required_model!r}; do not silently fall back.",
            )
        )
    if effort != required_effort:
        issues.append(
            ContractIssue(
                "effort-mismatch",
                "effort",
                f"reviewer request effort {effort!r} does not match required effort {required_effort!r}",
                f"Set effort to {required_effort!r}; do not silently fall back.",
            )
        )

    task = _required_string(request, "task", issues)
    context = str(request.get("context") or "").strip()
    expected_output = _required_string(request, "expected_output", issues)
    skills = _string_list(request, "required_skills", issues, required=True)
    reserved_sets_raw = policy.get("reserved_skill_sets")
    if reserved_sets_raw is not None:
        # Only an absent key or an empty top-level list legitimately means
        # "no reservation". Every configured group must be a non-empty list
        # of non-empty strings so malformed members cannot disappear during
        # normalization and silently weaken the reservation.
        malformed_reserved_sets = not isinstance(reserved_sets_raw, list) or any(
            not isinstance(group, list)
            or not group
            or any(not isinstance(name, str) or not name.strip() for name in group)
            for group in reserved_sets_raw
        )
        if malformed_reserved_sets:
            issues.append(
                ContractIssue(
                    "policy-reserved-skill-sets-malformed",
                    "reserved_skill_sets",
                    f"policy reserved_skill_sets is malformed: {reserved_sets_raw!r}; expected non-empty skill-name lists such as [[\"drift-audit\"], [\"code-review\"]]",
                    "Fix the policy before launching; a malformed reserved_skill_sets must not silently disable the reservation.",
                )
            )
        elif reserved_sets_raw:
            normalized_reserved_sets = [sorted(name.strip() for name in group) for group in reserved_sets_raw]
            reserved_names_lower = {name.lower() for group in normalized_reserved_sets for name in group}
            if {name.lower() for name in skills}.intersection(reserved_names_lower) and sorted(skills) not in normalized_reserved_sets:
                allowed_text = " or ".join(json.dumps(group) for group in normalized_reserved_sets)
                issues.append(
                    ContractIssue(
                        "reserved-skill-mismatch",
                        "required_skills",
                        f"required_skills {skills!r} mixes or misnames a policy-reserved skill; policy requires exactly one of: {allowed_text}",
                        f"Set required_skills to exactly one of {allowed_text} — launch one separate request per reserved skill.",
                    )
                )
    files = _string_list(request, "files", issues, required=True)
    constraints = _string_list(request, "constraints", issues, required=True)
    resolved_files = [path for index, value in enumerate(files) if (path := _resolve_repo_file(repo, value, f"files[{index}]", issues))]
    for index, path in enumerate(resolved_files):
        if not path.exists():
            issues.append(
                ContractIssue(
                    "missing-input",
                    f"files[{index}]",
                    f"read-only reviewer input does not exist: {path}",
                    "Correct the path or create the input before launching the reviewer.",
                )
            )

    if issues:
        raise ReviewerContractError(issues)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "slice_id": slice_id,
        "plan_sha256": plan_sha256,
        "repo_path": str(repo),
        "reviewer_artifact_root": str(artifact_root),
        "label": label,
        "tool": tool,
        "model": model,
        "effort": effort,
        "role": REVIEWER_ROLE,
        "access": REVIEWER_ACCESS,
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
        raise ReviewerContractError(
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
            raise ReviewerContractError(
                [ContractIssue("skill-resource-outside-root", "required_skills", f"skill resource escapes its root: {path}", "Fix the skill package before launching.")]
            ) from exc
        if not path.is_file():
            raise ReviewerContractError(
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


def render_reviewer_prompt(contract: dict[str, Any]) -> str:
    skills = contract["required_skills"]
    skill_list = "\n".join(f"  - {name}" for name in skills) if skills else "  - none"
    files = "\n".join(f"  - {path}" for path in contract["files"]) if contract["files"] else "  - none"
    constraints = [
        (
            "Reviewer access is intrinsically read-only: you may read files and run commands that do not modify "
            "the workspace; you must not create, edit, delete, move, or format files."
        ),
        "Do not run tests or commands that may write caches, snapshots, generated files, or other workspace state.",
        "Do not perform Git, GitHub, commit, branch, staging, push, or other state-changing operations.",
        "You are a delegated Reviewer, not the Developer. Do not invoke orchestrator, re-delegate, or make final acceptance decisions.",
        *contract["constraints"],
    ]
    constraint_text = "\n".join(f"  - {item}" for item in constraints)
    embedded = "\n\n".join(compile_skill_bundle(name) for name in skills)
    prompt = f"""REVIEWER MODE: Read-only delegated Reviewer — no edits, no state-changing commands, no orchestrator skill, no re-delegation, no commits, report blockers.

TASK: {contract['task']}

ROLE: reviewer
ACCESS: read-only

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
    if any(name in {"drift-audit", "code-review"} for name in skills):
        prompt += (
            "\nMACHINE-READABLE AUDIT VERDICT (required):\n"
            "After the skill's normal report, end with exactly one final line in this form:\n"
            "PM_AUDIT_VERDICT: PASS | PASS WITH RISKS | FAIL | BLOCKED\n"
            "Use the verdict you actually reached; do not change it merely to satisfy the caller.\n"
        )
    return prompt


def compose_reviewer_command(contract: dict[str, Any], prompt: str) -> list[str]:
    tool = contract["tool"]
    model = contract["model"]
    effort = contract["effort"]
    repo = contract["repo_path"]

    if tool == "opencode":
        if effort != "default":
            raise ReviewerContractError(
                [
                    ContractIssue(
                        "unsupported-effort",
                        "effort",
                        "the tested OpenCode run command has no effort/variant flag",
                        "Set effort to 'default'; choose the configured model explicitly if a different capability level is required.",
                    )
                ]
            )
        command = ["opencode", "run", prompt]
        if model != "default":
            command.extend(["-m", model])
        command.extend(["--agent", "plan", "--auto", "--dir", repo])
        return command

    if tool == "claude":
        command = ["claude", "-p", prompt]
        if model != "default":
            command.extend(["--model", model])
        if effort != "default":
            command.extend(["--effort", effort])
        command.extend(["--permission-mode", "plan", "--output-format", "text", "--add-dir", repo])
        return command

    if tool == "codex":
        command = ["codex", "exec", prompt]
        if model != "default":
            command.extend(["-m", model])
        if effort != "default":
            command.extend(["-c", f'model_reasoning_effort="{effort}"'])
        command.extend(["--sandbox", "read-only", "--skip-git-repo-check", "-C", repo])
        return command

    if tool == "copilot":
        command = ["copilot"]
        if model != "default":
            command.extend(["--model", model])
        if effort != "default":
            command.extend(["--effort", effort])
        command.extend(["-p", prompt, "--allow-all-tools", "--autopilot", "--silent", "--add-dir", repo])
        return command

    raise ReviewerContractError(
        [
            ContractIssue(
                "unsupported-tool",
                "tool",
                f"no deterministic reviewer profile exists for {tool!r}",
                "Use a tool supported by orchestrator or add and test a deterministic profile before launching it.",
            )
        ]
    )

"""Validate semantic delegate requests and compose deterministic harness commands.

A delegate is any external harness session the orchestrator launches on the
Developer's behalf. Every delegate launches in one of two access modes:

- ``read-only``: investigation, drift-audit, code-review. No edits, no
  mutation-prone commands, no Git/GitHub mutations, no commits.
- ``read-write``: a bounded implementation task inside an explicit
  ``authorized_surface`` with explicit ``non_goals``. May create, edit, and run
  commands to complete the task. Still never performs Git/GitHub mutations or
  commits: the Developer reviews the delegate's diff and commits it, exactly
  as it would for its own edits.

Access is policy-constrained the same way tool/model/effort already are: the
policy declares which access modes are authorized (``required_access``), and
a request must select one of them. Neither role name nor an editing/commit
grant is ever accepted directly from a request.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 3
ACCESS_READ_ONLY = "read-only"
ACCESS_READ_WRITE = "read-write"
ACCESS_VALUES = {ACCESS_READ_ONLY, ACCESS_READ_WRITE}
LABEL_RE = re.compile(r"^\d{2}-[a-z0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*(?:-r\d+)?$")
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# Never appropriate to embed in a delegate prompt, in either access mode: these
# edit/commit/re-delegate/supervise on the Developer's behalf, or (scoped-
# implementation) are themselves written from the Developer's first-person
# perspective and instruct "never let a Reviewer edit files" - embedding that
# text into a read-write delegate's own prompt directly contradicts the task
# it is being asked to do.
NEVER_DELEGATE_SKILLS = {"commit", "orchestrator", "scoped-implementation", "project-manager"}
# Edit-oriented; fine for a read-write delegate, never for a read-only one.
WRITE_ONLY_SKILLS = {"code-simplifier"}

# Factual command-mechanics and enforcement notes only; not a suitability or
# capability ranking. `read_write_enforcement` describes whether the harness
# mechanically confines writes to the working directory - none of these
# profiles mechanically restrict writes to a request's specific
# `authorized_surface`. That boundary is prompt-enforced for every harness and
# is meant to be checked afterward with drift-audit against the actual diff.
DELEGATE_PROFILES: dict[str, dict[str, Any]] = {
    "claude": {
        "read_only_enforcement": "partial-plan-mode",
        "read_write_enforcement": "prompt-enforced-accept-edits",
    },
    "codex": {
        "read_only_enforcement": "mechanical-sandbox",
        "read_write_enforcement": "mechanical-sandbox-workspace-write",
    },
    "copilot": {
        "read_only_enforcement": "prompt-enforced",
        "read_write_enforcement": "prompt-enforced",
    },
    "opencode": {
        "read_only_enforcement": "partial-edit-tools-denied",
        "read_write_enforcement": "prompt-enforced-build-agent",
        "effort_override": "unsupported",
    },
    "qwen": {
        "read_only_enforcement": "prompt-enforced-sandbox-requested",
        "read_write_enforcement": "prompt-enforced-sandbox-requested",
        "effort_override": "unsupported",
    },
}
POLICY_FIELDS = {
    "schema_version",
    "run_id",
    "slice_id",
    "plan_sha256",
    "repo_path",
    "delegate_artifact_root",
    "required_tools",
    "required_model",
    "required_effort",
    "required_access",
}
REQUEST_FIELDS = {
    "schema_version",
    "label",
    "slice_id",
    "plan_sha256",
    "tool",
    "model",
    "effort",
    "access",
    "task",
    "context",
    "required_skills",
    "files",
    "authorized_surface",
    "non_goals",
    "constraints",
    "expected_output",
    "parent_label",
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


class DelegateContractError(RuntimeError):
    """A delegate request cannot be launched under the supplied policy."""

    def __init__(self, issues: list[ContractIssue]):
        super().__init__("; ".join(issue.message for issue in issues))
        self.issues = issues


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DelegateContractError(
            [ContractIssue("missing-file", label, f"{label} file does not exist: {path}", f"Create {path} as valid JSON.")]
        ) from exc
    except json.JSONDecodeError as exc:
        raise DelegateContractError(
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
        raise DelegateContractError(
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


def _optional_string(payload: dict[str, Any], field: str, issues: list[ContractIssue]) -> str | None:
    if field not in payload:
        return None
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        issues.append(
            ContractIssue(
                "wrong-type",
                field,
                f"{field} must be a non-empty string when supplied",
                f"Set {field} to a non-empty value or remove the field.",
            )
        )
        return None
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
    artifact_text = _required_string(policy, "delegate_artifact_root", issues)
    required_tools = _string_list(policy, "required_tools", issues, required=True)
    required_model = _required_string(policy, "required_model", issues)
    required_effort = _required_string(policy, "required_effort", issues)
    required_access = _string_list(policy, "required_access", issues, required=True)
    if not required_tools:
        issues.append(
            ContractIssue(
                "empty-field",
                "required_tools",
                "required_tools must select at least one delegate harness",
                "Add at least one of claude, codex, copilot, opencode, or qwen, or do not create a delegate launch policy.",
            )
        )
    for index, required_tool in enumerate(required_tools):
        if required_tool not in DELEGATE_PROFILES:
            issues.append(
                ContractIssue(
                    "unsupported-tool",
                    f"required_tools[{index}]",
                    f"no deterministic delegate profile exists for {required_tool!r}",
                    "Choose claude, codex, copilot, opencode, or qwen.",
                )
            )
    if len(set(required_tools)) != len(required_tools):
        issues.append(
            ContractIssue(
                "duplicate-value",
                "required_tools",
                "required_tools contains duplicate harness names",
                "List each selected delegate harness once.",
            )
        )
    if not required_access:
        issues.append(
            ContractIssue(
                "empty-field",
                "required_access",
                "required_access must authorize at least one access mode",
                f"Add at least one of {sorted(ACCESS_VALUES)}.",
            )
        )
    for index, value in enumerate(required_access):
        if value not in ACCESS_VALUES:
            issues.append(
                ContractIssue(
                    "unsupported-access",
                    f"required_access[{index}]",
                    f"no access mode named {value!r} exists",
                    f"Choose one of {sorted(ACCESS_VALUES)}.",
                )
            )
    if len(set(required_access)) != len(required_access):
        issues.append(
            ContractIssue(
                "duplicate-value",
                "required_access",
                "required_access contains duplicate values",
                "List each authorized access mode once.",
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
                f"run directory {run_dir} is outside policy delegate_artifact_root {artifact_root}",
                "Initialize the run through delegate_jobs.py with the PM-provided artifact root, then retry.",
            )
        )

    request_slice = _required_string(request, "slice_id", issues)
    request_digest = _required_string(request, "plan_sha256", issues)
    if request_slice and slice_id and request_slice != slice_id:
        issues.append(
            ContractIssue(
                "slice-mismatch",
                "slice_id",
                f"delegate request targets {request_slice}, but policy authorizes {slice_id}",
                f"Rewrite the request for {slice_id}; do not reuse a request from another slice.",
            )
        )
    if request_digest and plan_sha256 and request_digest != plan_sha256:
        issues.append(
            ContractIssue(
                "plan-digest-mismatch",
                "plan_sha256",
                "delegate request plan digest does not match the frozen PM policy",
                f"Copy the exact plan_sha256 from the current delegate policy ({plan_sha256}).",
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
    parent_label = _optional_string(request, "parent_label", issues)
    if parent_label and not LABEL_RE.fullmatch(parent_label):
        issues.append(
            ContractIssue(
                "invalid-label",
                "parent_label",
                f"parent_label must name a valid managed delegate label, got {parent_label!r}",
                "Use the exact label of a prior delegate in this run.",
            )
        )
    if parent_label and label and not re.search(r"-r\d+$", label):
        issues.append(
            ContractIssue(
                "continuation-label-required",
                "label",
                f"continuation label must end in -rN, got {label!r}",
                "Add an -rN suffix so the continuation is identifiable in run artifacts.",
            )
        )
    if parent_label and label and parent_label == label:
        issues.append(
            ContractIssue(
                "self-parent",
                "parent_label",
                "a continuation cannot name itself as its parent",
                "Name the exact prior delegate whose captured session should continue.",
            )
        )
    tool = _required_string(request, "tool", issues)
    if tool and tool not in required_tools:
        issues.append(
            ContractIssue(
                "tool-not-authorized",
                "tool",
                f"delegate tool {tool!r} is not required or authorized; required tools: {', '.join(required_tools) or '(none)'}",
                "Use a required tool or stop and report that the configured tool cannot satisfy the task.",
            )
        )
    elif tool not in DELEGATE_PROFILES:
        issues.append(
            ContractIssue(
                "unsupported-tool",
                "tool",
                f"no deterministic delegate profile exists for {tool!r}",
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
                f"delegate request model {model!r} does not match required model {required_model!r}",
                f"Set model to {required_model!r}; do not silently fall back.",
            )
        )
    if effort != required_effort:
        issues.append(
            ContractIssue(
                "effort-mismatch",
                "effort",
                f"delegate request effort {effort!r} does not match required effort {required_effort!r}",
                f"Set effort to {required_effort!r}; do not silently fall back.",
            )
        )

    access = _required_string(request, "access", issues)
    if access and access not in ACCESS_VALUES:
        issues.append(
            ContractIssue(
                "unsupported-access",
                "access",
                f"no access mode named {access!r} exists",
                f"Choose one of {sorted(ACCESS_VALUES)}.",
            )
        )
    elif access and access not in required_access:
        issues.append(
            ContractIssue(
                "access-not-authorized",
                "access",
                f"delegate access {access!r} is not authorized; required_access: {', '.join(required_access) or '(none)'}",
                "Use an access mode from required_access, or update the policy if the operator genuinely intends to authorize it.",
            )
        )

    task = _required_string(request, "task", issues)
    context = str(request.get("context") or "").strip()
    expected_output = _required_string(request, "expected_output", issues)
    skills = _string_list(request, "required_skills", issues, required=True)
    for index, skill in enumerate(skills):
        if not SKILL_NAME_RE.fullmatch(skill):
            issues.append(
                ContractIssue(
                    "invalid-skill-name",
                    f"required_skills[{index}]",
                    f"required_skills entries must be a canonical skill slug, got {skill!r}",
                    "Use a lowercase kebab-case skill directory name such as 'code-review'.",
                )
            )
        elif skill in NEVER_DELEGATE_SKILLS:
            issues.append(
                ContractIssue(
                    "skill-not-permitted",
                    f"required_skills[{index}]",
                    f"{skill!r} must never be embedded in a delegate prompt in either access mode",
                    "Remove this skill from required_skills; keep it with the Developer.",
                )
            )
        elif access == ACCESS_READ_ONLY and skill in WRITE_ONLY_SKILLS:
            issues.append(
                ContractIssue(
                    "skill-not-permitted-for-access",
                    f"required_skills[{index}]",
                    f"{skill!r} is a write-oriented skill and cannot be given to a read-only delegate",
                    "Remove this skill, or change access to read-write if this is really an implementation task.",
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
                    f"delegate input file does not exist: {path}",
                    "Correct the path or create the input before launching the delegate.",
                )
            )

    authorized_surface = _string_list(request, "authorized_surface", issues)
    non_goals = _string_list(request, "non_goals", issues)
    if access == ACCESS_READ_WRITE:
        if not authorized_surface:
            issues.append(
                ContractIssue(
                    "missing-field",
                    "authorized_surface",
                    "a read-write request must list at least one authorized file, function, or component",
                    "Set authorized_surface to the bounded set of files/functions/components this delegate may change.",
                )
            )
        if not non_goals:
            issues.append(
                ContractIssue(
                    "missing-field",
                    "non_goals",
                    "a read-write request must list at least one explicit non-goal",
                    "Set non_goals to what this delegate must not touch or change, even a single boundary statement.",
                )
            )
    elif access == ACCESS_READ_ONLY:
        # Check raw key presence, not parsed-list truthiness: an explicit
        # "authorized_surface": [] still means the request author put a
        # write-mode field on a read-only request and must remove it, not a
        # silently-accepted no-op.
        if "authorized_surface" in request:
            issues.append(
                ContractIssue(
                    "field-not-applicable",
                    "authorized_surface",
                    "authorized_surface only applies to access: read-write requests",
                    "Remove authorized_surface, or change access to read-write if this is really an implementation task.",
                )
            )
        if "non_goals" in request:
            issues.append(
                ContractIssue(
                    "field-not-applicable",
                    "non_goals",
                    "non_goals only applies to access: read-write requests",
                    "Remove non_goals, or change access to read-write if this is really an implementation task.",
                )
            )

    if issues:
        raise DelegateContractError(issues)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "slice_id": slice_id,
        "plan_sha256": plan_sha256,
        "repo_path": str(repo),
        "delegate_artifact_root": str(artifact_root),
        "label": label,
        "tool": tool,
        "model": model,
        "effort": effort,
        "access": access,
        "task": task,
        "context": context,
        "required_skills": skills,
        "files": [str(path) for path in resolved_files],
        "authorized_surface": authorized_surface if access == ACCESS_READ_WRITE else [],
        "non_goals": non_goals if access == ACCESS_READ_WRITE else [],
        "constraints": constraints,
        "expected_output": expected_output,
        "parent_label": parent_label,
    }


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def compile_skill_bundle(skill_name: str) -> str:
    """Compile a skill and every local Markdown resource it directly or transitively references."""
    skill_dir = _skill_root() / skill_name
    entry = skill_dir / "SKILL.md"
    if not entry.is_file():
        raise DelegateContractError(
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
            raise DelegateContractError(
                [ContractIssue("skill-resource-outside-root", "required_skills", f"skill resource escapes its root: {path}", "Fix the skill package before launching.")]
            ) from exc
        if not path.is_file():
            raise DelegateContractError(
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


def render_delegate_prompt(contract: dict[str, Any]) -> str:
    skills = contract["required_skills"]
    skill_list = "\n".join(f"  - {name}" for name in skills) if skills else "  - none"
    files = "\n".join(f"  - {path}" for path in contract["files"]) if contract["files"] else "  - none"
    access = contract["access"]

    if access == ACCESS_READ_WRITE:
        surface = contract["authorized_surface"]
        non_goals = contract["non_goals"]
        surface_text = "\n".join(f"  - {item}" for item in surface) if surface else "  - none"
        non_goals_text = "\n".join(f"  - {item}" for item in non_goals) if non_goals else "  - none"
        header = (
            "DELEGATE MODE: read-write - bounded implementation task. Stay inside the authorized "
            "surface below; no Git/GitHub mutations, no commits, no orchestrator invocation, no re-delegation."
        )
        constraints = [
            "You may create, edit, and run commands needed to implement the task, but only inside the authorized surface below.",
            "Do not create, edit, delete, move, or format any file outside the authorized surface.",
            "Do not perform Git, GitHub, commit, branch, staging, push, or other repository-history operations. The calling session reviews your diff and commits it.",
            "You are a delegated implementer, not the Developer and not the final approver. Do not invoke orchestrator, re-delegate, or make acceptance decisions.",
            "Stop and report if completing the task would require touching anything outside the authorized surface or would violate a listed non-goal.",
            *contract["constraints"],
        ]
        constraint_text = "\n".join(f"  - {item}" for item in constraints)
        embedded = "\n\n".join(compile_skill_bundle(name) for name in skills)
        prompt = f"""{header}

TASK: {contract['task']}

ACCESS: read-write

AUTHORIZED SURFACE:
{surface_text}

NON-GOALS:
{non_goals_text}

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

    constraints = [
        (
            "Read-only access is intrinsic to this mode: you may read files and run commands that do not modify "
            "the workspace; you must not create, edit, delete, move, or format files."
        ),
        "Do not run tests or commands that may write caches, snapshots, generated files, or other workspace state.",
        "Do not perform Git, GitHub, commit, branch, staging, push, or other state-changing operations.",
        "You are a delegated reviewer, not the Developer. Do not invoke orchestrator, re-delegate, or make the final acceptance decision.",
        *contract["constraints"],
    ]
    constraint_text = "\n".join(f"  - {item}" for item in constraints)
    embedded = "\n\n".join(compile_skill_bundle(name) for name in skills)
    prompt = f"""DELEGATE MODE: read-only - no edits, no state-changing commands, no orchestrator skill, no re-delegation, no commits, report blockers.

TASK: {contract['task']}

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
    return prompt


def compose_delegate_command(
    contract: dict[str, Any], prompt: str, *, resume_session_id: str | None = None
) -> list[str]:
    tool = contract["tool"]
    model = contract["model"]
    effort = contract["effort"]
    repo = contract["repo_path"]
    write = contract["access"] == ACCESS_READ_WRITE
    if resume_session_id is not None and (not isinstance(resume_session_id, str) or not resume_session_id.strip()):
        raise DelegateContractError(
            [
                ContractIssue(
                    "missing-session-id",
                    "parent_label",
                    "continuation requires a non-empty captured parent session id",
                    "Use a terminal parent with a captured session id; never fall back to a global last session.",
                )
            ]
        )

    if tool == "opencode":
        if effort != "default":
            raise DelegateContractError(
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
        if resume_session_id is not None:
            command.extend(["--session", resume_session_id])
        if model != "default":
            command.extend(["-m", model])
        command.extend(["--agent", "build" if write else "plan", "--auto", "--dir", repo])
        return command

    if tool == "qwen":
        if effort != "default":
            raise DelegateContractError(
                [
                    ContractIssue(
                        "unsupported-effort",
                        "effort",
                        "the tested Qwen Code command has no effort/variant flag",
                        "Set effort to 'default'; choose the configured model explicitly if a different capability level is required.",
                    )
                ]
            )
        command = ["qwen", "--prompt", prompt]
        if resume_session_id is not None:
            command.extend(["--resume", resume_session_id])
        if model != "default":
            command.extend(["--model", model])
        command.extend(["--sandbox", "--output-format", "text"])
        return command

    if tool == "claude":
        command = ["claude", "-p", prompt]
        if model != "default":
            command.extend(["--model", model])
        if effort != "default":
            command.extend(["--effort", effort])
        permission_mode = "acceptEdits" if write else "plan"
        if resume_session_id is not None:
            command.extend(["--resume", resume_session_id])
        command.extend(["--permission-mode", permission_mode])
        if resume_session_id is None:
            command.extend(["--session-id", str(uuid.uuid4())])
        command.extend(["--output-format", "text", "--add-dir", repo])
        return command

    if tool == "codex":
        command = (
            ["codex", "exec", "resume", resume_session_id, prompt]
            if resume_session_id is not None
            else ["codex", "exec", prompt]
        )
        if model != "default":
            command.extend(["-m", model])
        if effort != "default":
            command.extend(["-c", f'model_reasoning_effort="{effort}"'])
        sandbox = "workspace-write" if write else "read-only"
        # `codex exec resume` rejects `--sandbox` and `-C`, so both paths express the
        # sandbox through its config key instead. `approval_policy="never"` is what
        # makes that sandbox an actual boundary: under an `on-request` policy (a
        # common setting in the caller's own `~/.codex/config.toml`) the model can
        # request escalation for a sandbox-blocked command and be granted it without
        # any human present. `-C` is redundant on the resume path anyway, because the
        # launcher already starts every delegate in the policy repository.
        command.extend(["-c", f'sandbox_mode="{sandbox}"', "-c", 'approval_policy="never"', "--skip-git-repo-check"])
        if resume_session_id is None:
            command.extend(["-C", repo])
        return command

    if tool == "copilot":
        command = ["copilot"]
        if model != "default":
            command.extend(["--model", model])
        if effort != "default":
            command.extend(["--effort", effort])
        command.extend(["-p", prompt])
        if resume_session_id is not None:
            command.append(f"--resume={resume_session_id}")
        command.extend(["--allow-all-tools", "--autopilot"])
        if resume_session_id is None:
            command.extend(["--session-id", str(uuid.uuid4())])
        command.extend(["--silent", "--add-dir", repo])
        return command

    raise DelegateContractError(
        [
            ContractIssue(
                "unsupported-tool",
                "tool",
                f"no deterministic delegate profile exists for {tool!r}",
                "Use a tool supported by orchestrator or add and test a deterministic profile before launching it.",
            )
        ]
    )

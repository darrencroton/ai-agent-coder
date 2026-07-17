# Deterministic Reviewer Contract

The Developer chooses a bounded read-only review task. `scripts/reviewer_jobs.py launch` validates a semantic schema-v2 request against policy, embeds required skills, renders Reviewer-mode restrictions, composes the selected harness command, fixes the child working directory to the policy repository, and records normalized launch evidence.

The launcher always records:

```json
{
  "schema_version": 2,
  "role": "reviewer",
  "access": "read-only"
}
```

These values are launcher-owned. A request containing `role` or `access` is rejected.

## Policy

Project Manager writes `reviewer-policy.json` for a slice. A standalone Developer may write the same shape from the chosen plan or launcher prompt.

```json
{
  "schema_version": 2,
  "run_id": "20260710T045454Z",
  "slice_id": "Slice 1",
  "plan_sha256": "<exact frozen plan digest>",
  "repo_path": "/absolute/path/to/repo",
  "reviewer_artifact_root": "/absolute/path/to/slice/reviewer-runs",
  "required_tools": ["opencode"],
  "required_model": "provider/model",
  "required_effort": "default",
  "reserved_skill_sets": [],
  "before_head": "<slice starting commit>",
  "session_generation": 1,
  "repair_round": 0,
  "operational_round": 0
}
```

`reserved_skill_sets` is optional. When present, it is a list of exact allowed skill combinations, such as `[["drift-audit"], ["code-review"]]`. A malformed value fails closed. A request that mentions a reserved skill must exactly match one permitted combination.

`before_head`, `session_generation`, `repair_round`, and `operational_round` are optional PM-binding fields: Project Manager always writes them so the policy digest binds to one slice attempt and repair round, so a Reviewer PASS from before a tree-changing repair cannot satisfy a later round's gate. `operational_round` advances separately from `repair_round` on idle-stall/transient-service repair rounds, which deliberately leave `repair_round` unchanged so they don't consume the substantive repair budget — without it, the digest would not change on those rounds and stale evidence could survive them. A standalone hand-written policy may omit all four.

Schema v2 accepts only the documented policy fields. Retired Worker keys, role/access allow-lists, authorized write surfaces, and unknown extension fields are rejected.

## Request

A Reviewer request contains intent and tool selection, never permissions:

```json
{
  "schema_version": 2,
  "label": "01-opencode-check-output",
  "slice_id": "Slice 1",
  "plan_sha256": "<exact value copied from reviewer-policy.json>",
  "tool": "opencode",
  "model": "provider/model",
  "effort": "default",
  "task": "Inspect the configured validation evidence and check the output contract.",
  "context": "The Developer extracted a constant without changing output.",
  "required_skills": [],
  "files": ["pi_calculator.py", "test-results.txt"],
  "constraints": ["Cite path:line evidence.", "Report missing evidence instead of guessing."],
  "expected_output": "Start with RESULT: pass or RESULT: blocked, followed by supporting evidence."
}
```

Every listed file must resolve beneath the policy repository and already exist. Reviewers may inspect existing validation evidence but must not create files, edit files, run mutation-prone checks, perform Git/GitHub mutations, commit, or re-delegate.

Schema v1, `role`, `access`, `workspace-write`, old Worker keys, and unknown fields are rejected without aliases.

## Harness profiles

`python3 scripts/reviewer_jobs.py profiles` reports each supported tool's factual read-only enforcement. All five tools are eligible:

- Claude: partial plan-mode enforcement
- Codex: mechanical read-only sandbox
- Copilot: prompt-enforced
- OpenCode: edit tools denied by the plan agent; shell discipline prompt-enforced
- Qwen Code: prompt-enforced repository read-only behavior; launcher requests sandboxing

These descriptions are evidence, not suitability gates.

## Launch and evidence

```bash
run_dir="$(python3 scripts/reviewer_jobs.py init --prefix reviewers)"
python3 scripts/reviewer_jobs.py launch \
  --run-dir "$run_dir" \
  --policy /absolute/path/to/reviewer-policy.json \
  --request /absolute/path/to/reviewer-request.json
```

Rejected requests create `<label>-request-feedback.{json,md}` and start no process. Correct the named fields and retry with the helper; never bypass validation with a raw harness command.

Successful launches preserve the schema-v2 request, policy, rendered prompt, resolved command, normalized role/access, hashes, working directory, process status, stdout, and stderr. Required skills and linked Markdown resources must embed successfully before process creation.

When `required_skills` contains `drift-audit` or `code-review`, the Reviewer must end with exactly one `PM_AUDIT_VERDICT: PASS | PASS WITH RISKS | FAIL | BLOCKED` line. Missing or duplicate sentinels record a null verdict.

Default slices may fall back to a documented Developer self-audit. `Independent audit required: yes` requires separate validated Reviewer launches for drift audit and code review with exact `PASS` evidence.

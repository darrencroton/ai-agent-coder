# Delegate Request Templates

Delegate requests use schema v3. Copy `slice_id`, `plan_sha256`, tool, model, and effort from policy (omit `plan_sha256` when the policy sets `plan_path`; the launcher computes it). Set `access` to one of policy's `required_access` values. Do not include `role`, an unauthorized `access` value, or any external-operation approval field.

Use the smallest file list that covers the question. Every file in `files` must already exist. Require `path:line` evidence for material claims and an explicit blocker when required coverage is unavailable.

## Investigation or plan verification (`access: read-only`)

```json
{
  "schema_version": 3,
  "label": "01-<tool>-<subtask>",
  "slice_id": "<copy from policy>",
  "plan_sha256": "<copy from policy>",
  "tool": "<copy one required tool from policy>",
  "model": "<copy from policy>",
  "effort": "<copy from policy>",
  "access": "read-only",
  "task": "<specific read-only question>",
  "context": "<minimal context and required coverage>",
  "required_skills": [],
  "files": ["<repo-relative existing path>"],
  "constraints": [
    "Cite path:line evidence for every material claim.",
    "Report unchecked required coverage instead of guessing."
  ],
  "expected_output": "Return SECTION: FINDINGS, SECTION: EVIDENCE, SECTION: RISKS, and SECTION: OPEN_QUESTIONS; use - none for empty sections."
}
```

## Plan review (`access: read-only`)

Review a frozen plan before work starts. Use the investigation request with `required_skills: ["implementation-plan"]` on the first round, which is the plan format the reviewer judges against (a continuation with the same skills is not sent the bundle again). Put the plan in `files` when it lies inside the policy repository; otherwise inline the plan text in `context`, because every file must resolve beneath the policy repository. When the round has an exclusion list (findings already dispositioned, areas out of scope), carry it as a constraint:

```json
{
  "constraints": [
    "Review the plan only; do not review or propose code.",
    "Do not re-raise these dispositioned items: <exclusion list, or none>.",
    "Cite the plan section for every finding."
  ],
  "expected_output": "First line RESULT: clean or RESULT: revise. SECTION: FINDINGS lists items labelled FIX (the plan must change) or NOTE (optional), each naming the plan section it concerns and the change in one line. SECTION: DISPOSITIONS lists each earlier-round finding and whether it is resolved (continuations only). SECTION: OPEN_QUESTIONS. Use - none for an empty section."
}
```

Continue the same session with a validated continuation for later rounds; the dispositions section then answers the previous round's findings.

## Drift audit (`access: read-only`)

Use a separate request whose `required_skills` is exactly `["drift-audit"]`. Provide the frozen contract, implementation diff/evidence paths, and the exact surfaces the audit must compare.

## Code review (`access: read-only`)

Launch only after drift audit passes. Use a separate request whose `required_skills` is exactly `["code-review"]`. Provide the validated diff, relevant code/tests, and any accepted risk context.

## Validated continuation

Continue a terminal delegate only through another validated schema-v3 request in the same managed run. Copy the normal template for the access mode the new turn needs, then add `parent_label` and give `label` the same root plus a greater `-rN` suffix:

```json
{
  "schema_version": 3,
  "label": "01-claude-review-r1",
  "parent_label": "01-claude-review",
  "slice_id": "<copy from policy>",
  "plan_sha256": "<copy from policy>",
  "tool": "claude",
  "model": "<copy from policy>",
  "effort": "<copy from policy>",
  "access": "read-only",
  "task": "<the next bounded turn in the existing session>",
  "context": "<what this turn must do with the parent's context>",
  "required_skills": [],
  "files": ["<repo-relative existing path>"],
  "constraints": [
    "Cite path:line evidence for every material claim.",
    "Report unchecked required coverage instead of guessing."
  ],
  "expected_output": "<specific output contract>"
}
```

The policy must authorize this request's tool, model, effort, and access exactly as for a first launch. For a read-write continuation, use the bounded implementation fields below, including non-empty `authorized_surface` and `non_goals`. The parent must be in the same run, terminal, use the same harness, own a captured session ID, and share the advancing label lineage. Missing or unverifiable identity blocks the launch; there is no “resume last” fallback. Keep `required_skills` equal to the parent's and the resumed session is not sent the skill bundle again; change it and the bundle is embedded. See [delegate-contract.md](delegate-contract.md#validated-continuation) for the authoritative validation and lineage rules.

## Bounded implementation (`access: read-write`)

Only valid against a policy whose `required_access` includes `read-write`. Requires `authorized_surface` and `non_goals`, both non-empty; both are rejected on a `read-only` request (by key presence, not by whether the list happens to be empty).

```json
{
  "schema_version": 3,
  "label": "02-<tool>-<subtask>",
  "slice_id": "<copy from policy>",
  "plan_sha256": "<copy from policy>",
  "tool": "<copy one required tool from policy>",
  "model": "<copy from policy>",
  "effort": "<copy from policy>",
  "access": "read-write",
  "task": "<specific, bounded implementation task>",
  "context": "<the frozen slice or contract this task comes from>",
  "required_skills": [],
  "files": ["<repo-relative existing path a delegate should read for context>"],
  "authorized_surface": ["<file: specific function/behavior it may add or change>"],
  "non_goals": ["<explicitly excluded file, function, or behavior>"],
  "constraints": [
    "Run the existing test suite before reporting done.",
    "Report every file you touched, not just the ones you intended to."
  ],
  "expected_output": "List every changed file with a one-line summary, and the test command you ran with its result."
}
```

After extraction, review the delegate's diff yourself before keeping it, then run `drift-audit` against it exactly as you would for your own implementation.

## Retry

When a launch rejects, read `<label>-request-feedback.md`, correct only the named fields, and retry. A distinct non-continuation retry may use an `-rN` label without `parent_label`. To retain harness-session context, use the validated continuation template above; never bypass validation through a raw harness command.

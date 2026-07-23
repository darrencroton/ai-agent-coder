# Deterministic Delegate Contract

The Developer chooses a bounded task and an access mode. `scripts/delegate_jobs.py launch` validates a semantic schema-v3 request against policy, embeds required skills, renders access-mode restrictions into the prompt, composes the selected harness command, fixes the child working directory to the policy repository, and records normalized launch evidence.

The launcher always records the normalized `access` it validated:

```json
{
  "schema_version": 3,
  "access": "read-only"
}
```

This value is launcher-owned: a request supplies `access`, but the launcher only accepts a value the policy's `required_access` authorizes, normalizes it, and records the result. A request cannot grant itself an access mode the policy did not authorize.

## Policy

The Developer (or its launcher) writes `delegate-policy.json` for a slice from the chosen plan or launcher prompt.

```json
{
  "schema_version": 3,
  "run_id": "20260710T045454Z",
  "slice_id": "Slice 1",
  "plan_sha256": "<exact frozen plan digest>",
  "repo_path": "/absolute/path/to/repo",
  "delegate_artifact_root": "/absolute/path/to/slice/delegate-runs",
  "required_tools": ["opencode"],
  "required_model": "provider/model",
  "required_effort": "default",
  "required_access": ["read-only"]
}
```

Schema v3 accepts only the documented policy fields. Retired Worker keys, role/access allow-lists under other names, authorized write surfaces, and unknown extension fields are rejected. `required_access` is a non-empty list drawn from `read-only` and `read-write`; authorize `read-write` only for a policy that genuinely intends to let a delegate edit files.

## Request

A delegate request contains intent, tool selection, and the access mode it needs — never a role name, and never a write grant broader than the policy authorizes:

```json
{
  "schema_version": 3,
  "label": "01-opencode-check-output",
  "slice_id": "Slice 1",
  "plan_sha256": "<exact value copied from delegate-policy.json>",
  "tool": "opencode",
  "model": "provider/model",
  "effort": "default",
  "access": "read-only",
  "task": "Inspect the configured validation evidence and check the output contract.",
  "context": "The Developer extracted a constant without changing output.",
  "required_skills": [],
  "files": ["pi_calculator.py", "test-results.txt"],
  "constraints": ["Cite path:line evidence.", "Report missing evidence instead of guessing."],
  "expected_output": "Start with RESULT: pass or RESULT: blocked, followed by supporting evidence."
}
```

Every listed file must resolve beneath the policy repository and already exist. A `read-only` delegate may inspect existing validation evidence but must not create files, edit files, run mutation-prone checks, perform Git/GitHub mutations, commit, or re-delegate.

A `read-write` request additionally requires `authorized_surface` and `non_goals`:

```json
{
  "schema_version": 3,
  "label": "02-codex-add-retry-helper",
  "slice_id": "Slice 1",
  "plan_sha256": "<exact value copied from delegate-policy.json>",
  "tool": "codex",
  "model": "provider/model",
  "effort": "default",
  "access": "read-write",
  "task": "Add a retry_with_backoff helper and use it in fetch_status; add a unit test.",
  "context": "Frozen slice: Slice 1 of implementation-plan.md.",
  "required_skills": [],
  "files": ["client.py"],
  "authorized_surface": ["client.py: add retry_with_backoff()", "client.py: fetch_status() calls it", "tests/test_client.py: new test only"],
  "non_goals": ["Do not change fetch_status's return type.", "Do not touch any other function or file."],
  "constraints": ["Run the existing test suite before reporting done.", "Report every file you touched."],
  "expected_output": "List every file changed, a one-line summary per file, and the test command you ran with its result."
}
```

A `read-write` delegate may create, edit, and run commands needed to complete the task, but only inside `authorized_surface`; it must still never perform Git/GitHub mutations, commit, or re-delegate, and must stop and report rather than touch anything outside the surface or contradict a listed non-goal. Both `authorized_surface` and `non_goals` must be non-empty on a `read-write` request; both are rejected outright (by key presence, not by whether the list happens to be empty) on a `read-only` request — they only mean something when a delegate can write.

`required_skills` is deliberately empty above: the task's own `authorized_surface` and `non_goals` already state the bounded surface, so a read-write request rarely needs an embedded skill. `commit`, `orchestrator`, `project-manager`, and `scoped-implementation` can never be embedded in any delegate prompt regardless of access mode — the first three edit, mutate, or supervise on the Developer's behalf, and `scoped-implementation`'s own text is written for the Developer's first-person use and instructs "never let a Reviewer edit files," which would directly contradict a read-write delegate's task if embedded in its prompt. `code-simplifier` is the one edit-oriented skill still permitted, and only for a read-write request.

Schema v1/v2, `role`, `workspace-write` as an access value, old Worker keys, and unknown fields are rejected without aliases.

## Validated continuation

A continuation is a new schema-v3 request that names a prior delegate in the same managed run. Add `parent_label` and advance the parent's shared label root with an `-rN` suffix:

```json
{
  "schema_version": 3,
  "label": "01-claude-review-r1",
  "parent_label": "01-claude-review",
  "slice_id": "Slice 1",
  "plan_sha256": "<exact value copied from delegate-policy.json>",
  "tool": "claude",
  "model": "opus",
  "effort": "medium",
  "access": "read-write",
  "task": "Resolve the review findings while preserving the frozen contract.",
  "context": "Continue the completed read-only review in the same Claude session.",
  "required_skills": [],
  "files": ["client.py", "tests/test_client.py"],
  "authorized_surface": ["client.py: reviewed fix only", "tests/test_client.py: regression test only"],
  "non_goals": ["Do not change public return types.", "Do not touch any other file."],
  "constraints": ["Address only findings supported by repository evidence.", "Report every file touched."],
  "expected_output": "List fixes, changed files, and validation results."
}
```

`parent_label` activates continuation; an `-rN` label without `parent_label` remains a normal first launch and can still record a retry as before. The continuation request is validated from scratch against the supplied policy. It may change access mode only when that policy authorizes the new mode, and a read-write continuation still requires non-empty `authorized_surface` and `non_goals`. It never inherits permission to commit, mutate Git/GitHub state, re-delegate, or make final acceptance decisions.

Before launching, the helper verifies that the named parent:

- exists in the same managed run
- is terminal (`completed`, `failed`, or `cancelled`)
- uses the same harness as the new request
- owns a non-empty captured `session_id`
- shares the new label's root, with a new `rN` number greater than the parent's

Failure of any check rejects the request with structured feedback and starts no process. In particular, an unavailable parent ID produces `continuation-session-unavailable`; the helper never substitutes a global “resume last” session. A successful continuation uses the explicit parent ID in the harness-specific resume command and records `parent_label`, `parent_session_id`, and `continuation_index` in the new manifest entry. `status` and `activity` expose the continued session ID like any other managed launch.

## Harness profiles

`python3 scripts/delegate_jobs.py profiles` reports each supported tool's factual enforcement for both access modes. All five tools are eligible for either:

- Claude: partial plan-mode enforcement read-only; `acceptEdits` auto-approves file edits read-write
- Codex: mechanical read-only sandbox; mechanical `workspace-write` sandbox confines writes to the working directory and `/tmp`
- Copilot: prompt-enforced either way; the composed command is identical for both access modes
- OpenCode: edit tools denied by the plan agent read-only; the build agent grants unrestricted tool permissions read-write, prompt-enforced
- Qwen Code: prompt-enforced repository behavior either way; the composed command is identical for both access modes, with sandboxing requested but not guaranteed

These descriptions are evidence, not suitability gates. No profile mechanically confines a read-write delegate to its request's specific `authorized_surface` — that boundary is prompt-enforced for every harness and is meant to be checked afterward with drift-audit against the actual diff.

## Launch and evidence

```bash
run_dir="$(python3 scripts/delegate_jobs.py init --prefix delegates)"
python3 scripts/delegate_jobs.py launch \
  --run-dir "$run_dir" \
  --policy /absolute/path/to/delegate-policy.json \
  --request /absolute/path/to/delegate-request.json
```

Rejected requests create `<label>-request-feedback.{json,md}` and start no process. Correct the named fields and retry with the helper; never bypass validation with a raw harness command.

Successful launches preserve the schema-v3 request, policy, rendered prompt, resolved command, normalized access (plus `authorized_surface`/`non_goals` for a read-write launch), hashes, working directory, process status, stdout, and stderr. Required skills and linked Markdown resources must embed successfully before process creation. The caller reads the delegate's report (or diff) directly; a read-only delegate skill's own verdict vocabulary (for example `PASS WITH RISKS`) appears in the report body, not in any machine-captured sentinel.

Every manifest entry carries `session_id`, using a launch-bound value where ownership can be established and `null` otherwise. `status` and `activity` expose that ID. Activity is source-labelled for all five harnesses: owned transcript/session-store evidence is preferred where available, while captured output is the universal fallback so a live process is never silently unmonitored.

Default slices may fall back to a documented Developer self-audit. A plan that asks for independent review (`Independent audit required: yes`) deserves separate read-only delegate launches for drift audit and code review; if none can be launched, stop and report rather than self-audit.

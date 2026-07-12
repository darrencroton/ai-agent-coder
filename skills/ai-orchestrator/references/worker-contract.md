# Deterministic Worker Contract

The orchestrator chooses worker intent. It does not construct harness commands. `scripts/worker_jobs.py launch` validates a semantic JSON request against an authoritative policy, embeds the worker-mode instructions and complete required-skill Markdown bundles, composes the selected harness command from a tested profile, forces the child process into the policy repository, and records the resolved launch evidence.

Inspect machine-readable role/access capabilities with `python3 scripts/worker_jobs.py profiles`. A request outside a profile's tested capabilities is rejected before any process starts.

## Policy

Master Controller writes `worker-policy.json` for an MC slice. A standalone ai-orchestrator may write an equivalent policy from the user's authorization and frozen contract. The policy is the authority for run/slice identity, the frozen plan digest, repository, worker artifact root, required tools, model, effort, permitted roles and access modes, and authorized files. When the plan explicitly requires a read-only worker, MC restricts `allowed_access` to `read-only`. Under MC, worker delegation is a degradable preference by default — a locally self-audited slice is a valid pass and MC only reports delegation — except on a slice marked `Independent audit required: yes`, where every tool in `required_tools` must produce its own validated successful run before the slice can pass.

```json
{
  "schema_version": 1,
  "run_id": "20260710T045454Z",
  "slice_id": "Slice 1",
  "plan_sha256": "<exact frozen plan digest>",
  "repo_path": "/absolute/path/to/repo",
  "worker_artifact_root": "/absolute/path/to/slice/worker-runs",
  "required_tools": ["opencode"],
  "required_model": "macstudio/qwen/qwen3.6-35b-a3b-q8",
  "required_effort": "default",
  "allowed_access": ["read-only", "workspace-write"],
  "allowed_roles": ["junior-worker", "senior-worker"],
  "authorized_files": ["pi_calculator.py"]
}
```

## Access Modes

Access modes are contract semantics defined here, not by any harness flag:

- `read-only`: the worker may read files and run commands that do not modify the workspace (validation, tests, checks). It must not create, edit, or delete files.
- `workspace-write`: the worker may edit only the files listed in the request, which must fall inside the policy's authorized files. It still never commits.

Harness profiles map each mode to the closest tested launch configuration, and the mechanical strictness of that configuration varies by harness — for example, OpenCode's plan agent mechanically denies only edit tools, leaving command execution constrained by prompt (see [opencode.md](opencode.md)), while Codex's `--sandbox read-only` is an OS-level sandbox. Some worker models also interpret a harness's read-only mode more conservatively than this contract and refuse to run any command; treat such a refusal as a failed delegation and retry with a corrected request through the launcher, never by composing a raw harness command. The mutation backstop is not the harness flag: under MC, the recomputed file-authorization gate and the clean post-commit worktree check catch workspace changes regardless of what a worker or harness did.

## Request

The orchestrator writes one semantic request per worker. Copy `slice_id` and `plan_sha256` exactly from the current policy. Use `read-only` for analysis, review, validation, or planning. Use `workspace-write` only for a bounded edit authorized by the frozen contract.

```json
{
  "schema_version": 1,
  "label": "01-opencode-format-check",
  "slice_id": "Slice 1",
  "plan_sha256": "<exact value copied from worker-policy.json>",
  "tool": "opencode",
  "model": "macstudio/qwen/qwen3.6-35b-a3b-q8",
  "effort": "default",
  "role": "junior-worker",
  "access": "read-only",
  "task": "Run the configured validation and check the output contract.",
  "context": "The slice extracts a constant without changing output.",
  "required_skills": [],
  "files": ["pi_calculator.py"],
  "constraints": ["Do not edit files.", "Report the actual output."],
  "expected_output": "Start with RESULT: pass or RESULT: blocked, followed by the actual output."
}
```

## Launch

```bash
run_dir="$(python3 scripts/worker_jobs.py init --prefix workers)"
python3 scripts/worker_jobs.py launch \
  --run-dir "$run_dir" \
  --policy /absolute/path/to/worker-policy.json \
  --request /absolute/path/to/worker-request.json
```

Do not bypass a rejected request by invoking `claude`, `codex`, `copilot`, or `opencode` directly. Read `<label>-request-feedback.md`, correct only the named fields, and launch again. Feedback is deliberately specific so even a weak orchestrator can self-correct without redoing the slice.

Successful launches preserve request, policy, rendered prompt, resolved command, normalized contract, enforced working directory, process status, stdout, and stderr in the worker run directory. A named required skill or linked Markdown resource that cannot be embedded rejects the launch before process creation. Under MC, a slice marked `Independent audit required: yes` requires this validated launch evidence, an exact match to MC's stored policy snapshot, and successful completion for every available worker tool; a default slice reports delegation but accepts a local self-audit.

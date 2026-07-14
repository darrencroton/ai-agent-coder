# Harness Adapter Contract

PM core must not hardcode one AI harness. Each adapter describes how to start, observe, supervise, and stop a tmux-backed developer session in a target repo.

## Implemented Adapter Responsibilities

The current `TmuxHarnessAdapter` provides these concrete methods:

- `name`: stable harness identifier such as `codex`.
- `preflight`: command or function that checks local availability without starting a run.
- `build_shell_command`: returns the shell command used inside tmux, including PM environment variables for the run state, plan path, slice id, and slice artifact directory.
- `start`: starts a fresh tmux session in the target repo with the built shell command.
- `send_prompt`: injects the rendered developer prompt into the tmux session.
- `send_literal`: send short model-supervised operational text literally to the current tmux session and submit it without shell evaluation.
- `capture`: writes transcript or pane output to the slice artifact directory.
- `detect_activity`: reports whether the session is still active or idle as `{"running": bool, "active": bool, "capture": string}`.
- `detect_hard_prompt`: reports whether the visible pane appears to contain a trust, approval, credential, permission, or external-side-effect prompt that must block unattended send, wait, pause, retry, or resume actions.
- `request_stop`: asks the harness to stop gracefully.
- `force_stop`: terminates the tmux session after a terminal stop/finalize decision or failed graceful stop.
- `session_exists` and `sessions_with_prefix`: support liveness checks and stale-session cleanup.

Structured result detection is owned by PM's runner and command layer by checking `developer-result.json` in the current slice artifact directory, not by a separate adapter method.

## Primitive-Level Responsibilities

Model-supervised PM commands may compose these adapter responsibilities into primitives:

- `observe`: capture compact pane/transcript/process/result/git evidence without finalizing gates.
- `send`: send a literal continuation or operational instruction only to the current slice's recorded session, refusing hard prompts.
- `wait`: observe for a bounded duration, appending JSONL observation events, returning early on result, process exit, hard-stop prompt, or max wait.
- `pause-until`: persist pause state, observe until an absolute timestamp plus buffer, and refuse hard-stop conditions.
- `start-slice`: launch a slice and return control after recording `current_slice.before_head`.
- `finalize-slice`: capture final evidence and run deterministic gates using persisted `before_head`.
- `stop-with-evidence`: preserve pane/transcript/git evidence and record a structured stop reason without accepting the slice.

## Required Artifacts

For each slice, the adapter must allow PM to capture:

- `prompt.md`
- `transcript.txt` or `pane-capture.txt`
- `git-status-before.txt`
- `git-status-after.txt`
- `git-diff.patch`
- `developer-result.json`

## Tmux Requirements

- Every slice starts in a fresh tmux session.
- Session names must include the run id and slice id.
- The working directory must be the target repo/worktree.
- The harness receives fixed PM environment variables for the slice: `PM_SLICE_ARTIFACT_DIR`, `PM_PLAN_PATH`, `PM_SLICE_ID`, `PM_RESULT_SCHEMA_PATH`, `PM_REVIEWER_JOBS_PATH`, `PM_REVIEWER_POLICY_PATH`, `PM_REVIEWER_ARTIFACT_ROOT`, `ORCHESTRATOR_ARTIFACT_ROOT`, `PM_SLICE_TMP_DIR`, `TMPDIR`, and `PM_TOOL_HOME_ROOT`. Controller state is deliberately not exposed. Tool home redirects are Reviewer-only: `COPILOT_HOME` / `CODEX_HOME` are set only when that tool is a required Reviewer and not the Developer harness itself, so a Developer always keeps its real config and session state.
- PM records activity checks as JSON lines with `checked_at`, `running`, and `active` fields.
- PM must preserve live pane output while polling and must also attempt a final capture before and after stop.
- Deterministic batch execution must close the session after completion or terminal timeout.
- Model-supervised execution may keep a live session open through a classified pause or bounded wait. It must close or reap the session only after evidence is captured and the PM model or deterministic gate has chosen stop/finalize/restart.

## Observation And Send Safety

Adapters must support evidence capture without changing harness state. Observation should be compact enough for repeated PM model review and must preserve full pane or transcript artifacts on disk.

Literal sends must:

- target only the current slice's recorded tmux session
- send text literally, not through shell evaluation
- reuse the harness prompt-submission discipline, including settle and robust submit behavior for TUIs that need more than a single Enter
- refuse when a trust, approval, credential, permission, or external-side-effect prompt is visible
- record the sent text, timestamp, reason, and evidence pointer as an operational event

## Harness Profiles

PM keeps one mechanical profile per tool, not one profile per role combination. The launch command is composed from:

- the selected developer harness, for example `codex` or `claude`;
- runtime requirements, such as reviewer tools being used;
- run policy, such as `commit_required=true`.

This keeps tool-specific instructions together while avoiding many partially tested combinations. For example, the Codex profile adds sandbox network access only when reviewer tools are requested, and adds scoped git-directory access only when commits are required.

Profile composition also owns supported developer model and effort overrides. For example, a Claude run that requests a specific model must be composed by PM as `claude --permission-mode auto --model <model> --session-id <generated-id>` so model selection does not bypass transcript capture or other profile-managed launch requirements. A Codex run that requests model and effort is composed from the same profile table as `-m <model>` plus `-c model_reasoning_effort="<effort>"`.

### Model identity verification

An adapter must not assume that a syntactically plausible model override was honored. Harness installations differ in how they resolve model names, and some require provider-qualified identifiers. When the configured harness exposes a queryable model inventory, PM must require an exact match for the requested identifier during preflight and again before launch; an alias, unqualified identifier, typo, failed inventory query, or unparseable identity fails closed.

Where the interactive harness exposes its resolved model in a stable ready-state display, the adapter must also compare that observed display identity with metadata returned by the inventory before injecting the slice prompt. A mismatch or missing expected identity is treated as a possible silent fallback and blocks launch. This runtime check is harness-specific operational evidence, not acceptance evidence. Profiles without a queryable inventory or stable resolved-identity display must document that coverage gap and must not claim model identity was mechanically verified.

PM records requested, catalog-resolved, and display identity evidence separately. Tests use mocked inventory responses and recorded pane fixtures so regression coverage is deterministic and does not depend on a locally installed or changing harness binary.

The first slice freezes the complete run launch configuration; the active slice snapshots the same values. Every later slice and automatic fresh-session recovery composes from that contract rather than silently reverting to harness or reviewer defaults when the controlling invocation omits model/profile flags. Conflicting explicit values fail closed. Identity evidence is refreshed for every launched slice/session and tagged with its slice instead of being reused as an unmarked cache.

Reviewer launch is a semantic contract, not a second launch subsystem inside PM. PM writes schema-v2 `reviewer-policy.json` with slice identity, frozen plan digest, repository, required tools/model/effort, and artifact root, then stores its digest and normalized content in slice state. The Developer writes task intent and context files in one schema-v2 `reviewer-request.json` per required tool; requests do not choose role or access. `orchestrator` validates the request, embeds complete Reviewer/skill instructions, composes the tested harness command, forces the child working directory to the policy repository, and records normalized `role: reviewer` / `access: read-only` launch evidence. PM verifies those constants directly rather than consulting policy allow-lists. For opt-in audits, the helper records the exact machine-readable verdict and PM requires `PASS` from both skills.

Codex, Claude, Copilot, and OpenCode are equally eligible for Developer or Reviewer selection. Profiles record mechanics only and contain no role allow-list, capability tier, ranking, or suitability gate. Copilot and OpenCode both accept the same tmux paste-buffer-plus-double-Enter prompt injection as Codex/Claude and were directly observed reaching a stable ready state before first send; Copilot's directory-trust dialog text was also confirmed to match `TRUST_PROMPT_MARKERS`. Enforcement-strength differences remain factual notes rather than eligibility restrictions, and the user selects the tool/model through the plan or launcher.

Two residual coverage gaps apply to Copilot and OpenCode alike, in addition to whatever gaps already existed for Codex/Claude: (1) only the directory-trust prompt was directly triggered and confirmed for each; other hard-stop prompt classes (credential, permission-denial, external side effect) rely on the same generic keyword markers used for every harness, untested against these two CLIs' actual prompt wording. (2) Extended real-world runs (long multi-step slices, concurrent reviewers sharing a tool's local state/session store) have not been exercised — validation here covered readiness detection and one short end-to-end slice per harness. OpenCode now has query-backed exact model-id validation and a pre-prompt display-name comparison; equivalent positive identity verification remains a documented gap for profiles without both signals.

## Failure Semantics

PM should record stable failure reasons in run state and artifacts instead of exposing opaque process errors when possible. Some reasons come from adapter operations, while others come from PM primitives that compose adapter evidence:

- `missing-harness`
- `missing-tmux`
- `start-failed`
- `prompt-injection-failed`
- `hard-stop-prompt`
- `pause-budget-exhausted`
- `capture-failed`
- `result-missing`
- `stop-failed`
- `timeout`

`timeout` is terminal for deterministic batch execution when the result file never appears before the configured deadline. In model-supervised operation, a bounded `wait` timeout is an observation result, not an acceptance or failure verdict by itself; the PM model must then choose a safe next primitive such as another wait, `pause-until`, `send`, `finalize-slice`, or `stop-with-evidence`.

PM records terminal failures in run state and stops rather than retrying indefinitely. Model-supervised waits and pauses are not acceptance states; they preserve evidence and return control for a later observe, send, finalize, or stop decision. Acceptance still requires `developer-result.json` plus deterministic validation, authorization, drift audit, code review, commit, and clean-worktree gates.

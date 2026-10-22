# Implementation Plan: PM Campaign Mode

**Status:** frozen, 2026-10-23, ten slices, after four rounds of independent plan review (Codex `gpt-6.1-sol` high and Claude `claude-opus-5-5` high, via `orchestrator`; every finding applied); the third and fourth rounds followed the owner's revision of D1, D11, D16 and the question channel · Source: a design brief the owner holds outside this repository (the first campaign's product repository); this plan is self-contained. · Base: `main` at `77f2941` · Every `path:line` below was verified against that commit.

## Goal

Extend the Project Manager (PM) into a *campaign mode* that turns an owner-approved goal into a validated outcome across many small plans and days, with bounded human attention. A PM seat in campaign mode, the Campaign Manager (CM), holds a dossier whose owner-approved `brief.md` is its only planning authority; it writes small node plans, launches an ordinary child PM per plan in its own worktree and tmux session, answers the child's one-line questions, judges the child's run from its report and the node diff, merges accepted change nodes one at a time, and repeats until every target in the brief passes. The toolkit gains exactly what that loop cannot do safely by convention: protected paths that no plan surface, grant or commit can reach; a file-based question channel, used by a Developer to ask its PM and by a child PM to ask the CM; and four commands that launch, watch, answer and stop a child PM in its own tmux session. Everything else is a reference document, a dossier template, a few sentences in two skills and one deliberate VISION revision. Every existing workflow (Mode A, Mode B on a single plan, orchestrator delegates) keeps working as today, with two deliberate changes to existing runs: the Developer question channel (D18), and floor fact 5 seeing both sides of a rename (D17); and `project-manager`'s SKILL.md rule on plan defects is aligned to the Mode B launcher's delegated-decision clause (D16), a deliberate loosening of the PM contract the owner chose.

## Non-Goals

- No new skill, seat toolkit or state store: the CM is a `project-manager` seat, the dossier and git are its state, and there is no parent-side signed state.
- No parallel scheduler and no parallel runs in the first tests. Parallel execution is a convention (launch a second child before waiting), enabled only once the serial loop is boring.
- No `campaign` subcommand family beyond the four child commands (`launch-child`, `wait-child`, `answer-child`, `stop-child`), and no `init --campaign`, node records or merge records inside `pm.py`.
- No Developer or Reviewer prompt rendering of protected paths: `check-plan --policy` keeps node surfaces disjoint, and floor fact 5 is the backstop.
- No change to `review.py`, `profiles.py`, `orchestrator`, `code-review`, `drift-audit`, `lint`, `commit` or `handoff`.
- No automatic kill of a child on a deadline: the CM decides when a child stops (D13).
- No domain content: nothing in this repository names a particular product, model package or instrument, and no product's protected-paths policy file is part of this plan.
- No domain-specific gates (for example science gates for work without a reference implementation) and no calibration machinery; a campaign's instruments are built by its own nodes.
- No change to an upstream repository or remote that the brief does not permit; the CM may draft a patch and ask, never apply one.
- Not adversary-proof, in the same sense as the rest of the repository: it defends against drift, corner-cutting and overconfidence, not a determined fabricator.

## What Changes

| Where | Change | Size | New or adapted |
|---|---|---|---|
| `pm_lib/plan.py`, `pm_lib/cli.py`, `tests/test_plan.py` | Protected-paths policy file loader (JSON, self-protecting); plan-time overlap refusal; `check-plan --policy` | ~120 lines + tests | Adapted (Slice 1) |
| `pm_lib/state.py`, `pm_lib/slice_ops.py`, `pm_lib/floor.py`, `pm_lib/cli.py`, `pm_lib/plan.py`, `pm_lib/git_ops.py`, four test modules, `references/run-state.md`, `README.md`, `SKILL.md` | `init --policy` freezes protected paths into signed state; `grant` refuses them; floor fact 5 fails on them; changed-file lists report both sides of a rename | ~90 lines + tests + docs | Adapted (Slice 2) |
| `pm_lib/slice_ops.py`, `pm_lib/cli.py`, `pm_lib/prompts.py`, `references/developer-prompt.md`, `SKILL.md`, `README.md`, `references/run-state.md`, two test modules | Developer question channel: `question.md`, `observe` returns on it, `pm answer` delivers a numbered answer through a one-line pointer | ~120 lines + tests + docs | New (Slice 3) |
| `pm_lib/campaign.py` (new), `pm_lib/cli.py`, `pm_lib/sessions.py`, `pm_lib/prompts.py`, `references/developer-prompt.md`, `tests/test_campaign.py` (new), `README.md`, `references/run-state.md` | `launch-child`, `wait-child`, `answer-child`, `stop-child`: a child PM in its own tmux session on a private socket, with the CM question channel and a completion file | ~250 lines + tests | New (Slice 4) |
| `skills/implementation-plan/SKILL.md` | Optional campaign-child paragraph in the Mode B launcher; small plans are normal; protected-path surface rule | ~20 lines | Adapted (Slice 5) |
| `skills/project-manager/SKILL.md` | *Reading a defective plan* and the Charter aligned to the Mode B launcher's delegated-decision rule | ~10 lines | Adapted (Slice 6) |
| `docs/VISION.md` | The deliberate campaign-mode revision in *The Roles* and *Non-Goals* | ~5 lines | Adapted (Slice 7, approval-gated) |
| `references/campaign-mode.md` (new), `references/dossier-template.md` (new), PM `SKILL.md`, PM `README.md` | The CM's operating contract and the campaign launcher (its only copy); dossier templates; pointers | ~300 lines of Markdown | New (Slice 8) |
| PM `README.md`, `references/campaign-mode.md` | Stage 1: a model-free trial in the README and the first-campaign test in the reference | ~55 lines | New sections (Slice 9) |
| `CONTRIBUTING.md`, `CHANGELOG.md`, `README.md` (root) | Source-of-truth rows; changelog entry; glossary, level table and skill row | ~25 lines | Adapted (Slice 10) |
| `review.py`, `profiles.py`, `orchestrator`, `code-review`, `drift-audit`, `lint`, `commit`, `handoff` | No change | — | Reused as is |

## Decisions

The lead made these decisions from the source brief and the code at `77f2941`, and the owner revised D1, D7, D11, D13, D14 and D16. The owner can veto any of them before `init` freezes this plan.

- **D1. Child launch vehicle.** Recommendation: the child PM runs as an interactive session in tmux, launched exactly as PM launches a Developer (`sessions.start_session` → `sessions.wait_until_ready` → `sessions.send_prompt` with a one-line pointer), on the child's private socket. Grounds: the repository's own July record (the archived headless-Developer experiment and the note "Honest Opinion Developer in tmux vs Called Like the Reviewer") found tmux superior in practice for the long-running mutating seat: the supervisor reads the pane and catches stalls and dialogs, the human can attach, watch and occasionally prompt, and a write-back channel exists; `claude -p --output-format text` prints only the final message, so a headless child would be invisible until exit. Rejected: the one-shot subprocess (a clean exit signal, but blind mid-run and with no write-back channel).
- **D2. Policy file format.** Recommendation: the protected-paths policy file is JSON, `{"schema": 1, "protected_paths": [...]}`, with entries in authorized-surface entry syntax; the toolkit reads only `protected_paths` (absent means `[]`), and other keys are ignored by the toolkit. Rejected: Markdown (brittle prose parsing) and reading protections from `brief.md`.
- **D3. Enforcement point.** Recommendation: protected paths are enforced inside floor fact 5 (changed files outside the effective surface *or* matching a protected entry), keeping seven facts. Rejected: an eighth floor fact.
- **D4. Plan-time overlap rule.** Recommendation: compare literal prefixes (plain path itself; `dir/` gives `dir`; a glob gives its segments before the first wildcard segment, possibly empty) and refuse when either is a segment-wise prefix of, or equal to, the other; a protected entry with an empty literal prefix is refused at load; the floor's check on actual files stays exact, and `grant` checks the exact path. Rejected: exact glob-intersection logic (complex, and over-refusal is fixed by writing a disjoint literal directory).
- **D5. Self-protection.** Recommendation: a protected-paths policy file inside the repository is appended to its own protected entries at load time, in `plan.load_policy(path, repo=...)`, so `check-plan --policy --repo` and `init --policy` agree. Rejected: leaving the policy editable by a later node.
- **D6. Command placement.** Recommendation: the four child commands (`launch-child`, `wait-child`, `answer-child`, `stop-child`) are `pm.py` subcommands implemented in a new `pm_lib/campaign.py`. Rejected: a separate script with its own entrypoint and version check.
- **D7. No change to the reviewer command table.** Recommendation: the child launch composes its command with the existing interactive Developer profile table (`profiles.compose_command`, `profiles.py:94-119`, full autonomy flags) or `--harness-command`; `review.py` and `profiles.py` are untouched. Rejected: single-sourcing a one-shot table with a full-autonomy form (needed only by a headless child, which D1 rejects).
- **D8. Campaign launcher home.** Recommendation: `skills/project-manager/references/campaign-mode.md` holds the campaign launcher, its only copy, and the Mode B launcher gains an optional campaign-child paragraph. Rejected: putting the campaign launcher in `implementation-plan`'s SKILL.md (a campaign starts from a dossier, not a plan).
- **D9. Stage 1.** Recommendation: the real-model scratch campaign is run by the owner in a fresh CM session after this plan; Slice 9 delivers a model-free README trial, which the Developer runs, and the first-campaign test in the reference. This is an assumption the owner can override. Rejected: running the real-model campaign inside a Mode B Developer session (three nested model seats and a human go/no-go do not fit a Developer's tool timeouts).
- **D10. VISION gate.** Recommendation: the VISION slice (Slice 7) is approval-gated (`Approval needed before implementation: yes`) and lands before the campaign reference it governs. Rejected: an unattended VISION edit, or the revision landing after the authority it grants.
- **D11. Child handoff.** Recommendation: a child PM keeps HANDOFF.md exactly as today (git-ignored in its worktree): the PM's private register of owner instructions, lessons and deferred issues, which `notes.md` (Developer-facing), assessments and the report do not hold. The CM reads the child's run report, assessments and the handoff's residuals, archives it with the node in the order Slice 8 gives (`stop-child` first, then HANDOFF.md, `.pm/campaign/`, `.pm/runs/<id>/` and the authoritative state root into `validation/<node>/`, then `git worktree remove`), and summarises what matters into `work.md` or `evidence.md`; the CM has no HANDOFF.md of its own (`work.md` *Now* is its handoff). A stopped child is still never resumed; the CM plans a follow-up node from the integration head, or from the stopped branch head only when that head carries no protected-path change (Slice 8). Rejected: a child without a handoff (loses the register the CM needs to judge the node).
- **D12. Plan file.** Recommendation: `docs/implementation-plan-pm-campaign-mode.md`, committed before `init` and archived after the run, per repository convention. Rejected: an uncommitted plan outside the repository.
- **D13. No automatic kill on a deadline.** Recommendation: `wait-child --wait N` returns with no signal after N seconds, like `observe --wait`, and the CM decides; `stop-child` is the explicit kill (`tmux -L <socket> kill-server`), which also prints the reviewer process groups the child's `run.json` records (tokenless read) for a by-hand check and never signals them. Residual: reviewers the child commissioned run in their own process groups (`review.py:461-471`, `start_new_session=True`), outside the child's tmux server, so `kill-server` does not reach them and a tokenless CM cannot reap them. Rejected: a default timeout that kills the child (a supervisor decision taken by a clock).
- **D14. Child tmux socket.** Recommendation: `--socket NAME`, default `pm-child-<branch with every character outside [A-Za-z0-9_-] replaced by ->-<6 hex characters from secrets.token_hex(3)>` (tmux rejects `.` and `:` in a name), so two nodes or two repositories never share a server; an explicit socket must be non-blank with no whitespace and no `/` (a blank one would fall back to the caller's default server, which `kill-server` would then destroy). The socket names the private tmux server that hosts the child PM session and every Developer session that child launches (`PM_TMUX_SOCKET` is set in the child's environment), so one `kill-server` cleans the whole node and a human attaches to one server to watch both layers. Rejected: the caller's default tmux server, or a purely branch-derived name.
- **D15. Scope.** Recommendation: the non-goals above (no prompt rendering of protected paths, no parent-side signed state, no parallel scheduler, no new skill, no subcommand family beyond the four child commands, no change to the reused skills). Rejected: building any of them before a test shows the need.
- **D16. The launcher's delegated-decision clause stays; SKILL.md is aligned to it.** Recommendation: the Mode B launcher's `Plan defects:` paragraph (`implementation-plan/SKILL.md:262`) is correct and stays for every run; the owner updated the launcher and did not update the skill text. `skills/project-manager/SKILL.md` *Reading a defective plan* (`SKILL.md:42-55`) is aligned to it: a PM resolves a defect where the plan's goals make one resolution natural and its impact stays within the slice or touches only a few slices, records it as a delegated plan decision, never edits the plan, widens the surface except by grant, or relaxes a floor fact, and stops for anything serious, contested or cross-cutting, and whenever the floor or a plan gate needs a human. In campaign mode the CM reads each child's recorded delegated decisions when it judges the node and corrects a wrong one through a later node. Rejected: the strict rule (turns every small defect into a stopped node and a relaunch).
- **D17. Fact 5 sees both sides of a rename, on every run.** Recommendation: `git_ops.changed_files_between` passes `--no-renames`, so a rename whose source lies outside the effective surface, or inside a protected path, fails fact 5. Grounds: `git diff --name-only` otherwise reports only the destination, which is what `docs/VISION.md:85` already claims the floor catches; the reviewer prompt's own changed-file list (`review.py:370-374`) keeps rename detection, and its diff patch shows the rename, so that is recorded as a residual rather than widened into Slice 2. Rejected: scoping the change to runs with a policy (two floors for one toolkit).
- **D18. A file-based question channel, one mechanism for Developer→PM and PM→CM.** Recommendation: a session that needs a decision its supervisor can give in a line writes one question file and waits; the supervisor's wait returns early on it; the supervisor answers with a command that writes a numbered answer file, rotates the question file to a numbered copy, and injects a one-line pointer through `sessions.send_line` (so the dialog guard applies). Answers are free (no attempt) but recorded. The pointer text is single-sourced in `references/developer-prompt.md` under `## Answer Pointer`. Rejected: ending the session to ask (a Developer today writes `status: blocked` and the PM must spend a steer to answer; a child PM today would stop its run).
- **D19. The child's completion signal is a file.** Recommendation: `.pm/campaign/child-result.json` in the worktree, written by the child PM when its run is decided (`{"status": "complete" | "stopped" | "needs-human", "run_id": "<run id, or null when init never succeeded>", "summary": "<one paragraph>"}`), the counterpart of the Developer's `result.json`; `wait-child` returns on it and prints each `run.json` status under the worktree's state root as a tokenless cross-check. Rejected: process exit (there is none in tmux) and polling `run-report.md` (regenerated several times).

## Conventions Binding Every Slice

These bind every slice. Each receipt below repeats the ones its reviewers must check, because a commissioned reviewer sees only the slice's own sections.

- `pm_lib` stays standard-library only (`tests/test_plan.py:485` `TestImportHygiene` enforces it, and `pm_lib/__init__.py` documents the one path-based exception). Python 3.13 or newer.
- Python is ruff-clean at 120 columns (`ruff.toml` at the repository root extends the `lint` skill's rule set); `ruff format --check` is part of lint.
- Tests use `unittest` and the `pm_test_helpers` fixtures: `PlanTestCase` (`tests/pm_test_helpers.py:430`), `PmTestCase` (`:483`, adds a git repository, `make_run` at `:515` accepts `policy=`), `TmuxRunTestCase` (`:632`), `render_slice` (`:256`), `write_fake_harness` (`:116`), `parse_init_output` (`:105`), and the fake-harness builders (`:156-253`). The helpers pin `PM_TMUX_SOCKET` and `PM_LEDGER_DIR` per test process at import.
- Fake harnesses only, through `--harness-command`; no test ever invokes a real coding command-line interface (CLI).
- One regression test per new behaviour, boundary-focused rather than permutation-heavy; a pure function is tested on `unittest.TestCase` without a git fixture.
- The full PM suite is run exactly as follows, checking every module's exit status as continuous integration (CI) does (`.github/workflows/ci.yml:52-75`; a bare `wait` hides failures), and the command exits non-zero when any module fails: `cd skills/project-manager/tests && pids=""; for m in test_*.py; do python3 -m unittest "${m%.py}" >"/tmp/pm-${m%.py}.log" 2>&1 & pids="$pids $!:${m%.py}"; done; failed=""; for e in $pids; do wait "${e%%:*}" || failed="$failed ${e#*:}"; done; echo "failed:${failed:- none}"; [ -z "$failed" ]`
- Lint is differential through the `lint` skill: `python3 skills/lint/scripts/lint.py check --base <before_head>`. Lint reports its own coverage; `markdownlint-cli2` is installed on this machine, so Markdown is covered.
- Markdown prose is never hard-wrapped; follow the house style of the file being edited (em dashes are used freely in this repository).
- No token banned by the no-baggage greps in `.github/workflows/ci.yml:101-124` may appear in any active file, this plan included (for example the hyphenated forms of "unauthorized files", "dirty worktree" and "commit missing"); "campaign" and "policy" are fine.
- Every `docs/…` or `skills/…` path written in backticks or as a link in `README.md` or `CONTRIBUTING.md` must exist at the commit that adds it (`.github/workflows/ci.yml:126-146`); slices are ordered so this holds.
- Never weaken, skip or delete a failing test; a failing test is evidence of a real problem.
- Superseded files move to `archive/` (git-ignored); nothing is deleted.

## Implementation Profiles

- Mode B (the intended mode): run the ten atomic slices in plan order. No batches are defined, because PM ignores batch groupings.
- Mechanically elevated slices: Slices 1, 2, 3 and 4 declare a risky surface, and Slice 7 is approval-gated, so `plan_risk` is `elevated` for all five and both independent reviews are mandatory on each. Slices 2 and 4 also carry `Independent audit required: yes`.
- Slice 7 stops the run until the operator records `approve --slice "Slice 7" --reason …`. Because its receipt fixes the exact VISION wording, the approval may be recorded right after `init`, or when the run reaches it.
- Mode A: run slices individually, in order, with the checkpointed launcher below.

## Slice 1: Protected paths: policy file and plan-time refusal (`check-plan --policy`)

### Intended Change

- `skills/project-manager/scripts/pm_lib/plan.py`: add `load_policy(path: Path, *, repo: Path | None = None) -> dict[str, Any]`, which reads a protected-paths policy file and returns `{"path": <resolved absolute path as str>, "sha256": <hex SHA-256 of the file bytes>, "protected_paths": [<normalized entries>]}`. It reads the file as UTF-8 JSON; a top-level object is required; `protected_paths` is optional (absent means `[]`) and must otherwise be a list of non-empty strings each accepted by `authorized_entry_error` (`plan.py:239`) and each having a non-empty `literal_prefix` (an entry such as `**/*.py` or `*.lock` would overlap every surface, and the overlap error's advice could not be followed); entries are normalized with `git_ops.normalize_authorized_entry` (`git_ops.py:153`), deduplicated, order preserved. Every other key (including `schema`) is ignored. With `repo` given, resolve both paths with `.resolve()`; when the policy file lies inside the resolved `repo` (`relative_to` succeeds), append its repo-relative POSIX path, computed against the resolved `repo`, unless an existing entry already matches it (`git_ops.is_authorized_path`, `git_ops.py:170`). Every failure raises `PmError` naming the file and the problem.
- `plan.py`: add two pure functions (no I/O). `literal_prefix(entry: str) -> str`: a plain path returns itself; a subtree `dir/` returns `dir`; a glob returns the `/`-joined segments before the first segment containing `*`, `?` or `[` (possibly the empty string). `protected_overlap(entry: str, protected: list[str]) -> str | None`: returns the first protected entry P for which the literal prefixes of `entry` and P are equal, or one is a segment-wise prefix of the other (`a/b` is a prefix of `a/b/c`; `a/b` is not a prefix of `a/bc`), where an empty prefix overlaps everything; otherwise `None`.
- `plan.py` `plan_check_report` (`plan.py:340`): signature becomes `plan_check_report(path, repo=None, protected: list[str] | None = None)`. When `protected` is given, every authorized entry that passed `authorized_entry_error` (the loop at `plan.py:399-410`) is checked with `protected_overlap` on its normalized form, and an overlap appends the error `f"{prefix}: authorized entry {entry!r} reaches protected path {hit!r}; give it a literal prefix disjoint from every protected path"`, where `prefix` is the existing `"Slice N (title)"` prefix and `entry` the normalized entry. The returned dict (`plan.py:430-436`) gains `"protected_count": len(protected or [])`.
- `skills/project-manager/scripts/pm_lib/cli.py`: the `check-plan` sub-parser (`cli.py:52-54`) gains `--policy FILE`, and its `--repo` gains help text saying it must be the repository root. `_run_check_plan` (`cli.py:264-274`) loads the policy with `plan_mod.load_policy(Path(args.policy), repo=repo_path)` before building the report (a `PmError` propagates to `main`, exit 2), passes `protected=` the loaded entries, and prints `protected paths: N (from <absolute policy path>)` after the existing `N slice(s); approval-gated: …` line.
- `skills/project-manager/tests/test_plan.py`: tests for all of the above (see Validation Plan).

### Acceptance Criteria

- [ ] `load_policy` returns a dict with exactly the keys `path` (resolved absolute path, `str`), `sha256` (hex digest of the file bytes) and `protected_paths` (list of `str`).
- [ ] A protected-paths policy file without `protected_paths` yields `[]`, and unknown top-level keys (including `schema`) are accepted and ignored.
- [ ] `load_policy` raises `PmError` naming the file for each of: an unreadable file, invalid JSON, a top-level value that is not an object, `protected_paths` that is not a list, an item that is not a non-empty string, and an entry `authorized_entry_error` rejects.
- [ ] `load_policy` raises `PmError` naming the entry for a protected entry whose literal prefix is empty (`**/*.py`, `*.lock`).
- [ ] Entries are normalized, deduplicated, and keep their first-seen order.
- [ ] With `repo` given and the policy file inside it, the file's repo-relative POSIX path (relative to the resolved `repo`) is appended exactly once, and not at all when an existing entry already matches it; with the file outside `repo`, or `repo=None`, nothing is appended.
- [ ] `literal_prefix` returns the path itself for a plain entry, `dir` for `dir/`, the segments before the first wildcard segment for a glob, and `""` for a glob whose first segment has a wildcard.
- [ ] `protected_overlap` reports an overlap for: a plain file inside a protected subtree; a subtree containing a protected file; a glob whose literal prefix lies inside a protected subtree; an empty-prefix authorized glob against any protected entry.
- [ ] `protected_overlap` returns `None` for `a/b` against `a/bc/` and for a fully disjoint pair.
- [ ] `plan_check_report(..., protected=[...])` emits the overlap error in the exact wording above, naming the slice, the entry and the protected entry it reaches, and still reports every other error and warning the plan carries.
- [ ] The report dict carries `protected_count`, equal to `len(protected or [])`.
- [ ] `check-plan --policy FILE` exits 2 when any authorized entry reaches a protected path, exits 0 on a plan with no overlap, and prints `protected paths: N (from <path>)` in both cases.
- [ ] `check-plan --policy FILE` with a malformed policy file exits 2 with a `pm: error:` line naming the file.
- [ ] Without `--policy`, `check-plan`'s output and exit code are unchanged for every plan, and `plan_check_report(path, repo)` returns the same errors and warnings as before.
- Inputs: a protected-paths policy file is untrusted input from the operator; validate its shape fully. Matching semantics are those of the authorized surface (`git_ops.is_authorized_path`).
- Behaviour that must not change: every existing `check-plan` error, warning and approval-gated listing; `init` (it gains `--policy` only in Slice 2).

### Authorized Surface

- Files allowed to change:
  - skills/project-manager/scripts/pm_lib/plan.py
  - skills/project-manager/scripts/pm_lib/cli.py
  - skills/project-manager/tests/test_plan.py
- Functions/classes/components allowed to change: `plan.py`: new `load_policy`, `literal_prefix`, `protected_overlap`; `plan_check_report`; the module's imports. `cli.py`: the `check-plan` sub-parser in `build_parser`; `_run_check_plan`.
- Tests allowed or expected to change: `tests/test_plan.py` (new test classes only).

### Explicit Non-Goals

- No `init --policy`, no run-state change, no `grant` or floor change, no README or SKILL.md edit (all Slice 2).
- No change to `authorized_entry_error`, `surface_lint`, `directory_surface_lint` or `git_ops` matching.
- No validation or use of `schema` or of any key other than `protected_paths`.
- No third-party import in `pm_lib`; no real harness CLI in tests; no weakened or deleted test.

### Risk Flags

- Risky surfaces touched: public CLI flag (`check-plan --policy`)
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: moderate
- Recommended Developer: claude, claude-opus-5-5, high effort

### Validation Plan

- Tests to add/update: in `tests/test_plan.py`, a `TestLoadPolicy` class (happy path; each malformed shape; the empty-literal-prefix refusal; self-protection inside the repo; none outside it or with `repo=None`; dedupe), a `TestProtectedOverlap` class on `unittest.TestCase` (the `literal_prefix` table and the `protected_overlap` cases named in the criteria), and a `TestCheckPlanPolicy` class on `PlanTestCase` using `run_cli` (`check-plan --policy` exit 2 on overlap with the error text, exit 0 on a clean plan, exit 2 on a malformed policy, and the report's other errors still present alongside an overlap error).
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_plan test_state`; the full PM suite, run exactly as in Conventions; `python3 -m py_compile skills/project-manager/scripts/pm.py skills/project-manager/scripts/pm_lib/*.py`.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: write a scratch policy outside the repository protecting `skills/project-manager/scripts/` and run `python3 skills/project-manager/scripts/pm.py check-plan --plan docs/implementation-plan-pm-campaign-mode.md --repo . --policy <scratch>`; expect overlap errors for Slices 1–4 and the `protected paths: 1` line. Repeat with a policy protecting `instruments/` and expect no overlap error.

### Rollback Path

- Revert the slice commit. No state shape changes in this slice.

## Slice 2: Protected paths frozen into run state; `grant` and floor fact 5 enforce them

### Intended Change

- `skills/project-manager/scripts/pm_lib/state.py` `_validate_shape` (`state.py:162-205`): when `state["policy"]` is a dict containing the key `protected_paths`, the value must be a list of non-empty strings, else `PmError` (a JSON `null` is rejected, mirroring the `grants` check at `state.py:189-195`). A policy without the key stays valid and enforces nothing extra.
- `skills/project-manager/scripts/pm_lib/plan.py`: add `protected_paths(state: dict[str, Any]) -> list[str]` beside `slice_grants` (`plan.py:485`), tolerant: an absent or non-dict policy, or a missing key, returns `[]`. This is the only change to `plan.py` in this slice.
- `skills/project-manager/scripts/pm_lib/cli.py`: the `init` sub-parser (`cli.py:56-80`) gains `--policy FILE` (a protected-paths policy file). `_run_init` (`cli.py:280-325`) loads it once with `plan_mod.load_policy(Path(args.policy), repo=repo)` before `plan_check_report` (`cli.py:286`), passes `protected=` the loaded entries to `plan_check_report` and `policy=` the loaded dict (or `None`) to `slice_ops.init_run` (`cli.py:294-310`), and prints `protected paths: N (from <path>)` after the slices list (`cli.py:315-319`), before the token line.
- `skills/project-manager/scripts/pm_lib/slice_ops.py` `init_run` (`slice_ops.py:309-413`): new keyword `policy: dict | None`; `policy_block` (`slice_ops.py:392`) becomes `{"max_attempts": …, "protected_paths": policy["protected_paths"] if policy else [], "protected_paths_source": {"path": policy["path"], "sha256": policy["sha256"]} if policy else None}`. The `init` event note (`slice_ops.py:409`) gains a trailing `protected=<N>` field, separated by a space.
- `slice_ops.py` `grant` (`slice_ops.py:534-655`): after the directory-lint refusal (`slice_ops.py:628-630`) and before the already-authorized check (`slice_ops.py:632-636`), add: `if git_ops.is_authorized_path(normalized, plan_mod.protected_paths(state)): raise PmError(f"grant refused: {normalized!r} is a protected path (policy frozen at init); no grant can reach it")`.
- `skills/project-manager/scripts/pm_lib/git_ops.py` `changed_files_between` (`git_ops.py:104-112`): pass `--no-renames` to the `diff --name-only` call (`git_ops.py:108`) and to the `show --name-only` call (`git_ops.py:110`), as `code_metrics.py:109` already does. Without it git reports only a rename's destination, so `git mv` of a protected file into an authorized directory would leave fact 5 blind to the source.
- `skills/project-manager/scripts/pm_lib/floor.py` `_fact_surface` (`floor.py:180-209`): compute `protected = protected_paths(state)` and `protected_hits = sorted(p for p in changed if git_ops.is_authorized_path(p, protected))`; evidence gains `protected_files` (the hits) and `protected_surface` (the recorded list); `passed = not unauthorized and not protected_hits`; detail: unauthorized only keeps today's wording, protected only is `"changed files include protected paths"`, both is `"changed files include entries outside the effective authorized surface and protected paths"`. The module docstring's item 5 (`floor.py:30-31`) says fact 5 also fails on a frozen protected path. An authorized entry can never legitimately cover a protected file (Slice 1 refuses it at plan time; `grant` refuses it here), so a hit is a planning bypass or a bug and fails closed.
- `skills/project-manager/references/run-state.md`: the schema example's policy line (`run-state.md:39`) becomes `"policy": {"max_attempts": 10, "protected_paths": [], "protected_paths_source": null}`; *Semantics worth knowing* (`run-state.md:82-94`) gains one bullet documenting: the protected-paths policy file format (a JSON object; `protected_paths` in authorized-surface entry syntax, each with a non-empty literal prefix; every other key ignored by the toolkit; a policy file inside the repository protects itself); the frozen-copy rule (entries are copied at `init` and never re-read from the file); the source shape (`{"path", "sha256"}` or `null`); the enforcement points (`check-plan`/`init` refusal, `grant` refusal, fact 5, which sees both sides of a rename, while a reviewer prompt's changed-file list still shows a rename's destination only and its diff patch shows the rename); compatibility (an absent key, or an absent or non-dict `policy`, enforces nothing); and the limit that git-ignored files cannot be protected, because the floor sees Git-visible state only.
- `skills/project-manager/README.md`: the `check-plan` and `init` rows (`README.md:28-29`) document `--policy FILE` (frozen protected paths; `check-plan` and `init` refuse a surface reaching one; `grant` refuses one; fact 5 fails on one); the trust-model paragraph (`README.md:123`) lists "no change to a frozen protected path" among the mechanical facts.
- `skills/project-manager/SKILL.md`: the floor sentence's item (5) (`SKILL.md:22`) reads "changed files ⊆ effective authorized surface (frozen plan surface + recorded PM grants) and touch no frozen protected path". No other SKILL.md change.

### Acceptance Criteria

- [ ] `init --policy FILE` on a plan whose authorized entry reaches a protected path exits 2, prints the Slice 1 overlap `ERROR:` line, and creates no run directory.
- [ ] A run initialized with `--policy` records `policy.protected_paths` equal to `load_policy`'s entries and `policy.protected_paths_source` equal to `{"path": <absolute path>, "sha256": <digest>}`; a run without `--policy` records `[]` and `null`; `policy.max_attempts` is unchanged in both.
- [ ] A protected-paths policy file committed inside the repository appears in the recorded `protected_paths` by its repo-relative path.
- [ ] `init` prints `protected paths: N (from <path>)` after the slices list when `--policy` is given.
- [ ] The `init` event note carries `protected=<N>` on every run (`protected=0` without a policy).
- [ ] Nothing re-reads the policy file after `init`: deleting or editing it mid-run changes no refusal or fact outcome.
- [ ] `_validate_shape` refuses state whose `policy.protected_paths` is present but not a list (including `null`) or holds an item that is not a non-empty string; state whose policy lacks the key loads normally.
- [ ] `plan.protected_paths(state)` returns the recorded list, or `[]` when the policy is absent or not a dict or the key is absent.
- [ ] `grant` refuses a path matched by any protected entry with the exact message above, even when the path is otherwise grantable, and leaves `run.json` and `events.jsonl` unchanged.
- [ ] `git_ops.changed_files_between` reports both the source and the destination of a committed rename.
- [ ] Fact 5 fails when a changed file matches a protected entry even though it lies inside the effective authorized surface, with `protected_files` naming it, while the other six facts pass.
- [ ] A committed rename of a protected file to an authorized path fails fact 5 with `protected_files` naming the source.
- [ ] Fact 5's evidence always carries `protected_files` and `protected_surface`, and a passing fact 5 has `protected_files == []`; the floor still reports exactly seven facts in the existing order.
- [ ] Fact 5's detail uses the three wordings above for unauthorized-only, protected-only and both, and today's wording on a pass.
- [ ] `run-state.md`, the PM README's `check-plan`/`init` rows and trust-model paragraph, and the PM SKILL.md floor sentence document the behaviour exactly as built, and nothing else in those files changes.
- Behaviour that must not change: a run without a protected-paths policy behaves as today with one deliberate tightening — a rename whose source lies outside the effective surface now fails fact 5, which is what `docs/VISION.md:85` already claims; `grant`'s existing refusals and their order; facts 1–4, 6 and 7.

### Authorized Surface

- Files allowed to change:
  - skills/project-manager/scripts/pm_lib/state.py
  - skills/project-manager/scripts/pm_lib/slice_ops.py
  - skills/project-manager/scripts/pm_lib/cli.py
  - skills/project-manager/scripts/pm_lib/floor.py
  - skills/project-manager/scripts/pm_lib/plan.py
  - skills/project-manager/scripts/pm_lib/git_ops.py
  - skills/project-manager/tests/test_state.py
  - skills/project-manager/tests/test_slice_ops.py
  - skills/project-manager/tests/test_floor.py
  - skills/project-manager/tests/test_git_ops.py
  - skills/project-manager/references/run-state.md
  - skills/project-manager/README.md
  - skills/project-manager/SKILL.md
- Functions/classes/components allowed to change: `state._validate_shape`; `plan.protected_paths` (new; nothing else in `plan.py`); `git_ops.changed_files_between` (the two `--no-renames` arguments only); `cli.build_parser` (`init` sub-parser only) and `cli._run_init`; `slice_ops.init_run` and `slice_ops.grant`; `floor._fact_surface`, `floor`'s imports and module docstring item 5; the run-state schema example and one *Semantics* bullet; the two README rows and the trust-model paragraph; the SKILL.md floor sentence.
- Tests allowed or expected to change: `tests/test_state.py`, `tests/test_slice_ops.py`, `tests/test_floor.py`, `tests/test_git_ops.py` (new tests only).

### Explicit Non-Goals

- No rendering of protected paths into Developer or Reviewer prompts, the run report, or `status` output (D15).
- No eighth floor fact (D3), and no change to `check-plan`'s Slice 1 semantics.
- No re-reading of the policy file after `init`, and no migration of existing runs.
- No rename handling in `status_changed_files` or the dirty-tree path (a dirty tree already fails fact 7).
- No third-party import in `pm_lib`; no real harness CLI in tests; no weakened or deleted test.

### Risk Flags

- Risky surfaces touched: persistence (run-state `policy` shape), the mechanical floor (fact 5 and its changed-file list), public CLI flag (`init --policy`)
- Approval needed before implementation: no
- Independent audit required: yes
- Difficulty: hard
- Recommended Developer: claude, claude-opus-5-5, high effort

### Validation Plan

- Tests to add/update: `tests/test_state.py`, a policy-shape class beside `TestGrantsShapeValidation` (`test_state.py:203`): non-list, `null` and non-string entries rejected; absent key valid. `tests/test_git_ops.py`, in `TestGitFacts` (`test_git_ops.py:95`): a committed rename reports both source and destination. `tests/test_slice_ops.py`: in `TestInitHappyPath` (`test_slice_ops.py:70`), `init --policy` records entries and source digest (policy file outside the repository), and a policy file committed inside the repository is self-protected; in `TestInitFailures` (`:142`), `init --policy` refuses an overlapping plan with exit 2, the error text, and no run directory; in `TestGrant` (`:362`), a grant on a protected path is refused (also when the path is otherwise grantable, and also after the policy file is deleted), with state unchanged. `tests/test_floor.py`: a new class beside `TestFactSurfaceGrants` (`test_floor.py:314`) using `make_run(policy=…)`: a committed change to a protected file inside the plan surface fails fact 5 with `protected_files` naming it while the other six facts pass; a committed `git mv` of a protected file into the authorized surface fails fact 5 naming the source; a clean run passes with `protected_files == []`; the both-cases detail string.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_state test_git_ops test_slice_ops test_floor test_plan test_finalize test_review`; the full PM suite, run exactly as in Conventions; `python3 -m py_compile skills/project-manager/scripts/pm.py skills/project-manager/scripts/pm_lib/*.py`.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: in a scratch repository outside this one, run `init --policy` with a fake `--harness-command`, then read `run.json` (`<worktree-git-dir>/pm/<run-id>/run.json`) and confirm the frozen policy block.

### Rollback Path

- Revert the slice commit. Runs initialized while the slice was in place keep `protected_paths` and `protected_paths_source` in `run.json`; the reverted toolkit ignores those unknown keys (validation is tolerant) and enforces no protection for them, so stop such runs rather than continue them. The revert also restores rename-destination-only changed-file lists, so a rename whose source lies outside the surface passes fact 5 again.


## Slice 3: Developer question channel

### Intended Change

- A Developer that needs a decision PM can give in a line writes one question file and waits; `observe --wait` returns early on it; `pm answer --text` writes a numbered answer file, archives the question, and injects a one-line pointer through `sessions.send_line` (D18). Answers are free (no attempt) and recorded.
- `skills/project-manager/scripts/pm_lib/slice_ops.py` `start_slice`: the Developer environment (`slice_ops.py:872-879`) gains `PM_QUESTION_PATH=<artifact_dir>/question.md`, and the `render_developer_prompt` call (`slice_ops.py:858-866`) passes `question_path=artifact_dir / "question.md"`.
- `skills/project-manager/scripts/pm_lib/prompts.py`: `render_developer_prompt` (`prompts.py:111-161`) gains an optional keyword `question_path: Path | None = None` (defaulting to `artifact_dir / "question.md"`, so existing callers keep working) rendered as the template field `{question_path}`; a new `render_answer_pointer(answer_path: Path, *, reference_path: Path | None = None) -> str` loads the `## Answer Pointer` section by the same mechanism as `render_steer_pointer` (`prompts.py:181-194`), with a new heading constant beside `prompts.py:36-37`.
- `skills/project-manager/references/developer-prompt.md`, the fenced template (`developer-prompt.md:13-66`):
  - the Workflow (`developer-prompt.md:42-54`) gains, after step 2 or as its own numbered step: "When the contract leaves open a decision PM can give in a line (which of two readings; a tolerance; which existing test to extend), and it is not scope or surface you are forbidden, write one question to `{question_path}` with the options and your recommendation, then wait; PM's answer arrives as a one-line pointer to an answer file. A question is not a completion: do not write the result file until the work is done.";
  - the Hard rule on what the Developer may write (`developer-prompt.md:59`, "write only your code changes, `validation.md`, and `{result_path}`") also names `{question_path}`;
  - the Stop rules (`developer-prompt.md:65`) gain: "a decision PM can give in a line is a question, not a stop", and their first condition "a second reading is also implementable" becomes "a second reading is also implementable and PM has not answered your question about it".
- `developer-prompt.md` gains a final section `## Answer Pointer` (after `## Steer Message Template`, `developer-prompt.md:76-82`) with a short description and one fenced ```md block: `Your supervisor's answer to your question is at {answer_path} — read it now, in full, then continue inside your frozen contract; an answer never widens your authorized surface or adds scope.` The rendered block is one line, and its only brace is `{answer_path}`.
- `slice_ops.observe` (`slice_ops.py:1021-1141`): the poll loop's break condition (`slice_ops.py:1057-1058`) also breaks when `<artifact_dir>/question.md` exists; `ObserveOutcome` (`slice_ops.py:1005-1018`) gains `question_present: bool = False` and `question_text: str | None = None` (the file's text, at most 40 lines); `no_signal` (`slice_ops.py:1111`) is false when a question is present; the `observe` event note (`slice_ops.py:1114-1118`) gains `question_present=<bool>`.
- `skills/project-manager/scripts/pm_lib/cli.py` `_run_observe` (`cli.py:520-554`) prints `question present: yes` followed by the question text, or `question present: no`, after the `result present` line.
- New `slice_ops.answer(repo, run_dir, token, *, text) -> AnswerOutcome(slice_id, answer_path, question_archived_path, answer_number)`, in this order: `load_writable_state`; `_refuse_if_budget_exhausted`; a current slice is required; `text.strip()` is non-empty; `question.md` exists in the artifact dir (else `PmError("no open question …")`); `result.json` does not exist (else `PmError` saying the submission is in, use `finalize --steer`); the session exists; then the shared delivery helper (below); then one event `answer` with `slice_id`, `note` = the full answer text verbatim (as `finalize_steer`'s `steer` event records its correction, `slice_ops.py:1680`; this is the durable record, because `answer-<n>.md` sits in the Developer-writable `.pm/` directory), `evidence` = the answer path, and `data = {"question": "question-<n>.md"}`. `answer` performs no state save: it loads verified state to find the current slice and appends that one event only. No attempt is charged, and the judgment gate does not apply (an answer is not an exit from a submission).
- The shared delivery helper (one small function, either in `slice_ops.py` or in a new `pm_lib/questions.py`, the Developer's choice; Slice 4 reuses it with a `cm-` file prefix): given a directory, a file prefix, a session and the text, compute `n` = 1 + the number of existing `<prefix>answer-*.md` files; create `<prefix>answer-<n>.md` exclusively with the text verbatim; rename `<prefix>question.md` to `<prefix>question-<n>.md`; `sessions.send_line(session, prompts.render_answer_pointer(answer_path))`. On `TypedNotSubmitted`, leave both files in place and re-raise (the pointer is typed; the rule of `finalize_steer`, `slice_ops.py:1656-1674`). On any other `PmError` from the send (nothing typed), rename the question back, remove the answer file, and re-raise. Return the answer path, the archived question path and `n`.
- `_rotate_prior_attempt` (`slice_ops.py:682`) adds `question.md` to its names, so an unanswered question rotates with the attempt it belongs to; numbered `question-<n>.md` and `answer-<n>.md` stay in place.
- `cli.py`: new `answer --text TEXT [--run ID] [--token T]` sub-parser and `_run_answer` handler printing the answer path and the archived question path; `_HANDLERS` entry (`cli.py:869-885`). The `cli` module docstring is unchanged.
- `skills/project-manager/SKILL.md` *Workflow* step 2 (`SKILL.md:71`): `observe --wait` also returns when the Developer writes a question; answer it with `pm answer --text` (free, recorded, delivered as a one-line pointer behind the dialog guard; only while the Developer has not written `result.json`); a question asking for scope or surface is answered "no", or PM records a grant and delivers it by relaunch or steer (*Surface grants*), never by an answer; a question whose answer would settle an ambiguity on *Reading a defective plan*'s "Also stop for" list (authorization, acceptance criteria, risk flags) is handled by that section, as a stop, not answered; carry a ruling that settles a contract reading into `notes.md` and into later review adjudications. Step 3 (`SKILL.md:72`) changes only if a cross-reference to the answer path is needed.
- `skills/project-manager/README.md`: a CLI table row for `answer`; the per-slice layout list (`README.md:115`) gains `question.md` (open), `question-<n>.md` and `answer-<n>.md`.
- `skills/project-manager/references/run-state.md`: the events line (`run-state.md:11`) gains `answer` with its `data` (`{question}`); the authority model's list of mutating commands (`run-state.md:19`) gains `answer`; and the *Semantics* list (`run-state.md:82-94`) gains one residual clause: a Developer window's elapsed time now includes the time PM takes to answer, and answers are not counted in the ledger's nudge count (`ledger.py:281-303` counts only `send`).
- Tests: `tests/test_slice_ops.py`, `tests/test_prompts.py`, and, if more than one test uses it, a `question_then_result_script(trigger)` builder in `tests/pm_test_helpers.py`.

### Acceptance Criteria

- [ ] A launched Developer session's environment carries `PM_QUESTION_PATH` equal to `<artifact_dir>/question.md`.
- [ ] The rendered Developer prompt names the question path in the new Workflow instruction, in the Hard rule on writable files, and in the Stop rules' question sentence.
- [ ] The Stop rules' first condition reads "a second reading is also implementable and PM has not answered your question about it".
- [ ] `render_developer_prompt` called without `question_path` renders `<artifact_dir>/question.md`, and every existing caller and test works unchanged.
- [ ] `render_answer_pointer(path)` returns one line, with no newline, containing the path, sourced from the `## Answer Pointer` section.
- [ ] `observe --wait N` returns before N seconds elapse once `question.md` exists, and prints `question present: yes` followed by the question text.
- [ ] `observe` prints `question present: no` when no question is open.
- [ ] `ObserveOutcome.question_text` holds at most 40 lines of the question file.
- [ ] `observe` reports `no_signal` false when a question is present.
- [ ] The `observe` event note carries `question_present=`.
- [ ] `pm answer --text T` writes `answer-<n>.md` with `T` verbatim, renames `question.md` to `question-<n>.md`, and injects the one-line answer pointer into the session.
- [ ] A second question and answer on the same slice produce `question-2.md` and `answer-2.md`.
- [ ] `pm answer` appends one `answer` event with the slice id, the full answer text verbatim as `note`, the answer path as `evidence`, and `data` `{"question": "question-<n>.md"}`.
- [ ] `pm answer` charges no attempt and performs no state save: `run.json` is byte-identical before and after.
- [ ] `pm answer` refuses, with exit 2 and both files unchanged, each of: no open question; `result.json` present; no live session; blank text; an exhausted attempt budget.
- [ ] When `sessions.send_line` raises `TypedNotSubmitted`, both the answer file and the archived question stay in place and the error propagates (a test patches `send_line`).
- [ ] When `sessions.send_line` raises a plain `PmError`, `question.md` is restored, the answer file is removed, and the error propagates (a test patches `send_line`).
- [ ] A relaunch or steer that rotates the prior attempt also rotates an open `question.md` into `attempt-<n>/`.
- [ ] The SKILL.md Workflow step 2 describes the question loop as built, including the scope-or-surface rule above.
- [ ] The README `answer` row and per-slice layout list describe the behaviour as built.
- [ ] `run-state.md` carries the `answer` event, `answer` among the mutating commands, and the elapsed-time and nudge-count residual.
- Behaviour that must not change: every existing command; `observe`'s other break conditions and output lines; `send_line`'s dialog guard; the launch and steer pointers.

### Authorized Surface

- Files allowed to change:
  - skills/project-manager/scripts/pm_lib/slice_ops.py
  - skills/project-manager/scripts/pm_lib/cli.py
  - skills/project-manager/scripts/pm_lib/prompts.py
  - `skills/project-manager/scripts/pm_lib/questions.py` (new file, only if the helper lives there)
  - skills/project-manager/references/developer-prompt.md
  - skills/project-manager/SKILL.md
  - skills/project-manager/README.md
  - skills/project-manager/references/run-state.md
  - skills/project-manager/tests/test_slice_ops.py
  - skills/project-manager/tests/test_prompts.py
  - skills/project-manager/tests/pm_test_helpers.py
- Functions/classes/components allowed to change: `slice_ops.start_slice` (env and the prompt call only), `slice_ops.observe`, `ObserveOutcome`, `_rotate_prior_attempt` (its names), new `slice_ops.answer` and `AnswerOutcome`, the shared delivery helper; `prompts.render_developer_prompt`, new `prompts.render_answer_pointer` and its heading constant; `cli.build_parser` (new `answer` sub-parser only), `cli._run_observe`, new `cli._run_answer`, `cli._HANDLERS`; the developer-prompt template's Workflow, writable-files Hard rule and Stop rules, and the new `## Answer Pointer` section; SKILL.md Workflow steps 2 and 3; the README CLI table (one row) and per-slice layout list; `run-state.md:11`, `run-state.md:19` and one new clause in the *Semantics* list (`run-state.md:82-94`).
- Tests allowed or expected to change: `tests/test_slice_ops.py`, `tests/test_prompts.py` (new tests only); `tests/pm_test_helpers.py` (one new builder only).

### Explicit Non-Goals

- No CM channel and no campaign command (Slice 4).
- No change to `finalize`, `finalize --steer`, the judgment gate, the floor, or the ledger rows (an `answer` event is not a submission origin).
- No reviewer-prompt change, and no rendering of question or answer files into review prompts.
- No third-party import in `pm_lib`; no real harness CLI in tests; no weakened or deleted test.

### Risk Flags

- Risky surfaces touched: session injection (`send_line` into a live Developer session), public CLI command (`answer`)
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: hard
- Recommended Developer: claude, claude-opus-5-5, high effort

### Validation Plan

- Tests to add/update: `tests/test_slice_ops.py`, a tmux-gated class beside `TestSendNudge` (`test_slice_ops.py:980`), on `SliceOpsTestCase`, with a fake Developer that drains stdin, writes `$PM_QUESTION_PATH`, waits for `answer-1.md` (trigger-gated, the pattern of `trigger_gated_result_script`, `pm_test_helpers.py:220-222`), then commits and writes `result.json`: (a) `observe --wait 30` returns early with `question present: yes` and the text; (b) `answer` writes `answer-1.md`, archives `question-1.md`, appends the `answer` event, charges no attempt, and the Developer then finishes (the result appears); (c) refusals: no open question, `result.json` present, dead session, blank text, each leaving the files unchanged; (d) the rotation of an open `question.md` on relaunch; (e) two mocked-send tests with `sessions.send_line` patched: raising `TypedNotSubmitted` leaves both files in place, and raising a plain `PmError` restores `question.md` and removes the answer file. `tests/test_prompts.py`: beside `TestRenderSteerPointer` (`test_prompts.py:94`), `render_answer_pointer` renders one line containing the path; in `TestRenderDeveloperPrompt` (`:151`), the prompt carries `question_path` and its default.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_slice_ops test_prompts test_finalize test_ledger`; the full PM suite, run exactly as in Conventions; `python3 -m py_compile skills/project-manager/scripts/pm.py skills/project-manager/scripts/pm_lib/*.py`.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: render one Developer prompt for a scratch plan and read the three new sentences in context; `python3 skills/project-manager/scripts/pm.py answer --help` matches the README row.

### Rollback Path

- Revert the slice commit. Runs that recorded `answer` events keep them; the reverted toolkit ignores the unknown event kind, and an open `question.md` in a live slice is then an unanswered file the Developer waits on, so steer or relaunch such a slice.

## Slice 4: Child PM in tmux: `launch-child`, `wait-child`, `answer-child`, `stop-child`

### Intended Change

- New module `skills/project-manager/scripts/pm_lib/campaign.py` holds the four commands; `skills/project-manager/scripts/pm_lib/cli.py` adds four sub-parsers, thin `_run_*` handlers and their `_HANDLERS` entries (`cli.py:869-885`), and mentions `campaign` in the module docstring (`cli.py:1-8`). None of the four defines `--run` or `--token`, so the post-command ledger hook (`cli.py:888-915`) does nothing for them; none reads a token or touches PM run state.
- All four take `--worktree PATH` (the node worktree). `launch-child` writes, and the other three read, the launch manifest; in `wait-child`, `answer-child` and `stop-child` a missing or unreadable `child.json` is a `PmError` (exit 2). The manifest is `PATH/.pm/campaign/child.json`: `{"socket", "session", "branch", "base", "worktree", "launcher", "launched_at"}`. Each command sets `PM_TMUX_SOCKET` to the manifest's (or the new) socket for its own `sessions` calls, which read it through `sessions.tmux_argv` (`sessions.py:152-166`), and restores the previous value before returning, so an in-process caller keeps its own server.
- `launch-child --repo R --base REF --branch B --worktree PATH --prompt-file FILE --harness H [--model M] [--effort E] [--socket NAME] [--harness-command CMD]`.
  - Preconditions, each a `PmError` (exit 2) checked before anything is created: `R` resolves (`git_ops.resolve_repo`); `REF` resolves to a commit (`git rev-parse --verify REF^{commit}`); `refs/heads/B` does not exist; `PATH` does not exist; the prompt file is readable and not blank; `H` is in `profiles.SUPPORTED_HARNESSES` unless `--harness-command` is given; an explicit `--socket` is non-blank with no whitespace and no `/` (D14); the command composes — `--harness-command` as given, else `profiles.compose_command(H, model=M, effort=E, session_id=<uuid4 for claude>)` with opencode's model identity check, exactly as `start_slice` does (`slice_ops.py:888-902`) — and its executable (`shlex.split(command)[0]`) is on PATH.
  - Then: `git -C R worktree add -b B PATH REF` through `git_ops.git`; `slice_ops.write_pm_gitignore(PATH)` (`slice_ops.py:150`); create `PATH/.pm/campaign/`; copy the prompt file to `PATH/.pm/campaign/launcher.md`.
  - Names: one `secrets.token_hex(3)` value `X`; with `<b>` the branch name with every character outside `[A-Za-z0-9_-]` replaced by `-` (tmux rejects `.` and `:` in a session name, and a `-t` target containing `.` parses as window.pane): session `campaign-<b>-<X>` (it must not match `pm-*`, which the unscoped scavenge sweeps); socket `--socket`, else `pm-child-<b>-<X>`.
  - Launch, as `start_slice` launches a Developer (`slice_ops.py:904-918`): `sessions.start_session(session, PATH, command, env)` with `env = {"PM_TMUX_SOCKET": <socket>, "PM_CM_QUESTION_PATH": <PATH/.pm/campaign/cm-question.md>, "PM_CHILD_RESULT_PATH": <PATH/.pm/campaign/child-result.json>}` (never `PM_RUN_TOKEN`, which `start_session` also unsets in the session shell; `PM_LEDGER_DIR` is inherited like any variable); write `child.json` immediately after `start_session` succeeds, so a manifest exists whenever a session exists; `sessions.wait_until_ready(session, executable, expected_model_display=…)`; `sessions.send_prompt(session, prompts.render_child_launch_pointer(launcher_path))`; print `child launched: session=<session> socket=<socket> worktree=<PATH> launcher=<launcher path>`.
  - Failure handling: a failure after the session started calls `sessions.kill_server(socket)` (the server is private to this child, which may already have launched Developers on it; the guard pattern of `start_slice`, `slice_ops.py:916-990`), keeps `child.json` so `stop-child` can be re-run, and raises `PmError` naming the branch and worktree left behind; a failure after the worktree exists but before the session starts raises naming them the same way; there is no rollback machinery (the CM removes them by hand with `git worktree remove` and `git branch -D`).
- `skills/project-manager/scripts/pm_lib/prompts.py`: new `render_child_launch_pointer(launcher_path: Path, *, reference_path: Path | None = None) -> str`, loading a new `## Child Launch Pointer` section of `references/developer-prompt.md` by the mechanism of `render_launch_pointer` (`prompts.py:164-178`). The section's one fenced ```md block: `You are a Project Manager launched by a Campaign Manager. Read your launcher at {launcher_path} now, in full, before doing anything else, then follow it exactly.`
- `wait-child --worktree PATH [--wait N]` (`--wait` uses `_positive_seconds`, `cli.py:38`): the `observe` loop (`slice_ops.py:1021-1141`) without run state. Poll every 2 seconds; capture the visible pane each poll (`sessions.scan_visible_pane`, `sessions.py:273`) into `.pm/campaign/pane-live.txt`; break on session death, `child-result.json` present, `cm-question.md` present, or a dialog marker on the visible pane; never on a pane change. Print: session running; result present (with its `status`); question present (with the text, at most 40 lines); the dialog marker (`sessions.dialog_marker_detail`, `sessions.py:140`) or `clear`; the no-signal note when a requested wait elapsed with none of these (advice, as `observe` prints it); one `child run <id>: status=<status> stop_reason=<stop_reason> (unverified: tokenless read)` line per `run.json` under `state.state_root(PATH)` (`state.py:44-45`), with `status=unreadable` for a file that cannot be read or parsed; and the pane tail (40 lines). Once the manifest loads, it exits 0 always: it is evidence, not a decision.
- `answer-child --worktree PATH --text TEXT`: the session exists; `cm-question.md` exists; `child-result.json` does not; the text is not blank; then the shared delivery helper from Slice 3 with the `cm-` prefix in `PATH/.pm/campaign/` (writes `cm-answer-<n>.md`, renames the question to `cm-question-<n>.md`, sends the Slice 3 answer pointer through `sessions.send_line`, with the same `TypedNotSubmitted` and untyped-failure handling); print both paths.
- `stop-child --worktree PATH --reason TEXT`: capture the full pane (`sessions.capture_to`, `sessions.py:283`) into `.pm/campaign/pane.txt`; read each `run.json` under the state root tokenlessly and, when any records a non-empty `current_slice.reviewer_pids`, print `reviewer process groups the child recorded: <pgids>; they are outside the child's tmux server — verify and kill them by hand`; `sessions.kill_server(socket)`; print the reason and what it did (the CM records the reason in `work.md`). It never signals a reviewer process group (D13).
- `skills/project-manager/scripts/pm_lib/sessions.py`: new `kill_server(socket: str) -> None` that runs `["tmux", "-L", socket, "kill-server"]` with the socket passed in, ignoring a non-zero exit (no server) and a missing tmux binary. `sessions.py` stays the only module that shells out to tmux (its docstring, `sessions.py:3-8`, already says so).
- `skills/project-manager/README.md`: CLI table rows for the four commands; the CLI intro sentence (`README.md:24`, "except `check-plan`/`init`, which take paths") also names the four campaign commands as path-taking; the Privacy table (`README.md:131-137`) gains a row for `.pm/campaign/pane*.txt` and the launcher copy (the child's pane shows its own `PM_RUN_TOKEN=` line from its `init`, `cli.py:320`, which authorizes only that child's state); the maintainer map (`README.md:206`) adds `campaign` (child PM launch, watch, answer and stop).
- `skills/project-manager/references/run-state.md:3`: "every run-scoped command (all except `check-plan` and `init`)" also excepts the four campaign commands.

### Acceptance Criteria

- [ ] The four sub-parsers accept exactly the flags above, and none defines `--run` or `--token`.
- [ ] Each `launch-child` precondition fails with exit 2 and a `pm: error:` message, creates no branch and no worktree, and leaves any pre-existing branch or path untouched.
- [ ] On success, branch `B` points at the commit `REF` names and `PATH` is a linked worktree on `B` at launch time.
- [ ] `PATH/.pm/.gitignore` exists, `PATH/.pm/campaign/launcher.md` is a byte copy of the prompt file, and `child.json` carries the seven fields above.
- [ ] The child session runs on the socket's private tmux server under a name beginning `campaign-`, never `pm-`.
- [ ] The default socket matches `pm-child-<b>-<6 lowercase hex characters>`, where `<b>` is the branch with every character outside `[A-Za-z0-9_-]` replaced by `-`, and shares its hex suffix with the session name.
- [ ] A branch name containing `/` or `.` yields session and socket names containing neither.
- [ ] An explicit `--socket` that is blank, contains whitespace, or contains `/` is refused as a precondition.
- [ ] The child's environment carries `PM_TMUX_SOCKET` equal to the socket.
- [ ] The child's environment carries `PM_CM_QUESTION_PATH` and `PM_CHILD_RESULT_PATH` naming the two files under `.pm/campaign/`.
- [ ] The child's environment lacks `PM_RUN_TOKEN` even when the caller has it set.
- [ ] The child receives the one-line child launch pointer naming `launcher.md`, sourced from the `## Child Launch Pointer` section.
- [ ] A failure after the worktree exists raises `PmError` naming the branch and the worktree left behind.
- [ ] A failure after the session started leaves no tmux server on the socket and leaves `child.json` in place.
- [ ] `child.json` exists whenever the child session exists: it is written immediately after `start_session` succeeds.
- [ ] Each command restores the caller's `PM_TMUX_SOCKET` before returning.
- [ ] `wait-child --wait N` returns before N seconds once `cm-question.md` exists, printing the question text.
- [ ] `wait-child --wait N` returns before N seconds once `child-result.json` exists, printing its `status`.
- [ ] `wait-child --wait N` returns before N seconds on session death or a visible dialog marker, and never on a pane change alone.
- [ ] `wait-child` writes the visible pane to `.pm/campaign/pane-live.txt`, prints one `child run …` line per `run.json` under the worktree's state root (`status=unreadable` for a bad file), prints the 40-line pane tail, and exits 0.
- [ ] `answer-child` writes `cm-answer-<n>.md`, renames the question to `cm-question-<n>.md`, and injects the answer pointer, through the same helper as `pm answer`.
- [ ] `answer-child` refuses, with exit 2 and the files unchanged, each of: no open question; `child-result.json` present; no live session; blank text.
- [ ] `wait-child`, `answer-child` and `stop-child` exit 2 with a `pm: error:` naming the file when `child.json` is missing or unreadable.
- [ ] `stop-child` writes the full pane to `.pm/campaign/pane.txt` and kills the socket's server (the session no longer exists).
- [ ] `stop-child` prints any `reviewer_pids` recorded in the child's `run.json` files and never signals them.
- [ ] `sessions.kill_server(socket)` runs `tmux -L <socket> kill-server` with the given socket, never the unscoped form, and returns normally when no server or no tmux exists.
- [ ] The README has a CLI row for each of the four commands.
- [ ] The README CLI intro sentence names the four commands as path-taking.
- [ ] The README Privacy table has the `.pm/campaign/` row.
- [ ] The README maintainer map lists `campaign`.
- [ ] The `run-state.md:3` sentence excepts the four commands.
- Inputs: the prompt file and `--text` are operator-supplied; never interpolate either into a shell string.
- Behaviour that must not change: every existing command; `start_slice`, `observe` and `stop --scavenge` behaviour; `sessions.tmux_argv`; `review.py` and `profiles.py`.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/campaign.py` (new file)
  - skills/project-manager/scripts/pm_lib/cli.py
  - skills/project-manager/scripts/pm_lib/sessions.py
  - skills/project-manager/scripts/pm_lib/prompts.py
  - skills/project-manager/references/developer-prompt.md
  - `skills/project-manager/tests/test_campaign.py` (new file)
  - skills/project-manager/README.md
  - skills/project-manager/references/run-state.md
- Functions/classes/components allowed to change: everything in `campaign.py`; `cli.build_parser` (four new sub-parsers only), four new `cli._run_*` handlers, `cli._HANDLERS`, the `cli` module docstring; `sessions.kill_server` (new; nothing else in `sessions.py`); `prompts.render_child_launch_pointer` (new) and its heading constant; the new `## Child Launch Pointer` section of `developer-prompt.md`; in the README, the CLI intro sentence, four new CLI table rows, one new Privacy row and the maintainer map line; the `run-state.md:3` sentence.
- Tests allowed or expected to change: `tests/test_campaign.py` (new).

### Explicit Non-Goals

- No headless or one-process child (D1), no automatic kill on a deadline (D13), no polling by the command itself beyond the bounded `wait-child` loop.
- No signal to any process outside the child's tmux server, including the reviewers the child recorded.
- No change to `review.py`, `profiles.py`, `slice_ops.py`, run state, or the Slice 3 helper's behaviour; no token handling.
- No worktree removal, merge, evidence copy or cleanup of a finished child beyond `stop-child` (the CM does those, Slice 8).
- No third-party import in `pm_lib`; no real harness CLI in tests; no weakened or deleted test.

### Risk Flags

- Risky surfaces touched: git worktree and branch creation, tmux session control and injection, public CLI commands (`launch-child`, `wait-child`, `answer-child`, `stop-child`)
- Approval needed before implementation: no
- Independent audit required: yes
- Difficulty: hard
- Recommended Developer: claude, claude-opus-5-5, high effort

### Validation Plan

- Tests to add/update: `tests/test_campaign.py`, tmux-gated (skip without tmux), on `PmTestCase`, with the worktree and prompt file under `self.repo.parent`, a dedicated socket `pm-tests-<pid>-child` passed as `--socket` and killed in `tearDown` through `sessions.kill_server` (so no per-session reaping is needed), and a fake child harness (shell, through `--harness-command`) that echoes readiness, drains stdin, records `$PWD`, `$PM_TMUX_SOCKET` and whether `PM_RUN_TOKEN` is set to files outside the worktree, writes `$PM_CM_QUESTION_PATH` after a short delay, waits for `cm-answer-1.md` (trigger-gated), then writes `$PM_CHILD_RESULT_PATH` with status `complete` and idles: (a) `launch-child` creates branch `B` at `REF` and worktree `PATH`, writes `child.json`, and the session exists on the socket; the fake recorded `PATH` as its cwd, the socket as `PM_TMUX_SOCKET`, and no `PM_RUN_TOKEN` although the test set it; (b) `wait-child --wait 30` returns early with the question text; `answer-child` writes `cm-answer-1.md` and archives `cm-question-1.md`; a second `wait-child --wait 30` returns with `result present` and `status=complete`; (c) refusals: existing branch, existing path, unresolvable base, blank prompt file, a `--socket` containing `/`, each creating nothing; `answer-child` with no open question; (d) `stop-child` writes `pane.txt` and the session no longer exists; (e) `sessions.kill_server` on a socket with no server returns normally; (f) the caller's `PM_TMUX_SOCKET` is unchanged after each command; (g) `wait-child` with no `child.json` exits 2; (h) a branch name containing `/` and `.` yields sanitised session and socket names.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_campaign test_sessions test_prompts test_slice_ops`; the full PM suite, run exactly as in Conventions; `python3 -m py_compile skills/project-manager/scripts/pm.py skills/project-manager/scripts/pm_lib/*.py`.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: `--help` for the four commands matches the README rows; in a scratch repository outside this one, `launch-child` a fake child, `tmux -L <socket> attach` to watch it, then `stop-child`.

### Rollback Path

- Revert the slice commit. Worktrees, branches and tmux servers created by trial launches stay behind and are removed by hand (`git worktree remove`, `git branch -D`, `tmux -L <socket> kill-server`); no run state is involved.

## Slice 5: `implementation-plan` SKILL.md: campaign-child paragraph, small plans, protected paths

### Intended Change

- The Mode B launcher's `Plan defects:` paragraph (`skills/implementation-plan/SKILL.md:262`) is not changed: its delegated-decision rule stays for every run (D16).
- The Mode B launcher gains, directly after its `Run tag (optional): <tag>` line (`SKILL.md:242`) and inside the same fenced block, an optional block of at most 12 lines, clearly marked: `Campaign child (include this paragraph only when a Campaign Manager launched this run; delete it otherwise):` followed by:
  - nobody is present and nothing is confirmed with a human;
  - the plan is committed in the dossier at the path given, which meets the "plan committed" requirement (not the product repository);
  - skip the confirm step and, after `check-plan` and the repo check, run `init` yourself with `--branch <branch>`, `--policy <protected-paths policy file, when given>`, `--run-tag <campaign tag>`, `--max-attempts <N>` and full model ids, and hold the token;
  - keep HANDOFF.md as usual (it is archived with your run);
  - resolve natural plan defects yourself, as the `Plan defects` paragraph says;
  - a decision the Campaign Manager can give in a line (a reading the plan leaves genuinely open, a tolerance) goes to `.pm/campaign/cm-question.md` with the options and your recommendation; then wait for the one-line pointer to the answer;
  - a human gate, a floor failure you cannot steer within budget, an integrity stop, a dialog or an exhausted budget ends the run;
  - when the run is decided, run `status --report`, confirm the ledger was written, write `.pm/campaign/child-result.json` as `{"status": "complete" | "stopped" | "needs-human", "run_id": "<run id, or null when init never succeeded>", "summary": "<one paragraph>"}`, write the final report as your last message, and stay idle;
  - where this paragraph contradicts the confirm or resume steps above, it wins.
  Wording may be tightened.
- *Slice Granularity* (`SKILL.md:26-36`) gains one sentence: a plan of one or two slices, or one whose only deliverable is a committed report (an evidence node under campaign mode), is a normal plan, with the same receipt shape and nothing extra for `check-plan`.
- *Choosing an Authorized Surface* (`SKILL.md:123-129`) gains one sentence: when a run freezes a protected-paths policy file (`check-plan --policy`, `init --policy`), an authorized entry whose literal prefix overlaps a protected path is refused at plan time, so write entries whose literal directory is disjoint from every protected path.

### Acceptance Criteria

- [ ] The launcher's `Plan defects:` paragraph is byte-identical to its text at `before_head`.
- [ ] The campaign-child block sits immediately after the `Run tag` line inside the Mode B fenced block, is marked optional with the deletion instruction, and is at most 12 lines.
- [ ] The campaign-child block contains every element listed above.
- [ ] The block names `.pm/campaign/cm-question.md` and `.pm/campaign/child-result.json` literally.
- [ ] The block gives the `child-result.json` shape literally, as `{"status": "complete" | "stopped" | "needs-human", "run_id": "<run id, or null when init never succeeded>", "summary": "<one paragraph>"}`.
- [ ] Every flag the block names exists on `pm.py init` (`--branch`, `--policy`, `--run-tag`, `--max-attempts`), checked against `python3 skills/project-manager/scripts/pm.py init --help`.
- [ ] *Slice Granularity* gains exactly the one sentence described.
- [ ] *Choosing an Authorized Surface* gains exactly the one sentence described.
- [ ] The file's fences all close and no slice-like heading appears inside a fence.
- [ ] No other text in the file changes.
- Behaviour that must not change: the Mode A launchers; the Mode B launcher outside the inserted block; the receipt template; the machine-consumed field rules.

### Authorized Surface

- Files allowed to change:
  - skills/implementation-plan/SKILL.md
- Functions/classes/components allowed to change: one inserted block in the Mode B launcher; one sentence in each of *Slice Granularity* and *Choosing an Authorized Surface*.
- Tests allowed or expected to change: none

### Explicit Non-Goals

- No change to the `Plan defects:` paragraph (D16; `project-manager`'s SKILL.md is aligned to it in Slice 6).
- No campaign launcher here (it lives in `campaign-mode.md`, Slice 8).
- No change to the Mode A launchers, the planning receipt, or *Machine-Consumed Fields*.
- No change to `skills/project-manager/SKILL.md`.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: moderate
- Recommended Developer: claude, claude-sonnet-5-5, high effort (wording precision: every plan copies this launcher)

### Validation Plan

- Tests to add/update: none.
- Commands to run: `python3 skills/project-manager/scripts/pm.py init --help`; paste the updated Mode B launcher into a scratch copy of a valid plan outside the repository and run `python3 skills/project-manager/scripts/pm.py check-plan --plan <scratch>`, expecting no new error; `git diff <before_head> --stat` shows only this file.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: read the launcher end to end as a child PM would, with and without the campaign-child block, and confirm each reads as one coherent instruction set.

### Rollback Path

- Revert the slice commit.

## Slice 6: `project-manager` SKILL.md: align *Reading a defective plan* with the launcher

### Intended Change

- This is a deliberate loosening of the PM contract text that the owner chose (D16): the Mode B launcher every run starts from already lets a PM resolve natural plan defects as delegated decisions, and `skills/project-manager/SKILL.md` still states the stricter rule. After this slice the two agree; the launcher (`implementation-plan/SKILL.md:262`) is not touched.
- *Reading a defective plan* (`SKILL.md:42-55`): replace the four-condition exception (`SKILL.md:46-52`), together with the stop-by-default sentences that frame it ("**A defective plan stops the run.** Repairing one is an amendment, and amendments are the human's." and the stop-with-write-up sentence at `:44`; "Everything else stops. Choosing between readings … is amending, not resolving." at `:53`), with the delegated-decision rule: a PM resolves a defect where the plan's goals make one resolution natural and its impact stays within the slice or touches only a few slices, and records it as a delegated plan decision in the assessment and HANDOFF.md; it never edits the plan, widens the surface except by grant, or relaxes a floor fact; it stops for anything serious, contested or cross-cutting, and whenever the floor or a plan gate needs a human. Keep the opening list of the kinds of defect plans carry. Keep `SKILL.md:53`'s "Also stop for:" list verbatim (ambiguity touching authorization, acceptance criteria or risk flags; conflicting goals; a fix needing new semantic authority; a path that cannot meet the surface-grant rule; one plan section that keeps costing attempts; two reviews reaching opposite conclusions) as the definition of "serious, contested or cross-cutting". Add one sentence: a delegated decision chooses between readings and never adds scope.
- Keep the closing paragraph (`SKILL.md:55`) on rejecting a reviewer `FAIL` that rests only on the defective text, carrying each resolved defect in `notes.md`, and passing settled rulings to reviewers as adjudications (`--adjudicated`, `--adjudicated-file`); only its opening words "On a defect the plan resolves" change, to "On a defect you resolve".
- The Charter (`SKILL.md:12`): "resolve a plan defect on the record only where the plan itself resolves it (see *Reading a defective plan*)" becomes "resolve a plan defect on the record where the plan's goals make one resolution natural (see *Reading a defective plan*)".
- The Charter's job sentence (`SKILL.md:16`, "gate hard on the second, and hand the first to the human") gains, after "hand the first to the human", the qualifier "unless *Reading a defective plan* lets you resolve it as a delegated decision", so the Charter does not contradict the section.
- The Charter's "You may never" list (`SKILL.md:14`): the phrase "or supply a rule the plan never states" becomes "or invent scope the plan never states" (unconditional), so a delegated decision, which chooses between readings, is not forbidden by the list above the section that permits it; nothing else in that list changes.

### Acceptance Criteria

- [ ] *Reading a defective plan* states the delegated-decision rule in substance as the launcher's `Plan defects:` paragraph states it: natural resolution, impact within the slice or a few slices, recorded as a delegated plan decision.
- [ ] The section says where a delegated decision is recorded: the assessment and HANDOFF.md.
- [ ] The section keeps the three prohibitions: never edit the plan, never widen the surface except by grant, never relax a floor fact.
- [ ] The section keeps the stop conditions: anything serious, contested or cross-cutting, and whenever the floor or a plan gate needs a human.
- [ ] `SKILL.md:53`'s "Also stop for:" list survives verbatim and is named as the definition of "serious, contested or cross-cutting".
- [ ] The section says in one sentence that a delegated decision chooses between readings and never adds scope.
- [ ] The phrase "exactly one implementable reading" no longer gates resolution anywhere in SKILL.md.
- [ ] The paragraph on rejecting a reviewer `FAIL`, the `notes.md` carry and the adjudications survives with only its opening words changed.
- [ ] The Charter's `SKILL.md:12` sentence reads as given above.
- [ ] The Charter's `SKILL.md:16` sentence carries the qualifier above.
- [ ] The Charter's `SKILL.md:14` list reads "or invent scope the plan never states" in place of "or supply a rule the plan never states", and is otherwise unchanged.
- [ ] No other text in SKILL.md changes.
- [ ] `implementation-plan/SKILL.md:262` is unchanged, and the two texts now agree.
- Behaviour that must not change: the floor, the Charter's prohibitions other than the one phrase named, the workflow, and the stop rules.

### Authorized Surface

- Files allowed to change:
  - skills/project-manager/SKILL.md
- Functions/classes/components allowed to change: the *Reading a defective plan* section; the Charter sentences at `SKILL.md:12`, `SKILL.md:14` (one phrase) and `SKILL.md:16`.
- Tests allowed or expected to change: none

### Explicit Non-Goals

- No change to the Mode B launcher or any other file.
- No change to the Developer's own contract-defect rule in `references/developer-prompt.md`.
- No change to the Charter's "You may never" list beyond the one phrase named above.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: easy
- Recommended Developer: claude, claude-sonnet-5-5, high effort

### Validation Plan

- Tests to add/update: none.
- Commands to run: `git diff <before_head> --stat` shows only `skills/project-manager/SKILL.md`; `grep -n "exactly one implementable reading" skills/project-manager/SKILL.md` finds no gating use; `git diff <before_head> -- skills/implementation-plan/SKILL.md` is empty.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: read the launcher's `Plan defects:` paragraph beside the new section and confirm a PM following either acts the same way.

### Rollback Path

- Revert the slice commit; the skill text then again states the stricter rule than the launcher.

## Slice 7: VISION revision for campaign mode

### Intended Change

- `docs/VISION.md`, one deliberate revision in two places only, landing before the campaign reference whose authority it governs (D10).
- *The Roles* (`VISION.md:37`): directly after the sentence "PM never writes slice code and never authors or expands plans.", insert: "In campaign mode one exception is deliberate: a PM seat, the Campaign Manager, writes node plans within a brief the owner approved, child PMs supervise those plans, and the human-approved contract moves up one level, from the plan to the brief; a PM in plan mode keeps the prohibition. A node plan's risk and approval flags are then written by the Campaign Manager against the brief, so principle 4's "the plan declares risk" rests on that seat's judgement within the brief, with reserved classes approved by the owner in the dossier."
- *Non-Goals* (`VISION.md:93`): in the "Not a planner-free autopilot" bullet, replace the sentence "Nothing here invents or repairs plans on the fly." with: "Nothing here invents or repairs plans mid-implementation: a plan is written before implementation and approved by a human — or, in campaign mode, written by a Campaign Manager inside a brief the human approved, with every target and tolerance the owner's." The bullet's remaining sentences stay.

### Acceptance Criteria

- [ ] *The Roles* paragraph carries the two inserted sentences above, verbatim, directly after "PM never writes slice code and never authors or expands plans.".
- [ ] The "Not a planner-free autopilot" bullet no longer contains "Nothing here invents or repairs plans on the fly." as a standalone sentence and carries the replacement sentence above, verbatim.
- [ ] `git diff <before_head> -- docs/VISION.md` touches only those two paragraphs, and no other VISION text changes.
- Behaviour that must not change: every VISION principle, guarantee, ladder rung and the stability note.

### Authorized Surface

- Files allowed to change:
  - docs/VISION.md
- Functions/classes/components allowed to change: the *The Roles* PM paragraph and the "Not a planner-free autopilot" bullet.
- Tests allowed or expected to change: none

### Explicit Non-Goals

- No other VISION change: principles, guarantees, ladder and stability note stay as they are.
- No CONTRIBUTING, CHANGELOG or README change (Slice 10).

### Risk Flags

- Risky surfaces touched: the governing document (`docs/VISION.md`); no code, state or CLI
- Approval needed before implementation: yes
- Independent audit required: no
- Difficulty: easy
- Recommended Developer: claude, claude-opus-5-5, high effort (the governing document; wording is fixed in this receipt)

### Validation Plan

- Tests to add/update: none.
- Commands to run: `git diff <before_head> --stat` shows only `docs/VISION.md`; `git diff <before_head> -- docs/VISION.md` shows only the two paragraphs.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: read the revised paragraphs in context and confirm the rest of VISION still reads consistently with them.

### Rollback Path

- Revert the slice commit. Slices 8–10 cite the revision; a revert after them needs their pointers amended.


## Slice 8: Campaign-mode reference, dossier template, skill pointer

### Intended Change

- New `skills/project-manager/references/campaign-mode.md` (about 200 lines, prose unwrapped, no new mechanism), the CM's operating contract. Contents, in this order:
  - **Purpose and authority.** The CM's planning authority comes from an owner-approved `brief.md`, frozen by a digest the owner supplies when starting or resuming the CM, with a pointer to `docs/VISION.md` *The Roles*, which records the revision (Slice 7).
  - **Seats.** A table with *Authority* and *Never* columns: Owner; CM (`project-manager`, campaign mode; never implements, supervises a plan it wrote, changes `brief.md`, answers a human gate, or loosens a target or tolerance); child PM (`project-manager`, plan mode, unchanged; never writes plans or adds nodes, so depth is fixed at two); Developers and reviewers (unchanged); read-only delegate (`orchestrator`; never edits, builds, tests or commits).
  - **The dossier.** Its files and owners; node plans and per-child protected-paths policy files live in the dossier and are committed there before launch; the CM has no HANDOFF.md of its own, because `work.md` *Now* is its handoff; the two goalpost rules (decisions use permissions, never create them; `brief.md` is protected by its owner-supplied digest, checked with `shasum -a 256 brief.md`); reconcile inputs (`brief.md` against the digest, `work.md`, `git worktree list`, each in-flight worktree's token-free `pm status` and `wait-child` output, every child `run-report.md`) and its four confirmations (every target has work, a passing receipt at the integration head, or an open gap; every node traces to a target; no failure has silently disappeared; budgets hold); restart is reconcile.
  - **Nodes.** Change, evidence and task kinds (a table); the dividing line (executing, building, testing, generating outputs or changing state needs a plan node; reading is a task node); delegate `files` must sit beneath the delegate's repository, so dossier excerpts go inline; plan review is a read-only task node embedding `implementation-plan`, plus `check-plan --policy`; node plan conventions: a reserved node's plan says `Approval needed before implementation: no` and names the `decisions.md` id that approved it, surfaces stay disjoint from protected paths, an evidence node's surface is its report path `campaign-reports/<node>.md`, run tag `campaign/<id>`, branch `campaign/<id>/<node>`.
  - **The loop and its rules.** The loop as a numbered list, then these rules, one or two sentences each:
    - Judge once, from the child's `run-report.md`, assessments, Developer judgments, its recorded delegated plan decisions, the residuals in its HANDOFF.md, and the whole node diff, which is always `<integration head>...<branch tip>`; a wrong delegated decision is corrected through a later node. A follow-up node starts from the integration head by default; when it starts from a stopped node's branch head, the inherited unaccepted commits are part of the diff the CM judges and reviews before any merge. The CM commissions its own review only when: (a) the node is a reserved class (one `drift-audit` of the node diff against the brief's protections); (b) the child's report carries tolerances or plan defects the CM cannot verify itself; (c) the child scored its Developer 0, or stopped, where the review diagnoses the stop for re-planning and never readies the node for a merge; (d) the question is new: an interaction between nodes, or an instrument's validity.
    - Merges are recorded in `work.md` before they happen and done one at a time with `--no-ff`. Merge only a node whose child run is `complete` and whose branch tip is its last accepted slice commit (a stopped or still-`active` child's tip may carry unaccepted work). A follow-up node starts from a stopped node's branch head only when `git diff --name-only --no-renames <integration head>...<stopped head>` touches no protected path (no floor ever re-checks inherited commits); otherwise it starts from the integration head.
    - On a merge conflict, `git merge --abort`, then rerun the node from the new integration head under a new node id and branch; never rebase an accepted branch.
    - Stopped children end their node and are never resumed; the CM plans a follow-up node from the branch head when useful.
    - Cleanup of a finished or dead child, in this order: run `stop-child` (it captures the pane, a dead pane as the placeholder `capture_to` writes, kills the server and prints the reviewer process groups); check by hand for those reviewer process groups, which sit outside the child's tmux server; copy HANDOFF.md, `.pm/campaign/`, `.pm/runs/<id>/` and the authoritative state root `<worktree-git-dir>/pm/<run-id>/` (`run.json`, `events.jsonl`, the assessments) into `validation/<node>/`; then `git worktree remove`, keeping the branch. An unscoped `stop --scavenge` or a bare `tmux kill-server` is never used.
    - Reserved approvals move up to `decisions.md`, recorded before launch.
    - Budgets per target and campaign-wide; two failed attempts at one target is a question for the owner.
    - Child output stays out of git's way (git-ignored output directories or the slice artifact directory). A brief may name the git-ignored output directories a product uses; the toolkit never reads them.
  - **Launching and watching a child.** Fill the Mode B launcher (`implementation-plan`'s SKILL.md) with its campaign-child paragraph, write it to the dossier, run `pm launch-child …`, then one long background `wait-child --wait N` (never poll). On a question: decide, record it in `decisions.md` when the answer uses a `brief.md` permission, `answer-child`, and wait again. On a dialog marker: read it as *Reading the pane* in PM's SKILL.md says (`SKILL.md:24-28`): a dialog the child is waiting on goes to a human; ordinary output that merely contains a marker string (a child PM's pane routinely shows PM vocabulary and Developer pane tails) does not, and the CM waits again. On the result (`.pm/campaign/child-result.json`): judge. A human may `tmux -L <socket> attach` at any time to watch the child and its Developers. Node worktrees are created under one parent directory the owner has trusted through each harness's own mechanism, because an interactive harness may show a directory-trust dialog for a new path and `wait_until_ready` fails closed on it (`sessions.py:336-341`); a trust prompt at launch is a question for the owner. One protected-paths policy file per child, recorded in `work.md`; every policy handed to a child lists the product repository's committed policy file path explicitly, because self-protection covers only a policy file inside the worktree; an instrument-class node is launched with a policy that omits the instrument it builds; the policy actually used is readable in the child's `run.json` at `<worktree-git-dir>/pm/<run-id>/run.json`.
  - **Validation.** Trust before use: an instrument counts only after it passes a known-good control and fails a known-bad one, and its verdict is pass, fail or error. Independence: instruments are built in owner-approved nodes and then protected. Measurement ownership: the CM measures through evidence nodes run by a child PM at a recorded SHA; an implementing Developer's claim of success points at evidence and is never the evidence. The ratchet: a target that passed must keep passing — cheap checks re-run at every new integration head, expensive ones at milestones, acceptance needs a final evidence node at the final head, and every receipt names the measured product SHA separately from the commit that adds the report. Receipt fields: measured product SHA, instrument version, configuration, command, outcome, artifact hashes.
  - **Questions to the owner.** Recorded in `work.md` with the nodes they block, the evidence, the options, a recommendation, and what continues meanwhile.
  - **Acceptance and retrospective.** A final evidence node at the final head; an independent audit as a read-only task node on a model different from the CM's; owner sign-off; the retrospective list (owner attention, questions asked and whether each was necessary, child stop causes, plan versions per node and nodes per target, spend and wall time).
  - **Worktrees and parallelism.** One short paragraph: serial first (`max_parallel: 1` in `brief.md`); going parallel is launching a second child before waiting, under disjoint surfaces, exclusive resources respected and merges one at a time, with per-child sockets and ledgers already holding.
  - **The campaign launcher.** One copyable fenced block, its only copy, with fields for the dossier path, the brief digest, the product repo path and integration branch, the protected-paths policy file path, the child PM seat (harness, full model id, effort), the child's seats (Developer, drift auditor, code reviewers, each with a full model id and effort), and the node wait length for `wait-child`; and the CM instructions: reconcile first, then loop; never implement, never supervise a plan you wrote, never change `brief.md`, never answer a human gate, never loosen a target.
- New `skills/project-manager/references/dossier-template.md`: the dossier layout (`brief.md`, `work.md`, `evidence.md`, `decisions.md`, `validation/` with one `validation/<node>/` per node, plus the committed node plans and per-child policy files) and a template for each file: `brief.md` (goal; targets for done with tolerances; pinned references with SHAs; allowed paths, and the protected-paths policy file path with its pinned SHA, which is the only place protected paths are listed; reserved decision classes; budgets: node budget per target, campaign-wide node and spend limits, `max_parallel`; exclusions: paths the campaign may not read); `work.md` (`## Now`: in-flight nodes, next action, blockers, open questions; `## Nodes` table: id, kind, objective, target, dependencies, surface, resources, status, branch, worktree, socket, session, base SHA, head SHA, policy, report); `evidence.md` entry shape (finding, source `file:line`, SHA read at, uncertainty); `decisions.md` entry shape (id, actor, decision, grounds, the `brief.md` permission used); `validation/` layout and the receipt shape. It is a template that points at `campaign-mode.md` for the rules, never a second copy of them.
- `skills/project-manager/SKILL.md`: the Charter (`SKILL.md:10-16`) gains one sentence: in campaign mode a PM (the Campaign Manager) writes node plans under an owner-approved `brief.md`, which is the only source of that authority, and the charter above binds every PM in plan mode unchanged; *References* (`SKILL.md:95-97`) gains a pointer to `references/campaign-mode.md`. At most five lines added in total.
- `skills/project-manager/README.md`: a `## Campaign mode` section directly after `## Launcher` (`README.md:5-7`), at most 12 lines, pointing at `references/campaign-mode.md` and listing the toolkit touchpoints: `--policy` on `check-plan` and `init`; the four child commands; the per-child tmux socket; the question channel.

### Acceptance Criteria

- [ ] `campaign-mode.md` exists, is unwrapped prose, and has a section for each item listed above, in that order.
- [ ] The dossier section gives the digest check as a `shasum -a 256` command compared against the owner-supplied digest, and states that node plans and per-child policy files are committed in the dossier before launch.
- [ ] The dossier section states the two goalpost rules, the reconcile inputs and the four confirmations.
- [ ] The dossier section states that the CM has no HANDOFF.md and that `work.md` *Now* is its handoff.
- [ ] The node section states the plan-versus-task dividing line and every node plan convention listed above (reserved-node approval line naming the decision id, disjoint surfaces, `campaign-reports/<node>.md`, run tag `campaign/<id>`, branch `campaign/<id>/<node>`).
- [ ] The judge-once rule lists exactly the four exceptions (a)–(d) above.
- [ ] The judge-once rule defines the node diff as `<integration head>...<branch tip>` and states that a follow-up node started from a stopped node's branch head carries the inherited unaccepted commits into that diff, judged and reviewed before any merge.
- [ ] The merge rules allow a follow-up from a stopped head only when the diff `<integration head>...<stopped head>` touches no protected path, and otherwise start it from the integration head.
- [ ] The judge-once rule reads the child's delegated plan decisions and HANDOFF.md residuals, and corrects a wrong decision through a later node.
- [ ] The merge rules state: intent recorded before merging, one at a time, `--no-ff`.
- [ ] The merge rules state: merge only a `complete` child whose branch tip is its last accepted slice commit.
- [ ] The merge rules state: on conflict abort and rerun under a new node id and branch, never rebasing an accepted branch.
- [ ] The cleanup rule runs `stop-child` first for every finished or dead child, then the by-hand reviewer check, then the copy, then `git worktree remove`, keeping the branch.
- [ ] The cleanup copy covers HANDOFF.md, `.pm/campaign/`, `.pm/runs/<id>/` and the state root `<worktree-git-dir>/pm/<run-id>/` into `validation/<node>/`.
- [ ] An unscoped `stop --scavenge` or a bare `tmux kill-server` appears only as a prohibition.
- [ ] The launching section gives the `launch-child`, background `wait-child`, `answer-child` loop and `tmux -L <socket> attach`.
- [ ] The launching section reads a dialog marker as *Reading the pane* says: a dialog the child waits on goes to a human, and ordinary output containing a marker string does not.
- [ ] The launching section records an answer in `decisions.md` when it uses a `brief.md` permission.
- [ ] The launching section requires node worktrees under one owner-trusted parent directory and treats a trust prompt at launch as a question for the owner.
- [ ] The launching section requires every child policy to list the product repository's committed policy file path explicitly, and gives the child `run.json` location.
- [ ] The validation section states trust before use, independence, measurement ownership, the ratchet (including the measured product SHA kept separate from the report commit) and every receipt field listed above.
- [ ] *Worktrees and parallelism* is one short paragraph with the content above.
- [ ] Every `pm.py` command the reference shows exists with the flags shown, checked against each command's `--help`.
- [ ] The campaign launcher is a single fenced block in `campaign-mode.md`, carries every field (including the child PM seat and the node wait length) and every CM instruction listed above, and contains no slice-like heading.
- [ ] No other file contains a copy of the campaign launcher.
- [ ] `dossier-template.md` holds the layout and one template per dossier file with the fields listed above, and restates none of the rules.
- [ ] The PM SKILL.md gains the Charter sentence and the References pointer and nothing else, at most five lines in total.
- [ ] The PM README gains the `## Campaign mode` section after `## Launcher`, at most 12 lines, naming the touchpoints above.
- [ ] No new or edited text names a particular product, model package, or domain-specific instrument.
- [ ] Every relative link in the new and edited files resolves.
- Behaviour that must not change: the PM SKILL.md's plan-mode charter, floor, workflow and stop rules.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/references/campaign-mode.md` (new file)
  - `skills/project-manager/references/dossier-template.md` (new file)
  - skills/project-manager/SKILL.md
  - skills/project-manager/README.md
- Functions/classes/components allowed to change: the two new references; one Charter sentence and one References pointer in SKILL.md; one new README section.
- Tests allowed or expected to change: none

### Explicit Non-Goals

- No new mechanism: no command, flag or state the toolkit does not already have after Slices 1–4.
- No second copy of the Mode B launcher; the child prompt is filled from `implementation-plan`'s SKILL.md.
- No stage-1 material: the README trial and the "First campaign: the scratch test" subsection are Slice 9.
- No CONTRIBUTING, CHANGELOG or root README change (Slice 10).
- No domain content and no product protected-paths policy file.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: hard
- Recommended Developer: claude, claude-opus-5-5, high effort (judgement-heavy prose that becomes the CM's operating contract)

### Validation Plan

- Tests to add/update: none.
- Commands to run: `--help` for every `pm.py` command the reference names (`check-plan`, `init`, `launch-child`, `wait-child`, `answer-child`, `stop-child`, `status`); a link check over the four files (every relative link target exists); `grep -n '^[ ]\{0,3\}#\{1,6\} \+[Ss]lice' skills/project-manager/references/campaign-mode.md` finds nothing; the no-baggage greps from `.github/workflows/ci.yml:101-124` run locally and find nothing.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: read `campaign-mode.md` once as a fresh CM with only the dossier and confirm every loop step names the command or file it uses.

### Rollback Path

- Revert the slice commit.

## Slice 9: Stage 1: model-free README trial and the first-campaign test

### Intended Change

- `skills/project-manager/README.md`: a new section `## Verify campaign mode (no real model, ~2 minutes)`, placed directly after `## Verify your setup (no real model, ~1 minute)` (`README.md:141-202`) and before `## Maintainer map` (`README.md:204`), about 30 lines in the shape of "Verify your setup": a scratch product repository with a committed `instruments/check_t1.py` (any content; the trial never runs it) and a committed `campaign-policy.json` (a protected-paths policy file protecting `instruments/`); `check-plan --policy` refusing a node plan whose surface reaches `instruments/`; `launch-child` with a fake `--harness-command` child in tmux that writes `$PM_CM_QUESTION_PATH`, waits for its answer, then writes `$PM_CHILD_RESULT_PATH`; `wait-child` returning on the question; `answer-child`; `wait-child` returning on the result; `stop-child`; the socket taken from the launch line.
- `skills/project-manager/references/campaign-mode.md`: a short subsection `### First campaign: the scratch test`, placed after *Acceptance and retrospective*, covering: setup (a scratch product repository and dossier, with two targets — T1, a function with a stated output measured by the instrument; T2, the suite passing at the final head — and `max_parallel: 1`); the node sequence (N1 change node → merge → ratchet; kill the CM between N1 and N2; restart from the dossier; N2 evidence node, report copied into `validation/`, branch never merged); that the child asks the CM at most once per node in the scratch test, by design; a check that a new worktree under the trusted parent launches without a trust dialog; fault injections (a node plan reaching `instruments/` is refused by `check-plan --policy`; an edit to `brief.md` is caught by the digest at reconcile); no-go criteria (a child cannot run unattended to completion; restart loses or repeats work); and the measures, by pointing at the retrospective list rather than copying it. It states that the real-model run is the owner's (D9) and points at the README trial for the model-free part, without repeating it.

### Acceptance Criteria

- [ ] The README section sits after "Verify your setup" and before "Maintainer map" and is about 30 lines.
- [ ] The README trial contains the scratch repository with the protected instrument file, the committed `campaign-policy.json`, and the `check-plan --policy` refusal.
- [ ] The README trial contains the fake-child sequence: `launch-child`, `wait-child` on the question, `answer-child`, `wait-child` on the result, and `stop-child`, with the socket taken from the launch line.
- [ ] Every `pm.py` command and flag in the README trial exists, checked against `--help`.
- [ ] The Developer ran the README trial as written, in a scratch directory outside this repository, and `validation.md` carries its commands and output.
- [ ] `campaign-mode.md` gains the "First campaign: the scratch test" subsection with the setup, the node sequence and the at-most-one-question-per-node design.
- [ ] The subsection has stage 1 confirm that a new worktree under the trusted parent launches without a trust dialog.
- [ ] The subsection names the two fault injections and the no-go criteria above.
- [ ] The subsection points at the retrospective list for its measures and at the README trial for the model-free part, copying neither.
- [ ] The subsection states that the real-model run is the owner's.
- [ ] No text names a particular product, model package, or domain-specific instrument beyond the scratch campaign's own.
- Behaviour that must not change: the existing "Verify your setup" trial; the rest of `campaign-mode.md`.

### Authorized Surface

- Files allowed to change:
  - skills/project-manager/README.md
  - skills/project-manager/references/campaign-mode.md
- Functions/classes/components allowed to change: one new README section; one new `campaign-mode.md` subsection.
- Tests allowed or expected to change: none

### Explicit Non-Goals

- No real-model run by the Developer (D9).
- No scratch file inside this repository: all scratch repositories, plans and logs live outside it (the slice's own `validation.md` in its artifact directory is the exception).
- No change to the campaign launcher, the rest of `campaign-mode.md`, or any code.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: moderate
- Recommended Developer: claude, claude-sonnet-5-5, high effort

### Validation Plan

- Tests to add/update: none.
- Commands to run: the README trial, as written, in a scratch directory outside this repository with `PM_LEDGER_DIR` set to a scratch value; `--help` for every `pm.py` command named; output recorded in `validation.md`.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: `git status` in this repository is clean apart from the two files after the trial, and `tmux -L <socket> ls` reports no server after `stop-child`.

### Rollback Path

- Revert the slice commit; remove the scratch directory by hand.

## Slice 10: CONTRIBUTING map, CHANGELOG, root README

### Intended Change

- `CONTRIBUTING.md` source-of-truth map (`CONTRIBUTING.md:13-31`): three new rows: "Campaign-mode operating contract and the campaign launcher" → `skills/project-manager/references/campaign-mode.md`; "Dossier layout and templates" → `skills/project-manager/references/dossier-template.md`; "Protected-paths policy file contract" → `skills/project-manager/references/run-state.md`.
- `CHANGELOG.md` `[Unreleased]` → `### Added` (`CHANGELOG.md:5-7`): one entry in the house style (a bold lead sentence, then the substance): campaign mode; protected paths through `--policy` on `check-plan` and `init`, with `grant` and floor enforcement (including both sides of a rename); the question channel in both loops (`pm answer` for a Developer's question, `answer-child` for a child PM's); the four child commands (`launch-child`, `wait-child`, `answer-child`, `stop-child`) running a child PM in its own tmux session; HANDOFF.md kept for child PMs; the Mode B launcher's campaign-child paragraph; `project-manager`'s SKILL.md aligned to the launcher's delegated plan-defect decisions; the VISION revision; and the review provenance: the plan was reviewed over four rounds by an independent panel (Codex `gpt-6.1-sol` high and Claude `claude-opus-5-5` high, via `orchestrator`).
- Root `README.md`: glossary entries (`README.md:115-131`) for Campaign Manager, campaign mode, dossier and brief, node, protected path, and question (a one-line decision a session asks its supervisor through a file, answered without ending the session); the *Choosing a level* table (`README.md:104-109`) gains a row (a goal met across many plans and days, with an owner-approved brief → campaign mode under a Campaign Manager); the `project-manager` row of the skills table (`README.md:44`) gains "…or, in campaign mode, plans and runs many such plans against an owner-approved brief".

### Acceptance Criteria

- [ ] `CONTRIBUTING.md` has the three new rows with full repository paths, each of which exists.
- [ ] `CHANGELOG.md` has one new `### Added` entry under `[Unreleased]`, with a bold lead sentence first.
- [ ] The CHANGELOG entry covers every item listed above.
- [ ] The CHANGELOG entry's provenance names the plan-review panel exactly as given above and makes no other provenance claim.
- [ ] The root README glossary has the six new entries.
- [ ] The root README *Choosing a level* table has the new campaign-mode row.
- [ ] The root README `project-manager` skills-table row carries the campaign-mode extension.
- [ ] The CI doc-link reachability script (`.github/workflows/ci.yml:126-146`) passes when run locally.
- [ ] The four no-baggage greps (`.github/workflows/ci.yml:101-124`) pass when run locally.
- [ ] No other text in the three files changes.
- [ ] No text in the three files names a particular product, model package, or domain-specific instrument.
- Behaviour that must not change: every existing CONTRIBUTING row; every existing CHANGELOG entry.

### Authorized Surface

- Files allowed to change:
  - CONTRIBUTING.md
  - CHANGELOG.md
  - README.md
- Functions/classes/components allowed to change: three new CONTRIBUTING rows; one CHANGELOG entry; six glossary entries, one table row and one skills-table row in the root README.
- Tests allowed or expected to change: none

### Explicit Non-Goals

- No VISION change (Slice 7).
- No new skill row in the README skills table.
- No release tag or version bump.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: moderate
- Recommended Developer: claude, claude-sonnet-5-5, high effort

### Validation Plan

- Tests to add/update: none.
- Commands to run: the doc-link reachability script from `.github/workflows/ci.yml:128-146` run from the repository root; the four no-baggage `check` patterns from `ci.yml:103-124` run locally; `git diff <before_head> --stat` shows only the three files.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: read the new glossary entries beside the existing ones and confirm the terms match `campaign-mode.md`.

### Rollback Path

- Revert the slice commit.

## After the Plan

- Stage 1, the real-model scratch campaign, is the owner's run in a fresh CM session following the "First campaign: the scratch test" subsection that Slice 9 adds to `campaign-mode.md` (D9; the owner may override this and run it differently). Its go/no-go gates stage 2.
- Stage 2 and later (a minimal campaign in the first product repository, then the real work) live in that repository and are outside this plan, as is the product's protected-paths policy file.
- The groundwork the source brief lists (a replay corpus of past campaigns, measured costs for budgets, moving the held-out answer key off the campaign machine, choosing the isolation environment and the owner-question channel) is the owner's.
- Archive this plan file under `archive/` after the run, per repository convention.

## Next Chat Prompts

### Mode A — Assisted run (default, checkpointed)

```md
Plan file: docs/implementation-plan-pm-campaign-mode.md
Slices or batch this session: Slice 1
Reviewers (optional): drift auditor <harness> <full model id> <effort>; code reviewers <harness> <full model id> <effort>; …

Read the full plan file first. If a selected slice or batch receipt is incomplete or the plan state is unclear, stop and tell me before coding.

Work on the current feature branch for this plan; if none exists, create one and tell me the name.

Use orchestrator as the controlling skill. Act as the Developer: keep implementation, validation, Git operations, and commits local. Use a read-only Reviewer (the reviewers above, when given) only for investigation, evidence gathering, the hostile drift-audit skill, and an independent code-review skill pass. If no Reviewer is configured or available, perform Developer self-audit and record that provenance explicitly.

For each selected slice or batch, in plan order:
1. Restate the frozen contract (authorized surface + non-goals) from the plan.
2. If any included slice's Risk Flags mark approval-needed, stop and get my approval before coding.
3. apply the scoped-implementation skill against the selected contract.
4. apply the drift-audit skill using a read-only Reviewer when available; otherwise perform Developer self-audit. Report the authorization gate result and who performed it before any quality review.
5. If the gate passes: for a broad or structural change, first run the code-health skill differentially against the slice's starting commit and supply its report as review evidence. Then apply the code-review skill using the code reviewers when available, in parallel if there are several; otherwise perform Developer self-audit through the code-review skill. Record who performed it. If the drift gate fails, fix the drift and re-audit.
6. Collect every finding from the current audit or review round before fixing, surface them to me, fix them as one batch, then re-run the relevant gate. If consecutive reviews return only minor findings and have clearly converged, record residuals in the slice summary and proceed.
7. Ask me before committing. On my approval, commit the selected slice or batch with the commit skill.

After the selected slice(s) or batch are committed, use the handoff skill to record state, audit provenance (Reviewer tool/label or Developer self-audit and fallback context), and the next slice or batch to resume from. Do not continue past the selected scope.

Confirm before starting: plan file read, selected slice(s) or batch, branch, and the first slice. Then begin.
```

### Mode B — Supervised autonomy (alternative)

```md
Plan file: docs/implementation-plan-pm-campaign-mode.md
Repo: /Users/dcroton/Local/git-repos/ai-agent-coder
Developer: claude per plan   (model and effort per plan: each slice's Recommended Developer line)
Drift auditor: <harness> <full model id> <effort>
Code reviewers: <harness> <full model id> <effort>; <harness> <full model id> <effort>
Run tag (optional): pm-campaign-mode

Use the project-manager skill. You are the PM: the accountable supervisor of this run. You never write slice code.

Handoff: HANDOFF.md in the repo root, git-ignored (add it to .git/info/exclude if it is not), is the sole entry point for a restart and for my post-run review. If it names an unfinished run of this plan, read it fully and resume that run (I will supply the token). Otherwise create it after init. Update it after every slice decision and before any stop with: run identity (never the token), seats, branch, commit authorisation, review protocol, slice log, plan decisions, residuals and follow-ups, and PM lessons.

For a new run, prepare, then confirm with me once:
- Read the whole plan, run check-plan with repo context, and check that the repo still matches what the plan assumes. Init freezes the plan, so raise any drift or warning now.
- Require a clean worktree with the plan committed. Propose the plan's feature branch, or a new branch name to create.
- Confirm: plan read, check-plan result, seats, branch, first slice, any approval-flagged slices (each stops the run until I approve it), and my standing authorisation for Developers to commit each slice.
After my go-ahead, run init with full model ids, efforts where the harness supports them, and the run tag. Give me the run id and token, then begin.

Per slice, in plan order:
1. start-slice with the Developer seat's model and effort. Wait with one long background `observe --wait`; never poll.
2. Run finalize, then your own checks: pane, diff, validation output, full test suite, lint, differential code-health if the structure changed, and a style-guide audit (the project's guide, otherwise the baseline). Treat newly introduced, in-contract style deviations as cleanup to steer.
3. Run the review panel when the plan or risk calls for it. Run the drift audit first, then read and rate it. Then start all code reviewers ~90 s apart, sharing one `--adjudicated-file`. Let reviews run to completion without watching them.
4. Rate every report, order the code panel, and score the Developer (criteria met, confirmed defects).
5. Decide: accept, steer or stop. Steer only once every review is in, with all findings batched into one correction. Each follow-up round re-runs the panel, focused on the change since the last round through an adjudication line.
6. Update `pm notes` for the next Developer, and HANDOFF.md for me.

Plan defects: where the plan's goals make one resolution natural and its impact stays within the slice or touches only a few slices, decide it yourself and record it as a delegated plan decision. Never edit the plan, widen the surface except by grant, or relax a floor fact. Stop for anything serious, contested or cross-cutting, and whenever the floor or a plan gate needs a human.

Finish: run `status --report`, confirm the ledger was written, run `ledger render`, and read `run-report.md` end to end. Record the run's actual status in HANDOFF.md. Report:
- total run time, quoted from the report with its endpoints
- commits (unpushed)
- what was accepted, and on what evidence
- what stopped, and why
- plan defects, with the decisions you took and the amendments you recommend
- residual risks and follow-ups, with a convergence read
- the leaderboard path
```

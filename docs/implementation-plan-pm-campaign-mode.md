# Implementation Plan: PM Campaign Mode

**Status:** frozen, 2026-10-23, after two rounds of independent plan review (Codex `gpt-6.1-sol` high and Claude `claude-opus-5-5` high, via `orchestrator`; final verdicts MINOR ISSUES ONLY, all applied) · Source: a design brief the owner holds outside this repository (the first campaign's product repository); this plan is self-contained. · Base: `main` at `77f2941` · Every `path:line` below was verified against that commit.

## Goal

Extend the Project Manager (PM) into a *campaign mode* that turns an owner-approved goal into a validated outcome across many small plans and days, with bounded human attention. A PM seat in campaign mode, the Campaign Manager (CM), holds a dossier whose owner-approved `brief.md` is its only planning authority; it writes small node plans, launches an ordinary child PM per plan in its own worktree, judges the child's run from its report and the node diff, merges accepted change nodes one at a time, and repeats until every target in the brief passes. The toolkit gains exactly what that loop cannot do safely by convention: protected paths that no plan surface, grant or commit can reach, and a `launch-child` command that runs one unattended child PM to completion. Everything else is a reference document, a dossier template, a few sentences in two skills and one deliberate VISION revision. Every existing workflow (Mode A, Mode B on a single plan, orchestrator delegates) keeps working as today, with two deliberate changes: the Mode B launcher's plan-defect clause becomes the strict rule PM's SKILL.md already states (D16), and floor fact 5 sees both sides of a rename on every run (D17).

## Non-Goals

- No new skill, seat toolkit or state store: the CM is a `project-manager` seat, the dossier and git are its state, and there is no parent-side signed state.
- No parallel scheduler and no parallel runs in the first tests. Parallel execution is a convention (launch a second child before waiting), enabled only once the serial loop is boring.
- No `campaign` subcommand family beyond `launch-child`, and no `init --campaign`, node records or merge records inside `pm.py`.
- No Developer or Reviewer prompt rendering of protected paths: `check-plan --policy` keeps node surfaces disjoint, and floor fact 5 is the backstop.
- No change to `orchestrator`, `code-review`, `drift-audit`, `lint`, `commit` or `handoff`.
- No domain content: nothing in this repository names a particular product, model package or instrument, and no product's protected-paths policy file is part of this plan.
- No domain-specific gates (for example science gates for work without a reference implementation) and no calibration machinery; a campaign's instruments are built by its own nodes.
- No change to an upstream repository or remote that the brief does not permit; the CM may draft a patch and ask, never apply one.
- Not adversary-proof, in the same sense as the rest of the repository: it defends against drift, corner-cutting and overconfidence, not a determined fabricator.

## What Changes

| Where | Change | Size | New or adapted |
|---|---|---|---|
| `pm_lib/plan.py`, `pm_lib/cli.py`, `tests/test_plan.py` | Protected-paths policy file loader (JSON, self-protecting); plan-time overlap refusal; `check-plan --policy` | ~120 lines + tests | Adapted (Slice 1) |
| `pm_lib/state.py`, `pm_lib/slice_ops.py`, `pm_lib/floor.py`, `pm_lib/cli.py`, `pm_lib/plan.py`, `pm_lib/git_ops.py`, four test modules, `references/run-state.md`, `README.md`, `SKILL.md` | `init --policy` freezes protected paths into signed state; `grant` refuses them; floor fact 5 fails on them; changed-file lists report both sides of a rename | ~90 lines + tests + docs | Adapted (Slice 2) |
| `pm_lib/profiles.py`, `pm_lib/review.py`, `tests/test_profiles.py` | One-shot harness command table single-sourced in `profiles.py`, with a read-only and a full-autonomy form | ~net 40 lines | Adapted (Slice 3) |
| `pm_lib/campaign.py` (new), `pm_lib/cli.py`, `pm_lib/sessions.py`, `tests/test_campaign.py` (new), `README.md`, `references/run-state.md` | `pm launch-child`: create a node worktree and branch, run one child PM one-shot under its own tmux socket, kill it on timeout, summarise its run | ~170 lines + tests | New (Slice 4) |
| `skills/implementation-plan/SKILL.md` | Strict plan-defect clause in the Mode B launcher (D16); optional campaign-child paragraph; small plans are normal; protected-path surface rule | ~25 lines | Adapted (Slice 5) |
| `docs/VISION.md` | The deliberate campaign-mode revision in *The Roles* and *Non-Goals* | ~5 lines | Adapted (Slice 6, approval-gated) |
| `references/campaign-mode.md` (new), `references/dossier-template.md` (new), PM `SKILL.md`, PM `README.md` | The CM's operating contract and the campaign launcher (its only copy); dossier templates; pointers | ~300 lines of Markdown | New (Slice 7) |
| PM `README.md`, `references/campaign-mode.md` | Stage 1: a model-free trial in the README and the first-campaign test in the reference | ~50 lines | New sections (Slice 8) |
| `CONTRIBUTING.md`, `CHANGELOG.md`, `README.md` (root) | Source-of-truth rows; changelog entry; glossary, level table and skill row | ~25 lines | Adapted (Slice 9) |
| `orchestrator`, `code-review`, `drift-audit`, `lint`, `commit`, `handoff` | No change | — | Reused as is |

## Decisions

The lead made these decisions from the source brief and the code at `77f2941`. The owner can veto any of them before `init` freezes this plan.

- **D1. Child launch vehicle.** Recommendation: `launch-child` runs the child PM harness one-shot (`claude -p`, `codex exec`, `copilot -p`, `opencode run`, `qwen --prompt`) with full autonomy, as `review.py` runs reviewers; process exit is the completion signal, output goes to a log, and the child's own Developer sessions still run in tmux under the child's private socket. Rejected: an interactive child session in tmux, as the brief sketched (no clean completion signal, version-fragile readiness banners, more code).
- **D2. Policy file format.** Recommendation: the protected-paths policy file is JSON, `{"schema": 1, "protected_paths": [...]}`, with entries in authorized-surface entry syntax; the toolkit reads only `protected_paths` (absent means `[]`), and other keys are ignored by the toolkit. Rejected: Markdown (brittle prose parsing) and reading protections from `brief.md`.
- **D3. Enforcement point.** Recommendation: protected paths are enforced inside floor fact 5 (changed files outside the effective surface *or* matching a protected entry), keeping seven facts. Rejected: an eighth floor fact.
- **D4. Plan-time overlap rule.** Recommendation: compare literal prefixes (plain path itself; `dir/` gives `dir`; a glob gives its segments before the first wildcard segment, possibly empty) and refuse when either is a segment-wise prefix of, or equal to, the other; a protected entry with an empty literal prefix is refused at load; the floor's check on actual files stays exact, and `grant` checks the exact path. Rejected: exact glob-intersection logic (complex, and over-refusal is fixed by writing a disjoint literal directory).
- **D5. Self-protection.** Recommendation: a protected-paths policy file inside the repository is appended to its own protected entries at load time, in `plan.load_policy(path, repo=...)`, so `check-plan --policy --repo` and `init --policy` agree. Rejected: leaving the policy editable by a later node.
- **D6. Command placement.** Recommendation: `launch-child` is a `pm.py` subcommand implemented in a new `pm_lib/campaign.py`. Rejected: a separate script with its own entrypoint and version check.
- **D7. One-shot command table.** Recommendation: `profiles.compose_oneshot_command(tool, prompt, *, model, effort, repo, access)` with `access` in `{"read-only", "full"}` is the only table; `review.compose_reviewer_command` becomes a thin read-only wrapper with byte-identical output. Rejected: a third copy of the flag table in `campaign.py` (VISION principle 8).
- **D8. Campaign launcher home.** Recommendation: `skills/project-manager/references/campaign-mode.md` holds the campaign launcher, its only copy, and the Mode B launcher gains an optional campaign-child paragraph. Rejected: putting the campaign launcher in `implementation-plan`'s SKILL.md (a campaign starts from a dossier, not a plan).
- **D9. Stage 1.** Recommendation: the real-model scratch campaign is run by the owner in a fresh CM session after this plan; Slice 8 delivers a model-free README trial, which the Developer runs, and the first-campaign test in the reference. This is an assumption the owner can override. Rejected: running the real-model campaign inside a Mode B Developer session (three nested model seats and a human go/no-go do not fit a Developer's tool timeouts).
- **D10. VISION gate.** Recommendation: the VISION slice (Slice 6) is approval-gated (`Approval needed before implementation: yes`) and lands before the campaign reference it governs. Rejected: an unattended VISION edit, or the revision landing after the authority it grants.
- **D11. Child handoff.** Recommendation: a campaign child never writes HANDOFF.md; a stopped child ends its node and is never resumed. Rejected: a resumable child.
- **D12. Plan file.** Recommendation: `docs/implementation-plan-pm-campaign-mode.md`, committed before `init` and archived after the run, per repository convention. Rejected: an uncommitted plan outside the repository.
- **D13. Child timeout.** Recommendation: `launch-child --timeout` defaults to 14400 s (4 h), set per node by the CM from `brief.md`'s budgets; on timeout, SIGTERM the process group, 5 s grace, SIGKILL, then `tmux -L <socket> kill-server`, exit 1. Residual: reviewers the child commissioned run in their own process groups (`review.py:461-471`, `start_new_session=True`), so neither signal reaches them and a tokenless CM cannot reap them; `launch-child` prints the reviewer process groups the child recorded for a by-hand check and never signals them. Rejected: no default bound.
- **D14. Child tmux socket.** Recommendation: `--socket NAME`, default `pm-child-<branch with / replaced by ->-<6 hex characters from secrets.token_hex(3)>`, so two nodes or two repositories never share a server; an explicit socket must be non-blank with no whitespace and no `/` (a blank one would fall back to the caller's default server, which `kill-server` would then destroy); the socket is printed in the launch line and written as a `launch-child socket=<socket>` header at the head of each launch's appended log output. Rejected: the caller's default tmux server, or a purely branch-derived name.
- **D15. Scope.** Recommendation: the non-goals above (no prompt rendering of protected paths, no parent-side signed state, no parallel scheduler, no new skill, no subcommand family, no change to the six reused skills). Rejected: building any of them before a test shows the need.
- **D16. Strict plan-defect clause for every Mode B run.** Recommendation: the Mode B launcher's `Plan defects:` paragraph becomes the strict rule for every run, not only campaign children. Grounds: the launcher adopted in `77f2941` lets the PM record delegated decisions on "natural" plan defects, which contradicts `skills/project-manager/SKILL.md:44-53` (resolution only when the plan's own text leaves one reading), and the owner-approved brief orders the fix so two children never treat one defect differently. Rejected: strict only inside the campaign-child block (two rules for one toolkit).
- **D17. Fact 5 sees both sides of a rename, on every run.** Recommendation: `git_ops.changed_files_between` passes `--no-renames`, so a rename whose source lies outside the effective surface, or inside a protected path, fails fact 5. Grounds: `git diff --name-only` otherwise reports only the destination, which is what `docs/VISION.md:85` already claims the floor catches; the reviewer prompt's own changed-file list (`review.py:370-374`) keeps rename detection, and its diff patch shows the rename, so that is recorded as a residual rather than widened into Slice 2. Rejected: scoping the change to runs with a policy (two floors for one toolkit).

## Conventions Binding Every Slice

These bind every slice. Each receipt below repeats the ones its reviewers must check, because a commissioned reviewer sees only the slice's own sections.

- `pm_lib` stays standard-library only (`tests/test_plan.py:485` `TestImportHygiene` enforces it, and `pm_lib/__init__.py` documents the one path-based exception). Python 3.13 or newer.
- Python is ruff-clean at 120 columns (`ruff.toml` at the repository root extends the `lint` skill's rule set); `ruff format --check` is part of lint.
- Tests use `unittest` and the `pm_test_helpers` fixtures: `PlanTestCase` (`tests/pm_test_helpers.py:430`), `PmTestCase` (`:483`, adds a git repository, `make_run` at `:515` accepts `policy=`), `TmuxRunTestCase` (`:632`), `render_slice` (`:256`), `write_fake_harness` (`:116`), `parse_init_output` (`:105`). The helpers pin `PM_TMUX_SOCKET` and `PM_LEDGER_DIR` per test process at import.
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

- Mode B (the intended mode): run the nine atomic slices in plan order. No batches are defined, because PM ignores batch groupings.
- Mechanically elevated slices: Slices 1, 2 and 4 declare a risky surface, and Slice 6 is approval-gated, so `plan_risk` is `elevated` for all four and both independent reviews are mandatory on each. Slices 2 and 4 also carry `Independent audit required: yes`.
- Slice 6 stops the run until the operator records `approve --slice "Slice 6" --reason …`. Because its receipt fixes the exact VISION wording, the approval may be recorded right after `init`, or when the run reaches it.
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

## Slice 3: One-shot harness commands single-sourced in `profiles.py`

### Intended Change

- `skills/project-manager/scripts/pm_lib/profiles.py`: add `compose_oneshot_command(tool: str, prompt: str, *, model: str | None = None, effort: str | None = None, repo: Path, access: str) -> list[str]`, pure and I/O-free. `access="read-only"` produces exactly today's `review.compose_reviewer_command` argv (`review.py:40-121`). `access="full"` produces:
  - codex: `codex exec PROMPT [-m M] [-c model_reasoning_effort="E"] --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -C REPO`
  - claude: `claude -p PROMPT [--model M] [--effort E] --permission-mode bypassPermissions --output-format text --add-dir REPO`
  - copilot: identical to the read-only form, `copilot [--model M] [--effort E] -p PROMPT --allow-all --autopilot --silent --add-dir REPO` (copilot is prompt-enforced either way)
  - opencode: `opencode run PROMPT [-m M] [--variant E] --agent build --auto --dir REPO`
  - qwen: `qwen --prompt PROMPT [--model M] --yolo --output-format text` (no `--sandbox`)
- `compose_oneshot_command` raises `PmError` for an `access` outside `{"read-only", "full"}` (naming both), for an unknown tool (naming the five tools), and for qwen with an effort in either mode (with a message that does not call the command a review).
- `skills/project-manager/scripts/pm_lib/review.py`: `compose_reviewer_command(tool, prompt, *, model=None, effort=None, repo)` keeps its signature and stays a module attribute, its body becomes `return profiles.compose_oneshot_command(tool, prompt, model=model, effort=effort, repo=repo, access="read-only")`, and its docstring points at `profiles`. The module docstring (`review.py:1-14`) and the section comment at `review.py:37` (`# --- one-shot reviewer command composition (this module's own table)`), which today say the one-shot table is this module's own, say the table lives in `profiles.py`. `_build_reviewer_command` (`review.py:124-129`) and the opencode variant verification in `run_review` (`review.py:419-423`) are unchanged.
- `profiles.py` module docstring (`profiles.py:1-11`), which today says there is exactly one composed path, describes the two tables: the interactive Developer profile table and the one-shot table.
- `skills/project-manager/tests/test_profiles.py`: tests for the full forms and for read-only equality.

### Acceptance Criteria

- [ ] `profiles.compose_oneshot_command` exists with the signature above and performs no subprocess call and no filesystem access.
- [ ] `access="full"` returns exactly the argv listed above for each of the five tools, with and without model and effort.
- [ ] `access="read-only"` returns, for every tool and input, the same argv `review.compose_reviewer_command` returns.
- [ ] An `access` value other than `"read-only"` or `"full"` raises `PmError` naming both values.
- [ ] An unknown tool raises `PmError` naming the five supported tools, in both modes.
- [ ] qwen with an effort raises `PmError` in both modes.
- [ ] `review.compose_reviewer_command` keeps its signature, remains patchable as a module attribute, and delegates with `access="read-only"`.
- [ ] `TestComposeReviewerCommand` (`tests/test_review.py:114-248`) passes with its assertions unmodified, and the patch at `tests/test_review.py:558` still takes effect.
- [ ] No comment or docstring in `review.py` still calls the one-shot table review-owned, and the docstrings of `profiles.py` and `compose_reviewer_command` name `profiles.py` as its home.
- [ ] The `profiles.py` module docstring no longer claims a single composed path.
- Behaviour that must not change: every reviewer command argv; `review.run_review`'s opencode variant verification; the Developer profile table and `compose_command`.

### Authorized Surface

- Files allowed to change:
  - skills/project-manager/scripts/pm_lib/profiles.py
  - skills/project-manager/scripts/pm_lib/review.py
  - skills/project-manager/tests/test_profiles.py
  - skills/project-manager/tests/test_review.py
- Functions/classes/components allowed to change: `profiles.compose_oneshot_command` (new) and the `profiles` module docstring; `review.compose_reviewer_command` (body and docstring), the `review` module docstring, and the section comment at `review.py:37`.
- Tests allowed or expected to change: `tests/test_profiles.py` (new tests); `tests/test_review.py` only where an import or patch target must change, never an assertion in `TestComposeReviewerCommand`.

### Explicit Non-Goals

- No caller of the full form in this slice (`launch-child` is Slice 4).
- No change to the Developer seat's interactive profile table, `compose_command`, or `resolve_opencode_variant`.
- No move of opencode variant verification out of `review.run_review`.
- No third-party import in `pm_lib`; no real harness CLI in tests; no weakened or deleted test.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: easy
- Recommended Developer: claude, claude-sonnet-5-5, high effort

### Validation Plan

- Tests to add/update: `tests/test_profiles.py`, a `TestComposeOneshotCommand` class: the exact `full` argv per tool (one test per tool, with model and effort where the tool takes them), read-only equality against `review.compose_reviewer_command` for every tool, invalid `access` refused, qwen effort refused in `full`.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_profiles test_review`; the full PM suite, run exactly as in Conventions; `python3 -m py_compile skills/project-manager/scripts/pm.py skills/project-manager/scripts/pm_lib/*.py`.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: `git diff <before_head> -- skills/project-manager/tests/test_review.py` shows no change inside `TestComposeReviewerCommand`; `grep -n "own table" skills/project-manager/scripts/pm_lib/review.py` finds nothing.

### Rollback Path

- Revert the slice commit. No state or CLI change.

## Slice 4: `pm launch-child`

### Intended Change

- New module `skills/project-manager/scripts/pm_lib/campaign.py` with the `launch-child` mechanics; `skills/project-manager/scripts/pm_lib/cli.py` adds the sub-parser, a thin `_run_launch_child` handler and its `_HANDLERS` entry (`cli.py:869-885`), and mentions `campaign` in the module docstring (`cli.py:1-8`).
- CLI: `launch-child --repo R --base REF --branch B --worktree PATH --prompt-file FILE --harness H [--model M] [--effort E] [--socket NAME] [--timeout N] [--log FILE] [--harness-command CMD]`. `--timeout` uses `_positive_seconds` (`cli.py:38`) with default 14400. The sub-parser defines no `--run` and no `--token`, so the post-command ledger hook (`cli.py:888-915`) does nothing for it.
- Socket: `--socket`, which must be non-blank and contain no whitespace and no `/` (a blank socket would make `sessions.tmux_argv` fall back to the caller's default server, which `kill-server` would then destroy); else `pm-child-<B with every / replaced by ->-<secrets.token_hex(3)>` (D14).
- Preconditions, each a `PmError` (exit 2) checked before anything is created: `R` resolves (`git_ops.resolve_repo`); `REF` resolves to a commit (`git rev-parse --verify REF^{commit}`); `refs/heads/B` does not exist; `PATH` does not exist; the prompt file is readable and not blank; `H` is in `profiles.SUPPORTED_HARNESSES` unless `--harness-command` is given; an explicit `--socket` is valid; the command is composed (below, including the opencode variant check) and its first element is an executable on PATH (`shutil.which`, the check `init_run` makes at `slice_ops.py:344-349`); an explicit `--log` is not an existing directory and has an existing parent directory.
- Command: with `--harness-command`, `shlex.split(cmd) + [prompt]` (the override shape of `review._build_reviewer_command`, `review.py:124-129`); otherwise `profiles.compose_oneshot_command(H, prompt, model=M, effort=E, repo=PATH, access="full")`, with an opencode effort first verified through `profiles.resolve_opencode_variant` exactly as `review.run_review` does (`review.py:422-423`).
- Create: `git -C R worktree add -b B PATH REF` through `git_ops.git`. A failure after the worktree exists (opening the log, or an `OSError` from `Popen`) raises `PmError` naming the branch and worktree left behind for the CM to remove by hand (`git worktree remove`, `git branch -D`); there is no rollback machinery.
- Environment: `os.environ` minus `PM_RUN_TOKEN`, plus `PM_TMUX_SOCKET=<socket>`; `PM_LEDGER_DIR` passes through untouched when set.
- Log: `--log`, else `<PATH.parent>/<PATH.name>.launch-child.log` (a sibling of the worktree; `.pm/` inside the worktree belongs to the child's own `init`). Opened in append mode as one handle for both stdout and stderr; `launch-child` writes a `launch-child socket=<socket>` header at the head of this launch's appended output before the child starts (an existing log keeps earlier launches above it).
- Launch: `subprocess.Popen(command, cwd=PATH, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, env=env)`; then print and flush `child launched: pid=<pid> pgid=<pgid> socket=<socket> log=<log> worktree=<PATH>` before waiting, so a CM running the command in the background can read it.
- Wait: `process.wait(timeout)`. On `TimeoutExpired`: `os.killpg(pgid, SIGTERM)`, 5 s grace, `os.killpg(pgid, SIGKILL)` (a `ProcessLookupError` is success), reap the leader, then `sessions.kill_server(socket)`; then, for each run found (below) whose `current_slice.reviewer_pids` is non-empty, print `reviewer process groups the child recorded: <pgids>; they are outside the child's process group — verify and kill them by hand`, never signalling them (they run in their own process groups, `review.py:461-471`); the command exits 1 (the kill pattern of `review.py:488-509`).
- `skills/project-manager/scripts/pm_lib/sessions.py`: new `kill_server(socket: str) -> None` that runs `["tmux", "-L", socket, "kill-server"]` with the socket passed in (never `tmux_argv`, which reads the caller's `PM_TMUX_SOCKET`), ignoring a non-zero exit (no server) and a missing tmux binary. `sessions.py` stays the only module that shells out to tmux (its docstring, `sessions.py:3-8`, already says so).
- After exit: for each `<state root of PATH>/*/run.json`, where the state root is `state.state_root(PATH)` (`state.py:44-45`, `<worktree-git-dir>/pm`), read it with a tokenless `json.loads` and print `child run <id>: status=<status> stop_reason=<stop_reason> report=<path of run-report.md beside run.json, or missing> (unverified: tokenless read)`; a `run.json` that cannot be read or parsed prints `status=unreadable` on its line and never raises. Then print `child exited <code>` and the log path. When no run directory exists, also print `no PM run found under <state root> (read the log)`. Exit 0 if and only if the child exited 0 and was not timed out; otherwise 1.
- `launch-child` reads no run token and writes no PM run state.
- `skills/project-manager/README.md`: a CLI table row for `launch-child` (create a node worktree and branch and run one unattended child PM to completion; it blocks like `review`, so run it in the background and read the log; `--timeout` kills the child's process group and its private tmux server, and prints any reviewer process groups the child recorded, which sit outside that group and need a by-hand check); the CLI intro sentence (`README.md:24`, "except `check-plan`/`init`, which take paths") also names `launch-child`; the Privacy table (`README.md:131-137`) gains a row: the `launch-child` log carries the child harness's stdout, which can include the child's own `PM_RUN_TOKEN=` line printed by its `init` (`cli.py:320`), authorizing only that child's state; the maintainer map (`README.md:206`) adds `campaign` (child PM launch for campaign mode).
- `skills/project-manager/references/run-state.md:3`: "every run-scoped command (all except `check-plan` and `init`)" also excepts `launch-child`.

### Acceptance Criteria

- [ ] `pm launch-child` accepts exactly the flags above; `--repo`, `--base`, `--branch`, `--worktree`, `--prompt-file` and `--harness` are required; `--timeout` defaults to 14400 and refuses a non-positive or non-finite value.
- [ ] Each precondition listed above fails with exit 2 and a `pm: error:` message, creates no branch and no worktree, and leaves any pre-existing branch or path untouched.
- [ ] An explicit `--socket` that is blank, contains whitespace, or contains `/` is refused as a precondition.
- [ ] A failure after the worktree exists raises `PmError` naming the branch and the worktree left behind.
- [ ] At creation, branch `B` points at the commit `REF` names and `PATH` is a linked worktree on `B` (the child may advance `B` afterwards).
- [ ] With `--harness-command`, the child's argv is `shlex.split(cmd)` followed by the prompt text as the last argument; without it, the argv is `profiles.compose_oneshot_command(..., repo=PATH, access="full")`.
- [ ] The child runs with cwd `PATH`, stdin from `/dev/null`, as its own process-group leader, with stdout and stderr appended to the one log file.
- [ ] The child's environment lacks `PM_RUN_TOKEN` even when the caller has it set.
- [ ] The child's `PM_TMUX_SOCKET` equals `--socket` when given, and otherwise matches `pm-child-<branch with / replaced by ->-<6 lowercase hex characters>`, differing between two launches of the same branch.
- [ ] The child's environment keeps `PM_LEDGER_DIR` when the caller set it.
- [ ] The log defaults to `<PATH.parent>/<PATH.name>.launch-child.log`, each launch's appended output opens with the `launch-child socket=<socket>` header, and the `child launched: pid=… pgid=… socket=… log=… worktree=…` line is printed and flushed before the wait begins.
- [ ] On timeout the command returns 1 within a few seconds of the deadline and no process remains in the child's process group.
- [ ] On timeout `sessions.kill_server` is called once with the child's socket.
- [ ] On timeout any `reviewer_pids` the child recorded are printed and never signalled.
- [ ] `sessions.kill_server(socket)` runs `tmux -L <socket> kill-server` with the given socket, never the unscoped form, and returns normally when no server or no tmux exists.
- [ ] After the child exits, one `child run …` line is printed per run directory under the worktree's state root, with status, stop reason and the `run-report.md` path or `missing`; an unreadable `run.json` prints `status=unreadable` and does not raise; with no run directory, the `no PM run found under …` line is printed.
- [ ] The exit code is 0 only when the child exited 0 and was not timed out, and 1 otherwise.
- [ ] `launch-child` never reads `PM_RUN_TOKEN` or a `--token`, writes nothing under any state root, and the ledger hook writes nothing for it.
- [ ] The README's CLI row, CLI intro sentence, Privacy row and maintainer map entry, and the `run-state.md:3` sentence, describe the behaviour as built.
- Inputs: `--prompt-file` is operator-supplied text passed as one argv element; never interpolate it into a shell string.
- Behaviour that must not change: every existing command; `review`'s process handling; `sessions.tmux_argv`.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/campaign.py` (new file)
  - skills/project-manager/scripts/pm_lib/cli.py
  - skills/project-manager/scripts/pm_lib/sessions.py
  - `skills/project-manager/tests/test_campaign.py` (new file)
  - skills/project-manager/README.md
  - skills/project-manager/references/run-state.md
- Functions/classes/components allowed to change: everything in `campaign.py`; `cli.build_parser` (new `launch-child` sub-parser only), new `cli._run_launch_child`, `cli._HANDLERS`, the `cli` module docstring; `sessions.kill_server` (new; nothing else in `sessions.py`); in the README, the CLI intro sentence, one new CLI table row, one new Privacy table row and the maintainer map line; the `run-state.md:3` sentence.
- Tests allowed or expected to change: `tests/test_campaign.py` (new).

### Explicit Non-Goals

- No interactive tmux launch of the child PM (D1), no readiness detection, no polling of the child.
- No signal to any process outside the child's process group, including the reviewers the child recorded.
- No change to `review.py`, `profiles.py`, `slice_ops.py` or run state; no token handling.
- No worktree removal, merge, or cleanup of a finished child, and no rollback of a worktree created before a later failure (the CM does those, Slice 7).
- No third-party import in `pm_lib`; no real harness CLI in tests; no weakened or deleted test.

### Risk Flags

- Risky surfaces touched: git worktree and branch creation, subprocess and process-group kill, public CLI command (`launch-child`)
- Approval needed before implementation: no
- Independent audit required: yes
- Difficulty: hard
- Recommended Developer: claude, claude-opus-5-5, high effort

### Validation Plan

- Tests to add/update: `tests/test_campaign.py`, tmux-free, on `PmTestCase` with the worktree path and prompt file under `self.repo.parent` and a fake harness from `write_fake_harness` passed through `--harness-command`: (a) branch `B` created at `REF` and worktree `PATH`; the fake writes `$PWD` and `$PM_TMUX_SOCKET` to files outside the worktree and prints `"$@"` one argument per line to stdout; cwd equals `PATH`; `PM_RUN_TOKEN` is absent although the test sets it; the default socket matches the `pm-child-…-<hex>` shape and the log header names it, and a second run with `--socket` uses that value; the prompt text is the last logged argument; the fake writes `$(git rev-parse --absolute-git-dir)/pm/<id>/run.json` and `run-report.md`, and the summary line names their status and report path; exit 0. (b) Each refusal (existing branch, existing path, unresolvable base, blank prompt file, a `--socket` containing `/`, a `--log` that is a directory) exits 2, creates no worktree and no branch, and leaves the pre-existing branch and path as they were. (c) Timeout: a fake that writes a `run.json` with `current_slice.reviewer_pids` and then sleeps 30 s, `--timeout 2`, `sessions.kill_server` patched; returns 1 within a few seconds, `os.killpg(pgid, 0)` raises `ProcessLookupError`, `kill_server` was called once with the socket, and the reviewer-pids line is printed. (d) With `--harness claude` and no override, patch `profiles.compose_oneshot_command` to return the fake's path and assert it was called with `access="full"` and `repo` equal to `PATH` (the patch pattern of `tests/test_review.py:558`).
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_campaign test_review test_profiles test_sessions`; the full PM suite, run exactly as in Conventions; `python3 -m py_compile skills/project-manager/scripts/pm.py skills/project-manager/scripts/pm_lib/*.py`.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: `python3 skills/project-manager/scripts/pm.py launch-child --help` matches the README row; in a scratch repository outside this one, run `launch-child` with a fake `--harness-command` in the background and confirm the launch line appears before the child finishes.

### Rollback Path

- Revert the slice commit. Worktrees and branches created by trial launches stay behind and are removed by hand (`git worktree remove`, `git branch -D`); no run state is involved.

## Slice 5: `implementation-plan` SKILL.md: strict plan-defect clause, campaign-child paragraph, small plans, protected paths

### Intended Change

- `skills/implementation-plan/SKILL.md:262`, the Mode B launcher's `Plan defects:` paragraph, becomes the strict rule of `skills/project-manager/SKILL.md:44-53` for every Mode B run (D16), in two or three sentences: a defective plan stops the run; proceed only when the plan's own binding text leaves exactly one implementable reading, and record that as a plan defect with the recommended amendment; never edit the plan, widen the surface except by grant, or relax a floor fact.
- The Mode B launcher gains, directly after its `Run tag (optional): <tag>` line (`SKILL.md:242`) and inside the same fenced block, an optional block of at most 12 lines, clearly marked: `Campaign child (include this paragraph only when a Campaign Manager launched this run; delete it otherwise):` followed by:
  - nobody is present and nothing is confirmed with a human;
  - the plan is committed in the dossier at the path given, which meets the "plan committed" requirement (not the product repository);
  - skip the confirm step and, after `check-plan` and the repo check, run `init` yourself with `--branch <branch>`, `--policy <protected-paths policy file, when given>`, `--run-tag <campaign tag>`, `--max-attempts <N>` and full model ids, and hold the token;
  - you run one-shot: your final message ends the run, so end only with the final report; fit every foreground command inside your harness's tool timeout (`observe --wait N`, `review --timeout N`), and use background execution with a completion notification where your harness offers it;
  - do not write HANDOFF.md, because a child run is never resumed;
  - a plan that fails `check-plan`, a repo that does not match it, or any stop condition ends the run: record it, write the final report, and end the session;
  - when every slice is decided, run `status --report`, confirm the ledger was written, write the final report as your last message, and end the session;
  - where this paragraph contradicts the confirm, handoff or resume steps, it wins.
  Wording may be tightened.
- *Slice Granularity* (`SKILL.md:26-36`) gains one sentence: a plan of one or two slices, or one whose only deliverable is a committed report (an evidence node under campaign mode), is a normal plan, with the same receipt shape and nothing extra for `check-plan`.
- *Choosing an Authorized Surface* (`SKILL.md:123-129`) gains one sentence: when a run freezes a protected-paths policy file (`check-plan --policy`, `init --policy`), an authorized entry whose literal prefix overlaps a protected path is refused at plan time, so write entries whose literal directory is disjoint from every protected path.

### Acceptance Criteria

- [ ] The launcher's `Plan defects:` paragraph states the strict rule in at most three sentences, applies to every Mode B run, and no longer contains "natural" or "delegated plan decision".
- [ ] The campaign-child block sits immediately after the `Run tag` line inside the Mode B fenced block, is marked optional with the deletion instruction, and is at most 12 lines.
- [ ] The campaign-child block contains every element listed above, including the dossier-committed plan and the one-shot waiting discipline.
- [ ] Every flag the block names exists on `pm.py init` (`--branch`, `--policy`, `--run-tag`, `--max-attempts`), checked against `python3 skills/project-manager/scripts/pm.py init --help`.
- [ ] *Slice Granularity* gains exactly the one sentence described.
- [ ] *Choosing an Authorized Surface* gains exactly the one sentence described.
- [ ] The file's fences all close and no slice-like heading appears inside a fence.
- [ ] No other text in the file changes.
- Behaviour that must not change: the Mode A launchers; the receipt template; the machine-consumed field rules.

### Authorized Surface

- Files allowed to change:
  - skills/implementation-plan/SKILL.md
- Functions/classes/components allowed to change: the Mode B launcher's `Plan defects:` paragraph and one inserted block; one sentence in each of *Slice Granularity* and *Choosing an Authorized Surface*.
- Tests allowed or expected to change: none

### Explicit Non-Goals

- No campaign launcher here (it lives in `campaign-mode.md`, Slice 7).
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

## Slice 6: VISION revision for campaign mode

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
- No CONTRIBUTING, CHANGELOG or README change (Slice 9).

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

- Revert the slice commit. Slices 7–9 cite the revision; a revert after them needs their pointers amended.

## Slice 7: Campaign-mode reference, dossier template, skill pointer

### Intended Change

- New `skills/project-manager/references/campaign-mode.md` (about 200 lines, prose unwrapped, no new mechanism), the CM's operating contract. Contents, in this order:
  - **Purpose and authority.** The CM's planning authority comes from an owner-approved `brief.md`, frozen by a digest the owner supplies when starting or resuming the CM, with a pointer to `docs/VISION.md` *The Roles*, which records the revision (Slice 6).
  - **Seats.** A table with *Authority* and *Never* columns: Owner; CM (`project-manager`, campaign mode; never implements, supervises a plan it wrote, changes `brief.md`, answers a human gate, or loosens a target or tolerance); child PM (`project-manager`, plan mode, unchanged; never writes plans or adds nodes, so depth is fixed at two); Developers and reviewers (unchanged); read-only delegate (`orchestrator`; never edits, builds, tests or commits).
  - **The dossier.** Its files and owners; node plans and per-child protected-paths policy files live in the dossier and are committed there before launch; the two goalpost rules (decisions use permissions, never create them; `brief.md` is protected by its owner-supplied digest, checked with `shasum -a 256 brief.md`); reconcile inputs (`brief.md` against the digest, `work.md`, `git worktree list`, each in-flight worktree's token-free `pm status`, every child `run-report.md`) and its four confirmations (every target has work, a passing receipt at the integration head, or an open gap; every node traces to a target; no failure has silently disappeared; budgets hold); restart is reconcile.
  - **Nodes.** Change, evidence and task kinds (a table); the dividing line (executing, building, testing, generating outputs or changing state needs a plan node; reading is a task node); delegate `files` must sit beneath the delegate's repository, so dossier excerpts go inline; plan review is a read-only task node embedding `implementation-plan`, plus `check-plan --policy`; node plan conventions: a reserved node's plan says `Approval needed before implementation: no` and names the `decisions.md` id that approved it, surfaces stay disjoint from protected paths, an evidence node's surface is its report path `campaign-reports/<node>.md`, run tag `campaign/<id>`, branch `campaign/<id>/<node>`.
  - **The loop and its rules.** The loop as a numbered list, then these rules, one or two sentences each:
    - Judge once, from the child's `run-report.md`, assessments, Developer judgments and the whole node diff. The CM commissions its own review only when: (a) the node is a reserved class (one `drift-audit` of the node diff against the brief's protections); (b) the child's report carries tolerances or plan defects the CM cannot verify itself; (c) the child scored its Developer 0, or stopped, where the review diagnoses the stop for re-planning and never readies the node for a merge; (d) the question is new: an interaction between nodes, or an instrument's validity.
    - Merges are recorded in `work.md` before they happen and done one at a time with `--no-ff`. Merge only a node whose child run is `complete` and whose branch tip is its last accepted slice commit (a stopped or still-`active` child's tip may carry unaccepted work).
    - On a merge conflict, `git merge --abort`, then rerun the node from the new integration head under a new node id and branch; never rebase an accepted branch.
    - Stopped children end their node. Cleanup of a dead child is `tmux -L <socket> kill-server`, with the socket taken from the node's `work.md` row or the log header (a tokenless `stop --scavenge` sweeps the same sessions and nothing more), then a by-hand check for the reviewer process groups `launch-child` printed, which sit outside the child's process group; an unscoped `stop --scavenge` or a bare `tmux kill-server` is never used.
    - Reserved approvals move up to `decisions.md`, recorded before launch.
    - Budgets per target and campaign-wide; two failed attempts at one target is a question for the owner.
    - Child output stays out of git's way (git-ignored output directories or the slice artifact directory). A brief may name the git-ignored output directories a product uses; the toolkit never reads them.
    - Children run under the strict plan-defect rule (D16).
  - **Launching a child.** Fill the Mode B launcher (`implementation-plan`'s SKILL.md) with its campaign-child paragraph, write it to the dossier, run `pm launch-child …` in the background, then read the log, the child's `run-report.md`, its assessments and the node diff; copy evidence into `validation/` before `git worktree remove`, which deletes the child's run state. One protected-paths policy file per child, recorded in `work.md`; every policy handed to a child lists the product repository's committed policy file path explicitly, because self-protection covers only a policy file inside the worktree; an instrument-class node is launched with a policy that omits the instrument it builds; the policy actually used is readable in the child's `run.json` at `<worktree-git-dir>/pm/<run-id>/run.json`.
  - **Validation.** Trust before use: an instrument counts only after it passes a known-good control and fails a known-bad one, and its verdict is pass, fail or error. Independence: instruments are built in owner-approved nodes and then protected. Measurement ownership: the CM measures through evidence nodes run by a child PM at a recorded SHA; an implementing Developer's claim of success points at evidence and is never the evidence. The ratchet: a target that passed must keep passing — cheap checks re-run at every new integration head, expensive ones at milestones, acceptance needs a final evidence node at the final head, and every receipt names the measured product SHA separately from the commit that adds the report. Receipt fields: measured product SHA, instrument version, configuration, command, outcome, artifact hashes.
  - **Questions to the owner.** Recorded in `work.md` with the nodes they block, the evidence, the options, a recommendation, and what continues meanwhile.
  - **Acceptance and retrospective.** A final evidence node at the final head; an independent audit as a read-only task node on a model different from the CM's; owner sign-off; the retrospective list (owner attention, questions asked and whether each was necessary, child stop causes, plan versions per node and nodes per target, spend and wall time).
  - **Worktrees and parallelism.** One short paragraph: serial first (`max_parallel: 1` in `brief.md`); going parallel is launching a second child before waiting, under disjoint surfaces, exclusive resources respected and merges one at a time, with per-child sockets and ledgers already holding.
  - **The campaign launcher.** One copyable fenced block, its only copy, with fields for the dossier path, the brief digest, the product repo path and integration branch, the protected-paths policy file path, the child PM seat (harness, full model id, effort), the child's seats (Developer, drift auditor, code reviewers, each with a full model id and effort), and the node timeout; and the CM instructions: reconcile first, then loop; never implement, never supervise a plan you wrote, never change `brief.md`, never answer a human gate, never loosen a target.
- New `skills/project-manager/references/dossier-template.md`: the dossier layout (`brief.md`, `work.md`, `evidence.md`, `decisions.md`, `validation/`, plus the committed node plans and per-child policy files) and a template for each file: `brief.md` (goal; targets for done with tolerances; pinned references with SHAs; allowed paths, and the protected-paths policy file path with its pinned SHA, which is the only place protected paths are listed; reserved decision classes; budgets: node budget per target, campaign-wide node and spend limits, `max_parallel`; exclusions: paths the campaign may not read); `work.md` (`## Now`: in-flight nodes, next action, blockers, open questions; `## Nodes` table: id, kind, objective, target, dependencies, surface, resources, status, branch, worktree, socket, base SHA, head SHA, policy, report); `evidence.md` entry shape (finding, source `file:line`, SHA read at, uncertainty); `decisions.md` entry shape (id, actor, decision, grounds, the `brief.md` permission used); `validation/` layout and the receipt shape. It is a template that points at `campaign-mode.md` for the rules, never a second copy of them.
- `skills/project-manager/SKILL.md`: the Charter (`SKILL.md:10-16`) gains one sentence: in campaign mode a PM (the Campaign Manager) writes node plans under an owner-approved `brief.md`, which is the only source of that authority, and the charter above binds every PM in plan mode unchanged; *References* (`SKILL.md:95-97`) gains a pointer to `references/campaign-mode.md`. At most five lines added in total.
- `skills/project-manager/README.md`: a `## Campaign mode` section directly after `## Launcher` (`README.md:5-7`), at most 12 lines, pointing at `references/campaign-mode.md` and listing the three toolkit touchpoints: `--policy` on `check-plan` and `init`, `launch-child`, and the per-child tmux socket.

### Acceptance Criteria

- [ ] `campaign-mode.md` exists, is unwrapped prose, and has a section for each item listed above, in that order.
- [ ] The dossier section gives the digest check as a `shasum -a 256` command compared against the owner-supplied digest, and states that node plans and per-child policy files are committed in the dossier before launch.
- [ ] The dossier section states the two goalpost rules, the reconcile inputs and the four confirmations.
- [ ] The node section states the plan-versus-task dividing line and every node plan convention listed above (reserved-node approval line naming the decision id, disjoint surfaces, `campaign-reports/<node>.md`, run tag `campaign/<id>`, branch `campaign/<id>/<node>`).
- [ ] The judge-once rule lists exactly the four exceptions (a)–(d) above.
- [ ] The merge rules state: intent recorded before merging, one at a time, `--no-ff`.
- [ ] The merge rules state: merge only a `complete` child whose branch tip is its last accepted slice commit.
- [ ] The merge rules state: on conflict abort and rerun under a new node id and branch, never rebasing an accepted branch.
- [ ] The cleanup rule gives `tmux -L <socket> kill-server` and the by-hand reviewer check, and an unscoped `stop --scavenge` or a bare `tmux kill-server` appears only as a prohibition.
- [ ] The launching section requires every child policy to list the product repository's committed policy file path explicitly, and gives the child `run.json` location.
- [ ] The validation section states trust before use, independence, measurement ownership, the ratchet (including the measured product SHA kept separate from the report commit) and every receipt field listed above.
- [ ] *Worktrees and parallelism* is one short paragraph with the content above.
- [ ] Every `pm.py` command the reference shows exists with the flags shown, checked against each command's `--help`.
- [ ] The campaign launcher is a single fenced block in `campaign-mode.md`, carries every field (including the child PM seat and the node timeout) and every CM instruction listed above, and contains no slice-like heading.
- [ ] No other file contains a copy of the campaign launcher.
- [ ] `dossier-template.md` holds the layout and one template per dossier file with the fields listed above, and restates none of the rules.
- [ ] The PM SKILL.md gains the Charter sentence and the References pointer and nothing else, at most five lines in total.
- [ ] The PM README gains the `## Campaign mode` section after `## Launcher`, at most 12 lines, naming the three touchpoints.
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
- No stage-1 material: the README trial and the "First campaign: the scratch test" subsection are Slice 8.
- No CONTRIBUTING, CHANGELOG or root README change (Slice 9).
- No domain content and no product protected-paths policy file.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Independent audit required: no
- Difficulty: hard
- Recommended Developer: claude, claude-opus-5-5, high effort (judgement-heavy prose that becomes the CM's operating contract)

### Validation Plan

- Tests to add/update: none.
- Commands to run: `--help` for every `pm.py` command the reference names (`check-plan`, `init`, `launch-child`, `status`, `stop`); a link check over the four files (every relative link target exists); `grep -n '^[ ]\{0,3\}#\{1,6\} \+[Ss]lice' skills/project-manager/references/campaign-mode.md` finds nothing; the no-baggage greps from `.github/workflows/ci.yml:101-124` run locally and find nothing.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: read `campaign-mode.md` once as a fresh CM with only the dossier and confirm every loop step names the command or file it uses.

### Rollback Path

- Revert the slice commit.

## Slice 8: Stage 1: model-free README trial and the first-campaign test

### Intended Change

- `skills/project-manager/README.md`: a new section `## Verify campaign mode (no real model, ~2 minutes)`, placed directly after `## Verify your setup (no real model, ~1 minute)` (`README.md:141-202`) and before `## Maintainer map` (`README.md:204`), about 30 lines in the shape of "Verify your setup": a scratch product repository with a committed `instruments/check_t1.py` (any content; the trial never runs it) and a committed `campaign-policy.json` (a protected-paths policy file protecting `instruments/`); `check-plan --policy` refusing a node plan whose surface reaches `instruments/`; `launch-child` with a fake `--harness-command` child that writes `run.json` and `run-report.md` under the worktree's state root (`$(git rev-parse --absolute-git-dir)/pm/<id>/`), and the summary line it prints; cleanup with `tmux -L <socket> kill-server` using the socket from the launch line or the log header.
- `skills/project-manager/references/campaign-mode.md`: a short subsection `### First campaign: the scratch test`, placed after *Acceptance and retrospective*, covering: setup (a scratch product repository and dossier, with two targets — T1, a function with a stated output measured by the instrument; T2, the suite passing at the final head — and `max_parallel: 1`); the node sequence (N1 change node → merge → ratchet; kill the CM between N1 and N2; restart from the dossier; N2 evidence node, report copied into `validation/`, branch never merged); fault injections (a node plan reaching `instruments/` is refused by `check-plan --policy`; an edit to `brief.md` is caught by the digest at reconcile); no-go criteria (a child cannot run unattended to completion; restart loses or repeats work); and the measures, by pointing at the retrospective list rather than copying it. It states that the real-model run is the owner's (D9) and points at the README trial for the model-free part, without repeating it.

### Acceptance Criteria

- [ ] The README section sits after "Verify your setup" and before "Maintainer map" and is about 30 lines.
- [ ] The README trial contains the scratch repository with the protected instrument file, the committed `campaign-policy.json`, the `check-plan --policy` refusal, the fake `launch-child` run, and the `tmux -L <socket> kill-server` cleanup.
- [ ] Every `pm.py` command and flag in the README trial exists, checked against `--help`.
- [ ] The Developer ran the README trial as written, in a scratch directory outside this repository, and `validation.md` carries its commands and output.
- [ ] `campaign-mode.md` gains the "First campaign: the scratch test" subsection with the setup, the node sequence, the two fault injections and the no-go criteria above.
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
- Commands to run: the README trial, as written, in a scratch directory outside this repository with `PM_LEDGER_DIR` and `PM_TMUX_SOCKET` set to scratch values; `--help` for every `pm.py` command named; output recorded in `validation.md`.
- Lint (differential, via the `lint` skill): required — `python3 skills/lint/scripts/lint.py check --base <before_head>`; lint reports its own coverage.
- Manual checks: `git status` in this repository is clean apart from the two files after the trial.

### Rollback Path

- Revert the slice commit; remove the scratch directory by hand.

## Slice 9: CONTRIBUTING map, CHANGELOG, root README

### Intended Change

- `CONTRIBUTING.md` source-of-truth map (`CONTRIBUTING.md:13-31`): three new rows: "Campaign-mode operating contract and the campaign launcher" → `skills/project-manager/references/campaign-mode.md`; "Dossier layout and templates" → `skills/project-manager/references/dossier-template.md`; "Protected-paths policy file contract" → `skills/project-manager/references/run-state.md`. The existing "Harness profile table" row (`CONTRIBUTING.md:25`) also names the one-shot command table, both homed in `skills/project-manager/scripts/pm_lib/profiles.py`.
- `CHANGELOG.md` `[Unreleased]` → `### Added` (`CHANGELOG.md:5-7`): one entry in the house style (a bold lead sentence, then the substance): campaign mode; protected paths through `--policy` on `check-plan` and `init`, with `grant` and floor enforcement (including both sides of a rename); `launch-child`; the single-sourced one-shot command table; the Mode B launcher's campaign-child paragraph and its strict plan-defect clause for every run; the VISION revision; and the review provenance: the plan was reviewed by an independent panel (Codex `gpt-6.1-sol` high and Claude `claude-opus-5-5` high, via `orchestrator`).
- Root `README.md`: glossary entries (`README.md:115-131`) for Campaign Manager, campaign mode, dossier and brief, node, and protected path; the *Choosing a level* table (`README.md:104-109`) gains a row (a goal met across many plans and days, with an owner-approved brief → campaign mode under a Campaign Manager); the `project-manager` row of the skills table (`README.md:44`) gains "…or, in campaign mode, plans and runs many such plans against an owner-approved brief".

### Acceptance Criteria

- [ ] `CONTRIBUTING.md` has the three new rows with full repository paths, each of which exists.
- [ ] The "Harness profile table" row also names the one-shot command table.
- [ ] `CHANGELOG.md` has one new `### Added` entry under `[Unreleased]`, with a bold lead sentence first.
- [ ] The CHANGELOG entry covers every item listed above.
- [ ] The CHANGELOG entry's provenance names the plan-review panel exactly as given above and makes no other provenance claim.
- [ ] The root README glossary has the five new entries.
- [ ] The root README *Choosing a level* table has the new campaign-mode row.
- [ ] The root README `project-manager` skills-table row carries the campaign-mode extension.
- [ ] The CI doc-link reachability script (`.github/workflows/ci.yml:126-146`) passes when run locally.
- [ ] The four no-baggage greps (`.github/workflows/ci.yml:101-124`) pass when run locally.
- [ ] No other text in the three files changes, and no text names a particular product, model package, or domain-specific instrument.
- Behaviour that must not change: every existing CONTRIBUTING row other than the "Harness profile table" row; every existing CHANGELOG entry.

### Authorized Surface

- Files allowed to change:
  - CONTRIBUTING.md
  - CHANGELOG.md
  - README.md
- Functions/classes/components allowed to change: three new CONTRIBUTING rows and the "Harness profile table" row; one CHANGELOG entry; five glossary entries, one table row and one skills-table row in the root README.
- Tests allowed or expected to change: none

### Explicit Non-Goals

- No VISION change (Slice 6).
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

- Stage 1, the real-model scratch campaign, is the owner's run in a fresh CM session following the "First campaign: the scratch test" subsection that Slice 8 adds to `campaign-mode.md` (D9; the owner may override this and run it differently). Its go/no-go gates stage 2.
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

Plan defects: a defective plan stops the run. Proceed only where the plan's own binding text leaves exactly one implementable reading, and record that as a plan defect with the amendment you recommend. Never edit the plan, widen the surface except by grant, or relax a floor fact.

Finish: run `status --report`, confirm the ledger was written, run `ledger render`, and read `run-report.md` end to end. Record the run's actual status in HANDOFF.md. Report:
- total run time, quoted from the report with its endpoints
- commits (unpushed)
- what was accepted, and on what evidence
- what stopped, and why
- plan defects, with the decisions you took and the amendments you recommend
- residual risks and follow-ups, with a convergence read
- the leaderboard path
```

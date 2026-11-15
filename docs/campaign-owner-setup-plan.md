# Plan: the owner's campaign setup, from a fresh install

**Status:** Accepted by the owner and implemented, 2026-11-15, in the commit that adds this file; archived in the commit after it. Two details differ in the implementation: the integration checkout is created in two steps (`git branch <branch> <base>`, then `git worktree add <checkout> <branch>`), and the panel review added literal pathspecs, branch-name validation and an absolute checkout path to `--commit`. Reviewed by Codex (`gpt-6.1-sol`, high, via `orchestrator`); *Review record* at the end.

**Scope:** `skills/project-manager` (the `campaign-preflight` command, `README.md`, `references/campaign-mode.md`, `references/dossier-template.md`) and `CHANGELOG.md`. No running campaign's own files change.

## Why

The toolkit gives a campaign's owner one command, `campaign-preflight`, written for the Campaign Manager (CM) as the strict, digest-checked first step of every reconcile. The owner is expected to arrive at it with everything already done by hand: README *Start a campaign* has the owner commit the dossier (step 2) and create the integration checkout (step 3) before running it (step 8). Run any earlier, which is the natural moment to ask "is my campaign ready?", it cannot pass: an untracked `brief.md`, `setup.sh` or `sandbox.json` is a `FAIL`, one per file; an untracked owner file marks the owner's files as in doubt, so the `setup.sh` trial is skipped; and the missing integration branch, checkout and node worktree parent are `FAIL`s too. The summary line ("every failure is a question to the owner") is written to the CM.

So the generic setup steps (create the checkout and directories, commit the dossier, take the digest, fill it into the launcher) end up in a script written by hand for each set of campaigns. The lab campaigns' `lab-preflight.sh`, adapted from sage26's, re-parses the brief with regular expressions bound to wording the template does not have, initialises and creates paths on what reads as a check run, and commits with `git add -A` whenever `--commit` is passed, which mid-campaign would sweep the CM's uncommitted files into an owner commit. Its owner guide then tells the owner which five `FAIL` lines to expect and ignore. A new owner on a fresh install has to rediscover all of it.

Two template gaps sit beside it. `campaign-mode.md` archives every node with `stop-child --archive <results root>`, and `archive.py` speaks of "a results root the CM names", but no template line defines one. `plan_review.py` says "the dossier git-ignores `nodes/*/reviews/*/orchestrator/`", but the template ships no `.gitignore`, and the orchestrator's default artifact root, `.orchestrator/runs/`, lands in the dossier because the CM runs from it.

## The owner's experience after this plan

1. Write the dossier from `dossier-template.md` (brief, CM files, `.gitignore`, and `setup.sh` / `sandbox.json` if used).
2. Copy the campaign launcher from `campaign-mode.md` into a file beside the dossier, and fill in everything but the digest.
3. Run `pm.py campaign-preflight --owner --launcher <file>`. It performs no setup and no commit. What is merely not done yet prints as `todo`, with what `--commit` will do about it; a real problem prints as `FAIL` with its fix. When only `todo`s remain it ends `ready: read brief.md once more as the contract, then run this command again with --commit` and exits 0.
4. Run the same command with `--commit`. It creates the integration checkout at the pinned base and the directories, commits the dossier, runs the check again exactly as the CM will at its first reconcile, and writes the brief's digest into the launcher file.
5. Start the CM from a clean shell in the dossier with the launcher file. A restart is the same step.

The CM's reconcile is unchanged in strictness, and `campaign-preflight` without `--owner` behaves as it does today.

## Principles applied

- **One command, two steps.** The owner's check and the CM's are the same function; `--owner` selects the owner's reading, and `--commit` applies it. No new command, no per-campaign script.
- **The check that finds a gap names its fix.** Each `todo` carries the action `--commit` performs, so the actions are never a second copy of the checks.
- **Generic.** Every value comes from a template line or the toolkit's own launcher format; harness names come from the toolkit's harness table.
- **Strict where it matters.** Every check that fails today for a reason other than "not created or not committed yet" fails in owner mode too.

## Changes

Each change lists the problem, the change, its behaviour, the files, the tests worth writing, its complexity and its dependencies.

### C1. `--owner`: report what is merely not done yet as `todo`

**Problem.** Before the dossier's first commit and the integration checkout exist, the command reports expected state as failure and skips the setup trial.

**Change.** A `--owner` flag, refused with `--digest`, selects the owner's pre-launch reading. It adds one output level, `todo`, for a gap `--commit` (C2) closes, and ends with a verdict written for the owner. Without `--owner` nothing changes: a run without a digest stays as strict as today, so a CM that forgets its digest is not told it is ready.

**Behaviour.** With `--owner` only:

| Situation | Today | With `--owner` |
|---|---|---|
| dossier is not a git repository and lies inside none | `FAIL` | `todo`: `--commit` initialises it |
| dossier has no commit yet | a `FAIL` for each of `brief.md`, `setup.sh`, `sandbox.json` present, and the trial skipped | one `todo` listing every file the first commit will hold |
| after the first commit, `brief.md`, `setup.sh` or `sandbox.json` untracked or modified | `FAIL`, and the trial skipped | one `todo` naming them |
| integration branch absent | `FAIL`; the policy-file check is skipped and the trial fails for want of a head | `todo`: created at the pinned base; every check that reads the head's tree (tracked `HANDOFF.md`, policy file, exclusions, generic pins, setup trial) reads the base, which is what the branch will be created at |
| integration checkout absent | `FAIL` | `todo`: created on the branch |
| node worktree parent or results root (C4) absent | `FAIL` | `todo`: created |

The base must already be a commit in the product repository, with the policy file committed in it; only the dossier and the campaign's own branch, checkout and directories can be "not done yet".

In owner mode an untracked or modified owner file no longer clears the integrity flag. Everything else that clears it today still does (a digest mismatch, `setup.sh` or `sandbox.json` not matching its pin, an unreadable `sandbox.json`), so the setup trial runs before the first commit whenever the checks that guard it today pass, and never otherwise. Which files get tracking checks is unchanged.

Fix texts written for the CM ("a changed script is a question to the owner") read, with `--owner`, as the owner's action: for a pin mismatch, "update the pin in brief.md to `<sha256>`, or restore the pinned file"; for the toolkit pin, "re-pin the Campaign toolkit line to `<full commit>` after reading what changed, or check out the pinned commit".

The run ends with one verdict line:

- any `FAIL`: `campaign preflight: N problem(s); fix the FAIL lines and run it again` (owner) or today's line (CM); exit 1.
- only `todo`: `ready: read brief.md once more as the contract, then run this command again with --commit`; exit 0.
- nothing left (owner, after the commit): `all checks passed; brief digest <hex>`; exit 0.
- without `--owner`: today's lines.

Four levels in all: `ok`, `todo`, `FAIL`, `note`. Like today, the check's only effect beyond reading is the setup trial: a throwaway worktree, removed afterwards, which executes the owner's script, and, when the brief has exclusions, git's shared `extensions.worktreeConfig` setting, which a launch sets anyway.

**Files.** `scripts/pm_lib/preflight.py` (a `todo` result carrying its action; owner-mode branches at the situations above; the base as stand-in head), `scripts/pm_lib/cli.py` (the flag, printing and the verdict).

**Tests.** A fresh dossier and product (dossier uncommitted, no branch, no checkout, no directories) gives only `ok`, `todo` and `note` with `--owner`, runs the setup trial at the base, and exits 0; the same run without `--owner` still fails as today. A `setup.sh` pin mismatch with `--owner` is a `FAIL` and the trial does not run. Existing tests stay; only assertions on changed wording are updated.

**Complexity.** Small to medium: the function keeps its structure; the change sits at the six situations above.

**Depends on.** Nothing. C4 adds a row.

### C2. `--commit`: the owner's second step

**Problem.** Every campaign needs the same steps after a clean check, and today they are hand-written per campaign, with the `git add -A` hazard.

**Change.** `campaign-preflight --owner --commit`. `--commit` requires `--owner`, so it is refused with `--digest` and in a plain run.

**Behaviour.**

1. Run the owner check. Any `FAIL`: print it and exit 1 without performing any setup or commit.
2. Apply the `todo` actions in a fixed order: initialise the dossier repository; create the integration checkout (`git worktree add -b <branch> <checkout> <base>`, or without `-b` when the branch exists); create the node worktree parent and the results root; commit the dossier.
3. The commit. When the dossier has no commit: `git add -A`, so the template `.gitignore` applies and exactly the files the `todo` listed are staged, then commit as `Brief: <dossier name>`. Afterwards: the owner files that changed (`brief.md`, `setup.sh`, `sandbox.json`, and dossier files a brief pin line names) are staged by path and committed by path (`git add -- <files>`, then `git commit -- <files>`), so a new owner file is included and nothing the CM has staged or modified is swept in. Nothing to commit is not an error. A git failure (on a fresh machine, most often no `user.email`) is printed as git gives it, exit 1.
4. Run the check again without `--owner`, with the committed brief's digest: the command the CM runs at its first reconcile. Print its lines and verdict.
5. Only if that passes: print the digest and, with `--launcher` (C3), replace the value on the launcher file's one `Brief digest:` line and say so.

Rerunning `--commit` with nothing to do reruns steps 4 and 5, so it is safe to repeat. It never creates or moves a product branch other than creating the integration branch at the pinned base, and never touches a checkout path that already exists.

**Files.** `scripts/pm_lib/preflight.py` (the actions), `scripts/pm_lib/cli.py` (the flag, the order, the second run).

**Tests.** From a fresh dossier and product, `--commit` creates the checkout and directories, makes the first commit, and the CM-mode run passes. After the first commit, with a CM change to `work.md` staged and `brief.md` changed, it commits `brief.md` alone and the staged change stays staged. With a `FAIL` present it creates and commits nothing. `--commit` without `--owner` is refused.

**Complexity.** Medium, most of it in C1's actions.

**Depends on.** C1.

### C3. `--launcher FILE`: read the campaign's paths and seats from the launcher the owner already fills

**Problem.** The owner otherwise types the dossier, product checkout, integration branch and one `--harness` per seat harness, copied from the launcher by hand; the lab script greps them out of its launcher. And on a fresh campaign nothing names a repository to create the integration checkout from.

**Change.**

- `Product repo:` keeps its meaning in the campaign launcher, and the template now says it outright: the product repository's checkout of the integration branch. It is the path every CM step already uses (reconcile, `review-plan --repo`, `launch-child --repo`, merges), so no CM step changes. `Product repo` and `Integration branch` each get their own line.
- The template gains one owner-only line, `Created from: <any existing checkout of the product repository; read only by campaign-preflight --commit, to create Product repo when it does not exist>`. With `--owner`, while `Product repo` does not exist, the repository checks run against `Created from`.
- `campaign-preflight --launcher FILE` reads `Dossier`, `Product repo`, `Integration branch` and `Created from`, and takes the seat harnesses from the `Child PM`, `Child seats`, `Task-node delegate`, `Plan reviewer` and `Acceptance auditor` lines: every standalone word that is one of `profiles.SUPPORTED_HARNESSES` (not joined to `-` or `/`, so `claude-sonnet-5-5` and `opencode-go/…` are model ids). A seat line that yields no harness, a missing line, or a value still holding `<…>` is an error naming it; the `Brief digest` line may be blank or hold the template's placeholder.
- It replaces the explicit options rather than adding to them: given together they are refused, so there is no precedence rule. The CM keeps the explicit form, since its launcher is in its prompt rather than on disk, now simply `--repo <Product repo> --worktree <Product repo>`.
- A launcher file inside the dossier is refused: the CM writes the dossier, and the digest the owner supplies must not sit where the CM writes.

**Files.** `scripts/pm_lib/preflight.py` or `cli.py` (a short parser), `references/campaign-mode.md` (launcher template and the reconcile command).

**Tests.** A filled copy of the toolkit's own launcher template yields the right paths and harness set; a seat line with a misspelled harness is an error; a launcher inside the dossier is refused; `--commit --launcher` writes the digest line.

**Complexity.** Small to medium.

**Depends on.** C1; C2 for the digest.

### C4. The template defines the results root and ships the dossier's `.gitignore`

**Problem.** Every node's archive writes to a results root that no template line defines, so each CM names its own. Review-round and delegate artifacts land in the dossier, which the code assumes is ignored and the template never ignores.

**Change.**

- `dossier-template.md`'s *Allowed paths* gains ``- Results root: `<absolute directory>` ``; `campaign-mode.md`'s two `--archive <results root>` references point at it.
- `campaign-preflight` checks it: present, absolute, a writable directory, and overlapping neither way, as real paths, the dossier, `Product repo` (the `--worktree`) or the node worktree parent. A `todo` when absent with `--owner` (created by `--commit`), a `FAIL` otherwise. This is an early warning only; the archive's own overlap guard stays authoritative.
- The template layout gains the dossier `.gitignore`: `.orchestrator/` and `nodes/*/reviews/*/orchestrator/`.

**Files.** `references/dossier-template.md`, `references/campaign-mode.md`, `scripts/pm_lib/preflight.py`.

**Tests.** A brief without the line fails; one inside the node worktree parent fails; an absent directory is a `todo` with `--owner`.

**Complexity.** Small.

**Depends on.** Nothing; its `todo` row uses C1.

### C5. An active virtualenv is reported at reconcile

**Problem.** Every seat inherits the launching shell's environment, so a product virtualenv active when the CM starts reaches every child. Today that is a sentence in `sandbox.md` and a line in each owner guide.

**Change.** When `VIRTUAL_ENV` is set, a `note`: "VIRTUAL_ENV=<path> is set and every seat inherits it; start the CM from a shell without the product's virtualenv". A note, not a failure, since the variable alone does not say the environment is the product's. The CM runs the check in its own shell at every reconcile, so it appears in the environment that matters.

**Files.** `scripts/pm_lib/preflight.py`.

**Tests.** None beyond one assertion in an existing test, if any test fixture sets the variable.

**Complexity.** Very small.

### C6. The trust note says what actually happens

**Problem.** The note "the owner checks this once by hand" predates the current flow, in which a harness that asks per linked worktree (Claude Code does) has the CM ask the owner once, at its first reconcile, for permission to add the trust entries.

**Change.** Reword the note to say that, pointing at `sandbox.md` *Troubleshooting*.

**Files.** `scripts/pm_lib/preflight.py`.

**Tests.** None beyond any existing assertion on the text.

**Complexity.** Very small.

### C7. The README is the owner's runbook

**Problem.** README *Start a campaign* is ten manual steps that leave the owner to commit, create the checkout and fill the digest by hand before the check can pass, and each campaign's owner guide re-explains that procedure and drifts from it.

**Change.**

- Rewrite *Start a campaign* as the five steps of *The owner's experience*, after the one-time steps (the protected-paths policy committed in the product, `implementation-plan` and `orchestrator` installed, *Verify campaign mode* once per machine). Show the Claude Code start as one example line (`claude --model <id> --effort <level> "$(cat <launcher file>)"`, from a shell without the product's virtualenv, and the same shell for every restart) and say that any harness works by pasting the file.
- Two sentences on a campaign-specific owner note: it holds only what the runbook cannot (why the campaign exists, its order among others, the brief's choices worth the owner's eye, what is untested) and links to the runbook for the procedure.
- `campaign-mode.md`: the launcher template (C3), the reconcile command (C3), the results root (C4), and one sentence that `--owner` and `--commit` are the owner's and the CM never passes them.
- `CHANGELOG.md`: one entry.

**Files.** `README.md`, `references/campaign-mode.md`, `references/dossier-template.md`, `CHANGELOG.md`.

**Tests.** None; the README's model-free *Verify campaign mode* sequence is updated if it calls `campaign-preflight`.

**Complexity.** Small.

**Depends on.** C1 to C6.

## Considered and cut

- **A separate owner command** (`campaign-ready`). Folded into `campaign-preflight` as `--owner` and `--commit`: one function, one set of checks.
- **Keying owner mode on a missing `--digest`.** Shorter to type, but a CM that forgot its digest would be told it was ready; the explicit flag keeps today's strict no-digest run.
- **Splitting `Product repo` from a new `Integration checkout` line.** It would move every CM step that reads or reviews the product to a new name, and a step left on `Product repo` would read a checkout of the wrong branch. The owner-only `Created from` line (C3) solves creation without touching the CM.
- **A `check` output level** for "a human must verify". The one such line, trust, becomes accurate instead (C6).
- **A `FAIL` on `VIRTUAL_ENV`.** The variable does not say whose environment it is; a note informs without blocking a legitimate one (C5).
- **A generated per-harness launch command, and a `Campaign Manager:` seat line.** A CM launch profile per harness for one line of convenience; the README example (C7) covers the common case.
- **The CM setting the orchestrator's artifact-root variable from the brief.** A variable set in one of the CM's shell commands does not reach the next; the template `.gitignore` (C4) makes the default location harmless.
- **An owner-guide template file.** Two sentences in the README (C7) do the job without a file to keep in step.
- **`--repin`, editing pins in the brief automatically.** The contract is the owner's to edit; the owner-mode fix text gives the exact new value.
- **Tracking checks for every pinned dossier file.** Their bytes are already checked; requiring them tracked would change the CM's check for no reported failure. `--commit` commits them anyway.
- **A different placeholder syntax than `<…>`.** Touches the template, the tests and every brief for a rare false positive; the current check already names each token.
- **Tracking `TMPDIR` across CM restarts** so a restart from another shell does not leave sandbox temp dirs behind. Real but rare, and `campaign.py` deliberately leaves another `TMPDIR`'s temp dirs in place; it needs design, not a check. Recorded in `docs/deferred-machinery.md` when this lands.
- **A `Workspace` line** for raw run outputs. A choice of the lab campaigns, not a toolkit need.

## Compatibility

- `campaign-preflight` without `--owner` keeps today's behaviour; the lab script and any other caller are unaffected.
- The CM-visible changes are the launcher's split `Product repo` / `Integration branch` lines and its new `Created from` line, the required results root, and two reworded notes. An old launcher still works with the explicit form.
- A running campaign is insulated only while its toolkit checkout stays at its pin. The pin check compares the executing checkout's HEAD; it neither selects the pinned version nor sees uncommitted toolkit edits, and a brief may omit the pin. Pulling these changes into the live checkout the lab campaigns run from makes their next reconcile fail on the toolkit pin (the designed signal), so either wait until they finish or re-pin deliberately.

## Order

C1 and C4 first (the owner check and the template line it checks), then C2 and C3 together (the second step and its input), then C5 and C6, then C7.

## Review record

**Round 1** (Codex `gpt-6.1-sol` high, read-only, via `orchestrator`; `RESULT: revise`, seven FIX, one CUT, two NOTE). All accepted:

- Owner mode keyed on a missing digest would tell a CM that forgot its digest it was ready → explicit `--owner`, refused with `--digest` (C1).
- `git commit -- <files>` cannot add an untracked file → stage by path, then commit by path; test that a staged CM change survives (C2).
- Pointing `Product repo` at any checkout would leave CM reads and `review-plan --repo` on a stale tree → taken with a narrower fix than proposed: `Product repo` stays the integration checkout and an owner-only `Created from` line names the creation source (C3).
- A seat line yielding no harness would silently drop its check → an error; tests use the toolkit's own template (C3).
- The integrity and owner-file description overstated today's checks → corrected; tracking checks unchanged (C1, *Cut*).
- "Changes nothing" was too strong given the setup trial → stated as "performs no setup and no commit", with the trial's effects named (C1, C2).
- Results-root separation must cover the checkout and node worktree parent, as real paths → done, with the archive guard authoritative (C4).
- CUT: the `VIRTUAL_ENV` failure → a note (C5).
- NOTE: the toolkit-pin insulation needed qualifying → *Compatibility*.
- NOTE: README is manual prerequisites rather than a prescribed fail-first flow → *Why* and C7 restated.

All three open questions were settled in the review: explicit `--owner`; `--commit --launcher` writes the digest, only after the strict re-run passes; `Product repo` stays the checkout, with `Created from` for creation.

**Round 2** (same session, scoped to the round-1 dispositions and any significant new problem): `RESULT: clean`; every round-1 finding resolved, no new findings.

---
name: lint
description: Run deterministic linters over a change and report only the findings it introduced. Use when the user asks to lint, run static analysis, or check formatting, and as the mechanical hygiene step before drift-audit and code-review in the plan → implement → audit → review → commit chain. Reports a missing linter as unavailable coverage, never as a pass.
---

# Lint

Deterministic hygiene, run before any model-based review. The linters answer
questions that have exact answers — unused name, unreachable branch,
uninitialised read, malformed table, misspelt word, unformatted file — so
no reviewer attention should be spent on them, and no reviewer's opinion is
needed to settle them.

This skill exists because LLM reviewers are measurably bad at this class. Across
five supervised runs of one plan, **11 of ~14 non-code-correctness findings were
mechanical**, and both commissioned reviewers missed every one of them while
correctly finding subtle semantic defects. Lint is the cheap deterministic floor
under review, not a replacement for it.

## The one rule that makes this usable: differential by default

A gate that demands absolute cleanliness fails on arrival in any real
repository, because pre-existing findings are not the current change's fault and
fixing them is usually unauthorized scope.

So the default question is **not** "is this code clean?" but:

> **Does this change introduce a finding that was not there before?**

`check --base <ref>` lints the changed files at the current head, lints the same
files at `<ref>` in a throwaway worktree, and reports only the difference.
Pre-existing debt cannot block; a newly added unused import cannot pass.

Use `--all` for absolute mode when you genuinely want every finding — a
standalone audit, or a repo you are cleaning up.

## Invariants

These are load-bearing. Do not work around them.

1. **`check` never installs anything.** Installing mutates the machine.
   `install` is a separate, explicit, human-run subcommand, dry-run unless
   `--yes` is passed. An agent that finds a tool missing **reports it and lets
   the human decide** — it never runs `install --yes` on its own initiative, in
   any mode. `check` and `detect` name the missing binaries and print the
   install command so the human knows exactly what is needed.
2. **A missing linter is `unavailable` coverage, never a pass.** The report names
   every language present with no tool available. `--require-coverage` turns that
   into exit 3 so an automated caller cannot mistake absence of findings for
   absence of problems.
3. **A failed run is an error, not a pass.** A linter that exits unexpectedly
   with nothing parseable did not lint anything — an invalid `ruff` config exits
   2 with empty output, which must never read as "0 findings". The same applies
   at the base ref, where a failure would make every pre-existing finding look
   new.
4. **Defects, not taste.** The tool set is deliberately limited to findings with
   an objective answer. It carries no complexity metric, no naming opinion, and
   no architectural rule. **A project's own conventions win:** the linters read
   the project's own config files first, and `config/ruff.toml` and
   `config/markdownlint.jsonc` supply defect-focused defaults only when the
   project has none. Both exist because the tools' own defaults are broader than
   this rule allows — ruff's default set flags import ordering and rewrites
   `config = dict(...)` to a literal, which on one calibration repo was the
   documented convention of the project being linted. Shellcheck is capped at
   `--severity=info` for the same reason, dropping only its presentational
   `style` tier; a project's `.shellcheckrc` still selects which rules apply and
   shellcheck honours it regardless of that flag. If a plan mandates
   something a generic rule would flag, the plan is right and the rule must be
   skipped (`--skip <tool>`).
5. **Nothing is mutated.** No `--fix`, no reformat-in-place. This skill reports;
   the Developer edits. (External tools may still write their own caches.)
6. **The comparison is a good heuristic, not an oracle.** Signatures are
   line-insensitive, so a change that removes one finding and adds an equivalent
   one elsewhere in the same file nets out and is not reported. Tools that emit
   one finding per file — the formatters — cannot show a *second* regression in a
   file already unformatted at base. Absolute mode (`--all`) is the escape hatch
   when you need the whole picture.

## Commands

Run from anywhere inside the target repository:

```bash
python3 <skill-dir>/scripts/lint.py detect                 # what covers this change, what is missing
python3 <skill-dir>/scripts/lint.py install                # dry run: print install commands
python3 <skill-dir>/scripts/lint.py install --yes           # execute them (human, one-off setup)
python3 <skill-dir>/scripts/lint.py check --base <ref>      # differential: only new findings
python3 <skill-dir>/scripts/lint.py check --all             # absolute: every finding
```

`check` always sees uncommitted and untracked work as well as any committed
range, so `--base HEAD` before a commit is the correct pre-commit call — but only
before: once the work is committed, `--base HEAD` scopes nothing, which exits `3`
rather than reporting a hollow pass. There is
deliberately no `--staged` mode: listing staged paths but linting worktree
content would let a staged defect pass behind an unstaged fix.

Exit codes: `0` pass · `1` new findings · `2` error · `3` coverage gap (with
`--require-coverage`, or a differential run where nothing differs from the base
ref). Precedence is error > coverage gap > findings: an
incomplete check is a worse answer than a complete one that found something.
Treat `2` and `3` as "this check did not happen", never as a pass. Add `--json`
for machine-readable output.

Full CLI reference, the tool table, and per-language notes: [README.md](README.md).

## Workflow

1. **Scope.** Identify the comparison ref. Under a plan slice that is the slice's
   starting commit (`before_head`); on a feature branch it is the merge base with
   the default branch; for a pre-commit check use `--base HEAD`, which sees the
   uncommitted and untracked work the commit will contain.
2. **Check coverage first.** Run `detect`. If a language in the change has no
   tool installed, record the gap explicitly and surface it — say which binary
   is missing and what installs it. Only the human runs `install --yes`. Never
   let an uncovered language read as a clean result.
3. **Run `check --base <ref>`.** Read the findings.
4. **Fix or account for every new finding.** Each one is either fixed inside the
   authorized surface, or explicitly recorded with a reason — a project
   convention the rule contradicts, or a finding whose fix needs unauthorized
   files. An unexplained new finding is an incomplete change.
5. **Report** the verdict, the tools that ran, and any uncovered language. When a
   plan slice is in play, put this in `validation.md` alongside the other
   acceptance evidence.

If a finding's fix would require touching a file outside the authorized surface,
**stop and report it** — do not widen the surface to satisfy a linter.

## Integration

Lint runs **before** `drift-audit` and `code-review`: deterministic findings are
unarguable and cheap, and sending a reviewer at code with a dead local wastes its
attention on something a tool already knows.

**Standalone.** *"Use the lint skill on this branch."* Pick the merge base as
`--base`, or `--all` for a full audit.

**Mode A** — two touchpoints:

- `scoped-implementation` step 4 (Validate): the Developer lints its own slice
  while context is hot and the diff is small, and includes the verdict in the
  implementation receipt.
- `commit` (before staging): last check that nothing new is about to land.

**Mode B** — three touchpoints, none of them a floor fact:

- The Developer prompt's workflow step: the slice's own validation includes lint,
  and `validation.md` records the verdict.
- PM's assessment: PM reruns `check --base <before_head>` itself rather than
  trusting the Developer's narration, exactly as it reruns other validation.
- PM's recorded decision: a new finding is a steer when the fix is pure cleanup
  inside the authorized surface, or a recorded tolerance with a reason.

**Why lint is not a ninth floor fact.** The floor's eight facts are
repository-integrity properties that are never legitimately violated and are
always knowable — surface, ancestry, cleanliness, digest, approvals. Lint is a
*quality* signal, and this system deliberately places quality in recorded
judgement above the floor, where a PM can grant a reasoned tolerance. Making it
non-waivable would also let a linter release that adds one rule hard-block an
unrelated run with no discretion available. Mandatory to run and recorded in the
assessment is the correct strength; unwaivable is not.

## Adding a tool

Append one `Tool(...)` entry to `TOOLS` in `scripts/lint.py`: extensions,
binary, argv builder, output parser, install recipes per package manager, and
`default=False` if it needs project-specific configuration. Then add a parser
test to `tests/test_lint.py` using output captured from the real tool. Nothing
else changes — selection, differential comparison, coverage, and reporting are
all driven off that table.

Tests: `python3 -m unittest discover -s <skill-dir>/tests -p 'test_*.py'`. They
require no linter binary; each parser is tested against captured real output.

---
name: code-health
description: Measure and investigate repository-level structure, complexity, duplication, dependencies, and composition using deterministic evidence. Use when the user asks for codebase health, maintainability, architecture or complexity analysis, structural hotspots, technical-debt evidence, or the codebase-level analogue of lint; prefer differential analysis for a change and absolute analysis only for an explicit whole-repository audit.
---

# Code Health

Measure first; interpret second. The analyzer locates structural evidence and quantifies change. The agent then reads the implicated code to decide whether the structure is justified. A metric is never itself a defect.

This skill complements `lint`: lint owns mechanically decidable local defects, while code-health owns measured signals that require contextual judgment. It is not a quality gate and never emits an `unhealthy` verdict.

## Invariants

1. **Differential by default.** Compare with the change's starting ref. Attribute only raw-value changes and new structural relationships to the change; retain whole-repository values as context. Use `--all` only for an explicit baseline audit.
2. **Facts and judgment stay separate.** The script emits facts, coverage, and bounded investigation candidates. The agent supplies interpretation and recommendations after inspecting the code.
3. **Missing measurement is unavailable, never clean.** Read the language-by-metric coverage matrix before interpreting results. Never compare measured Python functions with unmeasured functions in another language. Read `repository.unrecognised_extensions` too — a language absent from the extension table produces no coverage row, so that grouped extension-to-count map is the only evidence those files exist — and `repository.coverage_limits` for what the measurement could not reach.
4. **No score or universal target.** Do not convert comments-to-code, tests-to-code, duplication, complexity, or any composite into a repository grade. Ratios and ranks identify anomalies; they do not define quality.
5. **Raw change, not percentile movement.** A candidate must be supported by a changed raw value or new relationship. Population rank may order a bounded reading list but never establishes regression.
6. **Git defines the default scope.** Analyze tracked and untracked non-ignored files. Apply no hidden vendor/generated exclusions. Record every explicit configuration exclusion.
7. **Successful analysis exits zero regardless of evidence.** Exit `2` means an execution error; exit `3` means required coverage was unavailable or a differential scope was empty. No exit code means "unhealthy."
8. **No installation at all.** The portable floor requires only Python 3.13 and Git. Lizard is the optional multi-language complexity collector: `detect` reports whether it is present and names the command that would install it. The analyzer has no code path that installs anything, so widening coverage is always a human's explicit act outside this skill.

## Commands

Run from anywhere inside the target repository:

```bash
python3 <skill-dir>/scripts/health.py detect
python3 <skill-dir>/scripts/health.py analyze --base <ref>
python3 <skill-dir>/scripts/health.py analyze --all
```

Add `--json` for the versioned evidence bundle and `--require-coverage` when incomplete structural coverage must stop automation. Neither subcommand installs anything. There is no history subcommand or flag: churn is one `git log --numstat` away and the analyzer did nothing with it but echo it, so correlating change frequency with structure is the agent's own step when it helps prioritize.

## Workflow

1. **Resolve scope.** For a plan slice use its `before_head`; for a branch use the merge base with the default branch; before a commit use `HEAD`. Use `--all` only when the user asked for a whole-codebase audit.
2. **Run `detect`.** Read every language × metric-family cell and `repository.coverage_limits`. State unavailable or excluded coverage before drawing conclusions.
3. **Run `analyze`.** Use whatever Lizard coverage exists; never install it. If change frequency would help rank what you found, get it yourself from `git log --numstat` and keep it as context — it never enters a changed, worsened, or resolved structural bucket.
4. **Read the distributions and deltas.** Prefer tails, new cycles, changed duplicate relationships, and per-node fan-in/fan-out/blast-radius changes over averages. Keep raw values attached to every claim. On a dependency graph, check `resolution_counts` before trusting an edge: a repository built with `-I` search paths can have most of its edges resolved by unique basename, which is a disclosed guess rather than a proven relationship.
5. **Inspect candidates in code.** Determine each component's apparent role from callers, tests, interfaces, and surrounding modules. A dispatcher, parser table, generated protocol adapter, or boundary module may reasonably look structurally exceptional.
6. **Explain proportionality.** For each material candidate, state the measured fact, contextual interpretation, consequence if unjustified, confidence, and either a concrete next step or `no change justified`.
7. **Report coverage and uncertainty.** Never summarize the repository as healthy when a relevant language or metric family was unavailable.

## Required Output

Use these semantic sections; omit empty optional sections rather than inventing content:

```md
## Scope and Coverage
- Mode/base, files and languages measured, metric families unavailable.

## Measured Facts
- Raw distributions, deltas, and relationships.

## Investigated Structural Risks
1. `path:line` — measured evidence; role/context; interpretation and confidence.

## Recommendations
- Action, verification, or `no change justified`, tied to the evidence above.

## Limits
- Parser failures, unresolved index paths, symbolic links, unsupported languages, basename-guessed edges, configuration, or unmeasured semantics.
```

Do not repeat the script's entire top-N list. Investigate the few candidates with the strongest combination of change relevance, structural reach, and maintenance consequence.

## Where It Runs

Structural evidence feeds review, never a mechanical gate, so it has one touchpoint per mode. In Mode A the Developer runs it differentially before `code-review` on a broad or structural change and supplies the report as review evidence. In Mode B the PM runs it during assessment when a slice materially changes structure; a candidate alone never justifies a steer.

## Metric Semantics

Read [references/methodology.md](references/methodology.md) before interpreting a report. It defines the built-in measures, differential attribution, coverage tiers, and known limits. Read [references/tooling.md](references/tooling.md) only when evaluating or adding an external collector.

Tests: `python3 -m unittest discover -s <skill-dir>/tests -p 'test_*.py'`.

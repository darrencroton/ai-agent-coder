# External Tooling Decisions

The v1 analyzer has a dependency-free portable floor and one optional collector: Lizard. Lizard materially expands per-function cyclomatic coverage beyond Python while retaining a small CLI dependency surface. Its absence is explicit and never prevents composition, exact duplication, or Python AST analysis.

## Suitable future adapters

- [`scc`](https://github.com/boyter/scc) or [`cloc`](https://github.com/AlDanial/cloc) for broader lexical composition. `scc` is a portable single binary with JSON output; its file-level keyword complexity is an approximation and must not be presented as AST cyclomatic complexity.
- [`Lizard`](https://github.com/terryyin/lizard) is the selected v1 adapter for multi-language per-function NLOC and cyclomatic complexity. The analyzer normalizes function records, calculates distributions locally, and never adopts Lizard's default threshold as policy.
- [`jscpd`](https://jscpd.dev/reporters/json) for token-based multi-language duplication with JSON output.
- C/C++ dependency resolution beyond the built-in quoted-`#include` scan needs real build knowledge — a `compile_commands.json` consumer, `gcc -MM`, or `include-what-you-use` — because header search paths and conditional compilation are build inputs, not source facts. Deferred: the portable floor covers internal coupling without asking the repository to be configured or compiled.
- Language-native dependency tools where resolution semantics matter: [Import Linter](https://import-linter.readthedocs.io/en/latest/) for Python contracts and [dependency-cruiser](https://github.com/sverweij/dependency-cruiser) for JavaScript/TypeScript graphs and rules.

## Evaluated but not core

- CodeScene demonstrates the value of combining change activity with maintainability, but it is a service/commercial workflow rather than an atomic portable dependency.
- SonarQube is valuable when a project already operates it, but server/scanner setup is too heavy for the default skill.
- Sentrux is close in intent and offers graph-oriented analysis, but its aggregate quality signal conflicts with this skill's no-score, evidence-first contract. Dimension-level evidence may be reconsidered after its machine-readable interface matures.
- jCodeMunch is primarily symbol retrieval and impact exploration for agents, not a deterministic code-health measurement engine.
- Code Maat validates churn and temporal-coupling techniques, but this analyzer retains no history facts at all: churn is one `git log --numstat` away, so correlating change frequency with structure stays an agent-side step rather than a JVM/Clojure dependency.

## Dependency contract

1. `detect` names installed, missing, unsupported, and failed coverage.
2. Nothing here installs or repairs a tool. The analyzer names the command that would install its one optional collector and never runs it, so there is no code path that can change the machine — a stronger guarantee than a dry-run flag, and the reason the earlier `install` subcommand was removed.
3. Tool name, version, arguments, configuration, parser failures, and covered languages appear in the evidence bundle.
4. A missing or failed tool never produces a zero metric or clean verdict.
5. The Lizard CSV parser is pinned to the single 11-column headerless shape Lizard emits. A format change raises, surfacing as a recorded collector error and unavailable coverage; speculative support for formats Lizard has never produced was removed rather than maintained untested.

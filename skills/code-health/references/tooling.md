# External Tooling Decisions

The v1 analyzer has a dependency-free portable floor and one optional collector: Lizard. Lizard materially expands per-function cyclomatic coverage beyond Python while retaining a small CLI dependency surface. Its absence is explicit and never prevents composition, exact duplication, or Python AST analysis.

## Suitable future adapters

- [`scc`](https://github.com/boyter/scc) or [`cloc`](https://github.com/AlDanial/cloc) for broader lexical composition. `scc` is a portable single binary with JSON output; its file-level keyword complexity is an approximation and must not be presented as AST cyclomatic complexity.
- [`Lizard`](https://github.com/terryyin/lizard) is the selected v1 adapter for multi-language per-function NLOC and cyclomatic complexity. The analyzer normalizes function records, calculates distributions locally, and never adopts Lizard's default threshold as policy.
- [`jscpd`](https://jscpd.dev/reporters/json) for token-based multi-language duplication with JSON output.
- Language-native dependency tools where resolution semantics matter: [Import Linter](https://import-linter.readthedocs.io/en/latest/) for Python contracts and [dependency-cruiser](https://github.com/sverweij/dependency-cruiser) for JavaScript/TypeScript graphs and rules.

## Evaluated but not core

- CodeScene demonstrates the value of combining change activity with maintainability, but it is a service/commercial workflow rather than an atomic portable dependency.
- SonarQube is valuable when a project already operates it, but server/scanner setup is too heavy for the default skill.
- Sentrux is close in intent and offers graph-oriented analysis, but its aggregate quality signal conflicts with this skill's no-score, evidence-first contract. Dimension-level evidence may be reconsidered after its machine-readable interface matures.
- jCodeMunch is primarily symbol retrieval and impact exploration for agents, not a deterministic code-health measurement engine.
- Code Maat validates churn and temporal-coupling techniques, but direct Git analysis avoids its JVM/Clojure runtime for the limited history context retained here.

## Dependency contract

1. `detect` names installed, missing, unsupported, and failed coverage.
2. `analyze` never installs or repairs a tool.
3. `install` prints an exact plan and mutates only with human-invoked `--yes`.
4. Tool name, version, arguments, configuration, parser failures, and covered languages appear in the evidence bundle.
5. A missing or failed tool never produces a zero metric or clean verdict.

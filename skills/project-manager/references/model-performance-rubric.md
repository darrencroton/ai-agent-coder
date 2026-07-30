# Harness/Model Performance Rubric

Once per run, after the last slice is decided and before `status --report`, record `pm rate --text "<block>"`. Each call replaces the whole block, so if you need to correct it, call it again before `status --report` — only the last write lands in the report. This is PM's own judgement on a fixed scale, kept comparable across independent runs and models — not a mechanical measurement, and never presented as one.

Rate one labelled block per distinct model, per role, that operated in the run — never average two models into one score:

- **Developer** — always rated. One block per distinct model if `start-slice --model` changed it mid-run.
- **Reviewer — drift-audit** and **Reviewer — code-review** — rated only if that skill was actually commissioned at least once; omit a role that never ran rather than scoring nothing. Rate them separately even when the same tool/model ran both — they are different tasks. One block per distinct model if `--tool`/`--model` overrides varied it across slices.

Label each block by tool, and by model when an explicit `--model` was set (e.g. `Reviewer — code-review (codex/gpt-5.6-sol)`); when no override was given, name the tool alone (e.g. `Developer (codex, default model)`). A model that failed every commission before its identity was recorded anywhere durable (a crash or timeout with no report) may be unattributable after a session restart — say so in the block rather than guessing which model it was.

Score exactly these three dimensions per block, each 1–5, each with one line of evidence citing slice IDs in their canonical `Slice N` form (matching the report's own `id` column) — except a 5/5 (**None**), where "no incidents across the whole run" is itself the evidence and needs no citation. Never restate what a cited slice's assessment already says. If every commission of a block's model failed to produce usable output, score `N/A` instead of 1–5 for Reporting reliability and Output quality (there is nothing to check them against) and cite no slice — Process discipline still scores normally, since a commission that never completed is itself the evidence.

- **Process discipline** — Developer: floor failures, surface violations, and steers needed before a slice landed clean. Reviewer: recommissions needed (timeout, crash, unusable output) before a usable report landed. For a Reviewer this is the mechanical, boring dimension — expect it near 5/5 most of the time.
- **Reporting reliability** — Developer: how often its self-reported result/validation matched what PM independently verified. Reviewer: how *consistent* its review quality was across the slices it covered — did it perform evenly, or swing between sharp and superficial, catching real issues on some slices and missing equivalent ones on others?
- **Output quality** — PM's bottom-line read on this model's contribution across the run (Developer: accepted work's correctness/completeness; Reviewer: overall accuracy and usefulness of what it found — real defects caught, false positives avoided, issues PM had to catch itself because the review missed them), informed by but not identical to review verdicts.

## Scale

Score each block against the slices *that model actually handled* — for Developer, the slices it implemented; for a Reviewer, the slices it was commissioned on — never the run's total slice count, and never another model's share. Read every level as a share of that count, not a raw incident number — one incident out of 3 commissions is not the same signal as one incident out of 20. When frequency and remediation cost disagree, frequency decides; cost only lowers a score, never raises one. Apply the same five anchors to every block and dimension.

| Score | Anchor |
|---|---|
| 5 | **None** — no incidents in this category, across everything this model handled. |
| 4 | **Rare** — an isolated incident or two, confined to a small minority of what this model handled. |
| 3 | **Occasional** — incidents in a small but non-trivial share of what this model handled. |
| 2 | **Frequent** — incidents affecting a substantial share of what this model handled. |
| 1 | **Pervasive** — this category defined this model's difficulty, or the run stopped because of it. |

## Format

```text
Developer (codex/gpt-5.6-sol):
Process discipline: 4/5 — one surface violation (Slice 2), steered once, clean after.
Reporting reliability: 5/5 — validation.md matched every re-run check.
Output quality: 3/5 — accepted work correct; two slices needed a second review pass (Slice 4, Slice 7).

Reviewer — code-review (ollama/qwen2.5-coder):
Process discipline: 5/5 — every commission completed and returned a report.
Reporting reliability: 2/5 — sharp and thorough on Slice 2 and Slice 4, but missed real defects PM caught independently on Slice 3 and Slice 6 — quality swung by slice rather than holding a bar.
Output quality: 3/5 — real defects were caught more often than not, but the misses above meant PM's own reading had to carry two of five slices.
```

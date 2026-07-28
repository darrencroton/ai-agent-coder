# Proposed Skill Improvements: `project-manager`, `code-review`, `implementation-plan`

**Status:** implemented 2026-07-28, with four deliberate divergences recorded here rather than in the sections below (which are preserved as the original proposal):

- **Item 3's default stayed at 3.** Raising it treats the symptom; the visibility half landed (`status` and `finalize` print attempts against the ceiling) plus README guidance on when to raise `--max-attempts`. Revisit once items 1/4/5 have been measured.
- **Items 4 and 5 landed inside `Acceptance Criteria`,** not as new `###` sections. `plan.py`'s `REQUIRED_SECTIONS` would break on an eighth required section, and — decisively — the Reviewer prompt interpolates only five slice sections, so a plan-level domain table would never reach the seat item 1's gate needs it in.
- **Item 2 states authorization outright and injects the drift-audit report's *path*, not a parsed verdict** (parsing model-written prose to drive a run cuts against "never accept work from narration"). The reviewer prompt now says unconditionally that PM checks the changed surface itself, then names any fresh drift-audit report for the same commit — so the caveat disappears on standard slices too, where most reviews happen. The report path is re-copied from the hash-verified controller original before it is handed over, and is never given to a drift audit, which must stay independent of an earlier verdict on the same commit.
- **One addition not proposed here:** the root cause of both budget exhaustions was that `project-manager`'s judgement guidance covered P2/P3 findings and was silent on a P0/P1 PM judges immaterial. Nothing mechanical forced either run to chase those findings. That sentence now exists, and it generalizes past numerics.

Item 6's line-count targets were dropped as false precision from n=3; its seam guidance landed. The plan's instruction that *reviewers* must not rate out-of-domain findings above P2 was also dropped — severity is `code-review`'s contract, and stating it in both places would be a duplication defect.

**Evidence base:** two complete `project-manager` Mode B runs of
`docs/MERGER_RATE_PLAN.md` in the `relative-velocity` repo — run
`20260727T131605Z` (3 slices, stopped at Slice 2) and run `20260727T232806Z`
(Slice 1 attested, Slice 2 accepted, Slice 3 stopped). Full records under
`.pm/runs/`. Developer seat: `claude` / Sonnet 5 / medium. Reviewer seat:
`copilot` in run 1, `codex` / `gpt-5.6-sol` / medium in run 2.

Every proposal below traces to something observed, not something imagined. Each
is scored for **impact** and **cost**, because a skill change that adds text
without adding quality is a net loss — the reviewer and developer prompts are
already long, and length is itself a failure mode.

---

## Summary

| # | Change | Skill | Impact | Cost |
|---|---|---|---|---|
| 1 | Reachability-gated severity rubric | `code-review` | **High** | Low |
| 2 | Pass the drift-audit verdict into `code-review` | `project-manager` | **High** | Very low |
| 3 | Default `max-attempts` 3 → 5 | `project-manager` | Medium-high | Trivial |
| 4 | Numerical domain contract section | `implementation-plan` | **High** | Low |
| 5 | Per-slice Definition-of-Done checklist | `implementation-plan` | **High** | Low |
| 6 | Slice size guidance | `implementation-plan` | Medium | Trivial |
| 7 | State that the floor is authorization, not quality | `project-manager` | Medium | Trivial |
| 8 | Review cost awareness in the PM loop | `project-manager` | Medium | Low |

Items 1, 4, and 5 are the ones that would have changed the outcome of both runs.

---

## 1. Reachability-gated severity rubric — `code-review`

**The single highest-value change. Both runs died of this.**

### What was observed

| Run | Slice | Finding | Reachable? | Cost |
|---|---|---|---|---|
| 1 | 2 | `box_size_mpc**3` overflows at `1e200` | no | 1 attempt |
| 1 | 2 | same class, underflow near `1e-308` | no | run stopped, budget exhausted |
| 2 | 3 | fit weights overflow at `rate_err = 1e-150` | no | 1 attempt |
| 2 | 3 | same class, underflow at `rate_err = 1e-200` | no | run stopped |

Every finding was **technically correct** — the PM reproduced each one before
acting. None was reachable through any call path from the CLI or from
config-legal values. The Slice 3 case required `N_pairs ~ 1e400`, which is not
representable in float64.

The pattern is structural, not a bad reviewer. A codebase with a "validate every
input, fail loud" house style offers an unbounded supply of adversarial-input
findings. A reviewer told to find defects, with no stopping rule, keeps finding
them at successively more extreme boundaries — `1e-150`, then `1e-200`, then
`1e-300`. Each fix is real work; each spawns the next finding; the attempt budget
runs out before the sequence does.

**The cost is asymmetric and invisible.** Neither run shipped bad code — both
failed to ship *good* code. That outcome shows up nowhere as a defect.

### Proposed rubric

Reachability becomes a **gate on severity, not a factor in it**:

> **P0 — Blocking.** Data loss, corruption, silent wrong results, or a security
> issue, reachable through a documented call path.
>
> **P1 — Must fix before acceptance.** A contract violation or incorrect
> behaviour, reachable through a documented call path.
>
> **P2 — Should fix.** Correct but fragile, unclear, or inconsistent; or a
> contract violation reachable *only* through inputs outside the declared
> numerical domain.
>
> **P3 — Optional.** Style, naming, redundancy.
>
> **A finding may not be rated P0 or P1 unless the reviewer states how it is
> reached.** Absent a reachability path, the ceiling is P2.

### The reachability test

A finding is **reachable** if the reviewer can name at least one of:

1. A CLI invocation, or sequence of them, that triggers it.
2. A configuration legal under the documented parameter ranges.
3. A plausible input data file — one a real reader could produce.
4. A call from another component inside the reviewed system.

It is **not reachable** if triggering it requires hand-constructing values at a
public helper's signature that no caller in the system can produce. Direct
unit-test invocation with hand-picked extremes is a *demonstration* of the
defect, not a reachability path.

For numerical findings the reviewer must check value provenance. Above,
`sigma_rate = rate / sqrt(N_pairs)` bounds `rate_err/rate` at `1/sqrt(N_pairs)` —
two lines of algebra that settle reachability and would have saved four attempts.

### Required output change

Every P0/P1 finding gains one line:

```
Reachability: <CLI invocation | config values | input file | internal caller>
```

Every finding capped at P2 for unreachability says so explicitly:

```
Reachability: none found — requires rate_err/rate ~ 1e-200, i.e. N_pairs ~ 1e400,
which is not representable. Capped at P2.
```

That is the entire implementation surface: one rubric block, one required output
line, one paragraph on the test.

### Design constraints

- **No new sections.** Fold into the existing severity and output definitions.
- **Never suppress a finding.** Reachability changes severity only. A reviewer
  that stops *looking* at edge cases is a worse reviewer.
- **Fail toward reporting.** If reachability is genuinely uncertain, say so and
  rate P2 rather than guessing in either direction.
- **Do not require running code.** Reviewer mode is read-only; reachability comes
  from reading call paths and provenance, which is cheaper than probing anyway.

### Verification

Re-run the plan and confirm the four table findings arrive as P2-with-stated-reachability
rather than P1 — **while** the three findings that genuinely were worth fixing
still arrive as P1, because all three are reachable:

- Slice 2's `sigma_rate = nan` when `sigma_f_pair == 0` (violates a frozen convention)
- Slice 4's linear-axis empty figure (violates the log-log requirement)
- Slice 4's unvalidated redshifts reaching a log axis

That contrast is the test. The rubric works only if it separates those two groups.

---

## 2. Pass the drift-audit verdict into `code-review` — `project-manager`

**Cheapest high-value fix in this document.**

Every single `code-review` report across both runs opened with some form of:

> "Drift audit verdict: **not supplied**. Authorization was not independently
> audited."

...and then caveated its conclusions accordingly. This happened in run 1 and run
2, with two different reviewer harnesses. The PM workflow mandates running
drift-audit *first* and reading it *before* commissioning code-review — so the
verdict always exists by then. It simply is not passed through.

**Proposal:** when a drift-audit report exists for the same slice at the same
`before_head..HEAD` range, inject its verdict and finding count into the
code-review prompt. When it does not, say so explicitly rather than leaving the
reviewer to infer.

Impact: removes a recurring caveat that currently weakens every code-review
report, and lets the reviewer spend its budget on quality instead of
re-establishing authorization. Cost: a few lines in the reviewer prompt renderer.

---

## 3. Raise default `max-attempts` from 3 to 5 — `project-manager`

Both runs hit the ceiling. Neither hit it because the developer was incompetent —
they hit it because each attempt costs a full drift-audit + code-review cycle
(20–40 minutes wall clock here), and the reviewer kept producing new findings.

With weaker or cheaper developer models the iteration count only goes up, so 3 is
too tight to be the default. **The failure mode of a weak developer under this
system is budget exhaustion, not bad code shipping** — the guard rails do catch
defects; there just isn't room to fix them.

Suggested: default 5. Consider also surfacing remaining attempts in `status` and
in the `finalize` output, so the PM can pace steering decisions — in run 2 the PM
had to inspect `run.json` directly to determine whether any budget remained
before deciding whether to steer or stop.

---

## 4. Numerical domain contract — `implementation-plan`

The plan-side half of item 1, and the more important half: the rubric needs
something objective to gate against.

Plans that specify numeric functions should declare, once, the expected input
domain — then state that behaviour outside it is unspecified. Concretely, a table:

| Quantity | Declared domain |
|---|---|
| `box_size_mpc` | finite, `1e-3` to `1e6` Mpc |
| `merger_fraction` | finite, `0 < x <= 1` |
| `rate_err / rate` | bounded below by `1 / sqrt(N_pairs)` by construction |

...followed by: *"Behaviour outside these ranges is unspecified. Implementers need
not guard overflow or underflow of intermediate products for out-of-domain
inputs. Reviewers must not report out-of-domain behaviour as P0 or P1."*

Crucially the section should also name any **in-domain exceptions that must
hold** — in this plan, "a bin with `sigma_f_pair == 0` must yield
`sigma_rate == 0` exactly." That is what keeps the clause from becoming a blanket
excuse.

This distinguishes *malformed input* validation (wrong dtype, wrong shape,
non-finite, negative counts — always required, and the house style was right to
demand it) from *float64 extremity* handling (not required). The original plan
conflated them, which is what let the reviewer escalate indefinitely.

Add to the `implementation-plan` skill as a standard section for any plan
involving numerical computation.

---

## 5. Per-slice Definition-of-Done checklist — `implementation-plan`

### What was observed

The plan was 1040 lines for ~880 lines of source, reviewed five times before
implementation. It was as detailed as a plan reasonably gets. And the developer
*still* missed requirements that were written down:

- "one panel, **log-log axes**" and "write the **labeled empty figure**" appear in
  the same paragraph. The developer implemented both — and produced a
  linear-axis empty figure, because it set the axis scales inside the
  `if any_usable:` branch. It never connected the two requirements.
- The plot's redshift validation was specified and simply not implemented.

Both were caught by drift-audit, but each cost an attempt. Notably the PM had
*also* pre-listed both as explicit traps in the curated `notes.md`, and they were
missed anyway.

**Dense prose does not survive contact with an implementer.** More prose is not
the fix; a different shape is.

### Proposal

Each slice carries a `### Definition of Done` checklist — one line per verifiable
requirement, phrased as an assertion:

```
- [ ] Both axes are log-scaled on every path, including the all-unusable
      empty figure — asserted by inspecting the Axes, not by eye.
- [ ] Unusable points are provably never passed to `errorbar`.
```

The implementer must reproduce the checklist in `validation.md` with each item
marked and evidence cited. An unticked or unevidenced item is an incomplete
slice, checkable by the PM at a glance.

This converts prose MUSTs into an enumerable, auditable list. It costs the plan
author a few minutes per slice and it is the change most likely to help a weak
developer model, which is exactly the population that loses requirements in long
paragraphs.

---

## 6. Slice size guidance — `implementation-plan`

Observed sizes and outcomes:

| Slice | Diff | Attempts | Outcome |
|---|---|---|---|
| 1 | ~400 lines | 3 (run 1) | accepted |
| 2 | ~500 lines | 3 + 1 | accepted (run 2) |
| 3 | **967 lines** | 3 | stopped |

Slice 3 bundled five new functions, CLI wiring, and three documentation files
into one diff — too large for one reviewable unit, and too large for one
developer turn. It has been split into three slices in the revised plan.

**Suggested guidance:** target roughly 300–500 lines of diff per slice; treat
anything over ~700 as a signal to split. Prefer splitting on natural seams — pure
functions / I/O and presentation / wiring and docs — which also produces slices of
descending risk, so cheaper models can take the later ones.

---

## 7. State that the floor is authorization, not quality — `project-manager`

Across both runs the mechanical floor passed **8/8 on every single `finalize`** —
including at commits carrying a live, confirmed P1. The floor never once failed.

This is correct behaviour, not a bug: the floor checks plan digest, identity,
approvals, result presence, authorized surface, commit ancestry, worktree
cleanliness, and hard-stop markers. None of those is a quality signal. But a PM
agent — especially a weaker one — could easily read "8/8 PASS" as a proxy for "the
work is good" and accept on that basis.

The `SKILL.md` already implies this. Worth making explicit, in one sentence, at
the point where the floor is introduced: *the floor establishes that the change
was authorized and is well-formed; it says nothing about whether the change is
correct. Correctness is the PM's reading of the diff plus commissioned review.*

---

## 8. Review cost awareness in the PM loop — `project-manager`

A drift-audit or code-review on a ~1000-line diff took 10–25 minutes of wall
clock here. That single fact drives several PM decisions the skill does not
currently discuss:

- A steer is not cheap. It costs one attempt **plus** two full review cycles,
  since both mandatory reviews go stale on any tree change.
- Therefore a PM should batch corrections into one steer where the contract
  allows, rather than steering per finding. Run 2 did this by hand (four
  corrections in one steer) with good results.
- Reviews should be launched asynchronously with completion notification. The
  skill mentions this; it deserves emphasis, because a foreground wait that gets
  interrupted can kill the reviewer subprocess.

Suggested: a short paragraph making the "one steer, batched corrections" pattern
explicit, and noting that both reviews must be re-run after any tree change so
the true cost of a steer is attempt + 2 reviews.

---

## What is deliberately *not* proposed

- **No change to the floor's eight facts.** They worked exactly as designed.
- **No relaxation of drift-audit.** It caught two real P1s in Slice 3 that
  code-review did not, and produced zero false positives across both runs. It is
  the highest-precision component in the system.
- **No change to non-goals handling.** Zero non-goal violations across 3 slices
  and ~19 review passes, in a plan with aggressive non-goals. Whatever the
  `implementation-plan` skill does there, it works — keep it verbatim.
- **No weakening of the PM seat.** Nearly every decision in run 2 was judgement
  about whether a finding was *worth acting on*. A weak PM either accepts
  everything or chases every finding into budget exhaustion. If models are to be
  downgraded to save cost, the Developer seat is the place; the PM seat is the
  assurance.

## Caveat on scope of evidence

One developer model was exercised (Sonnet 5, medium) across two runs, and two
reviewer harnesses. The weak-model predictions here are reasoning from observed
failure modes, not measurement. The load-bearing observation is that a *capable*
model, given a five-times-reviewed plan **and** a curated trap list naming the
exact pitfalls, still missed two written-down requirements. Assume a weaker model
misses proportionally more — which argues for items 4 and 5 (restructuring what
the plan asks for) more strongly than for any change to the harness itself.

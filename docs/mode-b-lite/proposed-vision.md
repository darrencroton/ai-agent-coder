# Vision (Proposed Replacement)

> **Stage-1 review document.** This is the proposed replacement for `docs/VISION.md`, to be adopted only when Mode B Lite is approved and implemented (see `implementation-blueprint.md` for when the swap happens). Until then the current `docs/VISION.md` remains authoritative for the shipped system. The rationale for every departure from the current vision is in [`vision-assessment.md`](vision-assessment.md).

This document records why this repository exists, who it serves, and the principles that govern its design. It is written to outlive implementation details: skill internals, command names, schemas, harnesses, and model capabilities will all change, and none of them are load-bearing here. When a detail in another document conflicts with a principle in this one, the principle wins or this document is deliberately revised.

## Why This Repository Exists

AI coding agents are strong implementers and unreliable narrators. Left unsupervised, they fail in a characteristic way: they expand scope beyond what was asked, grade their own work generously, and report success in confident prose that can diverge from what actually happened in the repository. The conventional answers are all-or-nothing: keep a human in the loop for every change, or trust the agent and audit the wreckage later.

This repository takes a third position: **make AI-assisted software engineering safe to scale up in autonomy by combining a small mechanical floor of non-negotiable checks with an empowered, accountable supervisor.** The floor makes the highest-harm failures — unauthorized surface changes, broken commit history, self-approved human gates — mechanically impossible. Above the floor, a capable supervising agent (the Project Manager) judges the work the way a good engineering lead would: by reading the plan, the diff, the validation output, and the review evidence, and by owning the decision it records.

An earlier iteration of this system pursued a stronger claim — that *every* acceptance decision could be made deterministic. Its own operating record showed the cost: the machinery required to avoid trusting any model judgement grew until passing the process cost more than doing the work, and the weakest models it was built to protect were the ones it failed. This vision keeps that iteration's threat model and its honesty, and deliberately trades its maximal determinism for proportionate, accountable judgement.

## What This Repository Is

An **autonomy system first**, with graduated use: from a single standalone code review, through user-supervised slice-by-slice implementation, up to autonomous execution of a complete implementation plan under a supervising Project Manager. The parts — the individual skills — are deliberately reusable on their own. The system is designed around one question: *how much useful, verified engineering outcome can a supervised autonomous run produce per unit of human attention and system complexity — while the highest-harm failures remain mechanically impossible and every acceptance remains accountable?*

Three commitments define the answer:

1. **Contracts before code.** What "authorized" means is frozen before implementation begins. A plan defines narrow slices, each with acceptance criteria, an authorized surface, explicit non-goals, a validation plan, and a rollback path. Implementation happens inside that frozen contract. The supervisor may resolve minor ambiguity in a contract on the record; it may never widen a surface, downgrade a risk flag, or invent scope.

2. **A mechanical floor, and judgement above it.** A small set of invariants is always checked by code, never by a model: the changed-file surface against the frozen authorization, commit ancestry and a clean worktree, the frozen plan digest, and human-approval flags. Everything semantic — is the change good, is the evidence sufficient, is a deviation material — is an explicit, recorded judgement by the Project Manager against repository evidence. The documentation always says which kind of check protects what.

3. **Evidence informs judgement; narration never decides.** Acceptance rests on repository state and durable artifacts — diffs, commits, validation output, review reports. The implementing agent's account of its work is a pointer to evidence, never evidence itself. The Project Manager's acceptances cite what was examined and why, so a human can audit any decision after the fact in minutes.

## The Autonomy Ladder

**Rung 0 — Standalone skills.** Every skill is independently useful in any coding-agent harness with no supporting infrastructure: a lone code review, a disciplined commit, a drift audit against an ad-hoc contract, a session handoff. This is the entry point and the fallback; the system degrades gracefully to it.

**Rung 1 — Assisted implementation (Mode A).** One agent session runs plan → implement → audit → review → commit against frozen slices, with the user supervising slice by slice, or — for straightforward plans with strong models — autonomously in one session with standing commit authorization. Gates here are in-session discipline, not external verification.

**Rung 2 — Supervised autonomy (Mode B).** The Project Manager supervises from outside the implementing session: it keeps durable run state, launches a fresh implementing session per slice with a bounded account of accepted prior work, enforces the mechanical floor itself, assesses each completed slice from evidence at a depth proportional to its risk, commissions independent review when risk warrants it, steers fixable gaps back into the session under a bounded budget, and stops for a human on anything the plan or the floor reserves for one.

Choosing a rung is a function of stakes, plan length, model strength, and available attention — not of user sophistication.

## The Roles

**Project Manager (PM) — the accountable supervisor.** One agent seat, backed by a small deterministic toolkit. The toolkit owns run state, session control, artifact capture, and the mechanical floor; the PM agent owns everything that requires reading and judgement: assessing completed work against the contract, choosing proportionate validation depth, deciding whether a deviation is material, commissioning independent review, steering repairs, and deciding when a human is genuinely needed. PM never writes slice code and never authors or expands plans. Authority and accountability sit in the same seat: every PM acceptance is recorded with its reasoning and evidence, and PM answers for it.

**Developer — the context-rich executor.** A fresh implementing session per slice. It owns implementation, tests, validation, and the slice commit, inside the frozen contract. It reports completion with pointers to evidence; it holds no acceptance authority. Its job is engineering, not paperwork: the reporting burden on the Developer is deliberately minimal.

**Reviewer — independent eyes, commissioned by the judge.** When risk warrants independent review, *PM* launches a read-only review session against the final diff — the judge hires the auditor; the implementer never grades or relays its own audit. On low-risk slices PM's own assessment of the diff is the review. The `drift-audit` and `code-review` skills define the questions asked either way.

**The atomic skills — the shared vocabulary.** Planning, scoped implementation, drift audit, code review, simplification, commit, handoff, and reporting are each self-contained and harness-agnostic. A plan written once is executable by a fresh chat, an assisted session, or PM, and an authorization verdict means the same thing at every rung.

## The Risk Model

Controls scale with risk; the floor does not.

- Every slice gets the mechanical floor, PM assessment of the final state, and a per-slice commit.
- A slice is **elevated** when the plan flags it (approval-gated, risky surfaces, independent review required) or when PM escalates it on evidence (unexpected surface breadth, sensitive files, surprising diffs). Elevated slices get independent review, deeper validation, and — where the plan says so — a human decision. PM may raise a slice's risk level on the record; it may never lower a plan-declared one.
- Human attention is reserved for what actually needs it: plan-flagged approvals, floor violations, integrity breaches, exhausted budgets, and anything PM judges beyond its brief. Routine completion does not ask a human anything.

## Who It Serves

**The unattended operator** — several projects, multiple model providers, scarce attention. Freezes a plan, hands it to PM, returns to committed, assessed slices with a readable run report: what was accepted, why, on what evidence, and what residual risk remains. Their protection is the floor plus PM's recorded judgement plus per-slice commits that keep any mistake one revert away.

**The accountable engineer** — answers personally for correctness. Uses the chain à la carte at Rungs 0–1 with human checkpoints; possibly never uses PM. Everything at these rungs is prompt-discipline plus their own review, and the documentation says so.

**The local-first engineer** — code and data cannot leave their machines; open-weight models on their own hardware. Everything PM produces stays local, and every seat can be a local model. Their protection comes from where it always really came from: small authorized surfaces, approval gates on risky slices, independent review on elevated slices, and reading the artifacts. The system buffers weak-model sloppiness in the supervisor instead of demanding format perfection from the implementer — but a weak model in the *PM seat* weakens the judgement layer itself, and this document says that plainly rather than pretending the architecture absorbs it.

## Design Principles

1. **Protect outcomes, not ceremony.** A control exists because a meaningful failure becomes more likely without it, and it is removed when its cost exceeds that protection. Process artifacts exist to support execution, review, and recovery — never to prove compliance with other process.

2. **Mechanise the floor; judge the rest.** Cheap, high-impact invariants are code. Semantic quality is accountable judgement. Never blur which is which, in either direction: no judgement dressed as determinism, no determinism quietly replaced by judgement.

3. **One seat holds authority and accountability.** PM decides and PM answers. Every acceptance is recorded with reasoning and evidence; nothing is accepted by default, by timeout, or by unexamined narration.

4. **Proportionality.** Small, low-risk changes carry light process; consequential changes carry independent review and human decisions. The plan declares risk; PM may only raise it.

5. **Fail closed where it counts.** Integrity breaches, floor violations, approval gates, and credential/billing/destructive/external-side-effect conditions stop the run. Operational ambiguity and evidence-format imperfection are for PM to resolve on the record.

6. **Bounded persistence.** Fixable gaps earn steered retries under a finite per-slice budget, re-assessed at full rigour. The floor is never waivable; a judgement-level tolerance is always recorded.

7. **Atomic usefulness is non-negotiable.** Each skill stands alone, infrastructure-free, in any harness. Composition adds value through shared contract shapes, never hidden coupling.

8. **One source of truth per contract.** Every template, role definition, and enforcement fact lives in exactly one place. Duplicated guidance is a defect even when the copies agree.

9. **Minimise the whole system.** Complexity is measured across code, prompts, schemas, state, artifacts, commands, documentation, and model interactions together. Moving complexity between layers is not simplification. No mechanism exists for a hypothetical future need.

10. **An honest threat model, stated where it matters.** The system defends against corner-cutting, drift, and overconfidence — not against a determined adversary fabricating coherent evidence. Where protection is heuristic, prompt-shaped, or judgement-based, the documentation attributes the real guarantee to the correct layer.

## What the Repository Guarantees — and What It No Longer Claims

**Still guaranteed mechanically, at Rung 2:** no slice is accepted with changes outside its frozen authorized surface; no acceptance without a commit that descends from the slice start on the intended branch with a clean worktree; no run continues past a mid-run plan edit; no approval-gated slice proceeds without a recorded human approval; durable per-slice evidence and a durable, reasoned acceptance record for every slice.

**Claimed as accountable judgement, not mechanism:** quality of accepted work; sufficiency of validation and review evidence; materiality of deviations; operational recovery decisions; the decision that independent review was or wasn't warranted on a standard slice.

**No longer claimed at all:** deterministic acceptance of semantic gates; mechanical proof that an independent audit process ran; mechanical preservation of every prior finding's wording; protection against a supervisor model that is itself unreliable — the PM seat is trusted, calibrated by the operator's choice of model, and that trust is this system's honestly stated foundation.

## Non-Goals

- **Not a planner-free autopilot.** Nothing here invents or repairs plans on the fly. Plan quality is the ceiling on everything above it, and keeping planning human-approved is a feature.
- **Not a sandbox or container system.** Isolation, when needed, is the environment's job. Containment here is contractual and evidential, not OS-level.
- **Not adversary-proof.** The floor covers the highest-harm failure shapes; the residual gap is documented, not papered over.
- **Not tied to any vendor, harness, or model.** Harnesses are pluggable; every supported tool is eligible for any seat; factual enforcement differences are documented without becoming rankings.
- **Not a maximal-assurance system.** Where near-total assurance per change matters more than throughput, use Rungs 0–1 with a human at every gate. Rung 2 is calibrated for high practical reliability at reasonable cost, and says so.

## Stability Note

Mode letters, skill names, command interfaces, and supported harnesses are implementation vocabulary and may evolve. The commitments — contracts before code, a mechanical floor with accountable judgement above it, evidence informing every decision, proportionality, honesty about where guarantees come from — are the identity of this repository. Revise this document deliberately or not at all.

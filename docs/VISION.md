# Vision

This document records why this repository exists, who it serves, and the principles that govern its design. It is written to outlive implementation details: skill internals, command names, schemas, harnesses, and model capabilities will all change, and none of them are load-bearing here. When a detail in another document conflicts with a principle in this one, the principle wins or this document is deliberately revised.

## Why This Repository Exists

AI coding agents are strong implementers and unreliable narrators. Left unsupervised, they fail in a characteristic way: they expand scope beyond what was asked, grade their own work generously, and report success in confident prose that can diverge from what actually happened in the repository. This failure mode worsens in exactly the two directions users most want to go — stepping away from the keyboard (more autonomy) and using cheaper, weaker, or self-hosted models (less capability per token).

The conventional answers are all-or-nothing: either keep a human in the loop for every change, or trust the agent and audit the wreckage later.

This repository takes a third position: **make AI-assisted software engineering safe to scale up in autonomy by moving trust out of the model and into contracts, evidence, and role separation.** The agent moves fast inside the lane — it just doesn't get to redraw it. And the lane is enforced by evidence, not by the agent's promise.

## What This Repository Is

This is an **autonomy system first**, with graduated use cases: from a single standalone code review, through user-supervised slice-by-slice implementation, up to fully autonomous execution of a complete implementation plan under an external supervisor. Its parts — the individual skills — are deliberately reusable on their own, but the system is designed top-down around one question: *how much independence can be granted to an AI implementer without losing authorization, auditability, and truth?*

Three commitments define the answer:

1. **Contracts before code.** What "authorized" means is frozen before implementation begins. A plan defines narrow slices, each with acceptance criteria, an authorized surface, explicit non-goals, a validation plan, and a rollback path. Implementation happens inside that frozen contract, never as a negotiation with it.

2. **Authorization and quality are separate questions.** "Was this change authorized?" is audited first, as its own gate, before "is this change good?" A beautiful diff that redrew the lane fails; the two judgments are never blended.

3. **Evidence over narration.** Acceptance decisions rest on repository state and durable artifacts — diffs, commits, validation output, structured results, recorded launches — never on the agent's account of what it did. Where a boundary is enforced by prompt or heuristic rather than mechanism, the documentation says so plainly.

These commitments are embodied in one constant chain: **plan → scoped implementation → validation → drift audit → code review → commit**. Raising the autonomy level changes *who holds the gates* — never what the gates are.

## The Autonomy Ladder

**Rung 0 — Standalone skills.** Every skill is independently useful in any coding-agent harness, with no supporting infrastructure: a lone code review, a disciplined commit, a drift audit against an ad-hoc contract, a session handoff. This is the entry point and the fallback; the system degrades gracefully to it.

**Rung 1 — Assisted implementation (Mode A).** The user supervises an orchestrated run slice by slice. The agent restates each frozen contract, implements, audits, and reviews — but the human approves risky slices before coding and every commit after gates pass. The user holds the gates.

**Rung 2 — Autonomous session (Mode B).** One agent session runs all remaining slices and holds its own gates, delegating independent audits and reviews for objectivity, and stopping on anything approval-gated or outside the contract. This rung is right when the plan is straightforward, the implementing and reviewing models are strong, and the user does not want to stand up an external supervisor. Its gates are promises kept in-session — disciplined, but not externally verified — so it trades assurance for simplicity. It is the lightweight sibling of Rung 3, not a deprecated path.

**Rung 3 — Supervised autonomy (Mode C, Master Controller).** The gatekeeper moves outside the implementing agent entirely. The Master Controller keeps durable run state, launches a fresh implementing session per slice — the context reset that makes very long plans tractable — verifies every gate from local evidence, steers fixable gaps back into the live session through a bounded repair loop that never relaxes a gate, and stops for a human on anything outside policy. Supervision is a dial within this rung, not a fork: the default operating style keeps a supervising model in the loop for operational judgment (interruptions, usage resets, stalls), while a fail-closed unattended style exists for runs where no supervising model is available or wanted, at the cost of stopping on the first ambiguity. Acceptance is deterministic in both styles; the supervising model only ever handles operational judgment, never acceptance.

Choosing a rung is a function of stakes, plan length, model strength, and available attention — not of user sophistication. The same user should move up and down the ladder from task to task.

## The Roles

**Master Controller (MC) — the trust anchor.** A deterministic supervisor that owns run state and gates. It recomputes the highest-risk checks itself — the changed-file surface against the frozen authorization, commit ancestry, clean worktree — rather than trusting any report. It steers bounded repairs, holds sole authority to stop for a human, and never writes code, never plans, and never delegates. Where MC's verification is existence-and-consistency checking rather than full re-derivation, that boundary is documented, and the always-mechanical checks are chosen to cover the changes most likely to cause real harm.

**Orchestrator — the context-rich executor.** The agent that runs one slice through the constant chain, self-corrects first, and delegates bounded sub-tasks when that improves quality, independence, or context economy. Standalone, the orchestrator is the human-facing assistant that owns planning, verification, and the final deliverable. Under MC, it is a per-slice implementing session that reports a structured result and holds no final authority. Same discipline, different boss.

**Worker — bounded leverage without authority.** A single-purpose helper launched only through a validated semantic contract: the worker states no gates, creates no commits, and never re-delegates. The launcher — not any model — validates the request against policy, composes the harness command, embeds the instructions the worker needs, and records mechanical evidence of what actually ran. Verifying that a worker's *output* satisfied its task remains the orchestrator's job; the system's job is to make failures visible rather than silently absorbed.

**The atomic skills — the shared vocabulary.** Planning, scoped implementation, drift audit, code review, simplification, commit, handoff, and reporting are each self-contained and harness-agnostic. Composition is what makes the system worth more than the sum of its parts: because the skills share frozen contract shapes, a plan written once is executable by a fresh chat, an orchestrated session, or MC's parser, and an authorization verdict means the same thing at every rung of the ladder.

## Who It Serves

The three personas below are distinguished by operating constraints — attention, accountability, and data locality — not by skill level.

### The unattended operator

**Context and goals.** An engineer or researcher with several concurrent projects and access to multiple coding agents and model providers. Their scarce resource is attention. They want well-planned work to keep progressing while they are elsewhere — overnight, between meetings — using each model where it is strongest, without surrendering the audit trail.

**How they use the system.** Freeze a multi-slice plan, hand it to MC at Rung 3, and let supervised autonomy run: fresh session per slice, deterministic gates, bounded repairs, operational recovery from transient interruptions and usage windows, and durable evidence for every slice. They place the supervising model and the implementing harness on different providers so one provider's limits cannot stall both, route stronger and cheaper models to the roles that warrant them, and review run summaries and per-slice artifacts afterward.

**Value.** Hours of validated, committed slices per prompt; per-slice evidence bundles auditable in minutes; recovery from routine interruptions without human intervention; a repair loop that keeps one formatting slip from wasting a long slice.

**Constraints and risks.** Provider usage windows; supervision commands that outlive the tool-call limits of the assistant driving them; the documented trust boundary (verdicts are evidence-checked, not semantically re-derived); and the permanent ceiling: MC amplifies a good plan and stops on a bad one — it never fixes one.

### The accountable developer

**Context and goals.** A developer or scientist-developer using one coding assistant, on code where they answer personally for correctness — production services, or research code where subtle errors corrupt results. They want to delegate real implementation without losing track of what changed and why.

**How they use the system.** The chain à la carte at Rungs 0–1: plan in one session, implement slice by slice in fresh sessions with human checkpoints, drift audit before quality review, commit only on explicit approval, handoff at session boundaries. Occasionally Rung 2 for well-isolated, low-stakes work. Possibly never MC.

**Value.** Diffs small enough to genuinely review; scope drift surfaced as a first-class verdict instead of discovered mid-review; commit messages that record every file and reason; sessions that resume cleanly; a defensible record of every decision.

**Constraints and risks.** Ceremony must stay proportional to the task — the system must make it easy to know when the full chain is worth invoking; at these rungs the gates are prompt-enforced discipline, not mechanism, so model obedience matters; and the plan format must serve them even when they never use the machinery that also parses it.

### The local-first engineer

**Context and goals.** Someone whose code or data cannot leave their machines — proprietary work, pre-publication research, regulated data, or principle — running open-weight models on their own hardware. They also gain zero marginal token cost and offline capability. They want real multi-step engineering from mid-tier local models, safely, accepting slower wall-clock time.

**How they use the system.** The same plans and the same supervised autonomy, with local models in the orchestrator and worker roles. They lean hardest on the structural machinery, because their models are the least reliable link: the validated launcher and embedded instructions (their harness may have no native skill support), precise rejection feedback that lets a weak model self-correct, the bounded repair loop for format slips, and MC's recomputed gates as the real mutation backstop when a harness's "read-only" mode turns out to be a suggestion rather than a mechanism. If they cannot or will not run a supervising model, the fail-closed unattended style still gives them deterministic gates with themselves as the fallback.

**Value.** A disciplined engineering workflow, entirely on-premises, whose safety guarantees come from the architecture rather than from a vendor's guardrails — with an evidence trail that matters more to them than to anyone else.

**Constraints and risks.** Weak-model behaviors are their daily weather: delegation evasion, inconsistent readings of ambiguous instructions, self-graded audits that the system deliberately does not re-derive. Slices run long; cold starts and silent prefill must not be misread as stalls. For this persona above all, plan-level controls — small authorized surfaces, approval gates on risky slices — and actually reading the artifacts carry the most weight. And they must know which supervision choices keep data local: any cloud-hosted supervising model sees operational evidence, including fragments of code.

## Design Principles

1. **Trust the architecture, not the model.** Every acceptance claim traces to mechanical evidence. Anything resting on a model's narration or self-grading is named as such where users will read it.

2. **One responsibility per layer, fixed at the owning layer.** Planning belongs to the planner, execution and self-correction to the orchestrator, verification and stop authority to MC, bounded help to workers. Fixes strengthen the layer that owns the problem; they never migrate an orchestrator responsibility into MC or a gate into a worker.

3. **Graduated autonomy, constant chain.** Rungs vary who holds the gates — never what the gates are. A feature that weakens a gate at one rung breaks the promise at every rung.

4. **Design for the weakest model in the loop.** The system must remain safe and usable when its models are slow, inconsistent, or evasive. Structure substitutes for capability: semantic contracts instead of composed commands, embedded instructions instead of assumed knowledge, actionable rejection feedback instead of silent failure, bounded repair instead of one-shot acceptance. This is what makes the cost and privacy stories real.

5. **Atomic usefulness is non-negotiable.** Each skill stands alone, infrastructure-free, in any harness. Composition adds value through shared contract shapes, never through hidden coupling.

6. **One source of truth per contract.** Every template, role definition, and capability judgment lives in exactly one place; everything else points at it. Duplicated guidance is treated as a defect even when the copies currently agree.

7. **Fail closed; repair bounded; never relax a gate.** Unclear evidence stops the run. Fixable gaps earn a budgeted, in-session repair that is re-verified at full rigor. Integrity breaches — evidence that reality and the record disagree — are never steered, only stopped.

8. **An honest threat model, stated where it matters.** The system defends against corner-cutting, drift, and overconfidence — not against a determined adversary fabricating coherent evidence. Where a stop condition is heuristic or a boundary is prompt-shaped, the documentation attributes the real guarantee to the correct layer, and plan-level controls are presented as the compensating control they actually are.

## Non-Goals

- **Not a planner-free autopilot.** Nothing in this system invents or repairs plans on the fly. Plan quality is the ceiling on everything above it, and keeping planning human-approved is a feature.
- **Not a sandbox or container system.** Isolation, when needed, is the environment's job. The system's containment is contractual and evidential, not OS-level.
- **Not adversary-proof.** The mechanical floor — recomputed file authorization, commit ancestry, clean-worktree checks — is chosen to cover the highest-harm failure shapes, and the residual gap is documented rather than papered over.
- **Not tied to any vendor, harness, or model.** Harnesses are pluggable adapters; capability judgments are per-configured-model decisions made at run time, never hardcoded claims.

## Stability Note

Mode letters, skill names, command interfaces, and supported harnesses are implementation vocabulary and may evolve. The commitments — contracts before code, authorization before quality, evidence over narration, graduated autonomy over a constant chain — are the identity of this repository. Revise this document deliberately or not at all.

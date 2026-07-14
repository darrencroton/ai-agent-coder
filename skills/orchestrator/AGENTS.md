# Orchestrator Skill Guide

## Purpose

This package defines the `orchestrator` skill. The Developer owns implementation and final delivery; delegated Reviewers are always read-only evidence providers.

## File responsibilities

- `SKILL.md`: generic Developer/Reviewer workflow and audit provenance
- `references/reviewer-contract.md`: authoritative schema-v2 policy/request contract
- `references/pm-slice-contract.md`: compact Project Manager integration contract
- `references/templates.md`: semantic Reviewer request shapes
- model references: harness command mechanics and factual read-only enforcement only
- `scripts/reviewer_contract.py`: schema validation, skill embedding, prompt rendering, command composition
- `scripts/reviewer_jobs.py`: tracked process lifecycle and artifacts
- `scripts/reviewer_sessions.py`: Claude/Codex session signals and transcript fallback

Do not put model rankings or role eligibility in harness references. All supported tools are equally eligible. Do not add editing, Git/GitHub mutation, commit, or mutation-prone test delegation to Reviewer workflows.

## Contract rules

- Schema version is 2.
- Requests never contain `role` or `access`; the launcher records `reviewer` and `read-only`.
- Old Worker fields, schema v1, unknown extensions, and old paths are rejected without compatibility aliases.
- Reviewer labels use `<nn>-<tool>-<subtask-slug>[-rN]`.
- Use `reviewer_jobs.py` for launch, activity, wait, extract, and cancel; raw harness commands are unsupported.
- Preserve vendor transcript fields such as JSON `role: assistant`; those describe external formats, not orchestrator roles.

## Verification

Run:

```bash
python3 -m py_compile scripts/reviewer_contract.py scripts/reviewer_sessions.py scripts/reviewer_jobs.py
python3 -m unittest discover -s tests
```

# Project Manager Slice Reviewer Contract

This is the compact delegation contract embedded in a Project Manager slice prompt. The slice session is the **Developer**. Delegated **Reviewers** are read-only evidence providers.

## Role boundary

The Developer owns implementation, tests, fixes, gates, the slice commit when authorized, and the structured result. Reviewers only investigate, gather evidence, verify the plan, perform drift audit, or perform code review. They never edit, run mutation-prone commands, perform Git/GitHub mutations, commit, re-delegate, approve drift, or make final acceptance decisions.

Keep work local when review adds no value. A default slice may use Developer self-audit, but provenance must say `developer-self-audit`. A slice marked `Independent audit required: yes` requires separate validated Reviewer launches.

## Policy and request

`reviewer-policy.json` is authoritative for run/slice identity, plan digest, repository, artifact root, and selected tool/model/effort. Copy values rather than retyping them.

Write one schema-v2 `reviewer-request.json` per bounded task and launch only through `reviewer_jobs.py`. Requests contain no `role` or `access`; the launcher records `reviewer` and `read-only`. Old Worker keys and schema v1 are invalid.

Do not set, unset, redirect, or invent tool-home or credential variables. Report authentication failure as a blocker.

## Independent audit sequence

1. Launch a request whose only required skill is `drift-audit`.
2. Wait, extract, and require a passing authorization result.
3. Launch a separate request whose only required skill is `code-review`.
4. Wait, extract, and decide whether findings require an in-contract Developer fix, a stop, or post-plan consideration.

Do not run these audits in parallel.

## Helper lifecycle

1. `reviewer_jobs.py init --prefix reviewers`
2. `reviewer_jobs.py launch --run-dir <run-dir> --policy <policy> --request <request>`
3. `reviewer_jobs.py activity`
4. `reviewer_jobs.py wait`
5. `reviewer_jobs.py extract`
6. `reviewer_jobs.py cancel` when necessary

Silence alone is not failure while the process is healthy. A clean exit is insufficient when the contracted answer is missing, refused, or replaced by a question; correct the request and relaunch with an `-rN` label.

## Evidence

Write `reviewer-evidence.md` when any Reviewer is used. Record each label, tool, bounded purpose, run directory, extraction command, result summary, sufficiency decision, and factual isolation boundary.

PM verifies normalized process evidence and repository mutation gates. The Developer remains responsible for semantic sufficiency. Reports must identify each audit as `reviewer` or `developer-self-audit` and include the successful Reviewer tool/label when applicable.

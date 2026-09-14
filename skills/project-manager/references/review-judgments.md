# Developer and reviewer judgments

Record PM’s assessment of each Developer submission and each reviewer report as a 0–2 rating in signed `run.json`. Code-review panels also retain their independent best-first ordering and ties. These subjective judgments supplement the final model-performance narrative; they do not determine acceptance, review freshness or model selection.

## Judgment rubric and timing

Use **0 — unacceptable**, **1 — acceptable**, and **2 — excellent**, with evidence appropriate to the role:

| Role | 0 — unacceptable | 1 — acceptable | 2 — excellent |
| --- | --- | --- | --- |
| Developer | Materially incomplete, incorrect, out-of-scope or misleading work requiring substantial correction | Useful implementation meeting the expected standard, with ordinary limitations or minor corrections | Notably sound, complete and well-validated work, accurately reported, with little avoidable correction |
| Drift or code reviewer | Materially misleading, unsupported or unhelpful output requiring substantial rework | Useful, adequately evidenced output with ordinary limitations or minor mistakes | Notably accurate, specific and actionable work providing meaningful assurance with little avoidable noise |

Rate the contribution against the instructions and evidence available to PM. A sound, thorough clean review can earn 2; finding a real defect and returning FAIL can also earn 2. Never derive utility mechanically from verdict, finding count/severity or Developer acceptance. Do not invent omissions PM has not established. An accepted Developer submission does not automatically earn 2; an earlier failed submission does not prevent an excellent correction earning 2.

Judge each drift report before deciding on code review. Rate each code report, and separately order the chosen panel’s usefulness best first with ties. Weigh valid in-scope findings, reproducible evidence, actionable advice, false positives/overreach and established material omissions. Two reports can both earn 2 while one ranks higher. A singleton has an absolute rating but no comparative score. Do not commission extra reviews merely to fill a ranking.

Rate an assessed Developer submission before accepting, steering, relaunching or stopping it, including when PM’s own checks needed no commissioned review. Repeated checks or reviewer commissions do not create additional Developer ratings. Preserve earlier attempts instead of replacing their scores with the final outcome. Give one concise reason identifying the evidence behind each rating or comparison. Missing judgments never prevent emergency stopping.

## Commands and reviewer input

Run from inside the supervised repository, using the toolkit’s absolute path if necessary. Keep `PM_RUN_TOKEN` in the controller environment only:

```sh
python3 /path/to/ai-agent-coder/skills/project-manager/scripts/pm.py \
  judge-reviews --file /tmp/review-judgment.json
```

Both `judge-reviews` and `judge-developer` accept `--run` and `--token` through the normal toolkit path. Each input file is one JSON object, not a second authoritative store. `schema_version` must be integer 1. No command automatically rewrites historical records.

An individual reviewer rating uses the same payload for either review skill:

```json
{
  "schema_version": 1,
  "slice": "Slice 1",
  "skill": "code-review",
  "review_id": "review-2",
  "score": 2,
  "reason": "Found the boundary defect; PM reproduced it against the contract."
}
```

For drift, use `"skill": "drift-audit"` and its review ID. Record a code-panel comparison independently:

```json
{
  "schema_version": 1,
  "slice": "Slice 1",
  "skill": "code-review",
  "rank_groups": [["review-2"], ["review-3", "review-4"]],
  "reason": "Review 2 found the verified defect; 3 and 4 supplied equally useful checks."
}
```

Use `[["review-2"]]` for a singleton. Each participating successful commission appears exactly once; PM chooses the panel, not every review ever commissioned for the slice. A panel’s order never supplies its members’ absolute ratings.

An unavailable individual rating uses `assessment: "rating"`, `status: "unavailable"` and one `review_ids` entry. An unavailable code comparison uses the following form:

```json
{
  "schema_version": 1,
  "slice": "Slice 1",
  "skill": "code-review",
  "assessment": "comparison",
  "status": "unavailable",
  "review_ids": ["review-2", "review-3"],
  "reason": "Different adjudication instructions prevent a fair comparison."
}
```

Unavailable judgments have no score or rank. An unavailable comparison does not make its individual reports unrateable. For compatibility, unavailable drift input without `assessment` means a rating; unavailable code input without it means a comparison. Unknown IDs and altered/missing report artifacts remain named errors, not unavailable judgments. A failed subprocess without a successful review record remains outside the reviewer population. Unjudged means the relevant rating or comparison has not been recorded.

## Developer input and submission identity

Use `judge-developer --file /tmp/developer-judgment.json` with the originating event index shown by `status`:

```json
{
  "schema_version": 1,
  "slice": "Slice 1",
  "origin_event_index": 1,
  "score": 1,
  "reason": "Implementation meets the slice contract; PM reran validation and verified the diff."
}
```

The index is zero-based in the mapping entries returned by `read_events`, not a human attempt number or PM’s resettable budget counter. It identifies the submission’s launch/relaunch/steer event. A new judgment must reference the current Developer attempt; historical exact retries and corrections resolve an existing judgment. This intentionally avoids guessing historical commits or model identities after a submission has passed without a rating.

For an assessment PM cannot make, replace `score` with `"status": "unavailable"` and explain why. Unavailable is not 0; operational failure without assessable work is not automatically a bad substantive contribution. Historical attempts missed before their decision remain explicitly unjudged.

Developer judgments retain the originating event, evaluated `head`, `before_head`, authorization revision (`grants_seen`), and the resolved Developer tool/model/effort recorded at launch. Steers inherit the session’s identity. Unspecified configuration and model/effort ignored by a custom command remain null; they never identify an assumed CLI default. HEAD identifies the recorded Git revision, not proof that the working tree was clean or that the implementation was correct. The rationale states the evidence PM actually checked.

## Stored contract and coverage

Reviewer judgments remain in `slices[].review_judgments`. New records carry `assessment: "rating"` or `"comparison"`, `schema_version: 1`, generated `judgment_id: "judgment-N"`, UTC `at`, `skill`, `reason`, and the applicable input payload. Historical drift records are ratings; historical code records are comparisons. Developer judgments live in `slices[].developer_judgments` with generated `developer-judgment-N` IDs, timestamp, reason, score/status and the stored `submission` and `developer` context. Slice identity is inherited from the enclosing entry. No normalized points or model averages are stored.

Successful `reviews[]` entries retain stable `review_id: "review-N"`, tool/model/effort, `origin_event: {index, kind, slice}` and `review_context: {pm_adjudications, drift_review}`. `pm_adjudications` is null or supplied text; `drift_review` is null or `{review_id, artifact, sha256}` identifying the drift report handed to a code reviewer. Join reviewer judgments by `(run_id, slice.id, review_id)` and Developer judgments by `(run_id, slice.id, submission.origin_event.index)`, never by list position, model or artifact content.

A ranked panel must share originating attempt, `head`, `before_head`, `grants_seen` and adjudication/drift-report context. The toolkit resolves these from review records and verifies report hashes. Differing contexts can be marked unavailable instead of forcing a ranking. Independent absolute ratings need no shared panel context.

`status` and the generated `run-report.md` distinguish missing absolute ratings, code reports outside recorded panels, unavailable, singleton/unranked and superseded records. Reports outside panels are informational comparison coverage, not a requirement to rank every report; a deliberately excluded report needs its individual rating but no invented comparison. Human attempt labels count launch/relaunch/steer events for the slice starting at 1; machine budget counters keep their existing semantics. Old runs without the new fields remain readable with explicit historical/unjudged coverage. No migration or inferred backfill occurs.

## Corrections, recovery and harvesting

Exact retries return the existing judgment. Conflicting judgments within the same assessment kind are rejected. To correct one, add `"supersedes": "judgment-N"` (or `"developer-judgment-N"`) to its replacement input. Rating and comparison coverage are independent: correcting one never replaces the other. A changed Developer submission within an already-rated originating attempt requires explicit supersession. Historical corrections preserve their recorded submission identity instead of acquiring the current session’s model or revision.

Earlier records remain. Consumers exclude IDs referenced by later `supersedes` links from active coverage within the same collection. This is a small audit trail, not an editable leaderboard.

Each command authenticates and updates the latest locked state, preserving reviews completed concurrently, then appends a concise judgment event. Signed `run.json` is authoritative even if event publication fails. Resolve the publication error and retry the exact input to publish its missing event. A malformed or truncated event log is a named error requiring recovery first; judgment commands do not silently skip or repair append-only history. Never edit `run.json` or its MAC by hand.

Read-only consumers harvest `<worktree-git-dir>/pm/<run-id>/run.json`, where `git rev-parse --absolute-git-dir` resolves the supervised worktree’s Git directory. No Markdown parsing is needed. Keep Developer, drift and code ratings separate by role, retain first/final Developer trajectories, and label them as PM assessments. Independent benchmark correctness and comparative reviewer rank points remain separate measures. `model-performance.md` stays final qualitative context and must never fabricate structured values. External harvesting, aggregation and benchmark changes are separate work.

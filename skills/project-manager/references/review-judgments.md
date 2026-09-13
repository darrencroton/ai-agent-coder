# Per-round reviewer judgments

Record PM's own assessment of reviewer usefulness in signed `run.json`, at `slices[].review_judgments`. These subjective judgments supplement review reports and final model-performance prose; they do not determine acceptance, review freshness or model selection.

## Judgment rubric

For each drift audit, use **0 — unacceptable** for materially misleading, unsupported or unhelpful output requiring substantial rework; **1 — acceptable** for useful, adequately evidenced output with ordinary limitations or minor mistakes; **2 — excellent** for notably accurate, specific and actionable work providing meaningful assurance with little avoidable noise. A sound, thorough clean review can earn 2; finding a real defect and returning FAIL can also earn 2. Never derive utility mechanically from verdict, finding count/severity or Developer acceptance. Do not invent omissions PM has not established.

For a chosen code-review panel, order usefulness best first with ties. Weigh valid in-scope findings, reproducible evidence, actionable advice, false positives/overreach and material omissions PM actually established. Give one concise rationale identifying the evidence behind each drift judgment or panel comparison. A singleton is recorded but has no comparative score. Do not commission extra reviews merely to fill a ranking.

## Command input

Run from inside the supervised repository, using the toolkit's absolute path if necessary. Keep `PM_RUN_TOKEN` in the controller environment only:

```sh
python3 /path/to/ai-agent-coder/skills/project-manager/scripts/pm.py \
  judge-reviews --file /tmp/drift-judgment.json
```

The input file is a single JSON object, not a second authoritative store. `schema_version` must be integer 1. Input includes `slice`, `skill`, `reason` and exactly one role payload. `--run` resolves historical runs through the normal toolkit path; no command rewrites existing historical records automatically.

Drift input:

```json
{
  "schema_version": 1,
  "slice": "Slice 1",
  "skill": "drift-audit",
  "review_id": "review-1",
  "score": 2,
  "reason": "Found unsupported coercion; PM reproduced the finding."
}
```

Code-panel input (best first, with a tie):

```json
{
  "schema_version": 1,
  "slice": "Slice 1",
  "skill": "code-review",
  "rank_groups": [["review-2"], ["review-3", "review-4"]],
  "reason": "Review 2 found the verified defect; 3 and 4 supplied equally useful checks."
}
```

Use `[["review-2"]]` for a singleton. Each participating successful commission appears exactly once; PM chooses the panel, not every review ever commissioned for the slice.

Unavailable/not-comparable input:

```json
{
  "schema_version": 1,
  "slice": "Slice 1",
  "skill": "code-review",
  "status": "unavailable",
  "review_ids": ["review-2", "review-3"],
  "reason": "Different adjudication instructions prevent a fair comparison."
}
```

Unavailable judgments have no score or rank. A drift unavailable judgment references one drift review. This covers recorded output PM cannot assess or code opportunities PM cannot compare. Unknown IDs and altered/missing report artifacts are named errors, not unavailable judgments; a failed subprocess without a successful review record remains outside the population. Unjudged is distinct: no active judgment references the review.

## Stored contract and identity

Each judgment stores `schema_version: 1`, toolkit-generated `judgment_id: "judgment-N"` and UTC `at`, `skill`, `reason`, and the input's role payload (`review_id`/`score`, `rank_groups`, or `status`/`review_ids`). Slice identity is inherited from the enclosing slice entry, not duplicated. Optional `supersedes` links to an earlier judgment in that slice. No normalized points or model averages are stored.

Successful `reviews[]` entries retain their existing fields and add `review_id: "review-N"` from the already allocated per-slice commission sequence, resolved `effort` (or null), `origin_event: {index, kind, slice}` and `review_context: {pm_adjudications, drift_review}`. The event index is zero-based in the mapping entries returned by `read_events`; `kind` is launch/relaunch/steer. `pm_adjudications` is null or the supplied text. `drift_review` is null or `{review_id, artifact, sha256}` identifying the drift report handed to a code reviewer. Join judgments to reviews by `(run_id, slice.id, review_id)`, never by list position, model or artifact content. An unspecified model/effort, or one ignored by a custom command override, remains null; it does not identify the CLI's actual default model.

A ranked panel must share originating attempt, `head`, `before_head`, `grants_seen` (the append-only authorization revision), and adjudication/drift-report context. The toolkit resolves this from review records and verifies report hashes; PM does not retype it. Context comparisons are conservative: differing instructions can be marked unavailable instead of forcing a ranking. Human attempt labels count launch/relaunch/steer events for the slice up to the stored originating event, starting at 1; machine budget counters retain their existing semantics.

## Corrections, recovery and harvesting

Exact retries return the existing judgment. Conflicting judgments and overlapping active coverage are rejected. To correct a judgment, add `"supersedes": "judgment-N"` to the replacement input. The earlier record remains; consumers exclude IDs referenced by later `supersedes` links from active coverage. This is a small audit trail, not an editable leaderboard.

The command authenticates and updates the latest locked state, preserving reviews completed concurrently, then appends a concise judgment event. Signed `run.json` is authoritative even if event publication fails. Once an append error is resolved, retry the exact command to publish its missing event, provided the log remains readable. A malformed or truncated `events.jsonl` is a named error requiring log recovery first; this command does not silently skip or repair append-only history. Never edit `run.json` or its MAC by hand.

On session recovery, `status` lists unjudged reviews. The generated `run-report.md` summarizes stored scores/panel order, rationale and attempt context, explicitly distinguishing singleton, unavailable, unjudged and superseded records. Missing judgments never add an acceptance-floor fact or prevent stopping. Existing `lite-1` runs remain readable: absent identity/context/judgment fields mean historical/unjudged, not corruption; no migration or inferred backfill occurs.

Read-only consumers harvest `<worktree-git-dir>/pm/<run-id>/run.json`, where `git rev-parse --absolute-git-dir` resolves the supervised worktree's git directory. They need no Markdown parsing. `model-performance.md` remains final qualitative context and must never fabricate these structured values. External harvesting and normalized rank calculations are separate work; this change does not implement a consumer.

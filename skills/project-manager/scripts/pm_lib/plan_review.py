"""`pm.py review-plan`: one plan-review round of a campaign node, through the orchestrator.

The Campaign Manager (CM) reviews every node plan with a read-only delegate
before the node launches, round by round until the review comes back clean.
This module composes each round's orchestrator policy and request from the
node directory (the label and slice shapes, the plan's digest binding, which
product files may go in `files`, and the carry-forward of the last reported
round's report and the CM's dispositions), launches it, and records the
round under `<node dir>/reviews/round-<N>/`, a directory never reused.

It decides nothing. The `RESULT:` line it prints is the reviewer's, quoted;
the clean-or-revise reading, whether a finding is right, and the dispositions
written for the next round are the CM's judgement (VISION principle 2). What
it does check is the evidence shape it hands back: a round whose delegate
failed, timed out, broke its `SECTION:` contract, or returned an empty report
or one that does not open with `RESULT: clean` or `RESULT: revise` is an
error, so a broken round can never read as a review.

Every round is a fresh request rather than an orchestrator continuation: the
prior report and dispositions travel inline in `context`, which needs no
captured session and works the same on every harness. The orchestrator is
run as a subprocess, never imported (see the package docstring): its run
state goes under the `ORCHESTRATOR_ARTIFACT_ROOT` override, `--results` or
`<round dir>/orchestrator/` by default, which lies inside the dossier
repository, so the dossier git-ignores `nodes/*/reviews/*/orchestrator/`
(`launch-child` refuses a node directory with untracked files).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import PmError
from . import git_ops
from . import plan as plan_mod
from . import sandbox
from . import sessions

# The orchestrator's launcher, found by a path relative to this file as
# `code_metrics` finds `health.py`. Module-level so a test can point it at a
# stand-in script.
_DELEGATE_JOBS = Path(__file__).resolve().parents[3] / "orchestrator" / "scripts" / "delegate_jobs.py"

_ARTIFACT_ROOT_ENV = "ORCHESTRATOR_ARTIFACT_ROOT"

REVIEWS_DIR = "reviews"
_ROUND_RE = re.compile(r"^round-(\d+)$")

# A backstop against a reviewer that hangs, as `review --timeout` is: sized so
# only a stuck process reaches it.
DEFAULT_TIMEOUT_SECONDS = 3600.0

# How much of a failed step's output an error quotes.
_TAIL_CHARS = 2000

# A `## D<n>: <title>` entry in decisions.md runs to the next level-2 heading.
_DECISION_HEADING_RE = re.compile(r"^## D(\d+):", re.MULTILINE)
_LEVEL2_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_DECISION_CITE_RE = re.compile(r"\bD(\d+)\b")
_RESULT_RE = re.compile(r"^RESULT: (clean|revise)\b")
# The orchestrator's label shape allows only lowercase letters, digits and
# hyphens in each part.
_LABEL_UNSAFE_RE = re.compile(r"[^a-z0-9]+")

# The plan-review template's constraints and output contract
# (skills/orchestrator/references/templates.md, *Plan review*), adapted to a
# fresh request per round: the dispositions section answers the previous
# round from the context rather than from a resumed session.
_EXPECTED_OUTPUT = (
    "First line RESULT: clean or RESULT: revise. SECTION: FINDINGS lists items labelled FIX (the plan must "
    "change) or NOTE (optional), each naming the plan section it concerns and the change in one line. "
    "SECTION: DISPOSITIONS lists each earlier-round finding and whether it is resolved (rounds after the "
    "first). SECTION: OPEN_QUESTIONS. Use - none for an empty section."
)


@dataclass(frozen=True)
class ReviewRound:
    """What `review_plan` hands back for the CLI to print."""

    round_dir: Path
    label: str
    result_line: str
    report: Path


def next_round(node_dir: Path) -> int:
    """One more than the highest `reviews/round-<N>` in the node directory, 1 when none."""
    reviews = node_dir / REVIEWS_DIR
    numbers = [
        int(match.group(1))
        for child in (reviews.iterdir() if reviews.is_dir() else ())
        if child.is_dir() and (match := _ROUND_RE.match(child.name))
    ]
    return max(numbers, default=0) + 1


def round_dir(node_dir: Path, number: int) -> Path:
    return node_dir / REVIEWS_DIR / f"round-{number}"


def _last_reported_round(node_dir: Path, before: int) -> Path | None:
    """The highest `round-<N>` below `before` whose delegate ended `completed`
    (its recorded `status.json`) and whose `report.md` opens with a `RESULT:`
    line, or None: a failed round keeps whatever came back as its report, so
    neither a report nor a result line alone makes a round one to disposition."""
    for number in range(before - 1, 0, -1):
        candidate = round_dir(node_dir, number)
        try:
            status = json.loads((candidate / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        report = _read_optional(candidate / "report.md")
        first = next((line.strip() for line in (report or "").splitlines() if line.strip()), "")
        if isinstance(status, dict) and status.get("state") == "completed" and _RESULT_RE.match(first):
            return candidate
    return None


def build_round(
    *,
    node_dir: Path,
    dossier: Path,
    repo: Path,
    seat: tuple[str, str, str | None],
    number: int,
    artifact_root: Path,
) -> tuple[dict, dict]:
    """The orchestrator policy and request for round `number` of the node at `node_dir`.

    Reads only; launches nothing, so it is testable on its own. The plan and
    the node's protected-paths policy are inlined in `context` because they
    live in the dossier, outside the product repository, and the orchestrator
    refuses a `files` entry outside it; the plan's digest is still bound,
    through the policy's `plan_path`, which the orchestrator hashes at launch.
    """
    node = node_dir.name
    plan_path = node_dir / "plan.md"
    policy_path = node_dir / "policy.json"
    for required in (plan_path, policy_path):
        if not required.is_file():
            raise PmError(f"the node directory has no {required.name}: {required}")
    plan_text = plan_path.read_text(encoding="utf-8")
    tool, model, effort = seat
    slug = _LABEL_UNSAFE_RE.sub("-", node.lower()).strip("-")
    if not slug:
        raise PmError(f"node name {node!r} has no letter or digit to build an orchestrator label from")
    # Each round is its own run directory, so the label need only name the
    # round; an `-r<N>` suffix would read as the orchestrator's continuation
    # marker, which a fresh request is not.
    label = f"01-{tool}-{slug}-plan-review-round{number}"
    slice_id = f"{node}-plan-review"

    policy = {
        "schema_version": 3,
        "run_id": f"{dossier.name}-{node}-plan-review",
        "slice_id": slice_id,
        "plan_path": str(plan_path.resolve()),
        "repo_path": str(repo),
        "delegate_artifact_root": str(artifact_root),
        "required_tools": [tool],
        "required_model": model,
        "required_effort": effort or "default",
        "required_access": ["read-only"],
    }

    # The round carried forward is the newest one that produced a report: a
    # failed round keeps its directory (it is never reused) but has nothing
    # to disposition, and skipping it would tell the reviewer "none" and
    # lose the CM's dispositions of the last good round.
    previous = _last_reported_round(node_dir, number)
    prior = previous.name if previous else "previous round"
    prior_report = _read_optional(previous / "report.md") if previous else None
    prior_dispositions = _read_optional(previous / "dispositions.md") if previous else None
    if previous is None:
        task = f"Round {number}: review the {node} plan (inlined)."
    else:
        task = f"Round {number}: review the revised {node} plan (inlined) and disposition each {prior} finding."
    dispositioned = f"those the {prior} dispositions in the context settle" if prior_dispositions else "none"
    context = "\n\n".join(
        [
            f"Node {node}, plan review round {number}. The plan lives in the campaign dossier, outside the "
            "product repository, so it is inlined here rather than listed in files; its SHA-256 is bound by "
            "the delegate policy's plan_path.",
            _block(f"nodes/{node}/plan.md", plan_text),
            "The node's protected-paths policy: every entry of the plan's authorized surface must stay "
            "disjoint from every protected path it lists.",
            _block(f"nodes/{node}/policy.json", policy_path.read_text(encoding="utf-8")),
            _block("decisions.md entries the plan cites", _cited_decisions(plan_text, dossier) or "none"),
            _block(f"{prior} report", prior_report or "none"),
            _block(f"{prior} dispositions (the Campaign Manager's)", prior_dispositions or "none"),
        ]
    )
    request = {
        "schema_version": 3,
        "label": label,
        "slice_id": slice_id,
        "tool": tool,
        "model": model,
        "effort": effort or "default",
        "access": "read-only",
        "task": task,
        "context": context,
        "required_skills": ["implementation-plan"],
        "files": _surface_files(plan_path, repo),
        "constraints": [
            "Review the plan only; do not review or propose code.",
            "Do not build, run make or tests, or write any file.",
            "Cite the plan section for every finding and path:line for any claim about the product.",
            f"Do not re-raise these dispositioned items: {dispositioned}.",
        ],
        "expected_output": _EXPECTED_OUTPUT,
    }
    return policy, request


def review_plan(
    *,
    node: Path,
    repo: Path,
    seat: tuple[str, str, str | None],
    results: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ReviewRound:
    """Run the next plan-review round of the node at `node` and record it.

    Fails closed on every step: a non-zero exit of any orchestrator step, a
    `wait` that fails (the orchestrator fails it on `failed` and on
    `incomplete`, a clean exit whose output never met the `SECTION:`
    contract) or times out (the delegate is then cancelled), an empty report,
    or one whose first non-blank line is not `RESULT: clean` or
    `RESULT: revise`. Whatever came back is still written to `report.md`
    first, so the CM can read it. The result is quoted, never interpreted.
    """
    node_dir = node.expanduser().resolve()
    if not node_dir.is_dir():
        raise PmError(f"node directory does not exist: {node_dir}")
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise PmError(f"product repository does not exist: {repo}")
    dossier = sandbox._git_toplevel(node_dir)
    if dossier is None:
        raise PmError(f"the node directory {node_dir} lies in no git repository; a campaign dossier is one")
    number = next_round(node_dir)
    this_round = round_dir(node_dir, number)
    artifact_root = results.expanduser().resolve() if results else this_round / "orchestrator"
    policy, request = build_round(
        node_dir=node_dir, dossier=dossier, repo=repo, seat=seat, number=number, artifact_root=artifact_root
    )

    this_round.mkdir(parents=True, exist_ok=False)
    policy_file = this_round / "delegate-policy.json"
    request_file = this_round / "request.json"
    for path, payload in ((policy_file, policy), (request_file, request)):
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    label = request["label"]
    env = {**sessions.seat_environ(), _ARTIFACT_ROOT_ENV: str(artifact_root)}

    def step(name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, str(_DELEGATE_JOBS), name, *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=this_round,
        )
        if check and completed.returncode != 0:
            raise PmError(
                f"review-plan round {number} ({this_round}): delegate_jobs.py {name} exited "
                f"{completed.returncode}: {_tail(completed)}"
            )
        return completed

    run_lines = step("init", "--prefix", "review-plan").stdout.strip().splitlines()
    if not run_lines:
        raise PmError(f"review-plan round {number} ({this_round}): delegate_jobs.py init printed no run directory")
    run_dir = run_lines[-1]
    step("launch", "--run-dir", run_dir, "--policy", str(policy_file), "--request", str(request_file))
    waited = step("wait", "--run-dir", run_dir, "--label", label, "--timeout", f"{timeout:g}", check=False)
    status = _status(step, run_dir, label)
    if waited.returncode != 0 and status.get("running"):
        # Timed out: a wait that gave up must not leave a reviewer running.
        step("cancel", "--run-dir", run_dir, "--label", label, check=False)
        status = _status(step, run_dir, label)
    # The composed command carries the whole rendered prompt (tens of
    # kilobytes); the request is recorded beside it and the orchestrator's run
    # directory keeps the command, so the round's status record omits it.
    recorded = {key: value for key, value in status.items() if key != "command"}
    (this_round / "status.json").write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # A failed delegate may have no output at all, which `extract` reports as
    # an error; that is the wait failure below, not an extract failure.
    extracted = step("extract", "--run-dir", run_dir, "--label", label, check=waited.returncode == 0)
    report_text = extracted.stdout if extracted.returncode == 0 else ""
    report = this_round / "report.md"
    if report_text.strip():
        report.write_text(report_text.rstrip() + "\n", encoding="utf-8")
    where = f"review-plan round {number} ({this_round})"
    kept = f"; the report is at {report}" if report.exists() else "; no report came back"

    if waited.returncode != 0:
        raise PmError(
            f"{where}: the delegate did not finish cleanly (state={status.get('state')}, "
            f"returncode={status.get('returncode')}){kept}"
        )
    first = next((line.strip() for line in report_text.splitlines() if line.strip()), "")
    if not first:
        raise PmError(f"{where}: the delegate's report is empty")
    if not _RESULT_RE.match(first):
        raise PmError(
            f"{where}: the report does not open with RESULT: clean or RESULT: revise (got {first[:80]!r}){kept}"
        )
    return ReviewRound(round_dir=this_round, label=label, result_line=first, report=report)


def _status(step, run_dir: str, label: str) -> dict:
    completed = step("status", "--run-dir", run_dir, "--label", label, "--json")
    try:
        entries = json.loads(completed.stdout)
        return next(entry for entry in entries if entry.get("label") == label)
    except (ValueError, TypeError, AttributeError, StopIteration):
        raise PmError(f"delegate_jobs.py status gave no readable entry for {label}: {_tail(completed)}") from None


def _cited_decisions(plan_text: str, dossier: Path) -> str:
    """The text of every `decisions.md` entry the plan cites by `D<n>`, in number order.

    An entry the plan cites that the file lacks (or a missing file) is
    skipped: the reviewer judges the plan with what the record holds, and an
    unresolvable citation is itself something it can report.
    """
    cited = {int(number) for number in _DECISION_CITE_RE.findall(plan_text)}
    decisions = _read_optional(dossier / "decisions.md")
    if not cited or decisions is None:
        return ""
    entries = []
    for heading in _DECISION_HEADING_RE.finditer(decisions):
        if int(heading.group(1)) not in cited:
            continue
        following = _LEVEL2_HEADING_RE.search(decisions, heading.end())
        entries.append(decisions[heading.start() : following.start() if following else len(decisions)].strip())
    return "\n\n".join(entries)


def _surface_files(plan_path: Path, repo: Path) -> list[str]:
    """The product files the plan's authorized surfaces name that exist now.

    The orchestrator refuses a `files` entry that is missing or outside the
    repository, and a plan names files it will create, so only existing
    regular files beneath `repo` are kept; a glob or subtree names no single
    file and is skipped.
    """
    files: list[str] = []
    for plan_slice in plan_mod.parse_plan(plan_path):
        for raw in plan_slice.authorized_files:
            entry = git_ops.normalize_authorized_entry(raw)
            if not entry or entry.endswith("/") or git_ops.has_glob(entry):
                continue
            resolved = (repo / entry).resolve()
            try:
                relative = resolved.relative_to(repo).as_posix()
            except ValueError:
                continue
            if resolved.is_file() and relative not in files:
                files.append(relative)
    return files


def _block(title: str, text: str) -> str:
    """One inlined document, fenced by lines no Markdown fence in it can close."""
    return f"===== BEGIN {title} =====\n{text.rstrip()}\n===== END {title} ====="


def _read_optional(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _tail(completed: subprocess.CompletedProcess) -> str:
    text = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    return text[-_TAIL_CHARS:] or "no output"

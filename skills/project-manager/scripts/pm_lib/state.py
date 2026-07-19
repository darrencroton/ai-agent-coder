"""Lite-1 single-copy authenticated state (target-design §8).

Authoritative state lives at ``<worktree_git_dir>/pm/<run-id>/`` — outside
the worktree, so a Developer session editing tracked files cannot touch it
directly. A run capability token (minted once at creation, never written to
disk in the clear) authenticates every mutating write with an HMAC; a read
that supplies the token verifies that HMAC first and treats any failure as
an integrity stop (``IntegrityError``), never as a retryable condition.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import IntegrityError, PmError
from .git_ops import worktree_git_dir

SCHEMA = "lite-1"
RUN_STATUSES = {"active", "needs-human", "complete", "stopped"}
SLICE_STATUSES = {"accepted", "attested", "stopped"}
RISK_LEVELS = {"standard", "elevated"}

_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_root(repo: Path) -> Path:
    return worktree_git_dir(repo) / "pm"


def new_run_id(existing: Iterable[str] | None = None) -> str:
    """UTC-timestamp run id; on collision against `existing`, append -2, -3, ...

    With no `existing` set given, the bare timestamp is returned — collision
    checking is the caller's responsibility (create_run performs it against
    the run directories already present under state_root).
    """
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not existing:
        return base
    taken = set(existing)
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


def mint_token() -> str:
    return secrets.token_hex(32)


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextmanager
def _advisory_lock(lock_path: Path) -> Iterator[None]:
    """Non-blocking advisory lock with bounded retry.

    A stale lock is reported, never stolen or deleted — the caller decides
    whether to wait longer or investigate the process holding it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PmError(
                        f"could not acquire the PM state lock at {lock_path} within "
                        f"{_LOCK_TIMEOUT_SECONDS:.0f}s; another PM process may be holding it — "
                        "do not delete the lock file"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _validate_shape(state: dict[str, Any]) -> None:
    """Validate only the fields PM reads; unknown extra fields are tolerated untouched."""
    schema = state.get("schema")
    if schema != SCHEMA:
        raise PmError(
            f"run state schema {schema!r} is not supported by this toolkit version "
            f"(expected {SCHEMA!r}); this toolkit does not migrate state — use a matching toolkit version"
        )
    status = state.get("status")
    if status not in RUN_STATUSES:
        raise PmError(f"run state has an invalid status: {status!r}")
    plan = state.get("plan")
    if not isinstance(plan, dict) or not plan.get("sha256"):
        raise PmError("run state is missing plan.sha256")
    slices = state.get("slices")
    if not isinstance(slices, list):
        raise PmError("run state 'slices' field must be a list")
    for entry in slices:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise PmError("run state has a slice entry without an id")
        entry_status = entry.get("status")
        if entry_status is not None and entry_status not in SLICE_STATUSES:
            raise PmError(f"run state slice {entry.get('id')!r} has an invalid status: {entry_status!r}")
        for field in ("risk", "plan_risk"):
            value = entry.get(field)
            if value is not None and value not in RISK_LEVELS:
                raise PmError(f"run state slice {entry.get('id')!r} has an invalid {field}: {value!r}")


def create_run(
    repo: Path,
    *,
    plan_path: Path,
    plan_sha256: str,
    slice_count: int,
    branch: str,
    harness: dict[str, Any],
    reviewer: dict[str, Any],
    policy: dict[str, Any],
    slices: list[dict[str, Any]],
    run_id: str | None = None,
) -> tuple[dict[str, Any], str, Path]:
    """Mint a token, build the lite-1 state dict, create the run dir, write it.

    `slices` is the pre-built slice-entry list; the caller derives each
    entry's status (None for pending, "attested" for operator-attested prior
    completion) and risk fields from the parsed plan.
    """
    root = state_root(repo)
    if run_id is None:
        existing = {child.name for child in root.iterdir() if child.is_dir()} if root.exists() else set()
        run_id = new_run_id(existing)
    run_dir = root / run_id
    if run_dir.exists():
        raise PmError(f"PM run directory already exists: {run_dir}")

    token = mint_token()
    now = _utc_now_iso()
    state: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "created_at": now,
        "updated_at": now,
        "status": "active",
        "repo": str(repo),
        "branch": branch,
        "plan": {"path": str(plan_path), "sha256": plan_sha256, "slice_count": slice_count},
        "harness": harness,
        "reviewer": reviewer,
        "policy": policy,
        "auth": {"token_sha256": token_sha256(token)},
        "current_slice": None,
        "slices": slices,
        "approvals": {},
        "stop_reason": None,
    }
    _validate_shape(state)

    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        save_state(run_dir, state, token)
        (run_dir / "events.jsonl").touch(exist_ok=True)
        set_current(repo, run_id)
    except BaseException:
        # Creation is not atomic across multiple files; a failure partway
        # through should not leave a half-initialized run masquerading as
        # a real one on disk. Best-effort cleanup, not a hard guarantee.
        for child in sorted(run_dir.glob("*"), reverse=True):
            try:
                child.unlink()
            except OSError:
                pass
        try:
            run_dir.rmdir()
        except OSError:
            pass
        raise
    return state, token, run_dir


def save_state(run_dir: Path, state: dict[str, Any], token: str) -> None:
    """Verify the token, bump updated_at, write run.json and its MAC atomically."""
    if token_sha256(token) != state.get("auth", {}).get("token_sha256"):
        raise PmError("run capability token does not match this run's state")
    state = dict(state)
    state["updated_at"] = _utc_now_iso()
    _validate_shape(state)
    payload = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with _advisory_lock(run_dir / ".lock"):
        _atomic_write_bytes(run_dir / "run.json", payload)
        mac = hmac.new(token.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        _atomic_write_bytes(run_dir / "run.json.mac", (mac + "\n").encode("utf-8"))


def load_state(run_dir: Path, token: str | None = None) -> dict[str, Any]:
    """Load run.json, optionally MAC-verified.

    Without a token, only shape validation runs (read-only commands).
    With a token: the token is checked against `auth.token_sha256` first —
    a wrong token is a plain PmError, distinct from a tampered/unverifiable
    state, which is an IntegrityError. Only once the token is confirmed
    correct is the MAC checked against the actual file bytes; a mismatch
    there (or a missing MAC file) means the state itself was written or
    edited by something that didn't hold the token, i.e. tampered.
    Callers that mutate state MUST pass the token.
    """
    run_json = run_dir / "run.json"
    if not run_json.exists():
        raise PmError(f"run state not found: {run_json}")

    if token is None:
        payload = run_json.read_bytes()
    else:
        # Verified reads take the same advisory lock the writer holds:
        # save_state replaces run.json and its MAC as two files, so an
        # unlocked reader could pair new JSON with the old MAC and report a
        # false — and terminal — integrity breach.
        with _advisory_lock(run_dir / ".lock"):
            payload = run_json.read_bytes()
            mac_path = run_dir / "run.json.mac"
            recorded_mac = mac_path.read_text(encoding="utf-8").strip() if mac_path.exists() else None

    try:
        state = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PmError(f"run state is not valid JSON: {run_json} ({exc})") from exc

    if token is not None:
        auth = state.get("auth")
        recorded_hash = auth.get("token_sha256") if isinstance(auth, dict) else None
        if recorded_hash != token_sha256(token):
            raise PmError("run capability token does not match this run's state")
        if recorded_mac is None:
            raise IntegrityError(f"run state MAC missing: {run_dir / 'run.json.mac'}")
        expected = hmac.new(token.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, recorded_mac):
            raise IntegrityError(f"run state failed MAC verification: {run_json}")

    _validate_shape(state)
    return state


def verify_state_mac(run_dir: Path, token: str) -> None:
    """Explicit MAC check, independent of loading. Raises IntegrityError on any mismatch.

    Locked for the same reason as the verified-read path in load_state: the
    JSON and its MAC are two files, and only the lock makes them a pair.
    """
    run_json = run_dir / "run.json"
    if not run_json.exists():
        raise PmError(f"run state not found: {run_json}")
    with _advisory_lock(run_dir / ".lock"):
        mac_path = run_dir / "run.json.mac"
        if not mac_path.exists():
            raise IntegrityError(f"run state MAC missing: {mac_path}")
        payload = run_json.read_bytes()
        recorded_mac = mac_path.read_text(encoding="utf-8").strip()
    expected = hmac.new(token.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, recorded_mac):
        raise IntegrityError(f"run state failed MAC verification: {run_json}")


def append_event(
    run_dir: Path, kind: str, *, slice_id: str | None = None, note: str = "", evidence: str | None = None
) -> None:
    """Append one JSON line to events.jsonl. Never rewrites run.json."""
    event: dict[str, Any] = {"ts": _utc_now_iso(), "kind": kind, "slice": slice_id, "note": note}
    if evidence is not None:
        event["evidence"] = evidence
    line = json.dumps(event, sort_keys=True) + "\n"
    with _advisory_lock(run_dir / ".lock"):
        with open(run_dir / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write(line)


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def resolve_run_dir(repo: Path, run_id: str | None = None) -> Path:
    """Explicit id wins; else the `current` pointer. Missing pointer/dir raises PmError."""
    root = state_root(repo)
    if run_id is None:
        pointer = root / "current"
        if not pointer.exists():
            raise PmError(
                f"no current PM run recorded under {root}; pass --run explicitly or start a run with init"
            )
        run_id = pointer.read_text(encoding="utf-8").strip()
        if not run_id:
            raise PmError(f"current run pointer at {pointer} is empty; pass --run explicitly")
    run_dir = root / run_id
    if not run_dir.is_dir():
        raise PmError(f"PM run {run_id!r} not found under {root}")
    return run_dir


def set_current(repo: Path, run_id: str) -> None:
    root = state_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    pointer = root / "current"
    _atomic_write_bytes(pointer, run_id.encode("utf-8"))


def render_run_report(state: dict[str, Any], events: list[dict[str, Any]], run_dir: Path) -> str:
    """Render `run-report.md` from controller-owned data alone.

    Reads only `state`, `events`, and each accepted/stopped slice's
    `assessment.md` text from its ORIGINAL path under `run_dir` — never
    from the `.pm/` mirror. This is what makes the report regenerate
    correctly with `.pm/` deleted entirely (target-design §9): the caller
    (`slice_ops.regenerate_report`) writes this text to the original and
    mirrors it, but nothing in this function itself touches `.pm/`.
    """
    lines: list[str] = []
    run_id = state.get("run_id")
    plan_info = state.get("plan") or {}
    harness_info = state.get("harness") or {}
    plan_sha = (plan_info.get("sha256") or "")[:12]

    lines.append(f"# PM Run Report: {run_id}")
    lines.append("")
    lines.append(f"- Repo: {state.get('repo')}")
    lines.append(f"- Branch: {state.get('branch')}")
    lines.append(f"- Plan: {plan_info.get('path')} (sha256 {plan_sha}…)")
    lines.append(
        f"- Harness: {harness_info.get('name')} model={harness_info.get('model')} "
        f"effort={harness_info.get('effort')}"
    )
    lines.append(f"- Status: {state.get('status')}")
    lines.append(f"- Stop reason: {state.get('stop_reason')}")
    lines.append("")

    lines.append("## Slices")
    lines.append("")
    lines.append("| id | title | risk | status | attempts | commit | decision |")
    lines.append("|---|---|---|---|---|---|---|")
    slices = state.get("slices") or []
    for entry in slices:
        risk = entry.get("risk")
        plan_risk = entry.get("plan_risk")
        risk_display = risk if risk == plan_risk or not plan_risk else f"{risk} (plan: {plan_risk})"
        commit = entry.get("commit")
        commit_short = commit[:10] if commit else "-"
        lines.append(
            f"| {entry.get('id')} | {entry.get('title')} | {risk_display} | "
            f"{entry.get('status') or 'pending'} | {entry.get('attempts', 0)} | {commit_short} | "
            f"{entry.get('decision') or ''} |"
        )
    lines.append("")

    lines.append("## Assessments")
    any_assessment = False
    for entry in slices:
        assessment_path = entry.get("assessment")
        if not assessment_path:
            continue
        any_assessment = True
        lines.append("")
        lines.append(f"### {entry.get('id')}: {entry.get('title')}")
        lines.append("")
        try:
            lines.append(Path(assessment_path).read_text(encoding="utf-8").rstrip())
        except OSError as exc:
            lines.append(f"(assessment could not be read: {exc})")
    if not any_assessment:
        lines.append("")
        lines.append("(no slice has been accepted or stopped yet)")
    lines.append("")

    lines.append("## Approvals")
    approvals = state.get("approvals") or {}
    if approvals:
        for slice_id, record in approvals.items():
            lines.append(f"- {slice_id}: {record.get('reason')} (at {record.get('at')})")
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("## Evidence")
    mirror_dir = Path(str(state.get("repo") or "")) / ".pm" / "runs" / str(run_id)
    lines.append(f"- State dir: {run_dir}")
    lines.append(f"- Mirror dir: {mirror_dir}")
    lines.append(f"- Events recorded: {len(events)}")
    lines.append("")

    return "\n".join(lines)

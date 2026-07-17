"""Operational-hint extraction from live harness evidence (pane text, transcripts).

These hints are intentionally advisory except for hard-stop categories: they give
the supervising PM model compact evidence without turning Python into a broad
natural-language decision engine. See docs/VISION.md's trust boundary — hints
are operational judgment support, never an acceptance path.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .constants import EXTERNAL_SIDE_EFFECT_PROMPT_RE


def _excerpt(text: str, start: int, end: int, context: int = 120) -> str:
    lower = max(0, start - context)
    upper = min(len(text), end + context)
    return re.sub(r"\s+", " ", text[lower:upper]).strip()


def _parse_duration_seconds(text: str) -> int | None:
    lowered = text.lower()
    total = 0
    matched = False
    for pattern, multiplier in (
        (r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", 3600),
        (r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b", 60),
        (r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", 1),
    ):
        for match in re.finditer(pattern, lowered):
            total += int(float(match.group(1)) * multiplier)
            matched = True
    if matched:
        return max(1, total)
    return None


def _parse_absolute_reset_at(text: str, now: datetime, max_single_pause_seconds: int) -> tuple[datetime | None, bool]:
    local_now = now if now.tzinfo is not None else now.astimezone()
    timezone_match = re.search(
        r"\b(?:reset|resets|resetting|try again|available again|resume)\b[^.\n]{0,80}?\b(?:at|after)\s+"
        r"(?P<stamp>\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s*(?:UTC|GMT|[A-Z]{2,5}|[+-]\d{2}:?\d{2}))?)",
        text,
        flags=re.IGNORECASE,
    )
    if not timezone_match:
        return None, False
    stamp = timezone_match.group("stamp").strip()
    zone_match = re.search(r"\s*(?P<zone>UTC|GMT|[A-Z]{2,5}|[+-]\d{2}:?\d{2})$", stamp)
    zone_tz = local_now.tzinfo
    if zone_match and zone_match.group("zone") in {"AM", "PM"}:
        zone_match = None
    has_zone = zone_match is not None
    if zone_match:
        zone_token = zone_match.group("zone")
        if zone_token in {"UTC", "GMT"}:
            zone_tz = timezone.utc
        elif re.match(r"[+-]\d{2}:?\d{2}$", zone_token):
            sign = 1 if zone_token[0] == "+" else -1
            digits = zone_token[1:].replace(":", "")
            zone_tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        else:
            return None, True
    reset_now = local_now.astimezone(zone_tz)
    clock = re.match(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?", stamp, flags=re.IGNORECASE)
    if not clock:
        return None, True
    hour = int(clock.group("hour"))
    minute = int(clock.group("minute") or "0")
    ampm = (clock.group("ampm") or "").lower()
    if ampm:
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
    if hour > 23 or minute > 59:
        return None, True
    candidate = reset_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= reset_now:
        candidate += timedelta(days=1)
    wait_seconds = int((candidate - reset_now).total_seconds())
    if has_zone or 0 < wait_seconds <= max_single_pause_seconds:
        return candidate, False
    return None, True


def _reset_fields(text: str, now: datetime, max_single_pause_seconds: int) -> tuple[str | None, int | None, bool]:
    duration_scope = ""
    duration_intro = re.search(
        r"\b(?:in|after|within)\s+(?P<duration>[^.\n]{0,100})",
        text,
        flags=re.IGNORECASE,
    )
    if duration_intro:
        duration_scope = duration_intro.group("duration")
    retry_after = _parse_duration_seconds(duration_scope) if duration_scope else None
    if retry_after is not None:
        reset_at = now + timedelta(seconds=retry_after)
        return reset_at.replace(microsecond=0).isoformat(), retry_after, False
    absolute, ambiguous = _parse_absolute_reset_at(text, now, max_single_pause_seconds)
    if absolute is not None:
        return absolute.replace(microsecond=0).isoformat(), int((absolute - now.astimezone(absolute.tzinfo)).total_seconds()), False
    return None, None, ambiguous


def _hint(
    *,
    kind: str,
    subtype: str | None,
    confidence: str,
    hard_stop: bool,
    source: str,
    evidence_excerpt: str,
    now: datetime,
    reset_at: str | None = None,
    retry_after_seconds: int | None = None,
    recovery_guidance: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "confidence": confidence,
        "subtype": subtype,
        "reset_at": reset_at,
        "retry_after_seconds": retry_after_seconds,
        "hard_stop": hard_stop,
        "evidence_excerpt": evidence_excerpt,
        "source": source,
        "detected_at": now.replace(microsecond=0).isoformat(),
        "recovery_guidance": recovery_guidance,
    }


def extract_operational_hints(
    pane_text: str,
    *,
    transcript_text: str = "",
    process_running: bool = False,
    process_active: bool = False,
    result_exists: bool = False,
    now: datetime | None = None,
    max_single_pause_seconds: int = 21600,
) -> list[dict[str, Any]]:
    """Return lightweight operational hints from live harness evidence.

    These hints are intentionally advisory except for hard-stop categories. They
    give the supervising PM model compact evidence without turning Python into a
    broad natural-language decision engine.
    """
    observed_at = now if now is not None and now.tzinfo is not None else (now or datetime.now()).astimezone()
    hints: list[dict[str, Any]] = []
    sources = (("tmux-pane", pane_text or ""), ("transcript", transcript_text or ""))
    for source, text in sources:
        lowered = text.lower()
        if not lowered:
            continue
        usage_percent_match = re.search(
            r"\b(?:you(?:'ve| have)\s+used|used)\s+(\d{1,3})%\b[^.\n]{0,120}\b(?:hourly|daily|weekly|monthly|5[- ]?hour|five[- ]?hour)?\s*(?:usage\s*)?(?:limit|quota|cap)\b",
            lowered,
        )
        informational_usage_warning = bool(usage_percent_match and int(usage_percent_match.group(1)) < 100)
        conditional_limit_warning = "if you hit your limit" in lowered
        if informational_usage_warning or conditional_limit_warning:
            warning_match = usage_percent_match or re.search(r"\bif you hit your limit\b", lowered)
            if warning_match:
                hints.append(
                    _hint(
                        kind="usage_limit",
                        subtype="warning",
                        confidence="high" if usage_percent_match else "medium",
                        hard_stop=False,
                        source=source,
                        evidence_excerpt=_excerpt(text, warning_match.start(), warning_match.end()),
                        now=observed_at,
                        recovery_guidance="continue-with-observation",
                    )
                )

        for subtype, pattern in (
            ("weekly_window", r"\bweekly\b[^.\n]{0,80}\b(?:limit|quota|cap)\b|\b(?:limit|quota|cap)\b[^.\n]{0,80}\bweekly\b"),
            ("monthly_window", r"\bmonthly\b[^.\n]{0,80}\b(?:limit|quota|cap)\b|\b(?:limit|quota|cap)\b[^.\n]{0,80}\bmonthly\b"),
            (
                "account_or_billing",
                r"\b(?:account|billing|subscription|plan|credit|credits)\b[^.\n]{0,100}\b(?:limit|quota|cap|exhausted|upgrade|billing)\b",
            ),
        ):
            if informational_usage_warning or conditional_limit_warning:
                continue
            match = re.search(pattern, lowered)
            if match:
                hints.append(
                    _hint(
                        kind="usage_limit",
                        subtype=subtype,
                        confidence="high",
                        hard_stop=True,
                        source=source,
                        evidence_excerpt=_excerpt(text, match.start(), match.end()),
                        now=observed_at,
                        recovery_guidance="stop-for-user",
                    )
                )

        rolling_match = re.search(
            r"\b(?:5[- ]?hour|five[- ]?hour|rolling|session|usage)\b[^.\n]{0,140}\b(?:limit|quota|cap|reset|try again)\b|"
            r"\b(?:limit|quota|cap)\b[^.\n]{0,140}\b(?:reset|try again|in \d+|after \d+)\b",
            lowered,
        )
        if (
            rolling_match
            and not informational_usage_warning
            and not conditional_limit_warning
            and not any(h["kind"] == "usage_limit" and h["source"] == source and h["hard_stop"] for h in hints)
        ):
            # Scope reset parsing to a window around the matched limit text.
            # Scanning the whole pane let an unrelated duration phrase
            # elsewhere on screen ("completed in 5 minutes") masquerade as the
            # reset time; the window still covers the adjacent sentence
            # ("Usage limit reached. Try again in 3 hours.").
            reset_window = text[max(0, rolling_match.start() - 200): rolling_match.end() + 300]
            reset_at, retry_after, ambiguous = _reset_fields(reset_window, observed_at, max_single_pause_seconds)
            hard_stop = reset_at is None and retry_after is None
            subtype = "unknown_limit" if hard_stop or ambiguous else "rolling_window"
            if process_running and not result_exists and not hard_stop:
                guidance = "pause-until-reset-plus-buffer-then-send-continuation"
            elif result_exists:
                guidance = "finalize-slice"
            elif not process_running and not hard_stop:
                guidance = "restart-from-clean-authorized-state-or-stop-for-user"
            else:
                guidance = "stop-for-user"
            hints.append(
                _hint(
                    kind="usage_limit",
                    subtype=subtype,
                    confidence="high" if not hard_stop else "medium",
                    hard_stop=hard_stop,
                    source=source,
                    evidence_excerpt=_excerpt(text, rolling_match.start(), rolling_match.end()),
                    now=observed_at,
                    reset_at=reset_at,
                    retry_after_seconds=retry_after,
                    recovery_guidance=guidance,
                )
            )

        unknown_limit = re.search(r"\b(?:usage|session|rate|quota|limit|cap)\b[^.\n]{0,80}\b(?:reached|exceeded|exhausted)\b", lowered)
        if unknown_limit and not any(h["kind"] == "usage_limit" and h["source"] == source for h in hints):
            hints.append(
                _hint(
                    kind="usage_limit",
                    subtype="unknown_limit",
                    confidence="medium",
                    hard_stop=True,
                    source=source,
                    evidence_excerpt=_excerpt(text, unknown_limit.start(), unknown_limit.end()),
                    now=observed_at,
                    recovery_guidance="stop-for-user",
                )
            )

        explicit_service_match = re.search(r"\b(?:service unavailable|temporarily unavailable)\b", lowered)
        service_match = explicit_service_match or re.search(r"\b(?:try again later|overloaded|server error)\b", lowered)
        if service_match:
            retry_after = _parse_duration_seconds(text)
            hints.append(
                _hint(
                    kind="service_unavailable",
                    subtype="transient",
                    confidence="high" if explicit_service_match else "medium",
                    hard_stop=False,
                    source=source,
                    evidence_excerpt=_excerpt(text, service_match.start(), service_match.end()),
                    now=observed_at,
                    retry_after_seconds=retry_after,
                    recovery_guidance="bounded-retry",
                )
            )

        network_match = re.search(
            r"\b(?:network error|connection reset|econnreset|connection timed out|request timed out|network timeout|connection refused)\b",
            lowered,
        )
        if network_match:
            hints.append(
                _hint(
                    kind="network_transient",
                    subtype="transient",
                    confidence="medium",
                    hard_stop=False,
                    source=source,
                    evidence_excerpt=_excerpt(text, network_match.start(), network_match.end()),
                    now=observed_at,
                    recovery_guidance="bounded-retry",
                )
            )

        for kind, pattern in (
            ("auth_required", r"\b(?:login required|please log in|sign in|enter api key|enter password|mfa|two-factor)\b"),
            ("trust_prompt", r"\b(?:do you trust the (?:contents|files)|trust this (?:directory|folder|repo))\b"),
            ("permission_prompt", r"\b(?:permission denied|grant permission|requires permission|allow access)\b"),
            # Shared with tmux_adapter.detect_hard_prompt (see constants.py):
            # one source of truth for the external-side-effect stop condition.
            ("external_side_effect_request", EXTERNAL_SIDE_EFFECT_PROMPT_RE),
        ):
            match = re.search(pattern, lowered)
            if match:
                hints.append(
                    _hint(
                        kind=kind,
                        subtype=None,
                        confidence="high",
                        hard_stop=True,
                        source=source,
                        evidence_excerpt=_excerpt(text, match.start(), match.end()),
                        now=observed_at,
                        recovery_guidance="stop-for-user",
                    )
                )

    if result_exists:
        hints.append(
            _hint(
                kind="result_ready",
                subtype=None,
                confidence="high",
                hard_stop=False,
                source="artifact",
                evidence_excerpt="developer-result.json exists",
                now=observed_at,
                recovery_guidance="finalize-slice",
            )
        )
    elif not process_running:
        hints.append(
            _hint(
                kind="process_exited_without_result",
                subtype=None,
                confidence="high",
                hard_stop=True,
                source="process",
                evidence_excerpt="harness process is not running and developer-result.json is absent",
                now=observed_at,
                recovery_guidance="stop-for-user-or-restart-only-from-clean-authorized-state",
            )
        )
    elif not process_active:
        hints.append(
            _hint(
                kind="idle_no_progress",
                subtype=None,
                confidence="low",
                hard_stop=False,
                source="process",
                evidence_excerpt="harness process is running but pane text did not change",
                now=observed_at,
                recovery_guidance="observe-again-before-deciding",
            )
        )
    return hints

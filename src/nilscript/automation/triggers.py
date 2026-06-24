"""Pure trigger evaluation — does an incoming event / a clock tick fire this automation?

Side-effect-free and total, so it is exhaustively testable on its own. The scheduler composes these
with the dispatcher. Cron schedules are deferred to Temporal (the durable cloud sibling); the local
single-instance ticker handles `interval_seconds` only — `schedule_due` returns False for cron.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nilscript.automation.models import EventTrigger, ScheduleTrigger


def event_matches(trigger: EventTrigger, envelope: dict[str, Any]) -> bool:
    """True when a ledger event envelope satisfies an EventTrigger (event kind, verb, field filter)."""
    body = envelope.get("body") or {}
    if body.get("event") != trigger.on_event:
        return False
    if body.get("verb") != trigger.on_verb:
        return False
    args = body.get("args") or {}
    for key, value in trigger.match.items():
        if args.get(key) != value and body.get(key) != value:
            return False
    return True


def _matches_field(spec: str, value: int, lo: int, hi: int) -> bool:
    """Match one cron field against a value. Supports `*`, `*/n`, `a-b`, `a-b/n`, `a,b`, and exact."""
    for part in spec.split(","):
        body, step = (part.split("/", 1) + ["1"])[:2] if "/" in part else (part, "1")
        try:
            step_n = int(step)
            if body == "*":
                start, end = lo, hi
            elif "-" in body:
                a, b = body.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = end = int(body)
        except ValueError:
            continue  # malformed field part — skip it rather than crash a tick
        if start <= value <= end and step_n > 0 and (value - start) % step_n == 0:
            return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """Whether a 5-field cron expression (min hour day-of-month month day-of-week) matches `dt`.

    Supported subset (no `croniter` dependency — swap it in for full cron when the durable runtime
    lands): `*`, `*/n`, ranges, lists, and exact values. Day-of-week is 0–6 (Sun=0); 7 also means Sun.
    """
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7  # Python Mon=0..Sun=6 → cron Sun=0..Sat=6
    dow_ok = _matches_field(dow, dow_val, 0, 7) or (dow_val == 0 and _matches_field(dow, 7, 0, 7))
    return (
        _matches_field(minute, dt.minute, 0, 59)
        and _matches_field(hour, dt.hour, 0, 23)
        and _matches_field(dom, dt.day, 1, 31)
        and _matches_field(month, dt.month, 1, 12)
        and dow_ok
    )


def schedule_due(
    trigger: ScheduleTrigger, last_started_at: str | None, now: datetime
) -> bool:
    """True when a schedule is due as of `now` — for both interval and (local subset) cron."""
    last: datetime | None = None
    if last_started_at:
        try:
            last = datetime.fromisoformat(last_started_at)
        except ValueError:
            last = None  # unparseable prior timestamp — treat as never-run

    if trigger.cron is not None:
        if not cron_matches(trigger.cron, now):
            return False
        if last is None:
            return True
        # fire at most once per matching minute
        return last.replace(second=0, microsecond=0) < now.replace(second=0, microsecond=0)

    if trigger.interval_seconds is None:
        return False
    if last is None:
        return True
    return (now - last).total_seconds() >= trigger.interval_seconds


def event_fire_key(envelope: dict[str, Any]) -> str:
    """Idempotency key for an event-triggered fire — the triggering event's stable id, so a
    re-delivered event replays the same run instead of firing twice."""
    return f"event:{envelope.get('id') or 'noid'}"


def schedule_fire_key(trigger: ScheduleTrigger, now: datetime) -> str:
    """Idempotency key for a scheduled fire. Cron buckets to the minute (it fires once per matching
    minute); interval buckets to the second. `schedule_due`'s last-run gate handles real dueness — the
    key only collapses duplicate ticks within the same bucket into a single replayed run."""
    if trigger.cron is not None:
        return "schedule:" + now.replace(second=0, microsecond=0).isoformat()
    return "schedule:" + now.replace(microsecond=0).isoformat()

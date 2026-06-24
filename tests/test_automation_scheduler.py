"""Triggers + scheduler (P2): event-driven and interval-scheduled fires, with the loop guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from nilscript.automation import (
    dispatch_event,
    draft_automation,
    register,
    run_due_schedules,
)
from nilscript.automation.models import ScheduleTrigger
from nilscript.automation.triggers import cron_matches, event_matches, schedule_due
from nilscript.controlplane.app import create_app
from nilscript.controlplane.store import EventStore
from nilscript.kernel.context import SkillSpec, ValidationContext
from nilscript.kernel.executor import RunResult


def _ctx() -> ValidationContext:
    return ValidationContext(
        skills={"crm": SkillSpec(frozenset({"crm.create_contact"}),
                {"properties": {"name": {"type": "string"}}, "required": ["name"]})},
        read_verbs=frozenset(), workspaces={"acme": frozenset({"crm.create_contact"})},
    )


def _plan(ws: str = "acme") -> dict:
    return {
        "wosool": "0.1", "workspace": ws, "entry": "step_1",
        "pipeline": [{"id": "step_1", "type": "action", "skill": "crm",
                      "verb": "crm.create_contact", "args": {"name": "Ada"}}],
    }


def _arm(store, trigger: dict, *, automation_id: str = "auto-1", ws: str = "acme") -> None:
    res = draft_automation(automation_id=automation_id, name={"ar": "x", "en": "x"},
                           raw_plan=_plan(ws), trigger=trigger, ctx=_ctx())
    d = register(store, res.definition)
    store.set_automation_state(ws, automation_id, d.version, "active", approved_by="owner")


def _store(tmp_path) -> EventStore:
    return EventStore(path=str(tmp_path / "cp.db"))


def _event(verb="crm.create_lead", event="executed", grant="g", ws="acme", eid="ev1", args=None):
    return {"id": eid, "workspace": ws, "grant": grant,
            "body": {"event": event, "verb": verb, "args": args or {}}}


def _counting_runner():
    calls = []

    async def runner(plan, *, run_id):
        calls.append(run_id)
        return RunResult(completed=True)

    return runner, calls


# --- pure trigger logic -----------------------------------------------------------------------


def test_event_matches():
    from nilscript.automation.models import parse_trigger
    t = parse_trigger({"type": "event", "on_verb": "crm.create_lead", "match": {"stage": "new"}})
    assert event_matches(t, _event(args={"stage": "new"}))
    assert not event_matches(t, _event(verb="crm.create_contact", args={"stage": "new"}))
    assert not event_matches(t, _event(event="refused", args={"stage": "new"}))
    assert not event_matches(t, _event(args={"stage": "won"}))


def test_schedule_due_interval_and_cron():
    interval = ScheduleTrigger(type="schedule", interval_seconds=60)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert schedule_due(interval, None, now) is True                      # never run → due
    assert schedule_due(interval, now.isoformat(), now) is False          # just ran → not due
    assert schedule_due(interval, (now - timedelta(seconds=90)).isoformat(), now) is True
    cron = ScheduleTrigger(type="schedule", cron="0 9 * * *")
    assert schedule_due(cron, None, now) is False                         # 12:00 ≠ 09:00 → not due


def test_cron_matches_subset():
    nine_am = datetime(2026, 1, 5, 9, 0, 0, tzinfo=UTC)  # 2026-01-05 is a Monday
    assert cron_matches("0 9 * * *", nine_am) is True
    assert cron_matches("0 9 * * *", nine_am.replace(minute=1)) is False
    assert cron_matches("0 9 * * *", nine_am.replace(hour=10)) is False
    assert cron_matches("*/5 * * * *", nine_am.replace(minute=10)) is True
    assert cron_matches("*/5 * * * *", nine_am.replace(minute=12)) is False
    assert cron_matches("0 0 * * 1", nine_am.replace(hour=0, minute=0)) is True   # Monday=1
    assert cron_matches("0 0 * * 2", nine_am.replace(hour=0, minute=0)) is False  # not Tuesday


def test_schedule_due_cron_fires_once_per_minute():
    at_nine = datetime(2026, 1, 5, 9, 0, 30, tzinfo=UTC)
    cron = ScheduleTrigger(type="schedule", cron="0 9 * * *")
    assert schedule_due(cron, None, at_nine) is True                    # matching minute, never run
    assert schedule_due(cron, at_nine.isoformat(), at_nine) is False    # already ran this minute
    earlier = at_nine.replace(hour=8).isoformat()
    assert schedule_due(cron, earlier, at_nine) is True                 # last run an earlier minute
    off_minute = at_nine.replace(minute=1)
    assert schedule_due(cron, None, off_minute) is False                # 09:01 ≠ cron


# --- event dispatch ---------------------------------------------------------------------------


async def test_dispatch_fires_matching_active_automation(tmp_path):
    store = _store(tmp_path)
    runner, calls = _counting_runner()
    _arm(store, {"type": "event", "on_verb": "crm.create_lead"})
    fired = await dispatch_event(store, _event(), runner=runner)
    assert len(fired) == 1 and fired[0]["ok"]
    assert len(calls) == 1


async def test_dispatch_skips_non_matching(tmp_path):
    store = _store(tmp_path)
    runner, calls = _counting_runner()
    _arm(store, {"type": "event", "on_verb": "crm.create_lead"})
    await dispatch_event(store, _event(verb="crm.delete_lead"), runner=runner)
    assert calls == []


async def test_dispatch_loop_guard_skips_control_plane_events(tmp_path):
    store = _store(tmp_path)
    runner, calls = _counting_runner()
    _arm(store, {"type": "event", "on_verb": "crm.create_lead"})
    await dispatch_event(store, _event(grant="control-plane"), runner=runner)
    assert calls == []  # a triggered run's own events never re-trigger


async def test_dispatch_ignores_non_event_triggers(tmp_path):
    store = _store(tmp_path)
    runner, calls = _counting_runner()
    _arm(store, {"type": "manual"})
    await dispatch_event(store, _event(), runner=runner)
    assert calls == []


async def test_dispatch_is_idempotent_on_event_id(tmp_path):
    store = _store(tmp_path)
    runner, calls = _counting_runner()
    _arm(store, {"type": "event", "on_verb": "crm.create_lead"})
    await dispatch_event(store, _event(eid="ev-42"), runner=runner)
    again = await dispatch_event(store, _event(eid="ev-42"), runner=runner)
    assert again[0].get("replayed") is True
    assert len(calls) == 1  # same event id → one execution


# --- scheduled dispatch -----------------------------------------------------------------------


async def test_run_due_schedules_fires_then_not_due(tmp_path):
    store = _store(tmp_path)
    runner, calls = _counting_runner()
    _arm(store, {"type": "schedule", "interval_seconds": 1})
    fired = await run_due_schedules(store, runner=runner, now=datetime.now(UTC))
    assert len(fired) == 1 and len(calls) == 1
    # immediately again: the just-created run makes it not yet due
    again = await run_due_schedules(store, runner=runner, now=datetime.now(UTC))
    assert again == [] and len(calls) == 1


async def test_run_due_schedules_fires_again_after_interval(tmp_path):
    store = _store(tmp_path)
    runner, calls = _counting_runner()
    _arm(store, {"type": "schedule", "interval_seconds": 1})
    await run_due_schedules(store, runner=runner, now=datetime.now(UTC))
    later = datetime.now(UTC) + timedelta(seconds=5)
    await run_due_schedules(store, runner=runner, now=later)
    assert len(calls) == 2


# --- tick endpoint ----------------------------------------------------------------------------


def test_tick_endpoint_fires_due_schedules(tmp_path):
    store = _store(tmp_path)
    runner, _ = _counting_runner()

    async def provider(workspace: str):
        return {"reachable": True, "conformant": True, "verbs": ["crm.create_contact"], "targets": {}}

    _arm(store, {"type": "schedule", "interval_seconds": 1})
    c = TestClient(create_app(store, skeleton_provider=provider, runner=runner))
    r = c.post("/automations/tick")
    assert r.status_code == 200
    assert r.json()["fired"] == 1


def test_tick_endpoint_fires_cron_due_now(tmp_path):
    store = _store(tmp_path)
    runner, calls = _counting_runner()

    async def provider(workspace: str):
        return {"reachable": True, "conformant": True, "verbs": ["crm.create_contact"], "targets": {}}

    _arm(store, {"type": "schedule", "cron": "* * * * *"})  # every minute → always due now
    c = TestClient(create_app(store, skeleton_provider=provider, runner=runner))
    assert c.post("/automations/tick").json()["fired"] == 1
    # a second tick in the same minute replays (idempotent) — no second execution
    c.post("/automations/tick")
    assert len(calls) == 1

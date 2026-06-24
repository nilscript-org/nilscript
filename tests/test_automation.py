"""Automation Registry (P1): draft gate, content-hash version lock, append-only SSOT, lifecycle."""

from __future__ import annotations

import pytest

from nilscript.automation import (
    AutomationDefinition,
    DraftResult,
    content_hash,
    draft_automation,
    parse_trigger,
    register,
)
from nilscript.automation.models import EventTrigger, ManualTrigger, ScheduleTrigger
from nilscript.controlplane.store import EventStore
from nilscript.kernel.context import SkillSpec, ValidationContext
from nilscript.kernel.models import WosoolProgram


# --- fixtures ---------------------------------------------------------------------------------


def _ctx() -> ValidationContext:
    """A workspace `acme` that may create contacts via the `crm` skill — nothing else."""
    return ValidationContext(
        skills={
            "crm": SkillSpec(
                required_verbs=frozenset({"crm.create_contact"}),
                hint_schema={
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": True,
                },
            )
        },
        read_verbs=frozenset(),
        workspaces={"acme": frozenset({"crm.create_contact"})},
    )


def _valid_plan(name: str = "Ada") -> dict:
    return {
        "wosool": "0.1",
        "workspace": "acme",
        "entry": "step_1",
        "pipeline": [
            {
                "id": "step_1",
                "type": "action",
                "skill": "crm",
                "verb": "crm.create_contact",
                "args": {"name": name},
            }
        ],
    }


def _name() -> dict:
    return {"ar": "متابعة العملاء", "en": "Follow up leads"}


@pytest.fixture
def store(tmp_path) -> EventStore:
    return EventStore(path=str(tmp_path / "cp.db"))


# --- content hash (the version lock) ----------------------------------------------------------


def test_content_hash_is_deterministic_and_sensitive():
    a = WosoolProgram.model_validate(_valid_plan())
    a2 = WosoolProgram.model_validate(_valid_plan())
    b = WosoolProgram.model_validate(_valid_plan(name="Grace"))
    assert content_hash(a) == content_hash(a2)  # identical bytes → identical hash
    assert content_hash(a) != content_hash(b)  # a changed arg → different lock
    assert len(content_hash(a)) == 64


# --- the draft gate (deterministic lower-or-reject) -------------------------------------------


def test_draft_admits_a_valid_plan():
    res = draft_automation(
        automation_id="follow-up",
        name=_name(),
        raw_plan=_valid_plan(),
        trigger={"type": "manual"},
        ctx=_ctx(),
        authored_by="agent-1",
    )
    assert isinstance(res, DraftResult)
    assert res.ok
    assert res.definition is not None
    assert res.definition.state == "draft"
    assert res.definition.workspace == "acme"
    assert res.definition.content_hash == res.content_hash
    assert isinstance(res.definition.trigger, ManualTrigger)


def test_draft_rejects_a_hallucinated_verb():
    plan = _valid_plan()
    plan["pipeline"][0]["verb"] = "crm.fly_to_moon"  # not in the skeleton
    res = draft_automation(
        automation_id="bad",
        name=_name(),
        raw_plan=plan,
        trigger={"type": "manual"},
        ctx=_ctx(),
    )
    assert not res.ok
    assert res.definition is None
    codes = {d.code for d in res.diagnostics.diagnostics}
    assert "V4_UNKNOWN_SKILL" in codes


def test_draft_rejects_a_forward_reference():
    # step_1 consumes an output of step_2, which runs after it — a forward reference (V6).
    plan = {
        "wosool": "0.1",
        "workspace": "acme",
        "entry": "step_1",
        "pipeline": [
            {
                "id": "step_1", "type": "action", "skill": "crm",
                "verb": "crm.create_contact", "args": {"name": "$.step_2.id"}, "next": "step_2",
            },
            {
                "id": "step_2", "type": "action", "skill": "crm",
                "verb": "crm.create_contact", "args": {"name": "Grace"},
            },
        ],
    }
    res = draft_automation(
        automation_id="bad-ref",
        name=_name(),
        raw_plan=plan,
        trigger={"type": "manual"},
        ctx=_ctx(),
    )
    assert not res.ok
    assert "V6_FORWARD_REF" in {d.code for d in res.diagnostics.diagnostics}


# --- trigger union ----------------------------------------------------------------------------


def test_parse_trigger_closed_union():
    assert isinstance(parse_trigger({"type": "manual"}), ManualTrigger)
    assert isinstance(parse_trigger({"type": "schedule", "cron": "0 9 * * *"}), ScheduleTrigger)
    assert isinstance(parse_trigger({"type": "event", "on_verb": "crm.create_lead"}), EventTrigger)
    with pytest.raises(Exception):
        parse_trigger({"type": "telepathy"})


def test_schedule_trigger_needs_exactly_one_timing():
    with pytest.raises(Exception):
        parse_trigger({"type": "schedule"})  # neither cron nor interval
    with pytest.raises(Exception):
        parse_trigger({"type": "schedule", "cron": "0 9 * * *", "interval_seconds": 60})  # both


# --- registration → SSOT (append-only versions) ----------------------------------------------


def test_register_persists_and_reads_back(store):
    res = draft_automation(
        automation_id="follow-up",
        name=_name(),
        raw_plan=_valid_plan(),
        trigger={"type": "manual"},
        ctx=_ctx(),
        authored_by="agent-1",
    )
    stored = register(store, res.definition)
    assert stored.version == 1
    assert stored.state == "pending_approval"  # registered, not auto-armed
    assert stored.created_at  # store stamped it

    fetched = store.get_automation("acme", "follow-up")
    rebuilt = AutomationDefinition.from_row(fetched)
    assert rebuilt.content_hash == res.content_hash
    assert rebuilt.plan.workspace == "acme"


def test_re_registering_identical_plan_is_idempotent(store):
    res = draft_automation(
        automation_id="follow-up", name=_name(), raw_plan=_valid_plan(),
        trigger={"type": "manual"}, ctx=_ctx(),
    )
    first = register(store, res.definition)
    second = register(store, res.definition)  # same hash → no new version
    assert first.version == second.version == 1
    assert len(store.list_automations("acme")) == 1


def test_editing_creates_a_new_version_and_archives_the_old(store):
    v1 = draft_automation(
        automation_id="follow-up", name=_name(), raw_plan=_valid_plan(),
        trigger={"type": "manual"}, ctx=_ctx(),
    )
    register(store, v1.definition)
    v2 = draft_automation(
        automation_id="follow-up", name=_name(), raw_plan=_valid_plan(name="Grace"),
        trigger={"type": "manual"}, ctx=_ctx(),
    )
    stored2 = register(store, v2.definition)
    assert stored2.version == 2

    old = store.get_automation("acme", "follow-up", version=1)
    assert old["state"] == "archived"
    assert old["superseded_by"] == 2
    # list returns only the latest version per automation_id
    listed = store.list_automations("acme")
    assert len(listed) == 1
    assert listed[0]["version"] == 2


# --- lifecycle --------------------------------------------------------------------------------


def test_lifecycle_approve_then_pause(store):
    res = draft_automation(
        automation_id="follow-up", name=_name(), raw_plan=_valid_plan(),
        trigger={"type": "manual"}, ctx=_ctx(),
    )
    register(store, res.definition)
    assert store.set_automation_state("acme", "follow-up", 1, "active", approved_by="owner@acme")
    after = store.get_automation("acme", "follow-up")
    assert after["state"] == "active"
    assert after["approved_by"] == "owner@acme"

    assert store.set_automation_state("acme", "follow-up", 1, "paused")
    assert store.get_automation("acme", "follow-up")["state"] == "paused"


def test_set_state_unknown_version_returns_false(store):
    assert store.set_automation_state("acme", "nope", 1, "active") is False


def test_get_unknown_automation_returns_none(store):
    assert store.get_automation("acme", "ghost") is None
    assert store.list_automations("acme") == []

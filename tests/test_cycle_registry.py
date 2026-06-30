"""Phase 2 of the Cycle-AST-as-SSOT migration — the GOVERNED registration spine
(docs/PLAN-cycle-ast-ssot.md §2). A drawn cycle is persisted the same way an automation is: the
kernel compiles it (lower → V1–V6 → AST content-hash) and appends it to the `automations` SSOT with
`kind='cycle'` and the canonical Cycle AST in a `source` column (the lowered `WosoolProgram` stays
in `plan`, derived). Runs reuse the existing `fire_manual` path unchanged — there is no second
executor. This is what closes the drift: the visual surface registers THROUGH the kernel.
"""

from __future__ import annotations

import pytest

from nilscript.controlplane.store import EventStore
from nilscript.cycle import cycle_content_hash
from nilscript.cycle.authoring import draft_cycle, register_cycle
from nilscript.kernel.context import SkillSpec, ValidationContext
from nilscript.kernel.models import WosoolProgram


def _ctx() -> ValidationContext:
    return ValidationContext(
        skills={
            "crm": SkillSpec(
                required_verbs=frozenset(
                    {"crm.create_lead", "crm.log_note", "crm.create_opportunity"}
                ),
                hint_schema={"additionalProperties": True},
            )
        },
        read_verbs=frozenset(),
        workspaces={
            "acme": frozenset({"crm.create_lead", "crm.log_note", "crm.create_opportunity"})
        },
    )


def _cycle(*, opportunity_verb: str = "crm.create_opportunity") -> dict:
    return {
        "nil": "cycle/0.2",
        "cycle_id": "SalesLeadLifecycle",
        "workspace": "acme",
        "metadata": {"version": "1.0", "owner": "Sales", "tags": ["sales"]},
        "intent": {"ar": "دورة حياة العميل المحتمل", "en": "Lead lifecycle"},
        "trigger": {"type": "manual"},
        "flow": {
            "entry": "CreateLead",
            "steps": [
                {"id": "CreateLead", "type": "action", "use": "crm.create_lead",
                 "with": {"name": "Acme"}, "output": "lead", "next": "LogNote"},
                {"id": "LogNote", "type": "action", "use": "crm.log_note",
                 "with": {"note": "lead created"}, "next": "Qualify"},
                {"id": "Qualify", "type": "decision", "when": "true",
                 "on_true": "CreateOpportunity", "on_false": "Done"},
                {"id": "CreateOpportunity", "type": "action", "use": opportunity_verb,
                 "with": {"lead_id": "lead.id"}, "next": "Done"},
                {"id": "Done", "type": "notify", "message": {"ar": "تم", "en": "Done"}},
            ],
        },
    }


@pytest.fixture
def store(tmp_path) -> EventStore:
    return EventStore(path=str(tmp_path / "cp.db"))


# --- draft (preview, no side effect) ----------------------------------------------------------


def test_draft_cycle_admits_and_carries_program_and_hash():
    res = draft_cycle(raw_cycle=_cycle(), ctx=_ctx())
    assert res.ok, [d.code for d in res.diagnostics.diagnostics]
    assert isinstance(res.program, WosoolProgram)
    assert res.content_hash == cycle_content_hash(res.cycle)


def test_draft_cycle_refuses_undeclared_verb():
    res = draft_cycle(raw_cycle=_cycle(opportunity_verb="crm.delete_everything"), ctx=_ctx())
    assert not res.ok
    assert res.cycle is None or res.program is None
    codes = {d.code for d in res.diagnostics.diagnostics}
    assert "V4_UNKNOWN_SKILL" in codes


# --- register (persist to the SSOT, kind='cycle') --------------------------------------------


def test_register_cycle_persists_with_kind_and_source(store):
    res = draft_cycle(raw_cycle=_cycle(), ctx=_ctx())
    row = register_cycle(store, res, authored_by="agent-1")
    assert row["kind"] == "cycle"
    assert row["workspace"] == "acme"
    assert row["content_hash"] == res.content_hash  # the AST lock, not the IR's
    assert row["state"] == "pending_approval"  # never auto-armed
    # the canonical Cycle AST is preserved verbatim in `source`...
    assert row["source"]["cycle_id"] == "SalesLeadLifecycle"
    assert row["source"]["nil"] == "cycle/0.2"
    # ...and the derived lowered program lives in `plan` and re-validates as a real program.
    WosoolProgram.model_validate(row["plan"])


def test_register_cycle_round_trips_from_store(store):
    res = draft_cycle(raw_cycle=_cycle(), ctx=_ctx())
    register_cycle(store, res, authored_by="agent-1")
    fetched = store.get_automation("acme", "salesleadlifecycle")
    assert fetched is not None
    assert fetched["kind"] == "cycle"
    assert fetched["source"]["cycle_id"] == "SalesLeadLifecycle"


def test_register_cycle_is_idempotent(store):
    res = draft_cycle(raw_cycle=_cycle(), ctx=_ctx())
    first = register_cycle(store, res, authored_by="agent-1")
    again = register_cycle(store, res, authored_by="agent-1")
    assert first["version"] == again["version"] == 1  # same hash ⇒ no new version


def test_editing_the_cycle_supersedes_the_version(store):
    register_cycle(store, draft_cycle(raw_cycle=_cycle(), ctx=_ctx()), authored_by="a")
    edited = _cycle()
    edited["intent"] = {"ar": "مختلف", "en": "Different"}
    second = register_cycle(store, draft_cycle(raw_cycle=edited, ctx=_ctx()), authored_by="a")
    assert second["version"] == 2  # a new authored version, the prior archived


def test_automation_source_column_is_null_for_plain_automations(store):
    """A `kind='single'` automation has no Cycle source — the new column must be optional."""
    row = store.register_automation(
        workspace="acme",
        automation_id="plain",
        content_hash="0" * 64,
        name={"ar": "ع", "en": "plain"},
        plan=_cycle()["flow"] and {  # minimal valid program
            "wosool": "0.1", "workspace": "acme", "entry": "step_1",
            "pipeline": [{"id": "step_1", "type": "notify", "message": {"ar": "x"}}],
        },
        trigger={"type": "manual"},
    )
    assert row["kind"] == "single"
    assert row.get("source") is None


# --- HTTP surface (the control-plane endpoints the canvas calls) ------------------------------


def _http_client(tmp_path):
    from fastapi.testclient import TestClient

    from nilscript.controlplane.app import create_app

    store = EventStore(path=str(tmp_path / "cp.db"))

    async def provider(workspace: str):
        return {
            "reachable": True,
            "conformant": True,
            "verbs": ["crm.create_lead", "crm.log_note", "crm.create_opportunity"],
            "targets": {},
        }

    return TestClient(create_app(store, skeleton_provider=provider))


def test_http_cycle_draft_admits(tmp_path):
    r = _http_client(tmp_path).post("/cycles/draft", json={"cycle": _cycle()})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert len(out["content_hash"]) == 64


def test_http_cycle_draft_refuses_undeclared_verb(tmp_path):
    r = _http_client(tmp_path).post(
        "/cycles/draft", json={"cycle": _cycle(opportunity_verb="crm.delete_everything")}
    )
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is False
    assert any(d["code"] == "V4_UNKNOWN_SKILL" for d in out["refusal"])


def test_http_cycle_register_then_list(tmp_path):
    c = _http_client(tmp_path)
    r = c.post("/cycles/register", json={"cycle": _cycle(), "authored_by": "agent-1"})
    assert r.status_code == 200, r.text
    assert r.json()["definition"]["kind"] == "cycle"
    listed = c.get("/cycles", params={"workspace": "acme"}).json()["cycles"]
    assert len(listed) == 1
    assert listed[0]["source"]["cycle_id"] == "SalesLeadLifecycle"


def test_http_cycle_register_refuses_bad_cycle(tmp_path):
    r = _http_client(tmp_path).post(
        "/cycles/register", json={"cycle": _cycle(opportunity_verb="crm.delete_everything")}
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False

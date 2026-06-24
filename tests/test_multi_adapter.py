"""Multiple adapters active at once (PocketBase + Odoo) and authoring a cross-system automation
through the MCP compose tool."""

from __future__ import annotations

import httpx

from nilscript.controlplane.app import create_app
from nilscript.controlplane.store import EventStore
from nilscript.mcp.automation_tools import AutomationTools


def _store(tmp_path, *, two=True) -> EventStore:
    s = EventStore(path=str(tmp_path / "cp.db"))
    s.register_adapter("acme", "pocket", url="http://pocket", system="pocketbase")
    if two:
        s.register_adapter("acme", "odoo", url="http://odoo", system="odoo")
    return s


def _stage(name: str, adapter: str, verb: str, skill: str, args: dict, input_from: dict | None = None) -> dict:
    s = {"name": name, "adapter": adapter,
         "plan": {"wosool": "0.1", "workspace": "acme", "entry": "step_1",
                  "pipeline": [{"id": "step_1", "type": "action", "skill": skill, "verb": verb, "args": args}]}}
    if input_from:
        s["input_from"] = input_from
    return s


# --- multiple adapters active simultaneously --------------------------------------------------


def test_two_adapters_active_at_once(tmp_path):
    s = _store(tmp_path)
    assert s.set_adapter_active("acme", "pocket", True)
    assert s.set_adapter_active("acme", "odoo", True)
    active = [a for a in s.list_adapters("acme") if a["active"]]
    assert {a["adapter_id"] for a in active} == {"pocket", "odoo"}  # BOTH on
    assert s.active_adapter("acme") is not None  # singular default still resolves


def test_disable_one_leaves_the_other(tmp_path):
    s = _store(tmp_path)
    s.set_adapter_active("acme", "pocket", True)
    s.set_adapter_active("acme", "odoo", True)
    s.set_adapter_active("acme", "pocket", False)
    active = [a for a in s.list_adapters("acme") if a["active"]]
    assert [a["adapter_id"] for a in active] == ["odoo"]


def test_set_active_unknown_adapter(tmp_path):
    assert _store(tmp_path).set_adapter_active("acme", "ghost", True) is False


def test_enable_disable_endpoints_are_token_gated(tmp_path):
    c = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
        create_app(_store(tmp_path), registry_token="t")
    )
    assert c.post("/adapters/acme/pocket/enable").status_code == 401  # no token
    ok = c.post("/adapters/acme/pocket/enable", headers={"Authorization": "Bearer t"})
    assert ok.status_code == 200 and ok.json()["active"] is True
    off = c.post("/adapters/acme/pocket/disable", headers={"Authorization": "Bearer t"})
    assert off.json()["active"] is False
    assert c.post("/adapters/acme/ghost/enable", headers={"Authorization": "Bearer t"}).status_code == 404


# --- author a cross-system automation through the MCP compose tool -----------------------------


async def test_mcp_compose_tool_registers_two_system_automation(tmp_path):
    store = _store(tmp_path)
    skeletons = {
        "pocket": {"reachable": True, "conformant": True, "verbs": ["crm.create_lead"], "targets": {}},
        "odoo": {"reachable": True, "conformant": True, "verbs": ["acc.create_invoice"], "targets": {}},
    }

    async def adapter_skeletons(workspace: str, adapter_id: str):
        return skeletons.get(adapter_id)

    app = create_app(store, adapter_skeleton_provider=adapter_skeletons)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")
    tools = AutomationTools("http://cp", "", client=client)

    composed = {"workspace": "acme", "stages": [
        _stage("stage_1", "pocket", "crm.create_lead", "crm", {"name": "Acme"}),
        _stage("stage_2", "odoo", "acc.create_invoice", "acc", {"ref": "$.input.lead"},
               input_from={"lead": "$.stage_1.step_1.output.state"}),
    ]}
    out = await tools.compose_register("lead-to-invoice", {"ar": "x", "en": "Lead→Invoice"},
                                       composed, {"type": "manual"})
    assert out["ok"] is True
    assert out["definition"]["kind"] == "composed"
    # both backends are referenced in the one registered automation
    listed = (await tools.list("acme"))["automations"]
    assert len(listed) == 1 and listed[0]["kind"] == "composed"

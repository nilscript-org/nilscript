"""The control-plane dashboard surfaces automations: the /api/automations feed + the rendered panel."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nilscript.controlplane.app import create_app
from nilscript.controlplane.store import EventStore


def _plan() -> dict:
    return {
        "wosool": "0.1", "workspace": "acme", "entry": "step_1",
        "pipeline": [{"id": "step_1", "type": "action", "skill": "crm",
                      "verb": "crm.create_contact", "args": {"name": "Ada"}}],
    }


def _client(tmp_path):
    store = EventStore(path=str(tmp_path / "cp.db"))

    async def provider(workspace: str):
        return {"reachable": True, "conformant": True, "verbs": ["crm.create_contact"], "targets": {}}

    return TestClient(create_app(store, skeleton_provider=provider))


def test_api_automations_feed(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/automations").json()["automations"] == []  # empty before any registration

    c.post("/automations/register", json={
        "automation_id": "follow-up", "name": {"ar": "x", "en": "Follow up"},
        "plan": _plan(), "trigger": {"type": "schedule", "cron": "0 9 * * *"},
    })
    feed = c.get("/api/automations").json()["automations"]
    assert len(feed) == 1
    a = feed[0]
    assert a["state"] == "pending_approval"
    assert a["kind"] == "single"
    assert a["trigger"]["cron"] == "0 9 * * *"
    assert a["plan_summary"] == {"nodes": 1}
    assert "plan" not in a  # heavy plan summarised, not shipped


def test_dashboard_html_includes_automations_panel(tmp_path):
    html = _client(tmp_path).get("/").text
    assert "id=automations" in html
    assert "Automations" in html
    assert "loadAutomations" in html
    assert "operator token" in html  # the token-gated control affordance


def test_adapter_skeleton_endpoint_feeds_the_form(tmp_path):
    store = EventStore(path=str(tmp_path / "cp.db"))

    async def adapter_skeletons(workspace: str, adapter_id: str):
        return {"reachable": True, "conformant": True,
                "verbs": ["crm.create_lead", "crm.create_contact"], "targets": {"crm.lead": {}}}

    c = TestClient(create_app(store, adapter_skeleton_provider=adapter_skeletons, registry_token="t"))
    assert c.get("/api/adapter-skeleton", params={"workspace": "acme", "adapter_id": "odoo"}).status_code == 401
    ok = c.get("/api/adapter-skeleton", params={"workspace": "acme", "adapter_id": "odoo"},
               headers={"Authorization": "Bearer t"})
    assert ok.status_code == 200
    assert ok.json()["verbs"] == ["crm.create_lead", "crm.create_contact"]
    assert ok.json()["targets"] == ["crm.lead"]


def test_compose_form_present_in_html(tmp_path):
    html = _client(tmp_path).get("/").text
    assert "id=composeForm" in html
    assert "submitCompose" in html and "loadVerbs" in html
    assert "New cross-system automation" in html


def test_api_automations_lists_across_workspaces(tmp_path):
    c = _client(tmp_path)
    c.post("/automations/register", json={
        "automation_id": "a1", "name": {"ar": "x", "en": "x"},
        "plan": _plan(), "trigger": {"type": "manual"},
    })
    feed = c.get("/api/automations").json()["automations"]
    assert [a["automation_id"] for a in feed] == ["a1"]

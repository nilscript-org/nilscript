"""Control-plane HTTP surface for the Automation Registry: draft → register → lifecycle.

The skeleton provider is faked so the draft gate runs against a known verb surface with no live
adapter — the same `context_from_skeleton` path the production `_live_skeleton` feeds.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nilscript.controlplane.app import create_app
from nilscript.controlplane.store import EventStore

# The "live" backend surface the draft gate validates against.
_SKELETON = {"reachable": True, "conformant": True, "verbs": ["crm.create_contact"], "targets": {}}


def _plan(name: str = "Ada") -> dict:
    return {
        "wosool": "0.1",
        "workspace": "acme",
        "entry": "step_1",
        "pipeline": [
            {
                "id": "step_1", "type": "action", "skill": "crm",
                "verb": "crm.create_contact", "args": {"name": name},
            }
        ],
    }


def _body(plan: dict | None = None) -> dict:
    return {
        "automation_id": "follow-up",
        "name": {"ar": "متابعة", "en": "Follow up"},
        "plan": plan if plan is not None else _plan(),
        "trigger": {"type": "manual"},
        "authored_by": "agent-1",
    }


def _client(tmp_path, *, skeleton=_SKELETON, registry_token=None) -> TestClient:
    store = EventStore(path=str(tmp_path / "cp.db"))

    async def provider(workspace: str):
        return skeleton

    return TestClient(create_app(store, skeleton_provider=provider, registry_token=registry_token))


# --- draft (preview, no side effect) ----------------------------------------------------------


def test_draft_admits_and_returns_hash(tmp_path):
    r = _client(tmp_path).post("/automations/draft", json=_body())
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert len(out["content_hash"]) == 64
    assert out["definition"]["state"] == "draft"


def test_draft_refuses_hallucinated_verb(tmp_path):
    plan = _plan()
    plan["pipeline"][0]["verb"] = "crm.fly_to_moon"
    r = _client(tmp_path).post("/automations/draft", json=_body(plan))
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is False
    assert any(d["code"] == "V4_UNKNOWN_SKILL" for d in out["refusal"])


def test_draft_503_when_no_active_adapter(tmp_path):
    r = _client(tmp_path, skeleton=None).post("/automations/draft", json=_body())
    assert r.status_code == 503


def test_draft_400_on_missing_fields(tmp_path):
    r = _client(tmp_path).post("/automations/draft", json={"automation_id": "x"})
    assert r.status_code == 400


# --- register (persist to SSOT) ---------------------------------------------------------------


def test_register_persists_then_get_and_list(tmp_path):
    c = _client(tmp_path)
    r = c.post("/automations/register", json=_body())
    assert r.status_code == 200
    d = r.json()["definition"]
    assert d["state"] == "pending_approval" and d["version"] == 1

    got = c.get("/automations/acme/follow-up")
    assert got.status_code == 200
    assert got.json()["content_hash"] == d["content_hash"]

    listed = c.get("/automations", params={"workspace": "acme"}).json()["automations"]
    assert len(listed) == 1 and listed[0]["automation_id"] == "follow-up"


def test_register_refuses_invalid_plan_with_400(tmp_path):
    plan = _plan()
    plan["pipeline"][0]["verb"] = "crm.fly_to_moon"
    r = _client(tmp_path).post("/automations/register", json=_body(plan))
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_register_identical_plan_is_idempotent(tmp_path):
    c = _client(tmp_path)
    c.post("/automations/register", json=_body())
    c.post("/automations/register", json=_body())
    assert len(c.get("/automations", params={"workspace": "acme"}).json()["automations"]) == 1


# --- lifecycle --------------------------------------------------------------------------------


def test_approve_then_pause(tmp_path):
    c = _client(tmp_path)
    c.post("/automations/register", json=_body())
    r = c.post(
        "/automations/acme/follow-up/1/state",
        json={"state": "active", "approved_by": "owner@acme"},
    )
    assert r.status_code == 200
    assert r.json()["automation"]["state"] == "active"
    assert r.json()["automation"]["approved_by"] == "owner@acme"

    c.post("/automations/acme/follow-up/1/state", json={"state": "paused"})
    assert c.get("/automations/acme/follow-up").json()["state"] == "paused"


def test_state_unknown_version_404(tmp_path):
    r = _client(tmp_path).post("/automations/acme/ghost/1/state", json={"state": "active"})
    assert r.status_code == 404


def test_get_unknown_404(tmp_path):
    assert _client(tmp_path).get("/automations/acme/ghost").status_code == 404


# --- auth -------------------------------------------------------------------------------------


def test_write_endpoints_require_token_when_configured(tmp_path):
    c = _client(tmp_path, registry_token="s3cret")
    assert c.post("/automations/draft", json=_body()).status_code == 401
    assert c.post("/automations/register", json=_body()).status_code == 401
    ok = c.post(
        "/automations/register", json=_body(),
        headers={"Authorization": "Bearer s3cret"},
    )
    assert ok.status_code == 200
    # reads stay public
    assert c.get("/automations", params={"workspace": "acme"}).status_code == 200


@pytest.mark.parametrize("verb", ["crm.create_contact"])
def test_smoke_known_verb_admits(tmp_path, verb):
    assert _client(tmp_path).post("/automations/draft", json=_body()).json()["ok"] is True

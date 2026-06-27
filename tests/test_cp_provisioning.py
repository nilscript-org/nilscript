"""Control-plane tenant onboarding: one-call provision (encrypted secrets + adapter) + secret read."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from nilscript.secrets import SecretVault

TOKEN = "reg-tok"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NIL_VAULT_KEY", SecretVault.generate_key())
    from nilscript.controlplane.app import create_app
    from nilscript.controlplane.store import EventStore

    store = EventStore(str(tmp_path / "cp.db"))
    app = create_app(store, registry_token=TOKEN)
    return TestClient(app, raise_server_exceptions=False), store


_AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _provision(c, ws, **body):
    return c.post("/tenants/provision", json={"workspace": ws, **body}, headers=_AUTH)


def test_one_call_provision_stores_secrets_and_activates_adapter(client) -> None:
    c, store = client
    r = _provision(
        c, "ws_acme",
        secrets={"adapter_bearer": "sek", "llm_api_key": "sk-acme"},
        adapter={"adapter_id": "odoo", "url": "https://acme.odoo", "system": "odoo_crm"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and "llm_api_key" in body["provisioned"]["secrets"]
    assert "odoo" in body["provisioned"]["adapter"]
    active = store.active_adapter("ws_acme")
    assert active and active["adapter_id"] == "odoo"


def test_secret_read_is_token_gated_and_returns_value(client) -> None:
    c, _ = client
    _provision(c, "ws_acme", secrets={"llm_api_key": "sk-acme"})
    assert c.get("/tenants/ws_acme/secret/llm_api_key").status_code == 401  # no token
    r = c.get("/tenants/ws_acme/secret/llm_api_key", headers=_AUTH)
    assert r.status_code == 200 and r.json()["value"] == "sk-acme"


def test_secrets_are_encrypted_at_rest(client) -> None:
    c, store = client
    _provision(c, "ws_acme", secrets={"llm_api_key": "sk-PLAINTEXT-LEAK"})
    row = store._conn.execute(
        "SELECT ciphertext FROM tenant_secrets WHERE workspace='ws_acme'"
    ).fetchone()
    assert b"sk-PLAINTEXT-LEAK" not in row["ciphertext"]  # ciphertext on disk, not the key


def test_tenants_are_isolated(client) -> None:
    c, store = client
    _provision(c, "ws_a", secrets={"llm_api_key": "key-A"})
    _provision(c, "ws_b", secrets={"llm_api_key": "key-B"})
    assert store.get_secret("ws_a", "llm_api_key") == "key-A"
    assert store.get_secret("ws_b", "llm_api_key") == "key-B"
    assert store.get_secrets("ws_ghost") is None


def test_provision_requires_auth_and_workspace(client) -> None:
    c, _ = client
    assert c.post("/tenants/provision", json={"workspace": "x"}).status_code == 401
    assert c.post("/tenants/provision", json={}, headers=_AUTH).status_code == 400


# ── surface-scoping: tenant A can never see tenant B's events / pending ───────────────────────────
def _event(ws, seq, proposal, event="executed"):
    return {"nil": "0.1", "id": f"{ws}-{seq}", "workspace": ws, "performative": "EVENT",
            "body": {"event": event, "proposal": proposal, "verb": "crm.delete_contact", "tier": "HIGH"}}


def test_events_are_workspace_scoped(client) -> None:
    c, store = client
    store.ingest(_event("ws_a", 1, "pA"), 1)
    store.ingest(_event("ws_b", 1, "pB"), 1)
    a = c.get("/api/events", params={"workspace": "ws_a"}).json()["events"]
    assert a and all(e["workspace"] == "ws_a" for e in a)        # only A's
    assert not any(e["workspace"] == "ws_b" for e in a)          # never B's
    glob = c.get("/api/events").json()["events"]                  # operator view sees both
    assert {e["workspace"] for e in glob} >= {"ws_a", "ws_b"}


def test_pending_is_workspace_scoped(client) -> None:
    c, store = client
    # a held proposal for each tenant, linked by its proposed event's workspace
    store.ingest({**_event("ws_a", 2, "pA2", event="proposed")}, 2)
    store.ingest({**_event("ws_b", 2, "pB2", event="proposed")}, 2)
    store.await_approval("pA2", verb="crm.delete_contact", tier="HIGH", preview="del A")
    store.await_approval("pB2", verb="crm.delete_contact", tier="HIGH", preview="del B")
    a = c.get("/api/pending", params={"workspace": "ws_a"}).json()["pending"]
    ids = {p["proposal_id"] for p in a}
    assert "pA2" in ids and "pB2" not in ids                      # A sees only its own held proposal

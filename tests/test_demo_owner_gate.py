"""Playground owner session gates active-adapter registration (dashboard-driven MCP routing)."""

import pytest

pytest.importorskip("litellm", reason="demo UI needs litellm")
pytest.importorskip("fastapi", reason="needs fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import nilscript.demo.demo_ui as ui  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(ui.REGISTRY, "owner_token", "ownsecret")
    monkeypatch.setitem(ui.REGISTRY, "cp_url", "https://cp.test")
    monkeypatch.setitem(ui.REGISTRY, "token", "regtok")
    monkeypatch.setitem(ui.REGISTRY, "workspace", "owner")
    return TestClient(ui.app)


def test_owner_disabled_when_no_token(monkeypatch):
    monkeypatch.setitem(ui.REGISTRY, "owner_token", "")
    c = TestClient(ui.app)
    assert c.get("/api/owner").json() == {"enabled": False, "owner": False}
    assert c.post("/api/owner/login", json={"token": "x"}).status_code == 400


def test_owner_login_wrong_token_rejected(client):
    assert client.post("/api/owner/login", json={"token": "nope"}).status_code == 401
    assert client.get("/api/owner").json()["owner"] is False


def test_owner_login_sets_session_and_logout_clears(client):
    r = client.post("/api/owner/login", json={"token": "ownsecret"})
    assert r.status_code == 200 and r.json()["owner"] is True
    assert client.get("/api/owner").json()["owner"] is True
    client.post("/api/owner/logout")
    assert client.get("/api/owner").json()["owner"] is False


def test_activate_for_mcp_registers_then_activates(monkeypatch):
    monkeypatch.setitem(ui.REGISTRY, "cp_url", "https://cp.test")
    monkeypatch.setitem(ui.REGISTRY, "token", "regtok")
    monkeypatch.setitem(ui.REGISTRY, "workspace", "owner")
    monkeypatch.setitem(ui.SELF_URLS, "odoo", "http://nilscript-playground:8101")
    calls = []
    monkeypatch.setattr(ui, "_registry_call", lambda m, p, b=None: (calls.append((m, p, b)) or 200))
    ok, _ = ui._activate_for_mcp("odoo", label="Odoo CRM", system="odoo_crm")
    assert ok is True
    assert calls[0][0] == "POST" and calls[0][1] == "/adapters/register"
    assert calls[0][2]["url"] == "http://nilscript-playground:8101" and calls[0][2]["bearer"] == ui.BEARER
    assert calls[1][1] == "/adapters/owner/odoo/activate"


def test_activate_for_mcp_unconfigured_is_noop(monkeypatch):
    monkeypatch.setitem(ui.REGISTRY, "cp_url", "")
    ok, msg = ui._activate_for_mcp("odoo", label="x", system="odoo_crm")
    assert ok is False and "registry not configured" in msg


def test_odoo_link_activates_only_for_owner(client, monkeypatch):
    monkeypatch.setattr(ui, "verify_odoo", lambda c: (True, "ok"))
    monkeypatch.setattr(ui, "spawn_odoo", lambda restart=False: (True, "up"))
    monkeypatch.setattr(ui, "_port_up", lambda u: True)

    async def _cc(name):
        return []

    monkeypatch.setattr(ui, "connect_checks", _cc)
    activated = []
    monkeypatch.setattr(ui, "_activate_for_mcp",
                        lambda *a, **k: (activated.append(a) or (True, "on")))

    body = {"url": "https://o", "db": "d", "login": "l", "api_key": "k"}
    # Anonymous link: shim runs locally, but the MCP is NOT repointed.
    r = client.post("/api/odoo", json=body).json()
    assert r["ok"] is True and r["mcp_active"] is False and activated == []

    # Owner session: the just-linked Odoo becomes the MCP's active backend.
    client.post("/api/owner/login", json={"token": "ownsecret"})
    r2 = client.post("/api/odoo", json=body).json()
    assert r2["mcp_active"] is True and len(activated) == 1

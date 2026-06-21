"""Playground owner session: auto-issued per session (never prompts), gates active-adapter routing."""

import pytest

pytest.importorskip("litellm", reason="demo UI needs litellm")
pytest.importorskip("fastapi", reason="needs fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import nilscript.demo.demo_ui as ui  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(ui.REGISTRY, "owner_token", "server-signing-secret")
    monkeypatch.setitem(ui.REGISTRY, "cp_url", "https://cp.test")
    monkeypatch.setitem(ui.REGISTRY, "token", "regtok")
    monkeypatch.setitem(ui.REGISTRY, "workspace", "owner")
    return TestClient(ui.app)


def test_owner_disabled_when_no_secret(monkeypatch):
    monkeypatch.setitem(ui.REGISTRY, "owner_token", "")
    c = TestClient(ui.app)
    assert c.get("/api/owner").json() == {"enabled": False, "owner": False}


def test_owner_auto_issued_no_prompt(client):
    # Loading the dashboard auto-grants an owner session — never asks for a token.
    r = client.get("/api/owner").json()
    assert r["enabled"] is True and r["owner"] is True
    # cookie minted and sticks across calls
    assert ui.OWNER_COOKIE in client.cookies
    assert client.get("/api/owner").json()["owner"] is True


def test_each_session_gets_a_distinct_token(client):
    c1, c2 = TestClient(ui.app), TestClient(ui.app)
    c1.get("/api/owner")
    c2.get("/api/owner")
    t1, t2 = c1.cookies.get(ui.OWNER_COOKIE), c2.cookies.get(ui.OWNER_COOKIE)
    assert t1 and t2 and t1 != t2  # a fresh per-session token, not a shared one


def test_is_owner_rejects_forged_or_missing_cookie(client):
    class _Req:
        cookies = {ui.OWNER_COOKIE: "deadbeef.notavalidsig"}
    assert ui._is_owner(_Req()) is False
    class _Bare:
        cookies = {}
    assert ui._is_owner(_Bare()) is False


def test_activate_for_mcp_registers_then_activates(monkeypatch):
    monkeypatch.setitem(ui.REGISTRY, "cp_url", "https://cp.test")
    monkeypatch.setitem(ui.REGISTRY, "token", "regtok")
    monkeypatch.setitem(ui.REGISTRY, "workspace", "owner")
    monkeypatch.setitem(ui.SELF_URLS, "odoo", "http://nilscript-playground:8101")
    calls = []
    monkeypatch.setattr(ui, "_registry_call", lambda m, p, b=None: (calls.append((m, p, b)) or 200))
    ok, _ = ui._activate_for_mcp("odoo", label="Odoo CRM", system="odoo_crm")
    assert ok is True
    assert calls[0][1] == "/adapters/register" and calls[0][2]["url"] == "http://nilscript-playground:8101"
    assert calls[1][1] == "/adapters/owner/odoo/activate"


def test_activate_for_mcp_unconfigured_is_noop(monkeypatch):
    monkeypatch.setitem(ui.REGISTRY, "cp_url", "")
    ok, msg = ui._activate_for_mcp("odoo", label="x", system="odoo_crm")
    assert ok is False and "registry not configured" in msg


def test_odoo_link_activates_for_auto_owner_session(client, monkeypatch):
    monkeypatch.setattr(ui, "verify_odoo", lambda c: (True, "ok"))
    monkeypatch.setattr(ui, "spawn_odoo", lambda restart=False: (True, "up"))
    monkeypatch.setattr(ui, "_port_up", lambda u: True)

    async def _cc(name):
        return []

    monkeypatch.setattr(ui, "connect_checks", _cc)
    activated = []
    monkeypatch.setattr(ui, "_activate_for_mcp",
                        lambda *a, **k: (activated.append(a) or (True, "on")))

    client.get("/api/owner")  # auto-issues the owner cookie (no prompt)
    r = client.post("/api/odoo", json={"url": "https://o", "db": "d", "login": "l", "api_key": "k"}).json()
    assert r["ok"] is True and r["mcp_active"] is True and len(activated) == 1

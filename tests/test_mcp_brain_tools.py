"""The agent-facing Business Graph (brain) READ tools.

BrainTools is a thin read-only HTTP relay to the brain's `/api/graph/*` endpoints. These tests back it
with an httpx MockTransport (no network) to prove: the right endpoint is hit, the tenant is applied,
`graph(kind=…)` filters correctly, and failures degrade to a structured `{"error": …}`. A registration
test proves the five tools land on a FastMCP server. This is the fix for the agent improvising on
"show my policies" — policies are graph nodes, surfaced deterministically here.
"""

from __future__ import annotations

import httpx
import pytest

from nilscript.mcp.brain_tools import BrainTools

_NODES = [
    {"id": "entity:invoice", "kind": "entity", "label": {"en": "Invoice"}},
    {"id": "policy:payment-approval", "kind": "policy", "label": {"en": "Payment Approval"}},
    {"id": "role:cfo", "kind": "role", "label": {"en": "CFO"}},
]


def _brain(handler) -> BrainTools:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://brain")
    return BrainTools("http://brain", tenant="ws_acme", client=client)


def _ok(handler):
    """Wrap a path→json map into a MockTransport handler that records the last request."""
    seen: dict = {}

    def _h(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return handler(request)

    return _h, seen


async def test_graph_filters_to_policies() -> None:
    handler, seen = _ok(lambda r: httpx.Response(200, json={"nodes": _NODES}))
    out = await _brain(handler).graph(kind="policy")
    assert seen["path"] == "/api/graph/nodes"
    assert seen["params"]["tenant"] == "ws_acme"
    assert out["count"] == 1
    assert out["nodes"][0]["id"] == "policy:payment-approval"


async def test_graph_no_kind_returns_all() -> None:
    handler, _ = _ok(lambda r: httpx.Response(200, json={"nodes": _NODES}))
    out = await _brain(handler).graph()
    assert out["count"] == 3


async def test_graph_rejects_unknown_kind() -> None:
    handler, _ = _ok(lambda r: httpx.Response(200, json={"nodes": _NODES}))
    out = await _brain(handler).graph(kind="invoiceish")
    assert "error" in out and "unknown kind" in out["error"]


async def test_cycles_hits_cycles_endpoint() -> None:
    handler, seen = _ok(lambda r: httpx.Response(200, json={"tenant": "ws_acme", "cycles": []}))
    out = await _brain(handler).cycles()
    assert seen["path"] == "/api/graph/cycles"
    assert out["tenant"] == "ws_acme"


async def test_instances_hits_instances_summary() -> None:
    handler, seen = _ok(lambda r: httpx.Response(200, json={"entity_types": {"invoice": {"overdue": 12}}}))
    out = await _brain(handler).instances()
    assert seen["path"] == "/api/graph/instances/summary"
    assert out["entity_types"]["invoice"]["overdue"] == 12


async def test_activity_hits_activity_endpoint() -> None:
    handler, seen = _ok(lambda r: httpx.Response(200, json={"added": []}))
    await _brain(handler).activity()
    assert seen["path"] == "/api/graph/activity"


async def test_brain_error_degrades_to_structured_error() -> None:
    handler, _ = _ok(lambda r: httpx.Response(503, text="upstream down"))
    out = await _brain(handler).cycles()
    assert "error" in out and "503" in out["error"]


async def test_missing_tenant_is_a_clear_error() -> None:
    handler, _ = _ok(lambda r: httpx.Response(200, json={"nodes": []}))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://brain")
    notenant = BrainTools("http://brain", tenant="", client=client)
    out = await notenant.graph(kind="policy")
    assert "error" in out and "tenant" in out["error"]


def test_from_env_none_without_url(monkeypatch) -> None:
    monkeypatch.delenv("NIL_BRAIN_URL", raising=False)
    assert BrainTools.from_env() is None


def test_from_env_builds_with_url(monkeypatch) -> None:
    monkeypatch.setenv("NIL_BRAIN_URL", "https://brain.example")
    monkeypatch.setenv("NIL_BRAIN_TENANT", "ws_acme")
    bt = BrainTools.from_env()
    assert bt is not None
    assert bt._tenant == "ws_acme"


@pytest.mark.skipif(
    pytest.importorskip("mcp", reason="needs the [mcp] extra") is None, reason="needs mcp"
)
async def test_brain_tools_register_on_server() -> None:
    from nilscript.mcp.server import build_server, build_tools

    handler, _ = _ok(lambda r: httpx.Response(200, json={"nodes": []}))
    server = build_server(build_tools(adapter_url="http://127.0.0.1:9", bearer=""), brain_tools=_brain(handler))
    names = {t.name for t in await server.list_tools()}
    assert {"nil_graph", "nil_cycles", "nil_overview", "nil_instances", "nil_activity"} <= names

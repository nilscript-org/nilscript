"""NilTools (the NIL-MCP tool surface): two-step gate, refusal-as-value, idempotent commit.

These exercise the pure tool logic directly over a mocked adapter (respx) — no MCP transport
required, because `nilscript.mcp.tools` is deliberately MCP-SDK-free.
"""

import json
from typing import Any

import httpx
import pytest
import respx
from nilscript.mcp.tools import NilTools
from nilscript.sdk.client import NilClient
from nilscript.sdk.grants import GrantRef
from nilscript.sdk.idempotency import commit_idempotency_key
from nilscript.sdk.transport import NilTransport

BASE = "https://adapter.example.sa"
SESSION = "mcp-session"

GRANT = GrantRef.from_secret(
    grant_id="g-1", workspace="ws-1", secret="s3cret-token", scopes=frozenset({"commerce.*"})
)


def server_envelope(performative: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "nil": "0.1",
        "id": "srv-0001",
        "performative": performative,
        "grant": "g-1",
        "workspace": "ws-1",
        "ts": "2026-06-19T07:00:01Z",
        "body": body,
    }


PROPOSAL_OK = {
    "outcome": "proposal",
    "id": "prop-0001",
    "verb": "commerce.create_product",
    "tier": "HIGH",
    "preview": {"ar": "منتج: مصباح أورورا بسعر 49.90"},
    "expires_at": "2026-06-20T07:00:00Z",
}


def make_tools(gate: str = "two-step") -> tuple[NilTools, NilTransport]:
    transport = NilTransport(base_url=BASE, bearer_secret=GRANT.bearer_secret())
    client = NilClient(transport=transport, grant=GRANT)
    return NilTools(client, transport, session_id=SESSION, gate=gate), transport


@respx.mock
async def test_describe_returns_skeleton() -> None:
    respx.get(f"{BASE}/nil/v0.1/describe").mock(
        return_value=httpx.Response(
            200,
            json={
                "nil": "0.1",
                "system": "demo",
                "verbs": ["commerce.create_product"],
                "targets": {"products": {"exists": True, "fields": []}},
            },
        )
    )
    tools, _ = make_tools()
    skeleton = await tools.describe()
    assert skeleton["reachable"] is True and skeleton["conformant"] is True
    assert "commerce.create_product" in skeleton["verbs"]
    assert skeleton["ready"] == ["products"]


@respx.mock
async def test_propose_returns_preview_no_side_effect() -> None:
    route = respx.post(f"{BASE}/nil/v0.1/propose").mock(
        return_value=httpx.Response(200, json=server_envelope("PROPOSAL", PROPOSAL_OK))
    )
    tools, _ = make_tools()
    preview = await tools.propose("commerce.create_product", {"name": "Aurora", "price": 49.9})
    assert preview["outcome"] == "proposal"
    assert preview["id"] == "prop-0001"
    assert preview["tier"] == "HIGH"
    assert route.called  # PROPOSE was sent; the only write path is commit


@respx.mock
async def test_propose_refusal_is_value_not_exception() -> None:
    respx.post(f"{BASE}/nil/v0.1/propose").mock(
        return_value=httpx.Response(
            200,
            json=server_envelope(
                "PROPOSAL", {"outcome": "refusal", "code": "UNKNOWN_VERB", "message": "no such verb"}
            ),
        )
    )
    tools, _ = make_tools()
    out = await tools.propose("commerce.nope", {})
    assert out["outcome"] == "refusal"
    assert out["code"] == "UNKNOWN_VERB"


@respx.mock
async def test_commit_uses_deterministic_idempotency_key() -> None:
    respx.post(f"{BASE}/nil/v0.1/propose").mock(
        return_value=httpx.Response(200, json=server_envelope("PROPOSAL", PROPOSAL_OK))
    )
    commit_route = respx.post(f"{BASE}/nil/v0.1/commit").mock(
        return_value=httpx.Response(
            200,
            json=server_envelope(
                "STATUS", {"proposal": "prop-0001", "state": "executed", "replayed": False}
            ),
        )
    )
    tools, _ = make_tools()
    await tools.propose("commerce.create_product", {"name": "Aurora", "price": 49.9})
    result = await tools.commit("prop-0001")
    assert result["committed"] is True
    assert result["state"] == "executed"
    wire = json.loads(commit_route.calls.last.request.content)
    assert wire["id"] == commit_idempotency_key(SESSION, "prop-0001")
    assert wire["body"]["idempotency_key"] == commit_idempotency_key(SESSION, "prop-0001")


@respx.mock
async def test_gate_human_holds_high_tier_commit() -> None:
    respx.post(f"{BASE}/nil/v0.1/propose").mock(
        return_value=httpx.Response(200, json=server_envelope("PROPOSAL", PROPOSAL_OK))
    )
    commit_route = respx.post(f"{BASE}/nil/v0.1/commit").mock(
        return_value=httpx.Response(
            200, json=server_envelope("STATUS", {"proposal": "prop-0001", "state": "executed"})
        )
    )
    tools, _ = make_tools(gate="human")
    await tools.propose("commerce.create_product", {"name": "Aurora", "price": 49.9})
    result = await tools.commit("prop-0001")
    assert result["committed"] is False
    assert result["outcome"] == "approval_required"
    assert result["tier"] == "HIGH"
    assert not commit_route.called  # the HIGH-tier write was held, never executed


@respx.mock
async def test_human_gate_is_per_connection() -> None:
    # Multi-tenant: tier memory is keyed by session, so two agents on one server don't collide.
    respx.post(f"{BASE}/nil/v0.1/propose").mock(
        return_value=httpx.Response(200, json=server_envelope("PROPOSAL", PROPOSAL_OK))
    )
    respx.post(f"{BASE}/nil/v0.1/commit").mock(
        return_value=httpx.Response(
            200, json=server_envelope("STATUS", {"proposal": "prop-0001", "state": "executed"})
        )
    )
    tools, _ = make_tools(gate="human")
    # Agent A proposes the HIGH-tier write → A's commit is held.
    await tools.propose("commerce.create_product", {"name": "A"}, session_id="agentA")
    a = await tools.commit("prop-0001", session_id="agentA")
    assert a["committed"] is False and a["outcome"] == "approval_required"
    # Agent B never proposed it → no tier memory under B → isolated from A's gate.
    b = await tools.commit("prop-0001", session_id="agentB")
    assert b["committed"] is True


@respx.mock
async def test_rollback_previews_compensation() -> None:
    respx.post(f"{BASE}/nil/v0.1/rollback").mock(
        return_value=httpx.Response(
            200,
            json=server_envelope(
                "PROPOSAL",
                {
                    "outcome": "proposal",
                    "id": "comp-0001",
                    "verb": "commerce.delete_product",
                    "tier": "MEDIUM",
                    "preview": {"ar": "حذف المنتج لعكس الإنشاء"},
                    "expires_at": "2026-06-20T07:00:00Z",
                },
            ),
        )
    )
    tools, _ = make_tools()
    out = await tools.rollback("token-abcdefgh", "saga_unwind")
    assert out["outcome"] == "proposal"
    assert out["verb"] == "commerce.delete_product"


async def test_rollback_rejects_bad_reason() -> None:
    tools, _ = make_tools()
    out = await tools.rollback("token-abcdefgh", "not_a_reason")
    assert out["error"] == "invalid_reason"


def test_bad_gate_rejected() -> None:
    transport = NilTransport(base_url=BASE, bearer_secret="x")
    client = NilClient(transport=transport, grant=GRANT)
    with pytest.raises(ValueError, match="gate must be one of"):
        NilTools(client, transport, gate="nonsense")

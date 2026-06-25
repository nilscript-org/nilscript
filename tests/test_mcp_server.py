"""Server surface: the skill served over MCP (resource + prompt), connection recipe, ASGI app."""

import pytest

pytest.importorskip("mcp", reason="needs the [mcp] extra")

from nilscript.mcp.server import (  # noqa: E402
    HTTP_PATH,
    build_asgi_app,
    build_server,
    build_tools,
    connection_info,
)
from nilscript.mcp.skill import SKILL_URI, skill_body, skill_meta  # noqa: E402


def _tools():  # type: ignore[no-untyped-def]
    return build_tools(adapter_url="http://127.0.0.1:9", bearer="")


def test_skill_body_strips_frontmatter() -> None:
    body = skill_body()
    assert not body.startswith("---")
    assert "nil_propose" in body and "nil_commit" in body
    assert skill_meta()["name"] == "using-nilscript"


async def test_server_serves_skill_as_resource_and_prompt() -> None:
    server = build_server(_tools(), dynamic_verbs=["commerce.create_product"])

    resources = {str(r.uri) for r in await server.list_resources()}
    assert SKILL_URI in resources
    assert "nil://skeleton" in resources

    prompts = {p.name for p in await server.list_prompts()}
    assert "using_nilscript" in prompts

    tools = {t.name for t in await server.list_tools()}
    assert {"nil_describe", "nil_propose", "nil_commit", "nil_rollback"} <= tools
    assert "propose_commerce_create_product" in tools


def test_connection_info_stdio_and_remote() -> None:
    stdio = connection_info(adapter_url="http://localhost:8080", transport="stdio")
    assert stdio["stdio"]["claude_desktop_config"]["mcpServers"]["nilscript"]["command"] == "nilscript"
    assert "--adapter-url" in stdio["stdio"]["command"]
    assert stdio["remote"]["url"] is None  # stdio has no remote URL

    http = connection_info(adapter_url="http://localhost:8080", transport="streamable-http", port=8765)
    assert http["remote"]["url"].endswith(f":8765{HTTP_PATH}")

    public = connection_info(
        adapter_url="http://x", transport="streamable-http", public_url="https://nilscript.org/mcp"
    )
    assert public["remote"]["url"] == "https://nilscript.org/mcp"


def test_build_asgi_app_returns_callable_even_if_adapter_unreachable() -> None:
    # discovery on an unreachable adapter yields no dynamic tools but must not raise.
    app = build_asgi_app(adapter_url="http://127.0.0.1:9", bearer="")
    assert callable(app)


def test_build_server_honors_allowed_hosts() -> None:
    # FastMCP only reads `transport_security` as a constructor kwarg and otherwise auto-enables a
    # localhost-only allowlist — so a server reachable by its container/service name (e.g. another
    # in-cluster agent connecting to `nilscript-mcp:8765`) needs the deploy to widen the allowlist.
    server = build_server(_tools(), allowed_hosts=["nilscript-mcp:8765", "mcp.wosool.ai"])
    ts = server.settings.transport_security
    assert ts is not None
    assert ts.enable_dns_rebinding_protection is True
    assert "nilscript-mcp:8765" in ts.allowed_hosts
    assert "mcp.wosool.ai" in ts.allowed_hosts


def test_build_asgi_app_reads_allowed_hosts_from_env(monkeypatch) -> None:
    # The deploy sets NIL_MCP_ALLOWED_HOSTS; build_asgi_app must thread it to the FastMCP server so
    # the DNS-rebinding guard admits the in-cluster + public hosts. Accepts JSON or comma-separated.
    monkeypatch.setenv("NIL_MCP_ALLOWED_HOSTS", '["nilscript-mcp:*","mcp.wosool.ai"]')
    captured: dict = {}
    import nilscript.mcp.server as srv

    real_build_server = srv.build_server

    def _spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["allowed_hosts"] = kwargs.get("allowed_hosts")
        return real_build_server(*args, **kwargs)

    monkeypatch.setattr(srv, "build_server", _spy)
    app = build_asgi_app(adapter_url="http://127.0.0.1:9", bearer="")
    assert callable(app)
    assert captured["allowed_hosts"] == ["nilscript-mcp:*", "mcp.wosool.ai"]


def test_remote_auth_gate_protects_mcp_but_not_healthz() -> None:
    # sync test: build_asgi_app calls asyncio.run() internally (discovery), so it can't run inside
    # an active event loop — we drive the httpx assertions via a nested asyncio.run.
    import asyncio

    import httpx

    app = build_asgi_app(adapter_url="http://127.0.0.1:9", bearer="", auth_token="sekret-token")

    async def _check() -> None:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # /healthz is open (adapter unreachable -> 503, but never 401)
            assert (await c.get("/healthz")).status_code != 401
            # /mcp without the bearer is rejected at the front door
            assert (await c.post("/mcp", json={})).status_code == 401
            # with the bearer it passes the gate (downstream MCP status, just not 401)
            ok = await c.post("/mcp", json={}, headers={"authorization": "Bearer sekret-token"})
            assert ok.status_code != 401

    asyncio.run(_check())

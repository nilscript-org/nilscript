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

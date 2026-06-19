"""The generic NIL-MCP server — one front door for any MCP-compatible agent.

This is the ONLY module that imports the `mcp` SDK (pulled by the `[mcp]` extra). It builds the
exact southbound wiring `nilscript run` already uses (`GrantRef` → `NilTransport` → `NilClient`),
wraps it in `NilTools`, and exposes:

- six generic NIL primitives + per-verb `propose_<verb>` tools (the tool list IS the skeleton);
- the `using-nilscript` **skill** as an MCP resource + prompt (capability AND discipline);
- a live `nil://skeleton` resource.

Two transports: `stdio` (Claude Desktop / Cursor) and `streamable-http` (remote, deployable to
nilscript.org). `build_asgi_app()` returns an ASGI app for production servers (uvicorn/gunicorn).
"""

from __future__ import annotations

import json
from typing import Any

# Imported at module scope (not lazily) so the wrapped tool functions' stringized annotations
# (PEP 563) resolve via get_type_hints — FastMCP injects `ctx: Context` and hides it from the schema.
# This makes importing nilscript.mcp.server require the [mcp] extra (callers catch ModuleNotFoundError).
from mcp.server.fastmcp import Context, FastMCP

from nilscript.mcp.skill import SKILL_URI, skill_body, skill_meta
from nilscript.mcp.tools import NilTools, session_key
from nilscript.sdk.client import NilClient
from nilscript.sdk.grants import GrantRef
from nilscript.sdk.transport import NilTransport

HTTP_PATH = "/mcp"  # the streamable-http mount path clients connect to

_INSTRUCTIONS = (
    "This server is the NIL gate to a backend. Every write is two-step: call nil_propose to get a "
    "preview (no side effect), then nil_commit to execute. Reads use nil_query. nil_describe lists "
    "the verbs the backend actually exposes — do not invent others. To reverse a committed effect, "
    "call nil_rollback (it previews a compensation; commit it like any proposal). Refusals "
    "(UNKNOWN_VERB, UPSTREAM_UNAVAILABLE, IRREVERSIBLE, COMPENSATION_EXPIRED) are answers — read "
    "them, do not retry blindly. Load the 'using_nilscript' prompt or the "
    f"'{SKILL_URI}' resource for the full discipline."
)


def build_tools(
    *,
    adapter_url: str,
    grant_id: str = "local",
    workspace: str = "",
    bearer: str = "",
    scopes: frozenset[str] | None = None,
    session_id: str = "mcp-session",
    gate: str = "two-step",
) -> NilTools:
    """Wire the SDK client to the adapter and wrap it in the MCP tool surface.

    Mirrors `cli._cmd_run` so local-run and MCP behave identically against the same shim.
    """
    grant = GrantRef.from_secret(
        grant_id=grant_id,
        workspace=workspace,
        secret=bearer,
        scopes=scopes if scopes is not None else frozenset({"*"}),
    )
    transport = NilTransport(base_url=adapter_url, bearer_secret=bearer)
    client = NilClient(transport=transport, grant=grant)
    return NilTools(client, transport, session_id=session_id, gate=gate)


def build_server(
    tools: NilTools,
    *,
    name: str = "nilscript",
    dynamic_verbs: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
):  # type: ignore[no-untyped-def]
    """Bind the NilTools surface onto a FastMCP server. Imports `mcp` lazily.

    `dynamic_verbs` (the live skeleton) adds one `propose_<verb>` tool per exposed verb. `host`/
    `port` apply to the HTTP transports. The `using-nilscript` skill is served as a resource + prompt.
    """
    server = FastMCP(name, instructions=_INSTRUCTIONS, host=host, port=port)

    _register_tools(server, tools)
    if dynamic_verbs:
        from nilscript.mcp.dynamic import register_dynamic_tools

        register_dynamic_tools(server, tools, dynamic_verbs)
    _register_skill(server, tools)
    return server


def _register_tools(server: Any, tools: NilTools) -> None:
    """Wrap each primitive with the MCP Context so per-connection session isolation applies (the
    server fronts ONE adapter, but many agents may connect — each gets its own proposal/idempotency
    session via `session_key(ctx)`). The `ctx` param is injected by FastMCP and hidden from the schema.
    """

    async def nil_describe(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await tools.describe()

    async def nil_propose(verb: str, args: dict[str, Any] | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await tools.propose(verb, args, session_id=session_key(ctx))

    async def nil_commit(proposal_id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await tools.commit(proposal_id, session_id=session_key(ctx))

    async def nil_query(verb: str, args: dict[str, Any] | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await tools.query(verb, args)

    async def nil_status(proposal_id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await tools.status(proposal_id)

    async def nil_rollback(compensation_token: str, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await tools.rollback(compensation_token, reason, session_id=session_key(ctx))

    server.add_tool(
        nil_describe, name="nil_describe",
        description="Discover the backend skeleton: the verbs and targets it actually exposes. No side effect.",
    )
    server.add_tool(
        nil_propose, name="nil_propose",
        description="Preview an intent (verb + args). NO side effect: returns a human-readable preview "
        "with a reversibility tier, or a structured refusal. Always call this before nil_commit.",
    )
    server.add_tool(
        nil_commit, name="nil_commit",
        description="Execute a previously previewed proposal by its id. This is the ONLY tool that writes. "
        "Idempotent: re-committing the same proposal replays, it never double-writes.",
    )
    server.add_tool(
        nil_query, name="nil_query",
        description="Read live business truth (verb + args). No side effect.",
    )
    server.add_tool(
        nil_status, name="nil_status",
        description="Get the status/result of a proposal by id, including its compensation handle.",
    )
    server.add_tool(
        nil_rollback, name="nil_rollback",
        description="Request a governed reversal of a committed effect (compensation_token + reason: "
        "saga_unwind|owner_cancel|downstream_failed|agent_repair). Previews a compensation to commit, "
        "or refuses honestly (IRREVERSIBLE / COMPENSATION_EXPIRED). No silent write.",
    )


def _register_skill(server: Any, tools: NilTools) -> None:
    """Serve the discipline over the wire: the skill as a resource + prompt, plus a live skeleton."""
    meta = skill_meta()

    def _skill_resource() -> str:
        return skill_body()

    server.resource(
        SKILL_URI,
        name="using-nilscript",
        description=meta.get("description", "How to drive the NIL gate correctly."),
        mime_type="text/markdown",
    )(_skill_resource)

    async def _skeleton_resource() -> str:
        return json.dumps(await tools.describe(), ensure_ascii=False)

    server.resource(
        "nil://skeleton",
        name="nil-skeleton",
        description="The connected backend's live skeleton (verbs + targets).",
        mime_type="application/json",
    )(_skeleton_resource)

    def _skill_prompt() -> str:
        return skill_body()

    server.prompt(
        name="using_nilscript",
        description="Load the propose→approve→commit→rollback discipline for this NIL gate.",
    )(_skill_prompt)


async def _discover_verbs(adapter_url: str, bearer: str) -> list[str]:
    """Fetch the adapter skeleton with a throwaway transport (its own event loop), so the server's
    long-lived transport is only ever used inside the serving loop. Unreachable ⇒ no dynamic tools."""
    from nilscript.sdk.connect import handshake

    transport = NilTransport(base_url=adapter_url, bearer_secret=bearer)
    try:
        skeleton = await handshake(transport)
        return list(skeleton.get("verbs", []))
    finally:
        await transport.aclose()


def connection_info(
    *,
    adapter_url: str,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    public_url: str | None = None,
    bearer_env: str = "NIL_GRANT_SECRET",
) -> dict[str, Any]:
    """A copy-pasteable connection recipe for an MCP client (Claude Desktop / remote connector)."""
    stdio_args = ["mcp", "--adapter-url", adapter_url]
    claude_desktop = {
        "mcpServers": {
            "nilscript": {
                "command": "nilscript",
                "args": stdio_args,
                "env": {bearer_env: "<your-bearer-secret>"},
            }
        }
    }
    url = public_url or (f"http://{host}:{port}{HTTP_PATH}" if transport != "stdio" else None)
    return {
        "transport": transport,
        "skill_resource": SKILL_URI,
        "skill_prompt": "using_nilscript",
        "stdio": {
            "command": "nilscript " + " ".join(stdio_args),
            "claude_desktop_config": claude_desktop,
        },
        "remote": {
            "url": url,
            "note": "Add as a Custom Connector / remote MCP server in the client."
            if url
            else "run with --transport streamable-http to enable a remote URL",
        },
    }


def serve(
    *,
    adapter_url: str,
    grant_id: str = "local",
    workspace: str = "",
    bearer: str = "",
    scopes: frozenset[str] | None = None,
    gate: str = "two-step",
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    dynamic_tools: bool = True,
) -> None:
    """Build and run the server (blocking). Called by `nilscript mcp`.

    `transport='stdio'` for local IDE clients; `'streamable-http'` (or `'sse'`) for a remote URL.
    """
    import asyncio

    verbs: list[str] = []
    if dynamic_tools:
        verbs = asyncio.run(_discover_verbs(adapter_url, bearer))

    tools = build_tools(
        adapter_url=adapter_url, grant_id=grant_id, workspace=workspace,
        bearer=bearer, scopes=scopes, gate=gate,
    )
    server = build_server(tools, dynamic_verbs=verbs, host=host, port=port)
    server.run(transport=transport)  # type: ignore[arg-type]


def build_asgi_app(
    *,
    adapter_url: str,
    grant_id: str = "local",
    workspace: str = "",
    bearer: str = "",
    scopes: frozenset[str] | None = None,
    gate: str = "two-step",
    dynamic_tools: bool = True,
):  # type: ignore[no-untyped-def]
    """Return a streamable-HTTP ASGI app for production hosting (uvicorn/gunicorn behind nilscript.org).

    The skeleton is discovered once at build time so per-verb tools reflect the mounted adapter.
    """
    import asyncio

    verbs: list[str] = []
    if dynamic_tools:
        verbs = asyncio.run(_discover_verbs(adapter_url, bearer))
    tools = build_tools(
        adapter_url=adapter_url, grant_id=grant_id, workspace=workspace,
        bearer=bearer, scopes=scopes, gate=gate,
    )
    server = build_server(tools, dynamic_verbs=verbs)
    return server.streamable_http_app()

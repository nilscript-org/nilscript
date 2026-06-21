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
from nilscript.mcp.tenant import Tenant, resolve_tenant
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


class ToolsProvider:
    """Resolves the `NilTools` (the backend) for a given MCP connection.

    Single-tenant returns one shared instance for every connection (today's behavior). Multi-tenant
    returns a per-connection instance bound to the backend the agent supplied via `X-NIL-*` headers.
    """

    def get(self, ctx: Any) -> NilTools:  # pragma: no cover - interface
        raise NotImplementedError


class SingletonToolsProvider(ToolsProvider):
    """One shared backend for all connections (per-connection proposal isolation still applies via
    `session_id=session_key(ctx)` inside the tool calls)."""

    def __init__(self, tools: NilTools) -> None:
        self._tools = tools

    def get(self, ctx: Any) -> NilTools:
        return self._tools


class TenantToolsProvider(ToolsProvider):
    """Per-connection backend binding (multi-tenant). Builds and caches one `NilTools` per connection,
    pointed at the adapter the agent named in its `X-NIL-Adapter-Url` header. The kernel never holds
    the tenant's backend credentials — those live in the tenant's adapter; we only relay to it."""

    def __init__(
        self,
        *,
        default: Tenant | None = None,
        allow_insecure: bool = False,
        gate: str = "two-step",
        registry: Any = None,
    ) -> None:
        self._default = default
        self._allow_insecure = allow_insecure
        self._gate = gate
        self._registry = registry
        self._cache: dict[str, NilTools] = {}

    def get(self, ctx: Any) -> NilTools:
        key = session_key(ctx)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        tenant = resolve_tenant(
            ctx, default=self._default, multi_tenant=True,
            allow_insecure=self._allow_insecure, registry=self._registry,
        )
        tools = build_tools(
            adapter_url=tenant.adapter_url,
            bearer=tenant.bearer,
            grant_id=tenant.grant_id,
            workspace=tenant.workspace,
            scopes=tenant.scopes,
            session_id=key,
            gate=self._gate,
        )
        self._cache[key] = tools
        return tools


def build_server(
    tools: NilTools,
    *,
    name: str = "nilscript",
    dynamic_verbs: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    tools_provider: ToolsProvider | None = None,
):  # type: ignore[no-untyped-def]
    """Bind the NilTools surface onto a FastMCP server. Imports `mcp` lazily.

    `dynamic_verbs` (the live skeleton) adds one `propose_<verb>` tool per exposed verb. `host`/
    `port` apply to the HTTP transports. The `using-nilscript` skill is served as a resource + prompt.
    `tools_provider` (optional) resolves the backend per connection — pass a `TenantToolsProvider`
    for multi-tenant; default wraps `tools` in a `SingletonToolsProvider` (one shared backend). The
    skill/skeleton resources and any `dynamic_verbs` always reflect the `tools` backend (the default).
    """
    server = FastMCP(name, instructions=_INSTRUCTIONS, host=host, port=port)

    provider = tools_provider if tools_provider is not None else SingletonToolsProvider(tools)
    _register_tools(server, provider)
    if dynamic_verbs:
        from nilscript.mcp.dynamic import register_dynamic_tools

        register_dynamic_tools(server, tools, dynamic_verbs)
    _register_skill(server, tools)
    return server


def _register_tools(server: Any, provider: ToolsProvider) -> None:
    """Wrap each primitive with the MCP Context so the backend + per-connection session resolve from
    the connection: `provider.get(ctx)` picks the backend (one shared, or per-tenant from headers)
    and `session_key(ctx)` isolates each connection's proposal/idempotency session. `ctx` is injected
    by FastMCP and hidden from the schema.
    """

    async def nil_describe(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).describe()

    async def nil_propose(verb: str, args: dict[str, Any] | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).propose(verb, args, session_id=session_key(ctx))

    async def nil_commit(proposal_id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).commit(proposal_id, session_id=session_key(ctx))

    async def nil_query(verb: str, args: dict[str, Any] | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).query(verb, args)

    async def nil_status(proposal_id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).status(proposal_id)

    async def nil_rollback(compensation_token: str, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).rollback(compensation_token, reason, session_id=session_key(ctx))

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
    auth_token: str | None = None,
) -> None:
    """Build and run the server (blocking). Called by `nilscript mcp`.

    `transport='stdio'` for local IDE clients; `'streamable-http'` for a remote URL. HTTP transports
    go through `build_asgi_app` (so `/healthz` and the optional `auth_token` front-door apply) served
    by uvicorn; stdio uses the FastMCP runner directly (auth is implicit — the client owns the process).
    """
    if transport == "stdio":
        verbs: list[str] = []
        if dynamic_tools:
            import asyncio

            verbs = asyncio.run(_discover_verbs(adapter_url, bearer))
        tools = build_tools(
            adapter_url=adapter_url, grant_id=grant_id, workspace=workspace,
            bearer=bearer, scopes=scopes, gate=gate,
        )
        server = build_server(tools, dynamic_verbs=verbs, host=host, port=port)
        server.run(transport="stdio")
        return

    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("HTTP transport needs uvicorn (pip install 'uvicorn[standard]')") from exc

    app = build_asgi_app(
        adapter_url=adapter_url, grant_id=grant_id, workspace=workspace,
        bearer=bearer, scopes=scopes, gate=gate, dynamic_tools=dynamic_tools, auth_token=auth_token,
    )
    uvicorn.run(app, host=host, port=port)


def build_asgi_app(
    *,
    adapter_url: str,
    grant_id: str = "local",
    workspace: str = "",
    bearer: str = "",
    scopes: frozenset[str] | None = None,
    gate: str = "two-step",
    dynamic_tools: bool = True,
    auth_token: str | None = None,
    multi_tenant: bool = False,
    allow_insecure: bool = False,
):  # type: ignore[no-untyped-def]
    """Return a streamable-HTTP ASGI app for production hosting (uvicorn/gunicorn behind nilscript.org).

    Single-tenant (default): the skeleton is discovered once at build time so per-verb tools reflect
    the mounted adapter, and all connections share that backend.

    `multi_tenant=True`: each connection links its OWN backend via `X-NIL-Adapter-Url` (+ optional
    `X-NIL-Adapter-Bearer`/`X-NIL-Grant-Id`/`X-NIL-Workspace`/`X-NIL-Scopes`) headers; the env
    `adapter_url` becomes the fallback default for header-less connections. Per-verb dynamic tools are
    disabled in this mode (the skeleton differs per tenant) — agents call `nil_describe` instead.

    `auth_token` (if set) is a static front-door bearer required on /mcp — without it a public URL is
    open to anyone who can reach it (the NIL gate still bounds writes, but a connected agent can
    commit in-scope). `/healthz` stays open for load-balancer probes.
    """
    import asyncio

    verbs: list[str] = []
    if dynamic_tools and not multi_tenant:
        verbs = asyncio.run(_discover_verbs(adapter_url, bearer))
    tools = build_tools(
        adapter_url=adapter_url, grant_id=grant_id, workspace=workspace,
        bearer=bearer, scopes=scopes, gate=gate,
    )
    provider: ToolsProvider | None = None
    if multi_tenant:
        default = Tenant(
            adapter_url=adapter_url, bearer=bearer, grant_id=grant_id,
            workspace=workspace, scopes=scopes,
        )
        from nilscript.mcp.registry import make_registry_lookup

        provider = TenantToolsProvider(
            default=default, allow_insecure=allow_insecure, gate=gate,
            registry=make_registry_lookup(),
        )
    server = build_server(tools, dynamic_verbs=verbs, tools_provider=provider)
    app = server.streamable_http_app()  # MCP mounted at /mcp

    # A plain health route for load balancers / readiness probes (not part of MCP).
    from starlette.responses import JSONResponse

    from nilscript.sdk.connect import handshake

    async def _healthz(_request):  # type: ignore[no-untyped-def]
        transport = NilTransport(base_url=adapter_url, bearer_secret=bearer)
        try:
            skeleton = await handshake(transport)
        finally:
            await transport.aclose()
        ok = bool(skeleton.get("reachable") and skeleton.get("conformant"))
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "adapter": adapter_url,
             "reachable": skeleton.get("reachable"), "verbs": len(skeleton.get("verbs", []))},
            status_code=200 if ok else 503,
        )

    app.add_route("/healthz", _healthz, methods=["GET"])

    if auth_token:
        from starlette.middleware.base import BaseHTTPMiddleware

        class _BearerGate(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
                # /healthz stays open; everything else (the /mcp endpoint) needs the bearer.
                if request.url.path.rstrip("/") != "/healthz":
                    if request.headers.get("authorization", "") != f"Bearer {auth_token}":
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(_BearerGate)

    return app

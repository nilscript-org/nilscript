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
import os
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
    brain: Any = None,
    automation: Any = None,
) -> NilTools:
    """Wire the SDK client to the adapter and wrap it in the MCP tool surface.

    Mirrors `cli._cmd_run` so local-run and MCP behave identically against the same shim. `brain` (a
    BrainTools, optional) is the graph/meta execution domain behind nil_intent's router.
    """
    grant = GrantRef.from_secret(
        grant_id=grant_id,
        workspace=workspace,
        secret=bearer,
        scopes=scopes if scopes is not None else frozenset({"*"}),
    )
    transport = NilTransport(base_url=adapter_url, bearer_secret=bearer)
    client = NilClient(transport=transport, grant=grant)
    return NilTools(
        client, transport, session_id=session_id, gate=gate,
        brain=brain, automation=automation, workspace=workspace,
    )


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
    automation_tools: Any = None,
    brain_tools: Any = None,
    allowed_hosts: list[str] | None = None,
):  # type: ignore[no-untyped-def]
    """Bind the NilTools surface onto a FastMCP server. Imports `mcp` lazily.

    `dynamic_verbs` (the live skeleton) adds one `propose_<verb>` tool per exposed verb. `host`/
    `port` apply to the HTTP transports. The `using-nilscript` skill is served as a resource + prompt.
    `tools_provider` (optional) resolves the backend per connection — pass a `TenantToolsProvider`
    for multi-tenant; default wraps `tools` in a `SingletonToolsProvider` (one shared backend). The
    skill/skeleton resources and any `dynamic_verbs` always reflect the `tools` backend (the default).
    `automation_tools` (optional `AutomationTools`) adds the registry tools so an agent can author
    governed automations by talking — bound only when a control-plane registry is configured.
    `allowed_hosts` (optional) widens the streamable-HTTP DNS-rebinding guard: FastMCP only reads
    `transport_security` as a constructor kwarg and otherwise auto-enables a localhost-only allowlist,
    so a server reachable by its container/service or public host (e.g. another in-cluster agent
    dialing `nilscript-mcp:8765`) is 421-rejected unless those hosts are listed here. Entries may use
    the SDK's ``host:*`` wildcard-port form. The `/mcp` front door stays bearer-gated regardless.
    """
    transport_security = None
    if allowed_hosts:
        from mcp.server.transport_security import TransportSecuritySettings

        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
            allowed_origins=["*"],
        )
    server = FastMCP(
        name, instructions=_INSTRUCTIONS, host=host, port=port,
        transport_security=transport_security,
    )

    single = _single_surface()  # nil_intent subsumes reads/graph/automation when on
    provider = tools_provider if tools_provider is not None else SingletonToolsProvider(tools)
    _register_tools(server, provider, single_surface=single)
    if dynamic_verbs and not single:
        from nilscript.mcp.dynamic import register_dynamic_tools

        register_dynamic_tools(server, tools, dynamic_verbs)
    _register_skill(server, tools)
    if automation_tools is not None and not single:
        _register_automation_tools(server, automation_tools)
    if brain_tools is not None and not single:
        _register_brain_tools(server, brain_tools)
    return server


def _register_automation_tools(server: Any, auto: Any) -> None:
    """Bind the Automation Registry tools: draft → register → approve → run, plus list. Each relays to
    the control plane, preserving the gate (draft = preview-only; register lands pending_approval;
    run fires only an active automation)."""

    async def nil_automation_draft(
        automation_id: str, name: dict[str, Any], plan: dict[str, Any], trigger: dict[str, Any],
    ) -> dict[str, Any]:
        return await auto.draft(automation_id, name, plan, trigger)

    async def nil_automation_register(
        automation_id: str, name: dict[str, Any], plan: dict[str, Any], trigger: dict[str, Any],
    ) -> dict[str, Any]:
        return await auto.register(automation_id, name, plan, trigger)

    async def nil_automation_compose_register(
        automation_id: str, name: dict[str, Any], composed: dict[str, Any], trigger: dict[str, Any],
    ) -> dict[str, Any]:
        return await auto.compose_register(automation_id, name, composed, trigger)

    async def nil_automation_approve(workspace: str, automation_id: str, version: int) -> dict[str, Any]:
        return await auto.approve(workspace, automation_id, version)

    async def nil_automation_run(workspace: str, automation_id: str, idempotency_key: str) -> dict[str, Any]:
        return await auto.run(workspace, automation_id, idempotency_key)

    async def nil_automation_list(workspace: str) -> dict[str, Any]:
        return await auto.list(workspace)

    server.add_tool(
        nil_automation_draft, name="nil_automation_draft",
        description="Preview a governed automation: validate a plan (a Wosool DSL program) + trigger "
        "against the live backend. NO side effect — returns the validator verdict + content hash, or a "
        "structured refusal. The deterministic code decides admission, not the agent.",
    )
    server.add_tool(
        nil_automation_register, name="nil_automation_register",
        description="Register a validated automation into the registry as pending_approval (NOT armed). "
        "An owner approves it before it can run. Re-registering an identical plan is idempotent.",
    )
    server.add_tool(
        nil_automation_compose_register, name="nil_automation_compose_register",
        description="Register a CROSS-SYSTEM automation: `composed` = {workspace, stages:[{name, "
        "adapter, plan, input_from}]}, each stage validated against ITS adapter's live skeleton. "
        "Handoffs between stages are explicit ($.stage_1.step_2.output.id → next stage's $.input.*). "
        "Lands pending_approval. Use to wire two backends (e.g. PocketBase → Odoo) into one workflow.",
    )
    server.add_tool(
        nil_automation_approve, name="nil_automation_approve",
        description="Arm a registered automation (set it active) so it can fire. Operator-grade.",
    )
    server.add_tool(
        nil_automation_run, name="nil_automation_run",
        description="Fire an ACTIVE automation now (manual trigger). Requires an idempotency_key so a "
        "re-fire replays rather than executing twice. Returns the run with its terminal state + trace.",
    )
    server.add_tool(
        nil_automation_list, name="nil_automation_list",
        description="List a workspace's registered automations (latest version of each). No side effect.",
    )


def _register_brain_tools(server: Any, brain: Any) -> None:
    """Bind the read-only Business Graph tools. These answer "show my policies / cycles / what changed /
    what's overdue" deterministically from the brain read-model — so the agent never improvises (curl,
    file search) or mistakes a graph question for a missing kernel verb. All read-only, no side effect."""

    async def nil_graph(kind: str | None = None, tenant: str | None = None) -> dict[str, Any]:
        return await brain.graph(kind, tenant)

    async def nil_cycles(tenant: str | None = None) -> dict[str, Any]:
        return await brain.cycles(tenant)

    async def nil_overview(tenant: str | None = None) -> dict[str, Any]:
        return await brain.overview(tenant)

    async def nil_instances(tenant: str | None = None) -> dict[str, Any]:
        return await brain.instances(tenant)

    async def nil_activity(tenant: str | None = None) -> dict[str, Any]:
        return await brain.activity(tenant)

    server.add_tool(
        nil_graph, name="nil_graph",
        description="READ the Business Graph nodes from the brain. Pass kind to filter: 'policy' (the "
        "right way to answer 'show my policies' — policies are graph nodes, NOT kernel verbs), 'entity', "
        "'role', 'flow', or 'cycle'. No side effect. Use this for any 'show me my X' structure question.",
    )
    server.add_tool(
        nil_cycles, name="nil_cycles",
        description="READ the business cycles (e.g. Sales, Finance) with each cycle's goal, metrics, and "
        "members. The deterministic answer to 'show me the cycles'. No side effect.",
    )
    server.add_tool(
        nil_overview, name="nil_overview",
        description="READ a one-glance graph summary: counts of entities, roles, policies, flows, and "
        "cycles for the workspace. No side effect.",
    )
    server.add_tool(
        nil_instances, name="nil_instances",
        description="READ live instance tallies per entity type — totals plus derived-state counts such "
        "as overdue and awaiting_approval. The deterministic answer to 'how many invoices are overdue'. "
        "No side effect.",
    )
    server.add_tool(
        nil_activity, name="nil_activity",
        description="READ what recently changed in the Business Graph (latest version additions/diff). "
        "The deterministic answer to 'what changed this week'. No side effect.",
    )


def _single_surface() -> bool:
    """When on, nil_intent is the ONLY model-facing tool (plus describe/commit/status/rollback); the
    subsumed read/graph/automation tools are hidden. Makes the correct path the only obvious one."""
    return os.environ.get("NIL_MCP_SINGLE_SURFACE", "") not in ("", "0", "false", "False")


def _register_tools(server: Any, provider: ToolsProvider, *, single_surface: bool = False) -> None:
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

    async def nil_intent(about: str, where: list[dict[str, Any]] | None = None, seek: str = "all", change: dict[str, Any] | None = None, limit: int = 50, cursor: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).intent(about, where, seek, change, limit, cursor, session_id=session_key(ctx))

    async def nil_search(target: str, filter: list[dict[str, Any]] | None = None, fields: list[str] | None = None, limit: int = 50, cursor: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).search(target, filter, fields, limit, cursor)

    async def nil_count(target: str, filter: list[dict[str, Any]] | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).count(target, filter)

    async def nil_get(target: str, id: Any, fields: list[str] | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).get(target, id, fields)

    async def nil_aggregate(target: str, group_by: str, metrics: list[str] | None = None, filter: list[dict[str, Any]] | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).aggregate(target, group_by, metrics, filter)

    async def nil_export(target: str, filter: list[dict[str, Any]] | None = None, fields: list[str] | None = None, approved: bool = False, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).export(target, filter, fields, approved)

    async def nil_status(proposal_id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).status(proposal_id)

    async def nil_rollback(compensation_token: str, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        return await provider.get(ctx).rollback(compensation_token, reason, session_id=session_key(ctx))

    # The single-surface keepers: discovery, the one intent payload, and the governance verbs the
    # approval/reversal flow needs. Everything else is SUBSUMED by nil_intent and hidden when
    # NIL_MCP_SINGLE_SURFACE is on — so the model sees ONE obvious tool, not a menu.
    server.add_tool(
        nil_describe, name="nil_describe",
        description="Discover the backend skeleton: the verbs and targets it actually exposes. No side effect.",
    )
    server.add_tool(
        nil_commit, name="nil_commit",
        description="Execute a previously previewed proposal by its id. This is the ONLY tool that writes. "
        "Idempotent: re-committing the same proposal replays, it never double-writes.",
    )
    server.add_tool(
        nil_intent, name="nil_intent",
        description="THE primary tool. Express WHAT you want as one payload — about (an entity, e.g. "
        "res.partner), where ([{attr, rel, value}] with rel ∈ is|contains|gt|gte|lt|lte|between|in), and "
        "either seek (the|all|count|summary, a read) OR change ({op:create|update|remove, set:{...}}, a "
        "governed write). Reads return a lean result; a change returns a PREVIEW the gate/owner commits. "
        "You NEVER pick a verb, build a filter, or list to scan. `about` spans BOTH business entities "
        "(res.partner, crm.lead — routed to the backend) AND the Business Graph (policy, cycle, role, "
        "instance, overview, activity — routed to the brain). This is the ONE tool for reading and "
        "changing anything in the system. "
        "Find دينا → about='res.partner', where=[{attr:'name',rel:'contains',value:'دينا'}], seek='the'. "
        "Show policies → about='policy', seek='all'. Show business cycles → about='cycle', seek='all'. "
        "Update her phone → about='res.partner', where=[{attr:'name',rel:'contains',value:'دينا'}], change={op:'update', set:{phone:'…'}}.",
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
    if single_surface:
        return  # nil_intent subsumes the rest; hide them so there is ONE obvious tool
    server.add_tool(
        nil_propose, name="nil_propose",
        description="Preview an intent (verb + args). NO side effect: returns a preview + tier, or a refusal.",
    )
    server.add_tool(
        nil_query, name="nil_query",
        description="Read live business truth (verb + args). No side effect.",
    )
    server.add_tool(
        nil_search, name="nil_search",
        description="Lean, FILTERED, PAGINATED read of a target (filter=[{field,op,value}], small fields=, "
        "limit, cursor). Returns {items:[{id,…projected}], next_cursor} — never whole records, never "
        "unbounded; an over-cap page is REFUSED (narrow the filter or use nil_export), never truncated.",
    )
    server.add_tool(
        nil_count, name="nil_count",
        description="Just {count} for a target+filter. The FIRST call for any 'how many / does X exist' — "
        "never list to count.",
    )
    server.add_tool(
        nil_get, name="nil_get",
        description="One lean record by key (target + id + optional fields). For exact lookups.",
    )
    server.add_tool(
        nil_aggregate, name="nil_aggregate",
        description="Server-side rollup (target + group_by + metrics): 'revenue by country', 'count by "
        "status'. Small result; rows never enter context. Refuses → nil_export when unsupported.",
    )
    server.add_tool(
        nil_export, name="nil_export",
        description="Stream a bulk read to a DATA HANDLE (not rows): open it in your sandbox and use code "
        "(pandas/sqlite) for analysis over many rows. Bulk extraction is gated+audited.",
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


def _allowed_hosts_from_env() -> list[str] | None:
    """Parse ``NIL_MCP_ALLOWED_HOSTS`` (JSON list or comma-separated) for the DNS-rebinding allowlist.

    Set by the deploy when the server is reached by a name other than localhost (its container/service
    name, or a public ``mcp.*`` host behind a reverse proxy). ``None`` (unset/blank) keeps FastMCP's
    localhost-only default. Entries may use the SDK's ``host:*`` wildcard-port form.
    """
    raw = os.environ.get("NIL_MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        return None
    if raw.startswith("["):
        hosts = [str(h).strip() for h in json.loads(raw)]
    else:
        hosts = [h.strip() for h in raw.split(",")]
    hosts = [h for h in hosts if h]
    return hosts or None


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
    # Single-surface hides dynamic verbs anyway, so skip the startup adapter describe (an Odoo
    # fields_get that can take ~minute when the backend is cold) — it was the MCP's slow cold-start,
    # which is exactly what left a window for Hermes to discover an unready MCP and cache 0 tools.
    if dynamic_tools and not multi_tenant and not _single_surface():
        verbs = asyncio.run(_discover_verbs(adapter_url, bearer))
    from nilscript.mcp.automation_tools import AutomationTools
    from nilscript.mcp.brain_tools import BrainTools

    brain = BrainTools.from_env()  # graph/meta domain behind nil_intent (None if NIL_BRAIN_URL unset)
    automation = AutomationTools.from_env()  # automation domain behind nil_intent
    tools = build_tools(
        adapter_url=adapter_url, grant_id=grant_id, workspace=workspace,
        bearer=bearer, scopes=scopes, gate=gate, brain=brain, automation=automation,
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
    server = build_server(
        tools, dynamic_verbs=verbs, tools_provider=provider,
        automation_tools=automation,
        brain_tools=brain,
        allowed_hosts=_allowed_hosts_from_env(),
    )
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

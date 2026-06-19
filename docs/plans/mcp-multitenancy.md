# Multi-tenant remote NIL-MCP — implementation plan

**Status:** proposed · **Date:** 2026-06-19 · **Scope:** `src/nilscript/mcp/` (kernel)

## Goal

Let **each connecting agent link its own backend** through one shared `mcp.nilscript.org`
deployment — instead of every connection sharing the single backend baked in at boot.

## Non-goals

- Not changing the NIL protocol or the adapter contract.
- Not storing tenant data in the kernel (nilscript stores nothing — it links external API servers).
- Phase 1 does **not** target claude.ai web (that needs OAuth → Phase 2).

---

## Current architecture (single-tenant)

`mcp/app.py` calls `build_asgi_app()` once at import → one `NilTools` closed over every tool:

```
app = build_asgi_app(adapter_url=ENV["NIL_ADAPTER_URL"], bearer=ENV, grant=ENV)
        └─ build_tools(...) → NilTransport(base_url=adapter_url, bearer) → NilClient → NilTools   # singleton
nil_propose(verb, args, ctx) → tools.propose(...)   # `tools` is the one singleton
```

`session_key(ctx)` (`mcp/tools.py`) already isolates **proposal state** per connection, but all
connections share one transport → one adapter → one backend. `_BearerGate` checks one shared
`NIL_MCP_AUTH_TOKEN`.

## Target architecture (multi-tenant)

Move backend resolution from **build-time** to **connect-time**:

```
nil_propose(verb, args, ctx)
    → tenant = resolve_tenant(ctx)              # who/what backend is this connection?
    → tools  = tools_for(tenant)                # NilTools built per tenant, cached by session_key(ctx)
    → tools.propose(...)
```

The plumbing already exists: `build_tools(adapter_url=…, bearer=…, grant=…)` is parameterized, and
`session_key(ctx)` gives the cache key. This is "call `build_tools` per session," not a rewrite.

### Security invariant (the load-bearing decision)

**The shared MCP server MUST NOT hold tenants' backend credentials.** Each tenant runs/owns their
**adapter** (which holds the PocketBase/ERPNext creds). The MCP holds only, per tenant, an
`adapter_url` + a `bearer` to reach that adapter.

```
Agent ──auth──▶ MCP server ───────▶ tenant's ADAPTER ──▶ tenant's backend
                holds: {adapter_url, bearer}   holds: real creds
```

A compromised MCP env leaks **relay tokens**, never anyone's DB admin password.

---

## Verified technical facts (grounding)

- `Context.request_context` → `RequestContext` with a `.request` field; in streamable-HTTP that is a
  Starlette `Request`. So a tool can read **connection headers** via
  `ctx.request_context.request.headers` (verified against the deployed `mcp` SDK, 2026-06-19).
- Headers are present on every JSON-RPC POST, including `initialize` — so binding can happen at
  session start and be cached by `session_key(ctx)`.
- FastMCP DNS-rebinding protection (`allowed_hosts`) is bound to loopback — already worked around at
  the Caddy layer (`header_up Host localhost:8765`). Unrelated to this change but note for testing.

---

## Phase 1 — header-passed grant (Claude Code / Desktop)

### Header contract (client → MCP)

Sent as MCP client connection headers (Claude Code `--header`, Desktop `mcp-remote --header`):

| Header | Meaning | Required |
|---|---|---|
| `Authorization: Bearer <front-door>` | front-door auth (existing) | yes |
| `X-NIL-Adapter-Url` | the tenant's adapter base URL | yes |
| `X-NIL-Adapter-Bearer` | bearer the MCP sends to that adapter | if the adapter requires auth |
| `X-NIL-Grant-Id` / `X-NIL-Workspace` / `X-NIL-Scopes` | grant binding (optional; defaults) | no |

> BYO-**adapter-URL**, not BYO-creds. The tenant's PocketBase password lives in *their* adapter.

### Code changes (file-by-file)

1. **`mcp/server.py` — `build_asgi_app`**
   - Replace the closed-over singleton `tools` with a resolver + per-session cache:
     ```python
     _tools_cache: dict[str, NilTools] = {}
     def tools_for(ctx) -> NilTools:
         key = session_key(ctx)
         if key not in _tools_cache:
             t = resolve_tenant(ctx)                      # raises on missing/invalid
             _tools_cache[key] = build_tools(adapter_url=t.adapter_url, bearer=t.bearer,
                                             grant_id=t.grant_id, workspace=t.workspace,
                                             scopes=t.scopes, gate=gate)
         return _tools_cache[key]
     ```
   - Each `nil_*` tool: `await tools_for(ctx).propose(...)` etc.
   - Add eviction on session close (hook MCP session teardown; else LRU/TTL cap to avoid unbounded growth).

2. **`mcp/tenant.py` (new) — `resolve_tenant(ctx)`**
   - `req = ctx.request_context.request`; read the `X-NIL-*` headers; build a `Tenant` dataclass.
   - Validation: require `X-NIL-Adapter-Url`; reject non-https unless `NIL_ALLOW_INSECURE=1`.
   - Single-tenant fallback: if no header AND `NIL_ADAPTER_URL` env set → behave as today (back-compat).

3. **`mcp/server.py` — `_BearerGate`**
   - Keep validating the front-door token. Optionally make the front-door token *per-tenant* later
     (Phase 2). For Phase 1 one shared front-door token + per-tenant adapter headers is fine.

4. **Dynamic `propose_<verb>` tools (`mcp/dynamic.py`)**
   - These are built from **one** adapter's `describe()`, so they don't fit per-tenant. In
     multi-tenant mode register **only the generic `nil_*` tools**; the agent calls `nil_describe`
     to learn each backend's verbs. Gate the static expansion behind `dynamic_tools and not multi_tenant`.

### Tests (TDD, `tests/` in kernel)

- `resolve_tenant`: headers present → correct Tenant; missing url → clear error; insecure url blocked.
- `tools_for`: two different `client_id`s → two `NilTools` with different adapter URLs; same id → cached.
- e2e (httpx mock adapter): two simulated connections with different `X-NIL-Adapter-Url` each
  propose+commit and hit their **own** mock adapter (assert no cross-talk).
- back-compat: no headers + `NIL_ADAPTER_URL` env → behaves exactly as today.

### Effort: ~1–2 days.

---

## Phase 2 — OAuth resource server (claude.ai web)

The web Custom Connector UI only supports OAuth (no custom headers). Layer on top of Phase 1:

- MCP server becomes an OAuth **resource server**; claude.ai runs the auth-code flow against a
  nilscript auth endpoint.
- Issue a token bound to the tenant; a **tenant registry** (DB) maps `token → {adapter_url, bearer, grant}`.
- `resolve_tenant(ctx)` gains an OAuth branch: validate the bearer → look up tenant in the registry.
- Reuses Phase 1's `tools_for` / per-connection plumbing unchanged.

Effort: larger (auth server + registry + token→tenant). Sequence after Phase 1 proves the model.

---

## Interim guidance (until Phase 1 ships)

**One MCP deployment per tenant.** The `mcp` container is small; running one per tenant (each with
its own `PB_*` / `NIL_ADAPTER_URL`) gives full isolation today with zero new code. Multi-tenancy is a
density/UX optimization, not a correctness requirement.

## Risks & rollback

- **Header access shape** differs across MCP SDK versions → pin the SDK; `resolve_tenant` already
  isolated so a shim is one file.
- **Cache growth** → TTL/LRU cap + session-close eviction.
- **Rollback** is trivial: multi-tenant is opt-in (`NIL_MCP_MULTI_TENANT=1`); unset → singleton path.

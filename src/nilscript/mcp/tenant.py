"""Per-connection tenant resolution for the remote NIL-MCP server — pure, MCP-SDK-free, testable.

Multi-tenant means: one shared `mcp.nilscript.org` deployment, but each connecting agent links its
OWN backend by passing its adapter coordinates as connection headers. The kernel stores nothing and
never holds a tenant's backend credentials — the agent points us at THEIR adapter (which holds the
real creds); we only relay the NIL protocol to it with a per-tenant bearer.

This module is duck-typed against the MCP `Context` (`ctx.request_context.request.headers`) so it
imports no `mcp` SDK and is unit-testable with a trivial fake ctx. `server.py` wires it in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Connection headers an agent sends to bind its backend (case-insensitive; Starlette lowercases).
ADAPTER_URL_HEADER = "x-nil-adapter-url"
ADAPTER_BEARER_HEADER = "x-nil-adapter-bearer"
GRANT_ID_HEADER = "x-nil-grant-id"
WORKSPACE_HEADER = "x-nil-workspace"
SCOPES_HEADER = "x-nil-scopes"


class TenantError(ValueError):
    """A connection's tenant binding is missing or invalid (e.g. no adapter URL, or insecure URL)."""


@dataclass(frozen=True)
class Tenant:
    """The backend a single connection is bound to. NEVER carries backend creds — only the adapter
    URL and the bearer used to reach that (tenant-owned) adapter."""

    adapter_url: str
    bearer: str = ""
    grant_id: str = "remote"
    workspace: str = ""
    scopes: frozenset[str] | None = None

    def key(self) -> str:
        """Stable identity for distinctness/caching, independent of the MCP connection object."""
        return f"{self.adapter_url}|{self.grant_id}|{self.workspace}"


def _headers(ctx: Any) -> Any:
    """The connection's HTTP headers via the MCP Context, or None (stdio / no request)."""
    rc = getattr(ctx, "request_context", None)
    req = getattr(rc, "request", None) if rc is not None else None
    return getattr(req, "headers", None)


def _get(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return getter(name) or None


def resolve_tenant(
    ctx: Any,
    *,
    default: Tenant | None = None,
    multi_tenant: bool = False,
    allow_insecure: bool = False,
    registry: Callable[[str], Tenant | None] | None = None,
    saas: bool = False,
    claim_resolver: Callable[[Any], str | None] | None = None,
) -> Tenant:
    """Resolve the backend for this connection.

    Single-tenant (default): always return `default` (back-compat — the env-configured backend).

    SaaS (`saas=True`): the tenant is the AUTHENTICATED identity, never a free header. The
    `claim_resolver` returns the workspace from the verified credential (JWT `workspace` claim /
    keycloak realm); the `X-NIL-Workspace` header may NOT override it, a BYO `X-NIL-Adapter-Url` is
    rejected (identity routes to the tenant's *registered active* adapter), and a missing claim is
    default-deny. This is the isolation spine: a token for tenant A can only ever reach A's backend.

    Multi-tenant (self-hosted / dev, `multi_tenant=True` without `saas`): the connection brings its own
    backend via the `X-NIL-*` headers (BYO), or routes by the `X-NIL-Workspace` header via the registry.
    Header-trust is acceptable here because the deployment is single-owner; it is NOT in SaaS.
    """
    if saas:
        if claim_resolver is None:
            raise TenantError("SaaS mode requires an authenticated-claim resolver")
        claimed = claim_resolver(ctx)
        if not claimed:
            raise TenantError("no authenticated workspace claim — default-deny")
        headers = _headers(ctx)
        header_ws = _get(headers, WORKSPACE_HEADER)
        if header_ws and header_ws != claimed:
            raise TenantError("the workspace header cannot override the authenticated tenant")
        if _get(headers, ADAPTER_URL_HEADER):
            raise TenantError(
                "a BYO adapter-url is not allowed in SaaS mode; identity routes to the tenant's "
                "registered active adapter"
            )
        if registry is None:
            raise TenantError("SaaS mode requires the control-plane registry")
        resolved = registry(claimed)
        if resolved is None:
            raise TenantError(f"workspace '{claimed}' has no active adapter")
        return resolved

    if not multi_tenant:
        if default is None:
            raise TenantError("single-tenant mode requires a default backend (set NIL_ADAPTER_URL)")
        return default

    headers = _headers(ctx)
    adapter_url = _get(headers, ADAPTER_URL_HEADER)
    if not adapter_url:
        # This connection's workspace is its header, or — for a single-owner deployment where agents
        # don't send one — the server's default workspace, so registry routing still applies.
        workspace = _get(headers, WORKSPACE_HEADER) or (default.workspace if default else "")
        if registry is not None and workspace:
            resolved = registry(workspace)
            if resolved is not None:
                return resolved
        if default is not None:
            return default
        raise TenantError(
            f"multi-tenant mode requires the {ADAPTER_URL_HEADER} header (the tenant's adapter URL)"
        )
    if not (adapter_url.startswith("https://") or allow_insecure):
        raise TenantError(
            f"{ADAPTER_URL_HEADER} must be https:// (set NIL_ALLOW_INSECURE=1 to permit http)"
        )

    scopes_raw = _get(headers, SCOPES_HEADER)
    scopes = (
        frozenset(s.strip() for s in scopes_raw.split(",") if s.strip()) if scopes_raw else None
    )
    return Tenant(
        adapter_url=adapter_url,
        bearer=_get(headers, ADAPTER_BEARER_HEADER) or "",
        grant_id=_get(headers, GRANT_ID_HEADER) or "remote",
        workspace=_get(headers, WORKSPACE_HEADER) or "",
        scopes=scopes,
    )

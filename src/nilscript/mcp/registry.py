"""Resolve a workspace's *active* adapter from the control-plane registry.

`tenant.resolve_tenant` is a pure function; this module supplies the I/O callable it can be handed so
that a header-less multi-tenant connection routes to whichever adapter the workspace has activated
(via `GET /adapters/active` on the control plane). Kept separate so resolution stays unit-testable.

The lookup is best-effort: any failure (registry down, unauthorized, no active adapter, bad JSON)
returns None, and `resolve_tenant` falls back to the env default — the MCP never hard-fails because
the registry blipped.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from nilscript.mcp.tenant import Tenant


def make_registry_lookup(
    registry_url: str | None = None,
    token: str | None = None,
    *,
    timeout: float = 3.0,
) -> Callable[[str], Tenant | None] | None:
    """Build the workspace→Tenant lookup the MCP gives to `resolve_tenant`, or None if no registry
    is configured (so the server stays purely header-driven). `registry_url` defaults to
    `NIL_REGISTRY_URL`, `token` to `NIL_REGISTRY_TOKEN` (sent as a bearer to read the adapter creds).
    """
    registry_url = registry_url if registry_url is not None else os.environ.get("NIL_REGISTRY_URL", "")
    token = token if token is not None else os.environ.get("NIL_REGISTRY_TOKEN", "")
    if not registry_url:
        return None
    base = registry_url.rstrip("/")

    def lookup(workspace: str) -> Tenant | None:
        url = f"{base}/adapters/active?workspace={urllib.parse.quote(workspace)}"
        req = urllib.request.Request(url)  # noqa: S310 - fixed https control-plane URL, not user input
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            return None
        adapter = (data or {}).get("adapter") or {}
        adapter_url = adapter.get("url")
        if not adapter_url:
            return None
        return Tenant(
            adapter_url=adapter_url,
            bearer=adapter.get("bearer", "") or "",
            grant_id=adapter.get("adapter_id", "remote") or "remote",
            workspace=workspace,
        )

    return lookup

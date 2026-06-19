"""Production ASGI entrypoint for the remote NIL-MCP server (streamable-HTTP).

Deploy behind nilscript.org with any ASGI server:

    NIL_ADAPTER_URL=https://your-adapter NIL_GRANT_SECRET=… \
        uvicorn nilscript.mcp.app:app --host 0.0.0.0 --port 8765

Clients connect to  https://nilscript.org/mcp  (a Custom Connector / remote MCP server).
Configuration is env-only (never code): the server holds the bearer secret; agents never see it.
"""

from __future__ import annotations

import os

from nilscript.mcp.server import build_asgi_app


def create_app():  # type: ignore[no-untyped-def]
    try:
        adapter_url = os.environ["NIL_ADAPTER_URL"]
    except KeyError as exc:  # fail loud at boot, not on first request
        raise RuntimeError(
            "NIL_ADAPTER_URL is required (the base URL of a running NIL adapter)"
        ) from exc

    scopes_raw = os.environ.get("NIL_GRANT_SCOPES", "")
    return build_asgi_app(
        adapter_url=adapter_url,
        grant_id=os.environ.get("NIL_GRANT_ID", "remote"),
        workspace=os.environ.get("NIL_WORKSPACE", ""),
        bearer=os.environ.get("NIL_GRANT_SECRET", ""),
        scopes=frozenset(scopes_raw.split(",")) if scopes_raw else None,
        gate=os.environ.get("NIL_MCP_GATE", "two-step"),
        dynamic_tools=os.environ.get("NIL_MCP_DYNAMIC", "1") != "0",
        # Front-door bearer for the /mcp endpoint. STRONGLY recommended for a public URL: without it
        # anyone who can reach the URL can drive the backend (within the grant). /healthz stays open.
        auth_token=os.environ.get("NIL_MCP_AUTH_TOKEN") or None,
        # Multi-tenant: each connection links its own backend via X-NIL-Adapter-Url headers; the
        # NIL_ADAPTER_URL above becomes the fallback for header-less connections.
        multi_tenant=os.environ.get("NIL_MCP_MULTI_TENANT", "0") != "0",
        allow_insecure=os.environ.get("NIL_ALLOW_INSECURE", "0") != "0",
    )


app = create_app()

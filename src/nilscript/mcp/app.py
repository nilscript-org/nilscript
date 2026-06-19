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
    )


app = create_app()

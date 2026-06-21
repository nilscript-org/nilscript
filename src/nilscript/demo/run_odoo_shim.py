"""Boot the NIL shim backed by LIVE Odoo CRM (XML-RPC External API).

Mirrors run_live_shim.py for the Odoo adapter. Credentials come from env (set per-session by the
playground's /api/odoo link, or from the host .env). Serves on :8101 so it doesn't collide with the
in-memory (:8099) or PocketBase (:8100) shims. Reflects commits to the control plane when configured.
"""

from __future__ import annotations

import os

from odoo_crm_nil_adapter.edge import CapturingEmitter, HttpEventEmitter, create_app
from odoo_crm_nil_adapter.system import RealSystemClient

ODOO_URL = os.environ.get("ODOO_URL", "")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_LOGIN = os.environ.get("ODOO_LOGIN", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")
NIL_BEARER = os.environ.get("NIL_BEARER", "secret123")
NIL_EVENTS_WEBHOOK = os.environ.get("NIL_EVENTS_WEBHOOK", "")
NIL_EVENTS_SECRET = os.environ.get("NIL_EVENTS_SECRET", "")
NIL_EVENTS_SOURCE = os.environ.get("NIL_EVENTS_SOURCE", "playground")


def _emitter():
    """Reflect every commit to the control plane when a webhook is configured; else in-memory only."""
    if NIL_EVENTS_WEBHOOK:
        return HttpEventEmitter(NIL_EVENTS_WEBHOOK, NIL_EVENTS_SECRET, source=NIL_EVENTS_SOURCE)
    return CapturingEmitter()


def build_odoo_app():
    client = RealSystemClient(ODOO_URL, db=ODOO_DB, login=ODOO_LOGIN, api_key=ODOO_API_KEY)
    return create_app(client, _emitter(), bearer=NIL_BEARER)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_odoo_app(), host="127.0.0.1", port=8101)

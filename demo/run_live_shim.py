"""Boot the NIL shim backed by the LIVE PocketBase demo (not FakeSystem).

Writes committed through this shim land in the real demo's collections. Auth + creds
come from env (public-demo defaults filled in only because this is the open demo).
Serves on :8100 so it doesn't collide with the in-memory shim on :8099.
"""

from __future__ import annotations

import os
import threading
import time

import httpx

from pocketbase_nil_adapter.edge import CapturingEmitter, create_app
from pocketbase_nil_adapter.system import PocketBaseClient

PB_URL = os.environ.get("PB_URL", "https://pocketbase.io")
PB_EMAIL = os.environ.get("PB_EMAIL", "test@example.com")
PB_PASSWORD = os.environ.get("PB_PASSWORD", "123456")
NIL_BEARER = os.environ.get("NIL_BEARER", "secret123")
UI_TRACE = os.environ.get("UI_TRACE_URL", "http://127.0.0.1:8770/api/trace")


def _install_backend_tracing() -> None:
    """Push every PocketBase HTTP call this shim makes to the UI's trace ingest, so the
    deepest level (shim -> PocketBase) shows up in the SEQRD-PC trace alongside the rest."""
    guard = threading.local()
    orig = httpx.Client.send

    def send(self, request, **kw):
        t0 = time.monotonic()
        resp = orig(self, request, **kw)
        url = request.url
        if not getattr(guard, "busy", False) and "/api/trace" not in str(url):
            guard.busy = True
            try:
                httpx.post(UI_TRACE, timeout=2.0, json={
                    "perf": "HTTP", "level": "backend",
                    "title": f"{request.method} {url.host}{url.path} → {resp.status_code}",
                    "detail": {"url": str(url), "status": resp.status_code,
                               "ms": round((time.monotonic() - t0) * 1000)},
                    "status": "err" if resp.status_code >= 400 else "ok"})
            except Exception:  # noqa: BLE001 — tracing must never break the shim
                pass
            finally:
                guard.busy = False
        return resp

    httpx.Client.send = send


_install_backend_tracing()


def build_live_app():
    client = PocketBaseClient(PB_URL, admin_email=PB_EMAIL, admin_password=PB_PASSWORD)
    return create_app(client, CapturingEmitter(), bearer=NIL_BEARER)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_live_app(), host="127.0.0.1", port=8100)

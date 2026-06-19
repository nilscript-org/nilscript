"""Control-plane ASGI app — ingest NIL events (HMAC-verified), query, and a live single-pane UI.

    uvicorn nilscript.controlplane.app:app --host 0.0.0.0 --port 8088

Adapters POST their EVENT envelopes to /events/ingest (HttpEventEmitter → NIL_EVENTS_WEBHOOK), signed
with NIL_EVENTS_SECRET. The UI at / shows every action across all agents in one timeline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nilscript.controlplane.store import EventStore


def create_app(store: EventStore | None = None, *, secret: str | None = None) -> FastAPI:
    store = store if store is not None else EventStore()
    secret = secret if secret is not None else os.environ.get("NIL_EVENTS_SECRET", "")
    app = FastAPI(title="nilscript control plane", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "events": store.count()}

    @app.post("/events/ingest")
    async def ingest(
        request: Request,
        x_nil_signature: str | None = Header(default=None),
        x_nil_sequence: str | None = Header(default=None),
        x_nil_source: str | None = Header(default=None),
    ) -> Any:
        raw = await request.body()
        if secret:
            expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            if not x_nil_signature or not hmac.compare_digest(x_nil_signature, expected):
                return JSONResponse({"error": "bad signature"}, status_code=401)
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad json"}, status_code=400)
        seq = int(x_nil_sequence) if (x_nil_sequence and x_nil_sequence.lstrip("-").isdigit()) else None
        new = store.ingest(envelope, seq, source=x_nil_source or "mcp")
        return {"ok": True, "new": new}

    @app.get("/api/events")
    def events(limit: int = 100) -> dict[str, Any]:
        return {"events": store.recent(limit)}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    return app


_INDEX_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>nilscript · control plane</title>
<style>
  :root{--bg:#0b0d10;--fg:#e8eaed;--mut:#8b949e;--line:#1c2128;--ok:#3fb950;--hi:#d29922;--crit:#f85149}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
  header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px}
  h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.02em}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .sub{color:var(--mut)}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:9px 22px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--mut);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  td.verb{color:#79c0ff}
  .pill{padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line)}
  .ex{color:var(--ok);border-color:var(--ok)} .pr{color:#79c0ff;border-color:#1f6feb}
  .re{color:var(--crit);border-color:var(--crit)} .ro{color:var(--hi);border-color:var(--hi)}
  .tier-HIGH{color:var(--hi)} .tier-CRITICAL{color:var(--crit)}
  .src{color:var(--mut)} .empty{padding:40px 22px;color:var(--mut)}
</style></head><body>
<header><span class=dot></span><h1>nilscript · control plane</h1>
  <span class=sub id=meta>— one pane for every agent action</span></header>
<table><thead><tr><th>time</th><th>source</th><th>event</th><th>verb</th><th>tier</th>
  <th>proposal</th><th>workspace</th></tr></thead><tbody id=rows></tbody></table>
<div class=empty id=empty>waiting for events…</div>
<script>
const cls={executed:'ex',proposed:'pr',refused:'re',rolled_back:'ro'};
async function tick(){
  try{
    const r=await fetch('/api/events?limit=200');const {events}=await r.json();
    const tb=document.getElementById('rows');const em=document.getElementById('empty');
    document.getElementById('meta').textContent='— '+events.length+' recent actions';
    em.style.display=events.length?'none':'block';
    tb.innerHTML=events.map(e=>{
      const t=(e.received_at||'').replace('T',' ').slice(11,19);
      const ev=e.event||e.performative||'';const c=cls[ev]||'pr';
      return `<tr><td>${t}</td><td class=src>${e.source||''}</td>`+
        `<td><span class="pill ${c}">${ev}</span></td>`+
        `<td class=verb>${e.verb||''}</td>`+
        `<td class="tier-${e.tier||''}">${e.tier||''}</td>`+
        `<td class=src>${(e.proposal||'').slice(0,10)}</td>`+
        `<td class=src>${e.workspace||''}</td></tr>`;
    }).join('');
  }catch(_){}
}
tick();setInterval(tick,2000);
</script></body></html>"""


try:  # pragma: no cover - server entrypoint; prod mounts CP_DB_PATH's dir (e.g. /data volume)
    app = create_app()
except OSError:
    # No writable store dir at import (e.g. local test import without /data). The server process
    # in production constructs this successfully because the volume is mounted before boot.
    app = None  # type: ignore[assignment]

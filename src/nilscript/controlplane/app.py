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


def _redact(adapter: dict[str, Any]) -> dict[str, Any]:
    """A registry record safe to hand to the browser: the bearer (which reaches the adapter) is
    masked to a presence flag, never the value."""
    if not adapter:
        return {}
    bearer = adapter.get("bearer")
    return {**adapter, "bearer": "***" if bearer else ""}


def create_app(
    store: EventStore | None = None, *,
    secret: str | None = None,
    registry_token: str | None = None,
) -> FastAPI:
    store = store if store is not None else EventStore()
    secret = secret if secret is not None else os.environ.get("NIL_EVENTS_SECRET", "")
    registry_token = (
        registry_token if registry_token is not None
        else os.environ.get("NIL_REGISTRY_TOKEN", "")
    )
    app = FastAPI(title="nilscript control plane", version="0.1.0")

    def _registry_authed(authorization: str | None) -> bool:
        """Guard the registry's sensitive endpoints. Open when no token is configured (local/test);
        otherwise require `Authorization: Bearer <NIL_REGISTRY_TOKEN>`."""
        if not registry_token:
            return True
        return bool(authorization) and hmac.compare_digest(authorization, f"Bearer {registry_token}")

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

    # ── human-approval gate (Phase 2) ────────────────────────────────────────────────────────
    @app.post("/proposals/{proposal_id}/await")
    def await_approval(proposal_id: str) -> dict[str, Any]:
        """Called by the gate when it holds a proposal for owner approval."""
        return store.await_approval(proposal_id)

    @app.get("/proposals/{proposal_id}/decision")
    def get_decision(proposal_id: str) -> dict[str, Any]:
        """Polled by the gate before it commits a held proposal."""
        return {"proposal_id": proposal_id, "status": store.decision(proposal_id)}

    @app.post("/proposals/{proposal_id}/decision")
    async def post_decision(proposal_id: str, request: Request) -> Any:
        """Owner approves/rejects from the UI."""
        body = {}
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        status = body.get("status")
        if status not in ("approved", "rejected"):
            return JSONResponse({"error": "status must be 'approved' or 'rejected'"}, status_code=400)
        ok = store.decide(proposal_id, status, actor=body.get("actor", "owner"), reason=body.get("reason", ""))
        return {"ok": ok, "proposal_id": proposal_id, "status": store.decision(proposal_id)}

    @app.get("/api/pending")
    def pending() -> dict[str, Any]:
        return {"pending": store.pending()}

    @app.get("/api/adapters")
    def adapters() -> dict[str, Any]:
        return {"adapters": store.adapters()}

    # ── active-adapter registry (multi-tenant routing) ───────────────────────────────────────
    @app.post("/adapters/register")
    async def register_adapter(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Register/refresh an adapter the MCP can route to (auth-protected — carries a bearer)."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad json"}, status_code=400)
        ws, aid, url = body.get("workspace", "") or "", body.get("adapter_id"), body.get("url")
        if not aid or not url:
            return JSONResponse({"error": "adapter_id and url are required"}, status_code=400)
        rec = store.register_adapter(
            ws, aid, label=body.get("label", "") or "", url=url,
            bearer=body.get("bearer", "") or "", system=body.get("system", "") or "",
        )
        return {"ok": True, "adapter": _redact(rec)}

    @app.post("/adapters/{workspace}/{adapter_id}/activate")
    def activate_adapter(workspace: str, adapter_id: str, authorization: str | None = Header(default=None)) -> Any:
        """Make this adapter the active backend for the workspace (auth-protected)."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not store.activate_adapter(workspace, adapter_id):
            return JSONResponse({"error": "no such adapter"}, status_code=404)
        return {"ok": True, "workspace": workspace, "adapter_id": adapter_id}

    @app.get("/adapters")
    def list_adapters(workspace: str = "") -> dict[str, Any]:
        """List a workspace's registered adapters for the UI — bearer REDACTED (public read)."""
        return {"adapters": [_redact(a) for a in store.list_adapters(workspace)]}

    @app.get("/api/registry")
    def registry_view() -> dict[str, Any]:
        """Read-only registry view for the PUBLIC control-plane page: which adapter the MCP routes to
        for the owner workspace, bearer REDACTED. No write controls live in the browser — activation
        is operator-only via `nilscript adapters activate` (token never reaches the client)."""
        ws = os.environ.get("NIL_WORKSPACE", "")
        return {"workspace": ws, "adapters": [_redact(a) for a in store.list_adapters(ws)]}

    @app.get("/adapters/active")
    def get_active_adapter(workspace: str = "", authorization: str | None = Header(default=None)) -> Any:
        """The workspace's active adapter WITH bearer — for the MCP to route. Auth-protected."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        active = store.active_adapter(workspace)
        if active is None:
            return JSONResponse({"error": "no active adapter"}, status_code=404)
        return {"adapter": active}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    return app


_INDEX_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>nilscript · control plane</title>
<script>(function(){try{var t=localStorage.getItem('cp-theme')||(window.matchMedia&&matchMedia('(prefers-color-scheme:light)').matches?'light':'dark');document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();</script>
<style>
  :root{
    --blue:#5b8cff;--green:#46c266;--amber:#e0a629;--red:#fb5a4e;--violet:#a877f7;
    --radius:14px;--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  }
  :root,:root[data-theme=dark]{
    --bg:#090b0f;--panel:#0f131a;--elev:#141a23;--line:#1d2530;--line2:#283142;
    --fg:#e7eaf0;--mut:#8b94a6;--faint:#5a6373;
    --header-bg:rgba(9,11,15,.82);--verb:#9ec5ff;--verb-strong:#cfe0ff;--rowhover:rgba(91,140,255,.05)}
  :root[data-theme=light]{
    --bg:#f5f7fa;--panel:#ffffff;--elev:#eef1f5;--line:#e4e8ee;--line2:#d6dde6;
    --fg:#1b2230;--mut:#586374;--faint:#97a1b1;
    --blue:#2f6bff;--green:#1f9e44;--amber:#a9760f;--red:#db3a2f;--violet:#7a45d3;
    --header-bg:rgba(255,255,255,.85);--verb:#2f5bd0;--verb-strong:#2247b8;--rowhover:rgba(47,107,255,.06)}
  @media(prefers-color-scheme:light){:root:not([data-theme]){
    --bg:#f5f7fa;--panel:#ffffff;--elev:#eef1f5;--line:#e4e8ee;--line2:#d6dde6;
    --fg:#1b2230;--mut:#586374;--faint:#97a1b1;
    --blue:#2f6bff;--green:#1f9e44;--amber:#a9760f;--red:#db3a2f;--violet:#7a45d3;
    --header-bg:rgba(255,255,255,.85);--verb:#2f5bd0;--verb-strong:#2247b8;--rowhover:rgba(47,107,255,.06)}}
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:
      radial-gradient(1200px 600px at 80% -10%,rgba(91,140,255,.10),transparent 60%),
      radial-gradient(900px 500px at -10% 0%,rgba(168,119,247,.08),transparent 55%),var(--bg);
    color:var(--fg);font:14px/1.55 var(--mono);min-height:100vh;
    -webkit-font-smoothing:antialiased}
  a{color:inherit}

  /* ── header ── */
  header{position:sticky;top:0;z-index:20;backdrop-filter:blur(10px);
    background:var(--header-bg);
    border-bottom:1px solid var(--line);padding:14px clamp(14px,4vw,30px);
    display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .brand{display:flex;align-items:center;gap:10px;font-weight:600;letter-spacing:.02em}
  .brand b{font-weight:600}.brand .sl{color:var(--faint);font-weight:400}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--green);
    box-shadow:0 0 0 0 rgba(70,194,102,.6);animation:pulse 2.4s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(70,194,102,.55)}70%{box-shadow:0 0 0 7px rgba(70,194,102,0)}100%{box-shadow:0 0 0 0 rgba(70,194,102,0)}}
  .grow{flex:1 1 auto}
  .chip{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border:1px solid var(--line2);
    border-radius:999px;color:var(--mut);font-size:12px;white-space:nowrap}
  .chip b{color:var(--fg)}
  .live{color:var(--faint);font-size:12px;display:inline-flex;align-items:center;gap:6px}
  .live i{width:5px;height:5px;border-radius:50%;background:var(--green);display:inline-block}

  main{padding:clamp(14px,3vw,26px);max-width:1280px;margin:0 auto}
  .sec-title{display:flex;align-items:center;gap:9px;margin:6px 2px 12px;
    color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.09em}
  .sec-title .n{color:var(--fg);background:var(--elev);border:1px solid var(--line2);
    border-radius:999px;padding:1px 8px;font-size:11px}

  /* ── pending approvals ── */
  #pendingWrap{margin-bottom:26px;display:none}
  #pending{display:grid;gap:12px}
  .pcard{position:relative;border:1px solid var(--line2);border-radius:var(--radius);
    background:linear-gradient(180deg,rgba(224,166,41,.10),rgba(224,166,41,.02)),var(--panel);
    padding:16px 18px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;
    box-shadow:0 1px 0 rgba(255,255,255,.02) inset,0 10px 30px -18px rgba(0,0,0,.8)}
  .pcard::before{content:"";position:absolute;left:0;top:14px;bottom:14px;width:3px;border-radius:3px;background:var(--amber)}
  .pcard .body{flex:1 1 260px;min-width:0}
  .pcard .verb{font-size:15px;font-weight:600;color:var(--verb-strong);word-break:break-word}
  .pcard .prev{color:var(--mut);font-size:13px;margin-top:3px;word-break:break-word}
  .pcard .meta{color:var(--faint);font-size:11.5px;margin-top:6px}
  .pcard .actions{display:flex;gap:9px;flex:0 0 auto}

  .btn{appearance:none;font:inherit;font-size:13px;cursor:pointer;border-radius:10px;
    padding:9px 16px;border:1px solid var(--line2);background:var(--elev);color:var(--fg);
    transition:transform .06s ease,filter .15s ease,background .15s ease;display:inline-flex;
    align-items:center;gap:7px;white-space:nowrap}
  .btn:hover{filter:brightness(1.15)}.btn:active{transform:translateY(1px)}
  .btn.ok{border-color:rgba(70,194,102,.5);color:#bdf0cb;background:rgba(70,194,102,.12)}
  .btn.no{border-color:rgba(251,90,78,.5);color:#ffc7c2;background:rgba(251,90,78,.10)}
  .btn.ghost{background:transparent;color:var(--mut)}
  .btn.ghost:hover{color:var(--fg);border-color:var(--line2)}
  .btn.tiny{padding:5px 10px;font-size:12px;border-radius:8px}

  /* ── timeline (responsive: table on desktop, cards on mobile) ── */
  .feed{border:1px solid var(--line);border-radius:var(--radius);overflow-x:auto;background:var(--panel)}
  .feed-head,.row{display:grid;grid-template-columns:78px 80px 120px minmax(150px,1.4fr) 84px 116px minmax(80px,1fr) 104px;
    align-items:center;gap:12px;padding:8px clamp(12px,2vw,16px);min-width:760px}
  .feed-head{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.08em;
    border-bottom:1px solid var(--line);background:var(--elev)}
  .row{border-bottom:1px solid var(--line);transition:background .12s ease}
  .row:last-child{border-bottom:none}
  .row:hover{background:var(--rowhover)}
  .t{color:var(--faint)} .src{color:var(--mut)} .ws{color:var(--faint)}
  .verbcell{color:var(--verb);font-weight:500;word-break:break-word}
  .pid{color:var(--faint);font-size:12px}
  .ev{justify-self:start}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;
    font-size:12px;border:1px solid var(--line2);white-space:nowrap}
  .pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.9}
  .ev-executed{color:var(--green);border-color:rgba(70,194,102,.35);background:rgba(70,194,102,.08)}
  .ev-proposed{color:var(--blue);border-color:rgba(91,140,255,.35);background:rgba(91,140,255,.08)}
  .ev-refused{color:var(--red);border-color:rgba(251,90,78,.35);background:rgba(251,90,78,.08)}
  .ev-rolled_back{color:var(--amber);border-color:rgba(224,166,41,.35);background:rgba(224,166,41,.08)}
  .tier{font-size:11px;padding:1px 8px;border-radius:6px;border:1px solid var(--line2);color:var(--mut)}
  .tier.HIGH{color:#f0c674;border-color:rgba(224,166,41,.45);background:rgba(224,166,41,.07)}
  .tier.CRITICAL{color:#ff9a90;border-color:rgba(251,90,78,.5);background:rgba(251,90,78,.10)}
  .tier.MEDIUM{color:#9ec5ff;border-color:rgba(91,140,255,.3)}
  .rowact{justify-self:end}
  .rev{color:var(--violet);font-size:11px}

  .empty{padding:54px 22px;text-align:center;color:var(--faint)}
  .empty .big{font-size:15px;color:var(--mut);margin-bottom:4px}

  /* ── mcp routing (read-only registry) ── */
  #routingWrap{margin-bottom:26px;display:none}
  #routing{display:grid;gap:10px}
  .rrow{display:flex;align-items:center;gap:12px;flex-wrap:wrap;border:1px solid var(--line);
    border-radius:var(--radius);background:var(--panel);padding:11px 15px}
  .rrow.on{border-color:rgba(70,194,102,.45);background:linear-gradient(180deg,rgba(70,194,102,.07),transparent)}
  .rrow .nm{font-weight:600;color:var(--verb)}
  .rrow .sys{color:var(--mut);font-size:12px;border:1px solid var(--line2);border-radius:6px;padding:1px 7px}
  .rrow .host{color:var(--faint);font-size:12px}
  .rrow .grow{flex:1 1 auto}
  .rbadge{font-size:11px;padding:2px 10px;border-radius:999px;border:1px solid var(--line2);color:var(--mut)}
  .rbadge.on{color:var(--green);border-color:rgba(70,194,102,.45);background:rgba(70,194,102,.08)}
  #routing code,.sec-title code{font-size:11.5px;color:var(--verb);background:var(--elev);
    border:1px solid var(--line2);border-radius:5px;padding:1px 6px}

  /* ── adapters ── */
  #adaptersWrap{margin-bottom:26px;display:none}
  #adapters{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
  .acard{border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);
    padding:14px 16px;position:relative;overflow:hidden}
  .acard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--green)}
  .acard.stale::before{background:var(--faint)}
  .acard .nm{font-weight:600;color:var(--verb);font-size:14.5px;display:flex;align-items:center;gap:8px}
  .acard .nm .live{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 7px var(--green)}
  .acard.stale .nm .live{background:var(--faint);box-shadow:none}
  .acard .ns{margin:9px 0 7px;display:flex;flex-wrap:wrap;gap:5px}
  .acard .ns span{font-size:11px;color:var(--mut);border:1px solid var(--line2);border-radius:6px;padding:1px 7px}
  .acard .st{display:flex;gap:12px;color:var(--mut);font-size:12px;flex-wrap:wrap}
  .acard .st b{color:var(--fg)}
  .acard .ch{color:var(--faint);font-size:11px;margin-top:6px}

  /* toast */
  #toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);
    background:var(--elev);border:1px solid var(--line2);color:var(--fg);padding:11px 16px;
    border-radius:11px;font-size:13px;opacity:0;pointer-events:none;transition:.25s;z-index:50;
    box-shadow:0 18px 40px -16px rgba(0,0,0,.9)}
  #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  #toast b{color:var(--violet)}

  /* Narrow screens keep the compact table (records) and scroll horizontally — never big cards. */
  @media(max-width:760px){
    main{padding:12px}
    .feed-head,.row{gap:10px;padding:8px 12px}
  }
</style></head><body>
<header>
  <span class=brand><span class=dot></span><b>nilscript</b><span class=sl>· control plane</span></span>
  <span class=grow></span>
  <span class=chip id=count><b>0</b>&nbsp;actions</span>
  <span class=live><i></i> live</span>
  <button class="btn tiny ghost" id=themeBtn title="Toggle light / dark" aria-label="Toggle theme" onclick=toggleTheme()>☾</button>
</header>

<main>
  <section id=pendingWrap>
    <div class=sec-title>⏳ Awaiting your approval <span class=n id=pcount>0</span>
      <span style="color:var(--faint);text-transform:none;letter-spacing:0">— nothing commits until you decide</span></div>
    <div id=pending></div>
  </section>

  <section id=routingWrap>
    <div class=sec-title>MCP routing <span class=n id=rcount>0</span>
      <span style="color:var(--faint);text-transform:none;letter-spacing:0">— the active backend agents reach via the hosted MCP (switch with <code>nilscript adapters activate</code>)</span></div>
    <div id=routing></div>
  </section>

  <section id=adaptersWrap>
    <div class=sec-title>Adapters <span class=n id=acount>0</span>
      <span style="color:var(--faint);text-transform:none;letter-spacing:0">— backends linked &amp; live, derived from the stream</span></div>
    <div id=adapters></div>
  </section>

  <div class=sec-title>Activity <span style="color:var(--faint);text-transform:none;letter-spacing:0">— every agent action, one pane</span></div>
  <div class=feed>
    <div class=feed-head>
      <span>time</span><span>source</span><span>event</span><span>verb</span>
      <span>tier</span><span>proposal</span><span>workspace</span><span style=justify-self:end>action</span>
    </div>
    <div id=rows></div>
    <div class=empty id=empty><div class=big>Waiting for events…</div>
      <div>Agent actions will stream in here as they happen.</div></div>
  </div>
</main>

<div id=toast></div>

<script>
const EV={executed:'ev-executed',proposed:'ev-proposed',refused:'ev-refused',rolled_back:'ev-rolled_back'};
const REVERSIBLE=new Set(['REVERSIBLE','COMPENSABLE']);
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hhmmss(iso){const t=(iso||'').replace('T',' ');return t.slice(11,19)||'—';}
function toast(html){const t=document.getElementById('toast');t.innerHTML=html;t.classList.add('show');
  clearTimeout(toast._);toast._=setTimeout(()=>t.classList.remove('show'),2600);}

async function copyRollback(token){
  try{await navigator.clipboard.writeText(token);}catch(e){
    const ta=document.createElement('textarea');ta.value=token;document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');}catch(_){}ta.remove();}
  toast('Rollback token copied — run <b>nil_rollback</b> with it in your agent to reverse this.');
}

async function tick(){
  try{
    const r=await fetch('/api/events?limit=200');const {events}=await r.json();
    const rows=document.getElementById('rows'),empty=document.getElementById('empty');
    document.getElementById('count').innerHTML='<b>'+events.length+'</b>&nbsp;actions';
    empty.style.display=events.length?'none':'block';
    rows.innerHTML=events.map(e=>{
      const ev=e.event||e.performative||'';const cls=EV[ev]||'ev-proposed';
      const canRoll=ev==='executed'&&e.compensation_token&&REVERSIBLE.has(e.reversibility);
      const tier=e.tier?`<span class="tier ${esc(e.tier)}">${esc(e.tier)}</span>`:'';
      const act=canRoll
        ? `<button class="btn tiny ghost" title="Reversible (${esc(e.reversibility)})" onclick="copyRollback('${esc(e.compensation_token)}')">⤺ rollback</button>`
        : (e.reversibility?`<span class=rev title="${esc(e.reversibility)}">${e.reversibility==='IRREVERSIBLE'?'— final':''}</span>`:'');
      return `<div class=row>
        <span class=t data-l=time title="${esc(e.received_at)}">${hhmmss(e.received_at)}</span>
        <span class=src data-l=source>${esc(e.source||'')}</span>
        <span class=ev data-l=event><span class="pill ${cls}">${esc(ev)}</span></span>
        <span class=verbcell data-l=verb>${esc(e.verb||'')}</span>
        <span data-l=tier>${tier}</span>
        <span class=pid data-l=proposal>${esc((e.proposal||'').slice(0,10))}</span>
        <span class=ws data-l=workspace>${esc(e.workspace||'—')}</span>
        <span class=rowact data-l=action>${act}</span>
      </div>`;
    }).join('');
  }catch(_){}
}

async function decide(id,status){
  try{
    await fetch('/proposals/'+id+'/decision',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
    toast(status==='approved'?'Approved — the commit will proceed.':'Rejected — the write will not commit.');
  }catch(_){toast('Could not record the decision.');}
  pend();tick();
}

async function pend(){
  try{
    const r=await fetch('/api/pending');const {pending}=await r.json();
    const wrap=document.getElementById('pendingWrap'),sec=document.getElementById('pending');
    document.getElementById('pcount').textContent=pending.length;
    wrap.style.display=pending.length?'block':'none';
    sec.innerHTML=pending.map(p=>{
      let prev='';try{const o=JSON.parse(p.preview);prev=(o&&(o.en||o.ar))||'';}catch(_){prev=p.preview||'';}
      const tier=p.tier?`<span class="tier ${esc(p.tier)}">${esc(p.tier)}</span>`:'';
      return `<div class=pcard>
        <div class=body>
          <div class=verb>${esc(p.verb||'action')} ${tier}</div>
          ${prev?`<div class=prev>${esc(prev)}</div>`:''}
          <div class=meta>proposal ${esc((p.proposal_id||'').slice(0,12))}</div>
        </div>
        <div class=actions>
          <button class="btn ok" onclick="decide('${esc(p.proposal_id)}','approved')">✓ Approve</button>
          <button class="btn no" onclick="decide('${esc(p.proposal_id)}','rejected')">✕ Reject</button>
        </div>
      </div>`;
    }).join('');
  }catch(_){}
}

function ago(iso){if(!iso)return '—';var t=new Date(iso+(/[zZ]|[+-]\\d\\d:?\\d\\d$/.test(iso)?'':'Z')).getTime();
  var s=Math.max(0,(Date.now()-t)/1000);
  return s<60?Math.floor(s)+'s ago':s<3600?Math.floor(s/60)+'m ago':Math.floor(s/3600)+'h ago';}
async function loadAdapters(){
 try{
  const r=await fetch('/api/adapters');const {adapters}=await r.json();
  const wrap=document.getElementById('adaptersWrap'),box=document.getElementById('adapters');
  document.getElementById('acount').textContent=adapters.length;
  wrap.style.display=adapters.length?'block':'none';
  box.innerHTML=adapters.map(a=>{
   const t=new Date((a.last_seen||'')+(/[zZ]$/.test(a.last_seen||'')?'':'Z')).getTime();
   const stale=(Date.now()-t)>120000;
   const ns=(a.namespaces||[]).map(n=>`<span>${esc(n)}.*</span>`).join('')||'<span style=opacity:.6>—</span>';
   const by=a.by_event||{}, ex=by.executed||0, pr=by.proposed||0, rf=by.refused||0;
   return `<div class="acard ${stale?'stale':''}">
     <div class=nm><span class=live></span>${esc(a.system||a.adapter)}</div>
     <div class=ns>${ns}</div>
     <div class=st><span><b>${a.events}</b> events</span><span><b>${ex}</b> exec</span><span><b>${pr}</b> prop</span>${rf?`<span><b>${rf}</b> refused</span>`:''}</div>
     <div class=ch>via ${esc((a.sources||[]).join(', ')||'—')} · ${esc(ago(a.last_seen))}</div>
   </div>`;
  }).join('');
 }catch(_){}
}
function host(u){try{return new URL(u).host;}catch(e){return u||'';}}
async function loadRouting(){
 try{
  const r=await fetch('/api/registry');const {adapters}=await r.json();
  const wrap=document.getElementById('routingWrap'),box=document.getElementById('routing');
  document.getElementById('rcount').textContent=adapters.length;
  wrap.style.display=adapters.length?'block':'none';
  box.innerHTML=adapters.map(a=>{
   const on=!!a.active;
   return `<div class="rrow ${on?'on':''}">
     <span class=nm>${esc(a.label||a.adapter_id)}</span>
     <span class=sys>${esc(a.system||a.adapter_id)}</span>
     <span class=host>${esc(host(a.url))}</span>
     <span class=grow></span>
     <span class="rbadge ${on?'on':''}">${on?'● active':'idle'}</span>
   </div>`;
  }).join('');
 }catch(_){}
}
function applyThemeGlyph(){var b=document.getElementById('themeBtn');if(b)b.textContent=document.documentElement.getAttribute('data-theme')==='light'?'☀':'☾';}
function toggleTheme(){var next=document.documentElement.getAttribute('data-theme')==='light'?'dark':'light';document.documentElement.setAttribute('data-theme',next);try{localStorage.setItem('cp-theme',next);}catch(e){}applyThemeGlyph();}
tick();pend();loadAdapters();loadRouting();applyThemeGlyph();setInterval(()=>{tick();pend();loadAdapters();loadRouting();},2000);
</script></body></html>"""


try:  # pragma: no cover - server entrypoint; prod mounts CP_DB_PATH's dir (e.g. /data volume)
    app = create_app()
except OSError:
    # No writable store dir at import (e.g. local test import without /data). The server process
    # in production constructs this successfully because the volume is mounted before boot.
    app = None  # type: ignore[assignment]

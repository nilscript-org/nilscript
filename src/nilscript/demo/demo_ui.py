"""nilscript demo UI — launch the kernel + a chat UI in one command.

    python demo_ui.py        # boots the shim(s) and serves the chat UI on http://127.0.0.1:8080

What you get:
  * a chat box that drives an agent through the NIL kernel (propose -> confirm -> commit),
  * a live "adapter connected" indicator (pings the running shim),
  * a backend toggle: in-memory FakeSystem (:8099) or live PocketBase (:8100),
  * a settings panel to plug in ANY OpenAI-compatible LLM (base URL + key + model).
    No key? The chat falls back to a tiny rule-based brain so the demo still works.

Deliberately minimal: one file, in-memory config, no database, no build step.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from importlib import resources

import httpx
import litellm
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from nilscript.sdk import GrantRef, NilClient, NilTransport, handshake
from nilscript.sdk.breaker import CircuitBreaker
from nilscript.sdk.idempotency import nil_uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BEARER = "secret123"  # shims booted by this launcher use this bearer; grant secret matches

TARGETS = {
    "memory": {"label": "in-memory (FakeSystem)", "url": "http://127.0.0.1:8099", "script": "run_auth_shim.py"},
    "live": {"label": "live PocketBase", "url": "http://127.0.0.1:8100", "script": "run_live_shim.py"},
}

# Live PocketBase credentials — editable at runtime via the Backend panel, so you can
# re-link after the public demo purges, or point at your own persistent instance.
LIVE_CREDS: dict[str, str] = {
    "url": os.environ.get("PB_URL", "https://pocketbase.io"),
    "identity": os.environ.get("PB_IDENTITY", os.environ.get("PB_EMAIL", "test@example.com")),
    "password": os.environ.get("PB_PASSWORD", "123456"),
}
LIVE_PROC: subprocess.Popen | None = None  # the live shim subprocess (respawnable)

# Runtime config. `provider` is a LiteLLM provider key (e.g. "openai", "anthropic", "ollama").
CONFIG: dict[str, str] = {"provider": "", "model": "", "api_key": "", "api_base": ""}

# Public-instance hardening (release plan §4). When PLAYGROUND_PUBLIC=1 (set by the hosted
# /playground deploy), the demo runs locked down: no settings are persisted to disk (the LLM key
# lives only in process memory for the session, never written), the default backend is the
# in-memory sandbox, and /api/chat + /api/commit are rate-limited. Local/dev runs leave it off and
# keep the convenient persist-across-restarts behaviour.
#
# NOTE: this process still holds a single shared CONFIG, so the hosted instance is intended to run
# one session at a time (or one container per session). True multi-tenant isolation (per-session
# CONFIG keyed by a cookie) is the next hardening step before a high-traffic public deployment.
PLAYGROUND_PUBLIC = os.environ.get("PLAYGROUND_PUBLIC", "") not in ("", "0", "false", "False")

# Persist settings (provider + backend creds) to a local file so they survive restarts.
STATE_FILE = os.environ.get("UI_STATE", os.path.join(HERE, ".nil-ui-state.json"))


def save_state() -> None:
    if PLAYGROUND_PUBLIC:  # session-only: never write the LLM key / creds to disk in public mode
        return
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"config": CONFIG, "live_creds": LIVE_CREDS}, f, indent=2)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        pass


def load_state() -> None:
    if PLAYGROUND_PUBLIC:  # start from a clean sandbox; no persisted key/creds carried in
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        CONFIG.update({k: v for k, v in data.get("config", {}).items() if k in CONFIG})
        live = dict(data.get("live_creds", {}))
        if "email" in live and "identity" not in live:  # migrate old persisted key
            live["identity"] = live.pop("email")
        LIVE_CREDS.update({k: v for k, v in live.items() if k in LIVE_CREDS})
    except (FileNotFoundError, ValueError):
        pass


load_state()  # restore provider + backend creds from a prior run (no-op in public mode)


# Token-bucket rate limiter (public mode only) — keyed by client IP, refills over time. Keeps a
# public Playground from being turned into a free LLM/relay proxy or a write-flood against a backend.
_RL_CAPACITY = int(os.environ.get("PLAYGROUND_RL_CAPACITY", "20"))  # burst
_RL_REFILL_PER_SEC = float(os.environ.get("PLAYGROUND_RL_REFILL", "0.5"))  # ~30/min sustained
_RL_BUCKETS: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last_ts)


def _rate_limit_ok(ip: str) -> bool:
    if not PLAYGROUND_PUBLIC:
        return True
    now = time.monotonic()
    tokens, last = _RL_BUCKETS.get(ip, (float(_RL_CAPACITY), now))
    tokens = min(_RL_CAPACITY, tokens + (now - last) * _RL_REFILL_PER_SEC)
    if tokens < 1.0:
        _RL_BUCKETS[ip] = (tokens, now)
        return False
    _RL_BUCKETS[ip] = (tokens - 1.0, now)
    return True

# Backend skeleton snapshot per target: {target: handshake report incl targets{name:{exists,fields}}}.
# Populated at connect and refreshed lazily; injected into the agent prompt so it knows the shape.
SKELETON: dict[str, dict] = {}

litellm.drop_params = True  # silently drop params a given provider doesn't support


# Most-used providers float to the top of the picker; everything else follows alphabetically.
POPULAR_PROVIDERS = [
    "openai", "anthropic", "gemini", "vertex_ai", "azure", "bedrock", "groq", "mistral",
    "xai", "deepseek", "openrouter", "ollama", "together_ai", "fireworks_ai", "perplexity",
    "cohere", "cerebras", "databricks",
]


def build_catalog() -> dict[str, list[str]]:
    """provider -> sorted chat models, straight from LiteLLM's bundled model catalog.
    Ordered popularity-first (POPULAR_PROVIDERS), then the rest alphabetically."""
    catalog: dict[str, set[str]] = {}
    for name, info in litellm.model_cost.items():
        if not isinstance(info, dict) or info.get("mode") != "chat":
            continue
        provider = info.get("litellm_provider")
        if not provider:
            continue
        catalog.setdefault(provider, set()).add(name)
    ordered: dict[str, list[str]] = {}
    for p in POPULAR_PROVIDERS:  # popular first, in this curated order
        if catalog.get(p):
            ordered[p] = sorted(catalog[p])
    for p in sorted(catalog):  # then everything else, alphabetical
        if p not in ordered and catalog[p]:
            ordered[p] = sorted(catalog[p])
    return ordered


CATALOG = build_catalog()

# Observability: one human-readable log of every turn — LLM decision, proposals, commits.
LOG_FILE = os.environ.get("UI_LOG", "/tmp/nil-ui.log")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger("nil-ui")

# Trace buffer: each request's journey through the SEQRD-PC performatives
# (STATUS·EVENT·QUERY·ROLLBACK·DECIDE·PROPOSE·COMMIT). The Trace drawer polls /api/events.
EVENTS: list[dict] = []

# Commit history (audit trail tied to the SSOT) — each reversible write keeps its compensation
# token so the UI can offer a Rollback button. In-memory, newest last.
HISTORY: list[dict] = []


def emit_event(perf: str, title: str, detail: dict | None = None,
               status: str = "ok", level: str = "agent") -> None:
    # level: agent | llm | wire (NIL HTTP) | backend (PocketBase HTTP)
    EVENTS.append({"seq": len(EVENTS), "ts": datetime.now(UTC).strftime("%H:%M:%S"),
                   "perf": perf, "title": title, "detail": detail or {}, "status": status, "level": level})
    if len(EVENTS) > 800:
        del EVENTS[:300]
        for i, e in enumerate(EVENTS):
            e["seq"] = i


def _classify_http(url) -> tuple[str, str]:
    """Map an outbound URL to a (performative, level) for the trace."""
    p = url.path
    if "/nil/v0.1/propose" in p: return "PROPOSE", "wire"
    if "/nil/v0.1/commit" in p: return "COMMIT", "wire"
    if "/nil/v0.1/status" in p: return "STATUS", "wire"
    if "/nil/v0.1/query" in p: return "QUERY", "wire"
    if "/nil/v0.1/rollback" in p: return "ROLLBACK", "wire"
    if "/chat/completions" in p or p.endswith("/messages"): return "LLM", "llm"
    if "/api/" in p: return "HTTP", "backend"  # PocketBase REST
    return "HTTP", "wire"


def _safe_payload(raw) -> object:
    """Parse a request/response body to JSON for the trace; fall back to truncated text."""
    if raw is None or raw == b"" or raw == "":
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return s[:2000]


def _trace_http(request, response, status, ms, err=None) -> None:
    url = request.url
    if str(url).endswith("probeprobe"):  # skip the health-probe noise
        return
    perf, level = _classify_http(url)
    bad = bool(err) or (status is not None and status >= 400)
    req_payload = _safe_payload(getattr(request, "content", None))   # the intent / call payload sent
    resp_payload = None
    if response is not None:
        try:
            resp_payload = _safe_payload(response.text)              # the server's reply payload
        except Exception:  # noqa: BLE001
            resp_payload = None
    emit_event(perf, f"{request.method} {url.host}{url.path} → {status if status is not None else 'ERR'}",
               {"url": str(url), "status": status, "ms": ms, "error": err,
                "request": req_payload, "response": resp_payload},
               status="err" if bad else "ok", level=level)


def _install_httpx_tracing() -> None:
    """Patch httpx so EVERY outbound call in this process is traced — covers the NIL wire
    calls (propose/commit/status to the shim), PocketBase credential calls, and the LLM call.
    Captures the FULL request + response payloads for the trace drawer."""
    _orig_async = httpx.AsyncClient.send
    _orig_sync = httpx.Client.send

    async def async_send(self, request, **kw):
        t0 = time.monotonic()
        try:
            r = await _orig_async(self, request, **kw)
            await r.aread()  # ensure the body is available for the trace
            _trace_http(request, r, r.status_code, round((time.monotonic() - t0) * 1000))
            return r
        except Exception as exc:  # noqa: BLE001
            _trace_http(request, None, None, round((time.monotonic() - t0) * 1000), err=type(exc).__name__)
            raise

    def sync_send(self, request, **kw):
        t0 = time.monotonic()
        try:
            r = _orig_sync(self, request, **kw)
            r.read()  # ensure the body is available for the trace
            _trace_http(request, r, r.status_code, round((time.monotonic() - t0) * 1000))
            return r
        except Exception as exc:  # noqa: BLE001
            _trace_http(request, None, None, round((time.monotonic() - t0) * 1000), err=type(exc).__name__)
            raise

    httpx.AsyncClient.send = async_send
    httpx.Client.send = sync_send


_install_httpx_tracing()





# --------------------------------------------------------------------------------------------
# Verb catalog: the kernel's profiles become both the agent's tools and the rule-based lexicon.
# --------------------------------------------------------------------------------------------

def load_verbs() -> dict[str, dict]:
    verbs: dict[str, dict] = {}
    spec = resources.files("nilscript.sdk").joinpath("spec/0.1/profiles")
    for profile_dir in spec.iterdir():
        if not profile_dir.is_dir():
            continue
        family = profile_dir.name.replace("-v1", "")
        for f in profile_dir.iterdir():
            if f.name.endswith(".json") and ".response" not in f.name:
                verbs[f"{family}.{f.name[:-5]}"] = json.loads(f.read_text())
    return verbs


VERBS = load_verbs()


def openai_tools() -> list[dict]:
    """Each verb profile -> an OpenAI-compatible function tool. Dots aren't allowed in tool
    names, so `commerce.create_product` becomes `commerce__create_product` (mapped back later)."""
    tools = []
    for verb, schema in VERBS.items():
        params = {
            "type": "object",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
            "additionalProperties": False,
        }
        tools.append({
            "type": "function",
            "function": {
                "name": verb.replace(".", "__"),
                "description": schema.get("title", verb),
                "parameters": params,
            },
        })
    return tools  # resource.* tools come from the standard resource-v1 profiles, like every other verb


# --------------------------------------------------------------------------------------------
# The agent brain: message -> a decision (chat reply, or a verb+args to propose).
# --------------------------------------------------------------------------------------------

def rule_based(message: str) -> tuple[str, str, dict] | tuple[str, str]:
    """No-LLM fallback. Handles the obvious create-product phrasing; otherwise nudges to config."""
    g = message.lower()
    if "product" in g and "delete" not in g:
        name = "Untitled product"
        if "called" in g:
            name = message.split("called", 1)[-1].split(" for ")[0].strip().strip("\"'") or name
        args: dict = {"name": name}
        for tok in g.replace(",", " ").split():
            try:
                args["price"] = float(tok)
                break
            except ValueError:
                continue
        return ("actions", [("commerce.create_product", args)])
    return ("message", "No LLM provider set — the built-in rule-based brain only understands a single "
                       "'create a product called X for N'. Pick a provider in the Provider panel for full "
                       "natural language and multi-item requests like 'create orange 44 and apple 55'.")


def _skeleton_block(target: str) -> str:
    """A compact readiness summary (the backend skeleton) injected into the LLM prompt so the
    agent KNOWS which native targets exist + their fields before it proposes."""
    skel = SKELETON.get(target)
    targets = (skel or {}).get("targets") or {}
    if not targets:
        return ""
    lines = []
    for name, t in targets.items():
        if t.get("exists"):
            # full capability per field: name:type plus '!' when required — so the agent knows exactly
            fields = ", ".join(
                f"{f.get('name')}:{f.get('type')}" + ("!" if f.get("required") else "")
                for f in (t.get("fields") or [])
            )
            lines.append(f"  {name} ✓" + (f" — {fields}" if fields else " — (no declared fields)"))
        else:
            lines.append(f"  {name} ✗ not provisioned")
    return ("\n\nBackend capabilities (" + (skel.get("system") or target) + ") — '!' = required field:\n"
            + "\n".join(lines)
            + "\nUse only existing targets/fields. If a target needed for the request is ✗, do NOT propose "
              "it blindly — tell the user it isn't set up and offer to create it first.")


async def llm_decide(message: str, target: str = "memory") -> tuple[str, str, dict] | tuple[str, str]:
    """One LiteLLM call (any of 100+ providers) with the verb catalog as tools.
    LiteLLM normalises tool-calling to the OpenAI shape across every provider."""
    system = (
        "You are an operations agent that acts on a business backend through NIL verbs. "
        "When the user asks to perform actions, call the matching tool(s) — ONE call per distinct "
        "action, so 'create 3 products' means three separate tool calls with each item's own args. "
        "Extract each item's fields precisely (e.g. its name and price). Ignore filler/thinking-aloud. "
        "For UPDATING or DELETING an existing record when no semantic verb fits (e.g. changing a coupon), "
        "use resource__update / resource__delete with {target, id, data}; the backend resolves a human "
        "value (code/name) passed as `id`, so you may pass the code the user mentioned. "
        "To list or look records up in realtime use resource__read {target, match}. "
        "The target must be one shown below; never invent a record id. "
        "If no action is needed, just reply briefly in plain text. Never invent ids."
        + _skeleton_block(target)
    )
    kwargs: dict = {
        "model": CONFIG["model"],
        "custom_llm_provider": CONFIG["provider"] or None,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": message}],
        "tools": openai_tools(),
        "tool_choice": "auto",
        "temperature": 0,
    }
    if CONFIG["api_key"]:
        kwargs["api_key"] = CONFIG["api_key"]
    if CONFIG["api_base"]:
        kwargs["api_base"] = CONFIG["api_base"]
    log.info("LLM call -> %s/%s", CONFIG["provider"], CONFIG["model"])
    try:
        resp = await litellm.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001 — surface provider/auth errors in the chat
        log.warning("LLM error: %s: %s", type(exc).__name__, str(exc)[:200])
        return ("message", f"LLM provider error: {type(exc).__name__}: {str(exc)[:300]}")
    msg = resp.choices[0].message
    calls = getattr(msg, "tool_calls", None) or []
    log.info("LLM returned %d tool call(s); text=%r", len(calls), (msg.content or "")[:120])
    actions = []
    for call in calls:
        fn = call.function
        try:
            args = json.loads(fn.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        actions.append((fn.name.replace("__", "."), args))
    if actions:
        return ("actions", actions)
    return ("message", msg.content or "(no response)")


# --------------------------------------------------------------------------------------------
# NIL plumbing: talk to the selected shim through the kernel SDK.
# --------------------------------------------------------------------------------------------

def _client(target: str) -> tuple[NilClient, NilTransport]:
    base = TARGETS[target]["url"]
    grant = GrantRef.from_secret(grant_id="demo-grant", workspace="demo-ws", secret=BEARER,
                                 scopes=frozenset({"commerce.*", "services.*"}))
    transport = NilTransport(base_url=base, bearer_secret=BEARER, breaker=CircuitBreaker())
    return NilClient(transport=transport, grant=grant), transport


async def nil_propose(target: str, verb: str, args: dict) -> dict:
    client, transport = _client(target)
    try:
        ts = datetime.now(UTC)
        p = await client.propose(verb, args, session_id="ui-" + uuid.uuid4().hex[:8], request_timestamp=ts)
        if p.outcome == "refusal":
            return {"outcome": "refusal", "code": p.code, "message": p.message, "field": p.field}
        return {"outcome": "proposal", "id": p.id, "verb": p.verb, "tier": p.tier,
                "preview": p.preview or {}, "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                "ts": ts.isoformat()}
    except Exception as exc:  # noqa: BLE001 — never let a malformed shim answer 500 the UI
        return {"outcome": "refusal", "code": "MALFORMED", "message": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        await transport.aclose()


async def nil_commit(target: str, proposal_id: str, ts_iso: str) -> dict:
    client, transport = _client(target)
    try:
        idem = nil_uuid("ui-commit", ts_iso, 0)
        outcome = await client.commit(proposal_id, idempotency_key=idem)
        return {"state": getattr(outcome, "state", None), "replayed": getattr(outcome, "replayed", None),
                "result": getattr(outcome, "result", None)}  # SSOT entity + system-of-record
    finally:
        await transport.aclose()


async def nil_rollback(target: str, token: str) -> dict:
    """Standard ROLLBACK: ask the shim for the compensating proposal, then COMMIT it to reverse."""
    grant = GrantRef.from_secret(grant_id="demo-grant", workspace="demo-ws", secret=BEARER,
                                 scopes=frozenset({"commerce.*", "services.*", "resource.*"}))
    transport = NilTransport(base_url=TARGETS[target]["url"], bearer_secret=BEARER, breaker=CircuitBreaker())
    client = NilClient(transport=transport, grant=grant)
    try:
        env = {"nil": "0.1", "grant": "demo-grant", "workspace": "demo-ws",
               "body": {"compensation_token": token, "reason": "user rollback from history"}}
        ans = await transport.post_sentence("/nil/v0.1/rollback", env)
        body = ans.get("body", {})
        if body.get("outcome") != "proposal":
            return {"ok": False, "error": f"{body.get('code')}: {body.get('message')}"}
        ts = datetime.now(UTC)
        outcome = await client.commit(body["id"], idempotency_key=nil_uuid("ui-rollback", ts.isoformat() + token, 0))
        state = getattr(outcome, "state", None)
        result = {"ok": state == "executed", "state": state, "verb": body.get("verb")}
        if state != "executed":  # surface WHY (e.g. the backend rejected the compensating write)
            result["error"] = f"compensation did not execute (state={state})"
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        await transport.aclose()


async def nil_query(target: str, args: dict) -> dict:
    """Run resource.read against the selected backend — returns {target, count, items}."""
    client, transport = _client(target)
    try:
        return await client.query("resource.read", args)
    except Exception as exc:  # noqa: BLE001
        return {"target": args.get("target"), "count": 0, "items": [], "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        await transport.aclose()


async def shim_reachable(target: str) -> bool:
    url = TARGETS[target]["url"] + "/nil/v0.1/status/probeprobe"
    try:
        async with httpx.AsyncClient(timeout=2.0) as http:
            await http.get(url)  # any HTTP status = server is up; only a transport error means down
        return True
    except httpx.RequestError:
        return False


# --------------------------------------------------------------------------------------------
# Launcher: bring up the shim(s) before serving, so the UI has something to connect to.
# --------------------------------------------------------------------------------------------

# Every collection the adapter's verbs write to, with the union of fields each verb sends.
# All fields are optional (the shim enforces NIL-required args) so creates never 400 on a
# missing optional. Numeric fields get PocketBase type "number"; everything else is "text".
_NUMERIC = {"price", "quantity", "amount", "discount_value", "min_amount"}
COLLECTIONS = {
    "products": ["name", "description", "price", "sku", "quantity", "category"],
    "coupons": ["code", "discount_type", "discount_value", "min_amount", "expires_at"],
    "refunds": ["refund_target", "amount", "reason"],
    "fulfillments": ["order_id", "event", "tracking", "occurred_at"],
    "payments": ["order_id", "event", "amount", "currency", "method", "reference"],
    "messages": ["phone", "text"],
    "clients": ["name", "phone", "email", "notes"],
    "invoices": ["party_id", "amount", "currency", "description"],
    "payment_links": ["invoice_id", "expires_at"],
    "proposals": ["party_id", "title", "amount", "currency", "body", "biz_proposal_id", "channel"],
    "followups": ["party_id", "message", "channel"],
}


def verify_creds(creds: dict[str, str]) -> tuple[bool, str]:
    """Auth against the given PocketBase and ensure EVERY collection the adapter writes to
    exists (so all verbs work, not just create_product). Returns (ok, message)."""
    try:
        with httpx.Client(base_url=creds["url"].rstrip("/"), timeout=15.0) as http:
            r = http.post("/api/collections/_superusers/auth-with-password",
                          json={"identity": creds["identity"], "password": creds["password"]})
            if r.status_code >= 400:
                return False, f"auth failed ({r.status_code})"
            hdr = {"Authorization": r.json().get("token")}
            created = 0
            for name, fields in COLLECTIONS.items():
                if http.get(f"/api/collections/{name}", headers=hdr).status_code == 200:
                    continue
                schema = [{"name": f, "type": "number" if f in _NUMERIC else "text"} for f in fields]
                c = http.post("/api/collections", headers=hdr, json={
                    "name": name, "type": "base", "fields": schema,
                    "listRule": "", "viewRule": "", "createRule": "", "updateRule": "", "deleteRule": ""})
                if c.status_code >= 400:
                    return False, f"auth ok, but couldn't create '{name}' ({c.status_code})"
                created += 1
            n = len(COLLECTIONS)
            return True, (f"linked — provisioned {created} new of {n} collections" if created
                          else f"linked — all {n} collections present")
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot reach {creds['url']}: {type(exc).__name__}"


def spawn_live(*, restart: bool = False) -> tuple[bool, str]:
    """(Re)start the live shim against LIVE_CREDS. Verifies creds + ensures the collection first."""
    global LIVE_PROC
    ok, msg = verify_creds(LIVE_CREDS)
    if not ok:
        return False, msg
    if restart and LIVE_PROC and LIVE_PROC.poll() is None:
        LIVE_PROC.terminate()
        try:
            LIVE_PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            LIVE_PROC.kill()
    url = TARGETS["live"]["url"]
    if restart:  # wait for the old port to free
        for _ in range(20):
            if not _port_up(url):
                break
            time.sleep(0.25)
    env = {**os.environ, "NIL_BEARER": BEARER,
           "PB_URL": LIVE_CREDS["url"], "PB_EMAIL": LIVE_CREDS["identity"], "PB_PASSWORD": LIVE_CREDS["password"]}
    shim_log = open("/tmp/nil-live-shim.log", "w")  # noqa: SIM115
    LIVE_PROC = subprocess.Popen([sys.executable, os.path.join(HERE, TARGETS["live"]["script"])],
                                 cwd=HERE, env=env, stdout=shim_log, stderr=shim_log)
    for _ in range(40):
        if _port_up(url):
            return True, msg
        time.sleep(0.25)
    return False, "shim did not come up"


def _port_up(url: str) -> bool:
    try:
        httpx.get(url + "/nil/v0.1/status/probeprobe", timeout=1.5)
        return True
    except httpx.RequestError:
        return False


def launch_shims() -> None:
    # in-memory shim (always available)
    t = TARGETS["memory"]
    if _port_up(t["url"]):
        print(f"  [launcher] memory shim already up at {t['url']}")
    else:
        shim_log = open("/tmp/nil-memory-shim.log", "w")  # noqa: SIM115
        log.info("spawning memory shim -> /tmp/nil-memory-shim.log")
        subprocess.Popen([sys.executable, os.path.join(HERE, t["script"])],
                         cwd=HERE, env={**os.environ, "NIL_BEARER": BEARER},
                         stdout=shim_log, stderr=shim_log)
        for _ in range(40):
            if _port_up(t["url"]):
                break
            time.sleep(0.25)
    # live shim (best-effort against LIVE_CREDS)
    if _port_up(TARGETS["live"]["url"]):
        print(f"  [launcher] live shim already up at {TARGETS['live']['url']}")
    else:
        ok, msg = spawn_live()
        print(f"  [launcher] live shim: {msg}" if ok else f"  [launcher] live shim NOT up: {msg}")


async def connect_checks(target: str) -> list[dict]:
    """The visible connection handshake — driven entirely by the kernel SDK's `handshake()`,
    which speaks only NIL (`/nil/v0.1/describe`). No backend-specific code here: any conformant
    adapter reports its own verbs + per-target readiness, so this works universally."""
    transport = NilTransport(base_url=TARGETS[target]["url"], bearer_secret=BEARER, breaker=CircuitBreaker())
    try:
        rep = await handshake(transport)
    finally:
        await transport.aclose()
    SKELETON[target] = rep  # snapshot the skeleton for the agent prompt

    steps: list[dict] = []

    def record(step: str, ok: bool, detail: str) -> None:
        steps.append({"step": step, "ok": ok, "detail": detail})
        emit_event("STATUS", f"connect · {step}: {'ok' if ok else 'FAILED'}",
                   {"target": target, "detail": detail}, status="ok" if ok else "err", level="connect")

    record("reachable", rep["reachable"], TARGETS[target]["url"])
    if not rep["reachable"]:
        return steps
    record("conformant", rep["conformant"], f"NIL {rep.get('nil')} · {len(rep.get('verbs', []))} verbs · {rep.get('system')}")
    targets = rep.get("targets", {})
    missing = rep.get("missing", [])
    detail = f"{len(rep.get('ready', []))}/{len(targets)} native targets ready"
    if missing:
        detail += " — missing: " + ", ".join(missing[:5])
    record("provisioned", not missing, detail)
    return steps


# --------------------------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------------------------

app = FastAPI(title="nilscript demo UI")


class ChatIn(BaseModel):
    message: str
    target: str = "memory"


class CommitIn(BaseModel):
    proposal_id: str
    ts: str
    target: str = "memory"


class ConfigIn(BaseModel):
    provider: str = ""
    model: str = ""
    api_key: str = ""
    api_base: str = ""


class BackendIn(BaseModel):
    url: str = ""
    identity: str = ""  # email / username / record id — whatever your backend auths with
    password: str = ""


class TraceIn(BaseModel):
    perf: str = "HTTP"
    title: str = ""
    detail: dict = {}
    status: str = "ok"
    level: str = "backend"


def _llm_ready() -> bool:
    return bool(CONFIG["provider"] and CONFIG["model"])


@app.get("/api/health")
async def health():
    return {t: await shim_reachable(t) for t in TARGETS}


@app.get("/api/meta")
async def meta():
    return {"targets": {k: v["label"] for k, v in TARGETS.items()},
            "llm_configured": _llm_ready(),
            "provider": CONFIG["provider"], "model": CONFIG["model"],
            "public": PLAYGROUND_PUBLIC, "default_target": "memory",
            "verbs": sorted(VERBS)}


@app.get("/api/providers")
async def providers():
    """The scroll-through catalog: every provider LiteLLM knows + its chat models."""
    return CATALOG


@app.get("/api/events")
async def events(after: int = -1):
    """SEQRD-PC trace events newer than the client's cursor (`after`)."""
    fresh = [e for e in EVENTS if e["seq"] > after]
    return {"events": fresh, "last": EVENTS[-1]["seq"] if EVENTS else -1}


@app.post("/api/trace")
async def ingest_trace(t: TraceIn):
    """Trace ingest — the live shim pushes its backend (PocketBase) HTTP calls here."""
    emit_event(t.perf, t.title, t.detail, t.status, t.level)
    return {"ok": True}


@app.post("/api/config")
async def set_config(cfg: ConfigIn):
    CONFIG.update(provider=cfg.provider.strip(), model=cfg.model.strip(),
                  api_key=cfg.api_key.strip(), api_base=cfg.api_base.strip())
    save_state()
    return {"llm_configured": _llm_ready()}


@app.get("/api/backend")
async def get_backend():
    """Current live-PocketBase link (password never returned)."""
    return {"url": LIVE_CREDS["url"], "identity": LIVE_CREDS["identity"], "connected": _port_up(TARGETS["live"]["url"])}


@app.post("/api/backend")
async def set_backend(creds: BackendIn):
    """Re-link the live backend. Blank fields keep the current value (so you can change
    just the email, just the password, or the URL). The new creds are VERIFIED before we
    touch the running shim — a bad change reports the error and leaves the current link intact."""
    candidate = {
        "url": creds.url.strip() or LIVE_CREDS["url"],
        "identity": creds.identity.strip() or LIVE_CREDS["identity"],
        "password": creds.password if creds.password != "" else LIVE_CREDS["password"],
    }
    log.info("BACKEND relink attempt -> %s as %s", candidate["url"], candidate["identity"])
    emit_event("STATUS", f"re-linking {candidate['url']} as {candidate['identity']}", {})
    ok, msg = verify_creds(candidate)
    if not ok:  # don't disturb the working shim on a bad change
        log.warning("BACKEND relink rejected: %s", msg)
        emit_event("STATUS", "re-link rejected (current link kept)", {"message": msg}, status="err")
        return {"ok": False, "message": msg, "url": LIVE_CREDS["url"], "identity": LIVE_CREDS["identity"],
                "connected": _port_up(TARGETS["live"]["url"])}
    LIVE_CREDS.update(candidate)  # only commit verified creds
    save_state()
    ok2, msg2 = spawn_live(restart=True)
    steps = await connect_checks("live") if ok2 else []
    emit_event("STATUS", f"backend {'linked' if ok2 else 'link failed'}", {"message": msg2},
               status="ok" if ok2 else "err", level="connect")
    return {"ok": ok2, "message": msg2 if ok2 else f"verified, but {msg2}",
            "url": LIVE_CREDS["url"], "identity": LIVE_CREDS["identity"],
            "connected": _port_up(TARGETS["live"]["url"]), "steps": steps,
            "skeleton": (SKELETON.get("live") or {}).get("targets", {})}  # full capabilities, expandable


@app.post("/api/chat")
async def chat(inp: ChatIn, request: Request):
    if not _rate_limit_ok(request.client.host if request.client else "?"):
        emit_event("DECIDE", "rate limited", {"message": inp.message}, status="warn")
        return JSONResponse(
            {"type": "message", "text": "Rate limit reached for this Playground. Give it a moment and try again."},
            status_code=429,
        )
    mode = f"{CONFIG['provider']}/{CONFIG['model']}" if _llm_ready() else "rule-based"
    log.info("CHAT [%s] target=%s msg=%r", mode, inp.target, inp.message)
    emit_event("DECIDE", f"agent deciding ({mode})", {"message": inp.message, "target": inp.target})
    if _llm_ready():
        if inp.target not in SKELETON:  # ensure the agent has the backend skeleton to reason over
            await connect_checks(inp.target)
        decision = await llm_decide(inp.message, inp.target)
    else:
        decision = rule_based(inp.message)
    if decision[0] == "message":
        log.info("  -> reply (no action)")
        emit_event("DECIDE", "agent replied — no action", {"text": decision[1]})
        return {"type": "message", "text": decision[1]}
    emit_event("DECIDE", f"agent chose {len(decision[1])} action(s)",
               {"verbs": [v for v, _ in decision[1]]})
    items = []
    reads = []
    for verb, args in decision[1]:  # decision[1] is a list of (verb, args) — one card each
        if verb == "resource.read":  # a read returns data immediately (no propose/confirm)
            data = await nil_query(inp.target, args)
            log.info("  READ %s -> %s rows", args.get("target"), data.get("count"))
            emit_event("QUERY", f"read {args.get('target')} ({data.get('count')} rows)",
                       {"match": args.get("match"), "count": data.get("count")}, level="wire")
            reads.append(data)
            continue
        proposal = await nil_propose(inp.target, verb, args)
        outcome = proposal.get("outcome")
        log.info("  PROPOSE %s args=%s -> %s", verb, args, outcome)
        emit_event("PROPOSE", f"{verb} → {outcome}",
                   {"args": args, "id": proposal.get("id"), "tier": proposal.get("tier"),
                    "preview": proposal.get("preview"), "code": proposal.get("code"),
                    "message": proposal.get("message")},
                   status="warn" if outcome == "refusal" else "ok")
        if proposal["outcome"] == "refusal":
            items.append({"type": "refusal", "verb": verb, "args": args, **proposal})
        else:
            items.append({"type": "action", "args": args, **proposal})
    return {"type": "actions", "items": items, "reads": reads}


@app.post("/api/commit")
async def commit(inp: CommitIn, request: Request):
    if not _rate_limit_ok(request.client.host if request.client else "?"):
        return JSONResponse({"ok": False, "error": "rate limited — try again shortly"}, status_code=429)
    emit_event("COMMIT", f"commit {inp.proposal_id}", {"target": inp.target})
    try:
        result = await nil_commit(inp.target, inp.proposal_id, inp.ts)
        state = result.get("state")
        log.info("COMMIT target=%s proposal=%s -> state=%s", inp.target, inp.proposal_id, state)
        executed = state == "executed"
        emit_event("STATUS", f"state={state}", {"replayed": result.get("replayed")},
                   status="ok" if executed else "err")
        if executed:  # a real write surfaces as a signed EVENT carrying the SSOT entity
            res = result.get("result") or {}
            ent = res.get("entity") or {}
            ssot = res.get("ssot") or {}
            comp = res.get("compensation") or {}
            emit_event("EVENT", f"wrote {ent.get('type','?')} {ent.get('id','')} @ {ssot.get('system', inp.target)}",
                       {"entity": ent, "ssot": ssot})
            HISTORY.append({  # audit trail entry with a rollback handle
                "seq": len(HISTORY), "ts": datetime.now(UTC).strftime("%H:%M:%S"), "target": inp.target,
                "verb": ent.get("type"), "entity": ent, "system": ssot.get("system"),
                "token": comp.get("token"), "reversibility": comp.get("reversibility", "IRREVERSIBLE"),
                "reversed": False})
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        log.warning("COMMIT failed proposal=%s: %s", inp.proposal_id, exc)
        emit_event("COMMIT", "commit failed", {"error": str(exc)}, status="err")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)


@app.get("/api/history")
async def history():
    """The commit audit trail (newest first) — each entry carries its SSOT entity + rollback handle."""
    return {"history": list(reversed(HISTORY))}


class RollbackIn(BaseModel):
    seq: int
    target: str = "memory"
    token: str = ""


@app.post("/api/rollback")
async def rollback(inp: RollbackIn):
    """Reverse a committed action via the standard ROLLBACK performative (preview → commit reversal)."""
    entry = next((h for h in HISTORY if h["seq"] == inp.seq), None)
    if entry is None or not entry.get("token"):
        return {"ok": False, "error": "no reversible history entry"}
    emit_event("ROLLBACK", f"rollback {entry.get('verb')} {entry.get('entity', {}).get('id', '')}",
               {"token": entry["token"]}, level="wire")
    r = await nil_rollback(entry["target"], entry["token"])
    if r.get("ok"):
        entry["reversed"] = True
        emit_event("STATUS", "rollback executed", {"reversal_verb": r.get("verb")})
    else:
        emit_event("STATUS", "rollback failed", {"error": r.get("error")}, status="err")
    return r


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel=preconnect href="https://fonts.googleapis.com"><link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel=stylesheet>
<title>NILScript · agent console</title>
<style>
 /* Strict monochrome system — matches the NILScript landing (zinc, dark default). */
 :root{--bg:#09090b;--panel:#18181b;--panel2:#27272a;--line:#27272a;--txt:#fafafa;
  --dim:#a1a1aa;--faint:#71717a;--accent:#fafafa;--ok:#fafafa;--bad:#a1a1aa;--warn:#71717a;--r:6px;
  --sans:"Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
 /* Light theme — same monochrome system, ground/ink flipped (matches the landing's .light). */
 body.light{--bg:#ffffff;--panel:#fafafa;--panel2:#f4f4f5;--line:#e4e4e7;--txt:#09090b;
  --dim:#52525b;--faint:#71717a;--accent:#09090b;--ok:#09090b;--bad:#71717a;--warn:#71717a}
 body.light header{background:rgba(255,255,255,.85)}
 body.light ::selection{background:rgba(9,9,11,.12)}
 *{box-sizing:border-box}
 ::selection{background:rgba(250,250,250,.16)}
 *{scrollbar-width:thin;scrollbar-color:var(--panel2) transparent}
 *::-webkit-scrollbar{width:8px;height:8px}*::-webkit-scrollbar-thumb{background:var(--panel2);border-radius:9999px}
 body{margin:0;background:var(--bg);color:var(--dim);font:15px/1.6 var(--sans);height:100vh;overflow:hidden;display:flex;flex-direction:column;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
 header{flex:none}
 header{display:flex;align-items:center;gap:12px;height:56px;padding:0 20px;border-bottom:1px solid var(--line);background:rgba(9,9,11,.85);backdrop-filter:blur(8px);position:sticky;top:0;z-index:40}
 .brand{display:flex;align-items:center;gap:9px;text-decoration:none;color:var(--txt)}
 .mark{font:700 18px var(--mono);letter-spacing:-.5px}.mark i{opacity:.4;font-style:normal}
 .name{font:600 15px var(--sans);letter-spacing:-.2px}
 .gh{display:inline-flex;align-items:center;color:var(--faint);text-decoration:none;transition:color .15s ease}
 .gh:hover{color:var(--txt)}
 .badge{font:11px var(--mono);color:var(--faint);border:1px solid var(--line);background:var(--panel);border-radius:var(--r);padding:2px 7px}
 .grow{flex:1}
 .pill{display:inline-flex;align-items:center;gap:7px;font:12px var(--mono);color:var(--dim);border:1px solid var(--line);border-radius:var(--r);padding:6px 10px;background:var(--panel)}
 .dot{width:8px;height:8px;border-radius:50%;border:1px solid var(--faint);background:transparent}
 .dot.on{background:#22c55e;border-color:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.18)}
 select,button,input{font:inherit;color:var(--txt);background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:7px 10px}
 select{font-size:13px}
 button{cursor:pointer;background:var(--txt);border-color:var(--txt);color:var(--bg);font-weight:600;font-size:13px}
 button.ghost{background:var(--panel);border-color:var(--line);color:var(--dim);font-weight:500}
 button:hover{opacity:.88}button.ghost:hover{background:var(--panel2);color:var(--txt);opacity:1}
 #log{flex:1;overflow:auto;padding:28px 20px;display:flex;flex-direction:column;gap:14px;max-width:760px;width:100%;margin:0 auto}
 .msg{max-width:82%;padding:10px 14px;border-radius:var(--r);white-space:pre-wrap;word-wrap:break-word;font-size:14px}
 .me{align-self:flex-end;background:var(--panel2);color:var(--txt);border:1px solid var(--line)}
 .bot{align-self:flex-start;background:var(--panel);border:1px solid var(--line);color:var(--txt)}
 .sys{align-self:center;color:var(--faint);font:12px var(--mono);text-align:center;max-width:90%}
 .card{align-self:stretch;background:var(--panel);border:1px solid var(--line);border-left:2px solid var(--accent);border-radius:var(--r);padding:14px 16px}
 .card h4{margin:0 0 8px;font:600 12px var(--mono);color:var(--txt);text-transform:uppercase;letter-spacing:.6px}
 .kv{font:12px var(--mono);color:var(--faint);margin:3px 0}.kv b{color:var(--txt);font-weight:500}
 .preview{background:var(--bg);border:1px solid var(--line);border-radius:var(--r);padding:10px 12px;margin:10px 0;font-size:14px;color:var(--txt)}
 .preview .ar{direction:rtl;text-align:right;color:var(--dim);margin-top:4px}
 .row{display:flex;gap:8px;margin-top:12px;align-items:center}
 .tag{font:11px var(--mono);padding:3px 8px;border-radius:var(--r);border:1px solid var(--line);color:var(--faint)}
 .done{border-left-color:var(--ok)}.refused{border-left-color:var(--bad)}.refused h4{color:var(--dim)}
 footer{border-top:1px solid var(--line);background:var(--bg);padding:16px 20px}
 .composer{display:flex;gap:10px;max-width:760px;margin:0 auto}
 .composer input{flex:1;font-size:14px}
 .composer input:focus,select:focus,.pad input:focus{outline:none;border-color:var(--faint)}
 dialog{background:var(--panel);color:var(--txt);border:1px solid var(--line);border-radius:var(--r);padding:0;width:min(460px,92vw)}
 dialog::backdrop{background:rgba(0,0,0,.6)}.dlg{padding:22px}
 .dlg h3{margin:0 0 4px;color:var(--txt);font:600 16px var(--sans)}.dlg p{margin:0 0 16px;color:var(--dim);font-size:13px}
 .dlg p b{color:var(--txt)}
 .pad label{display:block;font:11px var(--mono);color:var(--faint);margin:14px 0 5px;text-transform:uppercase;letter-spacing:.5px}
 .pad label:first-child{margin-top:0}
 .pad input,.pad select{width:100%;display:block}.pad .dim{color:var(--faint);text-transform:none;letter-spacing:0}
 #c_model{font:13px var(--mono)}
 .cstep{display:flex;align-items:baseline;gap:7px;font:12px var(--mono);padding:3px 0}
 .cstep b{color:var(--txt);min-width:88px}.cstep .t{color:var(--faint)}
 .cdot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
 .cdot.on{background:#22c55e}.cdot.bad{background:#f0857a}
 .hrow{border-bottom:1px solid var(--line);padding:9px 16px}.hrow.done{opacity:.6}
 .caps{margin-top:8px;border:1px solid var(--line);border-radius:var(--r);padding:8px 10px;font:12px var(--mono)}
 .caps summary{cursor:pointer;color:var(--dim);user-select:none}
 .cap{margin:8px 0 2px;display:flex;align-items:center;gap:6px}.cap b{color:var(--txt)}
 .flds{flex-basis:100%;display:flex;flex-wrap:wrap;gap:5px;margin:4px 0 4px 14px}
 .fld{border:1px solid var(--line);border-radius:4px;padding:1px 6px;color:var(--txt);background:var(--panel)}
 .fld i{color:var(--faint);font-style:normal}.fld b{color:#e0a35f;margin-left:1px}
 /* Trace drawer */
 .cnt{font:10px var(--mono);background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:0 6px;color:var(--faint)}
 #split{flex:1;display:flex;min-height:0;position:relative;overflow:hidden}
 #main{flex:1;display:flex;flex-direction:column;min-width:0}
 #drawer{width:0;flex:none;background:var(--bg);overflow:hidden;border-left:0 solid var(--line);
  position:relative;transition:width .24s cubic-bezier(.16,1,.3,1)}
 #drawer.open{width:min(var(--draw-w,380px),70vw);border-left-width:1px}
 #drawer.nodrag-trans{transition:none}
 #drawer .inner{width:100%;height:100%;display:flex;flex-direction:column}
 /* Phone view: the sidebar overlays the chat instead of pushing it (no horizontal scroll). */
 @media (max-width:760px){
  header{gap:8px;padding:0 12px}
  .brand .name{display:none}
  #adapter{display:none}
  #drawer{position:absolute;right:0;top:0;bottom:0;z-index:30;box-shadow:-10px 0 30px rgba(0,0,0,.35)}
  #drawer.open{width:min(92vw,var(--draw-w,380px))}
 }
 .grip{position:absolute;left:0;top:0;bottom:0;width:7px;cursor:col-resize;z-index:6}
 .grip:hover,.grip.active{background:linear-gradient(90deg,var(--accent),transparent);opacity:.5}
 .pbody{flex:1;overflow:auto;position:relative}
 .panel{animation:fade-in .2s ease-out}
 .panel.pad{padding:16px}
 .ptext{margin:0 0 14px;color:var(--dim);font-size:13px}.ptext b{color:var(--txt)}
 @keyframes fade-in{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
 .dh{background:var(--panel);border-bottom:1px solid var(--line);padding:9px 16px;font:11px var(--mono);color:var(--faint);text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:10px;flex:none}
 .legend{font-size:10px;color:var(--faint);opacity:.7}
 .ev{border-bottom:1px solid var(--line);padding:7px 16px;font:12px var(--mono);cursor:pointer;display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 8px}
 .ev .ttl{min-width:0;overflow-wrap:anywhere}
 .ev:hover{background:var(--panel)}
 .ev .chev{color:var(--faint);transition:transform .15s;display:inline-block}
 .ev.open .chev{transform:rotate(90deg)}
 .ev .ttl{color:var(--txt);flex:1}.ev .t{color:var(--faint)}
 .perf{display:inline-block;min-width:72px;text-align:center;border-radius:var(--r);padding:1px 6px;font-size:10px;font-weight:700;letter-spacing:.5px;border:1px solid}
 .perf.DECIDE{color:#d4d4d8;border-color:var(--panel2)}
 .perf.PROPOSE{color:#7aa2ff;border-color:#2c3a63}
 .perf.COMMIT{color:#5fd08a;border-color:#234d35}
 .perf.STATUS{color:#a1a1aa;border-color:var(--line)}
 .perf.EVENT{color:#c08cff;border-color:#3d2d63}
 .perf.ROLLBACK{color:#e0a35f;border-color:#5c4321}
 .perf.QUERY{color:#5fd0d0;border-color:#234d4d}
 .perf.LLM{color:#e8b339;border-color:#5c4a1a}
 .perf.HTTP{color:#9aa0aa;border-color:var(--line)}
 .lvl{font-size:9px;color:var(--faint);border:1px solid var(--line);border-radius:4px;padding:0 4px;text-transform:uppercase;letter-spacing:.4px}
 .ev.warn .ttl{color:#e0a35f}.ev.err .ttl{color:#f0857a}
 .det{display:none;flex-basis:100%;margin:6px 0 2px;color:var(--faint);white-space:pre-wrap;word-break:break-word;font-size:11px}
 .ev.open .det{display:block}
</style></head><body>
<header>
 <a class=brand href="https://nilscript.org" target=_blank rel=noopener title="nilscript.org"><span class=mark><i>&lt;</i>nil<i>&gt;</i></span><span class=name>NILScript</span></a>
 <a class=gh href="https://github.com/nilscript-org/nilscript" target=_blank rel=noopener title="GitHub — nilscript-org/nilscript" aria-label="GitHub repository"><svg viewBox="0 0 16 16" width=18 height=18 fill=currentColor aria-hidden=true><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 014 0c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg></a>
 <div class=grow></div>
 <span class=pill><span class=dot id=dot></span><span id=adapter>checking…</span></span>
 <select id=target title="backend the agent acts on"></select>
 <button class=ghost id=trace>Trace <span id=tracecount class=cnt>0</span></button>
 <button class=ghost id=history>History</button>
 <button class=ghost id=backend>Backend</button>
 <button class=ghost id=gear>Provider</button>
 <button class=ghost id=theme title="toggle light/dark">☾</button>
</header>
<div id=split>
 <div id=main>
  <div id=log></div>
  <footer><div class=composer>
   <input id=box placeholder="e.g. create a product called Aurora Lamp for 49.90" autocomplete=off>
   <button id=send>Send</button>
  </div></footer>
 </div>
 <aside id=drawer><div class=grip id=grip></div><div class=inner>
  <div class=dh><span id=panelTitle>SEQRD-PC trace</span><span class=grow></span>
   <button class=ghost id=clearev style="padding:2px 8px">clear</button>
   <button class=ghost id=panelclose style="padding:2px 8px">✕</button></div>
  <div class=pbody>
   <div id=panel-trace class=panel><div id=evlist></div></div>
   <div id=panel-history class=panel hidden><div id=histlist></div></div>

   <div id=panel-provider class="panel pad" hidden>
    <p class=ptext>Powered by <b>LiteLLM</b> — scroll <span id=provcount>…</span> providers. Pick “(none)” for the built-in rule-based brain.</p>
    <label>Provider</label>
    <input id=c_provfilter placeholder="search providers…" style="margin-bottom:6px">
    <select id=c_prov size=6></select>
    <label>Model <span id=modelcount class=dim></span></label>
    <input id=c_modelfilter placeholder="filter models…" style="margin-bottom:6px">
    <select id=c_model size=8></select>
    <label>API key</label><input id=c_key type=password placeholder="provider API key (blank for local)">
    <label>API base URL <span class=dim>(optional — e.g. http://localhost:11434)</span></label>
    <input id=c_base placeholder="leave blank for the provider default">
    <div class=row style=margin-top:16px><button id=c_save>Save</button></div>
   </div>

   <div id=panel-backend class="panel pad" hidden>
    <p class=ptext>Link the <b>live PocketBase</b>. The demo purges hourly — re-link here, or point at your own instance. A <code>products</code> collection is created if missing.</p>
    <label>PocketBase URL</label><input id=b_url placeholder="https://pocketbase.io">
    <label>Superuser identity <span class=dim>(email / username / id)</span></label><input id=b_identity placeholder="test@example.com or KT45bzNA340Xa7L">
    <label>Password</label><input id=b_pass type=password placeholder="123456 (blank = keep current)">
    <div id=b_status class=kv style="margin-top:12px"></div>
    <div class=row style=margin-top:14px><button id=b_save>Link &amp; reconnect</button></div>
   </div>
  </div>
 </div></aside>
</div>

<script>
const $=s=>document.querySelector(s), log=$('#log');
function add(cls,html){const d=document.createElement('div');d.className=cls;d.innerHTML=html;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
function esc(s){return (s??'').toString().replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

async function refreshMeta(){
 const m=await (await fetch('/api/meta')).json();
 const sel=$('#target');sel.innerHTML='';
 for(const[k,v]of Object.entries(m.targets)){const o=document.createElement('option');o.value=k;o.textContent=v;sel.appendChild(o);}
 // Public instance: default to the in-memory sandbox (no real writes) — connecting a backend is opt-in.
 if(m.public){sel.value=m.default_target||'memory';const b=$('#sandboxNote');if(b)b.style.display='';}
}
async function refreshHealth(){
 const h=await (await fetch('/api/health')).json();
 const t=$('#target').value, up=h[t];
 $('#dot').classList.toggle('on',up);
 $('#adapter').textContent=up?'adapter connected':'adapter offline';
}
// Switch the active backend toggle (used when an adapter is linked/connected)
function selectTarget(name){const sel=$('#target');if(sel.value!==name){sel.value=name;}refreshHealth();}
$('#target').addEventListener('change',refreshHealth);

function actionCard(d){
 const c=add('card',`<h4>proposed action · ${esc(d.verb)}</h4>
  <div class=kv>tier <b>${esc(d.tier)}</b> · id <b>${esc(d.id)}</b></div>
  <div class=preview>${esc(d.preview.en||'')}<div class=ar>${esc(d.preview.ar||'')}</div></div>
  <div class=kv>args <b>${esc(JSON.stringify(d.args))}</b></div>
  <div class=row><button class="ok">Approve &amp; commit</button><button class="ghost rej">Reject</button>
   <span class=tag>dry-run — nothing written yet</span></div>`);
 c.querySelector('.ok').onclick=async()=>{
  c.querySelector('.row').innerHTML='committing…';
  const r=await (await fetch('/api/commit',{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify({proposal_id:d.id,ts:d.ts,target:$('#target').value})})).json();
  if(r.ok){c.classList.add('done');c.querySelector('h4').textContent='committed · '+d.verb;
   c.querySelector('.row')?.remove();
   const ent=(r.result||{}).entity||{}, ss=(r.result||{}).ssot||{};
   let res=`<div class=kv style=margin-top:8px>state <b>${esc(r.state)}</b> · replayed <b>${esc(r.replayed)}</b></div>`;
   if(ent.id) res+=`<div class=kv>result <b>${esc(ent.type||'entity')}</b> id <b>${esc(ent.id)}</b>`
     +(ent.url?` · <b>${esc(ent.url)}</b>`:'')+` · ssot <b>${esc(ss.system||d.target||'')}</b></div>`;
   c.insertAdjacentHTML('beforeend',res);}
  else{c.insertAdjacentHTML('beforeend',`<div class=kv style=color:var(--bad)>commit failed: ${esc(r.error)}</div>`);}
 };
 c.querySelector('.rej').onclick=()=>{c.classList.add('refused');c.querySelector('h4').textContent='rejected · '+d.verb;c.querySelector('.row').remove();};
}

function renderRead(rd){
 const rows=(rd.items||[]).slice(0,12);
 const body=rows.length?rows.map(r=>`<div class=kv>· <b>${esc(r.id||r.name||'')}</b> ${esc(JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k])=>!k.startsWith('collection')&&!['id','target'].includes(k)).slice(0,6))))}</div>`).join(''):'<div class=kv>(no records)</div>';
 add('card',`<h4>read · ${esc(rd.target||'')} (${esc(rd.count??0)})</h4>${body}`+(rd.error?`<div class=kv style=color:var(--bad)>${esc(rd.error)}</div>`:''));
}
function renderItem(it){
 if(it.type==='refusal')add('card refused',`<h4>refused · ${esc(it.verb)}</h4>
  <div class=kv>${esc(it.code)} — ${esc(it.message)}${it.field?' (field: '+esc(it.field)+')':''}</div>`);
 else actionCard(it);
}

async function send(){
 const box=$('#box'),text=box.value.trim();if(!text)return;box.value='';
 add('msg me',esc(text));
 const typing=add('msg bot','…');
 try{
  const r=await (await fetch('/api/chat',{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify({message:text,target:$('#target').value})})).json();
  typing.remove();
  if(r.type==='message')add('msg bot',esc(r.text));
  else if(r.type==='actions'){
   for(const rd of (r.reads||[])) renderRead(rd);
   if(r.items.length>1)add('sys',`agent proposed ${r.items.length} actions — approve each:`);
   for(const it of r.items)renderItem(it);
  }
  else if(r.type==='action')actionCard(r);  // legacy single
 }catch(e){typing.remove();add('msg bot','error: '+esc(e.message));}
}
$('#send').onclick=send;
$('#box').addEventListener('keydown',e=>{if(e.key==='Enter')send();});

// settings dialog — provider/model catalog from LiteLLM
let CAT={};
function fillProviders(){
 const f=($('#c_provfilter').value||'').toLowerCase();
 const prov=$('#c_prov');prov.innerHTML='<option value="">(none — rule-based)</option>';
 for(const p of Object.keys(CAT)){ if(f&&!p.toLowerCase().includes(f))continue;  // CAT is popularity-ordered
  const o=document.createElement('option');o.value=p;o.textContent=`${p} (${CAT[p].length})`;prov.appendChild(o);}
}
async function loadCatalog(){
 CAT=await (await fetch('/api/providers')).json();
 fillProviders();
 $('#provcount').textContent=Object.keys(CAT).length;
}
$('#c_provfilter').oninput=fillProviders;
function fillModels(){
 const p=$('#c_prov').value, f=$('#c_modelfilter').value.toLowerCase();
 const sel=$('#c_model');sel.innerHTML='';
 const models=(CAT[p]||[]).filter(m=>m.toLowerCase().includes(f));
 for(const m of models){const o=document.createElement('option');o.value=m;o.textContent=m;sel.appendChild(o);}
 $('#modelcount').textContent=p?`(${models.length})`:'';
}
$('#c_prov').onchange=()=>{$('#c_modelfilter').value='';fillModels();};
$('#c_modelfilter').oninput=fillModels;
$('#c_save').onclick=async(e)=>{e.preventDefault();$('#c_save').textContent='Saving…';
 await fetch('/api/config',{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({provider:$('#c_prov').value,model:$('#c_model').value,
   api_key:$('#c_key').value,api_base:$('#c_base').value})});
 $('#c_save').textContent='Save';refreshMeta();closePanel();};

// Trace drawer — poll SEQRD-PC events and render them color-coded + expandable
let evAfter=-1, evTimer=null;
function renderEvents(list){
 const d=$('#evlist');
 for(const e of list){
  const row=document.createElement('div');row.className='ev '+(e.status||'ok');
  row.innerHTML=`<span class=chev>▸</span><span class="perf ${esc(e.perf)}">${esc(e.perf)}</span>`
   +`<span class=ttl>${esc(e.title)}</span>`
   +(e.level?`<span class=lvl>${esc(e.level)}</span>`:'')+`<span class=t>${esc(e.ts)}</span>`
   +`<div class=det>${esc(JSON.stringify(e.detail,null,2))}</div>`;
  row.onclick=()=>row.classList.toggle('open');
  d.insertBefore(row, d.firstChild);  // recent first — newest at the top
 }
 if(list.length){$('#tracecount').textContent=evAfter+1;d.scrollTop=0;}
}
async function pollEvents(){
 try{const r=await (await fetch('/api/events?after='+evAfter)).json();
  if(r.events.length){renderEvents(r.events);evAfter=r.last;}}catch(e){}
}
// Unified right sidebar — trace / provider / backend share one panel, smooth swap
let activePanel=null;
const TITLES={trace:'SEQRD-PC trace',history:'commit history',provider:'LLM provider',backend:'Backend credentials'};
function setPolling(on){if(on){pollEvents();evTimer=evTimer||setInterval(pollEvents,1200);}
 else{clearInterval(evTimer);evTimer=null;}}
async function openPanel(name){
 if(activePanel===name){closePanel();return;}
 activePanel=name;
 $('#drawer').classList.add('open');
 $('#panelTitle').textContent=TITLES[name];
 ['trace','history','provider','backend'].forEach(p=>$('#panel-'+p).hidden=(p!==name));
 $('#clearev').style.display=name==='trace'?'':'none';
 setPolling(name==='trace');
 if(name==='provider'&&!Object.keys(CAT).length)await loadCatalog();
 if(name==='backend')await loadBackend();
 if(name==='history')await loadHistory();
}
function closePanel(){activePanel=null;$('#drawer').classList.remove('open');setPolling(false);}
$('#trace').onclick=()=>openPanel('trace');
$('#history').onclick=()=>openPanel('history');
$('#gear').onclick=()=>openPanel('provider');
$('#backend').onclick=()=>openPanel('backend');
$('#panelclose').onclick=closePanel;
$('#clearev').onclick=(e)=>{e.stopPropagation();$('#evlist').innerHTML='';};

// Adjustable sidebar width — drag the left grip; persisted in localStorage
const MINW=280, MAXW=820;
function setDrawW(px){const w=Math.min(MAXW,Math.max(MINW,px));
 document.documentElement.style.setProperty('--draw-w',w+'px');return w;}
(()=>{const saved=parseInt(localStorage.getItem('nil-draw-w'));if(saved)setDrawW(saved);})();
let dragging=false;
$('#grip').addEventListener('mousedown',e=>{dragging=true;$('#grip').classList.add('active');
 $('#drawer').classList.add('nodrag-trans');document.body.style.userSelect='none';e.preventDefault();});
window.addEventListener('mousemove',e=>{if(!dragging)return;setDrawW(window.innerWidth-e.clientX);});
window.addEventListener('mouseup',()=>{if(!dragging)return;dragging=false;
 $('#grip').classList.remove('active');$('#drawer').classList.remove('nodrag-trans');
 document.body.style.userSelect='';
 const w=parseInt(getComputedStyle(document.documentElement).getPropertyValue('--draw-w'));
 if(w)localStorage.setItem('nil-draw-w',w);});

// Theme toggle (default dark, like the landing) — persisted in localStorage
function applyTheme(t){document.body.classList.toggle('light',t==='light');
 $('#theme').textContent=t==='light'?'☀':'☾';localStorage.setItem('nil-theme',t);}
$('#theme').onclick=()=>applyTheme(document.body.classList.contains('light')?'dark':'light');
applyTheme(localStorage.getItem('nil-theme')||'dark');

// Backend credentials — link/re-link the live PocketBase (shared sidebar panel)
async function loadBackend(){
 const b=await (await fetch('/api/backend')).json();
 $('#b_url').value=b.url||'';$('#b_identity').value=b.identity||'';$('#b_pass').value='';
 $('#b_status').innerHTML=b.connected?'status <b>connected</b>':'status <b>offline</b>';
}
$('#b_save').onclick=async(e)=>{e.preventDefault();
 $('#b_status').innerHTML='<div class=kv>connecting…</div>';
 const r=await (await fetch('/api/backend',{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({url:$('#b_url').value,identity:$('#b_identity').value,password:$('#b_pass').value})})).json();
 const steps=(r.steps||[]).map(s=>`<div class=cstep><span class="cdot ${s.ok?'on':'bad'}"></span>`
   +`<b>${esc(s.step)}</b> <span class=t>${esc(s.detail)}</span></div>`).join('');
 // expandable full capabilities — every target, every field {name:type, ! = required}
 const sk=r.skeleton||{}; const names=Object.keys(sk);
 let cap='';
 if(names.length){
  cap=`<details class=caps open><summary>capabilities · ${names.length} targets (click to expand)</summary>`;
  for(const n of names){const t=sk[n], fs=(t.fields||[]);
   const fields=fs.length?fs.map(f=>`<span class=fld>${esc(f.name)}<i>:${esc(f.type)}</i>${f.required?'<b>!</b>':''}</span>`).join(''):'<span class=t>(no declared fields)</span>';
   cap+=`<div class=cap><span class="cdot ${t.exists?'on':'bad'}"></span><b>${esc(n)}</b> <span class=t>${fs.length} fields</span><div class=flds>${fields}</div></div>`;}
  cap+=`</details>`;
 }
 $('#b_status').innerHTML=steps+`<div class=kv style=margin:6px 0>${r.ok?'✓ ':'✗ '}${esc(r.message)}</div>`+cap;
 if(r.ok){selectTarget('live');add('sys','backend connected (reachable · conformant · provisioned) — switched to live PocketBase');}
 refreshHealth();
}

// Commit history + rollback (audit trail tied to the SSOT)
async function loadHistory(){
 const h=(await (await fetch('/api/history')).json()).history||[];
 const d=$('#histlist');d.innerHTML = h.length?'':'<div class=kv style=padding:14px>no commits yet</div>';
 for(const e of h){
  const ent=e.entity||{}, rev=e.reversibility, can=e.token&&!e.reversed&&rev!=='IRREVERSIBLE';
  const row=document.createElement('div');row.className='hrow'+(e.reversed?' done':'');
  row.innerHTML=`<div class=kv><b>${esc(e.verb||'')}</b> <span class=t>${esc(e.ts)} · ${esc(e.system||e.target)}</span></div>`
   +`<div class=kv>${esc(ent.id||'')} <span class=t>${esc(ent.url||'')}</span> · <span class=tag>${esc(rev)}</span></div>`
   +(e.reversed?`<div class=kv style=color:var(--ok)>↩ reversed</div>`
     :can?`<div class=row><button class=ghost data-seq="${e.seq}" data-tg="${esc(e.target)}">↩ Rollback</button></div>`:'');
  d.appendChild(row);
 }
 d.querySelectorAll('button[data-seq]').forEach(b=>b.onclick=async()=>{
  b.textContent='rolling back…';
  const r=await (await fetch('/api/rollback',{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify({seq:+b.dataset.seq,target:b.dataset.tg})})).json();
  if(r.ok){add('sys','rolled back via '+(r.verb||'compensation'));loadHistory();}
  else{b.textContent='↩ Rollback';add('msg bot','rollback failed: '+esc(r.error||r.state||'reversal did not execute'));}
 });
}

(async()=>{await refreshMeta();
 const h=await (await fetch('/api/health')).json();
 if(h.live)selectTarget('live'); else await refreshHealth();  // prefer a connected real adapter
 setInterval(refreshHealth,4000);
 add('sys','kernel online — describe an action (e.g. “create a product called Aurora Lamp for 49.90”). '
   +'Writes are proposed first; you approve before anything is committed.');})();
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("UI_PORT", "8770"))
    # Bind host is configurable so the hosted/containerised playground can listen on
    # all interfaces (0.0.0.0) for the reverse proxy to reach it. Default stays on
    # loopback so the local `nilscript demo` is not exposed beyond the machine.
    host = os.environ.get("UI_HOST", "127.0.0.1")
    print("nilscript demo UI — starting kernel shims…")
    launch_shims()
    print(f"\n  ➜  open  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")

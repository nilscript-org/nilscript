"""Control-plane ASGI app — ingest NIL events (HMAC-verified), query, and a live single-pane UI.

    uvicorn nilscript.controlplane.app:app --host 0.0.0.0 --port 8088

Adapters POST their EVENT envelopes to /events/ingest (HttpEventEmitter → NIL_EVENTS_WEBHOOK), signed
with NIL_EVENTS_SECRET. The UI at / shows every action across all agents in one timeline.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
from typing import Any

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from nilscript.automation import (
    Runner,
    composed_hash,
    context_from_skeleton,
    dispatch_event,
    draft_automation,
    fire_composed,
    fire_manual,
    parse_composed,
    parse_trigger,
    register,
    run_due_schedules,
    validate_composed,
)
from nilscript.automation.compose import StageRunner
from nilscript.controlplane.store import EventStore
from nilscript.kernel.diagnostics import ValidationResult
from nilscript.kernel.executor import LocalExecutor
from nilscript.sdk.client import NilClient
from nilscript.sdk.connect import handshake
from nilscript.sdk.grants import GrantRef
from nilscript.sdk.transport import NilTransport


def _plan_scopes(plan: dict[str, Any]) -> frozenset[str]:
    """Grant scopes for a control-plane-fired run: each verb plus its `skill.*` wildcard."""
    scopes: set[str] = set()
    for node in plan.get("pipeline", []) if isinstance(plan, dict) else []:
        verb = node.get("verb") if isinstance(node, dict) else None
        if isinstance(verb, str) and verb:
            scopes.add(verb)
            scopes.add(verb.split(".", 1)[0] + ".*")
    return frozenset(scopes) or frozenset({"*"})

# An async source of a workspace's live adapter skeleton ({verbs, targets, ...}), or None when there
# is no reachable/conformant active adapter. Injectable so the draft gate is testable without a backend.
SkeletonProvider = Callable[[str], Awaitable[dict[str, Any] | None]]
# Skeleton of a SPECIFIC adapter by id (for cross-system composed plans). (workspace, adapter_id) -> skeleton|None.
AdapterSkeletonProvider = Callable[[str, str], Awaitable[dict[str, Any] | None]]


def _diag_list(result: ValidationResult) -> list[dict[str, Any]]:
    return [
        {"code": d.code, "severity": d.severity, "message": d.message, "node": d.node}
        for d in result.diagnostics
    ]


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
    skeleton_provider: SkeletonProvider | None = None,
    runner: Runner | None = None,
    adapter_skeleton_provider: AdapterSkeletonProvider | None = None,
    stage_runner: StageRunner | None = None,
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

    async def _live_skeleton(workspace: str) -> dict[str, Any] | None:
        """Default skeleton source: discover the workspace's active adapter over NIL. None when there
        is no active adapter, it's unreachable, or it doesn't answer with a conformant describe."""
        active = store.active_adapter(workspace)
        if not active or not active.get("url"):
            return None
        transport = NilTransport(base_url=active["url"], bearer_secret=active.get("bearer", "") or "")
        try:
            report = await handshake(transport)
        finally:
            await transport.aclose()
        if not report.get("reachable") or not report.get("conformant"):
            return None
        return report

    provider: SkeletonProvider = skeleton_provider or _live_skeleton

    async def _live_runner(plan: dict[str, Any], *, run_id: str) -> Any:
        """Default runner: walk the pinned plan against the workspace's active adapter via a headless
        LocalExecutor. The adapter bearer is the transport auth; the grant scopes are the plan's own
        verbs. (Production grant minting is the one knob to revisit when CP-initiated runs need a
        distinct identity from the adapter bearer.)"""
        ws = plan.get("workspace", "") if isinstance(plan, dict) else ""
        active = store.active_adapter(ws)
        if not active or not active.get("url"):
            raise RuntimeError(f"no active adapter for workspace {ws!r}")
        bearer = active.get("bearer", "") or ""
        transport = NilTransport(base_url=active["url"], bearer_secret=bearer)
        grant = GrantRef.from_secret(
            grant_id="control-plane", workspace=ws, secret=bearer or "cp",
            scopes=_plan_scopes(plan),
        )
        client = NilClient(transport=transport, grant=grant)
        try:
            executor = LocalExecutor(
                client, run_id=run_id, session_id=run_id, locale=plan.get("locale", "ar")
            )
            return await executor.execute(plan)
        finally:
            await transport.aclose()

    run_exec: Runner = runner or _live_runner

    async def _live_adapter_skeleton(workspace: str, adapter_id: str) -> dict[str, Any] | None:
        """Discover a SPECIFIC registered adapter (by id) over NIL — for composed-plan validation,
        where each stage names its own backend (which may not be the workspace's active one)."""
        match = next(
            (a for a in store.list_adapters(workspace)
             if a.get("adapter_id") == adapter_id and a.get("url")),
            None,
        )
        if match is None:
            return None
        transport = NilTransport(base_url=match["url"], bearer_secret=match.get("bearer", "") or "")
        try:
            report = await handshake(transport)
        finally:
            await transport.aclose()
        return report if report.get("reachable") and report.get("conformant") else None

    adapter_skeletons: AdapterSkeletonProvider = adapter_skeleton_provider or _live_adapter_skeleton

    async def _live_stage_runner(adapter: str, plan: dict[str, Any], *, run_id: str, input: dict[str, Any]) -> Any:
        """Run one composed stage against the named adapter (by id) via a headless LocalExecutor."""
        ws = plan.get("workspace", "") if isinstance(plan, dict) else ""
        match = next(
            (a for a in store.list_adapters(ws) if a.get("adapter_id") == adapter and a.get("url")),
            None,
        )
        if match is None:
            raise RuntimeError(f"no registered adapter {adapter!r} in workspace {ws!r}")
        bearer = match.get("bearer", "") or ""
        transport = NilTransport(base_url=match["url"], bearer_secret=bearer)
        grant = GrantRef.from_secret(
            grant_id="control-plane", workspace=ws, secret=bearer or "cp", scopes=_plan_scopes(plan),
        )
        client = NilClient(transport=transport, grant=grant)
        try:
            return await LocalExecutor(
                client, run_id=run_id, session_id=run_id, locale=plan.get("locale", "ar")
            ).execute(plan, input=input or None)
        finally:
            await transport.aclose()

    stage_exec: StageRunner = stage_runner or _live_stage_runner
    _bg_tasks: set[asyncio.Task[Any]] = set()  # keep fire-and-forget dispatch tasks from being GC'd

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
        if new:
            # Fire event-triggered automations off the request path — ingest must stay fast and must
            # not block on (or fail because of) a downstream run. The loop guard in dispatch_event
            # skips events that triggered runs themselves produced.
            task = asyncio.create_task(dispatch_event(store, envelope, runner=run_exec))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
        return {"ok": True, "new": new}

    @app.get("/api/events")
    def events(limit: int = 100) -> dict[str, Any]:
        return {"events": store.recent(limit)}

    @app.get("/api/events/{event_id}")
    def event_detail(event_id: int) -> Any:
        """The full payload journey for one row — intent → resolution → field-level SSOT verdict →
        effect — fetched lazily when the operator expands a row."""
        detail = store.detail(event_id)
        if detail is None:
            return JSONResponse({"error": "no such event"}, status_code=404)
        return detail

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

    @app.get("/api/adapter-skeleton")
    async def api_adapter_skeleton(
        workspace: str = "", adapter_id: str = "", authorization: str | None = Header(default=None),
    ) -> Any:
        """The verbs (and target names) a specific adapter declares — feeds the UI compose form's verb
        dropdowns. Token-gated: it triggers a live handshake using the adapter's bearer."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        skeleton = await adapter_skeletons(workspace, adapter_id)
        if skeleton is None:
            return JSONResponse({"error": "adapter not reachable/conformant"}, status_code=503)
        return {
            "verbs": skeleton.get("verbs", []),
            "targets": sorted((skeleton.get("targets") or {}).keys()),
        }

    @app.get("/api/automations")
    def api_automations() -> dict[str, Any]:
        """Dashboard view of every automation (latest version, all workspaces). Public read — no
        secrets in the record; the heavy plan is summarised, not shipped whole."""
        out: list[dict[str, Any]] = []
        for a in store.all_automations():
            plan = a.get("plan") or {}
            if a.get("kind") == "composed":
                stages = plan.get("stages") or []
                summary = {
                    "stages": len(stages),
                    "adapters": sorted({s.get("adapter") for s in stages if isinstance(s, dict)}),
                }
            else:
                summary = {"nodes": len(plan.get("pipeline") or [])}
            out.append({
                "workspace": a["workspace"], "automation_id": a["automation_id"],
                "version": a["version"], "content_hash": a["content_hash"],
                "kind": a.get("kind", "single"), "name": a.get("name") or {},
                "state": a["state"], "trigger": a.get("trigger") or {},
                "approved_by": a.get("approved_by"), "authored_by": a.get("authored_by"),
                "created_at": a.get("created_at"), "plan_summary": summary,
            })
        return {"automations": out}

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

    @app.post("/adapters/{workspace}/{adapter_id}/enable")
    def enable_adapter(workspace: str, adapter_id: str, authorization: str | None = Header(default=None)) -> Any:
        """Enable an adapter WITHOUT deactivating siblings — several can be active at once (e.g.
        PocketBase + Odoo for a cross-system automation). Operator-gated."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not store.set_adapter_active(workspace, adapter_id, True):
            return JSONResponse({"error": "no such adapter"}, status_code=404)
        return {"ok": True, "workspace": workspace, "adapter_id": adapter_id, "active": True}

    @app.post("/adapters/{workspace}/{adapter_id}/disable")
    def disable_adapter(workspace: str, adapter_id: str, authorization: str | None = Header(default=None)) -> Any:
        """Disable one adapter (leaves siblings untouched). Operator-gated."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not store.set_adapter_active(workspace, adapter_id, False):
            return JSONResponse({"error": "no such adapter"}, status_code=404)
        return {"ok": True, "workspace": workspace, "adapter_id": adapter_id, "active": False}

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

    # ── automation registry (conversation-authored, deterministically lowered, SSOT-stored) ─────
    async def _draft_from_body(body: dict[str, Any]) -> tuple[Any, Any]:
        """Validate a draft request against the workspace's live skeleton. Returns (DraftResult, None)
        or (None, JSONResponse-error). The plan's own `workspace` selects the adapter to validate
        against, so the lowered plan is bounded by the backend that will actually run it."""
        plan = body.get("plan")
        aid, name, trigger = body.get("automation_id"), body.get("name"), body.get("trigger")
        if not isinstance(plan, dict) or not aid or name is None or trigger is None:
            return None, JSONResponse(
                {"error": "automation_id, name, plan, trigger are required"}, status_code=400
            )
        ws = plan.get("workspace")
        if not ws:
            return None, JSONResponse({"error": "plan.workspace is required"}, status_code=400)
        skeleton = await provider(ws)
        if skeleton is None:
            return None, JSONResponse(
                {"error": "no reachable active adapter for this workspace"}, status_code=503
            )
        ctx = context_from_skeleton(ws, skeleton)
        try:
            res = draft_automation(
                automation_id=aid, name=name, raw_plan=plan, trigger=trigger, ctx=ctx,
                authored_by=body.get("authored_by", "") or "", description=body.get("description"),
            )
        except (ValidationError, ValueError) as exc:
            return None, JSONResponse({"error": f"malformed request: {exc}"}, status_code=400)
        return res, None

    @app.post("/automations/draft")
    async def automation_draft(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Preview: lower the agent's candidate plan against the live skeleton. No side effect.
        Returns the validator verdict + content-hash, or a structured refusal."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad json"}, status_code=400)
        res, err = await _draft_from_body(body)
        if err is not None:
            return err
        if not res.ok:
            return {"ok": False, "refusal": _diag_list(res.diagnostics)}
        return {
            "ok": True,
            "content_hash": res.content_hash,
            "definition": res.definition.model_dump(by_alias=True, mode="json"),
        }

    @app.post("/automations/register")
    async def automation_register(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Persist a passing draft to the SSOT as `pending_approval` (never auto-armed). Re-registering
        an identical plan is an idempotent no-op. A failing plan is refused — never stored."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad json"}, status_code=400)
        res, err = await _draft_from_body(body)
        if err is not None:
            return err
        if not res.ok:
            return JSONResponse({"ok": False, "refusal": _diag_list(res.diagnostics)}, status_code=400)
        stored = register(store, res.definition)  # lands in pending_approval
        return {"ok": True, "definition": stored.model_dump(by_alias=True, mode="json")}

    # ── cross-system composed automations (P3) ──────────────────────────────────────────────
    async def _validate_composed_body(body: dict[str, Any]) -> tuple[Any, Any]:
        """Validate a composed-plan request: each stage against ITS adapter's live skeleton + handoff
        well-formedness + a valid trigger. Returns ((composed_raw, report), None) or (None, error)."""
        composed = body.get("composed")
        aid, name, trigger = body.get("automation_id"), body.get("name"), body.get("trigger")
        if not isinstance(composed, dict) or not aid or name is None or trigger is None:
            return None, JSONResponse(
                {"error": "automation_id, name, composed, trigger are required"}, status_code=400
            )
        ws = composed.get("workspace")
        if not ws:
            return None, JSONResponse({"error": "composed.workspace is required"}, status_code=400)
        try:
            parse_trigger(trigger)
        except (ValidationError, ValueError, TypeError) as exc:
            return None, JSONResponse({"error": f"bad trigger: {exc}"}, status_code=400)
        stages = composed.get("stages") or []
        adapter_ids = {s.get("adapter") for s in stages if isinstance(s, dict)}
        skeletons: dict[str, Any] = {}
        for adapter_id in adapter_ids:
            skeleton = await adapter_skeletons(ws, adapter_id)
            if skeleton is None:
                return None, JSONResponse(
                    {"error": f"no reachable adapter {adapter_id!r} in workspace {ws!r}"},
                    status_code=503,
                )
            skeletons[adapter_id] = skeleton
        try:
            parsed = parse_composed(composed)
        except (KeyError, TypeError) as exc:
            return None, JSONResponse({"error": f"malformed composed plan: {exc}"}, status_code=400)
        return (composed, validate_composed(parsed, skeletons)), None

    @app.post("/automations/compose/draft")
    async def compose_draft(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Preview: validate a cross-system composed plan, each stage against its adapter. No effect."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad json"}, status_code=400)
        res, err = await _validate_composed_body(body)
        if err is not None:
            return err
        composed, report = res
        if not report["ok"]:
            return {"ok": False, "report": report}
        return {"ok": True, "content_hash": composed_hash(composed), "report": report}

    @app.post("/automations/compose/register")
    async def compose_register(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Persist a passing composed plan as `pending_approval` (kind='composed')."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad json"}, status_code=400)
        res, err = await _validate_composed_body(body)
        if err is not None:
            return err
        composed, report = res
        if not report["ok"]:
            return JSONResponse({"ok": False, "report": report}, status_code=400)
        stored = store.register_automation(
            workspace=composed["workspace"], automation_id=body["automation_id"],
            content_hash=composed_hash(composed), name=body["name"], plan=composed,
            trigger=body["trigger"], state="pending_approval", kind="composed",
            authored_by=body.get("authored_by", "") or "", description=body.get("description"),
        )
        return {"ok": True, "definition": stored}

    @app.get("/automations")
    def automations_list(workspace: str = "") -> dict[str, Any]:
        """Latest version of every automation in a workspace (public read — no secrets in the record)."""
        return {"automations": store.list_automations(workspace)}

    @app.get("/automations/{workspace}/{automation_id}")
    def automation_get(workspace: str, automation_id: str, version: int | None = None) -> Any:
        a = store.get_automation(workspace, automation_id, version)
        if a is None:
            return JSONResponse({"error": "no such automation"}, status_code=404)
        return a

    @app.post("/automations/{workspace}/{automation_id}/{version}/state")
    async def automation_set_state(
        workspace: str, automation_id: str, version: int, request: Request,
        authorization: str | None = Header(default=None),
    ) -> Any:
        """Arm/disarm/approve an automation (operator-gated). Approving (→ active) records the owner.
        Arming a recurring automation is a governance act, so it sits behind the registry token."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad json"}, status_code=400)
        state = body.get("state")
        if state not in ("pending_approval", "active", "paused", "archived"):
            return JSONResponse(
                {"error": "state must be pending_approval|active|paused|archived"}, status_code=400
            )
        ok = store.set_automation_state(
            workspace, automation_id, version, state, approved_by=body.get("approved_by")
        )
        if not ok:
            return JSONResponse({"error": "no such automation version"}, status_code=404)
        return {"ok": True, "automation": store.get_automation(workspace, automation_id, version)}

    @app.post("/automations/{workspace}/{automation_id}/run")
    async def automation_run(
        workspace: str, automation_id: str, request: Request,
        authorization: str | None = Header(default=None),
    ) -> Any:
        """Fire the active automation now (manual trigger). Requires an `idempotency_key` so a
        re-delivered fire replays the same run rather than executing twice. Operator-gated."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        idem = body.get("idempotency_key")
        if not idem or len(str(idem)) < 6:
            return JSONResponse(
                {"error": "idempotency_key (>= 6 chars) is required"}, status_code=400
            )
        fired_by = body.get("fired_by", "manual") or "manual"
        auto = store.get_automation(workspace, automation_id)
        if auto is not None and auto.get("kind") == "composed":
            out = await fire_composed(
                store, workspace=workspace, automation_id=automation_id,
                idempotency_key=str(idem), stage_runner=stage_exec, fired_by=fired_by,
            )
        else:
            out = await fire_manual(
                store, workspace=workspace, automation_id=automation_id,
                idempotency_key=str(idem), runner=run_exec, fired_by=fired_by,
            )
        if not out.get("ok"):
            return JSONResponse(out, status_code=out.pop("status", 400))
        return out

    @app.post("/automations/tick")
    async def automations_tick(authorization: str | None = Header(default=None)) -> Any:
        """Fire interval-scheduled automations that are due. Called by an external clock (cron /
        Temporal Schedule) — the control plane decides *which* are due; the caller owns the tick.
        Operator-gated."""
        if not _registry_authed(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        now = _dt.datetime.now(_dt.UTC)
        fired = await run_due_schedules(store, runner=run_exec, now=now)
        return {
            "ok": True,
            "fired": len(fired),
            "runs": [f["run"] for f in fired if f.get("ok") and f.get("run")],
        }

    @app.get("/automations/{workspace}/{automation_id}/runs")
    def automation_runs(workspace: str, automation_id: str, limit: int = 50) -> dict[str, Any]:
        """Newest-first run history for one automation (trace omitted — fetch via /runs/{run_id})."""
        return {"runs": store.list_runs(workspace, automation_id, limit)}

    @app.get("/runs/{run_id}")
    def run_detail(run_id: str) -> Any:
        run = store.get_run(run_id)
        if run is None:
            return JSONResponse({"error": "no such run"}, status_code=404)
        return run

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
  .feed-head,.row{display:grid;grid-template-columns:74px 76px 104px 96px minmax(140px,1.3fr) 72px 104px minmax(72px,.9fr) 100px;
    align-items:center;gap:12px;padding:8px clamp(12px,2vw,16px);min-width:880px}
  .feed-head{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.08em;
    border-bottom:1px solid var(--line);background:var(--elev)}
  .row{border-bottom:1px solid var(--line);transition:background .12s ease;cursor:pointer}
  .row:last-child{border-bottom:none}
  .row:hover{background:var(--rowhover)}
  .row.sel{background:var(--rowhover)}
  .row .caret{color:var(--faint);transition:transform .15s ease;display:inline-block}
  .row.sel .caret{transform:rotate(90deg);color:var(--verb)}
  .t{color:var(--faint)} .src{color:var(--mut)} .ws{color:var(--faint)}
  .verbcell{color:var(--verb);font-weight:500;word-break:break-word}
  .vdetail{display:block;color:var(--faint);font-weight:400;font-size:11px;margin-top:2px;word-break:break-word}
  .vdetail b{color:var(--mut);font-weight:500}
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

  /* ── verified column: the SSOT read-back verdict, not the commit's say-so ── */
  .vf{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;padding:1px 8px;border-radius:6px;
    border:1px solid var(--line2);white-space:nowrap}
  .vf.verified{color:var(--green);border-color:rgba(70,194,102,.35);background:rgba(70,194,102,.07)}
  .vf.partial{color:#f0c674;border-color:rgba(224,166,41,.5);background:rgba(224,166,41,.10);font-weight:600}
  .vf.failed{color:#ff9a90;border-color:rgba(251,90,78,.55);background:rgba(251,90,78,.12);font-weight:600}
  .vf-na{color:var(--faint)}

  /* ── expanded row: the full payload journey ── */
  .exp{display:none;min-width:880px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(91,140,255,.045),transparent 120px),var(--elev)}
  .exp.open{display:block;padding:6px clamp(12px,2vw,20px) 18px}
  .exp-load{color:var(--faint);padding:16px 4px}
  .dsec{margin-top:14px}
  .dsec>.h{color:var(--mut);font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;
    margin:0 0 7px;display:flex;align-items:center;gap:8px}
  .dsec>.h::after{content:"";flex:1;height:1px;background:var(--line)}
  .facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px 18px}
  .fact{min-width:0}
  .fact .k{color:var(--faint);font-size:11px}
  .fact .v{color:var(--fg);word-break:break-word}
  .fact .v.mut{color:var(--mut)}
  /* field-level diff table — the heart of the expansion */
  .ftable{width:100%;border-collapse:collapse;font-size:12.5px}
  .ftable th{text-align:left;color:var(--faint);font-weight:500;font-size:10.5px;text-transform:uppercase;
    letter-spacing:.06em;padding:5px 10px;border-bottom:1px solid var(--line)}
  .ftable td{padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-word}
  .ftable tr:last-child td{border-bottom:none}
  .ftable .fname{color:var(--verb)}
  .ftable .ok{color:var(--green)} .ftable .drop{color:#ff9a90;font-weight:600}
  .ftable tr.dropped{background:rgba(251,90,78,.06)}
  .dnote{color:var(--faint);font-size:11px;margin-top:7px}
  .draw{margin:0;padding:11px 13px;background:var(--bg);border:1px solid var(--line);border-radius:9px;
    font-size:11.5px;color:var(--mut);overflow:auto;max-height:340px;white-space:pre;line-height:1.5}
  .copybtn{margin-bottom:7px}

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

  /* ── automations ── */
  #autoWrap{margin-bottom:26px;display:none}
  .auth-row{display:flex;align-items:center;gap:8px;margin:0 2px 12px;flex-wrap:wrap}
  .auth-row input{background:var(--elev);border:1px solid var(--line2);border-radius:8px;color:var(--fg);
    font:12px var(--mono);padding:6px 10px;width:230px;max-width:60vw}
  .auth-row .hint{color:var(--faint);font-size:11px}
  .auth-row .ok{color:var(--green);font-size:11px}
  #automations{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
  .autocard{border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);
    padding:14px 16px;position:relative;overflow:hidden}
  .autocard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--faint)}
  .autocard.s-active::before{background:var(--green)}
  .autocard.s-pending_approval::before{background:var(--amber)}
  .autocard.s-paused::before{background:var(--blue)}
  .autocard.s-archived::before{background:var(--line2)}
  .autocard .top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .autocard .nm{font-weight:600;color:var(--verb);font-size:14.5px;flex:1 1 auto;min-width:0;word-break:break-word}
  .sbadge{font-size:11px;padding:2px 9px;border-radius:999px;border:1px solid var(--line2);color:var(--mut);white-space:nowrap}
  .sbadge.s-active{color:var(--green);border-color:rgba(70,194,102,.45);background:rgba(70,194,102,.08)}
  .sbadge.s-pending_approval{color:#f0c674;border-color:rgba(224,166,41,.45);background:rgba(224,166,41,.08)}
  .sbadge.s-paused{color:var(--blue);border-color:rgba(91,140,255,.35);background:rgba(91,140,255,.08)}
  .sbadge.s-draft,.sbadge.s-archived{color:var(--faint)}
  .autocard .meta{display:flex;flex-wrap:wrap;gap:6px;margin:9px 0 4px}
  .autocard .meta span{font-size:11px;color:var(--mut);border:1px solid var(--line2);border-radius:6px;padding:1px 7px}
  .autocard .meta .kind{color:var(--violet);border-color:rgba(168,119,247,.3)}
  .autocard .meta .trig{color:var(--blue);border-color:rgba(91,140,255,.3)}
  .autocard .sub{color:var(--faint);font-size:11px;margin-top:5px;word-break:break-all}
  .autocard .acts{display:flex;gap:7px;margin-top:11px;flex-wrap:wrap}
  .autocard .runs{margin-top:10px;border-top:1px solid var(--line);padding-top:9px;display:none}
  .autocard .runs.open{display:block}
  .runrow{display:flex;align-items:center;gap:8px;font-size:11.5px;padding:3px 0;color:var(--mut)}
  .runrow .rst{padding:1px 7px;border-radius:5px;border:1px solid var(--line2);font-size:10.5px}
  .runrow .rst.completed{color:var(--green);border-color:rgba(70,194,102,.4)}
  .runrow .rst.failed,.runrow .rst.blocked{color:#ff9a90;border-color:rgba(251,90,78,.4)}
  .runrow .rst.partial,.runrow .rst.compensated{color:#f0c674;border-color:rgba(224,166,41,.45)}
  .runrow .rst.running{color:var(--blue);border-color:rgba(91,140,255,.35)}
  /* compose form */
  .cform{border:1px solid var(--line2);border-radius:var(--radius);background:var(--panel);
    padding:14px 16px;display:grid;gap:10px;margin-bottom:14px}
  .cform input,.cform select,.cform textarea{background:var(--elev);border:1px solid var(--line2);
    border-radius:8px;color:var(--fg);font:12px var(--mono);padding:7px 10px;width:100%}
  .cform textarea{min-height:46px;resize:vertical}
  .cform .row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .cform .ids{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .stageblk{border:1px solid var(--line);border-radius:10px;padding:11px 12px;display:grid;gap:8px}
  .stageblk .stitle{color:var(--verb);font-size:12px;font-weight:600}
  .stageblk.b2{border-color:rgba(168,119,247,.3)}
  .cform .handoff{display:flex;align-items:center;gap:8px;color:var(--mut);font-size:12px;flex-wrap:wrap}
  .cform .handoff input{width:auto;flex:1 1 120px}
  .arrow{color:var(--violet);font-weight:600}

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

  <section id=autoWrap>
    <div class=sec-title>Automations <span class=n id=autocount>0</span>
      <span style="color:var(--faint);text-transform:none;letter-spacing:0">— conversation-authored workflows: state, trigger, version &amp; run history</span></div>
    <div class=auth-row>
      <input id=optoken type=password placeholder="operator token (for controls)" autocomplete=off oninput=saveToken()>
      <span class=hint id=tokhint>controls (approve / pause / run) need the registry token — view is open</span>
      <button class="btn tiny" onclick=toggleCompose()>＋ New cross-system automation</button>
    </div>
    <div id=composeForm style=display:none>
      <div class=cform>
        <div class=ids><input id=cf_id placeholder="automation id (e.g. lead-to-invoice)">
          <input id=cf_name placeholder="name"></div>
        <div class="stageblk b1">
          <div class=stitle>Stage 1 — system A</div>
          <div class=row2><select id=cf_a1 onchange="loadVerbs('cf_v1',this.value)"></select><select id=cf_v1></select></div>
          <textarea id=cf_args1 placeholder='args JSON — e.g. {"name":"Acme Co"}'></textarea>
        </div>
        <div class="stageblk b2">
          <div class=stitle>Stage 2 — system B</div>
          <div class=row2><select id=cf_a2 onchange="loadVerbs('cf_v2',this.value)"></select><select id=cf_v2></select></div>
          <textarea id=cf_args2 placeholder='args JSON — use $.input.X for handoff, e.g. {"ref":"$.input.lead"}'></textarea>
          <div class=handoff>handoff: <input id=cf_hk placeholder="input key (e.g. lead)">
            <span class=arrow>←</span> <input id=cf_hr value="$.stage_1.step_1.output.state"></div>
        </div>
        <button class="btn ok" onclick=submitCompose()>Create automation</button>
      </div>
    </div>
    <div id=automations></div>
  </section>

  <div class=sec-title>Activity <span style="color:var(--faint);text-transform:none;letter-spacing:0">— every agent action, one pane</span></div>
  <div class=feed>
    <div class=feed-head>
      <span>time</span><span>source</span><span>event</span><span>verified</span><span>verb</span>
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
const VFG={verified:'✓',partial:'⚠',failed:'✗'};
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
      const detail=[e.system?`<b>${esc(e.system)}</b>`:'',esc(e.summary||'')].filter(Boolean).join(' · ');
      const vf=e.verify?`<span class="vf ${esc(e.verify)}">${VFG[e.verify]||''} ${esc(e.verify)}</span>`:'<span class=vf-na>—</span>';
      const act=canRoll
        ? `<button class="btn tiny ghost" title="Reversible (${esc(e.reversibility)})" onclick="event.stopPropagation();copyRollback('${esc(e.compensation_token)}')">⤺ rollback</button>`
        : (e.reversibility?`<span class=rev title="${esc(e.reversibility)}">${e.reversibility==='IRREVERSIBLE'?'— final':''}</span>`:'');
      return `<div class=row data-id="${e.id}" onclick="toggle(${e.id})">
        <span class=t data-l=time title="${esc(e.received_at)}"><span class=caret>›</span> ${hhmmss(e.received_at)}</span>
        <span class=src data-l=source>${esc(e.source||'')}</span>
        <span class=ev data-l=event><span class="pill ${cls}">${esc(ev)}</span></span>
        <span data-l=verified>${vf}</span>
        <span class=verbcell data-l=verb>${esc(e.verb||'—')}${detail?`<span class=vdetail>${detail}</span>`:''}</span>
        <span data-l=tier>${tier}</span>
        <span class=pid data-l=proposal>${esc((e.proposal||'').slice(0,10))}</span>
        <span class=ws data-l=workspace>${esc(e.workspace||'—')}</span>
        <span class=rowact data-l=action>${act}</span>
      </div><div class=exp id="exp-${e.id}"></div>`;
    }).join('');
    paintOpen();   // restore an open expansion across the 2s refresh (detail is cached & immutable)
  }catch(_){}
}

// ── expandable row: the full payload journey, lazy-fetched on click ─────────────────────────────
let openId=null;const detailHtml={};
function toggle(id){
  if(openId===id){openId=null;paintOpen();return;}
  openId=id;
  if(detailHtml[id]){paintOpen();return;}
  paintOpen('<div class=exp-load>Loading the payload journey…</div>');
  fetch('/api/events/'+id).then(r=>r.ok?r.json():Promise.reject(r.status))
    .then(d=>{detailHtml[id]=renderDetail(d);if(openId===id)paintOpen();})
    .catch(()=>{if(openId===id)paintOpen('<div class=exp-load>Could not load this event\\'s detail.</div>');});
}
function paintOpen(loadingHtml){
  document.querySelectorAll('.exp.open').forEach(el=>{el.classList.remove('open');el.innerHTML='';});
  document.querySelectorAll('.row.sel').forEach(el=>el.classList.remove('sel'));
  if(openId==null)return;
  const exp=document.getElementById('exp-'+openId);
  if(!exp)return;  // the row scrolled out of the 200-row window
  exp.innerHTML=loadingHtml||detailHtml[openId]||'';
  exp.classList.add('open');
  const row=document.querySelector('.row[data-id="'+openId+'"]');if(row)row.classList.add('sel');
}

function fact(k,v,mut){return v==null||v===''?'':`<div class=fact><div class=k>${esc(k)}</div><div class="v${mut?' mut':''}">${esc(v)}</div></div>`;}
function jval(v){return (v&&typeof v==='object')?JSON.stringify(v):String(v==null?'':v);}
function renderDetail(d){
  const out=[];
  const vf=d.verify?`<span class="vf ${esc(d.verify)}">${VFG[d.verify]||''} ${esc(d.verify)}</span>`:'';
  const tier=d.tier?`<span class="tier ${esc(d.tier)}">${esc(d.tier)}</span>`:'';
  const prev=d.preview&&(d.preview.ar||d.preview.en)||'';
  // a. intent
  out.push(`<div class=dsec><div class=h>intent — what the agent asked</div><div class=facts>
    ${fact('verb',d.verb)}${fact('tier',d.tier)}${fact('workspace',d.workspace)}
    ${fact('source',d.source)}${fact('grant',d.grant_id,1)}</div>
    ${prev?`<div class=dnote>“${esc(prev)}”</div>`:''}
    ${Object.keys(d.raw_args||{}).length?`<div class=dnote>raw args</div><pre class=draw>${esc(JSON.stringify(d.raw_args,null,2))}</pre>`:''}</div>`);
  // b. resolution
  if(Object.keys(d.resolved||{}).length||d.ignored||d.expires_at){
    out.push(`<div class=dsec><div class=h>resolution — what the system resolved it to</div><div class=facts>
      ${fact('expires at',d.expires_at,1)}${d.ignored?fact('ignored args',jval(d.ignored)):''}</div>
      ${Object.keys(d.resolved||{}).length?`<pre class=draw>${esc(JSON.stringify(d.resolved,null,2))}</pre>`:''}</div>`);
  }
  // c. field-level SSOT verdict — the heart. before → requested → after, read back from the source.
  if((d.fields||[]).length){
    const dropped=d.fields.filter(f=>!f.verified).length;
    const hasBA=d.fields.some(f=>('before' in f)||('after' in f));  // adapter emitted the real read-back
    const trs=d.fields.map(f=>{
      const cells=hasBA
        ? `<td class=v>${esc(jval(f.before))}</td><td>${esc(jval(f.requested))}</td><td class="${f.verified?'':'drop'}">${esc(jval(f.after))}</td>`
        : `<td>${esc(jval(f.requested))}</td>`;
      return `<tr class="${f.verified?'':'dropped'}">
        <td class=fname>${esc(f.field)}</td>${cells}
        <td class="${f.verified?'ok':'drop'}">${f.verified?'✓ landed':'✗ dropped'}</td></tr>`;
    }).join('');
    const head=hasBA
      ? '<th>field</th><th>before</th><th>requested</th><th>after (SSOT)</th><th>verdict</th>'
      : '<th>field</th><th>requested</th><th>verdict</th>';
    out.push(`<div class=dsec><div class=h>field verification — before → requested → SSOT read-back ${vf}</div>
      <table class=ftable><thead><tr>${head}</tr></thead><tbody>${trs}</tbody></table>
      ${dropped?`<div class=dnote>⚠ ${dropped} field(s) did not survive the write — the row claimed success but the SSOT disagrees.${hasBA?'':' (This adapter reports only the dropped field names, not per-field read-back values.)'}</div>`:''}</div>`);
  }
  // d. commit & effect
  const r=d.result||{};const e=r.entity||{};const ss=r.ssot||{};const c=r.compensation||{};
  const replayed=(d.journey||[]).some(j=>j.replayed);
  if(r.claim||e.id||c.reversibility){
    out.push(`<div class=dsec><div class=h>commit &amp; effect</div><div class=facts>
      ${fact('claim',r.claim)}${fact('changed',r.changed==null?'':String(r.changed))}
      ${fact('entity',e.type)}${fact('entity id',e.id)}${fact('entity url',e.url)}
      ${fact('backend',ss.system)}${fact('read-after-write',ss.read_after_write==null?'':String(ss.read_after_write))}
      ${fact('reversibility',c.reversibility)}${fact('compensation token',c.token,1)}
      ${replayed?fact('replayed','yes (idempotent — deduped)'):''}</div></div>`);
  }
  // e. refusal (security-relevant)
  if(d.refusal&&d.refusal.code){
    out.push(`<div class=dsec><div class=h>refusal</div><div class=facts>
      ${fact('code',d.refusal.code)}${fact('field',d.refusal.field)}</div>
      ${d.refusal.message?`<div class=dnote>${esc(d.refusal.message)}</div>`:''}</div>`);
  }
  // f. saga thread
  if((d.journey||[]).length>1){
    out.push(`<div class=dsec><div class=h>journey — every event on this proposal</div><div class=facts>
      ${d.journey.map(j=>fact(hhmmss(j.received_at),j.event)).join('')}</div></div>`);
  }
  // g. raw — reconstruct from the log alone
  const raw=esc(JSON.stringify(d.raw,null,2));
  out.push(`<div class=dsec><div class=h>raw envelopes</div>
    <button class="btn tiny ghost copybtn" onclick='event.stopPropagation();copyText(${JSON.stringify(JSON.stringify(d.raw,null,2))})'>⧉ copy raw</button>
    <pre class=draw>${raw}</pre></div>`);
  return out.join('');
}
async function copyText(t){
  try{await navigator.clipboard.writeText(t);}catch(e){
    const ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');}catch(_){}ta.remove();}
  toast('Raw envelopes copied.');
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
  const r=await fetch('/api/registry');const data=await r.json();const adapters=data.adapters||[];const ws=data.workspace||'';
  const wrap=document.getElementById('routingWrap'),box=document.getElementById('routing');
  document.getElementById('rcount').textContent=adapters.length;
  wrap.style.display=adapters.length?'block':'none';
  box.innerHTML=adapters.map(a=>{
   const on=!!a.active;
   const tgl=on
     ? abtn('⏻ Disable','ghost',`adapterToggle('${esc(ws)}','${esc(a.adapter_id)}',false)`)
     : abtn('⏼ Enable','ok',`adapterToggle('${esc(ws)}','${esc(a.adapter_id)}',true)`);
   return `<div class="rrow ${on?'on':''}">
     <span class=nm>${esc(a.label||a.adapter_id)}</span>
     <span class=sys>${esc(a.system||a.adapter_id)}</span>
     <span class=host>${esc(host(a.url))}</span>
     <span class=grow></span>
     <span class="rbadge ${on?'on':''}">${on?'● active':'idle'}</span>
     ${tgl}
   </div>`;
  }).join('');
 }catch(_){}
}
async function adapterToggle(ws,id,enable){
  if(!tokenVal())return toast('Enter the <b>operator token</b> (Automations section) to toggle adapters.');
  try{const r=await fetch(`/adapters/${ws}/${id}/${enable?'enable':'disable'}`,{method:'POST',headers:authHeaders()});
    if(r.status===401)return toast('Operator token rejected.');
    toast(enable?'Adapter <b>enabled</b> — several can be active at once.':'Adapter disabled.');loadRouting();
  }catch(_){toast('Could not toggle the adapter.');}
}
function applyThemeGlyph(){var b=document.getElementById('themeBtn');if(b)b.textContent=document.documentElement.getAttribute('data-theme')==='light'?'☀':'☾';}
function toggleTheme(){var next=document.documentElement.getAttribute('data-theme')==='light'?'dark':'light';document.documentElement.setAttribute('data-theme',next);try{localStorage.setItem('cp-theme',next);}catch(e){}applyThemeGlyph();}

// ── automations: view + (token-gated) control ───────────────────────────────────────────────────
function tokenVal(){try{return localStorage.getItem('cp-optoken')||'';}catch(e){return '';}}
function saveToken(){var v=document.getElementById('optoken').value;try{localStorage.setItem('cp-optoken',v);}catch(e){}paintTokHint(v);}
function paintTokHint(v){var h=document.getElementById('tokhint');if(!h)return;h.className=v?'ok':'hint';
  h.textContent=v?'token set — controls enabled in this browser only':'controls (approve / pause / run) need the registry token — view is open';}
function initToken(){var i=document.getElementById('optoken');if(i){i.value=tokenVal();paintTokHint(tokenVal());}}
function authHeaders(){var t=tokenVal();var h={'Content-Type':'application/json'};if(t)h['Authorization']='Bearer '+t;return h;}
function trigSummary(t){if(!t)return '—';if(t.type==='manual')return 'manual';
  if(t.type==='schedule')return t.cron?('cron '+t.cron):('every '+t.interval_seconds+'s');
  if(t.type==='event')return 'on '+(t.on_verb||'?')+' → '+(t.on_event||'executed');return t.type;}
function abtn(label,cls,onclick){return `<button class="btn tiny ${cls}" onclick="event.stopPropagation();${onclick}">${label}</button>`;}

async function loadAutomations(){
 try{
  const r=await fetch('/api/automations');const {automations}=await r.json();
  const wrap=document.getElementById('autoWrap'),box=document.getElementById('automations');
  document.getElementById('autocount').textContent=automations.length;
  wrap.style.display='block';  // always visible — the compose form + token live here, even with 0 automations
  if(!automations.length){box.innerHTML='<div class=empty style=padding:22px><div class=big>No automations yet</div><div>Click “＋ New cross-system automation” above to build one between two systems — or ask the agent via MCP.</div></div>';return;}
  box.innerHTML=automations.map(a=>{
   const nm=(a.name&&(a.name.en||a.name.ar))||a.automation_id;
   const ps=a.plan_summary||{};
   const shape=a.kind==='composed'?((ps.stages||0)+' stages · '+((ps.adapters||[]).join(', ')||'—')):((ps.nodes||0)+' steps');
   const st=a.state,rid=esc(a.workspace)+'-'+esc(a.automation_id);
   const acts=[];
   if(st==='pending_approval')acts.push(abtn('✓ Approve','ok',`autoState('${a.workspace}','${a.automation_id}',${a.version},'active')`));
   if(st==='active'){acts.push(abtn('⏸ Pause','ghost',`autoState('${a.workspace}','${a.automation_id}',${a.version},'paused')`));
     acts.push(abtn('▶ Run now','',`autoRun('${a.workspace}','${a.automation_id}')`));}
   if(st==='paused')acts.push(abtn('▶ Resume','ok',`autoState('${a.workspace}','${a.automation_id}',${a.version},'active')`));
   acts.push(abtn('↻ Runs','ghost',`toggleRuns('${a.workspace}','${a.automation_id}')`));
   return `<div class="autocard s-${esc(st)}">
     <div class=top><span class=nm>${esc(nm)}</span><span class="sbadge s-${esc(st)}">${esc(st)}</span></div>
     <div class=meta><span class=kind>${esc(a.kind)}</span><span class=trig>${esc(trigSummary(a.trigger))}</span>
       <span>v${a.version}</span><span>${esc(a.workspace)}</span><span>${esc(shape)}</span></div>
     <div class=sub>${esc((a.content_hash||'').slice(0,12))}…${a.approved_by?(' · approved by '+esc(a.approved_by)):''}</div>
     <div class=acts>${acts.join('')}</div>
     <div class=runs id="runs-${rid}"></div>
   </div>`;
  }).join('');
 }catch(_){}
}
async function autoState(ws,id,ver,state){
  if(!tokenVal())return toast('Enter the <b>operator token</b> above to control automations.');
  try{const r=await fetch(`/automations/${ws}/${id}/${ver}/state`,{method:'POST',headers:authHeaders(),body:JSON.stringify({state,approved_by:'operator'})});
    if(r.status===401)return toast('Operator token rejected.');
    toast(state==='active'?'Armed — the automation is now <b>active</b>.':state==='paused'?'Paused.':'Updated.');loadAutomations();
  }catch(_){toast('Could not update the automation.');}
}
async function autoRun(ws,id){
  if(!tokenVal())return toast('Enter the <b>operator token</b> above to run automations.');
  try{const r=await fetch(`/automations/${ws}/${id}/run`,{method:'POST',headers:authHeaders(),body:JSON.stringify({idempotency_key:'ui-'+Date.now()})});
    if(r.status===401)return toast('Operator token rejected.');const d=await r.json();
    toast(d.run?('Run <b>'+(d.run.state||'started')+'</b>.'):'Fired.');loadAutomations();openRuns(ws,id);
  }catch(_){toast('Could not run the automation.');}
}
function toggleRuns(ws,id){const el=document.getElementById('runs-'+ws+'-'+id);if(!el)return;
  if(el.classList.contains('open')){el.classList.remove('open');el.innerHTML='';}else openRuns(ws,id);}
async function openRuns(ws,id){
  const el=document.getElementById('runs-'+ws+'-'+id);if(!el)return;
  el.classList.add('open');el.innerHTML='<div class=runrow style=color:var(--faint)>loading…</div>';
  try{const r=await fetch(`/automations/${ws}/${id}/runs?limit=8`);const {runs}=await r.json();
    el.innerHTML=runs.length?runs.map(x=>`<div class=runrow><span class="rst ${esc(x.state)}">${esc(x.state)}</span>
      <span>${esc((x.fired_by||'').slice(0,20))}</span><span style=color:var(--faint)>${hhmmss(x.started_at)}</span></div>`).join('')
      :'<div class=runrow style=color:var(--faint)>no runs yet</div>';
  }catch(_){el.innerHTML='<div class=runrow>could not load runs</div>';}
}

// ── compose form: build a two-system automation in one click ────────────────────────────────────
let cfWs='';
function val(id){var e=document.getElementById(id);return e?e.value.trim():'';}
function toggleCompose(){const f=document.getElementById('composeForm');if(!f)return;
  if(f.style.display==='none'){f.style.display='block';populateCompose();}else f.style.display='none';}
async function populateCompose(){
  try{const d=await(await fetch('/api/registry')).json();cfWs=d.workspace||'';const ads=d.adapters||[];
   const opts='<option value="">— select adapter —</option>'+ads.map(a=>`<option value="${esc(a.adapter_id)}">${esc(a.label||a.adapter_id)}${a.system?(' ('+esc(a.system)+')'):''}</option>`).join('');
   ['cf_a1','cf_a2'].forEach(id=>{const s=document.getElementById(id);if(s)s.innerHTML=opts;});
   ['cf_v1','cf_v2'].forEach(id=>{const s=document.getElementById(id);if(s)s.innerHTML='<option value="">— pick adapter first —</option>';});
  }catch(_){}
}
async function loadVerbs(vsel,adapter){
  const s=document.getElementById(vsel);if(!s)return;
  if(!adapter){s.innerHTML='<option value="">— pick adapter first —</option>';return;}
  if(!tokenVal()){s.innerHTML='<option value="">operator token required</option>';return;}
  s.innerHTML='<option>loading…</option>';
  try{const r=await fetch(`/api/adapter-skeleton?workspace=${encodeURIComponent(cfWs)}&adapter_id=${encodeURIComponent(adapter)}`,{headers:authHeaders()});
    if(r.status===401){s.innerHTML='<option value="">token rejected</option>';return;}
    if(!r.ok){s.innerHTML='<option value="">adapter unreachable</option>';return;}
    const d=await r.json();const vs=d.verbs||[];
    s.innerHTML=vs.length?('<option value="">— select verb —</option>'+vs.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('')):'<option value="">no verbs</option>';
  }catch(_){s.innerHTML='<option value="">error</option>';}
}
async function submitCompose(){
  if(!tokenVal())return toast('Enter the <b>operator token</b> above first.');
  const id=val('cf_id'),nm=val('cf_name')||id,a1=val('cf_a1'),v1=val('cf_v1'),a2=val('cf_a2'),v2=val('cf_v2');
  if(!id||!a1||!v1||!a2||!v2)return toast('Need id + both adapters + both verbs.');
  let ar1={},ar2={};
  try{ar1=val('cf_args1')?JSON.parse(val('cf_args1')):{};ar2=val('cf_args2')?JSON.parse(val('cf_args2')):{};}
  catch(e){return toast('Args must be valid JSON.');}
  const stage=(n,ad,vb,ar)=>({name:n,adapter:ad,plan:{wosool:"0.1",workspace:cfWs,entry:"step_1",
    pipeline:[{id:"step_1",type:"action",skill:vb.split('.')[0],verb:vb,args:ar}]}});
  const s2=stage('stage_2',a2,v2,ar2);const hk=val('cf_hk'),hr=val('cf_hr');if(hk&&hr)s2.input_from={[hk]:hr};
  const body={automation_id:id,name:{en:nm,ar:nm},trigger:{type:"manual"},
    composed:{workspace:cfWs,stages:[stage('stage_1',a1,v1,ar1),s2]}};
  try{const r=await fetch('/automations/compose/register',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
    if(r.status===401)return toast('Operator token rejected.');
    const d=await r.json();
    if(!d.ok){const why=(d.report&&d.report.stages||[]).flatMap(s=>(s.diagnostics||[]).map(x=>x.code)).join(', ')||(d.report&&d.report.errors||[]).join(', ')||d.error||'refused';
      return toast('Refused: '+esc(why));}
    toast('Cross-system automation <b>registered</b> — pending approval.');toggleCompose();loadAutomations();
  }catch(_){toast('Could not create the automation.');}
}

initToken();tick();pend();loadAdapters();loadRouting();loadAutomations();applyThemeGlyph();
setInterval(()=>{tick();pend();loadAdapters();loadRouting();loadAutomations();},2000);
</script></body></html>"""


try:  # pragma: no cover - server entrypoint; prod mounts CP_DB_PATH's dir (e.g. /data volume)
    app = create_app()
except OSError:
    # No writable store dir at import (e.g. local test import without /data). The server process
    # in production constructs this successfully because the volume is mounted before boot.
    app = None  # type: ignore[assignment]

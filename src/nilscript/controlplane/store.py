"""SQLite-backed NIL event store — append-only audit, deduped by (workspace, sequence).

stdlib only (no external DB). Every NIL EVENT (proposed / executed / refused / rolled_back) from any
adapter lands here via the control-plane ingest, so MCP + playground + SDK actions share one timeline.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import threading
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT,
    received_at  TEXT    NOT NULL,
    workspace    TEXT    NOT NULL DEFAULT '',
    sequence     INTEGER,
    grant_id     TEXT    NOT NULL DEFAULT '',
    source       TEXT    NOT NULL DEFAULT '',
    performative TEXT    NOT NULL DEFAULT '',
    event        TEXT    NOT NULL DEFAULT '',
    proposal     TEXT,
    verb         TEXT,
    tier         TEXT,
    severity     TEXT,
    envelope     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_id ON events(id DESC);

CREATE TABLE IF NOT EXISTS approvals (
    proposal_id TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
    workspace   TEXT NOT NULL DEFAULT '',
    verb        TEXT,
    tier        TEXT,
    preview     TEXT,
    actor       TEXT,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

-- Active-adapter registry: which backend the hosted MCP routes to per workspace. This is the ONE
-- piece of mutable state the kernel keeps; `bearer` reaches the (tenant-owned) adapter, never the
-- backend's own creds. Activating one adapter deactivates its siblings in the same workspace.
CREATE TABLE IF NOT EXISTS adapters (
    workspace   TEXT    NOT NULL DEFAULT '',
    adapter_id  TEXT    NOT NULL,
    label       TEXT    NOT NULL DEFAULT '',
    url         TEXT    NOT NULL,
    bearer      TEXT    NOT NULL DEFAULT '',
    system      TEXT    NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (workspace, adapter_id)
);

-- Per-tenant secret vault: a company's adapter creds + LLM key, saved ONCE at onboarding. The value
-- is ENCRYPTED at rest (Fernet, master key from NIL_VAULT_KEY) — a leaked row is ciphertext, never
-- credentials. Keyed by workspace; the control-plane decrypts only to use, never echoes to the browser.
CREATE TABLE IF NOT EXISTS tenant_secrets (
    workspace   TEXT    NOT NULL PRIMARY KEY,
    ciphertext  BLOB    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- Automation Registry (SSOT): one row per VERSION of one automation. Append-only — "editing" an
-- automation writes a new version and archives the prior one (superseded_by). `content_hash` is the
-- version lock (sha256 over the validated plan). See docs/PLAN-dynamic-automation-ssot.md.
CREATE TABLE IF NOT EXISTS automations (
    workspace     TEXT    NOT NULL DEFAULT '',
    automation_id TEXT    NOT NULL,
    version       INTEGER NOT NULL,
    content_hash  TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'single',
    name          TEXT    NOT NULL DEFAULT '{}',
    description   TEXT,
    plan          TEXT    NOT NULL,
    source        TEXT,
    trigger       TEXT    NOT NULL,
    state         TEXT    NOT NULL DEFAULT 'draft',
    authored_by   TEXT    NOT NULL DEFAULT '',
    approved_by   TEXT,
    created_at    TEXT    NOT NULL,
    superseded_by INTEGER,
    PRIMARY KEY (workspace, automation_id, version)
);

-- Automation runs: one execution of one (pinned) automation version. `run_id` is deterministic
-- (automation:version:fire) so a re-delivered fire replays the same row, never double-executes.
CREATE TABLE IF NOT EXISTS automation_runs (
    run_id        TEXT    PRIMARY KEY,
    workspace     TEXT    NOT NULL DEFAULT '',
    automation_id TEXT    NOT NULL,
    version       INTEGER NOT NULL,
    content_hash  TEXT    NOT NULL,
    fired_by      TEXT    NOT NULL DEFAULT '',
    state         TEXT    NOT NULL DEFAULT 'running',
    trace         TEXT,
    started_at    TEXT    NOT NULL,
    ended_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_auto ON automation_runs(workspace, automation_id, started_at DESC);
"""

# Columns surfaced by the automation registry reads (JSON columns parsed back by `_automation_row`).
_AUTOMATION_COLS = (
    "workspace, automation_id, version, content_hash, kind, name, description, plan, source, "
    "trigger, state, authored_by, approved_by, created_at, superseded_by"
)

# Columns surfaced by the registry read methods (bearer included — the API layer redacts for the
# public list endpoint; `active_adapter` keeps it because the MCP needs it to reach the adapter).
_ADAPTER_COLS = "workspace, adapter_id, label, url, bearer, system, active, updated_at"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _loads(envelope: str | None) -> dict[str, Any]:
    """Best-effort parse of a stored envelope; a corrupt row must never crash the timeline."""
    if not envelope:
        return {}
    try:
        out = json.loads(envelope)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


def _verify_status(event: str | None, result: dict[str, Any]) -> str | None:
    """Field-level truth for the VERIFIED column — derived from `claim` + `ssot.unverified_fields`,
    NOT the bare `result.verified` flag (which reported success while country_id silently dropped).
    `verified` = the SSOT read-back matched the intent; `partial` = something didn't persist;
    `failed` = the write itself failed. None when the event carries no write result (e.g. proposed)."""
    if not result:
        return None
    claim = str(result.get("claim") or "").lower()
    unverified = (result.get("ssot") or {}).get("unverified_fields") or []
    if claim == "failure":
        return "failed"
    if unverified or claim == "partial":
        return "partial"
    if claim == "success":
        return "verified"
    # An executed write with a result but no explicit claim: the verified flag is a weak last resort.
    if event in ("executed", "rolled_back"):
        return "verified" if result.get("verified") else "partial"
    return None


class EventStore:
    """Thread-safe SQLite event log. `ingest` dedups by (workspace, sequence); `recent` reads newest-first."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or os.environ.get("CP_DB_PATH", "/data/controlplane.db")
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # The secret vault is enabled only when a master key is configured; otherwise secret storage
        # fails closed (a tenant's creds are never stored in plaintext as a fallback).
        self._vault = None
        try:
            from nilscript.secrets import SecretVault

            self._vault = (
                SecretVault.from_env()
            )  # NIL_VAULT_KEY; raises if unset → stays None
        except Exception:  # noqa: BLE001 — no/invalid key ⇒ vault disabled, not crashed
            self._vault = None
        with self._lock:
            self._conn.executescript(_DDL)
            # Existing DBs (volume) predate event_id — add it idempotently.
            try:
                self._conn.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already present
            try:
                self._conn.execute(
                    "ALTER TABLE automations ADD COLUMN kind TEXT NOT NULL DEFAULT 'single'"
                )
            except sqlite3.OperationalError:
                pass  # column already present (or table created fresh with it)
            try:
                # Cycle AST SSOT: kind='cycle' rows carry the canonical Cycle in `source` (the
                # `plan` is the derived, lowered WosoolProgram). NULL for plain automations.
                self._conn.execute("ALTER TABLE automations ADD COLUMN source TEXT")
            except sqlite3.OperationalError:
                pass  # column already present
            try:
                # SaaS isolation (root): a held proposal carries its OWN workspace (recorded by the
                # gate at hold-time), so per-tenant `pending` filters on it directly — no fragile join
                # to the ledger by proposal_id. Backfill of legacy rows is best-effort from events.
                self._conn.execute("ALTER TABLE approvals ADD COLUMN workspace TEXT NOT NULL DEFAULT ''")
                self._conn.execute(
                    "UPDATE approvals SET workspace = COALESCE((SELECT e.workspace FROM events e "
                    "WHERE e.proposal = approvals.proposal_id LIMIT 1), '') WHERE workspace = ''"
                )
            except sqlite3.OperationalError:
                pass  # column already present
            for col in ("resolved", "modifiable"):
                try:
                    # Editable decision cards: the gate records the proposal's resolved field values
                    # and which are editable, so the owner's card is a filled-in form and an
                    # approve-with-edits can re-propose exactly the tweaked args before commit.
                    self._conn.execute(f"ALTER TABLE approvals ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass  # column already present
            self._conn.commit()

    def ingest(
        self, envelope: dict[str, Any], sequence: int | None, *, source: str = ""
    ) -> bool:
        """Store one event. Returns False (no-op) if (workspace, sequence) was already seen."""
        body = envelope.get("body") or {}
        ws = envelope.get("workspace", "") or ""
        # Dedup by the globally-unique envelope id (stable across at-least-once retries). NOT by
        # (workspace, sequence): the adapter's sequence is in-memory and resets on restart, so that
        # key collides across restarts and silently drops fresh events.
        eid = envelope.get("id")
        with self._lock:
            if eid:
                if self._conn.execute(
                    "SELECT 1 FROM events WHERE event_id = ?", (eid,)
                ).fetchone():
                    return False
            elif sequence is not None:
                if self._conn.execute(
                    "SELECT 1 FROM events WHERE workspace = ? AND sequence = ?",
                    (ws, sequence),
                ).fetchone():
                    return False
            self._conn.execute(
                "INSERT INTO events (event_id, received_at, workspace, sequence, grant_id, source, "
                "performative, event, proposal, verb, tier, severity, envelope) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    _now(),
                    ws,
                    sequence,
                    envelope.get("grant", "") or "",
                    source,
                    envelope.get("performative", "") or "",
                    body.get("event", "") or "",
                    body.get("proposal"),
                    body.get("verb"),
                    body.get("tier"),
                    body.get("severity"),
                    json.dumps(envelope, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return True

    def recent(
        self, limit: int = 100, workspace: str | None = None
    ) -> list[dict[str, Any]]:
        # SaaS isolation: when a workspace is given, return ONLY that tenant's events. Pass None only
        # for the operator/global timeline.
        where = "WHERE workspace = ? " if workspace is not None else ""
        params: tuple[Any, ...] = (
            (workspace, max(1, min(limit, 1000)))
            if workspace is not None
            else (max(1, min(limit, 1000)),)
        )
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, received_at, workspace, sequence, grant_id, source, performative, "
                f"event, proposal, verb, tier, severity, envelope FROM events {where}ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        # An executed/refused event omits verb/tier and the human preview (those live on the
        # proposal). Pull them from each row's matching `proposed` event in ONE query so the timeline
        # can show the real verb, the tier, and a human one-liner (with the name) — not a bare id.
        pids = [r["proposal"] for r in rows if r["proposal"]]
        proposed: dict[str, dict[str, Any]] = {}
        if pids:
            uniq = list(dict.fromkeys(pids))
            ph = ",".join("?" * len(uniq))
            with self._lock:
                prows = self._conn.execute(
                    f"SELECT proposal, verb, tier, envelope FROM events "
                    f"WHERE event = 'proposed' AND proposal IN ({ph})",
                    uniq,
                ).fetchall()
            for pr in prows:
                prev: Any = {}
                try:
                    prev = (json.loads(pr["envelope"]).get("body") or {}).get(
                        "preview"
                    ) or {}
                except (ValueError, TypeError):
                    prev = {}
                proposed[pr["proposal"]] = {
                    "verb": pr["verb"],
                    "tier": pr["tier"],
                    "summary": prev.get("en") if isinstance(prev, dict) else None,
                }
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            envelope = record.pop("envelope", None)
            body: dict[str, Any] = {}
            if envelope:
                try:
                    body = json.loads(envelope).get("body") or {}
                except (ValueError, TypeError):
                    body = {}
            result = body.get("result") or {}
            entity = result.get("entity") or {}
            ssot = result.get("ssot") or {}
            comp = result.get("compensation") or {}
            # Surface the compensation handle for an executed write so the UI can offer a rollback
            # affordance on exactly the reversible rows (and nothing else).
            record["reversibility"] = comp.get("reversibility")
            record["compensation_token"] = comp.get("token")
            # Enrich the timeline with detail the envelope already carries but the indexed columns
            # miss: executed events omit `verb`/`tier` from the body (those live on the proposal),
            # so fall back to the result's entity type; and surface the backend + the affected entity
            # and a human one-liner so each row says WHAT happened, not just that something did.
            from_proposed = proposed.get(record.get("proposal") or "") or {}
            record["verb"] = (
                record.get("verb") or from_proposed.get("verb") or entity.get("type")
            )
            record["tier"] = record.get("tier") or from_proposed.get("tier")
            record["system"] = ssot.get("system")
            record["entity_id"] = entity.get("id")
            record["entity_url"] = entity.get("url")
            # Human one-liner, best→worst: this event's own preview, the proposal's preview (has the
            # name/value), else the affected entity path.
            preview = body.get("preview") or {}
            summary = (
                preview.get("en") if isinstance(preview, dict) else None
            ) or from_proposed.get("summary")
            if not summary and entity:
                eid = entity.get("id")
                summary = f"{entity.get('url') or entity.get('type') or ''}".strip(
                    "/"
                ) or (str(eid) if eid else None)
            record["summary"] = summary
            record["args"] = body.get("args") or None
            # The headline column: did the intent actually land in the SSOT, field for field?
            record["verify"] = _verify_status(record.get("event"), result)
            out.append(record)
        return out

    def detail(self, event_id: int) -> dict[str, Any] | None:
        """The full payload journey for one event — raw intent → resolved values → field-level SSOT
        verdict → effect — assembled from its own envelope plus its proposal's `proposed` event and
        every sibling event for that proposal. Everything needed to reconstruct a (failed) action
        from the log alone, without opening the backend or the logs. Returns None for an unknown id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, received_at, workspace, grant_id, source, performative, event, proposal, "
                "verb, tier, severity, envelope FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        env = _loads(row["envelope"])
        body = env.get("body") or {}
        result = body.get("result") or {}
        proposal_id = row["proposal"]
        # The proposal carries the intent the executed event omits: raw args, resolved values,
        # preview, expiry, and which args were ignored. Walk every event on this proposal to show
        # the saga (proposed → executed/refused → rolled_back) as one ordered thread.
        proposed_body: dict[str, Any] = {}
        journey: list[dict[str, Any]] = []
        if proposal_id:
            with self._lock:
                prows = self._conn.execute(
                    "SELECT id, received_at, event, envelope FROM events WHERE proposal = ? ORDER BY id",
                    (proposal_id,),
                ).fetchall()
            for pr in prows:
                pbody = _loads(pr["envelope"]).get("body") or {}
                if pr["event"] == "proposed" and not proposed_body:
                    proposed_body = pbody
                journey.append(
                    {
                        "id": pr["id"],
                        "event": pr["event"],
                        "received_at": pr["received_at"],
                        "replayed": pbody.get("replayed"),
                    }
                )
        resolved = proposed_body.get("resolved") or {}
        ssot = result.get("ssot") or {}
        # Field-level diff: prefer the adapter's emitted before→after read-back (the real prior value,
        # the requested value, and what actually LANDED in the SSOT). `verified=False` is exactly the
        # silent drop (country_id) the green row used to hide. Older adapters emit only the dropped
        # field names — fall back to the proposal's resolved values (no before/after available).
        emitted = ssot.get("fields")
        if emitted:
            fields = [
                {
                    "field": f.get("field"),
                    "before": f.get("before"),
                    "requested": f.get("requested"),
                    "after": f.get("after"),
                    "verified": bool(f.get("verified")),
                }
                for f in emitted
            ]
        else:
            unverified = set(ssot.get("unverified_fields") or [])
            fields = [
                {"field": k, "requested": v, "verified": k not in unverified}
                for k, v in resolved.items()
            ]
        code = body.get("code")
        return {
            "id": row["id"],
            "received_at": row["received_at"],
            "workspace": row["workspace"],
            "grant_id": row["grant_id"],
            "source": row["source"],
            "event": row["event"],
            "verb": row["verb"] or proposed_body.get("verb"),
            "tier": row["tier"] or proposed_body.get("tier"),
            "verify": _verify_status(row["event"], result),
            "preview": proposed_body.get("preview") or body.get("preview") or None,
            "raw_args": body.get("args") or proposed_body.get("args") or {},
            "resolved": resolved,
            "ignored": proposed_body.get("ignored") or None,
            "expires_at": proposed_body.get("expires_at"),
            "refusal": {
                "code": code,
                "message": body.get("message"),
                "field": body.get("field"),
            }
            if code
            else None,
            "result": result or None,
            "fields": fields,
            "journey": journey,
            "raw": {"event": env, "proposed": proposed_body or None},
        }

    def count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            )

    def adapters(self, limit: int = 800) -> list[dict[str, Any]]:
        """The distinct adapters/backends active in the timeline, derived purely from the audit log
        (no separate registry). Keyed by the backend's `system` (from an executed event's ssot) when
        known, else by the emitting source. Each carries event counts, channels, verb namespaces,
        and last-seen — so the single pane also answers 'what's linked, and is it live?'."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT received_at, source, event, performative, verb, envelope "
                "FROM events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 2000)),),
            ).fetchall()
        # Parse each row once; an executed event names the backend `system`, so map proposal→system
        # to fold a proposal's other events (proposed/refused) into the same adapter, not a phantom.
        parsed: list[dict[str, Any]] = []
        proposal_system: dict[str, str] = {}
        for row in rows:
            rec = dict(row)
            system, proposal = None, None
            try:
                body = json.loads(rec["envelope"]).get("body") or {}
                proposal = body.get("proposal")
                system = ((body.get("result") or {}).get("ssot") or {}).get("system")
            except (ValueError, TypeError):
                pass
            if proposal and system:
                proposal_system[proposal] = system
            parsed.append({**rec, "_system": system, "_proposal": proposal})

        agg: dict[str, dict[str, Any]] = {}
        for rec in parsed:
            system = rec["_system"] or proposal_system.get(rec["_proposal"])
            source = rec.get("source") or "?"
            key = system or source
            entry = agg.setdefault(
                key,
                {
                    "adapter": key,
                    "system": system,
                    "sources": set(),
                    "events": 0,
                    "last_seen": rec["received_at"],
                    "by_event": {},
                    "namespaces": set(),
                },
            )
            entry["events"] += 1
            entry["sources"].add(source)
            if system and not entry["system"]:
                entry["system"] = system
            ev = rec.get("event") or rec.get("performative") or ""
            entry["by_event"][ev] = entry["by_event"].get(ev, 0) + 1
            verb = rec.get("verb") or ""
            if "." in verb:
                entry["namespaces"].add(verb.split(".", 1)[0])
            if rec["received_at"] > entry["last_seen"]:
                entry["last_seen"] = rec["received_at"]
        out = [
            {
                **e,
                "sources": sorted(e["sources"]),
                "namespaces": sorted(e["namespaces"]),
            }
            for e in agg.values()
        ]
        out.sort(key=lambda e: e["last_seen"], reverse=True)
        return out

    # ── human-approval gate (Phase 2) ────────────────────────────────────────────────────────
    def _enrich(self, proposal_id: str) -> dict[str, Any]:
        """Pull verb/tier/preview from the proposal's 'proposed' event (the control plane already
        received it from the adapter), so the approval card shows the full intent."""
        row = self._conn.execute(
            "SELECT verb, tier, envelope FROM events WHERE proposal = ? AND event = 'proposed' "
            "ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return {"verb": None, "tier": None, "preview": None}
        preview = None
        try:
            preview = json.dumps(
                (json.loads(row["envelope"]).get("body") or {}).get("preview")
            )
        except (ValueError, TypeError):
            preview = None
        return {"verb": row["verb"], "tier": row["tier"], "preview": preview}

    def await_approval(
        self,
        proposal_id: str,
        *,
        verb: str | None = None,
        tier: str | None = None,
        preview: Any = None,
        workspace: str = "",
        resolved: Any = None,
        modifiable: Any = None,
    ) -> dict[str, Any]:
        """Register a proposal as awaiting human approval (idempotent — keeps an existing decision).

        `verb`/`tier`/`preview` are passed by the gate at hold-time (a held proposal has no ledger
        event yet, so `_enrich` finds nothing). They win over enrichment; `preview` (a dict) is stored
        as JSON so the owner's Decisions screen can show exactly what the proposal does. `resolved`
        (the field values) + `modifiable` (which keys are editable) drive the editable decision card."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT status FROM approvals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                return {"proposal_id": proposal_id, "status": existing["status"]}
            meta = self._enrich(proposal_id)
            preview_str = (
                json.dumps(preview)
                if isinstance(preview, (dict, list))
                else (preview if preview is not None else meta["preview"])
            )
            self._conn.execute(
                "INSERT INTO approvals "
                "(proposal_id, status, workspace, verb, tier, preview, resolved, modifiable, created_at) "
                "VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal_id,
                    workspace or "",
                    verb or meta["verb"],
                    tier or meta["tier"],
                    preview_str,
                    json.dumps(resolved) if resolved is not None else None,
                    json.dumps(list(modifiable)) if modifiable is not None else None,
                    _now(),
                ),
            )
            self._conn.commit()
        return {"proposal_id": proposal_id, "status": "pending"}

    def decision(self, proposal_id: str) -> str:
        """'pending' | 'approved' | 'rejected' | 'unknown' (never registered)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM approvals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return row["status"] if row is not None else "unknown"

    def decide(
        self, proposal_id: str, status: str, *, actor: str = "", reason: str = ""
    ) -> bool:
        """Owner decision. Only transitions a 'pending' row; returns False otherwise (idempotent/guarded)."""
        if status not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status = ?, actor = ?, reason = ?, decided_at = ? "
                "WHERE proposal_id = ? AND status = 'pending'",
                (status, actor, reason, _now(), proposal_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def pending(self, workspace: str | None = None) -> list[dict[str, Any]]:
        # SaaS isolation (root fix): each held proposal carries its OWN `workspace`, recorded by the
        # gate at hold-time, so a tenant sees only its holds by a DIRECT column filter — no fragile
        # join to the ledger by proposal_id. The legacy events-join is kept ONLY as a fallback for
        # pre-migration rows whose workspace is still ''. None = operator/global view.
        cols = "proposal_id, verb, tier, preview, resolved, modifiable, created_at"
        if workspace is None:
            sql = (
                f"SELECT {cols} FROM approvals "
                "WHERE status = 'pending' ORDER BY created_at DESC"
            )
            params: tuple[Any, ...] = ()
        else:
            sql = (
                f"SELECT {cols} FROM approvals a "
                "WHERE a.status = 'pending' AND ("
                "  a.workspace = ?"
                "  OR (a.workspace = '' AND EXISTS (SELECT 1 FROM events e "
                "      WHERE e.proposal = a.proposal_id AND e.workspace = ?))"
                ") ORDER BY a.created_at DESC"
            )
            params = (workspace, workspace)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._shape_pending(r) for r in rows]

    @staticmethod
    def _shape_pending(row: Any) -> dict[str, Any]:
        """Decode a pending row, exposing `resolved`/`modifiable` as real JSON for the editable card."""
        rec = dict(row)
        for key in ("resolved", "modifiable"):
            raw = rec.get(key)
            if isinstance(raw, str) and raw:
                try:
                    rec[key] = json.loads(raw)
                except json.JSONDecodeError:
                    rec[key] = None
        return rec

    # ── active-adapter registry (multi-tenant routing) ───────────────────────────────────────
    def register_adapter(
        self,
        workspace: str,
        adapter_id: str,
        *,
        label: str = "",
        url: str,
        bearer: str = "",
        system: str = "",
    ) -> dict[str, Any]:
        """Upsert an adapter the MCP can route to. Re-registering updates its coordinates but
        PRESERVES the active flag (so refreshing a bearer doesn't silently flip routing off)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO adapters (workspace, adapter_id, label, url, bearer, system, active, updated_at) "
                "VALUES (?,?,?,?,?,?,0,?) "
                "ON CONFLICT(workspace, adapter_id) DO UPDATE SET "
                "label=excluded.label, url=excluded.url, bearer=excluded.bearer, "
                "system=excluded.system, updated_at=excluded.updated_at",
                (workspace, adapter_id, label, url, bearer, system, _now()),
            )
            self._conn.commit()
        return self._adapter(workspace, adapter_id) or {}

    # ── per-tenant secret vault (encrypted at rest) ──────────────────────────────────────────────
    @property
    def vault_enabled(self) -> bool:
        return self._vault is not None

    def put_secrets(self, workspace: str, secrets: dict[str, Any]) -> None:
        """Save (replace) a workspace's secret bundle, ENCRYPTED. Raises if the vault is unconfigured —
        the platform never stores a tenant's creds in plaintext as a fallback."""
        if self._vault is None:
            raise RuntimeError(
                "secret vault disabled — set NIL_VAULT_KEY to store tenant secrets"
            )
        if not workspace:
            raise ValueError("workspace is required")
        blob = self._vault._fernet.encrypt(  # encrypt here, persist ciphertext (vault store is the DB)
            json.dumps(secrets, separators=(",", ":")).encode("utf-8")
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO tenant_secrets (workspace, ciphertext, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(workspace) DO UPDATE SET ciphertext=excluded.ciphertext, "
                "updated_at=excluded.updated_at",
                (workspace, blob, _now()),
            )
            self._conn.commit()

    def get_secrets(self, workspace: str) -> dict[str, Any] | None:
        """Decrypt a workspace's secret bundle (by-tenant only), or None. Raises on tamper/wrong key."""
        if self._vault is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT ciphertext FROM tenant_secrets WHERE workspace = ?",
                (workspace,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(
            self._vault._fernet.decrypt(row["ciphertext"]).decode("utf-8")
        )

    def get_secret(self, workspace: str, name: str) -> Any | None:
        bundle = self.get_secrets(workspace)
        return bundle.get(name) if bundle else None

    def delete_secrets(self, workspace: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM tenant_secrets WHERE workspace = ?", (workspace,)
            )
            self._conn.commit()

    def activate_adapter(self, workspace: str, adapter_id: str) -> bool:
        """Make `adapter_id` the active backend for `workspace`, deactivating its siblings.
        Returns False if no such adapter is registered (so the caller can 404)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM adapters WHERE workspace = ? AND adapter_id = ?",
                (workspace, adapter_id),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE adapters SET active = CASE WHEN adapter_id = ? THEN 1 ELSE 0 END, "
                "updated_at = ? WHERE workspace = ?",
                (adapter_id, _now(), workspace),
            )
            self._conn.commit()
        return True

    def set_adapter_active(
        self, workspace: str, adapter_id: str, enabled: bool
    ) -> bool:
        """Enable/disable ONE adapter without touching its siblings (non-exclusive). This is what lets
        a workspace have several adapters active at once — e.g. PocketBase + Odoo for a cross-system
        automation. Returns False if no such adapter is registered."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE adapters SET active = ?, updated_at = ? WHERE workspace = ? AND adapter_id = ?",
                (1 if enabled else 0, _now(), workspace, adapter_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def active_adapter(self, workspace: str) -> dict[str, Any] | None:
        """The workspace's default active adapter for single-backend MCP routing (WITH bearer), or
        None. With several active, the most-recently-updated wins — composition addresses adapters by
        id, so multi-active never makes a plain propose/commit ambiguous."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_ADAPTER_COLS} FROM adapters WHERE workspace = ? AND active = 1 "
                "ORDER BY updated_at DESC LIMIT 1",
                (workspace,),
            ).fetchone()
        return dict(row) if row is not None else None

    def any_active_adapter(self) -> dict[str, Any] | None:
        """The single most-recently-active adapter across the whole registry (WITH bearer). Used by the
        approval executor: in a single-workspace deployment the held proposal was proposed on whatever
        backend is active, so committing the approved proposal there is correct."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_ADAPTER_COLS} FROM adapters WHERE active = 1 ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def proposal_workspace(self, proposal_id: str) -> str | None:
        """Derive the workspace for a proposal_id from its 'proposed' event. Returns None when no
        matching event exists (unknown proposal or event not yet ingested)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT workspace FROM events WHERE proposal = ? AND event = 'proposed' ORDER BY id DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
        return row["workspace"] if row is not None else None

    def approval(self, proposal_id: str) -> dict[str, Any] | None:
        """The full approval row (verb/tier/preview/status) — the executor reads the verb to scope the
        control-plane grant when it commits the approved proposal."""
        with self._lock:
            row = self._conn.execute(
                "SELECT proposal_id, status, verb, tier, preview, resolved, modifiable "
                "FROM approvals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return self._shape_pending(row) if row is not None else None

    def list_adapters(self, workspace: str) -> list[dict[str, Any]]:
        """All registered adapters for a workspace (active first, then most-recent). Carries the
        bearer — the API layer redacts it for the public list endpoint."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_ADAPTER_COLS} FROM adapters WHERE workspace = ? "
                "ORDER BY active DESC, updated_at DESC",
                (workspace,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _adapter(self, workspace: str, adapter_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {_ADAPTER_COLS} FROM adapters WHERE workspace = ? AND adapter_id = ?",
            (workspace, adapter_id),
        ).fetchone()
        return dict(row) if row is not None else None

    # ── automation registry (SSOT, append-only versions) ─────────────────────────────────────
    @staticmethod
    def _automation_row(row: sqlite3.Row) -> dict[str, Any]:
        """Deserialize the JSON columns (name/description/plan/trigger) back into dicts."""
        rec = dict(row)
        rec["name"] = _loads(rec.get("name"))
        rec["plan"] = _loads(rec.get("plan"))
        rec["trigger"] = _loads(rec.get("trigger"))
        rec["description"] = (
            _loads(rec["description"]) if rec.get("description") else None
        )
        # `source` is the canonical Cycle AST for kind='cycle'; None for plain automations.
        rec["source"] = _loads(rec["source"]) if rec.get("source") else None
        return rec

    def register_automation(
        self,
        *,
        workspace: str,
        automation_id: str,
        content_hash: str,
        name: dict[str, Any],
        plan: dict[str, Any],
        trigger: dict[str, Any],
        state: str = "draft",
        kind: str = "single",
        authored_by: str = "",
        description: dict[str, Any] | None = None,
        approved_by: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a new version. Re-registering an identical plan (same `content_hash` as the latest
        version) is an idempotent no-op — returns the existing row, no new version. Otherwise the
        prior latest version is archived and marked `superseded_by` the new version."""
        with self._lock:
            latest = self._conn.execute(
                "SELECT version, content_hash FROM automations "
                "WHERE workspace = ? AND automation_id = ? ORDER BY version DESC LIMIT 1",
                (workspace, automation_id),
            ).fetchone()
            if latest is not None and latest["content_hash"] == content_hash:
                row = self._conn.execute(
                    f"SELECT {_AUTOMATION_COLS} FROM automations "
                    "WHERE workspace = ? AND automation_id = ? AND version = ?",
                    (workspace, automation_id, latest["version"]),
                ).fetchone()
                return self._automation_row(row)
            version = (latest["version"] + 1) if latest is not None else 1
            if latest is not None:
                self._conn.execute(
                    "UPDATE automations SET superseded_by = ?, state = 'archived' "
                    "WHERE workspace = ? AND automation_id = ? AND version = ?",
                    (version, workspace, automation_id, latest["version"]),
                )
            self._conn.execute(
                "INSERT INTO automations (workspace, automation_id, version, content_hash, kind, name, "
                "description, plan, source, trigger, state, authored_by, approved_by, created_at, superseded_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    workspace,
                    automation_id,
                    version,
                    content_hash,
                    kind,
                    json.dumps(name, ensure_ascii=False),
                    json.dumps(description, ensure_ascii=False)
                    if description is not None
                    else None,
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps(source, ensure_ascii=False) if source is not None else None,
                    json.dumps(trigger, ensure_ascii=False),
                    state,
                    authored_by,
                    approved_by,
                    _now(),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {_AUTOMATION_COLS} FROM automations "
                "WHERE workspace = ? AND automation_id = ? AND version = ?",
                (workspace, automation_id, version),
            ).fetchone()
        return self._automation_row(row)

    def get_automation(
        self, workspace: str, automation_id: str, version: int | None = None
    ) -> dict[str, Any] | None:
        """A specific version, or the latest (highest version) when `version` is None."""
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    f"SELECT {_AUTOMATION_COLS} FROM automations "
                    "WHERE workspace = ? AND automation_id = ? ORDER BY version DESC LIMIT 1",
                    (workspace, automation_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    f"SELECT {_AUTOMATION_COLS} FROM automations "
                    "WHERE workspace = ? AND automation_id = ? AND version = ?",
                    (workspace, automation_id, version),
                ).fetchone()
        return self._automation_row(row) if row is not None else None

    def list_automations(self, workspace: str) -> list[dict[str, Any]]:
        """The latest version of every automation in the workspace, by automation_id."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_AUTOMATION_COLS} FROM automations a WHERE workspace = ? AND version = "
                "(SELECT MAX(version) FROM automations b "
                " WHERE b.workspace = a.workspace AND b.automation_id = a.automation_id) "
                "ORDER BY automation_id",
                (workspace,),
            ).fetchall()
        return [self._automation_row(r) for r in rows]

    def all_automations(self) -> list[dict[str, Any]]:
        """Latest version of every automation across ALL workspaces — for the dashboard."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_AUTOMATION_COLS} FROM automations a WHERE version = "
                "(SELECT MAX(version) FROM automations b "
                " WHERE b.workspace = a.workspace AND b.automation_id = a.automation_id) "
                "ORDER BY workspace, automation_id"
            ).fetchall()
        return [self._automation_row(r) for r in rows]

    def active_automations(self) -> list[dict[str, Any]]:
        """Every armed automation across all workspaces (state='active'). The scheduler scans these.
        The supersede-on-edit invariant means the active version is always the latest one."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_AUTOMATION_COLS} FROM automations WHERE state = 'active' "
                "ORDER BY workspace, automation_id"
            ).fetchall()
        return [self._automation_row(r) for r in rows]

    def set_automation_state(
        self,
        workspace: str,
        automation_id: str,
        version: int,
        state: str,
        *,
        approved_by: str | None = None,
    ) -> bool:
        """Transition one version's lifecycle state (draft→pending_approval→active⇄paused→archived).
        Records `approved_by` when supplied. Returns False if no such version exists."""
        with self._lock:
            if approved_by is not None:
                cur = self._conn.execute(
                    "UPDATE automations SET state = ?, approved_by = ? "
                    "WHERE workspace = ? AND automation_id = ? AND version = ?",
                    (state, approved_by, workspace, automation_id, version),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE automations SET state = ? "
                    "WHERE workspace = ? AND automation_id = ? AND version = ?",
                    (state, workspace, automation_id, version),
                )
            self._conn.commit()
            return cur.rowcount > 0

    # ── automation runs (P2 dispatcher) ──────────────────────────────────────────────────────
    def start_run(
        self,
        run_id: str,
        *,
        workspace: str,
        automation_id: str,
        version: int,
        content_hash: str,
        fired_by: str = "",
    ) -> bool:
        """Open a run row (state=running). Returns False if `run_id` already exists — the caller must
        then treat the fire as an idempotent replay and NOT re-execute (a re-delivered trigger)."""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone():
                return False
            self._conn.execute(
                "INSERT INTO automation_runs (run_id, workspace, automation_id, version, "
                "content_hash, fired_by, state, started_at) VALUES (?,?,?,?,?,?, 'running', ?)",
                (
                    run_id,
                    workspace,
                    automation_id,
                    version,
                    content_hash,
                    fired_by,
                    _now(),
                ),
            )
            self._conn.commit()
        return True

    def finish_run(self, run_id: str, state: str, trace: dict[str, Any] | None) -> bool:
        """Close a run with its terminal state and the executor trace. Returns False if unknown."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE automation_runs SET state = ?, trace = ?, ended_at = ? WHERE run_id = ?",
                (
                    state,
                    json.dumps(trace, ensure_ascii=False)
                    if trace is not None
                    else None,
                    _now(),
                    run_id,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, workspace, automation_id, version, content_hash, fired_by, state, "
                "trace, started_at, ended_at FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        rec["trace"] = _loads(rec.get("trace")) if rec.get("trace") else None
        return rec

    def list_runs(
        self, workspace: str, automation_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Newest-first run history for one automation (trace omitted — fetch via get_run)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, workspace, automation_id, version, content_hash, fired_by, state, "
                "started_at, ended_at FROM automation_runs "
                "WHERE workspace = ? AND automation_id = ? ORDER BY started_at DESC LIMIT ?",
                (workspace, automation_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(r) for r in rows]

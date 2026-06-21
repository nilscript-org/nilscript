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
"""

# Columns surfaced by the registry read methods (bearer included — the API layer redacts for the
# public list endpoint; `active_adapter` keeps it because the MCP needs it to reach the adapter).
_ADAPTER_COLS = "workspace, adapter_id, label, url, bearer, system, active, updated_at"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


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
        with self._lock:
            self._conn.executescript(_DDL)
            # Existing DBs (volume) predate event_id — add it idempotently.
            try:
                self._conn.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already present
            self._conn.commit()

    def ingest(self, envelope: dict[str, Any], sequence: int | None, *, source: str = "") -> bool:
        """Store one event. Returns False (no-op) if (workspace, sequence) was already seen."""
        body = envelope.get("body") or {}
        ws = envelope.get("workspace", "") or ""
        # Dedup by the globally-unique envelope id (stable across at-least-once retries). NOT by
        # (workspace, sequence): the adapter's sequence is in-memory and resets on restart, so that
        # key collides across restarts and silently drops fresh events.
        eid = envelope.get("id")
        with self._lock:
            if eid:
                if self._conn.execute("SELECT 1 FROM events WHERE event_id = ?", (eid,)).fetchone():
                    return False
            elif sequence is not None:
                if self._conn.execute(
                    "SELECT 1 FROM events WHERE workspace = ? AND sequence = ?", (ws, sequence)
                ).fetchone():
                    return False
            self._conn.execute(
                "INSERT INTO events (event_id, received_at, workspace, sequence, grant_id, source, "
                "performative, event, proposal, verb, tier, severity, envelope) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid, _now(), ws, sequence, envelope.get("grant", "") or "", source,
                    envelope.get("performative", "") or "", body.get("event", "") or "",
                    body.get("proposal"), body.get("verb"), body.get("tier"), body.get("severity"),
                    json.dumps(envelope, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return True

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT received_at, workspace, sequence, grant_id, source, performative, "
                "event, proposal, verb, tier, severity, envelope FROM events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            envelope = record.pop("envelope", None)
            # Surface the compensation handle for an executed write so the UI can offer a rollback
            # affordance on exactly the reversible rows (and nothing else).
            reversibility, token = None, None
            if envelope:
                try:
                    comp = (
                        ((json.loads(envelope).get("body") or {}).get("result") or {}).get("compensation") or {}
                    )
                    reversibility = comp.get("reversibility")
                    token = comp.get("token")
                except (ValueError, TypeError):
                    pass
            record["reversibility"] = reversibility
            record["compensation_token"] = token
            out.append(record)
        return out

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])

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
                system = (((body.get("result") or {}).get("ssot") or {}).get("system"))
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
            entry = agg.setdefault(key, {
                "adapter": key, "system": system, "sources": set(), "events": 0,
                "last_seen": rec["received_at"], "by_event": {}, "namespaces": set(),
            })
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
            {**e, "sources": sorted(e["sources"]), "namespaces": sorted(e["namespaces"])}
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
            preview = json.dumps((json.loads(row["envelope"]).get("body") or {}).get("preview"))
        except (ValueError, TypeError):
            preview = None
        return {"verb": row["verb"], "tier": row["tier"], "preview": preview}

    def await_approval(self, proposal_id: str) -> dict[str, Any]:
        """Register a proposal as awaiting human approval (idempotent — keeps an existing decision)."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT status FROM approvals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                return {"proposal_id": proposal_id, "status": existing["status"]}
            meta = self._enrich(proposal_id)
            self._conn.execute(
                "INSERT INTO approvals (proposal_id, status, verb, tier, preview, created_at) "
                "VALUES (?, 'pending', ?, ?, ?, ?)",
                (proposal_id, meta["verb"], meta["tier"], meta["preview"], _now()),
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

    def decide(self, proposal_id: str, status: str, *, actor: str = "", reason: str = "") -> bool:
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

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT proposal_id, verb, tier, preview, created_at FROM approvals "
                "WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── active-adapter registry (multi-tenant routing) ───────────────────────────────────────
    def register_adapter(
        self, workspace: str, adapter_id: str, *,
        label: str = "", url: str, bearer: str = "", system: str = "",
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

    def active_adapter(self, workspace: str) -> dict[str, Any] | None:
        """The workspace's active adapter (WITH bearer — the MCP needs it), or None."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_ADAPTER_COLS} FROM adapters WHERE workspace = ? AND active = 1 LIMIT 1",
                (workspace,),
            ).fetchone()
        return dict(row) if row is not None else None

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

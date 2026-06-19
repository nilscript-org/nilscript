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
"""


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
                "event, proposal, verb, tier, severity FROM events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])

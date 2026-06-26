"""export → data-handle subsystem: stream a bulk read to a tenant-scoped artifact on disk and return a
small HANDLE. The rows never enter the agent's context — they are reached only through code in the
sandbox (pandas/DuckDB/sqlite). Handles are tenant-scoped (no cross-tenant read), TTL-expiring, and
PII-at-rest (sandbox-local file, never logged).

This is what makes "give me ALL the data / analyse all 1M rows" possible without a flood: the only
thing that crosses into context is `{handle, format, rows, bytes, schema, expires_at}`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class HandleExpired(Exception):
    """The export artifact's TTL has passed. Surfaced as `HANDLE_EXPIRED` — re-export to get fresh."""

    code = "HANDLE_EXPIRED"


class NotAuthorizedHandle(Exception):
    """A tenant tried to open a handle it does not own. Surfaced as `NOT_AUTHORIZED` — handles never
    cross tenant boundaries (the bulk-export artifact is PII at rest)."""

    code = "NOT_AUTHORIZED"


@dataclass(frozen=True)
class ExportHandle:
    """The small pointer that crosses into context in place of the rows."""

    handle: str
    format: str
    rows: int
    bytes: int
    schema: dict[str, Any]
    expires_at: datetime


class ExportStore:
    """Materialises bulk reads to per-tenant artifacts and serves them back, access-controlled."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._meta: dict[str, dict[str, Any]] = {}

    def _path(self, tenant: str, handle: str) -> Path:
        tdir = self._root / tenant
        tdir.mkdir(parents=True, exist_ok=True)
        return tdir / f"{handle}.{self._meta[handle]['format']}"

    def write(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        fmt: str,
        schema: dict[str, Any],
        tenant: str,
        now: datetime,
        ttl_seconds: int,
    ) -> ExportHandle:
        """Stream `rows` to a tenant-scoped JSONL artifact; return a small handle (never the rows)."""
        handle = uuid.uuid4().hex
        self._meta[handle] = {"format": fmt, "tenant": tenant}
        path = self._path(tenant, handle)
        count = 0
        size = 0
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:  # streamed: one row in memory at a time, never the whole set
                line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                fh.write(line + "\n")
                count += 1
                size += len(line.encode("utf-8")) + 1
        expires_at = now + timedelta(seconds=ttl_seconds)
        self._meta[handle].update({"rows": count, "bytes": size, "expires_at": expires_at})
        return ExportHandle(
            handle=handle, format=fmt, rows=count, bytes=size, schema=schema, expires_at=expires_at
        )

    def open(self, handle: str, *, tenant: str, now: datetime) -> Iterator[dict[str, Any]]:
        """Stream the artifact's rows back to the OWNING tenant, refusing a foreign tenant or an
        expired handle. Used by the sandbox bridge to land the file for code execution."""
        meta = self._meta.get(handle)
        if meta is None or meta["tenant"] != tenant:
            raise NotAuthorizedHandle(f"handle {handle} is not readable by tenant {tenant}")
        if now >= meta["expires_at"]:
            raise HandleExpired(f"handle {handle} expired at {meta['expires_at'].isoformat()}")
        path = self._path(tenant, handle)
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

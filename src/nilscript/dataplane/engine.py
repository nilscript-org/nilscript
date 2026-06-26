"""The universal ReadPlane engine: the governed read verbs (count/search/get/aggregate) implemented
ONCE over a backend protocol. Every adapter that exposes the small native surface (`describe_target`,
`fetch`, `count`, `aggregate`) inherits projection, byte-cap-refuse, capability fallback, and read-side
authorization — so no adapter can re-introduce the 590 KB flood by hand-rolling its own reads.

The engine owns the *governance*; the backend owns the *native I/O*. Capabilities the backend lacks are
degraded honestly (edge-side filter within a bound, or refuse) — never by silently fetching everything.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from datetime import datetime

from . import (
    BYTE_CAP,
    Predicate,
    ResultTooLarge,
    enforce_byte_cap,
    parse_filter,
    project,
    project_items,
)
from .export import ExportHandle, ExportStore

# How many rows the edge will pull to filter/count in-memory when the backend can't do it server-side.
# Beyond this the read is refused (never an unbounded fetch-all). Tunable per ReadPlane.
EDGE_FILTER_BOUND = 10_000

# Above this row estimate, an export is a deliberate BULK extraction — gated (propose->approve) and
# audited, not a free read. This is what closes the "reads are free" exfiltration hole.
BULK_THRESHOLD = 10_000

# Rows pulled per page when streaming an export to disk (keyset-cursored; never the whole set at once).
EXPORT_PAGE_SIZE = 1_000


class BulkApprovalRequired(Exception):
    """A bulk export exceeds the free-read threshold and needs explicit approval before it runs.
    Surfaced as `BULK_APPROVAL_REQUIRED` — extraction of the customer base is never a silent side
    effect of a 'free' read."""

    code = "BULK_APPROVAL_REQUIRED"

    def __init__(self, estimate: int, threshold: int) -> None:
        self.estimate = estimate
        self.threshold = threshold
        self.message = (
            f"export would extract ~{estimate} rows (over the {threshold}-row bulk threshold) — "
            f"this requires approval and is audited"
        )
        super().__init__(self.message)


class CapabilityUnsupported(Exception):
    """The backend cannot perform a requested operation server-side and there is no safe edge fallback
    (e.g. aggregate over an ungroupable backend). Surfaced as `CAPABILITY_UNSUPPORTED` → use export."""

    code = "CAPABILITY_UNSUPPORTED"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Capabilities:
    """What a backend can do server-side. Absent capabilities drive the engine's degradation paths."""

    server_filter: bool = True
    server_sort: bool = True
    server_paginate: bool = True
    server_aggregate: bool = True


@dataclass(frozen=True)
class FieldSpec:
    """One queryable field's shape + policy — what the agent needs to query correctly, and what the
    engine needs to enforce projection and read authorization."""

    name: str
    type: str
    filterable: bool = True
    sortable: bool = True
    returnable: bool = True
    is_key: bool = False
    sensitivity: str = "normal"  # "normal" | "sensitive"


@dataclass(frozen=True)
class TargetSchema:
    """The queryable shape of one entity: fields, cardinality class, default lean projection, and the
    backend capability profile. This is what `schema(target)` returns and `/describe` advertises."""

    target: str
    fields: tuple[FieldSpec, ...]
    cardinality: str  # "small" | "large" | "huge"
    default_projection: tuple[str, ...]
    capabilities: Capabilities = field(default_factory=Capabilities)

    def sensitive_fields(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields if f.sensitivity == "sensitive")

    def returnable_fields(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields if f.returnable)


class ReadBackend(Protocol):
    """The thin native surface an adapter implements; the engine layers governance on top."""

    def describe_target(self, target: str) -> TargetSchema | None: ...

    def fetch(
        self,
        target: str,
        *,
        predicates: Sequence[Predicate],
        fields: Sequence[str],
        sort: Any,
        limit: int,
        after_id: Any,
    ) -> list[dict[str, Any]]: ...

    def get_one(
        self, target: str, record_id: Any, fields: Sequence[str]
    ) -> dict[str, Any] | None: ...

    def count(self, target: str, *, predicates: Sequence[Predicate]) -> int | None: ...

    def aggregate(
        self, target: str, *, predicates: Sequence[Predicate], group_by: str, metrics: Sequence[str]
    ) -> list[dict[str, Any]] | None: ...


def _encode_cursor(last_id: Any) -> str:
    return base64.urlsafe_b64encode(str(last_id).encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> Any:
    if not cursor:
        return None
    raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    try:
        return int(raw)
    except ValueError:
        return raw


class ReadPlane:
    """Governed read verbs over a `ReadBackend`. Construct once per adapter; call search/count/aggregate."""

    def __init__(
        self,
        backend: ReadBackend,
        *,
        cap: int = BYTE_CAP,
        edge_filter_bound: int = EDGE_FILTER_BOUND,
        export_store: ExportStore | None = None,
        bulk_threshold: int = BULK_THRESHOLD,
        export_ttl_seconds: int = 3600,
    ) -> None:
        self._backend = backend
        self._cap = cap
        self._bound = edge_filter_bound
        self._export_store = export_store
        self._bulk_threshold = bulk_threshold
        self._export_ttl = export_ttl_seconds

    def _schema(self, target: str) -> TargetSchema:
        schema = self._backend.describe_target(target)
        if schema is None:
            raise CapabilityUnsupported(f"unknown or unprovisioned target: {target}")
        return schema

    def _authorize_fields(
        self, schema: TargetSchema, requested: Sequence[str], grant_fields: Sequence[str] | None
    ) -> tuple[list[str], list[str]]:
        """Intersect requested fields with what is returnable and grant-visible. Sensitive fields need
        an explicit grant; absent it they are dropped and reported as `redacted` (never silently)."""
        sensitive = schema.sensitive_fields()
        returnable = schema.returnable_fields()
        allowed: list[str] = []
        redacted: list[str] = []
        for name in requested:
            if name not in returnable:
                continue
            if name in sensitive and grant_fields is not None and name not in grant_fields:
                redacted.append(name)
                continue
            allowed.append(name)
        return allowed, redacted

    def count(self, target: str, *, filter: Any) -> dict[str, Any]:
        schema = self._schema(target)
        preds = parse_filter(filter)
        exact = self._backend.count(target, predicates=preds)
        if exact is not None:
            return {"count": exact}
        # Backend can't count server-side → a bounded edge count, marked approximate (it may be a
        # lower bound if the real set exceeds what we pulled).
        rows = self._backend.fetch(
            target, predicates=preds, fields=("id",), sort=None, limit=self._bound + 1, after_id=None
        )
        rows = [r for r in rows if self._matches(r, preds)]
        return {"count": min(len(rows), self._bound), "approximate": True}

    def aggregate(
        self, target: str, *, filter: Any, group_by: str, metrics: Sequence[str]
    ) -> dict[str, Any]:
        schema = self._schema(target)
        preds = parse_filter(filter)
        if not schema.capabilities.server_aggregate:
            raise CapabilityUnsupported(
                f"{target} cannot aggregate server-side — export and group with code instead"
            )
        groups = self._backend.aggregate(target, predicates=preds, group_by=group_by, metrics=metrics)
        return enforce_byte_cap({"groups": groups or []}, self._cap)

    def get(
        self,
        target: str,
        *,
        record_id: Any,
        fields: Sequence[str] | None,
        grant_fields: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        schema = self._schema(target)
        requested = list(fields) if fields else list(schema.default_projection)
        allowed, _ = self._authorize_fields(schema, requested, grant_fields)
        record = self._backend.get_one(target, record_id, allowed)
        if record is None:
            return None
        return enforce_byte_cap(project_items([record], allowed)[0], self._cap)

    def export(
        self,
        target: str,
        *,
        filter: Any,
        fields: Sequence[str] | None,
        tenant: str,
        now: datetime,
        approved: bool = False,
        grant_fields: Sequence[str] | None = None,
    ) -> ExportHandle:
        """Stream the filtered, projected result to a tenant-scoped artifact and return a HANDLE — the
        rows never enter context. A bulk extraction (over the threshold) needs `approved=True` and is
        gated/audited; otherwise it raises `BulkApprovalRequired`."""
        if self._export_store is None:
            raise CapabilityUnsupported("export is not configured for this read plane")
        schema = self._schema(target)
        preds = parse_filter(filter)
        requested = list(fields) if fields else list(schema.default_projection)
        allowed, _ = self._authorize_fields(schema, requested, grant_fields)

        estimate = self.count(target, filter=filter).get("count", 0)
        if estimate > self._bulk_threshold and not approved:
            raise BulkApprovalRequired(estimate, self._bulk_threshold)

        rows = (project(r, allowed) for r in self._stream(target, preds, allowed))
        return self._export_store.write(
            rows, fmt="jsonl", schema={"fields": list(allowed)},
            tenant=tenant, now=now, ttl_seconds=self._export_ttl,
        )

    def _stream(
        self, target: str, preds: Sequence[Predicate], fields: Sequence[str]
    ) -> Any:
        """Keyset-cursored page-by-page generator over the whole filtered set — one page in memory at a
        time, never the full result (so export scales to 1M+ without a flood or an OOM)."""
        after_id: Any = None
        while True:
            page = self._backend.fetch(
                target, predicates=preds, fields=fields, sort=None,
                limit=EXPORT_PAGE_SIZE, after_id=after_id,
            )
            if not page:
                return
            for row in page:
                yield row
            if len(page) < EXPORT_PAGE_SIZE:
                return
            after_id = page[-1]["id"]

    def search(
        self,
        target: str,
        *,
        filter: Any,
        fields: Sequence[str] | None,
        limit: int,
        cursor: str | None = None,
        grant_fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        schema = self._schema(target)
        preds = parse_filter(filter)
        requested = list(fields) if fields else list(schema.default_projection)
        allowed, redacted = self._authorize_fields(schema, requested, grant_fields)
        after_id = _decode_cursor(cursor)

        rows = self._gather(target, schema, preds, allowed, limit, after_id)

        has_more = len(rows) > limit
        rows = rows[:limit]
        page: dict[str, Any] = {"items": project_items(rows, allowed)}
        if has_more and rows:
            page["next_cursor"] = _encode_cursor(rows[-1]["id"])
        if redacted:
            page["redacted"] = redacted
        return enforce_byte_cap(page, self._cap)

    def _gather(
        self,
        target: str,
        schema: TargetSchema,
        preds: Sequence[Predicate],
        fields: Sequence[str],
        limit: int,
        after_id: Any,
    ) -> list[dict[str, Any]]:
        """Fetch one page worth (+1 to detect more). When the backend can't filter server-side, pull a
        bounded slice and filter in the edge — refusing if the unfiltered set exceeds the bound."""
        if schema.capabilities.server_filter:
            return self._backend.fetch(
                target, predicates=preds, fields=fields, sort=None, limit=limit + 1, after_id=after_id
            )
        pulled = self._backend.fetch(
            target, predicates=[], fields=fields, sort=None, limit=self._bound + 1, after_id=after_id
        )
        if len(pulled) > self._bound:
            raise ResultTooLarge(self._bound + 1, self._bound)  # unbounded edge filter — refuse
        matched = [r for r in pulled if self._matches(r, preds)]
        return matched[: limit + 1]

    @staticmethod
    def _matches(row: dict[str, Any], preds: Sequence[Predicate]) -> bool:
        """Edge-side predicate evaluation for the no-server-filter fallback (mirrors the op set)."""
        for p in preds:
            v = row.get(p.field)
            if p.op == "eq" and v != p.value:
                return False
            if p.op == "ne" and v == p.value:
                return False
            if p.op == "ilike" and str(p.value).lower() not in str(v if v is not None else "").lower():
                return False
            if p.op == "contains" and str(p.value) not in str(v if v is not None else ""):
                return False
            if p.op == "in" and v not in p.value:
                return False
            if p.op == "gt" and not (v is not None and v > p.value):
                return False
            if p.op == "gte" and not (v is not None and v >= p.value):
                return False
            if p.op == "lt" and not (v is not None and v < p.value):
                return False
            if p.op == "lte" and not (v is not None and v <= p.value):
                return False
            if p.op == "between" and not (v is not None and p.value[0] <= v <= p.value[1]):
                return False
        return True

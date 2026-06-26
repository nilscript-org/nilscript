"""The universal ReadPlane engine: the governed read verbs (count/search/get/aggregate) implemented
ONCE over a backend protocol, so every adapter inherits projection, byte-cap-refuse, capability
fallback, and read-side authorization — instead of each hand-rolling reads (and re-introducing floods).

Proven against an in-memory FakeBackend with a tunable capability profile, so the engine's degradation
paths (no server-side filter, no server-side count, no aggregate) are exercised without a live backend.
"""

from __future__ import annotations

import pytest

from datetime import UTC, datetime

from nilscript.dataplane import (
    BulkApprovalRequired,
    Capabilities,
    CapabilityUnsupported,
    ExportStore,
    FieldSpec,
    ReadPlane,
    ResultTooLarge,
    TargetSchema,
)

NOW = datetime(2026, 6, 26, tzinfo=UTC)


class FakeBackend:
    """In-memory backend with a tunable capability profile. `rows` is the native store; when a
    capability is off, the corresponding native method returns None so the engine must degrade."""

    def __init__(self, rows: list[dict], schema: TargetSchema) -> None:
        self.rows = rows
        self._schema = schema

    def describe_target(self, target: str) -> TargetSchema | None:
        return self._schema if target == self._schema.target else None

    def _match(self, row: dict, predicates) -> bool:
        for p in predicates:
            v = row.get(p.field)
            if p.op == "eq" and v != p.value:
                return False
            if p.op == "ilike" and str(p.value).lower() not in str(v or "").lower():
                return False
        return True

    def fetch(self, target, *, predicates, fields, sort, limit, after_id):
        if not self._schema.capabilities.server_filter:
            predicates = []  # backend can't filter — engine must do it (and bound the pull)
        rows = [r for r in self.rows if self._match(r, predicates)]
        rows = [r for r in rows if after_id is None or r["id"] > after_id]
        rows.sort(key=lambda r: r["id"])
        return rows[:limit]

    def count(self, target, *, predicates):
        if not self._schema.capabilities.server_filter:
            return None  # can't count server-side
        return sum(1 for r in self.rows if self._match(r, predicates))

    def get_one(self, target, record_id, fields):
        return next((r for r in self.rows if r["id"] == record_id), None)

    def aggregate(self, target, *, predicates, group_by, metrics):
        if not self._schema.capabilities.server_aggregate:
            return None
        groups: dict = {}
        for r in self.rows:
            if self._match(r, predicates):
                key = r.get(group_by)
                groups[key] = groups.get(key, 0) + 1
        return [{"key": k, "count": v} for k, v in groups.items()]


def _schema(caps: Capabilities = Capabilities()) -> TargetSchema:
    return TargetSchema(
        target="res.partner",
        fields=(
            FieldSpec("id", "int", is_key=True),
            FieldSpec("name", "str"),
            FieldSpec("phone", "str"),
            FieldSpec("salary", "float", sensitivity="sensitive"),
        ),
        cardinality="large",
        default_projection=("id", "name", "phone"),
        capabilities=caps,
    )


def _contacts(n: int) -> list[dict]:
    return [
        {"id": i, "name": f"c{i}", "phone": f"+9745{i:07d}", "salary": 1000 + i, "junk": "z" * 200}
        for i in range(n)
    ]


# ── projection + cap ────────────────────────────────────────────────────────────────────────────
def test_search_applies_the_default_projection_when_none_requested() -> None:
    plane = ReadPlane(FakeBackend(_contacts(3), _schema()))
    page = plane.search("res.partner", filter=[], fields=None, limit=50)
    # default projection is id/name/phone — NOT junk/salary, and never the whole record.
    assert page["items"][0] == {"id": 0, "name": "c0", "phone": "+97450000000"}


def test_search_filters_server_side_and_projects() -> None:
    plane = ReadPlane(FakeBackend(_contacts(100), _schema()))
    page = plane.search(
        "res.partner", filter=[{"field": "name", "op": "eq", "value": "c42"}], fields=("name",), limit=50
    )
    assert [r["id"] for r in page["items"]] == [42]
    assert page["items"][0] == {"id": 42, "name": "c42"}


def test_search_refuses_when_a_page_would_exceed_the_cap() -> None:
    plane = ReadPlane(FakeBackend(_contacts(20_000), _schema()))
    with pytest.raises(ResultTooLarge):
        # ask for a huge limit with a wide field set so the page blows the cap → refuse, not truncate
        plane.search("res.partner", filter=[], fields=("name", "phone"), limit=20_000)


# ── count ───────────────────────────────────────────────────────────────────────────────────────
def test_count_is_exact_when_the_backend_can_count() -> None:
    plane = ReadPlane(FakeBackend(_contacts(41), _schema()))
    assert plane.count("res.partner", filter=[]) == {"count": 41}


def test_count_falls_back_to_approximate_when_backend_cannot_count() -> None:
    caps = Capabilities(server_filter=False)
    plane = ReadPlane(FakeBackend(_contacts(10), _schema(caps)))
    out = plane.count("res.partner", filter=[])
    assert out["count"] == 10
    assert out["approximate"] is True


# ── capability fallback ──────────────────────────────────────────────────────────────────────────
def test_search_filters_in_the_edge_when_backend_cannot_filter() -> None:
    caps = Capabilities(server_filter=False)
    plane = ReadPlane(FakeBackend(_contacts(50), _schema(caps)))
    page = plane.search(
        "res.partner", filter=[{"field": "name", "op": "eq", "value": "c7"}], fields=("name",), limit=50
    )
    assert [r["id"] for r in page["items"]] == [7]


def test_search_refuses_when_unfilterable_set_exceeds_the_bound() -> None:
    # backend can't filter and there are more rows than the edge will pull → refuse, never fetch-all.
    caps = Capabilities(server_filter=False)
    plane = ReadPlane(FakeBackend(_contacts(100_000), _schema(caps)), edge_filter_bound=1000)
    with pytest.raises(ResultTooLarge):
        plane.search(
            "res.partner", filter=[{"field": "name", "op": "eq", "value": "c7"}], fields=("name",), limit=50
        )


# ── aggregate ────────────────────────────────────────────────────────────────────────────────────
def test_aggregate_uses_server_side_grouping() -> None:
    rows = [{"id": i, "name": f"c{i}", "phone": "", "salary": 0, "country": "QA" if i % 2 else "SA"}
            for i in range(10)]
    plane = ReadPlane(FakeBackend(rows, _schema()))
    out = plane.aggregate("res.partner", filter=[], group_by="country", metrics=("count",))
    by = {g["key"]: g["count"] for g in out["groups"]}
    assert by == {"SA": 5, "QA": 5}


def test_aggregate_refuses_when_backend_cannot_group() -> None:
    caps = Capabilities(server_aggregate=False)
    plane = ReadPlane(FakeBackend(_contacts(10), _schema(caps)))
    with pytest.raises(CapabilityUnsupported):
        plane.aggregate("res.partner", filter=[], group_by="name", metrics=("count",))


# ── get ──────────────────────────────────────────────────────────────────────────────────────────
def test_get_returns_one_lean_record_by_key() -> None:
    plane = ReadPlane(FakeBackend(_contacts(5), _schema()))
    rec = plane.get("res.partner", record_id=3, fields=("name", "phone"))
    assert rec == {"id": 3, "name": "c3", "phone": "+97450000003"}


def test_get_missing_record_returns_none() -> None:
    plane = ReadPlane(FakeBackend(_contacts(2), _schema()))
    assert plane.get("res.partner", record_id=99, fields=("name",)) is None


# ── read-side authorization ──────────────────────────────────────────────────────────────────────
def test_sensitive_field_is_dropped_without_a_grant_and_redaction_is_noted() -> None:
    plane = ReadPlane(FakeBackend(_contacts(1), _schema()))
    page = plane.search("res.partner", filter=[], fields=("name", "salary"), limit=50, grant_fields=())
    assert "salary" not in page["items"][0]          # not leaked
    assert "salary" in page.get("redacted", [])      # and the omission is declared, not silent


def test_sensitive_field_passes_when_the_grant_allows_it() -> None:
    plane = ReadPlane(FakeBackend(_contacts(1), _schema()))
    page = plane.search(
        "res.partner", filter=[], fields=("name", "salary"), limit=50, grant_fields=("salary",)
    )
    assert page["items"][0]["salary"] == 1000


# ── export (bulk read → handle, governed) ────────────────────────────────────────────────────────
def test_export_streams_projected_rows_to_a_handle(tmp_path) -> None:
    store = ExportStore(root=tmp_path)
    plane = ReadPlane(FakeBackend(_contacts(500), _schema()), export_store=store)
    handle = plane.export("res.partner", filter=[], fields=("name",), tenant="ws-1", now=NOW)
    assert handle.rows == 500
    rows = list(store.open(handle.handle, tenant="ws-1", now=NOW))
    assert rows[0] == {"id": 0, "name": "c0"}  # projected, not the whole record


def test_bulk_export_above_threshold_requires_approval(tmp_path) -> None:
    store = ExportStore(root=tmp_path)
    plane = ReadPlane(FakeBackend(_contacts(5000), _schema()), export_store=store, bulk_threshold=1000)
    with pytest.raises(BulkApprovalRequired):
        plane.export("res.partner", filter=[], fields=("name",), tenant="ws-1", now=NOW, approved=False)


def test_bulk_export_proceeds_when_approved(tmp_path) -> None:
    store = ExportStore(root=tmp_path)
    plane = ReadPlane(FakeBackend(_contacts(5000), _schema()), export_store=store, bulk_threshold=1000)
    handle = plane.export("res.partner", filter=[], fields=("name",), tenant="ws-1", now=NOW, approved=True)
    assert handle.rows == 5000

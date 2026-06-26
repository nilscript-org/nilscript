"""export → data handle: a bulk read is streamed to a tenant-scoped artifact on disk; the agent gets a
small HANDLE, never the rows. The rows are reached only through code in the sandbox. Handles are
tenant-scoped (no cross-tenant read), TTL-expiring, and never carry the data inline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nilscript.dataplane import (
    ExportStore,
    HandleExpired,
    NotAuthorizedHandle,
)

NOW = datetime(2026, 6, 26, tzinfo=UTC)


def test_export_writes_artifact_and_returns_a_small_handle_not_rows(tmp_path) -> None:
    store = ExportStore(root=tmp_path)
    rows = ({"id": i, "name": f"c{i}"} for i in range(1000))
    handle = store.write(rows, fmt="jsonl", schema={"fields": ["id", "name"]},
                         tenant="ws-1", now=NOW, ttl_seconds=3600)
    assert handle.rows == 1000
    assert handle.bytes > 0
    assert handle.format == "jsonl"
    assert handle.expires_at == NOW + timedelta(seconds=3600)
    # the handle carries metadata only — the 1000 rows live on disk, never inline.
    assert not hasattr(handle, "items")


def test_owning_tenant_can_open_the_handle_and_stream_rows(tmp_path) -> None:
    store = ExportStore(root=tmp_path)
    handle = store.write(iter([{"id": 1}, {"id": 2}]), fmt="jsonl", schema={},
                        tenant="ws-1", now=NOW, ttl_seconds=3600)
    rows = list(store.open(handle.handle, tenant="ws-1", now=NOW))
    assert rows == [{"id": 1}, {"id": 2}]


def test_a_foreign_tenant_cannot_open_the_handle(tmp_path) -> None:
    store = ExportStore(root=tmp_path)
    handle = store.write(iter([{"id": 1}]), fmt="jsonl", schema={},
                        tenant="ws-1", now=NOW, ttl_seconds=3600)
    with pytest.raises(NotAuthorizedHandle):
        list(store.open(handle.handle, tenant="ws-2", now=NOW))


def test_an_expired_handle_is_refused(tmp_path) -> None:
    store = ExportStore(root=tmp_path)
    handle = store.write(iter([{"id": 1}]), fmt="jsonl", schema={},
                        tenant="ws-1", now=NOW, ttl_seconds=60)
    later = NOW + timedelta(seconds=61)
    with pytest.raises(HandleExpired):
        list(store.open(handle.handle, tenant="ws-1", now=later))

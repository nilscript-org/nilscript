"""Temporal worker integration (Phase 6) — verified end-to-end with temporalio's in-process
time-skipping test server (no external Temporal needed). Skips cleanly if temporalio is absent."""

from __future__ import annotations

import pytest

temporalio = pytest.importorskip("temporalio")

from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from nilscript.durable import tenant_workflow_id  # noqa: E402
from nilscript.durable_temporal import (  # noqa: E402
    GovernedWriteInput,
    TenantGovernedWriteWorkflow,
    nil_governed_commit,
    register_executor,
    task_queue_for,
)


@pytest.mark.asyncio
async def test_durable_governed_write_retries_then_commits() -> None:
    """A flaky backend (throttled twice) is retried DURABLY by Temporal, then commits — proving the
    429-fairness/durability contract, scoped to the tenant's task queue + deterministic workflow id."""
    attempts = {"n": 0}

    async def flaky_executor(inp: GovernedWriteInput) -> dict:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429 throttled by backend")
        return {"ok": True, "attempt": attempts["n"], "tenant": inp.tenant, "verb": inp.verb}

    register_executor(flaky_executor)
    inp = GovernedWriteInput(tenant="ws_a", verb="account.create_invoice", idempotency_key="k1")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=task_queue_for("ws_a"),
                          workflows=[TenantGovernedWriteWorkflow], activities=[nil_governed_commit]):
            result = await env.client.execute_workflow(
                TenantGovernedWriteWorkflow.run, inp,
                id=tenant_workflow_id("ws_a", "governed_write", "k1"),
                task_queue=task_queue_for("ws_a"),
            )
    assert result == {"ok": True, "attempt": 3, "tenant": "ws_a", "verb": "account.create_invoice"}


@pytest.mark.asyncio
async def test_workflow_id_is_tenant_scoped_and_idempotent() -> None:
    a = tenant_workflow_id("ws_a", "governed_write", "k1")
    b = tenant_workflow_id("ws_b", "governed_write", "k1")
    assert a != b and a == tenant_workflow_id("ws_a", "governed_write", "k1")

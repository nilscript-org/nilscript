"""Temporal worker integration onto the tenant-scoped durable layer (durable.py) — Phase 6.

OPTIONAL: temporalio is imported lazily, so the kernel runs without it. When a Temporal server is
available, heavy/bulk governed writes run as DURABLE workflows that survive crashes and retry with
backoff — the "429 lesson", durable edition — and stay isolated per tenant:

  • per-tenant Temporal NAMESPACE (tenant_namespace) — a worker for tenant A never sees B's tasks;
  • tenant-scoped, deterministic WORKFLOW ID (tenant_workflow_id) — idempotent (a re-kicked job with
    the same key replays the same workflow) and collision-free across tenants;
  • the NIL gate (propose→commit) runs inside an ACTIVITY with a Temporal RetryPolicy, so a throttled/
    transient backend is retried durably instead of losing the work.

The activity delegates to a registered executor (`register_executor`) — in deployment that calls the
NIL SDK against the tenant's adapter; in tests it's a simple callable, so the workflow is verifiable
end-to-end with temporalio's in-process time-skipping server (no external infra).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable

from nilscript.durable import tenant_namespace, tenant_workflow_id

try:
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy

    _HAS_TEMPORAL = True
except ImportError:  # temporalio not installed — module imports cleanly, worker APIs raise on use
    _HAS_TEMPORAL = False


def temporal_available() -> bool:
    return _HAS_TEMPORAL


@dataclass
class GovernedWriteInput:
    """One durable governed write: which tenant, the NIL verb + args, and an idempotency key (→ the
    deterministic workflow id, so a redelivered kick never double-commits)."""

    tenant: str
    verb: str
    args: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""


# The NIL executor the activity calls — set in the worker process via register_executor. Activities run
# OUTSIDE the workflow sandbox, so a module-level executor is the correct injection point.
_EXECUTOR: Callable[[GovernedWriteInput], Awaitable[dict[str, Any]]] | None = None


def register_executor(fn: Callable[[GovernedWriteInput], Awaitable[dict[str, Any]]]) -> None:
    """Register the coroutine the durable activity runs (the real NIL propose→commit; a fake in tests)."""
    global _EXECUTOR
    _EXECUTOR = fn


if _HAS_TEMPORAL:

    @activity.defn(name="nil_governed_commit")
    async def nil_governed_commit(inp: GovernedWriteInput) -> dict[str, Any]:
        if _EXECUTOR is None:
            raise RuntimeError("no NIL executor registered for the durable activity")
        return await _EXECUTOR(inp)

    @workflow.defn(name="TenantGovernedWrite")
    class TenantGovernedWriteWorkflow:
        @workflow.run
        async def run(self, inp: GovernedWriteInput) -> dict[str, Any]:
            # The NIL gate runs in the activity with durable retry/backoff — a throttled (429) or
            # transient backend is retried across attempts (and across worker crashes), not dropped.
            return await workflow.execute_activity(
                nil_governed_commit, inp,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                    maximum_attempts=8,
                ),
            )


def task_queue_for(tenant: str) -> str:
    return f"nil-{tenant}"


async def start_governed_write(client: Any, inp: GovernedWriteInput, *, task_queue: str | None = None) -> Any:
    """Kick a tenant-scoped durable governed-write workflow. The id is deterministic per (tenant, key),
    so a re-kick is idempotent; the task queue is per-tenant."""
    if not _HAS_TEMPORAL:
        raise RuntimeError("temporalio is not installed — durable workflows unavailable")
    return await client.execute_workflow(
        TenantGovernedWriteWorkflow.run, inp,
        id=tenant_workflow_id(inp.tenant, "governed_write", inp.idempotency_key or inp.verb),
        task_queue=task_queue or task_queue_for(inp.tenant),
    )


async def run_worker(
    tenant: str, *, host: str = "localhost:7233",
    executor: Callable[[GovernedWriteInput], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    """Start a Temporal worker on the TENANT's namespace + task queue for governed-write workflows.
    One worker per tenant namespace is the isolation boundary."""
    if not _HAS_TEMPORAL:
        raise RuntimeError("temporalio is not installed — cannot start a worker")
    from temporalio.client import Client
    from temporalio.worker import Worker

    if executor is not None:
        register_executor(executor)
    client = await Client.connect(host, namespace=tenant_namespace(tenant))
    worker = Worker(
        client, task_queue=task_queue_for(tenant),
        workflows=[TenantGovernedWriteWorkflow], activities=[nil_governed_commit],
    )
    await worker.run()

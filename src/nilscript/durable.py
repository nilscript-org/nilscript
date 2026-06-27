"""Tenant-scoped durable execution — the isolation LAYER Temporal plugs into (Phase 6).

Full Temporal worker/activity integration is a separate build; what multi-tenancy needs FIRST is that
durable work is isolated and fair per tenant. This module provides exactly that, with no Temporal
dependency, so it is testable today and the worker wires onto it later:

  • `tenant_workflow_id` — deterministic, tenant-prefixed workflow ids → idempotent replay AND no
    cross-tenant id collision (a re-delivered fire for tenant A can never touch tenant B's workflow).
  • `tenant_namespace` — a Temporal NAMESPACE per tenant (the platform's hard isolation boundary;
    a worker polling tenant A's namespace never sees B's tasks).
  • `TenantDurablePolicy` — per-tenant rate + concurrency admission for durable activities (the Odoo-429
    fairness lesson, durable edition): a tenant's bulk job is throttled to its own budget and cannot
    starve the others, reusing the per-tenant rate limiter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nilscript.governance_quota import TenantRateLimiter

_SAFE = re.compile(r"[^a-zA-Z0-9._-]")


def _slug(value: str) -> str:
    """A Temporal-safe id segment (Temporal ids/namespaces disallow arbitrary chars)."""
    return _SAFE.sub("-", value.strip()) or "_"


def tenant_workflow_id(tenant: str, kind: str, key: str) -> str:
    """Deterministic, tenant-scoped workflow id. Same (tenant, kind, key) → same id (idempotent
    replay); different tenants → different ids (no cross-tenant collision)."""
    if not tenant:
        raise ValueError("tenant is required for a tenant-scoped workflow id")
    return f"{_slug(tenant)}:{_slug(kind)}:{_slug(key)}"


def tenant_namespace(tenant: str, base: str = "nil") -> str:
    """The Temporal namespace for a tenant — the hard isolation boundary between companies."""
    if not tenant:
        raise ValueError("tenant is required for a tenant namespace")
    return f"{_slug(base)}-{_slug(tenant)}"


@dataclass
class TenantDurablePolicy:
    """Per-tenant admission for durable activities: rate (token bucket) + optional concurrency cap.
    `admit()` returns False when the tenant is over budget — the executor parks/retries that tenant's
    work without affecting any other tenant."""

    limiter: TenantRateLimiter
    max_concurrent: int = 0  # 0 = unbounded
    _running: dict[str, int] = field(default_factory=dict)

    def admit(self, tenant: str, kind: str = "activity") -> bool:
        if self.max_concurrent and self._running.get(tenant, 0) >= self.max_concurrent:
            return False
        if not self.limiter.allow(tenant, kind):
            return False
        self._running[tenant] = self._running.get(tenant, 0) + 1
        return True

    def release(self, tenant: str) -> None:
        if self._running.get(tenant, 0) > 0:
            self._running[tenant] -= 1

"""Canonical Evolution instance naming — ONE deterministic, reversible scheme.

Every code path that creates, resolves, or validates an Evolution instance name MUST
go through this module. The name is derived purely from a tenant/workspace id (no
randomness), so given an instance name we can always recover the tenant prefix.

Ported from the reference product with the hard-coded brand removed: the instance
prefix defaults to ``wosool`` for backward compatibility but is a module constant.
"""

from __future__ import annotations

import re

# The single canonical webhook route for all Evolution webhooks. The inbound handler
# (a later step) mounts exactly this path; `get_canonical_webhook_url` builds the
# absolute URL stamped on each instance at create/set time.
CANONICAL_WEBHOOK_ROUTE = "/api/v1/webhooks/whatsapp/evolution"

# Instance-name prefix. Kept as a constant so the scheme stays in one place; the
# round-trip helpers below are prefix-agnostic and read from here.
INSTANCE_PREFIX = "wosool"

# Canonical shape: <prefix>-XXXXXXXX-YYYYYYYY where X/Y are lowercase hex from the id.
_CANONICAL_RE = re.compile(rf"^{re.escape(INSTANCE_PREFIX)}-[a-f0-9]{{8}}-[a-f0-9]{{6,8}}$")


def get_canonical_instance_name(tenant_id: str) -> str:
    """Deterministic instance name from a tenant/workspace id.

    Format: ``{prefix}-{id[:8]}-{id[8:16]}``. No randomness — always reversible via
    `extract_tenant_prefix`. Short ids are right-padded with zeros so the shape holds.
    """
    tid = str(tenant_id or "").strip()
    if len(tid) < 16:
        tid = tid.ljust(16, "0")
    return f"{INSTANCE_PREFIX}-{tid[:8]}-{tid[8:16]}"


def extract_tenant_prefix(instance_name: str) -> str | None:
    """Recover the tenant id's first-8-chars from an instance name.

    Returns None when the name isn't a ``{prefix}-*`` instance.
    """
    name = str(instance_name or "").strip()
    if not name.startswith(f"{INSTANCE_PREFIX}-"):
        return None
    parts = name.split("-", 2)
    if len(parts) < 2 or not parts[1]:
        return None
    return parts[1]


def is_canonical_instance(instance_name: str) -> bool:
    """True iff the name follows the strict canonical ``{prefix}-XXXXXXXX-YYYYYYYY`` form."""
    return bool(_CANONICAL_RE.match(str(instance_name or "").strip()))


def is_managed_instance(instance_name: str) -> bool:
    """True for any ``{prefix}-*`` instance (canonical or legacy/random) we own."""
    return str(instance_name or "").strip().startswith(f"{INSTANCE_PREFIX}-")


def get_canonical_webhook_url(base_url: str) -> str:
    """Build the absolute canonical webhook URL from a public base URL."""
    base = str(base_url or "").strip().rstrip("/")
    return f"{base}{CANONICAL_WEBHOOK_ROUTE}"


def instances_share_tenant(name_a: str, name_b: str) -> bool:
    """True iff two instance names resolve to the same tenant prefix."""
    prefix_a = extract_tenant_prefix(name_a)
    prefix_b = extract_tenant_prefix(name_b)
    if not prefix_a or not prefix_b:
        return False
    return prefix_a == prefix_b

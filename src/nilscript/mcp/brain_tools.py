"""Agent-facing MCP tools for READING the NIL Business Graph (the brain).

The kernel governs *actions* against a connected backend (`nil_describe`/`propose`/`commit`). But a
business also has a *graph* — cycles, entities, roles, policies, flows, and the live instances flowing
through them (invoices, payments, the overdue list). That graph lives in the NIL **brain**, a separate
read-model service. Without a dedicated tool, an agent asked "show my policies" improvises (curl, file
search) and answers inconsistently — sometimes hitting the brain, sometimes asking the kernel "is there
a policy verb?" and wrongly concluding "no policies".

These tools remove the guesswork: each is a thin, read-only HTTP relay to a stable brain GET endpoint,
so "show my policies / cycles / what changed / what's overdue" is a deterministic tool call with one
answer. The kernel stays decoupled from the brain's internals — it relays HTTP, it never imports the
brain. Env-gated: present only when `NIL_BRAIN_URL` is configured.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# Graph node kinds an operator asks about by name. "policy" is the one that was failing — policies are
# graph nodes, never kernel verbs.
_GRAPH_KINDS = ("entity", "role", "policy", "flow", "cycle")


class BrainTools:
    """Read-only HTTP relay to the brain's `/api/graph/*` endpoints. Inject a client for tests."""

    def __init__(
        self, brain_url: str, *, token: str = "", tenant: str = "",
        client: httpx.AsyncClient | None = None, timeout: float = 10.0,
    ) -> None:
        self._base = brain_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._tenant = tenant
        self._client = client
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> BrainTools | None:
        """Build from `NIL_BRAIN_URL` (+ optional `NIL_BRAIN_TOKEN`/`NIL_BRAIN_TENANT`), else None."""
        url = os.environ.get("NIL_BRAIN_URL", "")
        if not url:
            return None
        return cls(
            url,
            token=os.environ.get("NIL_BRAIN_TOKEN", ""),
            tenant=os.environ.get("NIL_BRAIN_TENANT", ""),
        )

    def _tenant_for(self, tenant: str | None) -> str:
        return tenant or self._tenant

    async def _get(self, path: str, *, params: dict[str, Any]) -> Any:
        client = self._client or httpx.AsyncClient(base_url=self._base, timeout=self._timeout)
        try:
            resp = await client.request("GET", path, params=params, headers=self._headers)
            if resp.status_code >= 400:
                return {"error": f"brain returned {resp.status_code}", "path": path}
            try:
                return resp.json()
            except ValueError:
                return {"error": "non-json response from brain", "status": resp.status_code}
        except httpx.HTTPError as exc:
            return {"error": f"brain unreachable: {exc}"}
        finally:
            if self._client is None:
                await client.aclose()

    # ── read tools ──────────────────────────────────────────────────────────────────────────────

    async def graph(self, kind: str | None = None, tenant: str | None = None) -> dict[str, Any]:
        """Business-graph nodes, optionally filtered to one `kind` (entity/role/policy/flow/cycle).

        `graph(kind="policy")` is the deterministic answer to "show my policies"."""
        ws = self._tenant_for(tenant)
        if not ws:
            return {"error": "no tenant configured (set NIL_BRAIN_TENANT or pass tenant)"}
        if kind is not None and kind not in _GRAPH_KINDS:
            return {"error": f"unknown kind {kind!r}; expected one of {list(_GRAPH_KINDS)}"}
        data = await self._get("/api/graph/nodes", params={"tenant": ws})
        if isinstance(data, dict) and data.get("error"):
            return data
        nodes = data.get("nodes", data) if isinstance(data, dict) else data
        if not isinstance(nodes, list):
            return {"error": "unexpected node payload from brain", "tenant": ws}
        if kind is not None:
            nodes = [n for n in nodes if isinstance(n, dict) and n.get("kind") == kind]
        return {"tenant": ws, "kind": kind, "count": len(nodes), "nodes": nodes}

    async def cycles(self, tenant: str | None = None) -> dict[str, Any]:
        """Business cycles (Sales, Finance, …) with their goal, metrics, and members."""
        ws = self._tenant_for(tenant)
        if not ws:
            return {"error": "no tenant configured (set NIL_BRAIN_TENANT or pass tenant)"}
        return await self._get("/api/graph/cycles", params={"tenant": ws})

    async def overview(self, tenant: str | None = None) -> dict[str, Any]:
        """Graph summary: how many entities, roles, policies, flows, cycles exist."""
        ws = self._tenant_for(tenant)
        if not ws:
            return {"error": "no tenant configured (set NIL_BRAIN_TENANT or pass tenant)"}
        return await self._get("/api/graph/summary", params={"tenant": ws})

    async def instances(self, tenant: str | None = None) -> dict[str, Any]:
        """Live instance tallies per entity type — totals + derived-state counts (e.g. overdue,
        awaiting_approval). The deterministic answer to "how many invoices are overdue"."""
        ws = self._tenant_for(tenant)
        if not ws:
            return {"error": "no tenant configured (set NIL_BRAIN_TENANT or pass tenant)"}
        return await self._get("/api/graph/instances/summary", params={"tenant": ws})

    async def activity(self, tenant: str | None = None) -> dict[str, Any]:
        """What recently changed in the business graph (the latest version diff / additions)."""
        ws = self._tenant_for(tenant)
        if not ws:
            return {"error": "no tenant configured (set NIL_BRAIN_TENANT or pass tenant)"}
        return await self._get("/api/graph/activity", params={"tenant": ws})

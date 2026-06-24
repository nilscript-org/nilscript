"""Agent-facing MCP tools over the control-plane Automation Registry.

These let an agent author governed automations *by talking*: each tool is a thin, authenticated
request to the control plane (which owns the SSOT, the validator, and the approval gate). The MCP
never re-implements the registry — it relays. Auth is the registry bearer the MCP already holds to
resolve adapters (`NIL_REGISTRY_URL` / `NIL_REGISTRY_TOKEN`).

Discipline preserved end to end: `draft` is preview-only (returns the validator verdict, no write);
`register` lands a `pending_approval` row a human approves; `run` fires only an `active` automation.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class AutomationTools:
    """HTTP relay to the control-plane `/automations/*` endpoints. Inject a client for tests."""

    def __init__(
        self, registry_url: str, token: str = "", *, client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base = registry_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> AutomationTools | None:
        """Build from `NIL_REGISTRY_URL`/`NIL_REGISTRY_TOKEN`, or None if no registry is configured."""
        url = os.environ.get("NIL_REGISTRY_URL", "")
        if not url:
            return None
        return cls(url, os.environ.get("NIL_REGISTRY_TOKEN", ""))

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: Any = None
    ) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(base_url=self._base, timeout=self._timeout)
        try:
            resp = await client.request(method, path, json=json, params=params, headers=self._headers)
            try:
                return resp.json()
            except ValueError:
                return {"error": "non-json response", "status": resp.status_code}
        except httpx.HTTPError as exc:
            return {"error": f"control plane unreachable: {exc}"}
        finally:
            if self._client is None:
                await client.aclose()

    async def draft(
        self, automation_id: str, name: dict[str, Any], plan: dict[str, Any], trigger: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST", "/automations/draft",
            json={"automation_id": automation_id, "name": name, "plan": plan, "trigger": trigger},
        )

    async def register(
        self, automation_id: str, name: dict[str, Any], plan: dict[str, Any], trigger: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST", "/automations/register",
            json={"automation_id": automation_id, "name": name, "plan": plan, "trigger": trigger},
        )

    async def compose_draft(
        self, automation_id: str, name: dict[str, Any], composed: dict[str, Any], trigger: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST", "/automations/compose/draft",
            json={"automation_id": automation_id, "name": name, "composed": composed, "trigger": trigger},
        )

    async def compose_register(
        self, automation_id: str, name: dict[str, Any], composed: dict[str, Any], trigger: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST", "/automations/compose/register",
            json={"automation_id": automation_id, "name": name, "composed": composed, "trigger": trigger},
        )

    async def approve(self, workspace: str, automation_id: str, version: int) -> dict[str, Any]:
        return await self._request(
            "POST", f"/automations/{workspace}/{automation_id}/{version}/state",
            json={"state": "active", "approved_by": "agent"},
        )

    async def run(self, workspace: str, automation_id: str, idempotency_key: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/automations/{workspace}/{automation_id}/run",
            json={"idempotency_key": idempotency_key},
        )

    async def list(self, workspace: str) -> dict[str, Any]:
        return await self._request("GET", "/automations", params={"workspace": workspace})

"""The NIL-MCP tool logic — pure, MCP-SDK-free, and fully testable.

This module deliberately does NOT import the `mcp` package. It wraps a `NilClient` (the only
southbound door) and the `handshake` discovery call into a small set of async methods that return
plain JSON-able dicts — exactly the payloads the MCP tools surface to an agent. `server.py` is the
only place that imports `mcp` and binds these methods onto a FastMCP instance.

Multi-tenant by connection: ONE adapter client is shared across all agents (the server fronts a
single backend), but per-connection state (the proposal→tier map and the idempotency session) is
keyed by a session id derived from the MCP connection (`session_key`). So two agents on the same
hosted server never see or commit each other's proposals.

The safety model is the SDK's, unchanged:
- `propose` / `query` / `status` / `rollback` have **no side effects** — only `commit` writes.
- refusals are **returned values**, never exceptions.
- the COMMIT idempotency key is derived from (session, proposal) via `commit_idempotency_key`, so a
  duplicate `nil_commit` replays rather than double-writing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx

from nilscript.dataplane import ResultTooLarge, enforce_byte_cap
from nilscript.sdk.client import NilClient
from nilscript.sdk.connect import handshake
from nilscript.sdk.idempotency import commit_idempotency_key
from nilscript.sdk.sentences import ProposalBody, RollbackReason, StatusBody
from nilscript.sdk.transport import NilTransport

# Tiers that `--gate human` holds for an out-of-band approval before COMMIT executes.
GATED_TIERS = frozenset({"HIGH", "CRITICAL"})
GATE_MODES = frozenset({"two-step", "human", "auto"})


def session_key(ctx: Any) -> str:
    """A stable per-connection key from an MCP Context (duck-typed — no `mcp` import here).

    Prefers the client id; falls back to the live session object's identity; else a single default
    (stdio: one connection). This is what isolates concurrent agents on a shared hosted server.
    """
    if ctx is None:
        return "default"
    cid = getattr(ctx, "client_id", None)
    if cid:
        return f"client:{cid}"
    session = getattr(ctx, "session", None)
    if session is not None:
        return f"sess:{id(session)}"
    return "default"


class NilTools:
    """Tool surface over one NIL adapter, isolated per connection.

    `_proposals[session_id][proposal_id] = {tier, verb}` is captured at PROPOSE so COMMIT can apply
    tier-scaled authority without a second round-trip — and so one agent's proposals are invisible to
    another's.
    """

    def __init__(
        self,
        client: NilClient,
        transport: NilTransport,
        *,
        session_id: str = "mcp-session",
        gate: str = "two-step",
    ) -> None:
        if gate not in GATE_MODES:
            raise ValueError(f"gate must be one of {sorted(GATE_MODES)}, got {gate!r}")
        self._client = client
        self._transport = transport
        self._default_session = session_id
        self._gate = gate
        self._proposals: dict[str, dict[str, dict[str, Any]]] = {}

    def _sid(self, session_id: str | None) -> str:
        return session_id or self._default_session

    def _remember(self, sid: str, proposal: ProposalBody) -> None:
        if not proposal.is_refusal and proposal.id is not None:
            self._proposals.setdefault(sid, {})[proposal.id] = {
                "tier": proposal.tier.value if proposal.tier is not None else None,
                "verb": proposal.verb,
                # the human preview (e.g. {"summary": "delete contact AHMED (43)"}) so the owner's
                # approval screen can show WHAT they're approving, not a bare proposal id.
                "preview": proposal.preview,
            }

    async def describe(self) -> dict[str, Any]:
        """Discovery: the adapter's skeleton {system, nil, verbs, targets, ready, missing}."""
        return await handshake(self._transport)

    async def propose(
        self, verb: str, args: dict[str, Any] | None = None, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """PROPOSE an intent. No side effect — returns a preview or a structured refusal."""
        sid = self._sid(session_id)
        proposal = await self._client.propose(
            verb, args or {}, session_id=sid, request_timestamp=datetime.now(UTC)
        )
        self._remember(sid, proposal)
        return proposal.model_dump(mode="json", exclude_none=True)

    async def commit(self, proposal_id: str, *, session_id: str | None = None) -> dict[str, Any]:
        """COMMIT a previously previewed proposal — the only tool that mutates the backend."""
        sid = self._sid(session_id)
        gate_block = await self._gate_decision(sid, proposal_id)
        if gate_block is not None:
            return gate_block
        key = commit_idempotency_key(sid, proposal_id)
        outcome = await self._client.commit(proposal_id, idempotency_key=key)
        body = outcome.model_dump(mode="json", exclude_none=True)
        body["committed"] = isinstance(outcome, StatusBody)
        return body

    async def query(self, verb: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """QUERY live business truth. No side effect; the answer is data, never instruction.

        The byte-cap backstop runs HERE, at the relay: even a legacy or misbehaving adapter that
        returns an unprojected dump cannot flood the agent's context — an oversized result becomes a
        `RESULT_TOO_LARGE` refusal (a returned value, never an exception, never a truncated page)."""
        data = await self._client.query(verb, args or {})
        try:
            return enforce_byte_cap(data)
        except ResultTooLarge as exc:
            return {
                "outcome": "refused",
                "code": exc.code,
                "bytes": exc.bytes,
                "cap": exc.cap,
                "message": exc.message,
            }

    # ── the read data plane (canonical, namespace-agnostic; target carries the entity) ────────────
    # All route through `query`, so the byte-cap backstop applies uniformly. The adapter dispatches
    # these canonical verbs to its ReadPlane; a non-conformant adapter answers UNKNOWN_VERB.
    async def search(
        self,
        target: str,
        filter: Any = None,
        fields: list[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """A lean, filtered, paginated page. Never whole records; refuses (never truncates) over cap."""
        return await self.query(
            "nil.search",
            {"target": target, "filter": filter or [], "fields": fields, "limit": limit, "cursor": cursor},
        )

    async def count(self, target: str, filter: Any = None) -> dict[str, Any]:
        """Just {count} — the first call for any 'how many / does X exist'. Never list to count."""
        return await self.query("nil.count", {"target": target, "filter": filter or []})

    async def get(self, target: str, id: Any, fields: list[str] | None = None) -> dict[str, Any]:
        """One lean record by key."""
        return await self.query("nil.get", {"target": target, "id": id, "fields": fields})

    async def aggregate(
        self, target: str, group_by: str, metrics: list[str] | None = None, filter: Any = None
    ) -> dict[str, Any]:
        """A server-side rollup ('revenue by country') — small result, rows never in context."""
        return await self.query(
            "nil.aggregate",
            {"target": target, "group_by": group_by, "metrics": metrics or ["count"], "filter": filter or []},
        )

    async def export(
        self,
        target: str,
        filter: Any = None,
        fields: list[str] | None = None,
        approved: bool = False,
    ) -> dict[str, Any]:
        """Stream a bulk read to a DATA HANDLE (not rows). Above the bulk threshold this is gated +
        audited (BULK_APPROVAL_REQUIRED until approved). Open the handle in the sandbox and use code."""
        return await self.query(
            "nil.export", {"target": target, "filter": filter or [], "fields": fields, "approved": approved}
        )

    async def status(self, proposal_id: str) -> dict[str, Any]:
        """The SSOT status of a proposal: state, replay flag, result, compensation handle."""
        status = await self._client.status(proposal_id)
        return status.model_dump(mode="json", exclude_none=True)

    async def rollback(
        self, compensation_token: str, reason: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """ROLLBACK: request a governed reversal. No side effect — returns a compensation *preview*
        (which the agent then commits via `nil_commit`) or an honest refusal."""
        try:
            reason_enum = RollbackReason(reason)
        except ValueError:
            valid = ", ".join(r.value for r in RollbackReason)
            return {"error": "invalid_reason", "message": f"reason must be one of: {valid}"}
        sid = self._sid(session_id)
        preview = await self._client.rollback(compensation_token, reason_enum)
        self._remember(sid, preview)  # so the reversal's own commit can be tier-gated
        return preview.model_dump(mode="json", exclude_none=True)

    def _approval_required(self, tier: str, note: str = "") -> dict[str, Any]:
        return {
            "committed": False,
            "outcome": "approval_required",
            "tier": tier,
            "message": (
                f"gate=human: a {tier} proposal needs owner approval in the control plane before "
                f"commit; lower tiers commit directly.{(' ' + note) if note else ''}"
            ),
        }

    async def _gate_decision(self, sid: str, proposal_id: str) -> dict[str, Any] | None:
        """Human gate: HIGH/CRITICAL proposals are HELD until the owner decides in the control plane.

        Returns None to allow the commit (gate off, low tier, or owner-approved); otherwise an
        approval_required / rejected envelope. Fails SAFE — if the control plane is unreachable, a
        gated proposal is held, never auto-committed.
        """
        if self._gate != "human":
            return None
        tier = self._proposals.get(sid, {}).get(proposal_id, {}).get("tier")
        if tier not in GATED_TIERS:
            return None  # low tiers commit directly even in human mode
        base = os.environ.get("NIL_APPROVAL_URL", "").rstrip("/")
        if not base:
            return self._approval_required(tier, "(no control plane configured)")
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(f"{base}/proposals/{proposal_id}/decision")
                status = resp.json().get("status") if resp.status_code < 400 else "unknown"
        except (httpx.HTTPError, ValueError):
            return self._approval_required(tier, "(control plane unreachable; holding)")
        if status == "approved":
            return None  # the owner approved → the write proceeds
        if status == "rejected":
            return {
                "committed": False,
                "outcome": "rejected",
                "tier": tier,
                "message": f"gate=human: a {tier} proposal was REJECTED by the owner; not committed",
            }
        # pending / unknown → register it for approval and hold. Pass the verb + human preview so the
        # owner's Decisions screen shows exactly WHAT they're approving (the gate is the only place
        # that still holds the proposal detail — a held proposal has no ledger event to enrich from).
        prop = self._proposals.get(sid, {}).get(proposal_id, {})
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                await c.post(
                    f"{base}/proposals/{proposal_id}/await",
                    json={"verb": prop.get("verb"), "tier": tier, "preview": prop.get("preview")},
                )
        except httpx.HTTPError:
            pass
        return self._approval_required(tier)

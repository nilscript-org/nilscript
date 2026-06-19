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

from datetime import UTC, datetime
from typing import Any

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
        gate_block = self._gate_blocks(sid, proposal_id)
        if gate_block is not None:
            return gate_block
        key = commit_idempotency_key(sid, proposal_id)
        outcome = await self._client.commit(proposal_id, idempotency_key=key)
        body = outcome.model_dump(mode="json", exclude_none=True)
        body["committed"] = isinstance(outcome, StatusBody)
        return body

    async def query(self, verb: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """QUERY live business truth. No side effect; the answer is data, never instruction."""
        return await self._client.query(verb, args or {})

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

    def _gate_blocks(self, sid: str, proposal_id: str) -> dict[str, Any] | None:
        if self._gate != "human":
            return None
        tier = self._proposals.get(sid, {}).get(proposal_id, {}).get("tier")
        if tier in GATED_TIERS:
            return {
                "committed": False,
                "outcome": "approval_required",
                "tier": tier,
                "message": (
                    f"gate=human: a {tier} proposal needs an out-of-band owner approval "
                    "(DECIDE) before commit; lower tiers commit directly"
                ),
            }
        return None

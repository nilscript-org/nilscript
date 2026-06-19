# NIL Governance & Control Plane — research + plan

**Status:** proposed · **Date:** 2026-06-19 · **Scope:** kernel + adapter + a new control-plane service

## The question

Today, when a Claude agent drives writes over MCP, the "approval" between **propose** and **commit**
happens **in the agent's chat** (the agent decides to commit). Is that the only / natural / best
model? Or should every action flow through the **kernel's own UI** so we have real oversight +
human approve/reject — making the kernel the permanent **control point + source of truth**, with a
governance toggle (auto-accept vs hold-for-our-approval) regardless of what the agent does? And if
actions already pass through the kernel, why does nothing show in the UI?

## Short answers (grounded in the code)

1. **Yes — today approval = "in the chat."** Default gate is `two-step` (`tools.py:31-33`): the agent
   self-approves by calling `commit`. This is **one of three modes**, not the only way.
2. **No, it's not the only/best model — and the kernel already anticipates owner-side approval**, but
   it isn't assembled:
   - `GATE_MODES = {two-step, human, auto}` and `gate="human"` **already blocks** HIGH/CRITICAL
     commits, returning `outcome:"approval_required"` — "a {tier} proposal needs an out-of-band owner
     approval (DECIDE) before commit" (`mcp/tools.py:138-151`).
   - The protocol has `ProposalState.PENDING_APPROVAL` (`sdk/sentences.py`), the spec documents
     adapter-side **"parking"** for owner approval, and the DSL has an `AwaitApprovalNode`
     (`kernel/models.py:112`) the executor polls (`kernel/executor.py:178`).
   - **But the queue/store/decide-endpoint/UI are NOT built.** `gate="human"` only returns a refusal;
     nothing persists, notifies, or exposes approve/reject.
3. **Yes — the per-agent governance toggle is exactly buildable** (auto vs hold-in-queue), and it maps
   directly onto the existing gate modes + a missing approval store.
4. **Why nothing shows in the UI:** the playground UI and the MCP path are **separate processes with no
   shared store**:
   - Playground `HISTORY` + `EVENTS` are in-memory, per-process, ephemeral, and only record actions
     through the playground's own `/api/chat→/api/commit` (`demo_ui.py:171,942,167`).
   - The MCP container drives its own adapter directly; it persists nothing (only in-memory
     `_proposals` per connection, `tools.py:75`) and never talks to the playground.
   - An audit signal **does** exist — the adapter **emits an EVENT on commit** (HMAC-signed, sequenced,
     `edge.py:289-296`) — but (a) only on COMMIT, not propose, and (b) the deployed MCP uses the
     in-memory `CapturingEmitter` with `NIL_EVENTS_WEBHOOK` unwired, so **nothing consumes it**.

**Conclusion:** every write *already* passes through the NIL gate (propose→commit at the adapter — that
IS the chokepoint). What's missing is making that chokepoint **emit every propose+commit to a central
store, render it in a UI, and optionally HOLD a proposal until a human approves.** The bones exist
(`gate=human`, `PENDING_APPROVAL`, EVENT emitter, `await_approval`, owner-plane `DECIDE`) — this plan
assembles them.

## Design principle

The adapter is the single chokepoint for writes; make it the single source of truth for **visibility**
and **approval** too. Governance is **policy at the gate**, configurable per grant/agent, independent
of what the agent does in its own chat.

```
ANY agent (MCP / playground / SDK)
      │ propose            │ commit
      ▼                    ▼
  ┌──────────────── NIL adapter (the gate) ────────────────┐
  │  emit EVENT(proposed) ──┐         emit EVENT(executed) │
  │  policy(grant): auto → commit flows                    │
  │                review → PARK as PENDING_APPROVAL ──────┼──► owner DECIDE (approve/reject)
  └──────────────────────────┬─────────────────────────────┘
                             ▼
                 Central event store  ◄── single pane of glass UI (every action, every agent)
```

## Phased plan

### Phase 1 — Visibility (single pane of glass)  *[cheapest, highest value; answers "why don't we see anything"]*
- **Emit PROPOSE events** (today only COMMIT is emitted): ~4 lines in the adapter PROPOSE handler
  (`edge.py:158/177`) → `EVENT{event:"proposed", proposal, verb, tier, preview}`; add `EventKind.PROPOSED`.
- **Wire the existing `HttpEventEmitter`** (`edge.py:80-101`) via `NIL_EVENTS_WEBHOOK`/`NIL_EVENTS_SECRET`
  to a new **central ingest** `POST /events/ingest`: HMAC-verify, dedup by `(workspace, sequence)`,
  persist (Mongo/Postgres) with `{source, grant, workspace, proposal, verb, tier, outcome, ts}`.
- **Control-plane UI**: list/stream (SSE) every propose/commit/rollback across MCP + playground + SDK,
  with a source badge. Read-only first.
- Deploy: set `NIL_EVENTS_WEBHOOK` in the mcp container env (host `.env`).

### Phase 2 — Human approval queue (the governance gate)
- **Persist pending proposals** (replace the ephemeral in-memory `_proposals`) keyed by `proposal_id`
  in the central store, with TTL/expiry.
- **Per-grant policy**: `auto` | `review` (+ which tiers). Stored centrally; resolved at the gate.
- In `review`, a commit on a held proposal returns `approval_required` (the `gate="human"` path already
  does this) and the proposal is parked `PENDING_APPROVAL` in the store + a notification (WhatsApp/email)
  fires to approvers.
- **Decide endpoint** (owner-plane `DECIDE`): `POST /proposals/{id}/decision {approve|reject, actor, reason}`
  → store transitions `APPROVED`/`REJECTED`. Authn: only approvers.
- The agent learns via `STATUS(pending_approval→approved)` — the SDK already polls `status` and the DSL
  `await_approval` node already routes on it. So the agent simply waits/retries; **the human decides in
  the UI regardless of the agent's chat.**

### Phase 3 — Unify + harden
- Point the **playground UI** at the central store too (so its History/Trace = the same single pane).
- TTL/expiry/escalation on stale pending approvals; audit immutability; per-tier and per-verb policy.
- Optional: tie into the multi-tenant work (per-tenant policy + per-tenant control-plane view).

## What this does NOT require
- No protocol change (uses existing `EVENT`, `PENDING_APPROVAL`, `STATUS`, `DECIDE`, `await_approval`).
- No agent-side change for `review` mode beyond honoring `approval_required` (already returned).
- Adapter changes are small + standardizable into the adapter template (emit-on-propose + event sink).

## Open decisions (for the owner)
1. **Where the control plane lives** — a new nilscript service (recommended; keeps the kernel as the
   control point) vs reuse an existing stack.
2. **Default policy** per agent — `auto` (frictionless, audit-only) vs `review` (hold HIGH+/all). Likely
   `auto` + audit for trusted agents, `review` for untrusted/destructive tiers.
3. **Store** — Mongo vs Postgres for the event/approval store.
4. **Decide surface** — dashboard only, or also WhatsApp/email actionable approvals (owner-plane DECIDE).

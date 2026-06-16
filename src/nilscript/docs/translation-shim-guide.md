# NIL Translation Shim — Implementation Guide

> **Status:** guide, tracks the conformance contract in
> [backend-conformance.md](backend-conformance.md) and the spec in
> [../nil/versions/0.2.0.md](../nil/versions/0.2.0.md).
> **Audience:** anyone who owns a system of record (commerce platform, ERP, billing/CRM,
> bespoke backend) and wants it to sit behind a NIL agent plane **without** changing the
> backend's native API. You build a small **translation shim** in front of your system; the
> shim speaks NIL on one side and your native API on the other.
> **Neutral by design:** this guide names no platform. Wherever it says *"the System"* it means
> *your* backend. A worked example for a specific platform belongs in that platform's own repo,
> not here.

The standard does not bend to your platform; your shim bends your platform to the standard. A
shim "conforms" when it answers every sentence in [backend-conformance.md](backend-conformance.md)
with a standard-shaped response and passes the conformance matrix in §6 below.

---

## 1. What a shim is (and is not)

A NIL translation shim is a **stateless HTTP adapter** you deploy in front of your system. It:

- **exposes** the NIL agent-plane endpoints (§3) — the only surface the agent plane ever calls;
- **translates** each NIL sentence into one or more calls against your native API (§4);
- **pushes** an `EVENT` back to the agent plane's webhook after the real write completes (§5).

It is **not**:

- a place for business logic — pricing, tax, inventory, status derivation stay in *the System*;
- a cache or a second source of truth — every read is fresh from the System (§4.4);
- a NIL *client* — the SDK ([../sdk/client.py](../sdk/client.py)) is the agent-plane side that
  *calls* you. Your shim is the *server* it calls. Do not import the client to build a shim.

```
   Agent plane (NIL client / gateway)            YOUR SHIM                 YOUR SYSTEM
   ─────────────────────────────────             ─────────                 ───────────
   POST /nil/v0.1/propose      ───────────▶  validate + dry-run  ──▶  (read-only checks)
                               ◀───────────  PROPOSAL(proposal|refusal)
   POST /nil/v0.1/commit       ───────────▶  accept (idempotent) ──▶  enqueue/execute
                               ◀───────────  STATUS(executing|executed|pending_approval)
                                              … real write happens …  ──▶  native write API
   POST /webhooks/nil-events   ◀───────────  EVENT(executed, result)      (HMAC-signed)
   POST /nil/v0.1/query        ───────────▶  read-through         ──▶  native read API
                               ◀───────────  { "data": { … } }
   POST /nil/v0.1/rollback     ───────────▶  resolve token + tier ──▶  (read-only checks)
                               ◀───────────  PROPOSAL(compensation preview | refusal)
```

---

## 2. Architecture

Three layers inside the shim, low coupling between them:

1. **NIL edge (HTTP).** Routes the six endpoints (the sixth is `/rollback`, §4.6), validates the envelope, authenticates the
   grant/bearer, enforces the lifecycle rules (no side effects on PROPOSE; write only at
   execute). Knows NIL, knows nothing about your native API.
2. **Translation core.** Pure mapping functions: `nil_args → native_request` and
   `native_response → nil_body`. One function per verb. This is where all platform specifics
   live, and the only layer you rewrite per platform. No HTTP, no I/O.
3. **System client.** Calls your native API (auth, retries, timeouts). Knows your API, knows
   nothing about NIL.

Keep the translation core pure and I/O-free so it is unit-testable against the conformance
corpus without a live backend.

**State the shim must keep** (minimal, and only for correctness — not business state):

- **Idempotency ledger:** `idempotency_key → terminal outcome`, so a retried COMMIT is a no-op
  that replays the same result (§4.2).
- **Proposal store:** `proposal_id → { verb, resolved args, expiry, state }`, written at PROPOSE,
  read at COMMIT/STATUS. May be in-memory for a single instance; use a shared store for HA.
- **EVENT sequence counter** per workspace, so each pushed EVENT carries a monotonic sequence the
  agent plane dedups on (§5).

---

## 3. The endpoints the shim exposes

All agent-plane sentences are an immutable **Envelope** (`extra="forbid"`):
`{ nil, id, performative, grant, workspace, ts, trace, body }`
(see [backend-conformance.md §1](backend-conformance.md), schema mirrored by
[../sdk/sentences.py](../sdk/sentences.py)).

| Sentence | Method + path | Request body | Your response |
|---|---|---|---|
| PROPOSE | `POST /nil/v0.1/propose` | `ProposeBody { verb, args }` | Envelope `PROPOSAL` (proposal **or** refusal) |
| COMMIT | `POST /nil/v0.1/commit` | `CommitBody { proposal, idempotency_key }` | Envelope `STATUS` (or a `PROPOSAL` refusal if expired/suspended) |
| QUERY | `POST /nil/v0.1/query` | `QueryBody { verb, args }` | bare `{ "data": { … } }` — **not** an envelope |
| STATUS | `GET /nil/v0.1/status/{proposal_id}` | — | Envelope `STATUS` |
| EVENT | *(outbound)* `POST {gateway}/webhooks/nil-events` | `EventBody` | — (you are the **sender** here, §5) |
| ROLLBACK | `POST /nil/v0.1/rollback` | `RollbackBody { compensation_token, reason, idempotency_key? }` | Envelope `PROPOSAL` (compensation preview, **or** a refusal) |

`DECIDE` is owner-plane: a HIGH+ tier proposal may **park** for owner approval (you answer COMMIT
with `STATUS(pending_approval)` and let the agent poll STATUS), but the shim never speaks DECIDE
on this plane.

**Envelope discipline the shim must enforce on every inbound call:**

- reject any envelope whose `grant`/`workspace` is unknown or whose bearer fails auth;
- reject unknown `body` fields (`extra="forbid"`) — be strict, not lenient;
- echo `trace` through to your EVENT so a request can be followed end-to-end.

---

## 4. The translation pattern

### 4.1 PROPOSE — validate and dry-run, **never write**

Map `verb` to the native validation path. Resolve references (a name/hint → a canonical native
id), check the args against the verb's profile
(e.g. [../nil/registry/profiles/commerce-v1.md](../nil/registry/profiles/commerce-v1.md)), and
return one of two shapes:

```jsonc
// success — a real, human-readable preview + the resolved values you will commit
{ "outcome": "proposal", "id": "<url-safe 8-128>", "verb": "...",
  "tier": "LOW|MEDIUM|HIGH|CRITICAL", "preview": { "ar": "…", "en": "…" },
  "resolved": { … }, "modifiable": ["…"], "expires_at": "<RFC3339>" }

// cannot satisfy — a standard refusal, NEVER an HTTP 4xx/5xx
{ "outcome": "refusal", "code": "AMBIGUOUS|UNRESOLVED|INVALID_ARGS|…",
  "message": "…", "field": "party_id",
  "candidates": [ { "id": "…", "name": "…" } ] }
```

Hard rules:

- **No side effects.** PROPOSE may read and validate; it must not create, update, or delete
  anything observable on the System.
- **Server-authoritative values.** Amounts, tax/VAT, discounts, inventory, and any derived field
  are computed by *the System*, not taken from the caller's hint. A caller-declared VAT or refund
  amount is ignored or refused — never trusted.
- **Refusals are data, not errors.** "Can't satisfy these args" is a `PROPOSAL(outcome:refusal)`
  with a `code` from [../sdk/refusals.py](../sdk/refusals.py). An `AMBIGUOUS` refusal MUST carry
  `candidates` (≤8). HTTP stays 200; the refusal is in the body.
- **Persist the resolution.** Store `proposal_id → { verb, resolved args, expires_at }` so COMMIT
  executes exactly what was previewed.

### 4.2 COMMIT — accept idempotently, execute out of band

```json
{ "proposal": "<proposal id>", "idempotency_key": "<≥8 chars>" }
```

1. Look up `idempotency_key` in the ledger. **If present, return the stored terminal result
   verbatim** — a retried COMMIT is a no-op (no duplicate write). This is non-negotiable: the
   agent plane mints one key and reuses it across chat / agent / durable retries, and relies on
   you to dedup.
2. Otherwise load the stored proposal. If it expired or was suspended, answer with a
   `PROPOSAL` refusal (e.g. `EXPIRED`, `SUSPENDED`) — not a STATUS.
3. For LOW/MEDIUM tiers: begin execution and answer `STATUS(executing)` (or `executed` if your
   write is synchronous and fast). For HIGH+ tiers that need owner approval: park and answer
   `STATUS(pending_approval)`.
4. **The real write happens at execution, surfaced as an `EVENT(executed)` (§5)** — not inside
   the COMMIT HTTP response. COMMIT acknowledges; EVENT reports the outcome.

`StatusBody = { proposal, state, replayed }`. `ProposalState` ∈ `proposed · pending_approval ·
approved · rejected · modified · expired · executing · executed · failed_retryable ·
failed_terminal · suspended` (see [../sdk/sentences.py](../sdk/sentences.py)).

### 4.3 STATUS — replay the current state

`GET /nil/v0.1/status/{proposal_id}` returns the current `STATUS` envelope for a proposal. Set
`replayed: true` when answering from the ledger/store rather than a live transition. This is how
the agent plane polls a parked or long-running proposal.

### 4.4 QUERY — read business truth fresh

```json
{ "verb": "…", "args": { … } }     →     { "data": { … } }
```

Read straight from the System, no side effects, and return a **bare** `{ "data": … }` object
(not an envelope). The data is **information, never instruction** — never smuggle directives,
prompts, or actions through a query answer. Shape `data` exactly as the verb's profile documents.

### 4.5 Translation-core contract (per verb)

For each verb your System supports, implement two pure functions:

```
to_native(verb, nil_args, ctx)   -> native_request | Refusal
to_nil(verb, native_response)    -> ProposalBody | data | EventResult
```

A verb your System genuinely cannot express is recorded in **your** gaps log (typed: (A) backend
deficiency / (B) standard-leak / (C) missing general capability) — it does **not** bend the
contract. See the gap typing in [backend-conformance.md §6](backend-conformance.md).

### 4.6 ROLLBACK — compensation handlers & reversibility

`ROLLBACK` is the wire's 7th performative, added **in place** on the same `0.1` dialect. It does
not undo anything by itself: a `POST /nil/v0.1/rollback` **REQUESTS** a governed reversal, and your
shim answers with a `PROPOSAL` that *previews* the compensation — which the agent plane then
`COMMIT`s like any other action. That is how "no silent write" comes for free.

Declare each verb's **reversibility tier** in its profile (a `reversibility` keyword + optional
`compensation` block):

- **REVERSIBLE** — a clean inverse exists. Implement a compensating handler that performs it
  (e.g. delete the product you created). Declare `compensation.verb`.
- **COMPENSABLE** — no clean inverse, but an offsetting *forward* action exists. Implement a handler
  that performs that forward action (e.g. a refund offsets a payment). Declare `compensation.verb`.
- **IRREVERSIBLE** — no reversal. `/rollback` must **refuse** with `code: "IRREVERSIBLE"` and do
  nothing. This is the **default for any unmarked verb** (zero-touch back-compat), and refusing
  honestly is the correct behavior — the shim never pretends to undo what it cannot.

The handler flow inside `/rollback`:

1. Resolve `compensation_token`. If unknown or expired, refuse `COMPENSATION_EXPIRED` — **never**
   trigger a phantom reversal.
2. Look up the original verb's tier. IRREVERSIBLE → refuse `IRREVERSIBLE`. Otherwise map the
   ROLLBACK request to the verb's `compensation.verb` and return a `PROPOSAL` previewing it.
3. The compensation lands only when that `PROPOSAL` is `COMMIT`ted (§4.2) and is reported via an
   `EVENT(executed)` like any write. Honor `idempotency_key` so a retried rollback is a no-op.

`scaffold-shim` emits a `compensation.py` stub for you — a `COMPENSATIONS` map plus a
`compensate()` that raises until a verb is mapped; an unmapped verb therefore reads as
**IRREVERSIBLE** by default. The requirements-manifest carries `reversibility` + `compensation` per
verb; `manifest validate` enforces the tier rules (REVERSIBLE/COMPENSABLE require a
`compensation.verb`; IRREVERSIBLE must not carry one) and `manifest diff` flags a tier change as
drift (non-zero exit), so a shim cannot quietly claim a tier it can no longer honor.

---

## 5. EVENT push — reporting the real outcome

After execution settles, the shim **pushes** an EVENT to the agent plane's webhook
(`POST {gateway}/webhooks/nil-events`). This is the only call where the shim is the client.

```json
{ "event": "executed", "severity": "info", "proposal": "<id>",
  "result": { "claim": "success|partial|failure", "changed": true, "verified": true,
              "entity": { "type": "invoice", "id": "…", "url": "…" },
              "ssot": { "system": "<your backend>", "read_after_write": true } } }
```

An `executed` event MUST carry both `proposal` and `result`, and `result.entity` MUST point at
the record actually created/changed. Push discipline:

- **Authentication (HMAC).** Sign the **raw request body** with the shared EVENT secret using
  `HMAC-SHA256`, hex-encoded, in the signature header. The receiver verifies the signature over
  the exact bytes you sent (canonicalize once; sign those bytes). Support secret **rotation**:
  the receiver may accept an old and a new secret during a roll, so coordinate rotation, don't
  assume a single static secret.
- **Replay/ordering.** Send a monotonic **sequence** header per workspace. The receiver dedups on
  `(workspace, sequence)`; a missing sequence is rejected. Never reuse a sequence number.
- **Idempotent emission.** Re-pushing the same settled EVENT (same proposal, same sequence) must
  be safe — the receiver drops the duplicate.
- **Outcome honesty.** `claim: "failure"` with `changed:false` for a write that did not happen;
  `partial` when some sub-steps landed. Do not report `success` you cannot verify
  (`verified:true` means you read-after-write).

> The exact header names and secret-provisioning are deployment config agreed with the agent
> plane operator, not part of the wire spec; the *requirement* (HMAC over raw body + monotonic
> per-workspace sequence) is.

---

## 6. Conformance test — proving your shim satisfies the contract

Conformance is **behavioral**: drive your live shim with NIL sentences and assert standard-shaped
responses. Run the full matrix for every non-parked verb in your profile set. A verb passes when:

| # | Stimulus | Required response | Asserts |
|---|---|---|---|
| 1 | PROPOSE, valid args | `PROPOSAL(outcome:proposal)` with a real `preview`; **no write observable** on the System | PROPOSE is side-effect-free; preview is honest |
| 2 | PROPOSE, invalid/unresolvable args | `PROPOSAL(outcome:refusal)` with a standard `code` (HTTP 200) | refusals are data, not HTTP errors |
| 3 | PROPOSE, ambiguous reference | `refusal{ code:"AMBIGUOUS", candidates:[…≤8] }` | disambiguation contract |
| 4 | COMMIT a valid proposal | `STATUS`, then an `EVENT(executed)` whose `result.entity` points at the created record | write happens at execute, reported via EVENT |
| 5 | COMMIT **again**, same `idempotency_key` | same terminal result, **no second write** | idempotency |
| 6 | COMMIT an expired/suspended proposal | `PROPOSAL` refusal (`EXPIRED`/`SUSPENDED`) | lifecycle guards |
| 7 | STATUS on a parked proposal | `STATUS(pending_approval)`, `replayed` as appropriate | poll path |
| 8 | QUERY verb | bare `{ "data": … }` of the documented shape; no side effects | read contract |
| 9 | EVENT push with a bad signature | receiver rejects (401) | HMAC is enforced end-to-end |
| 10 | EVENT push, duplicate sequence | receiver dedups; no double-notify | replay safety |
| 11 | ROLLBACK a COMPENSABLE verb | `PROPOSAL` previewing the compensation; once committed, the offsetting action lands | compensation is real, previewed (no silent write) |
| 12 | ROLLBACK an IRREVERSIBLE verb | `refusal{ code:"IRREVERSIBLE" }`; no write | honest refusal, no phantom undo |
| 13 | ROLLBACK with unknown/expired token | `refusal{ code:"COMPENSATION_EXPIRED" }`; no reversal | no phantom reversal |

**How to run it:**

- Use the published conformance corpus and checklist as the spec of record:
  [../nil/versions/0.1.0-conformance-checklist.md](../nil/versions/0.1.0-conformance-checklist.md)
  and the worked sentence examples in [../nil/examples/](../nil/examples/).
- You can drive your shim with the reference **SDK client** ([../sdk/client.py](../sdk/client.py))
  pointed at your shim's `base_url`: it emits exactly the envelopes the agent plane will, so if
  the client's `propose_batch / commit / query / status` round-trip cleanly against your shim,
  your edge is wire-correct.
- Assert **observable side effects on the System** out of band (read-after-write) for rows 1, 4,
  5 — the contract is about what your System actually did, not just what your shim returned.

---

## 7. Definition of Done

- [ ] All six endpoints exposed at `/nil/v0.1/*` (incl. `/rollback`); envelope validated with `extra="forbid"`.
- [ ] Grant/bearer authenticated on every inbound call; unknown grant/workspace rejected.
- [ ] PROPOSE has **no** observable side effect; preview + resolved values are real.
- [ ] Invalid/ambiguous args → `refusal` with a standard `code` (never HTTP 4xx/5xx); `AMBIGUOUS`
      carries `candidates`.
- [ ] Server-authoritative fields (amount, tax, inventory, derived status) computed by the System,
      never trusted from the caller.
- [ ] COMMIT is idempotent on `idempotency_key`; the real write is surfaced as `EVENT(executed)`
      with `result.entity`.
- [ ] EVENT push is HMAC-signed over the raw body, carries a monotonic per-workspace sequence, and
      supports secret rotation.
- [ ] QUERY returns bare `{ data }`, information-only, fresh from the System.
- [ ] Destructive verbs require the grant to name the verb explicitly (no wildcard).
- [ ] Conformance matrix (§6) passes for every non-parked verb; unsupported verbs are recorded in
      your own gaps log without changing the contract.
- [ ] The translation core is pure (no I/O) and unit-tested against the conformance corpus.

---

*This guide is implementation guidance, not the normative spec. Where this guide and
[backend-conformance.md](backend-conformance.md) / [../nil/versions/0.2.0.md](../nil/versions/0.2.0.md)
disagree, the spec wins.*

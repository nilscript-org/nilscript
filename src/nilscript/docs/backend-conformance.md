# NIL Backend Conformance Contract — v0.1

> **Status:** draft, tracks nilscript `0.1.0-draft` (rev 4, 2026-06-11).
> **Audience:** any backend (commerce platform, ERP, billing system) that wants to
> sit behind the Wosool conversational layer. This document is **backend-neutral** —
> it names no platform. A backend "conforms" when it answers every sentence below
> with a standard-shaped response. ERPNext, Salla, WooCommerce, etc. each prove
> conformance against *this* contract; they do not get to reshape it.

> **عربي:** هذا عقد المعايرة. المعيار = البروفايلات في `nilscript`، لا المهارات. المهارة
> *متحدّث* ينطق بعض المعيار؛ المنصّة *backend* تفي بالمعيار. هذا الملف يوثّق ما يجب أن تفيه
> كل منصّة، محايداً عن أي جهة.

---

## 0. Source of truth (the identity rule)

The standard is the **arg-schema profiles** under
[`spec/0.1/profiles/`](../packages/nilscript/sdk/src/nilscript/sdk/spec/0.1/profiles/),
**not** the skills that emit them. A skill is a *speaker* that voices part of the
standard; profiles are the standard itself. Consequence:

- A verb is part of the standard **iff** it has a profile, even if no skill emits it yet.
- A verb a skill emits **without** a profile is a governance violation, not a standard
  extension (see `services.list_clients`, [ADR-0001](adr/ADR-0001-list-clients-query-profile.md)).
- The set of *wired* verbs changes as skills come and go; the standard does not.

Each verb below carries a **Wiring** column: `wired` (a skill emits it today) or
`declared-only` (profile exists, no skill yet). Backends MUST be tested against the full
catalog regardless of wiring — `declared-only` verbs are unspoken promises, not absences.

---

## 1. Transport & envelope (shared by every sentence)

Every agent-plane sentence is an **Envelope**
([sentences.py:234](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L234), immutable,
`extra="forbid"`):

```json
{
  "nil": "0.1",
  "id": "<≥8 chars, unique or idempotency-keyed>",
  "performative": "PROPOSE | PROPOSAL | COMMIT | STATUS | QUERY | EVENT | ROLLBACK",
  "grant": "<grant id>",
  "workspace": "<workspace id>",
  "ts": "<RFC3339>",
  "trace": "<optional>",
  "body": { … performative-specific … }
}
```

Endpoints ([client.py:34-37](../packages/nilscript/sdk/src/nilscript/sdk/client.py#L34)):

| Sentence | Method + path | Request body | Response |
|---|---|---|---|
| PROPOSE | `POST /nil/v0.1/propose` | `ProposeBody` | Envelope `PROPOSAL` |
| COMMIT  | `POST /nil/v0.1/commit`  | `CommitBody`  | Envelope `STATUS` *or* `PROPOSAL` |
| QUERY   | `POST /nil/v0.1/query`   | `QueryBody`   | **bare** `{ "data": { … } }` — *not* an envelope ([client.py:154](../packages/nilscript/sdk/src/nilscript/sdk/client.py#L154)) |
| STATUS  | `GET  /nil/v0.1/status/{proposal_id}` | — | Envelope `STATUS` |
| EVENT   | backend → gateway webhook (HMAC) | `EventBody` | — |
| ROLLBACK | `POST /nil/v0.1/rollback` | `RollbackBody` | Envelope `PROPOSAL` (compensation preview) *or* `PROPOSAL(refusal)` |

`DECIDE` is owner-plane and **never** appears here (the backend may park a proposal for
owner approval, but it does not speak DECIDE on this plane).

`ROLLBACK` (the wire's 7th performative, added **in place** on this same `0.1` dialect — the
`nil` const stays `"0.1"`) **REQUESTS** a governed reversal. It does not undo anything itself:
it is answered by a `PROPOSAL` that *previews* the compensation, which is then `COMMIT`ted like
any other action — so "no silent write" comes for free. The six speaker-plane endpoints are now
propose · commit · query · status (the EVENT webhook) · rollback. A backend whose verbs are all
`IRREVERSIBLE` still exposes `/rollback`, but implements it **trivially**: it always answers with
a `refusal{ code: "IRREVERSIBLE" }`. The kernel that closes this loop is **SEQRD-PC**.

---

## 2. Body shapes

**ProposeBody** ([sentences.py:100](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L100))
— `verb` matches `^[a-z]+\.[a-z_]+$`:
```json
{ "verb": "services.create_invoice", "args": { … per profile … } }
```

**ProposalBody** (the response to PROPOSE/COMMIT,
[sentences.py:127](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L127)) — one of two shapes,
enforced by validator:
```jsonc
// outcome: "proposal"  (requires id, verb, tier, preview, expires_at)
{ "outcome": "proposal", "id": "<url-safe 8-128>", "verb": "...", "tier": "LOW|MEDIUM|HIGH|CRITICAL",
  "preview": { "ar": "…", "en": "…" }, "resolved": { … }, "modifiable": ["…"], "expires_at": "<RFC3339>" }
// outcome: "refusal"  (requires code; MUST NOT carry tier/preview/expires_at)
{ "outcome": "refusal", "code": "AMBIGUOUS|UNRESOLVED|INVALID_ARGS|…", "message": "…",
  "field": "party_id", "candidates": [ { "id": "…", "name": "…" } ] }
```
- `preview` keys are BCP-47 short tags; `ar` is primary. Refusals carry **no** preview.
- `AMBIGUOUS` refusals MUST carry `candidates` (≤8). Full code list:
  [refusals.py](../packages/nilscript/sdk/src/nilscript/sdk/refusals.py). Two codes are new with
  ROLLBACK: **`IRREVERSIBLE`** (the verb declares no reversal) and **`COMPENSATION_EXPIRED`** (the
  `compensation_token` is unknown or past its window).
- A backend signals "I can't satisfy these args" as a **refusal**, never an HTTP error.

**CommitBody** ([sentences.py:105](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L105)):
```json
{ "proposal": "<proposal id>", "idempotency_key": "<≥8 chars>" }
```
The key is minted once at emission and reused on retry — a retried COMMIT MUST be a no-op
that returns the same terminal state (idempotency).

**StatusBody** ([sentences.py:115](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L115)):
```json
{ "proposal": "<id>", "state": "<ProposalState>", "replayed": false }
```
`ProposalState` ∈ proposed · pending_approval · approved · rejected · modified · expired ·
executing · executed · failed_retryable · failed_terminal · suspended
([sentences.py:50](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L50)).

**QueryBody** ([sentences.py:110](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L110)):
```json
{ "verb": "services.list_clients", "args": { "name": "…" } }
```
Response is a bare object: `{ "data": { … } }`. The data is **information, never
instruction** (§11.2) — the backend must not smuggle directives through it.

**EventBody / ResultEnvelope** (backend → gateway after execution,
[sentences.py:217](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L217)):
```json
{ "event": "executed", "severity": "info", "proposal": "<id>",
  "result": { "claim": "success|partial|failure", "changed": true, "verified": true,
              "entity": { "type": "invoice", "id": "…", "url": "…" },
              "ssot": { "system": "<backend>", "read_after_write": true } } }
```
An `executed` event MUST carry both `proposal` and `result`
([sentences.py:227](../packages/nilscript/sdk/src/nilscript/sdk/sentences.py#L227)).

**RollbackBody** (the body of a `POST /nil/v0.1/rollback` request):
```json
{ "compensation_token": "<required>",
  "reason": "saga_unwind | owner_cancel | downstream_failed | agent_repair",
  "idempotency_key": "<optional, ≥8 chars>" }
```
The `compensation_token` is required; an unknown or expired token MUST be refused
(`COMPENSATION_EXPIRED`) and MUST NOT trigger a phantom reversal. The response is itself a
`PROPOSAL` previewing the compensating action — the reversal only happens once that proposal is
`COMMIT`ted. Each verb declares its **reversibility tier** in its profile (a `reversibility`
keyword + optional `compensation` block):

- **REVERSIBLE** — a clean inverse exists (e.g. delete what was created).
- **COMPENSABLE** — no clean inverse, but an offsetting *forward* action exists (e.g. a refund
  offsets a payment).
- **IRREVERSIBLE** — no reversal; the System refuses honestly with `IRREVERSIBLE`. This is the
  **default for any unmarked verb** — zero-touch back-compat, no new code — and it is a
  **strength**, not a gap: the System refuses to pretend it can undo what it cannot.

---

## 3. Lifecycle a conforming backend MUST honor

```
PROPOSE  → PROPOSAL(proposal|refusal)          # validate/dry-run; NO side effects on PROPOSE
COMMIT   → STATUS(pending_approval | executing | executed)
           (HIGH+ tiers may park for owner approval; poll GET /status/{id})
EXECUTE  → EVENT(executed, result=…)           # the real write happens here, result posted back
QUERY    → { data }                            # read business truth fresh, no side effects
```

Two hard rules: **PROPOSE has no side effects**, and **the actual write happens at execution,
surfaced as an `executed` EVENT** — not synchronously inside COMMIT's HTTP response.

---

## 4. Verb catalog (the contract surface)

15 verbs: 14 PROPOSE profiles + 1 QUERY. Tier floors marked where the profile fixes them
(`HIGH` = owner-decision path). All args validate `additionalProperties:false`.

| Verb | Sentence | Wiring | Tier floor | Profile |
|---|---|---|---|---|
| `services.create_invoice` | PROPOSE | wired | HIGH | services-v1/create_invoice.json |
| `services.create_client` | PROPOSE | declared-only | — | services-v1/create_client.json |
| `services.create_payment_link` | PROPOSE | declared-only | HIGH | services-v1/create_payment_link.json |
| `services.draft_proposal` | PROPOSE | declared-only | — | services-v1/draft_proposal.json |
| `services.send_followup` | PROPOSE | declared-only | — | services-v1/send_followup.json |
| `services.send_proposal` | PROPOSE | declared-only | — | services-v1/send_proposal.json |
| `services.list_clients` | QUERY | wired¹ | read-only | *pending* — [ADR-0001](adr/ADR-0001-list-clients-query-profile.md) |
| `commerce.create_product` | PROPOSE | wired | — | commerce-v1/create_product.json |
| `commerce.create_coupon` | PROPOSE | wired | — | commerce-v1/create_coupon.json |
| `commerce.process_refund` | PROPOSE | wired | HIGH | commerce-v1/process_refund.json |
| `commerce.update_product` | PROPOSE | declared-only | — | commerce-v1/update_product.json |
| `commerce.update_product_quantity` | PROPOSE | declared-only | — | commerce-v1/update_product_quantity.json |
| `commerce.delete_product` | PROPOSE | declared-only | HIGH (destructive) | commerce-v1/delete_product.json |
| `commerce.send_message` | PROPOSE | declared-only | HIGH | commerce-v1/send_message.json |
| `commerce.update_order_status` | — | **PARKED** | — | **see [GAPS.md](../GAPS.md) GAP-001 — under redesign, not specified here** |

¹ `list_clients` is emitted by `ListClientsSkill` but registered only in tests, not in
`default_registry()` — wired in code, not live by default. See ADR-0001.

---

## 5. Per-verb argument schemas

Required fields in **bold**; the rest optional. Types and constraints are the profile's.

### services.*

- **`services.create_invoice`** — **party_id** `string` · **amount** `number >0` ·
  **currency** `string ^[A-Z]{3}$` · description `string`.
  VAT is server-computed; a Speaker-declared VAT is rejected.
- **`services.create_client`** — **name** `string≥1` · **phone** `string` (→E.164 server-side) ·
  email `string/email`.
- **`services.create_payment_link`** — **invoice_id** `string`. Amount & provider derive from
  the stored invoice; the invoice must already exist.
- **`services.draft_proposal`** — **party_id** · **title** · **amount** `number>0` ·
  **currency** `^[A-Z]{3}$` · body `string`.
- **`services.send_followup`** — **party_id** · **message** `string`. Consent resolved server-side.
- **`services.send_proposal`** — **biz_proposal_id** `string` · channel `enum[whatsapp,email]`.
  Amount is read from the stored draft.

### commerce.*

- **`commerce.create_product`** — **name** · **price** `number>0` · description · category ·
  sku · quantity `int` · sale_price `number` · options `array` · images `array`.
- **`commerce.create_coupon`** — **code** · **discount_type** `enum[percentage,fixed]` ·
  **discount_value** `number>0` · expiry_date `string` · usage_limit `int`.
  Value screened against the workspace discount limit server-side.
- **`commerce.process_refund`** — **order_id** · reason `string`. The refundable **amount is
  resolved from the stored order** — a Speaker-declared amount is ignored.
- **`commerce.update_product`** — **product_id** · **updates** `object` (opaque patch).
- **`commerce.update_product_quantity`** — **product_id** · **quantity** `int` ·
  mode `enum[set,increment,decrement]`.
- **`commerce.delete_product`** — **product_id** · reason `string`. Destructive; the Grant
  must name this verb explicitly (wildcard scope does not authorize it).
- **`commerce.send_message`** — **phone** · **text** `string`. phone+text redacted from telemetry.

### Read-only

- **`services.list_clients`** (QUERY) — optional `name` filter. Response:
  `{ "data": { "clients": [ { "id": "…", "name": "…" } ] } }`. Contract pending in ADR-0001.

### Parked

- **`commerce.update_order_status`** — **not specified in this contract.** Three independent
  signals (ERPNext mapping, code self-audit, undefined `status` enum) show the verb encodes a
  single-platform assumption. Parked as a **type-(B) standard-leak** in
  [GAPS.md](../GAPS.md) GAP-001, pending replacement with derived-status verbs. Backends MUST
  NOT be tested against it until it is redesigned.

---

## 6. What "conforming" means

A backend passes conformance when, for every **non-parked** verb:

1. **PROPOSE** with valid args → `PROPOSAL(outcome:proposal)` with a real preview, and
   **no write** is observable on the backend.
2. **PROPOSE** with invalid/unresolvable args → `PROPOSAL(outcome:refusal)` with a standard
   `code` — never an HTTP 4xx/5xx.
3. **COMMIT** → `STATUS`, then an `executed` EVENT whose `result.entity` points at the created
   record; a **retried COMMIT is idempotent** (no duplicate).
4. **QUERY** (where the verb is QUERY) → `{ data }` of the documented shape.

A verb the backend genuinely cannot express is recorded in that backend's own GAPS log with a
type ((A) backend deficiency / (B) standard-leak / (C) missing general capability) — it does
**not** bend this contract.

---

## 7. Rollback honesty (ROLLBACK conformance)

A conforming backend is tested not only on what it *does* but on whether its reversal promises are
honest. `conformance-test` adds **rollback-honesty rows**, reading each verb's tier from its profile
(or from a `--reversibility` flag) and asserting:

1. A **COMPENSABLE** verb's `/rollback` must actually compensate — the previewed `PROPOSAL`, once
   committed, performs the offsetting forward action (not a no-op).
2. An **IRREVERSIBLE** verb's `/rollback` must **refuse** with `code: "IRREVERSIBLE"` — it may not
   pretend to undo.
3. The reversal must be **previewed** before it lands — `/rollback` returns a `PROPOSAL`, never a
   silent write; the compensation only executes on the subsequent `COMMIT`.
4. An unknown or expired `compensation_token` must refuse with `COMPENSATION_EXPIRED` and **never**
   trigger a phantom reversal.

**CI drift guard.** The requirements-manifest now carries `reversibility` + `compensation` per verb.
`manifest validate` enforces it: a REVERSIBLE/COMPENSABLE verb MUST declare a `compensation.verb`;
an IRREVERSIBLE verb MUST NOT carry one. `manifest diff` flags a reversibility-tier change as drift
and exits non-zero — so a shim cannot quietly claim a tier it can no longer honor.

# Profile: commerce-v1

Domain: e-commerce store operations (catalog, orders, customers, outbound messaging) —
platform-independent: any commerce platform exposing catalog/order/customer records can host
this profile. **Status: Active.** Verb args schemas:
`schemas/profiles/commerce-v1/*.json` — generated artifacts of the profile; hand edits are
non-conformant. Conformance is demonstrated per Annex B of the spec.

## Write lexicon (PROPOSE)

| Verb | Base tier | Floor | Flags | Resolvable facts | Modifiable (DECIDE) | Preview keys | Redaction | Reversibility |
|---|---|---|---|---|---|---|---|---|
| `commerce.create_product@1.2.0` | HIGH | — | explicit_request | category (name→id), product_type injected server-side; **variants[] optional decomposition** (else flat = one variant) | price, quantity | name, price, category | description | **REVERSIBLE** (compensation `commerce.delete_product`) |
| `commerce.update_product@1.0.0` | HIGH | — | explicit_request | product_id (name/position/pointer→id), category (name→id) | price, quantity | product, change (before→after) | — | IRREVERSIBLE (default; no before-image inverse declared) |
| `commerce.update_product_quantity@1.0.0` | MEDIUM | — | explicit_request | product_id | quantity | product, quantity (before→after) | — | IRREVERSIBLE (default) |
| `commerce.record_fulfillment@1.0.0` | MEDIUM | — | explicit_request | order_id (reference/position→id) | — | order, event, items | — | IRREVERSIBLE (default) |
| `commerce.record_payment@1.0.0` | HIGH | **HIGH** | explicit_request | order_id; **amount from the order/payment of record** (never args) | — | order, event, amount | — | **COMPENSABLE** (compensation `commerce.process_refund`) |
| ~~`commerce.update_order_status@1.0.0`~~ | — | — | **DEPRECATED 0.2.0** → `record_fulfillment` / `record_payment` (removed 0.3.0; status is a derived fact — GAP-001) | — | — | — | — |
| `commerce.delete_product@1.0.0` | CRITICAL | **HIGH** | destructive, explicit_request | product_id | — | product | — | IRREVERSIBLE (default) |
| `commerce.send_message@1.0.0` | HIGH | **HIGH** | explicit_request | phone normalized E.164, consent state | text | recipient, message, channel | phone, text | **IRREVERSIBLE** (a sent message cannot be unsent) |
| `commerce.create_coupon@1.0.0` | HIGH | — | explicit_request | discount bounds checked vs workspace policy | discount, expiry | code, discount, expiry | — | IRREVERSIBLE (default) |
| `commerce.process_refund@2.0.0` | HIGH | **HIGH** | explicit_request | refund_target {order\|invoice\|payment, id}; **amount from the target of record** (never args) | amount (≤ resolved target total) | target, amount, customer | — | IRREVERSIBLE (default) |

## Reversibility tiers (ROLLBACK, 0.3.0)

Each write verb declares a **reversibility** keyword (+ optional `compensation` block) consumed by
the `ROLLBACK` performative (see [backend-conformance.md §7](../../../docs/backend-conformance.md)).
`REVERSIBLE` = a clean inverse exists; `COMPENSABLE` = an offsetting forward action exists;
`IRREVERSIBLE` = no reversal, refused honestly with `code: "IRREVERSIBLE"`. **IRREVERSIBLE is the
default** for any unmarked verb (zero-touch back-compat). REVERSIBLE/COMPENSABLE verbs MUST name a
`compensation.verb`; IRREVERSIBLE verbs MUST NOT. `manifest validate` enforces this and `manifest
diff` flags a tier change as drift. Worked examples in this profile: `create_product` REVERSIBLE
(inverse `delete_product`); `record_payment` COMPENSABLE (offset by `process_refund`);
`send_message` IRREVERSIBLE (a sent message cannot be unsent).

Notes:
- `destructive` verbs are never matched by scope patterns; a Grant must name them (§14.2).
- Identity facts (`workspace`, `store`) are injected from the Grant on every verb — they do
  not appear in any args schema and Speaker-supplied values refuse the sentence (§5).
- Argument aliases (normative, §6.2): `order_id` ← orderId, order_number, reference_id ·
  `price` ← amount, list_price · `phone` ← mobile, msisdn, to · `status` ← order_status.

## Amount escalation (normative for this profile)
Resolved refund or discount exposure above the Workspace-configured limit ⇒ minimum HIGH.
Bulk verbs (acting on N>1 entities) take the highest tier of any member action and ⇒ minimum
HIGH when N exceeds the Workspace bulk threshold.

## Structured arguments (D-1, 0.2.0)

Args may be typed objects and arrays-of-objects, bounded by the **self-defined** D-1 rule (see
`versions/0.2.0.md`): **non-recursive nesting, maximum two chained structural levels** (an array of
objects may contain at most one array of objects), no third level. DSL reference paths: object →
`$.x.field`; array-of-objects → `$.x[0].field`; nested array-of-objects (the maximum) →
`$.x[0].values[0].field`. `create_product.options` is the deepest *application* of the rule, not
its source; `refund_target` (level-1 object) and `create_product.variants[]` (level-1/2) also apply it.

## Read lexicon (QUERY)

Read verbs carry a **typed response profile** (`schemas/profiles/commerce-v1/<verb>.response.json`)
conforming to `schemas/query-answer.schema.json` (0.2.0). Response profiles for the `get_*` verbs
below are a tracked 0.2.x follow-up; `services.list_clients` is the reference implementation of the
contract (closes ADR-0001).

| Verb | Notes |
|---|---|
| `commerce.get_orders@1.0.0` | filters: status, period, aggregate |
| `commerce.get_order@1.0.0` | order_id resolvable (reference/position) |
| `commerce.get_products@1.0.0` | keyword search; large results return preview + server reference (query.schema.json) |
| `commerce.get_product@1.0.0` | product_id resolvable (name/position/pointer) |
| `commerce.get_customer@1.0.0` | PII-bearing: customer contact fields redacted from telemetry |

Read results feed the System's recent-entity pool, which is an authoritative resolution
source for subsequent PROPOSEs (§6.3 tier "recently-observed entities").

## Resolution sources (this profile)
1. Live platform API lookup (bounded: ≤5 candidates, hard timeout, cached ≤5 min)
2. Recently-observed entities (≤50 per type, 30-min retention; supports exact-id, 1-indexed
   position — "delete the 6th" — and locale-folded name match)
3. Session context (the single most-recently referenced entity per type, for deictic
   references — "the product I just created")
4. Bounded model disambiguation over Candidates only (§6.3 rules 2–3)

Arabic locale folding (§6.3 rule 5): alef-variant unification (أ/إ/آ/ٱ→ا), yaa/taa-marbuta
unification, diacritic strip, per-token definite-article (ال) strip.

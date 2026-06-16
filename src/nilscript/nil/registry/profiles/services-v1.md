# Profile: services-v1
Domain: service businesses (consulting, freelancing, agencies).
**Status: Draft (design target).** The reference-implemented profile is
[commerce-v1](commerce-v1.md); services-v1 reuses its resolution sources, alias rules, and
escalation pattern and flips to Active when its verbs operate in the reference
implementation (GOVERNANCE: no normative release before running code).

| Verb | Base tier | Floor | Flags | Resolvable facts | Modifiable (DECIDE) | Preview keys | Reversibility |
|---|---|---|---|---|---|---|---|
| services.create_client | LOW | — | — | phone normalized E.164 | — | name, phone | **REVERSIBLE** (a freshly created client record can be deleted) |
| services.draft_proposal | LOW | — | — | party_id must exist | amount | title_ar, amount | IRREVERSIBLE (default) |
| services.send_proposal | HIGH | — | explicit_request | **amount from the stored draft** (never args) | channel | party, title, amount, channel | **IRREVERSIBLE** (a sent proposal cannot be unsent) |
| services.create_invoice | HIGH | **HIGH** | explicit_request | amount>0; VAT computed by System | amount | party, amount, vat | **COMPENSABLE** (offset by a credit note / refund) |
| services.create_payment_link | HIGH | **HIGH** | explicit_request | invoice must exist; PSP-licensed link only | — | invoice, amount, provider | IRREVERSIBLE (default) |
| services.send_followup | MEDIUM | — | — | party_id, consent active | message | party, message | **IRREVERSIBLE** (a sent follow-up cannot be unsent) |

Amount escalation (normative for this profile): resolved exposure > 1,000 SAR ⇒ min HIGH;
> 5,000 SAR ⇒ CRITICAL. Args schemas: `schemas/profiles/services-v1/*.json`.

## Reversibility tiers (ROLLBACK, 0.3.0)

Like commerce-v1, each write verb declares a **reversibility** keyword (+ optional `compensation`
block) consumed by the `ROLLBACK` performative (see
[backend-conformance.md §7](../../../docs/backend-conformance.md)). `REVERSIBLE` = clean inverse;
`COMPENSABLE` = offsetting forward action; `IRREVERSIBLE` = refused honestly with
`code: "IRREVERSIBLE"`, and the **default** for any unmarked verb (zero-touch back-compat).
REVERSIBLE/COMPENSABLE verbs MUST name a `compensation.verb`; IRREVERSIBLE verbs MUST NOT — enforced
by `manifest validate`, drift-guarded by `manifest diff`. In this profile `create_client` is
REVERSIBLE, `create_invoice` is COMPENSABLE (offset by a credit note / refund), and the `send_*`
verbs are IRREVERSIBLE — a sent artifact cannot be unsent. Tiers here are **design-target** until the
profile flips to Active in the reference implementation.

## Read lexicon (QUERY)

| Verb | Notes |
|---|---|
| `services.list_clients@1.0.0` | Optional `name` filter (locale-folded). **Typed response profile** `schemas/profiles/services-v1/list_clients.response.json` → `data.clients[]{id,name}`. First consumer of the 0.2.0 typed QUERY response contract (closes ADR-0001). |

Read results feed the System's recent-entity pool (an authoritative resolution source for
subsequent PROPOSEs, §6.3). Every read verb ships a response profile conforming to
`schemas/query-answer.schema.json` so an orchestration layer can type `$.read.output…` references.

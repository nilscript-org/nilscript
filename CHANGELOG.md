# Changelog

## 0.3.0 — 2026-06-17
**Bounded Reversibility — a 7th performative, ROLLBACK, added in place on the 0.1 wire.**
Fully additive and backward-compatible: every existing 0.1 message, DSL program, and profile
remains valid; the envelope `nil` const **stays "0.1"** (no new schema namespace). Any unmarked
verb is IRREVERSIBLE by default (zero-touch). Package version `0.2.0 → 0.3.0`. The upgraded kernel
is named **SEQRD-PC** — a mnemonic re-cut of the same set: S·STATUS, E·EVENT, Q·QUERY, R·ROLLBACK,
D·DECIDE, P·PROPOSE, C·COMMIT. Full test suite green at 180. Ships the reference Playground
(`demo/`, `nilscript demo`) — the chat UI that drives a live shim through the SDK.
### Added — discovery, generic CRUD & skeleton awareness
- **Discovery:** `GET /nil/v0.1/describe` — adapters self-describe their *skeleton* `{nil, system, verbs, targets:{name:{exists, fields[]}}}`. SDK gains `handshake(transport)` (reachable → conformant → provisioned). Conformance gains a **mandatory** `exposes_describe_skeleton` row (matrix now 11).
- **PROPOSE preflight:** a verb whose native target isn't provisioned is refused at PROPOSE (`UPSTREAM_UNAVAILABLE`) instead of a COMMIT-time `failed_terminal`.
- **Generic `resource.*` family** (`resource-v1` profiles: create/read/update/delete) — universal CRUD over any provisioned target, no per-entity authoring. `read` = QUERY; writes = PROPOSE→COMMIT with **synthesized reversibility** (create→delete, update→restore before-image, delete→recreate) via `ROLLBACK`.
- **Identifier resolution:** `update`/`delete` (generic and semantic) accept a real id or a human identifier (code/name/…) resolved server-side.
- **`STATUS.result`:** COMMIT returns the SSOT result envelope (`entity`, `ssot`, `compensation`).
- **Adapter I/O interface:** `exists(target)`, `schema(target)`, `get(target,id)`; `scaffold-shim` emits all of it (skeleton-aware by default) + a describe conformance test.
- **Fix:** unknown verb now refuses with the valid `UNKNOWN_VERB` (was the invalid `UNSUPPORTED`).
### Added
- **Wire:** `ROLLBACK` added to the envelope `performative` enum; new `rollback.schema.json`.
  EVENT `result` gains a `compensation` object `{reversibility, token?, expires_at?}`; new EVENT
  kinds (compensating / compensated / compensation_refused); new refusal codes `IRREVERSIBLE` and
  `COMPENSATION_EXPIRED`. New endpoint `POST /nil/v0.1/rollback` — NIL now exposes **six** endpoints;
  `export-openapi` emits all six.
- **SDK:** `Performative.ROLLBACK`; `Reversibility` + `RollbackReason` enums; `RollbackBody`;
  `Compensation` on the result envelope.
- **Profiles:** a `reversibility` tier per verb (REVERSIBLE / COMPENSABLE / IRREVERSIBLE) plus an
  optional `compensation` block. Examples: `commerce.create_product`=REVERSIBLE,
  `commerce.record_payment`=COMPENSABLE, `commerce.send_message`=IRREVERSIBLE.
- **CLI toolkit:** `scan` infers tiers (`propose_reversibility`); `scaffold` emits a
  `compensation.py` stub; `manifest` validates/merges/diffs reversibility (diff flags tier drift =
  CI drift guard); `conformance-test` adds rollback-honesty rows; `repair` gains `run_saga_unwind`
  (reverse-order governed compensation); memory gains `record_reversal` + `anchor_ratification`
  (+ `compute_spec_hash`).
- **DSL:** `action` nodes gain `compensate_with` — the language is now a Saga (`on_error: compensate`
  already existed).
### Changed
- The fifth axiom (self-healing grammar) now heals forward **and** backward ("Bounded
  Reversibility") — still five axioms.

## 0.2.0-draft — 2026-06-15
**Structural alignment release** — realigns the commerce/services lexicon with NIL's own
"intent, not implementation" philosophy, grounded in an 18-platform / 90-row calibration against
official vendor docs (`versions/0.2.0.md`, `versions/0.2.0-calibration-appendix.md`).
- **commerce-v1:** `update_order_status` **DEPRECATED** (removed 0.3.0) → new
  `record_fulfillment@1.0.0` (MEDIUM) + `record_payment@1.0.0` (HIGH, floor HIGH): record a *fact*,
  the System derives status (GAP-001). `process_refund@2.0.0` (breaking, deprecation overlap):
  `order_id` → abstract `refund_target {order|invoice|payment, id}` (GAP-002).
  `create_product@1.2.0` (additive): optional `variants[]` decomposition via `oneOf` with the flat
  single-variant shape (GAP-003).
- **Typed QUERY response contract** (keystone): new `schemas/query-answer.schema.json` +
  per-read-verb `*.response.json` profiles; closes the 0.1 read-half gap so an orchestration layer
  (nilscript DSL) can type `$.read.output…` references. First consumer `services.list_clients@1.0.0`
  (args + response) — closes ADR-0001.
- **D-1:** structured arguments (typed objects / arrays-of-objects) admitted as a self-defined
  pattern — non-recursive, max two chained levels; `create_product.options` is its deepest
  application, not its source. Normative arg-shape → DSL reference-path table added.
- **Scope:** structural only. Derivation behavior and `scheduling.*` (GAP-004) explicitly deferred.
  §15 security analysis: no new authority; floors and Refusal-not-error preserved.
- **Schema namespace:** `$id`s keep the `…/0.1/…` segment (release tracked via versions + verb semver).

## 0.1.0-draft revision 4 — 2026-06-11
**Vendor neutralization — NIL is now fully implementation-independent.**
- All references to any particular codebase removed from normative and supporting text; the
  reference implementation demonstrates the spec but never defines it.
- Annex B converted from an implementation-evidence ledger to a **conformance checklist**
  (63 testable assertions, Core + NIL-H, with verification methods) plus a W3C-style
  **implementation-report** process; 1.0 now requires two independent interoperable
  implementations.
- Name origin recast: *niẓām* (نظام, Arabic: "order; system") — the language of ordered
  intent — rather than any product.
- Worldwide-standards alignment made explicit: W3C Trace Context for `trace`; BCP 47 preview
  locale keys; ISO 4217 currency; RFC 6750 bearer auth, RFC 9457 Problem Details and
  RFC 6585 (429 + Retry-After) in the HTTP binding — with the rule that Refusals are protocol
  outcomes (200), never transport errors; Standard Webhooks conventions for NIL-H H4 push
  delivery; GDPR/PDPL named as the H5 design envelope.
- commerce-v1 marked platform-independent; governance/security contacts moved to the
  standard's own namespace.

## 0.1.0-draft revision 3 — 2026-06-11
**NIL-H — Hosted System profile** (`versions/0.1.0-hosted-profile.md`): the SaaS-grade
conformance class for multi-tenant operator-run Systems, layered on Core. Eight clauses:
H1 tenancy & isolation (Workspace as isolation unit, cross-tenant unobservability,
tenant-scoped idempotency, fairness); H2 credential & Grant lifecycle (prefixed keys,
two-key rotation, ≤60s revocation, vaulted secrets); H3 layered rate limits, fail-closed,
refuse-whole-before-side-effects; H4 signed at-least-once EVENT push with DLQ + drain and
per-tenant delivery isolation; H5 data protection (declared retention, mandatory audit
export, erasure-by-tombstone reconciling PDPL with append-only, telemetry redaction,
residency statement); H6 entitlements & discovery (single canonical plan resolver, reserved
`nil.capabilities` verb, unknown-plan = most restrictive); H7 tenant lifecycle
(provision/suspend/offboard cascade); H8 operational transparency (dated deprecation,
health, SLOs, incident disclosure).
- Core §8 gains conformance classes (Core System vs NIL-H); §14.2 references
  `nil.capabilities`; GOVERNANCE adds the tenant-isolation invariant.
- Annex B gains 25 H-clause entries covering credential lifecycle, per-Grant limits +
  fairness, outbound signing, export + erasure, capabilities discovery, offboarding cascade,
  and published retention/residency/SLOs (superseded by the rev-4 conformance checklist).

## 0.1.0-draft revision 2 — 2026-06-11
Deep-extraction pass: every normative clause grounded in (or honestly gapped against) the
operating reference implementation.
- **Spec:** refusal taxonomy (Annex A, 15 codes); three-outcome resolution semantics with
  bounded disambiguation + fabrication defense + locale folding (§6.3); ordered policy
  pipeline, tighten-only floors, `explicit_request` flag, origin escalation (§6.4);
  before→after Preview diffs (§6.5); scope-qualified idempotency with replay marker (§7.1);
  three budget classes, fail-closed (§7.2); Decision Windows with per-verb SLAs and
  timeout dispositions (§7.3); **DECIDE approve-with-modification** (§7.4); normative audit
  record fields + telemetry redaction (§10); **Result envelope** — System-computed claim
  classes, read-after-write verification, `data_gaps` (§11.1); EVENT taxonomy of 16 state
  changes (§11.3); **Suspension & human override** (§12); hardened MCP binding rules —
  workspace from credential only, no upstream-token transit (§13); Grant descriptor with
  audience classes + budgets (§14.1); Profile entries gain verb semver, aliases, destructive
  class, modifiable facts, error contract, redaction, deprecation (§14.2).
- **Schemas:** added `proposal`, `status`, `query`, `event` (with Result `$defs`), `grant`;
  DECIDE gains `modification`/`reason`; per-verb args schemas for both profiles under
  `schemas/profiles/`.
- **Registry:** added **commerce-v1** (Active — the reference-implemented profile);
  services-v1 re-marked Draft (design target) and extended with flags + modifiable facts.
- **Examples:** added 04 (ambiguity → candidates), 05 (decide-with-modification),
  06 (suspension).
- **Process:** added the Annex B conformance ledger with an 8-item gap register
  (CRITICAL cooling delay, pre-side-effect monetary budgets, unified append-only audit,
  Grant object, origin escalation, MCP binding deployment, auto-approve constraint, hamza
  folding); added SECURITY.md and MAINTAINERS.

## 0.1.0-draft — 2026-06-11
Initial public draft. Performative set (PROPOSE/PROPOSAL/COMMIT/STATUS/QUERY/EVENT/DECIDE),
envelope, Six Guarantees as conformance, Approval Surface class, MCP + HTTP bindings,
services-v1 profile, schemas, examples. Extracted from the wosool reference implementation.

# NIL Paper — Detailed Additions (Closing the Paper↔Code Gap)

> **Purpose.** The current paper describes NIL's *core architectural guarantee* (the wire
> contract, the four structural guarantees, and the InjecAgent A/B result) with precision, but
> the implementation ships substantially more than the paper documents. Everything the paper
> *claims* is backed by code; the gap is one-directional — the paper is **narrower** than the
> system. The sections below are written in the paper's voice and are ready to splice in. Each
> is grounded in the actual implementation, with file paths given in a "provenance" note so a
> reviewer can audit the claim. Suggested insertion points are marked **[INSERT AFTER §x]**.

---

## A. Expanded §4 — The Orchestration Layer: A Statically-Validated Plan Language

**[INSERT AFTER §4 first paragraph, replacing the single sentence "Above it, an optional declarative orchestration language…"]**

NIL is two layers, and the paper so far has described only the lower one. The lower layer is the
**wire contract** — a stable envelope, the seven `seqrd-pc` performatives, grants, structured
refusals, and per-domain profiles. The upper layer is an **optional declarative orchestration
language** (working name *Wosool DSL*, version `0.1`): a closed, JSON-serializable program model
that an agent emits as data and that a deterministic **kernel** statically validates and then
walks, driving NIL verbs underneath. The two layers compose but are independent: an agent may
speak raw NIL performatives, or it may emit a whole plan for the kernel to execute. The plan
language matters to the security thesis because it moves *multi-step* intent — not just a single
write — into the unexpressible-not-filtered regime: a plan that names an undeclared verb, forms a
cycle, or references an output that no prior step produces is **rejected before any effect
occurs**, by construction rather than by inspection.

### A.1 A closed node set

A program is an ordered pipeline of at most 256 nodes drawn from a **closed set of eight node
types**, expressed as a discriminated union on a `type` tag so that an unknown node type is
structurally unrepresentable:

| Node | Role | Side effect |
|------|------|-------------|
| `action` | Execute a write verb (`propose → commit`), store its result | yes (governed) |
| `query` | Read-only retrieval (`query`) | none |
| `condition` | Branch on a boolean expression over prior outputs | none |
| `parallel` | Fan out 2–64 branches; join on `all` or `any` | inherits |
| `foreach` | Bounded loop over a collection (`max_items ≤ 1000`) | inherits |
| `await_approval` | **Suspend until a human decides** (see §B) | none |
| `wait` | Bounded delay | none |
| `notify` | Emit a bilingual (ar/en) message | none |

Every node carries a `step_N` identifier matched against a fixed pattern; every verb reference is
matched against `^[a-z]+\.[a-z_]+$`; and every model is **frozen and rejects unknown members**
(`extra="forbid"`). Parsing the program *is* the first structural pass: a malformed plan cannot be
instantiated at all.

### A.2 Six static validation passes (V1–V6)

Before the kernel executes a single node, the plan passes a fixed validation pipeline. The
guarantee is that **admission is decided entirely from the plan text and the backend's discovery
skeleton — never from a trial run that might commit something.**

1. **V1 Structural.** Discriminated-union schema check: closed objects, enum tags, id/verb
   patterns, per-node required fields, unique node ids.
2. **V2 References.** Every `$.step_k.field` (and `$.input.*`) reference must resolve to an output a
   *prior* node actually produces. Forward or dangling references are rejected.
3. **V3 Acyclicity.** The `next` / branch / `body` edges must form a DAG. A plan that loops back
   into itself (outside a bounded `foreach`) is refused.
4. **V4 Whitelist.** Every verb named by an `action`/`query` node must appear in the backend's
   discovery skeleton. This is the multi-step generalization of the paper's **Skeleton-bounded**
   guarantee: a hallucinated verb has nothing to bind to *at plan-admission time*.
5. **V5 Argument typing.** Node arguments are checked against the verb's published arg schema
   (the same schema the CLI emits via `nilscript profile <verb>`).
6. **V6 Reachability.** The entry node and every declared error target must be reachable; dead or
   orphaned error handlers are rejected so failure paths cannot silently vanish.

### A.3 Honest multi-step reversibility: saga unwinding

When a program declares `on_error: compensate`, a node may carry a `compensate_with` clause naming
the inverse verb and (reference-bearing) arguments that undo it. On failure mid-pipeline, the
kernel **walks back through the completed steps in reverse**, executing each compensation through
the same `propose → commit` / `rollback` machinery — a direct realization of the Saga pattern that
§2 cites. Crucially, **absence of a `compensate_with` clause means the step is `IRREVERSIBLE`**, and
the unwind halts honestly at that boundary rather than fabricating a corrective write. This extends
the paper's third structural guarantee ("honest, bounded reversibility") from a single effect to a
whole transaction.

### A.4 Execution trace

The kernel returns a structured trace — `{completed, partial, blocked_at, refusal, compensated,
notifications, context}` — so that a *partial* execution (e.g. blocked at an `await_approval`, or
unwound after a refusal) is a first-class, inspectable outcome, never an exception that loses state.

> **Provenance.** AST and the closed node set: `src/nilscript/kernel/models.py`. Validation passes:
> `src/nilscript/kernel/validator.py`. Reference resolution: `src/nilscript/kernel/references.py`.
> Walker, saga unwind, trace: `src/nilscript/kernel/executor.py`. Scope/grant enforcement:
> `src/nilscript/kernel/guards.py`. Structured diagnostics (HINT…CRITICAL with location):
> `src/nilscript/kernel/diagnostics.py`. Tests: `tests/test_kernel_validator.py`,
> `tests/test_kernel_executor.py`, `tests/test_dsl_compensation.py`.

---

## B. New §4.x — The Governance Control Plane and the Human-Approval Gate

**[INSERT AS A NEW SUBSECTION after §4.2; this is the single largest paper↔code gap.]**

The paper's threat model assumes "the human approver" cannot be compromised, but the paper does not
describe the mechanism by which a human enters the loop. The implementation makes this concrete: a
**governance control plane** turns the `propose → approve → commit` lifecycle into an operable
product surface, so that the "approve" step can be *a person*, not the agent itself.

### B.1 Gate modes

The gate that sits between `propose` and `commit` runs in one of three modes, declared per
deployment:

- **`two-step`** (default): the agent self-approves by issuing its own `commit`. This is the mode
  evaluated in the InjecAgent A/B — it already drives UWR→0 because a hijacked agent still cannot
  name an undeclared write.
- **`human`**: proposals whose declared severity tier is **HIGH or CRITICAL are held** — `commit`
  returns `approval_required` instead of executing — until an owner decides. LOW/MEDIUM proposals
  flow through.
- **`auto`**: all proposals auto-commit (frictionless **audit-only** mode; never used to report a
  NIL safety figure, by the anti-tautology discipline of §6.2).

### B.2 An append-only, deduplicated audit store

Every NIL `event` — `proposed`, `executed`, `refused`, `rolled_back` — emitted by any adapter,
agent, or MCP tenant is POSTed to a central, **HMAC-verified** ingest endpoint and written to an
append-only store. The result is **one timeline across every agent and tenant**: a single pane in
which a reviewer sees what was proposed, what committed, what was refused (and with which refusal
code), and what was rolled back. Two design points are load-bearing:

- **Ingest is authenticated.** `/events/ingest` verifies an `X-NIL-Signature` HMAC-SHA256 over the
  raw body against a shared secret; an unsigned or mis-signed envelope is rejected `401`. The audit
  log cannot be poisoned by an unauthenticated writer.
- **Deduplication is by globally-unique envelope id, not `(workspace, sequence)`.** An adapter's
  sequence counter lives in memory and resets on restart, so a `(workspace, sequence)` key collides
  across restarts and would silently drop fresh events. Keying on the envelope's stable `id` makes
  ingestion correctly idempotent under at-least-once delivery. *(This is the fix recorded in the
  implementation's commit history and is exactly the kind of subtle correctness property the
  conformance discipline of §7 is meant to protect.)*

### B.3 The decision protocol

The human gate is a small, explicit state machine over a `pending → {approved | rejected}` row:

| Endpoint | Caller | Effect |
|----------|--------|--------|
| `POST /proposals/{id}/await` | the gate, when it holds a HIGH/CRITICAL proposal | register the proposal as `pending`, enriched with verb/tier/preview pulled from its `proposed` event |
| `GET /proposals/{id}/decision` | the gate, before committing a held proposal | poll: `pending` / `approved` / `rejected` / `unknown` |
| `POST /proposals/{id}/decision` | the **owner**, from the UI | transition `pending → approved\|rejected` with actor + reason |
| `GET /api/pending` | the UI | list everything currently awaiting a human |

The `decide` transition only fires on a `pending` row and is idempotent (a second click is a
no-op), so a double-approval cannot double-commit. The approval card is **enriched server-side**
from the proposal's own `proposed` event, so the owner sees the real verb, the real tier, and the
real human-readable preview — not an agent-supplied summary that an injection could have shaped.

### B.4 Why this strengthens the thesis

The paper argues that NIL collapses the security perimeter to one intent-to-effect boundary. The
control plane is the **operational instantiation of that boundary**: it is the one place a person
can stand. Because the gate holds *at commit*, outside the reasoning loop, a poisoned agent that
proposes a CRITICAL write produces exactly one artifact — a `pending` row a human can reject — and
no state change. This is the difference between "the model decided not to" (probabilistic) and "the
write physically could not commit without an out-of-loop human approval" (structural).

> **Provenance.** Endpoints + single-pane UI: `src/nilscript/controlplane/app.py`. Append-only
> store, HMAC ingest, dedup-by-id, approval state machine: `src/nilscript/controlplane/store.py`.
> Gate modes in the MCP front door: `src/nilscript/mcp/server.py` (`GATE_MODES`). Tests:
> `tests/test_controlplane.py`, `tests/test_mcp_gate.py`. Governance design:
> `docs/plans/governance-control-plane.md`.
>
> **Honest status (for §8).** Phase 1 (visibility) and the approval state machine are shipped.
> The MCP server currently stores *pending proposals* in per-connection memory; persisting them in
> the central store so approvals survive a restart and one owner can clear many agents is Phase 2
> and is not yet wired. The paper should claim the **architecture and the single-pane audit**, and
> list durable cross-restart approval as in-progress.

---

## C. New §4.x — Multi-Tenant Deployment: One Front Door, Tenant-Owned Backends

**[INSERT AS A NEW SUBSECTION; the paper says "composes with MCP" but does not describe how a single hosted gate serves many backends without holding their credentials.]**

NIL composes with the Model Context Protocol as the governed action layer MCP does not define. The
implementation ships a **single hosted MCP front door** (`mcp.nilscript.org`) that is multi-tenant
by a deliberately credential-free design:

- **The agent binds its own backend per connection.** A connecting agent sends `X-NIL-Adapter-Url`
  (which must be `https://`), an `X-NIL-Adapter-Bearer`, and optional `X-NIL-Grant-Id`,
  `X-NIL-Workspace`, and `X-NIL-Scopes` headers. The gate relays NIL performatives to *that*
  adapter; **it never stores, and never holds, the tenant's backend credentials** — the adapter the
  tenant runs holds the real secrets.
- **Tenant isolation is structural.** Each connection resolves to a frozen `Tenant` identity keyed
  by `adapter_url | grant_id | workspace`; in-flight proposals are namespaced per connection so two
  tenants cannot see or commit each other's proposals.
- **The skeleton menu is per-tenant.** The front door exposes both the generic six tools
  (`propose`, `commit`, `query`, `status`, `rollback`, `describe`) and, dynamically, one
  `propose_<verb>` tool per verb the *bound* backend actually declares — so the agent's tool list is
  exactly the backend's real, skeleton-bounded surface, computed from discovery rather than guessed.

This is what lets the safety property generalize beyond a benchmark harness: any NIL-speaking
backend can be governed by the same hosted gate without re-solving safety per integration, and
without the gate operator becoming a custodian of every tenant's credentials.

> **Provenance.** Per-connection tenant resolution (headers, https enforcement, key):
> `src/nilscript/mcp/tenant.py`. Generic + dynamic per-verb tools: `src/nilscript/mcp/tools.py`,
> `src/nilscript/mcp/dynamic.py`. Server wiring and front-door auth: `src/nilscript/mcp/server.py`,
> `src/nilscript/mcp/app.py`. Tests: `tests/test_mcp_multitenant.py`, `tests/test_mcp_tenant.py`,
> `tests/test_mcp_http_e2e.py`.

---

## D. New §4.x — Wire-Level Robustness: Refusals, Idempotency, Circuit-Breaking, Event Dedup

**[INSERT AS A NEW SUBSECTION; these are the properties that make "deterministic lifecycle" hold under retries and partial failure — the paper's axis 3, but undescribed at the mechanism level.]**

The paper's conformance axis asserts the wire is "correct under retries and partial failure." Four
concrete mechanisms underpin that claim.

### D.1 Refusals are typed outcomes, not exceptions

Annex A defines a **closed enumeration of refusal codes** — `MALFORMED`,
`UNKNOWN_PERFORMATIVE`, `UNKNOWN_VERB`, `SCOPE_DENIED`, `CAPABILITY_DENIED`, `POLICY_DENIED`,
`INVALID_ARGS`, `UNRESOLVED`, `AMBIGUOUS`, `BUDGET_EXHAUSTED`, `QUOTA_EXHAUSTED`, `SUSPENDED`,
`EXPIRED`, `RATE_LIMITED`, `UPSTREAM_UNAVAILABLE`, and the two backward-recovery codes `IRREVERSIBLE`
and `COMPENSATION_EXPIRED`. A refusal is a **structured answer the agent must read**, never a stack
trace and never a silent failure. Only `RATE_LIMITED` and `UPSTREAM_UNAVAILABLE` are marked
**retriable**; the rest are terminal answers, so an agent that retries a `SCOPE_DENIED` or an
`IRREVERSIBLE` blindly is making a protocol error, not encountering transient noise. The two
rollback-specific codes encode the paper's reversibility honesty directly: the system refuses to
*pretend* it can reverse an effect it cannot, rather than emitting a corrective write.

### D.2 Idempotency is deterministic, not best-effort

The commit token is `NIL_UUID = sha256(session_id : request_timestamp : command_index)`, minted
**exactly once** when a skill emits a batch and reused verbatim by every retry. For a commit, the
globally-unique, stable `proposal_id` fills the timestamp slot, so `commit_idempotency_key(session,
proposal)` is fully deterministic: **both** the chat-confirm path and the MCP tool mint the key the
same way, so a duplicate confirm — from a user double-click, a network retry, or a workflow
re-drive — replays the identical `commit` sentence and **cannot double-commit**. This is the
property the paper's "idempotency" invariant (axis 3) tests by construction.

### D.3 Circuit-breaking bounds blast radius

The client wraps a backend in a `closed → open → half-open` circuit breaker (default: five
consecutive failures trip it open; a 30-second window before a half-open trial). A flapping or
degraded backend therefore yields fast, typed `UPSTREAM_UNAVAILABLE` refusals rather than a pile-up
of in-flight writes of uncertain status — which keeps the "what actually committed?" question
answerable under partial failure.

### D.4 Event delivery is dedup-correct end to end

Events are deduplicated twice: at the SDK boundary by an LRU `EventDeduper` keyed on `(workspace,
sequence)` for at-least-once stream consumers, and at the audit store by globally-unique envelope id
(§B.2). The two keys are chosen for their respective lifetimes — a live stream cursor versus a
durable log that must survive adapter restarts.

> **Provenance.** `src/nilscript/sdk/refusals.py`, `src/nilscript/sdk/idempotency.py`,
> `src/nilscript/sdk/breaker.py`, `src/nilscript/sdk/events.py`. Tests: `tests/test_idempotency.py`,
> `tests/test_breaker.py`, `tests/test_events.py`, `tests/test_event_proposal_validation.py`.

---

## E. New §4.x — The Adapter Toolkit and Earned-Tier Conformance

**[INSERT AS A NEW SUBSECTION; expands "build-once adapters" (§4.2) into the tooling that makes "tiers are earned, not asserted" (guarantee 4) operational.]**

§4.2 states that a backend speaks NIL through a three-file adapter (I/O, verb-to-native translation,
compensation). The implementation ships the **developer toolchain** that makes this a repeatable,
verified process rather than a bespoke effort:

- **`nilscript scaffold-shim --name X --lang python`** generates a bootable adapter skeleton with
  exactly the three files to fill; the HTTP edge that enforces the NIL protocol
  (`/nil/v0.1/{propose,commit,query,status,rollback}`) is *generated*, not hand-written, so every
  adapter inherits the same lifecycle.
- **`nilscript verbs` / `nilscript profile <verb>`** expose the standard's verb catalog and each
  verb's argument schema — the same schema the kernel uses for V5 argument typing and a UI uses to
  render a form.
- **`nilscript export-openapi`** emits an OpenAPI 3.1 document for the six NIL endpoints — the bridge
  the paper's *planned* BFCL extension is designed to consume.
- **`nilscript conformance-test --url … --verb … --reversibility …`** runs a fixed conformance
  matrix against a live adapter, exercising the rollback-honesty and refusal-correctness properties
  per verb tier.
- **`nilscript manifest …`** works with a **requirements-manifest** — *data, not code* — that
  carries a backend's hidden field requirements, instance defaults, and transport quirks (e.g. "do
  not send `Expect: 100-continue`"), so backend reality lives in a manifest the CLI overlays rather
  than in the generic kernel.

The fourth structural guarantee — **tiers are earned, not asserted** — is the manifest **diff used
as a CI drift-guard**: if an adapter declares a reversibility tier (`REVERSIBLE`, `COMPENSABLE`,
`IRREVERSIBLE`) that its conformance run does not actually honor, the diff is non-empty and CI
fails. Reversibility is *synthesized* for the generic `resource.*` CRUD family
(`create→delete`, `update→restore-before-image`, `delete→recreate`), so a backend that exposes
provisioned targets gets honest, tested reversal with no per-entity verb authoring.

> **Provenance.** `src/nilscript/cli/` (`scaffold/`, `scan/`, `conformance/`, `manifest/`,
> `_openapi.py`, `repair.py`). Demo three-file adapter:
> `src/nilscript/demo/pocketbase_nil_adapter/{system,translate,compensation}.py`. Tests:
> `tests/test_scaffold.py`, `tests/test_conformance_runner.py`, `tests/test_manifest.py`,
> `tests/test_cli_toolkit.py`. Strategy: `docs/adapter-ecosystem-strategy.md`.
>
> **Honest status (for §8).** `scaffold-shim`, `verbs`, `profile`, `export-openapi`,
> `conformance-test`, and `run` are shipped. `scan` (error→requirement inference) is replay-only;
> live probing is not yet wired. The drift-guard is implemented as a manifest diff and documented;
> wiring it as a blocking CI job in every adapter repo is in progress.

---

## F. Additions to §8 (Threats to Validity)

**[APPEND these items to the existing §8 list so the honesty account covers the newly-documented surface.]**

- **(8) Gate-mode disclosure.** Three gate modes exist (`two-step`, `human`, `auto`). The InjecAgent
  result is reported under `two-step` with an intent oracle; `auto` is audit-only and is never used
  to report a NIL safety number. Every figure states its gate mode, per §6.2.
- **(9) Control-plane durability.** The human-approval gate's pending state is, in the current MCP
  server, per-connection and in-memory; cross-restart durability and one-owner-clears-many is Phase
  2. The audit *ingest and store* are durable (SQLite, append-only, HMAC-verified); the *held
  proposal* is not yet.
- **(10) Trusted ingest.** The single-pane audit is only as trustworthy as the HMAC secret on
  `/events/ingest`; a leaked secret would let an attacker forge audit rows. Secret rotation is an
  operational requirement, not a protocol guarantee.
- **(11) Orchestration-layer scope.** The plan language is validated statically against the
  discovery skeleton; it does not make the *plan's intent* correct. A well-formed plan of
  well-declared verbs that a human approves can still be a business mistake — the guarantee is on
  *unauthorized/undeclared* actions, consistent with §8(7).

---

## G. Claim → Code → Test Provenance Map (for an appendix or artifact-evaluation reviewer)

| Paper claim | Implementation | Test |
|---|---|---|
| Seven `seqrd-pc` performatives | `src/nilscript/nil/schemas/0.1/*.schema.json`; `src/nilscript/sdk/sentences.py` | `tests/test_contract.py`, `tests/test_sentences.py` |
| G1 No side effect on `propose` | `mcp/tools.py`, kernel executor | `tests/test_mcp_tools.py`, `bench/conformance/test_invariants.py` |
| G2 Skeleton-bounded | `describe()` + V4 whitelist (`kernel/validator.py`) | `tests/test_kernel_validator.py`, `tests/test_mcp_dynamic.py` |
| G3 Honest bounded reversibility | `sdk/refusals.py` (`IRREVERSIBLE`, `COMPENSATION_EXPIRED`); compensation adapter file | `tests/test_rollback.py`, `tests/test_dsl_compensation.py` |
| G4 Tiers earned (drift-guard) | `cli/manifest/`, `cli/conformance/` | `tests/test_conformance_runner.py`, `tests/test_manifest.py` |
| InjecAgent A/B (4,216 cases; UWR_NIL=0; benign=100%) | `bench/safety/injecagent_runner.py`, `bench/safety/matrix.json` | `bench/safety/last_result.json` |
| Anti-tautology / intent oracle | `bench/core/gate.py` (`oracle` vs `auto`) | `bench/README.md` discipline note |
| pass^k conformance | `bench/conformance/test_invariants.py`, `bench/core/report.py` | property-based, `max_examples`/`stateful_step_count` |
| Plan language + static validation | `kernel/{models,validator,executor,references,guards}.py` | `tests/test_kernel_*.py` |
| Human-approval gate + audit | `controlplane/{app,store}.py`, `mcp/server.py` gate modes | `tests/test_controlplane.py`, `tests/test_mcp_gate.py` |
| Multi-tenant MCP front door | `mcp/{tenant,tools,dynamic,app,server}.py` | `tests/test_mcp_multitenant.py`, `tests/test_mcp_tenant.py` |
| Idempotency / circuit-breaker | `sdk/{idempotency,breaker,events}.py` | `tests/test_idempotency.py`, `tests/test_breaker.py` |

---

### Note on scope (what *not* to add)

The paper's restraint is a feature. The InjecAgent axis is the one realized empirical result;
AgentDojo (629 cases), ToolEmu, the τ-bench bridge, BFCL via OpenAPI export, and
coordinated-omission-correct latency are genuinely **not yet built** and are correctly listed as
planned. The additions above are all for capabilities that **exist in the code today** and are
merely under-documented in the manuscript — adding them raises the paper's faithfulness without
weakening its honesty discipline.

---

## Empirical axis: structural unexpressibility (HVR / SRR / EL) — added 2026-06-23

Closes the gap that the InjecAgent A/B (§8) measures **scoped-approval** (a∈T), not the paper's
central **unexpressibility** claim (β⁻¹(a)=∅ for a∉declared). This axis measures the contribution
directly, through the **production edge** (not the standalone `bench/core/gate.py`).

### Definitions (report all three — different quantities)
- **SRR** (Structural Rejection Rate): of proposals naming an undeclared verb/target, the fraction
  refused at PROPOSE with a structured refusal **before any effect**. The defense metric.
- **EL** (Effect Leakage): undeclared-action proposals that produced **any** backend write, by
  read-after on the live adapter (not inferred from the decision). The falsifier; expected 0.
- **HVR** (Hallucinated-Verb Rate): fraction of *agent* proposals naming an undeclared verb — the
  attack-surface metric (model-dependent; the live-model run is the pluggable extension).

### Result (odoo-crm adapter, real edge, `conformance/test_unexpressibility.py`)
| corpus | N | SRR | EL |
|---|---|---|---|
| synthetic (undeclared verbs) | 50 | 100% | 0 |
| plausible-attacker (undeclared verbs) | 8 | 100% | 0 |
| injecagent-derived (undeclared verbs) | 8 | 100% | 0 |
| **resource.* × undeclared targets** | 8 | **100%** | **0** |

The 4th corpus is the load-bearing one. The generic `resource.*` CRUD family previously accepted
**any provisioned** Odoo model (gate: `client.exists`), while `describe()` advertised only the
curated `crm.*` targets — so committable ⊋ advertised: an agent could express a write to
`account.payment`, `hr.employee`, etc. A literal **Guarantee-2 violation** and a direct
counterexample to β⁻¹(a)=∅. **Before** the fix the 4th corpus was SRR 0% / EL > 0 (proven by a
failing test); the fix bounds `resource.*` to a `DECLARED_TARGETS` allowlist and makes `describe()`
advertise exactly it, so **advertised ≡ committable** by construction.

### Honest framing (do NOT overclaim)
- SRR=100% is **by construction**; the experiment is **implementation-faithfulness evidence** — the
  shipped edge does not regress from Prop 1/2 — **not** an empirical surprise.
- This exercises the **single-adapter edge + skeleton bound**, not a multi-system planner. Scope the
  claim to one adapter's declared boundary.
- The guarantee holds **iff** `describe()` is the sole source of declared verbs/targets **and NIL is
  the sole effect path (Assumption A1)**. A side channel that commits outside the edge makes SRR
  irrelevant — state this in threats-to-validity.
- `DECLARED_TARGETS` is an **operator-bounded** allowlist: the structural guarantee is only as tight
  as the declared set. Default-deny is the right posture (a CRM adapter must not reach payroll
  unless the operator declares it), and widening the set is a deliberate, re-advertised act.

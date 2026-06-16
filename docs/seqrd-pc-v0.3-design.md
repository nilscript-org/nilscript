# SEQRD-PC v1 — Protocol Upgrade Design (NIL wire 0.1 in place, package 0.2.0 → 0.3.0)

> **Implementation note (2026-06-16, as shipped):** SEQRD-PC is **NILScript upgraded in place — not a separate protocol and not a forked schema namespace.** The `ROLLBACK` capability was added **additively onto the existing `0.1` wire dialect** (the `nil/schemas/0.1/` lineage the repo already treats as the stable, release-decoupled namespace), *not* a new `0.2/` directory. The wire `nil` const stays `"0.1"`; `ROLLBACK` is an additive performative in that dialect; only the **package** bumps `0.2.0 → 0.3.0`. Where Part 4 / Appendix A–C below were originally drafted around a new `0.2/` namespace, the **in-place 0.1 reality is authoritative** — those passages are annotated inline.

> **Status:** Design / not ratified · **Branch:** `feat/adapter-toolkit-mvp` · **Date:** 2026-06-16
> **Scope:** Layer-1 protocol theory + spec deltas + Layer-2 DSL + 6-phase toolkit adaptation + ratification ("lock") + narrative reframe + migration.
> **Decision record:** Approach **A** (hybrid — additive on the wire, bold in narrative). Saga model with **reversibility tiers**. **Governed compensation, tier-scaled authority.** Bounded Reversibility folded into the **existing fifth axiom** (self-healing grammar), *not* a sixth axiom.
> **Companion docs:** [`adapter-toolkit-plan.md`](./adapter-toolkit-plan.md) · [`saas-grade-content-plan.md`](./saas-grade-content-plan.md) · [`HANDOFF.md`](./HANDOFF.md)

This is a **plan**, not an implementation. It specifies *what changes, where, and why* so that a subsequent `writing-plans` pass can sequence the work. Nothing here touches `main`; every delta is additive and lands on the feature branch.

---

## Part 0 — Framing: why 6 → 7 is *closure*, not *sprawl*

NIL's central safety claim is a **closed performative set** that "can never sprawl." Adding a seventh performative therefore demands a principled defense, not an apology.

**Thesis.** The original six performatives describe a transaction that can only ever move *forward*: an agent can `PROPOSE`, preview a `PROPOSAL`, `COMMIT`, `QUERY` truth, poll `STATUS`, and receive an `EVENT`. There is no governed primitive for *moving an effect backward when a later step fails*. Today, a half-completed multi-step program leaves the agent stranded at the wall: it has committed step 1, step 3 failed, and the protocol offers no sanctioned way to undo step 1. The only "recoveries" available are exactly the things NIL forbids — a silent corrective write, an invented compensating entity, or a hallucinated "it's fine."

`ROLLBACK` closes that gap. It is the **lifecycle-closing primitive**, the backward-recovery counterpart to `PROPOSE`. With it, the performative set spans the *complete* lifecycle of a governed effect:

```
        forward                         backward
  PROPOSE → PROPOSAL → COMMIT → EVENT ─────────────► ROLLBACK
     │          │         │       │                     │
   intent    preview   execute  fact            governed reversal
                          ▲                            │
                       QUERY/STATUS (observe)          │
                       DECIDE (owner authorizes) ◄──────┘
```

**The set stays closed.** Two structural guarantees prevent this from becoming a precedent for endless growth:

1. **`ROLLBACK` is defined as the *terminal* lifecycle primitive.** Forward (propose), observe (query/status), authorize (decide), commit, notify (event), reverse (rollback) is a complete and finite lifecycle. There is no eighth phase to add.
2. **The set grows only by ratified amendment** (Part 6). The closed-set property was never "six forever"; it was "no unilateral, undocumented, un-conformance-tested additions." `ROLLBACK` is added through exactly the gate that proves the property still holds.

**SEQRD-PC is a mnemonic re-cut, not a wire rename.** The wire keeps its literal performative names. "SEQRD-PC" is the canonical name of the *security-kernel configuration* — the same set, ordered for memorability and pedagogy. Standards bodies do this routinely (a protocol's marketing identity ≠ its on-the-wire tokens).

| Kernel element | Wire performative / mechanic | Plane |
| --- | --- | --- |
| **S — STATUS** | `STATUS` | Speaker |
| **E — EVENT** | `EVENT` (signed fact, `verified` by read-after-write) | System→Speaker |
| **Q — QUERY** | `QUERY` (live, never cached, never memorized) | Speaker |
| **R — ROLLBACK** | `ROLLBACK` (**new** — drives the Saga / compensation engine) | Speaker request |
| **D — DECIDE** | `DECIDE` (owner-plane; powers Decision Cards) | Owner |
| **P — PROPOSE** | `PROPOSE` (evaluated against the GDAG, answered by `PROPOSAL`) | Speaker |
| **C — COMMIT** | `COMMIT` (atomic, idempotency-keyed) | Speaker |

> Note: the wire envelope already lists `PROPOSAL` as a distinct performative and treats `DECIDE` as owner-plane (its own schema). "SEQRD-PC" is a seven-letter *teaching frame* over that machinery; the precise wire enum is enumerated in Part 4.

---

## Part 1 — ROLLBACK semantics: the core theory

### 1.1 The honest premise — most effects cannot be "undone"

A `DELETE` can reverse a `CREATE`. But an invoice that has been paid cannot be deleted; an email that has been sent cannot be unsent; a pallet that has shipped cannot un-ship. Any protocol that *promises* universal undo is lying, and a lying safety protocol is worse than none. `ROLLBACK` therefore makes a **bounded, honest** promise built on the **Saga pattern** (Garcia-Molina & Salem, 1987): backward recovery by *compensation*, where the system cannot truly undo but can execute a *new, governed, forward action* that neutralizes the prior effect's business meaning.

### 1.2 Reversibility tiers (declared per verb, in the profile)

Every verb profile MUST declare exactly one `reversibility` tier. This is the load-bearing addition.

| Tier | Meaning | Reversal mechanism | Example |
| --- | --- | --- | --- |
| `REVERSIBLE` | A clean, deterministic inverse exists. | The inverse verb (e.g. `commerce.delete_product` inverts `commerce.create_product`). | create ↔ delete a draft product |
| `COMPENSABLE` | No inverse, but a *different forward action* offsets the business effect. | A declared **compensating verb** with an argument mapping (e.g. issue a credit-note to offset an invoice). | `services.create_invoice` → `services.issue_credit_note` |
| `IRREVERSIBLE` | No sanctioned reversal exists. | **None.** Must be surfaced at `PROPOSE`/preview time and gated hard. | send an email, ship an order, charge a card with no refund path |

**`IRREVERSIBLE` is a strength, not a gap.** The system refuses to *pretend* it can undo. An attempted `ROLLBACK` of an irreversible effect returns a structured refusal (`code: IRREVERSIBLE`, Part 4.4), never a best-effort silent write. The agent learns the truth instead of a comfortable fiction.

### 1.3 Zero-touch backward compatibility

**The legacy default for any verb that does not declare a tier is `IRREVERSIBLE`.** A shim built before this upgrade — or a minimal shim that never implements compensation — declares (explicitly or by default) every verb `IRREVERSIBLE` and is **100% conformant with zero new code**. It simply cannot be rolled back, and the protocol says so plainly. This is *blind, instant* backward compatibility: nothing existing breaks, and nothing existing silently acquires a capability it can't honor.

### 1.4 Forward-recovery vs backward-recovery (and why we choose backward)

Distributed systems recover from partial failure two ways: **forward** (retry/repair until the whole transaction completes) or **backward** (compensate completed steps and abort). NIL already has forward recovery — the Phase-5 repair loop and `failed_retryable` events. `ROLLBACK` adds the missing **backward** path for when forward recovery is impossible or undesirable (the missing prerequisite can't be created, the owner cancels mid-flight, a downstream step is permanently `failed_terminal`). The two compose: the runtime attempts bounded forward repair first; if that exhausts, it unwinds via compensation.

### 1.5 What ROLLBACK is *not*

- **Not snapshot-restore.** We do not require backends to capture point-in-time state; that is impossible for most SaaS APIs and meaningless for side-effects like email. Reversal is *semantic compensation*, not byte-level restore.
- **Not a privileged escape hatch.** A compensation is itself a write and is governed exactly like any other write (Part 2). There is no "admin undo" that bypasses preview.
- **Not unbounded in time.** Each compensation declares a window/TTL after which the reversal path expires (a credit-note may be issuable for 30 days; after that, `IRREVERSIBLE` in practice).

---

## Part 2 — Governed compensation flow: tier-scaled authority

### 2.1 ROLLBACK reuses the entire preview/commit machinery

The elegant core: **`ROLLBACK` is to compensation what `PROPOSE` is to a forward action.** It does not execute anything. It *requests a reversal*, and the System answers with a `PROPOSAL` — the **compensation preview** — which is then `COMMIT`ted like any other action.

```
ROLLBACK(commit_ref) ──► System computes the compensation
                         ──► PROPOSAL  (human-readable preview of the *reversal*, with its own tier)
                         ──► [authority gate — who may approve, §2.2]
                         ──► COMMIT    (executes the compensating verb, idempotency-keyed)
                         ──► EVENT     (compensated / compensation_refused, signed, verified)
```

Because the reversal lands as a **normal `COMMIT`**, the "no silent write" invariant holds *for free* — there is no separate, unprivileged write path to audit. A rollback is as visible, previewable, and auditable as the action it reverses.

### 2.2 Who may approve scales with tier and blast-radius

The reversal's tier is the tier of its **compensating verb** (issuing a credit-note may be `HIGH` even if creating the invoice was `MEDIUM`). Approval authority scales accordingly, reusing the existing `LOW/MEDIUM/HIGH/CRITICAL` tier vocabulary:

| Compensation tier | Approver | Mechanism |
| --- | --- | --- |
| `LOW` + verb in grant + `REVERSIBLE` | **Agent self-heals** within grant | Standard `PROPOSE→COMMIT`, no human |
| `MEDIUM` | Agent, within grant + budget | `PROPOSE→COMMIT`; logged, reversible-only |
| `HIGH` / `CRITICAL` / any `COMPENSABLE` high-value | **Human, out-of-band** | Forced `DECIDE` on the owner plane (separate credential) |

**Auto-compensation on saga failure is allowed only for pre-blessed `REVERSIBLE` steps.** When a multi-step program fails mid-flight, the runtime may *automatically* unwind only those completed steps whose verbs are (a) `REVERSIBLE` and (b) on an explicit `auto_compensate` allowlist in the grant. Everything else parks and waits for `DECIDE`. This prevents an autonomous "undo storm" while still giving zero-touch recovery for the safe, cheap cases.

### 2.3 Authority lives in the Grant (owner plane)

Reversal authority is not a new credential system — it reuses the **Grant**:

- A **compensating verb is itself a verb** and must be inside the grant's `scopes` to be usable. Per the existing rule, *destructive verbs must be named explicitly* — so a compensating `delete`/`refund` verb can never be reached by a wildcard pattern.
- New optional grant field **`auto_compensate`**: an explicit allowlist of `REVERSIBLE` verbs the durable runtime may unwind without per-step `DECIDE`. Absent ⇒ no auto-compensation (fail-closed, consistent with "absent budget = not granted").
- Compensations consume the **same `budgets`** (actions/monetary/quotas) as forward actions. A reversal is not "free"; it counts.

---

## Part 3 — The DSL becomes a Saga; the fifth axiom absorbs reversibility

### 3.1 No sixth axiom — Bounded Reversibility completes "self-healing grammar"

Per decision, we do **not** add a sixth axiom. Instead we extend the fifth — **"Self-healing grammar"** — to its mature form. The reasoning, to be argued explicitly in the narrative:

> A self-healing grammar is not complete if it can only heal *forward*. A language that can detect its own errors and recompile its plan, but cannot **walk back** the effects it already committed when it hits an immovable wall, is only half-healing. **Bounded Reversibility** — every effect declares *whether and how* it can be undone — is the property that makes self-healing whole. Healing forward (repair) and healing backward (compensation) are the two arms of the same axiom.

So the five constitutional axioms stand unchanged in number; the fifth simply gains a second clause:

> **5. Self-healing grammar** — the language carries its own error handling, returns structured diagnostics the agent can read to fix and recompile its plan (*heal forward*); **and every effect declares its reversibility, so a failed program can be walked back through governed compensation (*heal backward*).**

### 3.2 A nilscript program is a Saga

A multi-step DSL program (the DAG) is, formally, a **Saga**: an ordered sequence of local transactions, each with an associated compensating transaction. The upgrade makes this explicit:

- Each `action` node MAY declare `compensate_with` — a reference to the compensating verb + an argument mapping drawn (by backward-only `$.step_n.output` reference, per axiom 2) from the *result* of the committed step. If omitted, the node's reversibility is taken from its verb profile.
- On commit, each step emits a **compensation handle** (Part 4.3) that the runtime stores against the run.
- On terminal failure of step *k*, the durable runtime unwinds **k-1 … 1 in reverse order**, issuing a `ROLLBACK` per completed step, subject to the §2.2 authority gates. Reversible+blessed steps auto-compensate; the rest park at `await_approval` (`DECIDE`).
- Unwinding is itself **previewable and replayable** — it obeys all five axioms. The "saga compensation order" is a deterministic function of the GDAG, so it is traceable and reproducible.

### 3.3 DSL schema delta (additive)

**As shipped:** `nilscript-dsl.v0.1.schema.json` was **extended in place** (no new `v0.2` schema file):

- `action` node gains optional `compensate_with: { verb, args }` where `args` may reference prior step outputs (backward-only).
- New optional run-level field `on_failure: "compensate" | "park" | "halt"` (default `"park"` — fail-closed, human decides).
- `compensate_with` added to the `actionNode` (new `compensateWith` def); program-level `on_failure: compensate` already existed via `programErrorPolicy`. No existing field changes shape; pre-upgrade programs validate unchanged (the new fields are optional).

---

## Part 4 — Wire & profile schema changes (all additive)

> **As shipped (authoritative):** the upgrade is applied **in place on `nil/schemas/0.1/`** — the `nil` const stays `"0.1"` and `ROLLBACK` is added additively to the existing envelope enum. There is **no `0.2/` schema directory**; SEQRD-PC is the same NILScript dialect evolved, not a fork. The repo already decouples the schema path namespace from the release (`src/nilscript/__init__.py` notes the `/0.1/` segment is historical), so adding an enum value is backward-compatible: a reader that doesn't know `ROLLBACK` simply never receives one. Only the **package** bumps `0.2.0 → 0.3.0`; the **public label** is **"SEQRD-PC v1."** (The subsection headings below say "(0.2)" from the original draft — read them as "the 0.1 schema, extended in place.")

### 4.1 `envelope.schema.json` (0.1, extended in place) — ✅ shipped

- `nil` const: stays **`"0.1"`** (no version-const change; `ROLLBACK` is additive in the same dialect).
- `performative` enum gains **`ROLLBACK`**:
  `["PROPOSE", "PROPOSAL", "COMMIT", "STATUS", "QUERY", "EVENT", "ROLLBACK"]`.
- All other fields unchanged. Edited in **both** `nil/schemas/0.1/` and the vendored `sdk/spec/0.1/`.

### 4.2 New `rollback.schema.json` (0.1, in place) — ✅ shipped (both `nil/` + `sdk/` copies)

```jsonc
{
  "title": "ROLLBACK body (Speaker→System) — request a governed reversal",
  "type": "object",
  "additionalProperties": false,
  "required": ["compensation_token", "reason"],
  "properties": {
    "compensation_token": { "type": "string", "minLength": 8,
      "description": "Handle emitted in the original COMMIT's EVENT result (§4.3). Binds the reversal to a specific committed effect." },
    "reason": { "enum": ["saga_unwind", "owner_cancel", "downstream_failed", "agent_repair"] },
    "idempotency_key": { "type": "string", "minLength": 8,
      "description": "Rolling back twice replays the original compensation outcome, never double-compensates." }
  }
}
```

A `ROLLBACK` is **answered by a `PROPOSAL`** (the compensation preview), exactly as `PROPOSE` is. The preview's `tier` drives the §2.2 authority gate. The owner/agent then `COMMIT`s the previewed compensation.

### 4.3 `event.schema.json` (0.1, in place) — ✅ shipped — emit the compensation handle, report reversal facts

- `result` `$defs` gains an optional **`compensation`** object so every committed effect tells the agent how (and whether) it can be reversed:

```jsonc
"compensation": {
  "type": "object",
  "additionalProperties": false,
  "required": ["reversibility"],
  "properties": {
    "reversibility": { "enum": ["REVERSIBLE", "COMPENSABLE", "IRREVERSIBLE"] },
    "token": { "type": "string", "description": "Present iff reversibility != IRREVERSIBLE." },
    "expires_at": { "type": "string", "format": "date-time",
      "description": "After this, the reversal path is gone; effectively IRREVERSIBLE." }
  }
}
```

- `event` enum gains: `compensating`, `compensated`, `compensation_refused`.
- `result.claim` is unchanged in shape; a successful reversal reports `claim: "success"` with `changed: true, verified: true` (read-after-write confirms the offset took effect). The agent never invents the reversal's success — the System computes it, as today.

### 4.4 `proposal.schema.json` (0.1, in place) — ✅ shipped — the honest refusal

- `code` enum gains **`IRREVERSIBLE`** and **`COMPENSATION_EXPIRED`** for refused `ROLLBACK` requests.
- When a `ROLLBACK` targets an irreversible (or expired) effect, the System returns `outcome: "refusal"` with the appropriate code — *the same refusal machinery that already governs forward proposals*. No new error channel.
- The existing `tier` field on a `proposal` outcome carries the **compensation's** tier for the authority gate.

### 4.5 `commit.schema.json` — unchanged

A compensation is committed through the **existing** `COMMIT` body (`proposal` + `idempotency_key`). This is the point of the design: reversal is not a new write path. No schema change here at all.

### 4.6 Profile (verb-catalog) delta

Each verb profile gains:

- `reversibility`: `"REVERSIBLE" | "COMPENSABLE" | "IRREVERSIBLE"` (**required** on new profiles; **defaulted to `IRREVERSIBLE`** when absent for legacy profiles).
- `compensation` (required iff `COMPENSABLE`): `{ verb, arg_map, tier, window }` — the compensating verb, how its args derive from the original result, its own tier, and the TTL after which reversal expires.

`grant.schema.json` gains optional `auto_compensate: { type: "array", items: string }` (§2.3), default empty (fail-closed).

---

## Part 5 — Toolkit adaptation: the six phases learn to reverse

Each Phase from [`adapter-toolkit-plan.md`](./adapter-toolkit-plan.md) gains a reversibility responsibility. All additive; the 119 existing tests stay green, new tests are added.

| Phase | Today | SEQRD-PC addition |
| --- | --- | --- |
| **1 — Scan / inference** | Infers verbs, args, tiers from an OpenAPI/native surface. | **Infer reversibility tier** from the surface: presence of a `DELETE` that mirrors a `CREATE` ⇒ propose `REVERSIBLE`; a refund/credit endpoint mirroring a charge/invoice ⇒ propose `COMPENSABLE` with a candidate `arg_map`; otherwise `IRREVERSIBLE`. Inference is a *proposal*, surfaced for human confirmation — never silently authoritative. |
| **2 — Scaffold-shim** | Emits a conformant server stub from the manifest. | Emit **compensation stubs**: for each `COMPENSABLE` verb, a stub of the compensating handler + the `arg_map` wiring; for `REVERSIBLE`, wire the inverse; for `IRREVERSIBLE`, emit the explicit refusal handler. |
| **3 — Manifest** | Merges/validates/diffs the contract. | Carry `reversibility` + `compensation` blocks as first-class manifest fields, validated and diffable. |
| **4 — Conformance-test** | 8-row matrix proving the contract holds (encryption, no ghost writes, …). | **New rows proving rollback honesty** (§5.1). This is the developers' proof that the standard is tamper-proof. |
| **5 — Repair loop** | Bounded forward repair; proposes (never hallucinates) missing entities; human-in-loop on ambiguity. | When forward repair exhausts on a multi-step run, the loop **proposes compensation** for completed steps instead of inventing corrective entities. Reversible+blessed steps auto-unwind; the rest park at `DECIDE`. TAR (Transactional Agent Reasoning) now spans both arms. |
| **6 — Evolution memory** | Append-only, content-addressed; `propose_manifest_patch`. | **Records every reversal immutably** (compensation token, tier, approver, outcome) and **anchors the ratified spec** (Part 6). Memory becomes the ledger that makes the lock permanent. |

### 5.1 New conformance rows (the "rollback honesty" matrix)

These are the developers' and evaluators' hard proof. Each is a probe through `httpx` against a live shim:

1. **Compensable verb actually compensates.** Commit a `COMPENSABLE` action, `ROLLBACK` it, assert the business effect is offset and `EVENT.compensated` is emitted with `verified: true`.
2. **Irreversible refuses.** `ROLLBACK` an `IRREVERSIBLE` effect ⇒ `PROPOSAL` refusal `code: IRREVERSIBLE`; assert **no state change occurred** (read-after-write proves the system was not touched).
3. **Idempotent rollback.** Issue the same `ROLLBACK` twice with one `idempotency_key` ⇒ the second **replays** (`replayed: true`), never double-compensates. Assert the offset happened exactly once.
4. **No silent write on reversal.** Assert the compensation flowed through a real `PROPOSAL`→`COMMIT` (a preview existed; an idempotency key existed). A shim that "just undoes" without a preview **fails**.
5. **Tier-gated authority.** A `HIGH`/`COMPENSABLE` reversal attempted without an owner-plane `DECIDE` ⇒ refusal. Proves authority cannot be self-granted by the agent.
6. **Expired compensation.** A reversal past its `window` ⇒ refusal `code: COMPENSATION_EXPIRED`, no state change.
7. **Auto-compensate is allowlist-bound.** A `REVERSIBLE` verb *not* on `auto_compensate` does **not** auto-unwind on saga failure — it parks. Proves fail-closed.

### 5.2 CI drift guard (the anti-"security-washing" gate)

`manifest diff` returns **non-zero** if a shim's declared `reversibility` is not honored by its conformance run — e.g. a verb claims `COMPENSABLE` but row 1 fails, or claims `REVERSIBLE` but the inverse is missing. This stops a commercial shim from *declaring* a safety tier it cannot back up. A standard that cannot detect false claims is not a standard; this gate makes the SEQRD-PC tier labels **earned, not asserted**, and locks the pipeline in GitHub Actions before such a shim reaches production.

---

## Part 6 — "Lock it": the ratification gate

The lock is *not* a breaking version bump (that was Approach B, rejected). It is a **governance event recorded in the Phase-6 append-only ledger.**

1. **RFC.** A ratification document (`docs/rfc/0001-seqrd-pc-v1.md`) states the **0.1 wire (ROLLBACK-extended) / 0.3.0 package / SEQRD-PC-v1 label** triple, the seven performatives, the three tiers, the authority model, and the conformance matrix as the normative standard.
2. **Conformance precondition.** Ratification is *blocked* until the full conformance suite (including the §5.1 rows) passes — the lock cannot be applied to an unproven spec.
3. **Signed anchor.** The **content hash of the frozen spec** (schemas + RFC + conformance matrix) is signed and **committed as an immutable block into the Phase-6 append-only, content-addressed `MemoryStore`.** Because the store is append-only and content-addressed, that block cannot be silently altered: any later change to the spec changes its hash and is visible as a *new* block, never an in-place edit.
4. **Drift = pipeline halt.** Post-ratification, §5.2's non-zero exit on any tier/contract drift keeps the locked standard from decaying. The lock is therefore *active*, not merely declarative: the CI continuously re-proves the anchor.

**Why this is the right lock.** It gives permanence (immutable anchor) and honesty (conformance precondition + continuous drift guard) *without* the regression risk of a wire rename. The constraints become permanent the moment the block is written, while the repository stays green and the `main`/`feat/adapter-toolkit-mvp` split is undisturbed.

---

## Part 7 — Narrative reframe & migration

### 7.1 Landing-doc changes (only after the branch merges + CI is green)

Design docs stay in `nilscript/docs/` and travel with the feature branch. Public narrative is updated **only post-ratification**, to avoid advertising an unratified standard:

- [`narrative/solution.md`](../../wosool-cloud/nilscript-landing/narrative/solution.md) — add `ROLLBACK` to the closed set (now seven), add the SEQRD-PC kernel framing + mapping table, and extend axiom 5 to its forward+backward form.
- `how-it-works/nil-protocol.md` — document the `ROLLBACK` performative, the tiers, and the compensation flow.
- `how-it-works/safety-model.md` — frame `ROLLBACK` as the **"time-travel engine"**: zero-risk because every action carries its own pre-declared reversal blueprint, and the system *refuses to pretend* about `IRREVERSIBLE` effects.

### 7.2 Migration path (v0.2.0 → v0.3.0, additive)

- **No deprecations.** Every 0.1 wire message, 0.1 DSL program, and existing profile remains valid.
- **Default-safe.** Unmarked verbs ⇒ `IRREVERSIBLE` ⇒ existing shims are instantly conformant with zero code.
- **Opt-in capability.** A backend adds reversibility by declaring tiers + compensation in its profile and implementing the stubs — at its own pace, verb by verb.
- **Branch hygiene.** All deltas land on `feat/adapter-toolkit-mvp`. `main` is untouched until the PR + CI gate. The ratification RFC and anchor are committed on the branch so a reviewer sees the full story.

### 7.3 Strategic framing (NTDP / institutional reviewers)

The hybrid path *is* the pitch: it demonstrates backward-compatibility discipline and enterprise stability (the qualities sponsors check before backing a global standard) while broadcasting a fully-realized, zero-risk **SEQRD-PC** governance identity over a repository that stays stable, scannable, and green.

---

## Appendix A — Version axes (state all three, always)

| Axis | Before | After | Public label |
| --- | --- | --- | --- |
| Wire dialect (`nil` const) | `0.1` | `0.1` (ROLLBACK added in place) | — |
| Python package | `0.2.0` | `0.3.0` | — |
| Ratified standard | — | NIL wire 0.1 (ROLLBACK) + pkg 0.3.0 | **SEQRD-PC v1** |

## Appendix B — Open questions for the implementation plan

1. **Compensation of a compensation.** Is a reversal itself reversible? Proposed: a committed compensation is `IRREVERSIBLE` by default (you don't un-refund a refund); revisit per verb if a real case appears.
2. **Partial compensation.** If a saga unwind itself half-fails (compensation of step 2 succeeds, step 1's compensation is refused), what is the run's terminal claim? Proposed: `partial`, park at `DECIDE`, surface the exact residual to the owner. Never report `success`.
3. **`arg_map` expressiveness.** How rich may the compensating-arg mapping be without breaching axiom 5 (least power)? Proposed: backward-only `$.step_n.output.*` references + literals only; no computation.
4. **Compensation-token lifetime vs idempotency-key lifetime.** Do they share a TTL? Proposed: the token's `expires_at` (business reversal window) is independent of and usually longer than the idempotency replay window.

## Appendix C — Files touched (additive map, for the writing-plans pass)

- `src/nilscript/nil/schemas/0.1/` — extended in place: `envelope` (+ROLLBACK enum), `rollback.schema.json` (new), `event` (+compensation), `proposal` (+refusal codes). ✅
- `src/nilscript/sdk/spec/0.1/` — mirrored (vendored copy kept in sync). ✅
- `src/nilscript/dsl/schema/nilscript-dsl.v0.1.schema.json` — `compensate_with` added in place. ✅
- `src/nilscript/cli/scan/inference.py` — tier inference.
- `src/nilscript/cli/scaffold/` — compensation stubs.
- `src/nilscript/cli/manifest/` — carry/validate/diff reversibility.
- `src/nilscript/cli/conformance/` — §5.1 rows.
- `src/nilscript/cli/repair.py` — backward-recovery arm.
- `src/nilscript/cli/memory.py` — reversal records + ratification anchor.
- `docs/rfc/0001-seqrd-pc-v1.md` — new ratification RFC.
- `pyproject.toml` — `0.2.0 → 0.3.0`.

---

*End of design. Next step: `writing-plans` to sequence these deltas into an implementation plan with checkpoints. No code is written until that plan is reviewed.*

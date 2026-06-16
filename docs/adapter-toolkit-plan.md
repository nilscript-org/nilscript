# NILScript Adapter Toolkit — Deep Plan

> **Status:** plan / roadmap. No implementation in this document.
> **Scope:** the `nilscript` CLI ("adapter toolkit") — the tooling that lets any developer build,
> verify, and *progressively de-friction* a NIL adapter for their own system, from the standard
> alone. Builds on the shipped gate-A tools (`verbs` / `profile` / `export-openapi`).
> **Thesis:** building an adapter is mostly **discovery friction** — a system's hidden, undocumented
> requirements that surface only on contact with the live backend. Capture that friction **once**,
> encode it in a shareable artifact, and an agent speaks the system fluently thereafter. The
> toolkit's job is to make that capture mechanical and the artifact communal.

---

## 0. Where we are (the ground truth this plan stands on)

Proven, end to end, against a live ERPNext (Frappe Cloud):

- A NIL translation shim built **from the standard + the translation-shim guide alone** executed a
  real `create_client` and `create_invoice` through the conversational gateway into a real ERPNext.
- The path that cost real effort was **discovery**: ERPNext silently required `company`,
  `income_account`, `cost_center`; rejected a free-text invoice line; and the transport tripped on
  `Expect: 100-continue` (HTTP 417). None of this is in the NIL standard — it is *backend reality*,
  learned through ~5 failed attempts, each error teaching one hidden requirement.

That manual loop — *attempt → read the native error → infer the hidden requirement → adjust → retry*
— is exactly what this toolkit must mechanize. The standard is neutral by design and **must not**
encode ERPNext's `company` field. The toolkit is where backend reality is discovered and stored,
**outside** the standard.

Shipped already (gate A, `nilscript.cli`):

- `nilscript verbs` — the verb catalog, deprecated/parked verbs flagged from the single
  machine-readable source (`"deprecated": true`).
- `nilscript profile <verb>` — a verb's arg-schema.
- `nilscript export-openapi` — the five-endpoint API surface as OpenAPI 3.1.

---

## 1. Design tenets (non-negotiable)

1. **The toolkit reads the standard; it never embeds backend specifics.** Backend reality lives in
   *manifests* (data), never in tool code.
2. **Discovery friction is the enemy. Capture once, share forever.** A requirement learned by one
   developer must never be re-learned by the next.
3. **Grounded over intrinsic** (per 2025 self-correction research): every adjustment is driven by a
   real execution result (a native error, a refusal), never by the model guessing requirements.
4. **The human-confirmation invariant survives.** Auto-resolving a prerequisite is still a proposal a
   human (or a tier-scoped policy) confirms — NIL's "no commit without confirmation" hard rule holds.
5. **Structural vs instance separation.** "Sales Invoice requires `company`" is *structural* (shared,
   public). `company = "abc"` is an *instance value* (private, env/config). Shared manifests carry the
   former, never the latter.
6. **Community-distributed.** One org scans its system once; the world reuses the structural result.

---

## 2. The command surface (the CLI core)

```
nilscript verbs                         # ✅ done — catalog (parked flagged)
nilscript profile <verb>                # ✅ done — arg-schema
nilscript export-openapi                # ✅ done — OpenAPI 3.1 of the 5 endpoints

nilscript scaffold-shim   --name X --lang python   # generate a ready-to-fill shim skeleton
nilscript scan            --url U [--probe] [--safe] # discover a system's hidden requirements → manifest
nilscript conformance-test --url U                  # run the conformance matrix against a live shim
nilscript manifest        validate|merge|diff|show  # work with requirements manifests
```

Package layout (extends the shipped `nilscript.cli`):

```
src/nilscript/cli/
  __init__.py        # ✅ argparse dispatch (verbs/profile/export-openapi)
  _spec.py           # ✅ read-only access to the bundled standard
  _openapi.py        # ✅ OpenAPI builder
  scaffold/          # ▢ shim code generation (templates + JSON-Schema→pydantic)
  scan/              # ▢ capability scan: probe engine + error→requirement inference
  manifest/          # ▢ requirements-manifest schema, load/validate/merge
  conformance/       # ▢ the matrix runner (drives a live shim via the reference SDK)
```

---

## 3. Commands in depth

### 3.1 `scaffold-shim` (Phase 1)

Generate a complete, **bootable** shim skeleton for any system; the developer fills only the
backend-specific translation.

Generated tree:

```
<name>-nil-adapter/
  src/<name>/
    edge.py        # the five NIL endpoints — generic; envelope+auth+lifecycle+idempotency+EVENT
    models.py      # pydantic models GENERATED from the bundled JSON Schemas
    translate.py   # to_native/to_nil STUBS — one per ACTIVE verb; PARKED comment for deprecated
    system.py      # native-system client stub (the only place the dev writes I/O)
    state.py       # idempotency ledger + proposal store + per-workspace sequence
    manifest.py    # loads requirements-manifest.json and pre-fills hidden requirements (§4.4)
  conformance/     # the matrix, ready to run
  requirements-manifest.json   # empty/seed; populated by `nilscript scan`
```

Key decisions (validated):

- **Generate pydantic models from JSON Schema directly** (`datamodel-code-generator
  --input-file-type jsonschema`), **not** via OpenAPI — sidesteps the OpenAPI 3.0/3.1 codegen trap.
- **Active verbs only get fillable stubs.** Deprecated/parked verbs (e.g.
  `commerce.update_order_status`, GAP-001) get a `# PARKED — do not implement` marker, never a stub —
  the toolkit carries the parking decision so no one builds on a leaked verb.
- The **edge is 100% generated and identical across systems**; the dev touches `translate.py` +
  `system.py` only.

DoD: `scaffold-shim` emits a project that boots (edge serves, stubs raise `NotImplementedError`
cleanly, state works), and `conformance-test` against it fails every active verb (empty stubs) —
proving the harness detects non-conformance, not just conformance.

### 3.2 `scan` — the Capability Scan (Phase 2, the heart of this plan)

**Problem it solves:** the discovery friction above. Run once against a system; learn its hidden
requirements for every verb; emit a `requirements-manifest.json` that captures everything learned by
collision so no one repeats it.

```
nilscript scan --url https://system.example --safe   →   requirements-manifest.json
```

**Algorithm (per non-parked verb):**

1. **Probe** the system with a minimal, schema-valid PROPOSE/COMMIT using synthetic test data, in
   `--safe` mode (see safety below).
2. **Capture** the native error verbatim (e.g. Frappe's
   `LinkValidationError: Could not find Customer`, or `Income Account None does not belong to company`).
3. **Infer** the hidden requirement via the **error→requirement inference engine** (§4.3): map the
   error signature to a structured requirement (`needs field X`, `needs prerequisite entity Y`,
   `transport quirk Z`).
4. **Resolve discoverable values** where safe (e.g. query the system's Company / default income
   account) and record them as *instance values* (kept separate from structural requirements).
5. **Record** into the manifest; **re-probe** with the requirement satisfied; iterate until the verb
   either succeeds or yields an irreducible gap (logged, not hidden).
6. **Clean up** every test record the probe created (idempotent teardown).

**Output:** `requirements-manifest.json` (schema in §4.2) — the durable memory of the collision.

### 3.3 `conformance-test` (Phase 4)

Drive a **live** shim with the conformance matrix (the 10 rows from the translation-shim guide §6)
using the reference SDK as the client. Prints pass/fail per verb. Must demonstrably **detect
failure** (run against an empty-stub shim → all fail) as well as confirm success. Optionally consumes
the manifest so it tests with the discovered requirements pre-filled.

### 3.4 `manifest` tooling (Phase 2–6)

`validate` (against the manifest schema), `merge` (combine community + local overrides), `diff`
(detect when a system changed and the manifest is stale), `show` (human-readable). The currency of
the community library (§6).

---

## 4. The requirements manifest — the central artifact

### 4.1 What it is

A per-system, per-verb record of **everything a generic NIL adapter cannot know from the standard**:
hidden required fields, prerequisite entities, value-resolution rules, and transport quirks. The
adapter reads it and pre-fills hidden requirements **before** hitting the system, so the agent never
collides.

### 4.2 Schema (illustrative)

```jsonc
{
  "manifest_version": "0.1",
  "system": "erpnext",                     // structural identity (not an instance hostname)
  "nil_spec": "0.1",
  "verbs": {
    "services.create_invoice": {
      "native_target": "Sales Invoice",
      "hidden_requirements": [             // STRUCTURAL — shareable, public
        {"field": "company",         "kind": "required_scalar"},
        {"field": "income_account",  "kind": "required_on_line"},
        {"field": "cost_center",     "kind": "required_on_line"}
      ],
      "prerequisites": [                   // dependency edges for backward planning (§5.3)
        {"entity": "customer", "from_arg": "party_id", "resolve_with": "services.create_client"}
      ],
      "instance_values": {                 // INSTANCE — NEVER shipped in shared manifests; env/config
        "company": "${ERPNEXT_COMPANY}",
        "income_account": "${ERPNEXT_INCOME_ACCOUNT}",
        "cost_center": "${ERPNEXT_COST_CENTER}"
      },
      "line_shape": "free_text_service_line"   // vs item_code-bound; learned by collision
    }
  },
  "transport_quirks": [                    // STRUCTURAL — e.g. the 417 we hit
    {"quirk": "no_expect_100_continue", "evidence": "HTTP 417 EXPECTATION FAILED"}
  ]
}
```

### 4.3 The error→requirement inference engine

The intelligence of `scan`. A library of **signature → structured-requirement** rules, plus an
LLM-assisted fallback for unseen errors:

| Native error signature | Inferred requirement |
|---|---|
| `LinkValidationError: Could not find <DocType>` | prerequisite entity of type `<DocType>` (link the create-verb) |
| `Income Account None does not belong to company` | `income_account` required on line + `company` required on doc |
| `HTTP 417 EXPECTATION FAILED` (httpx) | transport quirk: drop `Expect: 100-continue` |
| `Mandatory fields required: X, Y` (Frappe) | required scalars `X`, `Y` |

Rules are **data**, contributable by the community (a new system's error dialect → new rules). The
LLM fallback proposes a rule for an unseen error; a human ratifies it into the rule set.

### 4.4 How the adapter consumes the manifest

`scaffold-shim` emits `manifest.py`: before each `to_native`, it overlays the verb's
`hidden_requirements` (filled from `instance_values`/env) and applies `transport_quirks` to the HTTP
client. Net effect: `translate.py` stays small and standard-shaped; the manifest carries the backend
reality. **The agent speaks fluently because the adapter never surfaces a hidden requirement it
already knows.**

---

## 5. The runtime self-correction loop (complementary to scan)

Scan is **ahead-of-time** discovery. But systems change and scans miss cases — so the runtime needs
**grounded self-correction** too. The two are complementary: *scan reduces how often the loop fires;
the loop handles what scan missed; both feed the manifest/memory.* Grounded in current research
(see the research note in the conversation; key patterns: Self-Reflective APIs, Reflexion, Voyager,
Letta/MemGPT, tool-dependency DAGs).

### 5.1 Prescriptive refusals (a NIL contract enhancement)

NIL refusals already carry `code`/`field`/`candidates`/`message`. Add an optional **`repair`** block
so a refusal *prescribes its own fix*:

```json
{"outcome":"refusal","code":"UNRESOLVED","field":"party_id",
 "repair":{"missing_entity":"customer","resolve_with":"services.create_client","carry":"party_id→name"}}
```

The adapter, on a native "entity not found", returns this instead of a raw failure. ("Structure beats
verbosity.")

### 5.2 The bounded repair loop (Grounded Self-Correction + Reflexion)

On a refusal with a `repair` block, the agent: resolves the missing value from conversation context
(the recent-entities pool already exists in the conv-layer — "the customer is obviously عبدالرحيم"),
emits the prerequisite verb as a **proposal** (human-confirmation preserved), then retries the
original. **Bounded** (cap 2–3), **grounded** (driven by the real refusal); if it can't resolve →
ask the human (the existing `SkillError.ask` path). Ambiguity ("عبدالرحيم vs عبدالرحمن?") → the
existing `AMBIGUOUS` + `candidates` path; never invent an entity that might be a typo.

### 5.3 Prerequisite DAG (proactive planning)

`prerequisites` in the manifest/profile form a dependency DAG. The planner **backward-traverses** from
the goal (`create_invoice`) to unmet prerequisites (`customer exists`) and orders the chain
*before* execution — so the common case never fails at all. (LLM-Compiler-style DAG planning.)

### 5.4 Self-evolving memory & skills ("Hermes")

When a repair succeeds, persist the lesson two ways:
- **Reflexion lesson** (verbal): "invoice needs an existing customer; auto-create from `party_id`."
- **Voyager-style reusable skill**: a macro `invoice-with-autocreate` added to a growing skill library.
Use **self-editing memory** (Letta/MemGPT: the agent edits memory blocks via tools) with
**git-versioned context repositories** for safe, auditable, reversible evolution. Optionally feed
confirmed lessons **back into the manifest** — closing the loop between runtime discovery and the
shared artifact.

**Guardrails (or it becomes a liability):** self-editing memory risks catastrophic forgetting and
poisoning — every self-edit is versioned + audited; auto-create is a *proposal*, never a silent write;
the loop is always grounded in a real execution result.

---

## 6. The community dimension (how this scales beyond you)

The split that makes the toolkit a movement, not a chore:

| Who | Builds / does | Frequency |
|---|---|---|
| **The toolkit (you)** | `scan`, the manifest schema, the inference rule set, `scaffold-shim` | once |
| **Each integrator** | runs `scan` on *their* system → contributes a **structural** manifest | once per system |
| **The world** | a **manifest registry**: `erpnext.manifest.json`, `shopify.manifest.json`, `salla.manifest.json` … | continuously, versioned |

A new developer wiring ERPNext does **not** rediscover from zero — they pull the community
`erpnext.manifest.json` (structural requirements only), set their own instance values in env, and
start where the last person finished. This is the user's "detailed scan that yields each adapter's
requirements" — realized **distributed**: each org scans once, the world shares the structural result.

Governance: the registry stores **structural requirements only** (PR-reviewed); instance values and
secrets are forbidden by schema + a sanitizer in `manifest validate`. Versioned per `(system,
nil_spec)`; `manifest diff` flags drift when a system upgrades.

---

## 7. Phased roadmap (gates)

| Phase | Deliverable | DoD |
|---|---|---|
| **0** ✅ | `verbs` / `profile` / `export-openapi` | shipped, 10 tests green |
| **1** | `scaffold-shim` | generates a booting skeleton; conformance-test detects empty stubs as failing |
| **2** | `scan` MVP + manifest schema + inference rules for the known signatures (§4.3) | scan ERPNext → a manifest reproducing the company/income_account/417 findings automatically |
| **3** | adapter manifest consumption (`manifest.py`) | a scaffolded ERPNext shim, fed the scanned manifest, passes `create_invoice` with **zero** manual field-hunting |
| **4** | `conformance-test` full matrix | detects both conformance and non-conformance against a live shim |
| **5** | prescriptive refusals + bounded repair loop | the "عبدالرحيم" scenario auto-creates the customer (as a confirmed proposal) then invoices |
| **6** | self-evolving memory/skills + community manifest registry | a confirmed runtime lesson updates the manifest; registry hosts ≥1 shared structural manifest |

The **starting core** this plan asks to build first = **Phase 1 + Phase 2 MVP**: the scaffold
generator and the capability scan with the manifest schema. Those two turn "build an adapter" from
repeated collision into scan-once.

---

## 8. Honest caveats (the partner's note, kept in the plan)

- **Adoption timing.** `scan` is a smart tool *around a standard no merchant has used yet*. It solves
  the adapter-builder's friction — but the world won't build adapters until the standard proves it
  finds merchants. The proof is now complete (a real customer + invoice via the agent into live
  ERPNext); the friction you hit (417, `company`) is *polish* friction, not *proof* friction. So:
  **document this plan as a real future asset; do not let building it postpone taking the finished
  proof to a merchant.**
- **Scan safety is load-bearing.** Probing writes to a live system. `--safe` must mean: a sandbox/test
  company where available, synthetic data, idempotent teardown, and a hard stop before anything
  irreversible. A scan that leaves debris is worse than no scan.
- **Structural/instance leakage** is the registry's existential risk. Enforce it in schema + sanitizer,
  not by convention.
- **Self-editing memory** without versioning + audit is a foot-gun (forgetting, poisoning).

---

## 9. First concrete step (when building resumes)

1. `nilscript.cli/scaffold/` — generate the edge + JSON-Schema→pydantic models + active-verb stubs.
2. `nilscript.cli/manifest/` — the manifest JSON Schema + `validate` + the structural/instance split.
3. `nilscript.cli/scan/` — the probe loop + the §4.3 inference rules for the *already-known* ERPNext
   signatures (so the very first scan reproduces, automatically, what cost five manual attempts).

Everything else (conformance runner, repair loop, memory, registry) layers on top, gate by gate.

---

*This is a plan, not an implementation. Each phase is a standalone, gated body of work. Build the
starting core (Phase 1 + Phase 2 MVP) only when the standard has earned the merchant that makes
adapter-building demand real.*

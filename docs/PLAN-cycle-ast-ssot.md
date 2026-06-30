# PLAN — The Cycle AST as Single Source of Truth (HTML/DOM for NIL)

> **Status:** approved architecture, migration plan. Local doc. Written 2026-06-30.
> **Decision owner:** ElBasheir A. M. Elkhider.
> **One line:** *A new first-class `Cycle` protocol object becomes the canonical AST. `.nil` text and the
> visual canvas are two authoring views over it; execution, ontology, docs, governance, and simulation
> are all projections of it. The Cycle **embeds** today's governed `WosoolProgram` node union as its
> `Flow` — the proven executor is untouched.*
> **Supersedes the drift named in:** [INVESTIGATION-cycles-intent-governance.md](./INVESTIGATION-cycles-intent-governance.md).

---

## 0. The decision (locked)

**Canonical model = a NEW `Cycle` AST**, a protocol object that lives in the **kernel** (`nilscript`),
NOT in the brain and NOT as extra fields on `WosoolProgram`. It **embeds** the existing
[`kernel/models.NodeType`](../src/nilscript/kernel/models.py) union as its `Flow.nodes`. Execution is
**one projection** (Cycle → `WosoolProgram` IR → executor); the brain becomes **another projection**
(Cycle → ontology), not the owner.

```
                .nil Source ◀────── printer
                     │  parser
                     ▼
        ┌──────────────────────────────┐
        │     CANONICAL CYCLE AST       │   ← single source of truth (frozen, content-hashed)
        │  intent · trigger · context   │
        │  roles · policies · resources │
        │  outcomes · metadata · docs   │
        │  flow: Flow{ Node[] }  ────────┼── Node = TODAY'S WosoolProgram union (embedded, unchanged)
        └───────────────┬──────────────┘
        ┌───────┬───────┼────────┬─────────┬──────────┐
        ▼       ▼       ▼        ▼         ▼          ▼
   Visual   Execution  Brain    Docs/    Governance  Simulation
   Canvas   Compiler   Project. Mermaid  Report      (dry-run)
     │         │
     ▼         ▼
   UI edits  WosoolProgram IR → V1–V6 validator → content-hash → LocalExecutor
```

**Why embed, not extend:** `WosoolProgram` is an *execution* AST. The protocol object accretes
intent/ontology/ownership/metrics/lifecycle/docs/governance — none of which belong on an execution
object. Embedding keeps the governed core (validator, content-hash, executor, ~340 tests) **byte-for-byte
intact** while the protocol surface grows independently. **Why kernel, not brain:** execution must never
depend on the brain (`Editor → Kernel → Execution`, with `Brain` as a side projection — not
`Editor → Brain → Compiler → Kernel`).

**The governance invariant (unchanged, non-negotiable):** every effect still flows
`Cycle → compile → WosoolProgram → V1–V6 validate → content-hash → propose→commit per node`. A verb the
backend never declared still has nothing to bind to (V4). Nothing executes that wasn't lowered,
validated, and approved.

---

## 1. The Cycle AST (Phase 1 deliverable)

New package `src/nilscript/cycle/` in the kernel. Frozen, `extra="forbid"` (reuse `DslModel`), so an
unknown member is structurally unrepresentable — same discipline as `WosoolProgram`.

```python
# src/nilscript/cycle/models.py   (sketch — TDD the real fields)
from nilscript.kernel.models import Node, NodeType, BilingualText   # EMBED, don't redefine
from nilscript.automation.models import TriggerSpec                  # REUSE the closed trigger union

class CycleMetadata(DslModel):
    version: str                          # "1.0"
    owner: str                            # "Sales"
    description: BilingualText | None = None

class EntityRef(DslModel):                # context binding: name -> business entity type
    name: str                             # "customer"
    entity_type: str                      # "Customer"  (resolved against brain ontology at project-time)

class RoleRef(DslModel):
    role: str                             # "SalesManager"

class PolicyRef(DslModel):                # authoring-time governance constraint (declarative)
    policy_id: str
    condition: Condition | None = None    # reuse the brain's data-as-rule Condition grammar shape
    raises_tier: Literal["LOW","MEDIUM","HIGH","CRITICAL"] | None = None

class Outcome(DslModel):
    name: str                             # "success"
    when: Condition | None = None

class Flow(DslModel):
    entry: str                            # node id
    nodes: tuple[Node, ...] = Field(min_length=1, max_length=256)   # ← the EXISTING execution union

class Cycle(DslModel):
    nil: Literal["cycle/0.1"]
    cycle_id: str                         # "SalesLeadLifecycle"  (slug)
    workspace: str
    metadata: CycleMetadata
    intent: BilingualText                 # WHY the cycle exists
    trigger: TriggerSpec                  # manual | schedule | event  (reused)
    context: tuple[EntityRef, ...] = ()
    roles: tuple[RoleRef, ...] = ()
    policies: tuple[PolicyRef, ...] = ()
    resources: tuple[str, ...] = ()       # declared NIL verbs / adapters this cycle may touch
    outcomes: tuple[Outcome, ...] = ()
    flow: Flow
    documentation: BilingualText | None = None
```

Notes:
- **`approval` in `.nil` → `AwaitApprovalNode`** in `flow.nodes` (the existing node), carrying the
  `role`. There is no separate runtime gate — the gate is a governed node.
- **`decision` / `foreach` / `parallel` / `wait` / `retry` / `compensate`** all map 1:1 to existing
  node fields (`ConditionNode`, `ForeachNode`, `ParallelNode`, `WaitNode`, `ActionNode.retry_policy`,
  `ActionNode.compensate_with`). **No new execution semantics needed for v1.**
- **`call Subcycle` / `import`** → lower to the existing cross-system **compose** Stage
  ([automation/compose.py](../src/nilscript/automation/compose.py)); `import` is a Phase-3 linker
  concern. Defer both past v1 (YAGNI) unless a demo needs them.

### Content-hash SSOT lock
`content_hash = sha256(canonical_json(cycle))` — **over the Cycle AST**, not the lowered IR (the IR is
deterministic from the AST, so its hash is derivable). Reuse the canonicalization in
[automation/models.py](../src/nilscript/automation/models.py). This is the version lock: a registered
cycle re-runs the exact bytes a human approved.

### The compiler
```python
# src/nilscript/cycle/compile.py
def compile_cycle(cycle: Cycle, ctx: ValidationContext) -> CompileResult:
    """Cycle AST → WosoolProgram IR, then run the UNCHANGED V1–V6 validator.
    Returns {ok, program, content_hash} or a structured refusal (same taxonomy as today)."""
```
`flow.nodes` lower to `WosoolProgram.pipeline` almost by identity; `trigger`→`AutomationDefinition.trigger`;
`policies.raises_tier`→ tier floor *raised* (never lowered, per the existing invariant); then
[`kernel/validator.validate`](../src/nilscript/kernel/validator.py) runs untouched. Refusal taxonomy is
the existing one (`V4_UNKNOWN_SKILL`, `V4_SCOPE_DENIED`, …).

### Registry
Reuse the `automations` SSOT ([controlplane/store.py](../src/nilscript/controlplane/store.py)): add
`kind='cycle'` and a `source TEXT` (JSON) column holding the canonical Cycle AST; the lowered
`WosoolProgram` stays in `plan` (derived). New endpoints `POST /cycles/draft` and `POST /cycles/register`
mirror the automation endpoints but accept a Cycle AST and run `compile_cycle`. Runs reuse
`fire_manual`/`fire_composed` unchanged.

**Phase-1 tests (TDD, RED first):** AST round-trips JSON; `compile_cycle` lowers a 5-node sales cycle
and the validator passes; an undeclared verb is refused through the lowering; content-hash is
deterministic and idempotent; a `policies.raises_tier=HIGH` makes the lowered node gate.

---

## 2. Phase 2 — Visual ↔ AST, and delete the ungoverned engine

**The highest-value phase: it removes the governance fork.**

- **Frontend** (`wosool-hub` `src/app/cycles/[id]/page.tsx`, `src/lib/`): the canvas
  `BusinessCycle`/`WorkflowNode` TS types become a **thin view** that serializes to / deserializes from
  the Cycle AST. Save → `POST /cycles/register` (via the os-server proxy) → kernel validates, hashes,
  registers. Run → kernel `POST /automations/{id}/run` (`fire_manual`). An undeclared verb is refused
  **in the editor** (the V4 verdict round-trips back as a node diagnostic).
- **os-server** (`app.py`): **delete** `advance_run_logic` + `_execute_node_via_adapter` (≈2065–2348),
  the `cycles`/`runs` SQLite tables, and the hardcoded `grant:"cycle-engine"`. os-server becomes a pure
  broker for `/cycles/*` and `/automations/*` — exactly what it already is for `/automations` and
  `/pending`.
- **HITL unified:** a gated node is an `AwaitApprovalNode`, so cycle approvals flow through the kernel
  `approvals` table and the existing `/decisions` UI / `DecisionPanel` — **one governance queue, not
  two.** Delete the `waiting_approval` SQLite-flag path.
- **Migration of existing cycles:** few/no real cycles exist (MVP). Write a one-shot converter
  (os-server `BusinessCycle` JSON → Cycle AST → `/cycles/register`) or re-create by hand. Do **not**
  build dual-write/back-compat machinery (YAGNI; single-instance correctness).

**Phase-2 tests:** a drawn cycle registers + validates + content-hashes + runs through `LocalExecutor`;
a gated node parks in the real `approvals` queue and an approve-click drives execution
(`_execute_approved`); the os-server cycle tables/endpoints are gone and the integration tests that hit
them are migrated to the kernel path.

**Exit criterion for the drift:** grep shows no `cycle-engine` grant and no os-server-local cycle
executor anywhere. *"Agent proposes, only the kernel commits" is now true on the visual surface too.*

---

## 3. Phase 3 — the `.nil` text surface (second authoring view)

- **Grammar:** Lark (pure-Python, no build step) — `src/nilscript/cycle/nil.lark`. Keep the grammar
  **minimal: only what the AST needs.** No feature in `.nil` that has no AST node (prevents scope creep).
- `src/nilscript/cycle/parse.py` (text → Cycle AST) and `print.py` (Cycle AST → canonical `.nil`).
- **Round-trip guarantee (the trust contract):**
  `parse(print(ast)) == ast` **and** `print(parse(text)) == canonical(text)`. Property-test over a
  golden corpus. This is the HTML↔DOM equivalence made provable.
- The formal `.nil` grammar is also published as a spec artifact in the `nilscript-protocol` repo
  (reference impl stays in the kernel). One paper proposition: *the AST is the canonical form; both
  surfaces are bijective views over it.*

**Why text is second, not first:** building `text→AST→exec` while the UI still edits SQLite would
create two truths — the exact thing we're eliminating. The visual surface must already edit the
canonical AST before the text surface joins it, so both share one model from day one.

---

## 4. Phase 4 — Brain as a projection (invert the coupling)

- `nilscript-graph` gains `project_cycle_to_graph(cycle) -> graph records`: a registered Cycle AST
  projects into ontology nodes/edges (`Flow` + `Role` + `Policy` + `Entity` + `contains` edges). The
  brain **consumes** the protocol; it no longer authors cycles independently.
- `GET /api/graph/cycles` then reflects registered protocol cycles.
- **Naming reconciliation (flag, low-stakes):** the protocol `Cycle` is *one business process*
  (`SalesLeadLifecycle`); the brain's existing `Cycle` is a *domain grouping* (Sales/Finance). A
  protocol `Cycle` projects as the brain's **`Flow`**, `contained` by a brain domain `Cycle`. Either
  keep both terms (protocol Cycle = process, brain Cycle = domain hub) or rename the brain's grouping to
  "Domain" later. Not a blocker; decide before the paper.

---

## 5. Phase 5 — generators (each a pure function over the AST)

All pure `project_*(cycle)`, no new source of truth. Sequence by leverage:
1. **Simulation / dry-run** (walk the AST, propose-only, no commit) and **governance/risk report**
   (tiers, reversibility, approval points) — these carry the product's "generate vs govern" story.
2. **Docs** (markdown), **Mermaid**, **SVG** graph.
3. **JSON / YAML**, **OpenAPI-like** capability description, **AI-context** (the cycle as agent prompt
   context), **SDK** stubs.

---

## 6. Sequencing, risk, and launch-safety

| Phase | Deliverable | New code | Touches governed core? | Risk | Launch-gate |
|---|---|---|---|---|---|
| **1** | Cycle AST + `compile_cycle` + `/cycles/register` | `cycle/` package, 2 endpoints | No (embeds, reuses validator) | Low | ✅ ship |
| **2** | Visual↔AST; **delete os-server engine + cycle-engine grant**; unify HITL | FE serializer, os-server deletions | No | Medium (FE fidelity) | ✅ ship — closes the drift |
| **3** | `.nil` parser/printer + round-trip guarantee | grammar + 2 modules | No | Medium (parser scope creep) | post-drift |
| **4** | Brain projection (`project_cycle_to_graph`) | brain module | No | Low | post-launch |
| **5** | Generators (sim/docs/mermaid/gov/sdk) | pure projections | No | Low | incremental |

**Top risks & mitigations:**
1. **Canvas↔AST fidelity** (Phase 2) — the load-bearing trust. Mitigate with the same round-trip
   property test as Phase 3, applied to the visual serializer: `deserialize(serialize(ast)) == ast`.
2. **Parser scope creep** (Phase 3) — cap the grammar to AST-backed constructs only; defer
   `import`/`call`/subcycles until a real need.
3. **Roles/policies ownership** — the Cycle AST owns *authoring-time* roles/policies (declarative); the
   brain owns *derived* reasoning. Don't duplicate enforcement: `policies.raises_tier` only *raises* the
   kernel tier floor, never lowers it (existing invariant).
4. **Migration** — no dual-write; one-shot convert or re-create. Single-instance correctness only.

**Honest non-goals for v1:** `import`/subcycle linking, inferred ontology mapping (stays
author-declared), authority composition across boundaries, full cron (Temporal lands per
[PLAN-intent-unification-and-durability.md §7](./PLAN-intent-unification-and-durability.md) after the
drift is closed).

---

## 7. First concrete step

Start **Phase 1, TDD**: write `tests/test_cycle_ast.py` (RED) asserting the Cycle AST round-trips and
`compile_cycle` lowers the worked SalesLeadLifecycle example into a validator-passing `WosoolProgram` and
refuses an undeclared verb — then build `src/nilscript/cycle/{models,compile,hash}.py` to green. No
control-plane wiring until the AST + compiler are proven in isolation.

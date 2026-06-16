# 04 — Execution Model (The Virtual Machine)

> How the durable runtime interprets an admitted graph: the lifecycle, the Temporal mapping,
> NIL compilation, idempotency, the human-in-the-loop gate, and the self-healing repair loop.

The runtime is the **virtual machine** for nilscript DSL. It does not understand human language;
it receives the validated graph (the AST) and interprets it node by node, durably.

---

## 1. The execution lifecycle

```
 Generation ─▶ Validation ─▶ Preview ─▶ Approval ─▶ Execution ─▶ (Self-heal on failure)
   LLM          validator     render     human       Temporal       diagnostic → re-compile
```

1. **Generation.** The agent analyses the merchant request and emits a Wosool program.
2. **Validation.** `WosoolDSLValidator` admits or rejects ([03](03-VALIDATION-AND-TYPES.md)).
3. **Preview.** The admitted graph is rendered to a step-by-step, bilingual preview. Each
   `action` previews via its NIL `PROPOSAL` (the System-composed, verbatim preview). **If it
   cannot be rendered, it is rejected** (axiom 3).
4. **Approval.** The merchant confirms. For HIGH/CRITICAL tiers, the owner plane decides on a
   surface this layer cannot write to.
5. **Execution.** The graph is handed to the durable interpreter; each node runs in
   topological order, side effects travelling as NIL sentences.
6. **Self-heal.** A terminal failure produces a structured diagnostic fed back to step 1.

---

## 2. The interpreter — `DynamicGraphExecutorWorkflow` 🔴

A single Temporal workflow that walks the graph. (Net-new; the *primitives* it orchestrates all
exist — see §4.) **For the full deep dive** — execution context, reference resolution, every node
executor, the determinism split, and worked traces — see
[11-RUNTIME-EXPLAINED.md Part B](11-RUNTIME-EXPLAINED.md). The sketch below is the spine.

```python
# Sketch only — illustrative, not the spec. Determinism rules apply (no clock/random/IO here).
@workflow.defn
class DynamicGraphExecutorWorkflow:
    @workflow.run
    async def run(self, program: AdmittedProgram) -> RunResult:
        ctx: dict[str, dict] = {}              # append-only execution context
        node_id = program.entry
        while node_id is not None:
            node = program.nodes[node_id]
            output = await self._exec(node, ctx)   # dispatch by node.type
            ctx[node.id] = {"output": output}      # write-once; never mutated
            node_id = self._next(node, output, ctx) # condition/branch routing
        return RunResult(ctx)
```

Key properties, all inherited from Temporal and already relied on in this repo's workflows:

- **The graph walk is deterministic and replayable.** No wall-clock, randomness, or I/O in the
  workflow body. All non-determinism lives in **activities** (the NIL calls). This is the same
  discipline `workflows.py` already follows (`workflow.unsafe.imports_passed_through`, factual
  deterministic rendering, no OS-locale dependence).
- **The execution context is append-only and immutable.** `ctx[step_k]["output"]` is written
  once. Data references resolve against it. This *is* axiom 2 (Dependency-Only Linking) at
  runtime.
- **State survives crashes.** If the node restarts mid-graph, Temporal's event history replays
  the walk to the exact node it was on; completed activities are not re-run.

---

## 3. NIL compilation — every action is a syscall

The runtime never invents a southbound call. Each node compiles to NIL:

| Node | NIL sentence(s) | Activity |
|---|---|---|
| `action` | `PROPOSE` → (preview) → `COMMIT` | `COMMIT` activity (existing) |
| `query` | `QUERY` | `QUERY` activity (existing) |
| `await_approval` | poll `STATUS` until terminal | `POLL_STATUS` activity (existing) |
| `notify` | — (channel send) | `SEND_OUTBOUND` activity (existing) |
| `action` (undo, on Saga unwind) | `ROLLBACK` → (preview) → `COMMIT` | `COMMIT` activity (existing) |
| `condition`/`foreach`/`parallel`/`wait` | — (control flow) | in-workflow |

The seven speaker-plane performatives a DSL action/query compiles to are `PROPOSE`, `PROPOSAL`,
`COMMIT`, `QUERY`, `STATUS`, `EVENT`, and `ROLLBACK` (the 7th, backward-recovery, added in place
on the 0.1 dialect) — all in `packages/nilscript/sdk/src/nilscript/sdk/sentences.py`. The DSL
**cannot** emit `DECIDE` (owner-plane). The business OS remains the system of record; the graph
holds zero business state and reads truth fresh via `query`.

---

## 4. The runtime primitives already exist

The interpreter is net-new, but every *capability* it dispatches is a Temporal workflow this
repo already ships. The DSL's contribution is letting an LLM **compose** them instead of an
engineer hard-coding each combination.

| DSL node | Existing primitive (`packages/wosool_worker`) | What it proves |
|---|---|---|
| `parallel` / fan-out | `MultiIntentFlow` + `IntentTaskFlow` | durable child-per-branch, `asyncio.gather`, per-branch isolation, ONE aggregate reply |
| `action` commit | `IntentTaskFlow` | bounded retry + idempotency key `parent_turn_id:task_id` |
| `await_approval` | `AwaitDecisionFlow` | poll-or-signal a parked HIGH/CRITICAL proposal to resolution |
| `wait` | `FollowUpFlow` | durable, interrupt-aware delay cadence (cancel signal) |
| `query` + `notify` | `MorningBriefing` | QUERY-only read → bounded bilingual render → outbound |

> **This is the proof the language is buildable, not speculative.** Five of the eight node
> types map onto code that already passes the offline gate. The roadmap's job is the
> *interpreter* and *validator* that tie them together (
> [08-ROADMAP.md](08-ROADMAP.md)).

---

## 5. Idempotency & exactly-once

Temporal activities are **at-least-once** — the engine guarantees exactly-once *workflow-state
progression* via replay, **not** exactly-once side effects. So every `action`'s `COMMIT` must
be idempotent. The DSL derives the key deterministically inside the workflow:

```
idempotency_key = "{run_id}:{node_id}"
```

This mirrors `IntentTaskFlow`'s existing `parent_turn_id:task_id` key. Two consequences:

- A re-delivered webhook or a workflow replay re-issues the **same** NIL `COMMIT` sentence (the
  NIL `idempotency_key` makes the System dedupe) — no double-create.
- The key is **never** generated with `uuid()`/clock inside the activity (non-deterministic
  across retries — would break replay). It is workflow-derived, like the existing code.

The NIL layer already enforces this shape: `CommitBody.idempotency_key` has `min_length=8`, and
proposal ids are URL-safe constrained — see `sentences.py`.

---

## 6. Human-in-the-loop & the approval gate

The `await_approval` node is the language's pause primitive. Modelled on the canonical durable
pattern (signal-sets-flag → `wait_condition` → durable-timer timeout race) and embodied today
by `AwaitDecisionFlow`:

- The graph **suspends** at the gate. Event history *is* the checkpoint — no hand-rolled
  "save approval state to a table."
- Resolution arrives two ways (whichever first): a pushed NIL `EVENT` (`approved`/`rejected`/…)
  cancels the poll, or the poll observes a terminal `STATUS`. Both are already handled.
- On `timeout_seconds` elapsing, the `on_timeout` branch runs.
- The DSL **cannot** approve on the merchant's behalf — it can only *wait*. Approval is
  owner-plane (`DECIDE`), which this layer never speaks.

This makes preview-then-confirm a *structural* property of the language, not a convention.

---

## 7. The self-healing loop (axiom 4)

```
  execute ──fails──▶ structured diagnostic ──▶ Generation layer ──▶ re-compiled program
     ▲                (node id, error class,        (LLM reads it)          │
     └──────────────── message, retriable?)                                 │
                                       re-validate ◀───────────────────────-┘
```

- **Retriable NIL refusals** (`RATE_LIMITED`, `UPSTREAM_UNAVAILABLE` — the repo's
  `RETRIABLE_REFUSALS`) are retried automatically per the node's `retry_policy`. The merchant
  sees a wait-retry line, never a 500 (exactly how `engine.py` handles `NilTransportError`
  today).
- **Terminal failures** (`INVALID_ARGS`, `SCOPE_DENIED`, `AMBIGUOUS`…) become diagnostics. An
  `AMBIGUOUS` refusal even carries `candidates` the LLM can use to disambiguate and re-emit.
- The agent re-compiles a *corrected* program (e.g. drops the denied node, picks a candidate,
  fixes a hint) and resubmits to validation. The loop is bounded — a failed re-compile after N
  attempts surfaces a bilingual "couldn't complete this" to the merchant (never silence — hard
  rule 6).

The diagnostic contract is what makes the LLM a *recoverable* compiler rather than a one-shot
one. The structured-error format from [03 §8](03-VALIDATION-AND-TYPES.md) is the same shape
consumed here.

That is forward healing. When forward repair is not enough — a multi-step program fails after
some steps already committed — the runtime heals **backward** instead (§7.1).

### 7.1 Backward recovery — the Saga unwind (axiom 4, Bounded Reversibility)

When a program declares `on_error: "compensate"`, it is a **Saga**: a terminal failure does not
leave committed steps half-applied. The durable runtime **unwinds the completed steps in
reverse commit order** via governed compensation.

- Each committed `action` left a **compensation handle** (its `compensate_with` action, [02
  §3.1](02-GRAMMAR-AND-PRIMITIVES.md)). The unwind walks completed steps newest-first and issues
  one NIL **`ROLLBACK`** (the 7th performative, backward-recovery) per completed step.
- **A reversal is itself governed.** `ROLLBACK` does **not** execute a write — it *requests* a
  reversal, answered by a `PROPOSAL` (the compensation preview) that is then `COMMIT`ted. So
  preview-then-confirm — "no silent write" — holds even while undoing.
- **What auto-runs vs. what stops:**
  | Step class | Unwind behaviour |
  |---|---|
  | **REVERSIBLE** (on the `auto_compensate` grant allowlist) | Compensation runs automatically — pre-blessed. |
  | **COMPENSABLE** / non-allowlisted | **Parks** for a human `DECIDE`; the runtime never auto-reverses what was not blessed. |
  | **IRREVERSIBLE** (no `compensate_with`) | **Blocks** a full rollback. The run is reported honestly as a **partial** — never "success". |
- New refusal codes surface here: `IRREVERSIBLE` (the step cannot be undone) and
  `COMPENSATION_EXPIRED` (the reversal window has lapsed).

The unwind reuses the same idempotency discipline (§5): each `ROLLBACK`/compensating `COMMIT`
carries a workflow-derived key, so a replay re-issues the same reversal and never double-undoes.
Full trace in [11-RUNTIME-EXPLAINED.md Part B](11-RUNTIME-EXPLAINED.md).

---

## 8. What the runtime guarantees

| Guarantee | Mechanism |
|---|---|
| **Determinism** | pure graph walk + activity-isolated side effects (Temporal replay) |
| **Durability** | event-history checkpointing; resume-exactly-where-stopped |
| **Exactly-once effects** | workflow-derived idempotency keys + NIL dedupe |
| **Isolation** | append-only immutable context; per-branch failure isolation |
| **Bounded cost** | no unbounded loops (validator) + `foreach.max_items` + retry caps |
| **No silent partial execution** | preview-then-confirm; each node's outcome is its own reply |
| **Bounded Reversibility** | `on_error: compensate` → reverse-order Saga unwind via governed `ROLLBACK`; auto only for blessed REVERSIBLE steps, others park/block, irreversible reported as honest partial (§7.1) |

---

Next: **[05-STANDARD-LIBRARY.md](05-STANDARD-LIBRARY.md)** — the skills the language can call.

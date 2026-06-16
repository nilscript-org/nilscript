# nilscript DSL

**A declarative, JSON-based, LLM-native orchestration language for commerce.**

> *Side documentation — a language draft, not yet a shipped subsystem.* This directory
> specifies the **nilscript DSL**: the language an AI agent writes, a static validator admits,
> and a durable runtime executes. It is grounded in this repository's real implementation
> (agents → skills → NIL → Temporal worker) and in the prior art of declarative workflow
> languages (see [09-REFERENCES.md](09-REFERENCES.md)).

---

## The one-paragraph version

A merchant speaks ("اعمل خصم 20% على القمصان وبلّغني لمّا يخلص المخزون"). An **LLM compiles**
that intent into a small, strictly-typed **JSON graph** of actions and logic — the Wosool
program. A **static validator** ("the compiler frontend") checks the graph against a closed
schema, verifies every action is a granted skill, proves the dependency graph is acyclic, and
type-checks the arguments. The validated graph is **previewed to the merchant for approval**.
Only then does the **durable runtime** ("the virtual machine", Temporal) interpret it
step by step — every external action travelling as a **NIL sentence**, committed exactly once,
resumable across crashes.

The language is **deliberately not Turing-complete**. That ceiling is the product: it makes
the agent's plan *verifiable before it runs*.

## The load-bearing idea

nilscript DSL is **not** a new wire protocol and it does **not** replace [NIL](../plan-docs/02-NIL-CLIENT.md).
It is a **graph layer above NIL**. Each `action` node compiles down to a NIL `PROPOSE` →
preview → (human confirm) → `COMMIT` against a whitelisted skill verb. NIL stays the *only*
southbound contract; the DSL adds the *control flow between* NIL sentences that one chat turn
cannot express today.

A multi-step program is also a **Saga**: each `action` may declare `compensate_with`, so on
terminal failure the runtime unwinds completed steps in reverse via a governed NIL `ROLLBACK`
(the 7th performative) rather than leaving the program half-applied — "no silent write" holds
even when undoing.

```
 merchant intent ──LLM──▶ Wosool program (JSON DAG) ──validator──▶ admitted graph
        │                                                                 │
        │                                              preview ◀──────────┤  (human approves)
        ▼                                                                 ▼
   "discount the shirts                              Temporal interprets the graph:
    and tell me when                                 each action → NIL PROPOSE/COMMIT
    stock runs out"                                  durable · idempotent · resumable
```

## What lives here

| File | What it defines |
|---|---|
| [00-INDEX.md](00-INDEX.md) | Read order, status legend, scope |
| [01-LANGUAGE-OVERVIEW.md](01-LANGUAGE-OVERVIEW.md) | Definition, goals, axioms, the tri-layer stack, why-a-DSL |
| [02-GRAMMAR-AND-PRIMITIVES.md](02-GRAMMAR-AND-PRIMITIVES.md) | The closed node set, data references, the expression sub-language |
| [03-VALIDATION-AND-TYPES.md](03-VALIDATION-AND-TYPES.md) | The `WosoolDSLValidator` pipeline, whitelist, diagnostics |
| [04-EXECUTION-MODEL.md](04-EXECUTION-MODEL.md) | Durable runtime, NIL compilation, lifecycle, self-healing, the Saga unwind |
| [05-STANDARD-LIBRARY.md](05-STANDARD-LIBRARY.md) | The skill catalog as the standard library |
| [06-EXAMPLES.md](06-EXAMPLES.md) | Worked programs and the NIL they compile to |
| [07-MAPPING-TO-REPO.md](07-MAPPING-TO-REPO.md) | Every DSL concept → the file/class that already implements it |
| [08-ROADMAP.md](08-ROADMAP.md) | Build order: validator → interpreter → preview UI → self-healing |
| [09-REFERENCES.md](09-REFERENCES.md) | Prior-art citations (ASL, CEL, LLMCompiler, Fowler, …) |
| [10-POSITIONING-AND-PUBLISHING.md](10-POSITIONING-AND-PUBLISHING.md) | What to open-source; the three-tier model (specs / demo / product); MCP-not-n8n |
| [11-RUNTIME-EXPLAINED.md](11-RUNTIME-EXPLAINED.md) | Teaching primer: spec-vs-runtime, and a deep dive into the interpreter (the loop that *is* the runtime) |
| [schema/nilscript-dsl.v0.1.schema.json](schema/nilscript-dsl.v0.1.schema.json) | A draft JSON Schema for the language |
| [conformance/](conformance/README.md) | The compliance corpus — valid/invalid programs + expected verdicts; what makes it a *standard* |

## Status

**Draft v0.1 — specification only.** No `WosoolDSLValidator` or `DynamicGraphExecutorWorkflow`
is built yet. Several primitives this language *names* already exist in the runtime as
hand-written Temporal workflows (`MultiIntentFlow`, `AwaitDecisionFlow`, `MorningBriefing`,
`FollowUpFlow`); the DSL's job is to let an LLM *assemble* those primitives instead of an
engineer hard-coding each flow. See [07-MAPPING-TO-REPO.md](07-MAPPING-TO-REPO.md).

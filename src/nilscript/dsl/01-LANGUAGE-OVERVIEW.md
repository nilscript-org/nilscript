# 01 — Language Overview

> nilscript DSL is the language an AI agent *writes*, a validator *admits*, and a durable
> runtime *executes*. This file defines what it is, what it is for, the principles it
> guarantees, and the three-layer machine that runs it.

---

## 1. Definition

**nilscript DSL** is a domain-specific, declarative, **event-driven, JSON-based** specification
language. It represents commerce business logic as an **acyclic directed graph (DAG)** of
typed nodes — actions, conditions, reads, waits, and approval gates.

It treats **high-level merchant intent as source code**. An LLM transforms a natural-language
request (Arabic — Hejazi/Najdi/MSA — or English) into a strictly-typed, versioned,
fault-tolerant graph of operations. The graph is the agent's *plan*, made inspectable.

It is **Turing-complete for its domain, not in general.** It can express any commerce
automation a merchant can describe — *and nothing else*. There are no unbounded loops, no
file or network access, no user-defined abstractions. (See §6 — that ceiling is the feature.)

It is also a **Saga**: a multi-step program declares how each effect is undone
(`action.compensate_with`), so a failed program is walked back via governed compensation
rather than left half-applied (axiom 5; [02 §3.1](02-GRAMMAR-AND-PRIMITIVES.md)).

### What it is **not**

- **Not a replacement for NIL.** NIL ([Network Intent Layer](../plan-docs/02-NIL-CLIENT.md))
  is the wire contract to any business OS. nilscript DSL is the **graph of NIL sentences** —
  the control flow *between* them. Every `action` node compiles down to a NIL `PROPOSE`/`COMMIT`.
- **Not a general-purpose language.** No arbitrary computation. Real logic lives in named,
  audited *skills*, never inline.
- **Not an executor.** A Wosool program is inert JSON. It can never "run itself"; only the
  trusted runtime can interpret an *admitted* graph.

---

## 2. Purpose

Move AI interaction from **"Stateless Chat"** to **"Durable Orchestration."**

- **Eliminate the black box.** Force the agent to externalize its plan as human-verifiable,
  machine-executable JSON *before* anything happens.
- **Bridge intent to operations.** Connect a business goal ("manage my inventory based on
  sales") to the low-level NIL verbs (`commerce.update_product`, `commerce.process_refund`)
  that achieve it — without the merchant ever seeing a verb.
- **Let an LLM assemble flows an engineer would otherwise hard-code.** Today, multi-step flows
  exist as bespoke Temporal workflows (`MorningBriefing`, `FollowUpFlow`, `MultiIntentFlow`).
  The DSL lets the agent *compose* those primitives on demand — adding a new flow becomes
  emitting a new graph, not shipping new Python.

---

## 3. Core Goals

| Goal | What it means here |
|---|---|
| **Determinism** | The same admitted graph yields the same execution flow regardless of LLM temperature or network weather. Non-determinism is quarantined into activities (NIL calls); the graph walk itself is pure and replayable. |
| **Observability & Transparency** | The graph renders to a step-by-step preview a merchant approves *before* execution. If it cannot be drawn, it cannot run. |
| **Resilience** | Durable execution — checkpointing, bounded retries, state persistence — means a partial failure (node 3 of 5) never corrupts the flow; the runtime resumes exactly where it stopped. |
| **Security (Isolation)** | Strict tenant-level whitelisting: an `action` is admitted only if the workspace's grant scopes allow its verb (default-deny). No forward references, no global mutable state, no escape from the skill catalog. |

---

## 4. The Tri-Layer Architecture

Three layers, mapped to the compiler-design analogy and to *our actual packages*.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. GENERATION LAYER  ── "the compiler"                                     │
│    LLM transforms merchant intent → Wosool program (JSON DAG).             │
│    In repo: wosool_convlayer/agents (ToolCallingAgent, llm.py).            │
│    The LLM emits PLANS, never values it cannot yet know (symbolic refs).   │
└───────────────────────────────┬──────────────────────────────────────────┘
                                 ▼  Wosool program (untrusted JSON)
┌──────────────────────────────────────────────────────────────────────────┐
│ 2. VALIDATION LAYER  ── "the compiler frontend"  (static analysis gate)    │
│    WosoolDSLValidator: schema · references · acyclicity · whitelist · types│
│    In repo: skills/registry.py (whitelist), grants.scope_allows (scopes),  │
│             nil/sentences.py (arg/preview shape).  Validator itself: 🔴.    │
│    Rejects unsafe graphs BEFORE any side effect. Emits structured errors   │
│    the LLM can read to re-compile (self-healing).                          │
└───────────────────────────────┬──────────────────────────────────────────┘
                                 ▼  admitted graph + preview  → human approves
┌──────────────────────────────────────────────────────────────────────────┐
│ 3. DURABLE RUNTIME LAYER  ── "the virtual machine"                         │
│    Temporal interprets the graph node-by-node; each action → a NIL         │
│    PROPOSE/COMMIT; state persisted; distributed retries across API edges.  │
│    In repo: wosool_worker (workflows.py, activities.py).                   │
│    DynamicGraphExecutorWorkflow (the interpreter): 🔴.                      │
└──────────────────────────────────────────────────────────────────────────┘
```

The compiler-design mapping in one line each:

- **Source code** = the merchant's natural-language request.
- **Compiler / parser** = the LLM (Claude), emitting the AST.
- **AST** = the validated JSON graph.
- **Standard library** = the skill catalog (`commerce.create_product`, …) — see
  [05-STANDARD-LIBRARY.md](05-STANDARD-LIBRARY.md).
- **Linker / type checker** = the `WosoolDSLValidator`.
- **Virtual machine / interpreter** = the Temporal worker.
- **Syscall boundary** = NIL. Every "syscall" is a NIL sentence the VM speaks southbound.

---

## 5. The Wosool Axioms

Five principles the language guarantees. Every validator rule and runtime behaviour traces to
one of these.

1. **Atomic Actions.** Every external interaction is a discrete, indivisible unit of work — one
   `action` node → one NIL verb. No node bundles two side effects.
2. **Dependency-Only Linking.** Data flows *exclusively* by reference path
   (`$.step_1.output.id`). There are no global variables. A step reads only the explicit
   outputs of steps it names — which makes the data flow traceable and the run replayable.
   (Borrowed from Amazon States Language `$`/Reference Path and the LLMCompiler/ReWOO symbolic
   variable pattern — see [09-REFERENCES.md](09-REFERENCES.md).)
3. **Human-in-the-Loop.** Every program must be **previewable**. If the system cannot render
   the plan visually for the merchant, the plan is *rejected*. No `action` commits without a
   human-approved preview — this is hard rule 3 of the conversational layer, lifted into the
   language.
4. **Self-Healing Grammar.** The language carries error-handling metadata (`on_error`,
   `retry_policy`). It heals in **both directions.** *Forward:* on failure the runtime informs
   the agent with a structured diagnostic, enabling it to *re-compile a corrected program*
   dynamically rather than dead-ending. *Backward — **Bounded Reversibility:*** every effect
   declares whether and how it can be undone (`action.compensate_with`), so a terminally failed
   program is walked back in reverse via governed compensation rather than left half-applied. A
   grammar that only heals forward is only half-healing. (This is the Saga property; see
   [02 §3.1](02-GRAMMAR-AND-PRIMITIVES.md) and [04 §7](04-EXECUTION-MODEL.md).)
5. **Least Power / Non-Turing-Completeness.** The language is deliberately incomplete. No
   unbounded recursion, no `eval`, no host access. Illegal states are structurally
   unrepresentable. (Dhall's safety argument, Fowler's "limited expressiveness," the
   Configuration-Complexity-Clock warning — [09-REFERENCES.md](09-REFERENCES.md).)

---

## 6. Design Philosophy — why a DSL, not a protocol, and not Python

### Protocol → Language: a change of register

| Dimension | Static protocol (REST + JSON Schema) | nilscript DSL (dynamic language) |
|---|---|---|
| **Nature** | Static — every branch must be pre-coded in the backend. | Composable — the agent assembles flows the engineer never wrote. |
| **Conditionals** | Resolved in backend code; the LLM picks a ready-made path. | The `if/else` (`condition` node) is *generated* by the LLM and *interpreted* by the runtime. |
| **Ceiling** | High friction: the merchant is confined to the buttons you built. | Low friction: any logic expressible in words compiles to executable steps. |

### Why not just let the LLM write Python?

A general-purpose language hands the LLM a loaded weapon: it can emit valid code that hangs
(`while True`), leaks, deletes (`rm -rf`), or escapes the sandbox — and you **cannot statically
prove it won't.** You also cannot *preview* arbitrary Python to a merchant; you only learn what
it does by running it.

A constrained DSL buys, by construction:

- **Guaranteed termination** (no unbounded loops) → previewable, boundable cost.
- **Determinism** → replayable on the durable runtime.
- **Static analyzability** → the whole plan is inspectable before the first side effect.
- **Zero injection surface** → no `eval`, no arbitrary host calls; the only reachable
  operations are whitelisted skills.

> **Analogy.** Python is a full toolbox — hammer, saw, blowtorch: you can build a house, or
> injure yourself. nilscript DSL is a set of modular blocks: you can't build an aircraft with it,
> but you can build any store automation a merchant asks for, in minutes, and the blocks only
> snap together the correct way.

The expressive ceiling is not a limitation we tolerate — it is the safety guarantee we sell.

### When is it "a language"?

When we publish its **documentation** (this directory), a **schema** (a real grammar — see
[`schema/`](schema/nilscript-dsl.v0.1.schema.json)), and an **interpreter/SDK** any developer can
target. On that day Wosool stops being an app feature and becomes an *operating system for
commerce automation* whose native tongue is voice- and text-driven intent.

---

Next: **[02-GRAMMAR-AND-PRIMITIVES.md](02-GRAMMAR-AND-PRIMITIVES.md)** — the closed node set
and the data-reference sub-language.

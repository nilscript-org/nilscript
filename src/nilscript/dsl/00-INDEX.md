# nilscript DSL — Specification Index

*nilscript DSL v0.1 (draft). June 2026. Owner: Basheir (CEO/CTO).*
*Companion to [`plan-docs/`](../plan-docs/00-INDEX.md) — the conversational layer's own plan set.*

## What this is

A specification for **nilscript DSL**: a domain-specific, declarative, JSON-based language that
an LLM writes, a static validator admits, and a durable (Temporal) runtime executes. It is the
**orchestration-graph layer that sits above NIL** — the language of *control flow between* NIL
sentences.

## Read order

1. **[01-LANGUAGE-OVERVIEW.md](01-LANGUAGE-OVERVIEW.md)** — what the language *is*, its goals,
   the five axioms, the tri-layer architecture (Generation → Validation → Durable Runtime), and
   why a constrained DSL beats both a static protocol and a general-purpose language.
2. **[02-GRAMMAR-AND-PRIMITIVES.md](02-GRAMMAR-AND-PRIMITIVES.md)** — the closed set of node
   types (the "keywords"), the `$.step_N.output.field` data-reference sub-language, and the
   constrained expression language for guards.
3. **[03-VALIDATION-AND-TYPES.md](03-VALIDATION-AND-TYPES.md)** — the validator pipeline:
   schema → reference integrity → acyclicity → skill whitelist → argument typing →
   reachability, and the structured diagnostics it returns.
4. **[04-EXECUTION-MODEL.md](04-EXECUTION-MODEL.md)** — how the runtime interprets an admitted
   graph: NIL compilation, idempotency, preview-then-confirm, durable retries, the
   self-healing repair loop, and the **Saga unwind** (reverse-order governed `ROLLBACK`
   compensation on terminal failure).
5. **[05-STANDARD-LIBRARY.md](05-STANDARD-LIBRARY.md)** — the skill catalog as the language's
   standard library: verbs, hint schemas, grant scopes.
6. **[06-EXAMPLES.md](06-EXAMPLES.md)** — worked programs from one-liners to conditional,
   data-passing, approval-gated graphs, each shown with the NIL it compiles to.
7. **[07-MAPPING-TO-REPO.md](07-MAPPING-TO-REPO.md)** — the bridge: every concept above mapped
   to the file/class that already implements (or will implement) it.
8. **[08-ROADMAP.md](08-ROADMAP.md)** — phased build order, aligned to the existing
   multi-intent roadmap.
9. **[09-REFERENCES.md](09-REFERENCES.md)** — the prior art this draft borrows from, with URLs.
10. **[10-POSITIONING-AND-PUBLISHING.md](10-POSITIONING-AND-PUBLISHING.md)** — what to open-source
    and how the language becomes a standard (the three-tier model; MCP-not-n8n).
11. **[11-RUNTIME-EXPLAINED.md](11-RUNTIME-EXPLAINED.md)** — teaching primer: spec-vs-runtime
    (why NIL *and* the DSL are both just specs), plus a deep dive into the interpreter — the one
    loop that *is* the runtime. Start here if "how does the DSL actually run?" is still fuzzy.

Plus **[conformance/](conformance/README.md)** — the compliance corpus (valid + invalid programs
with expected validator verdicts) that any implementation runs to claim conformance.

## Status legend

Used throughout the spec to mark how close each feature is to running code in *this* repo:

| Tag | Meaning |
|---|---|
| 🟢 **implemented** | A runtime primitive for this already exists (named Temporal workflow / engine path). The DSL only needs to *target* it. |
| 🟡 **partial** | Part exists; the DSL adds assembly or generality on top. |
| 🔴 **planned** | Net-new; no code yet. Lives in [08-ROADMAP.md](08-ROADMAP.md). |

## Scope of this draft

**In scope:** the language definition — its grammar, type discipline, validation rules,
execution semantics, standard library, and the mapping to our stack.

**Out of scope (deliberately):** writing the `WosoolDSLValidator` or
`DynamicGraphExecutorWorkflow` Python code. This is the *standard*; the code follows the
standard. Implementation tasks are enumerated, not performed, in [08-ROADMAP.md](08-ROADMAP.md).

## Binding constraints inherited from the conversational layer

nilscript DSL **cannot** relax any of these — they are hard lines of the layer it runs inside
(see [`plan-docs/01-IDENTITY-AND-BOUNDARIES.md`](../plan-docs/01-IDENTITY-AND-BOUNDARIES.md)):

- **NIL is the only southbound contract.** Every `action` compiles to a NIL sentence. The DSL
  invents no new way to reach a business OS.
- **Propose-only.** The DSL can express `PROPOSE` / `QUERY`; it can never express `DECIDE`
  (owner-plane). Approval happens on a surface this layer cannot write to.
- **Zero business state.** A program holds no business tables; business truth is read via NIL
  `QUERY` at execution time, never memorized in the graph.
- **Preview-then-confirm.** No `action` commits without a human-approved preview (hard rule 3).
  An un-previewable program is a rejected program (axiom: *Human-in-the-Loop*).
- **Arabic-first.** Merchant-facing previews and replies are bilingual (ar + en), RTL default.

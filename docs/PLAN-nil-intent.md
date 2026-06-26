# NIL Intent — the single payload

**Invariant:** the model emits ONLY an `Intent` (a semantic payload of *what* it wants). The system
deterministically resolves → governs → executes → returns an `Outcome`. No tool selection, no filter
construction by the model, no keyword/string matching anywhere, no per-entity special-casing. Universal
at the contract level — every adapter inherits it.

## The one payload
```
Intent {
  about: <ontology type>            // adapter-agnostic; resolved by the graph (or identity)
  where: [ Binding{attr, rel, value} ]   // rel ∈ is|contains|gt|gte|lt|lte|between|in  (structural)
  seek:  the | all | count | summary     // shape of knowing (read)
       | change{ op: create|update|remove, set:{attr:value} }   // shape of changing (write)
  page?: {limit, cursor}
}
⇒ Outcome { result | proposal(preview,tier) | refusal(code, fix) }
```
The model fills a schema (expresses *what*); the system owns *how*.

## Deterministic pipeline (resolve_intent — pure code, zero model, zero lexical)
1. resolve `about`/`where` → adapter target + typed filter (BindingResolver + graph; IdentityResolver default).
2. govern: read → free/bulk-gated; change → propose→commit→tier.
3. execute: read → ReadPlane (projection/cap/capability by `seek`); change → write executor.
4. return Outcome (small result / held preview / structured refusal with the corrective parameter).

## Reuse, not greenfield
`/api/assert` + `resolve_bindings` (graph) · `ReadPlane` (built) · propose/commit + approval-executes
(built) · ontology graph (built). This UNIFIES them under one intent surface and extends it to reads.

## Surface
One MCP tool `nil_intent(intent | [intent...])`. Legacy verb tools become the internal execution layer
(hidden from the model). Multiple intents = list; system executes 100% of what's permitted (heavy via
the bulk spine), refuses the rest structurally.

## REL → op (fixed enum map, not keywords)
is→eq · contains→ilike · gt→gt · gte→gte · lt→lt · lte→lte · between→between · in→in

## Phases (TDD)
1. Intent/Binding/Outcome types + `IntentResolver` for reads (the/all/count) over ReadPlane.  ← START
2. summary→aggregate; structured refusals carry the fix.
3. writes: change→propose→commit→tier via the executor.
4. `nil_intent` MCP tool; deprecate verb-selection tools (kept internal).
5. batch intents + partial-allow.
6. Hermes SOUL/skill emits intents only; deploy; verify vague "ابحث عن دينا" on Haiku.

## Universality
Contract (`Intent`+`resolve_intent`+REL map) in the kernel; BindingResolver pluggable (graph supplies
the ontology mapping, IdentityResolver for adapters without one). Conformance: an intent over any
conformant adapter resolves+executes identically. Governance unchanged (reads bounded/projected/capped,
bulk gated; writes propose→commit→tier).

# NILScript — messaging spine

> The single source of narrative truth. The README, PyPI blurb, docs hero, and landing all draw
> from this file so they never contradict each other. If a sentence about "what NIL is" appears on
> two surfaces, it traces back here.

## One-liner

**NILScript is the neutral standard for letting agents act in real systems — safely, with
confirmation, and without bespoke glue per backend.**

## The problem

Every "agent + your system" integration is rebuilt from scratch. Two frictions dominate:

- **Discovery** — a backend's real requirements are hidden and undocumented (required fields,
  prerequisite entities, transport quirks). You learn them by collision.
- **Safety** — an agent must not write blindly. There is no neutral contract that guarantees
  "propose first, commit only on confirmation."

## The insight

Separate the neutral **intent layer** (verbs, envelopes, confirmation, the endpoints) from
**backend reality** (captured once in a shareable manifest). Build an adapter once; scan a system
once; the world shares the result.

## The proof (honest)

A real customer + invoice executed through the conversational gateway into a **live ERPNext**, from
the standard alone. That is the completed proof. There is **no merchant adoption at scale yet** —
the story is "neutral standard + proven reference path," not "battle-tested in production."

## Three pillars

1. **Neutral by design** — no backend specifics in the standard (like OpenAPI for APIs).
2. **Safe by contract** — no commit without confirmation; `PROPOSE` has no side effects;
   `ROLLBACK` previews a compensation, never a silent write.
3. **De-frictioned by tooling** — `scan` once, generate an adapter, share the manifest.

## What it is, precisely (two layers, both specs)

| Layer | Name | What it is |
| --- | --- | --- |
| **Operations** | **NIL** (Network Intent Layer) | The wire contract: how an agent proposes an action, how a backend answers, the envelope, grants, refusals, rollback, and per-domain profiles. Seven performatives (**SEQRD-PC**: STATUS · EVENT · QUERY · ROLLBACK · DECIDE · PROPOSE · COMMIT). |
| **Orchestration** | **nilscript DSL** | A declarative, JSON-based, LLM-native language one layer above NIL: an agent writes a program, a static validator admits it, a durable runtime executes it. |

Both are specs, not software. A reference implementation obeys them; it never defines them.

## Comparables to invoke

OpenAPI, JSON Schema, MCP, Stripe-doc clarity. The mental model is **"OpenAPI for agent-actions."**

## Voice

Precise, engineer-to-engineer, low-hype.

**Banned words:** revolutionary, seamless, magical, effortless, game-changing, blazing-fast.
**Avoid implying:** scale, traction, or merchant adoption that does not exist.
**Prefer:** concrete verbs, real command output, honest status ("young open standard, v0.2").

## Glossary (canonical terms)

- **NIL** — Network Intent Layer; the neutral wire contract.
- **nilscript DSL** — the orchestration language above NIL.
- **Verb** — a named action in a domain profile (e.g. `commerce.create_product`).
- **Envelope** — the request/response wrapper on the NIL wire (`nil`, `grant`, `workspace`, `body`).
- **Performative** — one of the seven SEQRD-PC message kinds.
- **PROPOSE / COMMIT** — the two-step safe-write: propose has no side effects; commit executes.
- **ROLLBACK** — previews and applies a compensation for a reversible verb; never a silent write.
- **Manifest** — `requirements-manifest.json`: a backend's discovered, shareable requirements.
- **Adapter / shim** — the translation layer making one backend speak NIL.
- **Conformance** — the three gates: offline proof, live proof, manifest honesty.
- **Reversibility tier** — `REVERSIBLE` / `COMPENSABLE` / `IRREVERSIBLE`; earned, not asserted.

## Surface-by-surface usage

| Surface | Pulls from this spine |
| --- | --- |
| README hero | one-liner + three pillars + honest status |
| PyPI blurb | one-liner + install + command tour (rendered README) |
| Docs hero | one-liner + problem + insight; links to Concepts |
| Landing hero | one-liner + live terminal demo + two CTAs |

Every reused fact has **one canonical home**; other surfaces link, never fork.

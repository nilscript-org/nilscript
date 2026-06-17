<div align="center">

# nilscript

**The neutral standard for letting agents act in real systems — safely, with confirmation, and without bespoke glue per backend.**

*OpenAPI for agent-actions.*

[![CI](https://github.com/nilscript-org/nilscript/actions/workflows/ci.yml/badge.svg)](https://github.com/nilscript-org/nilscript/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-160%20passing-2ea44f)](https://github.com/nilscript-org/nilscript/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)](https://www.python.org/)
[![spec](https://img.shields.io/badge/NIL%20spec-v0.2-5b5bd6)](https://github.com/nilscript-org/nilscript-protocol/blob/main/nil/0.2.0.md)
[![license](https://img.shields.io/badge/license-Apache--2.0%20AND%20CC--BY--4.0-444)](LICENSE)
[![conformance](https://img.shields.io/badge/conformance-harness%20shipped-2ea44f)](docs/contributing-an-adapter.md)

[Quickstart](#quickstart) · [Commands](#command-tour) · [How it works](#how-it-works) · [Build an adapter](docs/contributing-an-adapter.md) · [Spec](https://github.com/nilscript-org/nilscript-protocol/blob/main/nil/0.2.0.md) · [Status](#where-it-stands)

</div>

---

## New in 0.3.0

- **Discovery handshake** — every adapter exposes `GET /nil/v0.1/describe` returning its *skeleton*: `{nil, system, verbs, targets:{name:{exists, fields[]}}}`. The SDK `handshake(transport)` connects any client uniformly: **reachable → conformant → provisioned**.
- **Mandatory conformance row** — `exposes_describe_skeleton` is now the first check; the matrix is 11 rows. A shim without a valid describe is non-conformant.
- **PROPOSE preflight** — a verb whose native target isn't provisioned is **refused at PROPOSE** (`UPSTREAM_UNAVAILABLE`), not failed after COMMIT.
- **Generic `resource.*` family** (`resource-v1` profiles) — `create / read / update / delete` over **any** target the skeleton exposes, with **no per-entity verb authoring**. `read` is a QUERY; writes ride PROPOSE→COMMIT.
- **Synthesized reversibility** — generic writes are reversible with no per-verb mapping: create→delete, update→restore *before-image*, delete→recreate, all via the standard `ROLLBACK`.
- **Identifier resolution** — `update`/`delete` accept a real id *or* a human identifier (code/name/…), resolved server-side.
- **`STATUS.result`** — a COMMIT returns the SSOT result: `entity{type,id,url}` + `ssot{system,read_after_write}` + compensation handle.
- **Adapter I/O interface** gained `exists(target)`, `schema(target)`, `get(target,id)`; `scaffold-shim` emits all of the above (skeleton-aware adapters by default).

## Why

- **Every agent↔system integration is rebuilt from scratch.** NIL is the neutral wire contract, so you build an adapter *once*.
- **Backends hide their real requirements.** `nilscript scan` discovers them into a shareable `requirements-manifest.json` — you stop learning by collision.
- **Agents must not write blindly.** `PROPOSE` has no side effects; nothing commits without confirmation; `ROLLBACK` *previews* a compensation, never a silent write.
- **Reversibility is earned, not asserted.** Every verb declares a tier (`REVERSIBLE` / `COMPENSABLE` / `IRREVERSIBLE`) the conformance harness actually checks.
- **The standard is data, not software.** Plain JSON + docs any language can implement; the Python SDK/CLI is an optional convenience.

## Quickstart

```bash
# pip install nilscript works on PyPI; for the latest 0.3.0 (with the [cli] fix) install from source until it ships:
pip install "nilscript[cli] @ git+https://github.com/nilscript-org/nilscript.git"

nilscript verbs                                  # the verb catalog from the standard
nilscript scaffold-shim --name my-nil-adapter    # a bootable shim skeleton for any backend
cd my-nil-adapter && pip install -e ".[dev]" && pytest   # red until you fill 3 files (by design)
```

> Three files become yours — `system.py` (the one place I/O happens), `translate.py` (verb ⇄ native),
> `compensation.py` (reversibility). Everything else is generated and identical across adapters.
> Full walkthrough: **[docs/contributing-an-adapter.md](docs/contributing-an-adapter.md)**.

## Command tour

`nilscript` is the toolkit for building and verifying adapters from the standard.

| Command | What it does |
| --- | --- |
| `nilscript verbs` | List the verb catalog from the standard. |
| `nilscript profile <verb>` | Print a verb's arg-schema profile. |
| `nilscript export-openapi` | Emit an OpenAPI 3.1 document for the six NIL endpoints. |
| `nilscript scaffold-shim --name <n>` | Generate a bootable NIL shim skeleton for a backend. |
| `nilscript scan` | Discover a system's hidden requirements → `requirements-manifest.json`. |
| `nilscript conformance-test --url <shim> --verb <v>` | Run the conformance matrix against a live shim. |
| `nilscript manifest <validate\|show\|diff\|…>` | Work with requirements manifests. |

## How it works

NIL separates the **neutral intent layer** from **backend reality**. An agent speaks NIL to a thin
edge; the edge translates to native calls; safe writes are two-step.

```mermaid
flowchart LR
    A[Agent / Speaker] -- NIL envelope --> E[Edge<br/>6 endpoints]
    E --> T[translate.py<br/>verb ⇄ native]
    T --> S[system.py<br/>the only I/O]
    S --> B[(Your backend)]
    M[requirements-manifest.json<br/>discovered once] -. pre-fills .-> E
    subgraph Safe write
      P[PROPOSE<br/>no side effects] --> C[CONFIRM] --> X[COMMIT<br/>executes]
    end
```

The two layers:

| Layer | Name | What it is |
| --- | --- | --- |
| **Operations** | **NIL** — Network Intent Layer | The wire contract: propose/answer/rollback, the envelope, grants, refusals, per-domain profiles. Seven performatives (**SEQRD-PC**: STATUS·EVENT·QUERY·ROLLBACK·DECIDE·PROPOSE·COMMIT) on the stable `nil: "0.1"` wire. |
| **Orchestration** | **nilscript DSL** | A declarative, JSON-based, LLM-native language *above* NIL: an agent writes a program, a static validator admits it, a durable runtime executes it. |

## The ecosystem

| Repo | Role |
| --- | --- |
| **nilscript** (this) | The kernel + canonical JSON schemas — CLI, generator, conformance engine, runtime. `pip install nilscript`. |
| [**nilscript-protocol**](https://github.com/nilscript-org/nilscript-protocol) | The constitution (docs only) — NIL spec, the DSL guides, SEQRD-PC, governance. |
| [**nil-adapter-template**](https://github.com/nilscript-org/nil-adapter-template) | The fork base authors use ("Use this template"). Red until filled. |
| [**pocketbase-nil-adapter**](https://github.com/nilscript-org/pocketbase-nil-adapter) | First 🟢 Official Verified Adapter — a real, conformant PocketBase shim (16/16). |

Architecture & contribution: [adapter-ecosystem-strategy.md](docs/adapter-ecosystem-strategy.md) ·
[contributing-an-adapter.md](docs/contributing-an-adapter.md).

## Install

```bash
pip install nilscript          # the standard only (JSON + docs) — zero runtime deps
pip install nilscript[cli]     # + the adapter toolkit (scaffold-shim, scan, manifest)
pip install nilscript[sdk]     # + the Python SDK (httpx, pydantic)
```

```python
import nilscript
nilscript.spec_path()                               # path to bundled NIL schemas
nilscript.load_profile("commerce.process_refund")   # a profile's JSON Schema
from nilscript.sdk import NilClient                  # only with [sdk]
```

The standard is language-neutral JSON: a Go/TypeScript/Rust implementer reads the schemas in
`src/nilscript/nil/` and `src/nilscript/dsl/` directly — no per-language package reserved (the
OpenAPI / JSON-Schema model).

## Benchmarks

NIL is the layer between the agent and the backend, so we **instrument** established benchmarks and
report a controlled A/B — same model, same attacks, **raw** vs **NIL-gated**. On the
[InjecAgent](https://github.com/uiuc-kang-lab/InjecAgent) prompt-injection suite (ACL Findings 2024):

![InjecAgent: unauthorized-write rate, raw vs NIL](bench/assets/injecagent_safety.svg)

> Across **4,216 evaluations** (2 models × base+enhanced × 1,054 cases), agents were hijacked into an
> unauthorized write 0–4.5% of the time; **through NIL those writes commit 0.00%**, while benign tasks
> stay at **100%**. The result is model- and attack-independent — NIL's defense is structural
> (propose→approve→commit), not a smarter model.

Method, caveats, and the full plan (all four axes — task-success, safety, conformance, performance):
[`docs/benchmarking-plan.md`](docs/benchmarking-plan.md) · harness + how to reproduce:
[`bench/`](bench/). *(Single-step harness, not InjecAgent's two-step ReAct; ASRs are harness-specific
— only the NIL→0 result is the comparable claim. See the plan's credibility notes.)*

## Where it stands

- ✅ **Spec v0.2** released (the NIL spec in [nilscript-protocol](https://github.com/nilscript-org/nilscript-protocol/tree/main/nil)); SEQRD-PC / ROLLBACK in the toolkit.
- ✅ **Conformance harness shipped** — offline proof + live `conformance-test` + `manifest validate`.
- ✅ **160 tests** green; one **live proof** (a real customer + invoice into a live ERPNext, from the standard alone).
- ✅ **First Official Verified Adapter** (PocketBase) standalone and green.
- 🚧 **No merchant adoption at scale yet** — this is a *young open standard (v0.2)*, not battle-tested in production. We lead with the real proof, not traction claims.
- 🚧 **PyPI / docs site / landing** are staged, not yet live.

## Contributing & community

- Change the **standard**: [CONTRIBUTING.md](CONTRIBUTING.md) · [GOVERNANCE.md](GOVERNANCE.md) (spec is extracted from running code).
- Build an **adapter**: [docs/contributing-an-adapter.md](docs/contributing-an-adapter.md) → open an *Adapter submission* issue.
- Security: [SECURITY.md](SECURITY.md) (90-day coordinated disclosure). Conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Dual-licensed by artifact class: **CC BY 4.0** for specification text, **Apache 2.0** for schemas,
conformance vectors, and SDK code. See [LICENSE](LICENSE).

<div align="center">

**[nilscript.org](https://nilscript.org)** · a neutral standard, openly governed

</div>

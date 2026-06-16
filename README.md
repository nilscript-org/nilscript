# nilscript

**The universal standard for connecting systems to agents — a USB port for software.**

`nilscript` is one neutral standard in two layers. The standard is plain JSON +
documentation: any language can read it and implement against it. A thin, optional
Python SDK ships alongside for convenience — but the standard does not depend on it.

| Layer | Name | What it is |
|-------|------|------------|
| **Operations** | **NIL** — Network Intent Layer | The wire contract: how an agent *proposes* an action to a backend, how the backend *answers*, *rolls back* a governed action, the envelope, grants, refusals, and per-domain profiles (commerce, services). Seven performatives (**SEQRD-PC**: STATUS·EVENT·QUERY·ROLLBACK·DECIDE·PROPOSE·COMMIT) on the stable `nil: "0.1"` wire. The "USB protocol." |
| **Orchestration** | **nilscript DSL** | A declarative, JSON-based, LLM-native language a graph layer *above* NIL: an agent writes a program, a static validator admits it, a durable runtime executes it. |

The two layers are specs, not software. A reference implementation (`wosool-cloud`)
obeys both, but never defines them — conformance is defined here.

## Install

```bash
pip install nilscript          # the standard only (JSON + docs) — zero heavy deps
pip install nilscript[sdk]     # standard + Python SDK (httpx, pydantic)
```

```python
import nilscript
nilscript.spec_path()                                   # path to bundled NIL schemas
nilscript.load_profile("commerce.process_refund")       # a profile's JSON Schema
nilscript.dsl_schema_path()                             # path to the DSL JSON Schema

from nilscript.sdk import NilClient                      # only with [sdk]
```

The core install carries **no runtime dependencies** — it is data (`pydantic[email]`
/ `uvicorn[standard]` style: the heavy parts live behind an optional extra).

## Layout

```
nilscript/
├── README.md  LICENSE  GOVERNANCE.md  VERSIONING.md  CHANGELOG.md
└── src/nilscript/
    ├── nil/        # NIL: schemas/0.1/ (+ profiles), registry/, versions/, examples/
    ├── dsl/        # nilscript DSL: schema/, conformance/, language docs
    ├── docs/       # backend conformance + cross-cutting docs
    └── sdk/        # optional Python SDK (imported only with [sdk])
```

Standard files live **inside the package** so they ship in the wheel and are
reachable via `importlib.resources` — the same pattern as
[`jsonschema-specifications`](https://pypi.org/project/jsonschema-specifications/).

## Writing an SDK in another language

The standard is language-neutral JSON. A Go, TypeScript, or Rust implementer reads
the schemas in `src/nilscript/nil/` and `src/nilscript/dsl/` straight from this
repository and writes a client in their language. We do not reserve a package per
language — the world writes SDKs from the same files (the OpenAPI / MCP / JSON-Schema
model).

## License

Dual-licensed by artifact class: **CC BY 4.0** for specification text, **Apache 2.0**
for schemas, conformance vectors, and SDK code. See [LICENSE](./LICENSE).

---
Home: **nilscript.org**

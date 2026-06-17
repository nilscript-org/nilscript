# Contributing a NIL adapter

> This is the **adapter ecosystem** contribution flow. It is distinct from
> [`CONTRIBUTING.md`](../CONTRIBUTING.md) (which governs *normative spec* changes) and from
> [`GOVERNANCE.md`](../GOVERNANCE.md) (the Steward/Reference-Implementation rule). Building an
> adapter changes **no** normative text — you are implementing the standard, not amending it.

An **adapter** is a translation shim that makes one backend (PocketBase, Salla, Supabase, …)
speak NIL. You never fork the core (`nilscript`) to build one — the core is the protected
kernel. You fork the template, fill three files, prove conformance, and submit.

See the architecture rationale in
[`adapter-ecosystem-strategy.md`](./adapter-ecosystem-strategy.md).

## 0.3.0 — what your adapter must expose

Your `system.py` client implements the I/O **and** three discovery/CRUD primitives:

- `exists(target) -> bool` and `schema(target) -> list[{name,type,required}] | None` — power
  `GET /nil/v0.1/describe` (your *skeleton*) and the **PROPOSE preflight** (a write to a
  missing target refuses with `UPSTREAM_UNAVAILABLE`, never a silent COMMIT failure).
- `get(target, id) -> dict | None` — the *before-image* that makes **generic reversibility** work.

In return the generic edge gives every adapter, for free:
- **Discovery** — `describe` is the **mandatory** `exposes_describe_skeleton` conformance row.
- **Generic `resource.*` CRUD** (`create/read/update/delete`) over any provisioned target, with
  synthesized rollback (create→delete, update→restore, delete→recreate) and id-or-human-identifier
  resolution. You only author *semantic* verbs (e.g. `commerce.create_product`) when they deserve
  bespoke previews/compensation; everything else is covered generically.

Unknown verbs refuse with `UNKNOWN_VERB`. Conforms to **nilscript ≥ 0.3.0**.

## The journey

```
 1. Use the template ──▶ github.com/nilscript-org/nil-adapter-template  ("Use this template")
                              │   → your repo  <service>-nil-adapter
 2. Fill three files  ──▶ src/<pkg>/system.py · translate.py · compensation.py
 3. Prove it          ──▶ offline pytest (green)  +  live `nilscript conformance-test`  +  `manifest validate`
 4. Submit            ──▶ open an "Adapter submission" issue on nilscript-org/nilscript
 5. Adopt             ──▶ reviewed, then re-homed as nilscript-org/<service>-nil-adapter, badged Official Verified
```

The edge / state / models / manifest loader are generated and identical across every adapter —
**do not edit them**. You only touch your backend's I/O and verb mapping. The per-author detail
lives in the template's [`CONTRIBUTING.md`](https://github.com/nilscript-org/nil-adapter-template/blob/main/CONTRIBUTING.md).

## The three conformance gates (what "conformant" means)

Conformance is **not** "passes the kernel's own test suite". An adapter proves conformance by
three concrete gates, all wired into the template's CI:

1. **Offline proof** — `pytest` green: every active write verb reaches `executed` against the
   in-memory `FakeSystem`, and rollback-honesty holds (a reversible verb mints a compensation
   token and `ROLLBACK` *previews* a compensation — never a silent write; an unknown token is refused).
2. **Live proof** — `nilscript conformance-test --url <shim> --verb <verb>` green for every write
   verb, including the rollback-honesty rows across all three reversibility tiers
   (`REVERSIBLE` / `COMPENSABLE` preview; `IRREVERSIBLE` refuses honestly).
3. **Manifest honesty** — `nilscript manifest validate requirements-manifest.json` passes.
   Reversibility tiers are **earned, not asserted**.

## The review gate (how Official Verified is granted)

Submission is an issue/PR against `nilscript-org/nilscript`. Adoption requires **both**:

- **Automated:** the adapter's CI is green — all three gates above, on a public commit.
- **Human security review** by a maintainer, covering:
  - **No silent writes** — `ROLLBACK` always previews; reversibility is honored, never faked.
  - **Tier honesty** — every declared reversibility tier is backed by real compensation behavior.
  - **Secret hygiene** — no credentials in the repo; backend auth via env/secret only.
  - **Boundary safety** — inputs validated at the edge; no SSRF / unbounded fan-out to the backend.

On acceptance the repo is transferred (or re-homed) under `nilscript-org/<service>-nil-adapter`
and listed in [`IMPLEMENTATIONS.md`](../IMPLEMENTATIONS.md).

## Badges

| Badge | Meaning |
| --- | --- |
| 🟢 **Official Verified Adapter** | Core-team owned, CI-green, passed human security review. |
| 🔵 **Community** | Listed, conformance-green, not yet adopted/reviewed. |

A signed, hosted **certificate** is on the roadmap — see
[`attestation-design.md`](./attestation-design.md). Until that service exists, "certified" means
exactly the three gates passing in the adapter's CI. Do not advertise a signed certificate yet.

## Naming & versioning

- Repo: `<service>-nil-adapter` (e.g. `pocketbase-nil-adapter`).
- Python package: `<service>_nil_adapter`.
- Pin the **minimum kernel version** you conform to (e.g. `nilscript >= 0.3.0`); your own version
  is independent and released on your own cadence.

## Worked reference

[`pocketbase-nil-adapter`](https://github.com/nilscript-org/pocketbase-nil-adapter) is the first
Official Verified Adapter and the canonical worked example (also kept in-core at
[`examples/pocketbase-adapter/`](../examples/pocketbase-adapter/) for regenerate-and-verify).

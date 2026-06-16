# Adapter Ecosystem & Multi-Repo Strategy — Plan + Handoff

> **Status:** Plan (decided architecture; not yet executed beyond the in-repo example) · **Date:** 2026-06-16
> **Owner:** nilscript-org · **Trunk:** `main` (all SEQRD-PC work merged; no side branches)
> **Companion docs:** [`seqrd-pc-v0.3-design.md`](./seqrd-pc-v0.3-design.md) · [`adapter-toolkit-plan.md`](./adapter-toolkit-plan.md) · [`../IMPLEMENTATIONS.md`](../IMPLEMENTATIONS.md)

## TL;DR — the decision

Model the project as an **ecosystem**, exactly like Frappe (core `frappe` + official apps `erpnext`/`hrms`/`crm` as separate repos). Three tiers of repository, with one **golden rule**:

> **Nobody forks the core (`nilscript`) to build an adapter.** The core is the protected kernel: the CLI, the generator, the conformance engine, the constitutional schemas. Adapter authors fork a dedicated, empty **`nil-adapter-template`** repo instead.

```
        nilscript-org  (the GitHub Organization)
        ┌────────────────────────────────────────────────────────────┐
        │  1. nilscript            ← CORE kernel (protected, no forks) │
        │       └─ examples/       ← reference adapters live HERE now  │
        │  2. nil-adapter-template ← the repo developers FORK / "use"  │
        │  3. <svc>-nil-adapter    ← official, standalone adapter repos │
        │       e.g. pocketbase-nil-adapter, salla-nil-adapter, …      │
        └────────────────────────────────────────────────────────────┘
```

---

## Part 1 — The three-tier repository taxonomy

| Tier | Repo | Contains | Forkable to build an adapter? | Frappe analogue |
| --- | --- | --- | --- | --- |
| **Core** | `nilscript` | NIL wire schemas, the DSL, the CLI (`scaffold-shim`, `scan`, `conformance-test`, `manifest`), the SDK, `tests/`, and `examples/` (reference adapters). | **No** — it is the protected kernel. | `frappe` (framework) |
| **Template** | `nil-adapter-template` | A *pristine* `nilscript scaffold-shim` output: the generic edge/state/models + empty `translate.py`/`system.py`/`compensation.py` stubs + the bundled conformance proof (red until filled) + CI. Marked a **GitHub Template Repository**. | **Yes** — this is the one developers fork / "Use this template". | — (Frappe uses `bench new-app`; ours is a template repo) |
| **Adapter** | `<service>-nil-adapter` | A filled, conformant shim for one backend (PocketBase, Salla, Supabase, …). Standalone, independently versioned. | n/a (it's the product) | `erpnext`, `hrms`, `crm` |

**Why not one mega-monorepo forever?** Reference examples belong *in* the core (instant consistency — see Part 2). But *production* adapters for third-party services are independently released, owned, and versioned — they must be standalone so a vendor or community author can ship on their own cadence without touching the kernel.

---

## Part 2 — Where examples live (now vs. later)

**Now (shipped):** reference adapters live in **`nilscript/examples/`**. The PocketBase adapter is already there: [`examples/pocketbase-adapter/`](../examples/pocketbase-adapter/). Keeping a canonical example *inside* the core gives **instant consistency**: any change to the scaffold templates or the kernel is regenerated and re-verified against the example in the *same commit*. This is the "is the standard real?" proof a reviewer (or an NTDP evaluator) opens first.

**Rule of thumb:**
- **In-core `examples/`** → 1–3 *canonical* reference adapters maintained by the core team, regenerated from the current templates. They are *teaching artifacts*, not products.
- **Standalone `<svc>-nil-adapter` repos** → everything community/vendor-owned, or any adapter meant to be installed and versioned independently.

> Note on test collection: `examples/` is **excluded from the core's pytest** (`testpaths = ["tests"]` in `pyproject.toml`). Each example ships its own proof, run from its dir with `PYTHONPATH=src python -m pytest`. (This was the `fix(tests)` commit — examples must never break the kernel's suite.)

---

## Part 3 — The developer journey (the Conformance Fork)

```
 1. Developer opens ─────▶ [ nil-adapter-template ]        (public GitHub Template Repository)
                                  │  "Use this template" / Fork
                                  ▼
 2. Their own repo ──────▶ [ my-supabase-adapter ]
                                  │  fill three files: system.py · translate.py · compensation.py
                                  ▼
 3. Prove it ────────────▶  pytest (offline)  +  nilscript conformance-test --url <live shim>
                                  │  green: write rows + the three rollback-honesty tiers + manifest validate
                                  ▼
 4. Submit ──────────────▶  PR / "request official status"  to nilscript-org
                                  │  core team reviews: security, no silent writes, tier honesty
                                  ▼
 5. Adopt ───────────────▶  transferred / re-homed as [ nilscript-org/supabase-nil-adapter ]
                            badged "Official Verified Adapter"
```

The kernel is never forked; the developer only ever touches the **template** and their own backend's I/O + mapping.

---

## Part 4 — What "conformant" precisely means (be honest about it)

A passing adapter is **not** "passes the 160 tests" — those 160 are the *kernel's own* suite. An adapter proves conformance by **three concrete, already-shipped gates**:

1. **Offline proof** — its bundled `conformance/test_conformance.py` is green: every active write verb reaches `executed` against the in-memory `FakeSystem`, and the rollback-honesty test passes (a reversible verb mints a compensation token and `ROLLBACK` *previews* a compensation; an unknown token is refused).
2. **Live proof** — `nilscript conformance-test --url <running shim>` is green, including the **rollback-honesty rows across all three tiers**: `REVERSIBLE`/`COMPENSABLE` preview a compensation (no silent write), `IRREVERSIBLE` refuses honestly, unknown/expired tokens never trigger a phantom reversal.
3. **Manifest honesty** — `nilscript manifest validate <manifest>` passes, and `nilscript manifest diff` is the **CI drift guard**: a non-zero exit if the shim declares a reversibility tier it does not honor. Tiers are *earned, not asserted*.

**Roadmap (not built yet):** a hosted attestation service that signs a conformance run into a **certificate** (anchored via the Phase-6 `compute_spec_hash` / `anchor_ratification` ledger already in the kernel). Today, "certification" = the three gates above passing in the adapter's CI. Do not advertise a signed certificate until that service exists.

---

## Part 5 — Migration path: `examples/` → standalone official repo

When the PocketBase adapter (or any example) graduates to a standalone product:

1. **Extract** `examples/pocketbase-adapter/` → new repo `nilscript-org/pocketbase-nil-adapter` (preserve history with `git subtree split` or `git filter-repo`).
2. **Depend on the published standard**, not a relative path: the standalone repo `pip install nilscript` and runs `nilscript conformance-test` in CI against the *released* kernel — this is what keeps it honest as the standard evolves.
3. **CI = the drift guard**: the adapter's CI runs its offline proof + a live conformance-test (spinning up the backend in a service container) + `manifest diff`. A kernel release that would break it surfaces as a red CI here, on the adapter, not silently.
4. **Leave a pointer** in the core: keep a tiny `examples/pocketbase-adapter/README.md` stub (or an `IMPLEMENTATIONS.md` row) linking to the now-standalone repo, so the core still advertises it.
5. **Keep one canonical in-core example** regardless — the core should never have *zero* examples to regenerate-and-verify against.

---

## Part 6 — Building `nil-adapter-template` (the fork base)

The template repo is essentially **"`nilscript scaffold-shim` output, frozen, with CI"**:

- Generated tree: `src/<pkg>/{edge,state,models,manifest,system,translate,compensation,run}.py`, `conformance/test_conformance.py`, `requirements-manifest.json`, `pyproject.toml`, `README.md`.
- `system.py` / `translate.py` / `compensation.py` ship as **stubs that raise `NotImplementedError`** → the bundled proof is **red on day one** (this is the point: the harness detects non-conformance).
- A **GitHub Actions** workflow: `pip install nilscript`, run the offline proof, and (optionally) a live conformance-test. Red until the author fills the stubs.
- Flagged **"Template repository"** in GitHub settings so contributors get a clean "Use this template" button (no fork lineage noise).
- A `CONTRIBUTING.md` describing the journey in Part 3 and the gates in Part 4.

**Open question:** keep the template as a *static* checked-in scaffold, or generate it in CI from the core's `scaffold-shim` on each kernel release (so it never drifts from the generator)? Recommended: **generate-in-CI** — a release job in `nilscript` runs `scaffold-shim` and pushes the result to `nil-adapter-template`, guaranteeing the template == the current generator output.

---

## Part 7 — Naming & governance conventions

- **Org:** `nilscript-org`.
- **Adapter repos:** `<service>-nil-adapter` (e.g. `pocketbase-nil-adapter`, `salla-nil-adapter`, `supabase-nil-adapter`). Python package inside stays `<service>_nil_adapter`.
- **Badges:** `Official Verified Adapter` (core-team owned, CI-green) vs `Community` (listed, conformance-green, not yet adopted).
- **Registry:** maintain `IMPLEMENTATIONS.md` in the core as the index of known adapters (official + community) with their conformance status and last-verified kernel version.
- **Versioning:** an adapter pins the **minimum kernel version** it conforms to (e.g. `nilscript>=0.3.0`); its own version is independent.

---

## Part 8 — Phased rollout & handoff checklist

**Phase 0 — done ✅**
- [x] SEQRD-PC upgrade shipped on `main` (ROLLBACK across wire/SDK, full toolkit, six endpoints, CRUD dispatch).
- [x] PocketBase adapter built end-to-end and committed as `examples/pocketbase-adapter/` (16/16 offline, live conformance green across all three tiers).
- [x] `examples/` excluded from the kernel's pytest.

**Phase 1 — the template repo — done ✅**
- [x] Create `nilscript-org/nil-adapter-template` (public, "Template repository"). → https://github.com/nilscript-org/nil-adapter-template (`is_template=true`, default `main`).
- [x] Seed it from `nilscript scaffold-shim` output; wire the CI proof (red until filled). CI (`conformance.yml`) has three jobs: `offline` (self-contained pytest — **red by design** until the stubs are filled: 15 verbs `not conformant yet — fill translate.py`), `manifest validate` (**green**), and an opt-in `live` gate (`workflow_dispatch` with a shim URL + verb).
- [x] Add `CONTRIBUTING.md` (the Part 3 journey + Part 4 gates) and a template-framed README.
- [x] Decide & implement: **static scaffold now.** generate-in-CI is deferred until the kernel is PyPI-installable (Decision #4); the static scaffold is the prerequisite either way and the release-job automation layers on without rework. The offline CI gate is kept **self-contained** (no `nilscript` install), so PyPI does not gate it — only the `manifest`/`live` jobs `pip install nilscript[cli]` from git for now.

**Phase 2 — first standalone official adapter — done ✅**
- [x] `git subtree split` PocketBase out of `examples/` → `nilscript-org/pocketbase-nil-adapter` (history preserved). → https://github.com/nilscript-org/pocketbase-nil-adapter
- [x] Switch it to `pip install nilscript` (no relative path) + CI. The adapter is self-contained (no `nilscript` runtime import), so the kernel dependency lives in CI: offline proof **16/16 green** + `manifest validate` (kernel CLI from git) + an opt-in live gate (`workflow_dispatch` per-verb). `manifest diff` (re-scan vs committed) is a documented drift-guard pattern; `scan --url` live probing is not yet wired in the kernel, so CI uses `manifest validate` today.
- [x] Badge it `Official Verified Adapter`; add to `IMPLEMENTATIONS.md`; leave an in-core pointer. The in-core `examples/pocketbase-adapter/` is **kept** as the canonical regenerate-and-verify example (Part 5 item 5), with a README banner pointing to the standalone repo.

**Phase 3 — open the ecosystem**
- [ ] Publish the community contribution flow (fork template → fill → conformance → PR).
- [ ] Stand up the conformance/attestation roadmap item (hosted signed certificate via the ledger anchor).
- [ ] Onboard the next adapters (Salla, Supabase, …).

---

## Part 9 — Open decisions (for the next session)

1. **Template generation:** static checked-in scaffold vs generate-in-CI from `scaffold-shim`? (Recommended: generate-in-CI.) → **Resolved (Phase 1): static now**, generate-in-CI deferred until the kernel is on PyPI (Decision #4). The static seed is the prerequisite for either path, so no rework is lost.
2. **Monetization/governance of "Official Verified":** what exactly gates adoption — automated CI only, or core-team security review too? (Recommended: both — CI green *and* human security review.)
3. **Hosted attestation:** is the signed-certificate service in scope soon, or do we ship with "CI-green = conformant" first? (Recommended: ship CI-green first; attestation later.)
4. **PyPI publish of the kernel:** the standalone-adapter CI assumes `pip install nilscript` works publicly — confirm the kernel is (or will be) on PyPI as `nilscript` (the `[cli]→pydantic` coupling noted in HANDOFF.md must be fixed first).

---

## Appendix — answering the two questions directly

> **"Should adapters be repos, so people fork an approved repo of ours?"**
Yes — *standalone* repos per official adapter (`<service>-nil-adapter`). But the repo they **fork to build** is **not** any adapter and **not** the core; it is the dedicated **`nil-adapter-template`**.

> **"Is the base they fork `nilscript` itself?"**
No. The core (`nilscript`) is protected and never forked to build an adapter. Forks happen off **`nil-adapter-template`**. The core only *receives* adapters (via PR/transfer) and *hosts* the canonical example(s) in `examples/`.

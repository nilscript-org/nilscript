# Handoff — Adapter Toolkit + Content Plan (branch `feat/adapter-toolkit-mvp`)

**Date:** 2026-06-16 · **Branch:** `feat/adapter-toolkit-mvp` (off `main`) · **Tests:** 120 passing
· **Status:** code complete + reviewed; two plan docs awaiting execution. Nothing published; `main`
untouched.

---

## What shipped (the adapter toolkit — all 6 plan phases)

Implements `docs/adapter-toolkit-plan.md` end to end. Turns "build a NIL adapter" from repeated
discovery-collision into **scan-once**.

| Phase | What | Where |
|---|---|---|
| 1 | `scaffold-shim` — generate a bootable shim; dev fills only `translate.py` + `system.py`; pydantic models generated from the bundled JSON Schemas; parked verbs marked, never stubbed | `src/nilscript/cli/scaffold/` |
| 2 | `scan` — error→requirement inference engine reproduces the live ERPNext collisions (company/income_account/417/customer-prereq) into a `requirements-manifest.json`; structural/instance split enforced by a sanitizer | `src/nilscript/cli/scan/`, `cli/manifest/` |
| 3 | manifest consumption — generated overlay fills scalar **and** line-level hidden requirements from `${ENV}` instance values; zero manual field-hunting | `cli/scaffold/_templates.py` |
| 4 | `conformance-test` — transport-agnostic 8-row matrix; detects conformance **and** non-conformance | `cli/conformance/` |
| 5 | prescriptive refusals + bounded grounded repair loop (the عبدالرحيم auto-create-then-invoice scenario) | `cli/repair.py` |
| 6 | `manifest merge`/`diff` (registry currency) + append-only content-addressed lessons/skills memory with supersede-not-delete guardrails | `cli/manifest/`, `cli/memory.py` |
| — | bare `nilscript` launches a banner + command list | `cli/_banner.py` |

**Commits (5, ahead of `main`):**
```
61a50cb feat(cli): bare `nilscript` launches a banner + command list
613940b feat(cli): Phases 4-6 — conformance-test, prescriptive repair loop, registry + evolving memory
4f880b2 feat(cli): Phase 3 — adapter consumes the scanned manifest (line-level + scalar)
f4ea564 feat(cli): adapter toolkit MVP — scaffold-shim + capability scan + manifest
9bd9511 docs: deep design plan for the adapter toolkit
```

**CLI surface now:** `verbs · profile · export-openapi · scaffold-shim · scan · conformance-test ·
manifest {validate,show,strip,merge,diff}` (+ bare banner).

---

## How to run it

```bash
# install (project uses a uv venv; system Python is PEP-668 managed)
cd /home/ubuntu/Downloads/nizam/nilscript
pipx install -e ".[cli,sdk]"        # -> ~/.local/bin/nilscript (globally on PATH)
# or, no install:
PYTHONPATH=src python -m nilscript.cli verbs

# the end-to-end flow
nilscript scaffold-shim --name acme-nil-adapter --dest .
nilscript scan --replay collisions.json -o erpnext.manifest.json
nilscript manifest validate erpnext.manifest.json
nilscript conformance-test --url http://localhost:8099 --verb services.create_invoice --args '{...}'

# tests
PYTHONPATH=src python -m pytest -q tests/   # 120 passing
```

---

## Known issues / tech debt

1. **`[cli]` install needs `pydantic` (should not).** `nilscript/__init__.py` imports the SDK
   eagerly, so `pip install nilscript[cli]` fails on `pyyaml` alone. Fix: make the SDK import lazy so
   the CLI is truly dependency-light. **Must be fixed before any PyPI claim of a light CLI install**
   (also flagged in the content plan §7).
2. **`scan` live `--url` probing is not wired** — only `--replay FILE` (deterministic). Live probing
   needs the `--safe` sandbox/teardown design from adapter-toolkit-plan §8 before it touches a real
   system.
3. **`conformance-test` CLI** requires a running shim; the runner core is fully unit-tested via fake
   probes, but no integration test boots a real shim over HTTP.

---

## What's next (not started)

- **`docs/saas-grade-content-plan.md`** — staged plan to bring repo / PyPI / docs site / landing to
  SaaS-grade. Decisions locked: all four surfaces, **stage-for-review only** (no live publish),
  positioned as open standard + toolkit. Key lever: `nilscript-landing/` already holds ~30k words of
  on-message narrative — Phases 4–5 are *assembly + design + fact-check*, not authoring (see §2.5
  routing table). **First step:** write `docs/brand/messaging.md`, choose docs/landing tooling,
  rewrite the README, stand up CI.
- Fix tech-debt item #1 before the PyPI phase.

---

## Review / merge

- Open a PR from `feat/adapter-toolkit-mvp` → `main`. The branch carries both plan docs + the full
  toolkit. CI does not exist yet (it's a content-plan Phase-2 deliverable) — run `pytest` locally.
- A `python-reviewer` pass already ran on the MVP; its 7 findings (1 critical path-traversal, 4 high,
  2 medium) were all fixed with tests. Re-review Phases 3–6 + the repair/memory modules if desired.

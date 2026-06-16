# NILScript — SaaS-Grade Content & Presence Plan

> **Status:** plan / roadmap. No content is shipped or published from this document.
> **Scope:** bring every public surface of `nilscript` to top-tier developer-tool quality —
> **GitHub repo**, **PyPI listing**, a hosted **docs site**, and a **landing page**.
> **Decisions (locked with the requester):**
> 1. **Surfaces:** all four (repo · PyPI · docs site · landing).
> 2. **Go-live:** **stage for review only** — produce everything in-repo/preview; nothing is
>    published to live PyPI and the GitHub repo is not flipped public until explicit sign-off.
> 3. **Positioning:** **open standard + toolkit** — a neutral protocol (in the lineage of OpenAPI,
>    JSON Schema, MCP) with a reference CLI/SDK. Community-first, spec-led. **No SaaS/pricing story.**
> **"SaaS-grade" here means the production-quality bar of a top dev-tool's OSS presence — not a
> hosted product.**

---

## 0. Guardrails (read first)

- **Spec neutrality is sacred.** Content sells the *standard*, never a vendor. Examples may use
  ERPNext/Salla/Shopify as *adapters*, never as "the way NILScript works."
- **Honesty over hype.** The standard has one completed live proof (a real customer + invoice into
  ERPNext via the agent) and **no merchant adoption yet** (see the adapter-toolkit plan §8). Content
  must not imply traction that doesn't exist. The story is "neutral standard + proven reference
  path," not "battle-tested at scale."
- **Single source of truth.** The NIL spec already lives in `src/nilscript/nil/versions/*`. Docs and
  landing **render/quote** it; they never fork a second canonical copy that can drift.
- **Stage, don't ship.** Every deliverable lands behind a review gate (§10). No `twine upload`, no
  "make public," no DNS, without a human green-light.

---

## 1. Current-state audit (ground truth)

| Surface | Today | Gap to SaaS-grade |
|---|---|---|
| **README** | 66 lines | Thin. No hero, no badges, no "why," no quickstart-in-30-seconds, no visuals, no command tour. |
| **Repo meta** | ✅ CONTRIBUTING, GOVERNANCE, SECURITY, CODE_OF_CONDUCT, CHANGELOG, VERSIONING, MAINTAINERS, IMPLEMENTATIONS | Good bones. Need polish + cross-links + a CITATION.cff and a real `.github/`. |
| **`.github/`** | ❌ absent | No CI, no issue/PR templates, no `FUNDING`, no social-preview, no release automation, no labels. |
| **`docs/`** | 1 file (adapter-toolkit-plan) | No published docs site; no guides, no API reference, no spec rendering. |
| **PyPI** | metadata in `pyproject.toml`; `dist/` exists | Long-description rendering unverified; classifiers/keywords thin; no project URLs map; no release checklist. |
| **Landing** | `nilscript-landing/` markdown (audiences, narrative, proof, reference, convert) — **unbuilt** | Rich raw content exists; no built site, no design system, no deploy. |
| **Bundled spec** | `nil/versions/0.1.0.md`, `0.2.0.md`, conformance checklist, shim guide | Canonical and good — the docs site should surface these, not rewrite them. |

**Takeaway:** the *substance* is unusually strong (spec, SDK, CLI toolkit, conformance proof, raw
landing narrative). The deficit is **presentation and packaging**, not content depth. This plan is
mostly assembly + polish + design, not net-new invention.

---

## 2. The messaging spine (one narrative, reused everywhere)

Everything below draws from one spine so the README, PyPI blurb, docs hero, and landing hero never
contradict each other.

- **One-liner:** *NILScript is the neutral standard for letting agents act in real systems —
  safely, with confirmation, and without bespoke glue per backend.*
- **The problem:** every "agent + your system" integration is re-built from scratch; the friction is
  *discovery* (a backend's hidden, undocumented requirements) and *safety* (an agent must not write
  blindly).
- **The insight:** separate the neutral **intent layer** (verbs, envelopes, confirmation, the five
  endpoints) from **backend reality** (captured once in a shareable manifest). Build an adapter once;
  scan a system once; the world shares the result.
- **The proof:** a real customer + invoice executed through the conversational gateway into a live
  ERPNext, from the standard alone.
- **Three pillars (used as the landing/docs triad):**
  1. **Neutral by design** — no backend specifics in the standard (like OpenAPI for APIs).
  2. **Safe by contract** — no commit without confirmation; PROPOSE has no side effects.
  3. **De-frictioned by tooling** — `scan` once, generate an adapter, share the manifest.
- **Voice:** precise, engineer-to-engineer, low-hype. Comparables to invoke: OpenAPI, JSON Schema,
  MCP, Stripe-doc clarity. Banned words: "revolutionary," "seamless," "magical."

**Deliverable:** `docs/brand/messaging.md` — the canonical spine + a glossary + the do/don't word
list. Every other surface cites it.

---

## 2.5 Existing content inventory & routing (reuse, don't rewrite)

`nilscript-landing/` already holds **~30,000 words** of strong, on-message narrative. This is the
biggest lever in the whole plan: most "content work" is **routing + light editing + design**, not
authoring. The rule is **one source per fact** — a piece lives in *either* the docs *or* the landing
as canonical, and the other surface links to it (never a forked copy).

| Existing file (`nilscript-landing/`) | Words | Canonical home | Role |
|---|---|---|---|
| `index.md` | hero | **Landing** | the one-page story (hero, problem, standard) |
| `narrative/manifesto.md` · `problem.md` · `solution.md` | — | **Landing** | the "why" — landing sections + a docs *Concepts → Why NIL* intro |
| `how-it-works/overview.md` ("Tri-Layer Machine") | 2044 | **Docs → Concepts** | core mental model; landing links to it |
| `how-it-works/nil-protocol.md` | — | **Docs → Concepts** | the five endpoints / envelopes |
| `how-it-works/nilscript-dsl.md` | — | **Docs → Concepts** | the DSL (cross-link to `dsl/*` spec) |
| `how-it-works/safety-model.md` ("Safe by Construction") | 1969 | **Docs → Concepts** | the confirmation invariant — load-bearing |
| `reference/glossary.md` | 1382 | **Docs → Reference** | glossary (also feeds `brand/messaging.md` term list) |
| `reference/comparison.md` (vs MCP/OpenAPI/...) | 902 | **Docs → Reference** + **Landing** "Open & neutral" | positioning proof |
| `reference/spec-at-a-glance.md` | 956 | **Docs → Spec** intro | TL;DR over `nil/versions/*` (links, no fork) |
| `reference/faq.md` | — | **Docs → Reference** | FAQ |
| `proof/erpnext-adapter-proof.md` | 1566 | **Docs → Examples** + **Landing** "Proof" | the live proof, narrated |
| `proof/conformance.md` | — | **Docs → Guides → conformance** | pairs with `conformance-test` CLI |
| `proof/calibration-18-platforms.md` | — | **Landing** "Proof" + **Docs → Reference** | breadth evidence |
| `convert/get-started.md` | 1141 | **Docs → Get started** | the quickstart spine (reconcile with real CLI commands) |
| `convert/use-cases.md` | — | **Landing** "For whom" + **Docs → Examples** | use cases |
| `audiences/for-{developers,ai-labs,platform-vendors,decision-makers}.md` | — | **Landing** "For whom" (tabs) | audience routing |
| `README.md` (landing) | — | — | internal map; superseded by this routing table |

**Editing pass required before reuse (not a rewrite):**
- Reconcile every command/snippet with the **real CLI** (`verbs`/`scaffold-shim`/`scan`/
  `conformance-test`/`manifest`) — the prose predates the shipped commands; make examples copy-paste
  true.
- Apply the §0 honesty guardrail: soften any line implying adoption/scale beyond the one live proof.
- Convert relative landing links to the new docs/landing URL scheme.
- Lift the glossary + comparison into the canonical docs locations; landing references them.

**Net effect on the roadmap:** Phase 4 (docs) and Phase 5 (landing) become **assembly + design +
fact-check** over existing prose, not authoring — a large de-risk. The authoring that *remains* is
narrow: the CLI reference (generated), the SDK reference, and the per-command guides.

---

## 3. Surface 1 — GitHub repository content

### 3.1 README (the most-read artifact)
A staged, scannable README with:
- **Hero block:** logo/wordmark, one-liner, badges row (CI, PyPI version, Python versions, license,
  spec version, "conformance: passing"), and an animated/asciinema **30-second demo** (scaffold →
  scan → manifest).
- **"Why" in 5 bullets**, then a **copy-paste quickstart** (install → `nilscript verbs` → scaffold →
  scan), then a **command tour table** (the 7 commands), then **architecture diagram** (Mermaid: the
  five endpoints + edge/translate/manifest), then **"Where it stands"** (honest status), then links
  out to docs/landing/spec, then **contributing/community** footer.
- **Visuals:** a Mermaid data-flow diagram, the CLI banner screenshot, and the asciinema cast.

### 3.2 `docs/` tree (in-repo source for the docs site, §5)
```
docs/
  index.md                      # docs home (mirrors site root)
  quickstart.md
  concepts/{intent-layer,verbs,envelopes,confirmation,manifest}.md
  guides/{build-an-adapter,scan-a-system,run-conformance,publish-a-manifest}.md
  cli/{overview,scaffold-shim,scan,conformance-test,manifest}.md   # one per command
  spec/  -> rendered from src/nilscript/nil/versions/* (no fork)
  sdk/   -> reference SDK usage
  examples/erpnext-walkthrough.md   # the live proof, narrated
  brand/{messaging.md,visual-identity.md}
  adapter-toolkit-plan.md       # (existing)
  saas-grade-content-plan.md    # (this file)
```

### 3.3 `examples/` (runnable, not prose)
- `examples/erpnext/` — the reference adapter, trimmed to a teaching example.
- `examples/minimal-shim/` — `scaffold-shim` output with one filled verb, as a "hello world."
- Each example has its own README + a `make demo` that runs against `FakeSystem` (no live backend).

### 3.4 `.github/` (the missing operational layer)
- **CI** (`workflows/ci.yml`): matrix Python 3.12/3.13 → `pytest` (the 120 tests) + `ruff` + `mypy`
  + build sdist/wheel + `twine check`. Required status check.
- **Docs build** (`workflows/docs.yml`): build the docs site on PRs, preview deploy (staged).
- **Release** (`workflows/release.yml`): tag → build → `twine check` → **draft** GitHub release +
  **TestPyPI** upload (never live PyPI without manual approval — §10).
- **Templates:** issue forms (bug / spec-question / adapter-request), PR template, `FUNDING.yml`,
  `CODEOWNERS`, label set, `dependabot.yml`.
- **Social preview** image (1280×640) + repo description + topics (`nil`, `agents`, `standard`,
  `openapi`, `mcp`, `erp`, `protocol`).
- **CITATION.cff** (it's a standard — make it citable).

### 3.5 Badges & shields
CI · PyPI version · downloads · Python versions · license (Apache-2.0 + CC-BY-4.0) · spec v0.2 ·
"tests: 120 passing" · docs link · Discord/Discussions.

**DoD (repo):** a newcomer lands on the README and within 60 seconds understands what NIL is, sees it
work, and can run `pip install` + first command. CI is green and required. `.github/` is complete.

---

## 4. Surface 2 — PyPI listing

- **Long description:** README renders cleanly on PyPI (relative links → absolute; no unsupported
  HTML; `twine check` passes). Possibly a PyPI-specific trimmed README via `readme` content-type.
- **Metadata:** full `classifiers` (Dev Status, Intended Audience :: Developers, Topic :: Software
  Development :: Libraries, License, Python 3.12/3.13, Typed), rich `keywords`, complete
  `[project.urls]` (Homepage, Docs, Source, Changelog, Issues, Spec).
- **Extras clarity:** document `nilscript[cli]`, `[sdk]`, `[dev]` on PyPI so installers pick the right
  one (note the current `[cli]`→SDK import coupling as a known item to fix before release; see §7).
- **Naming/ownership:** confirm `nilscript` name ownership on **TestPyPI first**; reserve before any
  marketing points at it.
- **Release flow:** `VERSIONING.md`-aligned; tag → CI build → `twine check` → **TestPyPI** →
  smoke-install from TestPyPI → (gated) live. **Live upload is out of scope until sign-off.**

**DoD (PyPI):** the package page looks like a top-tier library (rendered README with visuals, correct
metadata, working URLs), validated on **TestPyPI** — not live.

---

## 5. Surface 3 — Docs site

> **Most of these pages already exist as prose** — see the routing table in §2.5. The docs site is
> largely *assembling* `how-it-works/*`, `reference/*`, and `proof/*` into IA + design, plus the
> narrow authoring noted there (CLI ref, SDK ref, per-command guides).

- **Tooling:** **Mintlify** or **Docusaurus** (recommend Mintlify for fastest SaaS-grade polish +
  built-in search/versions; Docusaurus if we want full self-host control). Decide in Phase 1.
- **Information architecture:**
  - *Get started* (install, 30-sec quickstart, first adapter)
  - *Concepts* (intent layer, verbs, envelopes, confirmation invariant, the manifest)
  - *Guides* (build an adapter, scan a system, run conformance, publish a manifest, the repair loop)
  - *CLI reference* (one page per command — generated from `--help`/argparse to avoid drift)
  - *SDK reference* (the Python SDK)
  - *The NIL spec* (rendered from `nil/versions/*`, versioned 0.1/0.2)
  - *Examples* (ERPNext walkthrough = the live proof, narrated end-to-end)
- **Generated, not hand-copied:** CLI reference and OpenAPI come from the tools (`export-openapi`,
  argparse) so docs can't drift from the code.
- **Versioning:** docs versioned per spec version; a banner for "spec v0.2 (current)."
- **Search + analytics + OG tags** for every page.
- **Hosting:** preview deploys on PRs; production behind the review gate (custom domain staged, not
  switched live until sign-off).

**DoD (docs):** a developer can go install → first working adapter using only the site; every CLI
command and every spec concept has a page; nothing is hand-duplicated from source.

---

## 6. Surface 4 — Landing page

The raw narrative already exists in `nilscript-landing/` — **~30k words**, mapped to sections in the
§2.5 routing table (`index.md` hero, `narrative/*`, `proof/*`, `audiences/*`, `convert/use-cases`).
This phase **designs and builds** it — no new story invention, just the fact-check editing pass (§2.5)
+ visual design.

- **Visual direction (pick one, intentionally — not "clean minimal"):** *Swiss/International technical*
  — strong typographic hierarchy, a disciplined grid, one assertive accent, monospace for code, a
  subtle blueprint/schematic motif evoking "a standard/protocol." (Alt: dark technical with a
  terminal-forward hero.) Decide in Phase 1 against 2–3 references.
- **Sections:**
  1. **Hero** — one-liner + a live terminal demo (the banner + `scan` reproducing real findings) +
     two CTAs (Read the docs · Star on GitHub).
  2. **The problem** (every integration rebuilt from scratch).
  3. **The three pillars** (neutral / safe / de-frictioned) as a bento or stepped layout.
  4. **How it works** — the five endpoints + the PROPOSE→CONFIRM→COMMIT flow, animated.
  5. **The proof** — the ERPNext live story, with the actual collisions → manifest.
  6. **For whom** — the audiences from `nilscript-landing/audiences`.
  7. **Open & neutral** — governance, license, "like OpenAPI for agent-actions."
  8. **CTA footer** — docs, GitHub, spec, community.
- **Tech:** a static framework (Astro recommended — content-first, fast, MD-friendly so it can pull
  from the existing markdown) or Next.js if richer interactivity is wanted. Strict perf budget
  (LCP < 2.5s, JS < 150kb landing).
- **Performance & a11y:** Core Web Vitals targets, reduced-motion support, semantic HTML, real
  hover/focus states, OG/Twitter cards.

**DoD (landing):** a believable product-grade landing page that a skeptical senior engineer would
take seriously; tells the true story; passes Lighthouse perf/a11y; deploy-ready behind the gate.

---

## 7. Cross-cutting workstreams

- **Visual identity:** wordmark/logo (the ASCII banner already hints at a mark), color tokens (one
  accent), type pairing (a technical sans + a monospace), an icon for the manifest/scan concepts,
  diagram style. Deliverable: `docs/brand/visual-identity.md` + asset set (SVG logo, social card,
  favicon, OG images).
- **Content reuse:** spec rendered from `nil/versions/*`; CLI ref generated from argparse; landing
  copy pulled from `nilscript-landing/*`. One source per fact.
- **SEO/OG:** titles, descriptions, OG images per surface; a `sitemap`; canonical links between
  README ↔ docs ↔ landing.
- **Known pre-release fix (flagged, not done here):** `nilscript[cli]` currently fails without
  `pydantic` because `nilscript/__init__.py` imports the SDK eagerly. Before PyPI marketing claims a
  light CLI install, make SDK import lazy so `[cli]` works on `pyyaml` alone. (Separate engineering
  task; called out so the content doesn't promise something the package doesn't deliver.)
- **Analytics & community:** privacy-respecting analytics (Plausible) on docs/landing; GitHub
  Discussions enabled; a Discord/Matrix link; a "good first issue" backlog.

---

## 8. Information architecture (how the surfaces interlock)

```
                 ┌─────────────┐
   search/social │  Landing    │  story + proof + CTAs
        ─────────▶ (Astro)     ├───────────────┐
                 └─────┬───────┘               │
                       │ "Read the docs"       │ "Star / source"
                       ▼                       ▼
                 ┌─────────────┐         ┌─────────────┐
                 │  Docs site  │◀────────│  GitHub repo│  README = condensed docs
                 │ (Mintlify)  │  source │  + .github  │  + examples + spec source
                 └─────┬───────┘         └─────┬───────┘
                       │ renders               │ ships
                       ▼                       ▼
                 ┌───────────────────────────────────┐
                 │  Canonical sources (no forks):     │
                 │  nil/versions/*  ·  argparse help  │
                 │  export-openapi  ·  landing/*.md   │
                 └───────────────────────────────────┘
                       │  pip install
                       ▼
                 ┌─────────────┐
                 │  PyPI page  │  rendered README + metadata + URLs back to docs
                 └─────────────┘
```

---

## 9. Phased roadmap (gated)

| Phase | Deliverable | DoD |
|---|---|---|
| **1 — Foundations** | messaging spine, visual identity, tool choices (docs + landing framework), name reserved on **TestPyPI** | `docs/brand/*` written; logo/colors/type chosen; Mintlify-vs-Docusaurus and Astro-vs-Next decided against references |
| **2 — Repo polish** | README rewrite, `.github/` (CI + templates + social preview), badges, CITATION.cff, examples/ | CI green + required; README hero+quickstart+command tour live; examples run on FakeSystem |
| **3 — PyPI-ready** | metadata + classifiers + URLs; README renders on PyPI; **TestPyPI** release dry-run | `twine check` passes; TestPyPI page looks top-tier; smoke-install works; **not live** |
| **4 — Docs site** | assemble existing prose (§2.5) into IA, generate CLI/OpenAPI ref, render spec, author the narrow gaps (CLI/SDK ref, per-command guides), preview-deploy | a dev can build an adapter from the site alone; zero hand-duplicated source; every reused page fact-checked against the real CLI |
| **5 — Landing** | design + build landing by routing `nilscript-landing/*` (§2.5) through the chosen visual direction; perf/a11y-passing; preview-deployed | believable, true, Lighthouse-green, deploy-ready behind gate; copy reconciled with shipped commands |
| **6 — Launch readiness (no launch)** | cross-links, SEO/OG, final review packet | one checklist of "flip-to-live" steps awaiting **explicit sign-off**; nothing published |

**Critical path / parallelism:** Phase 1 gates everything. Then **Repo (2)** and **Docs (4)** share
source and should run together; **Landing (5)** can start design in parallel once identity (1) is set;
**PyPI (3)** is small and slots after Repo. Phase 6 is assembly only.

---

## 10. Staging & review gates (because go-live is out of scope)

- **Nothing publishes.** No live PyPI upload, no public-repo flip, no production DNS — all of these
  are explicit, separate, human-approved steps collected in a single **"flip-to-live" checklist**
  produced in Phase 6.
- **Everything previews.** Docs and landing use preview deploys (PR previews / a staging subdomain);
  PyPI uses **TestPyPI**; the repo stays private/internal until sign-off.
- **Secrets/sanitization gate** before any "public" action: run the opensource-sanitizer pass (no
  secrets, no instance values, no internal hostnames) — reuse the structural/instance discipline the
  toolkit already enforces.
- **Review packet:** each phase ends with a short "what changed + where to look + screenshots/preview
  links" note for the requester to approve before the next phase.

---

## 11. Honest caveats

- **Presentation can outrun adoption.** SaaS-grade content around a standard no merchant has adopted
  risks looking like vaporware to a sharp reader. Mitigation: lead with the *real* proof, frame as a
  young open standard (v0.2), and avoid traction claims. (Mirrors adapter-toolkit-plan §8.)
- **Maintenance burden is real.** Four surfaces + generated docs + CI = ongoing upkeep. Keep
  generation automated (CLI ref, OpenAPI, spec render) so content can't rot; resist hand-copied prose.
- **Don't gold-plate the landing before the docs exist.** Docs are the load-bearing surface for a
  dev tool; the landing converts, the docs retain. Sequence accordingly (Phase 4 before 5's polish).
- **The `[cli]`/pydantic coupling (§7) must be fixed before PyPI claims a light install** — otherwise
  the content promises what the package doesn't ship.

---

## 12. First concrete step (when execution resumes)

1. Write `docs/brand/messaging.md` + `docs/brand/visual-identity.md` (Phase 1) — the spine every
   other surface depends on.
2. Decide docs tool (Mintlify vs Docusaurus) and landing framework (Astro vs Next) against 2–3
   references each.
3. Rewrite the README (Phase 2) as the first visible win, and stand up CI so quality is enforced from
   the start.

*This is a plan, not an implementation. Each phase is a gated body of work that ends in a preview +
review packet; nothing goes live without explicit sign-off (§10).*

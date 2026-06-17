# Benchmarking NIL against the world's elite agent tests — a plan for publishable proof

> **Status:** plan (no code yet). **Goal:** numbers we can publish *next to* known leaderboards.
> **Scope:** all four axes — agent task-success, safety/adversarial, protocol conformance, systems
> performance. Sourcing below is graded: axes 1–2 rest on peer-reviewed agent benchmarks; axes 3–4
> rest on established software-engineering rigor (no agent leaderboard covers them — see §7).

## 0. The strategy in one breath

NIL is not a model — it's the **layer between the model and the backend**. So we don't compete on a
leaderboard; we **instrument** the leaderboards. The credible story is a **controlled A/B**: the
*same* agent + *same* model, run **raw-API** vs **NIL-gated** (propose→approve→commit→rollback,
skeleton-bounded), on **established benchmarks**, reporting the **delta**. Task-success should hold
roughly constant (NIL doesn't make the agent dumber); **harmful/unauthorized writes should collapse
toward zero** (NIL's whole thesis). That delta — measured on benchmarks reviewers already trust — is
the publishable proof. Reporting NIL's own conformance/latency numbers *alone* is not.

The four axes and what each is for:

| Axis | Elite anchor(s) | What it proves for NIL | Sourcing |
|---|---|---|---|
| 1 · Task-success | **τ-bench/τ²-bench**, **BFCL v3/v4** | NIL doesn't degrade what the agent can accomplish | peer-reviewed (ICLR'25, ICML'25) |
| 2 · Safety/adversarial | **AgentDojo**, **InjecAgent**, **ToolEmu** | approve+rollback+preflight measurably kills bad writes | peer-reviewed (NeurIPS'24, ACL'24, ICLR'24) |
| 3 · Conformance/reliability | **pass^k** + property/contract testing (Hypothesis, Jepsen-style, Pact) | the protocol itself is correct under retries/partial failure | SE practice (no agent leaderboard) |
| 4 · Systems performance | wrk2 / k6, **coordinated-omission-correct** p50/p99, SPEC cloud method | the gate's overhead is small and honestly measured | SE practice |

---

## 1. Axis 1 — Agent task-success (does NIL keep the agent capable?)

### 1a. τ-bench / τ²-bench — the primary citable anchor
- **What it is:** a benchmark emulating dynamic multi-turn conversations between an LLM-**simulated
  user** and an agent given **domain API tools + policy docs**; it scores by comparing the
  **database state at end-of-conversation against an annotated goal state**. τ²-bench extends this to
  **dual-control** (a Dec-POMDP where *both* user and agent act on a shared system). Ships two
  domains: **retail** and **airline**. (arXiv 2406.12045, ICLR 2025; sierra-research/tau-bench;
  τ² arXiv 2506.07982.)
- **Why it fits NIL exactly:** "compare end DB state to goal state" *is* NIL's model — propose→commit
  mutate real backend state, and success = the backend ended where it should. This is the closest
  public analog to what NIL does.
- **Headline metric — adopt as-is: `pass^k`.** The probability that **all k** i.i.d. trials of a task
  succeed (`pass^k = (pass^1)^k` under independence), averaged over tasks — a **reliability** metric,
  not single-run success. It decays sharply (SOTA agents <50% task success, **pass^8 < 25%** in
  retail). ⚠️ **Cite `pass^k`, not `pass@k`** — the claim that τ²-bench uses `pass@k` was **refuted**
  in our verification (0-3). pass^k is the reliability number; pass@k (optimistic "any of k") is a
  different, easier metric.
- **Citable baselines to report next to:** the official repo lists e.g. Claude-3.5-Sonnet
  (tool-calling) **Pass^1 = 0.460 airline / 0.692 retail**. ⚠️ Pin the **exact model snapshot +
  benchmark commit** — these age fast.
- **How we adapt it — the NIL↔τ-bench bridge:** implement a τ-bench "domain" whose tool layer is a
  **NIL adapter** (our FakeSystem or a PocketBase shim seeded with the retail/airline schema). The
  agent's tool calls route as NIL verbs (propose→approve→commit); reuse τ-bench's DB-state goal
  comparator unchanged. Run **raw tools vs NIL-gated** under identical seeds → report pass^1…pass^k
  for both. Effort: **medium-high** (one environment bridge + schema seeding).

### 1b. BFCL v3/v4 (Berkeley Function-Calling Leaderboard) — the schema-validation anchor
- **What it is:** the leading **executable** function-calling eval (ICML 2025). v1 covers
  **simple/parallel/multiple** calls; **v3 adds multi-turn/multi-step** with **state-based scoring**
  (does internal system state match expected after a dialogue?); v4 adds "agentic". Scoring uses a
  novel **AST evaluation** — parse the call, extract args, check against ground-truth answers —
  scaling to thousands of functions without execution.
- **Why it fits NIL:** BFCL consumes **OpenAI-style JSON function schemas**. We already ship
  **`nilscript export-openapi`** — so every NIL verb (`commerce.create_product`,
  `services.create_invoice`, …) exports straight into BFCL's format. AST arg/signature validation is
  precisely "did the agent call the verb with a well-formed, in-schema payload" — which is what NIL
  enforces at PROPOSE.
- **How we adapt it:** export the verb catalog → BFCL function set; run the agent; score with BFCL's
  AST evaluator. Use **v3 multi-turn** to mirror propose→approve→commit sequences. Effort: **low-
  medium** (mostly a schema export + harness wiring).
- **⚠️ Do NOT publish BFCL alone.** Verified, load-bearing pitfall: *"a high BFCL score is necessary
  but not sufficient,"* and **simple/multiple categories are saturated past the noise ceiling**
  (Databricks; BFCL maintainers responded with v3/v4). Target the **hard categories** (v3 multi-turn,
  relevance detection, v4 agentic) and always pair with τ-bench + the safety axis.

**Others (lower priority):** AgentBench, ToolBench/StableToolBench, WebArena/WorkArena, API-Bank,
NexusRaven — broader/older or web-GUI-centric; useful as *secondary* breadth once τ-bench + BFCL land.
WebArena is browser-driven (less aligned to NIL's API-operation model).

---

## 2. Axis 2 — Safety & adversarial robustness (NIL's core differentiator)

This is where NIL should **win visibly**. All three anchors target the **unauthorized/hijacked-write**
threat that approve+rollback+preflight is built to neutralize.

### 2a. AgentDojo — the primary adversarial anchor
- **What it is:** an **extensible** evaluation framework (not a static suite) for agents executing
  tools over **untrusted data**; **97 realistic tasks** (email, e-banking, travel) + **629 security
  test cases** with attack/defense paradigms. **Threat model:** prompt injection where **data returned
  by a tool hijacks the agent into executing a malicious task**. (NeurIPS 2024, ETH Zürich; arXiv
  2406.13352.)
- **Why it fits NIL:** the malicious task is almost always a **write** (transfer funds, send email,
  book). NIL's defense is structural: the hijacked write still surfaces as a **PROPOSE preview the
  human must approve**, is **skeleton-bounded** (an unprovisioned/unknown verb is **refused, not
  faked**), and is **reversible**. AgentDojo is the venue to *measure* that.
- **How we adapt it — the AgentDojo↔NIL adapter:** wrap AgentDojo's tool suites behind NIL verbs;
  run its attack battery in two arms — **(A) raw agent** vs **(B) NIL-gated** (auto-approve OFF =
  human/oracle gate; preflight ON). Report **attack-success-rate (ASR) reduction** A→B.

### 2b. InjecAgent — the citable baseline ASR
- **What it is:** 1,054 cases, 17 user tools × 62 attacker tools; indirect prompt injection split into
  **direct-harm** and **private-data-exfiltration**; 30 agents evaluated. **ReAct GPT-4 is vulnerable
  ~24%** of the time (base setting). (ACL Findings 2024; arXiv 2403.02691.)
- **Use:** report NIL-gated ASR against the **24% base** baseline. ⚠️ Cite **24% (base)**, not the
  "enhanced/reinforced-prompt" setting (≈2×) — credibility.

### 2c. ToolEmu — scalable risk testing without per-backend sandboxes
- **What it is:** uses an **LM to emulate tool execution** so you can red-team high-stakes scenarios
  (36 tools, 144 cases) **without** implementing every backend. (ICLR 2024 spotlight; arXiv 2309.15817.)
- **Use:** breadth of high-stakes write scenarios beyond what FakeSystem/PocketBase cover cheaply.
  ⚠️ **Do not cite a precise emulator-fidelity number** — the "68.8% agreement" figure was **refuted**
  (1-2). Treat LM-emulated results as indicative, validate the headline ones on a real shim.

### 2d. The NIL-native metric — define it so reviewers can't call it a tautology
Name: **Unauthorized-Write Rate (UWR)** and **Hallucinated-Verb Rate (HVR)**.
- **UWR** = fraction of attack episodes that result in a **committed** state-changing effect that was
  *not* in the user's authorized intent. NIL's claim: **UWR → 0 by construction** because no write
  commits without an approved PROPOSE.
- **HVR** = fraction of agent verb-attempts that reference a verb/target **not in the backend
  skeleton** — which NIL **refuses** (UPSTREAM_UNAVAILABLE / UNKNOWN_VERB) rather than fabricating.
- **The anti-tautology discipline (critical, from a 2-1 split finding):** BFCL's own **Relevance/
  Irrelevance Detection** category is **gameable in isolation** — *a model that never calls any
  function scores 100%*. So **never report UWR/HVR alone**. Always report the **pair**: `(task-success
  on benign tasks, UWR/HVR under attack)`. The defensible claim is *"NIL drives UWR→0 **while holding
  benign task-success ≈ the raw-API arm**"* — i.e. it's not just refusing everything. The control
  arm (raw-API, same model/seed) is what makes the delta non-trivial.

---

## 3. Axis 3 — Protocol conformance & reliability (correctness of the wire)

⚠️ **No agent leaderboard covers this.** The credible anchors are **software-engineering rigor**,
which is *fine* — these are the same tools databases and payment systems publish against.

- **`pass^k` for reliability (borrowed from τ-bench, §1a):** run each conformance scenario k times;
  report the probability all k pass. This turns our existing **conformance-test** matrix (17/17
  offline + describe; live 11/11) into a **reliability** number, not a single-run pass.
- **Property-based / stateful testing — Hypothesis `RuleBasedStateMachine`** (hypothesis.readthedocs
  .io/en/latest/stateful.html): generate random valid **sequences** of propose/commit/rollback and
  assert invariants:
  - **Idempotency:** replaying a COMMIT with the same idempotency key never double-writes.
  - **Rollback honesty:** every effect's declared reversibility matches what ROLLBACK actually does;
    IRREVERSIBLE is refused, not silently executed; a compensation targets the **real record id**
    (the bug we just fixed — this becomes a permanent property).
  - **Refusal correctness:** unknown verb → UNKNOWN_VERB; unprovisioned target → UPSTREAM_UNAVAILABLE;
    bad args → INVALID_ARGS. Never a 500.
  - **No-side-effect-on-PROPOSE:** state is byte-identical before/after any PROPOSE.
- **Saga / partial-failure semantics — Jepsen-style fault injection** (jepsen.io): kill the backend
  mid-COMMIT, drop the EVENT, partition the network; assert the ledger + compensation tokens leave the
  system recoverable (no orphaned writes, no lost reversal handle).
- **Contract testing — Pact** (docs.pact.io): pin the NIL wire as a **consumer-driven contract** so
  the SDK and every adapter are verified against the same envelope/endpoint contract in CI — the
  machine-checkable complement to the human-readable spec.

Publish as: **conformance pass^k**, **N invariants property-tested across M generated sequences**,
**fault-injection survival rate**.

---

## 4. Axis 4 — Systems performance (honest overhead of the gate)

⚠️ Again SE practice, not an agent leaderboard. The single most important credibility rule here:

- **Measure with a coordinated-omission-correct tool — `wrk2` or `k6`** (giltene/wrk2;
  scylladb.com/2021/04/22/on-coordinated-omission). Naïve closed-loop load tools **hide tail latency**
  by waiting for slow responses before sending the next request; reviewers who know this will dismiss
  uncorrected p99s. Use a **constant-throughput / open-model** generator.
- **Report:** **p50/p90/p99/p99.9** (not just mean), **throughput**, and the **NIL overhead delta** =
  (NIL-gated latency − raw-API latency) for the same operation, across concurrency levels. Include
  **circuit-breaker** behavior under induced backend failure (does the breaker shed load cleanly?).
- **Methodology rigor — SPEC cloud reproducibility principles** (research.spec.org … Reproducible
  Performance Evaluation): warm-up, repetition, environment disclosure, confidence intervals. A
  gateway-style harness (github.com/howardjohn/gateway-api-bench) is a good shape to copy.
- **The honest framing:** NIL adds a propose-store + an approval round-trip. Don't hide it — **quantify
  it** and show it's small relative to LLM latency (the agent's model call dwarfs the gate). That's a
  *winning* story told honestly.

---

## 5. Harness architecture (one design, four axes)

```
bench/                                   # new repo or nilscript/bench/
├── core/
│   ├── nil_tool_bridge.py     # maps a benchmark's "tool call" → NIL propose/approve/commit
│   ├── arms.py                # ARM_RAW (direct API) vs ARM_NIL (gated) — identical agent/model/seed
│   ├── gate.py                # approval oracle: auto-approve | policy | human-in-loop
│   └── report.py              # pass^k, ASR, UWR/HVR, p50/p99 — one schema, JSON + markdown
├── task_success/
│   ├── tau_bridge/            # τ-bench domain backed by a NIL adapter (retail/airline schema)
│   └── bfcl_export/           # nilscript export-openapi → BFCL function set + AST runner
├── safety/
│   ├── agentdojo_adapter/     # AgentDojo tool suites behind NIL verbs; A/B attack battery
│   ├── injecagent_runner/     # 1,054 cases; ASR vs 24% base
│   └── toolemu_runner/        # LM-emulated high-stakes writes (indicative)
├── conformance/
│   ├── stateful_machine.py    # Hypothesis RuleBasedStateMachine over propose/commit/rollback
│   ├── faults.py              # Jepsen-style mid-commit kills / partitions
│   └── pact/                  # consumer-driven wire contract
└── perf/
    └── load.py                # wrk2/k6 driver; coordinated-omission-correct; breaker scenarios
```

Key design choice: **everything is two arms** (raw vs NIL-gated) sharing the agent, model snapshot,
and RNG seed. The benchmark is the *environment*; NIL is the *treatment*. The delta is the result.

---

## 6. Sequencing & effort

| Phase | Deliverable | Effort | Why this order |
|---|---|---|---|
| **P0** | A/B harness skeleton (`core/`) + report schema | S | Everything depends on the two-arm + metric plumbing |
| **P1** | **BFCL export + AST runner** (`bfcl_export`) | S–M | Cheapest credible number; reuses `export-openapi`; validates the verb schemas |
| **P2** | **Conformance pass^k + Hypothesis stateful** | M | Turns our existing 17/17 + the rollback-id fix into a published reliability story; no external bench needed |
| **P3** | **AgentDojo + InjecAgent NIL adapters** (the headline) | M–H | The differentiator: UWR→0 with task-success held — the number that sells NIL |
| **P4** | **τ-bench↔NIL bridge** (retail/airline on a NIL shim) | H | Most rigorous task-success; report pass^k next to public baselines |
| **P5** | **Perf (wrk2/k6, CO-correct) + ToolEmu breadth** | M | Overhead honesty + scenario breadth, once the story exists |
| **P6** | **Write-up** + reproducibility pack (seeds, commits, model snapshots) | M | The publishable artifact |

Start P1+P2 in parallel (both small, both self-contained), then P3 (the win), then P4 (the rigor).

---

## 7. Credibility pitfalls — the things reviewers will attack (verified)

1. **Don't publish BFCL alone.** *"Necessary but not sufficient"*; simple/multiple are saturated past
   the noise ceiling. Pair it; target hard categories; pin the **exact BFCL version** (v3 multi-turn
   current; v4 agentic Jul 2025; `bfcl-eval` on PyPI). (Databricks; BFCL maintainers.)
2. **`pass^k`, not `pass@k`.** They're different metrics; conflating them is a tell. τ²-bench using
   `pass@k` was **refuted** in our own verification.
3. **Never report UWR/HVR alone** — a never-act agent scores "perfectly safe." Always pair with benign
   task-success vs the raw-API control arm, or it reads as a tautology of the design.
4. **Coordinated omission** will get your latency numbers thrown out. Use wrk2/k6 open-model; report
   tails with CIs.
5. **Baselines age.** Every cited number (Claude-3.5 τ-bench Pass^1; GPT-4 InjecAgent 24%) is tied to
   a **model snapshot + benchmark commit** — state both, or it's not reproducible.
6. **Don't cite ToolEmu's emulator-fidelity %** — that figure didn't survive verification; validate
   headline ToolEmu results on a real shim.
7. **Single-source caveats:** AgentDojo and ToolEmu each rest on one (authoritative, peer-reviewed)
   source in our verified set — fine to cite, but lead with AgentDojo + InjecAgent (two sources) for
   the safety headline.
8. **Contamination/overfitting:** don't tune NIL to a benchmark's tasks; report on held-out splits;
   disclose any seeding of the τ-bench/PocketBase schemas.

---

## 8. Open questions to resolve before publishing
- The right **control** for the safety A/B: raw-API agent vs NIL-gated with which gate policy
  (auto-approve vs oracle vs human)? The delta's credibility hinges on this.
- The **precise operational definition** of UWR/HVR mapped onto AgentDojo's task/security split and
  BFCL relevance — written so a reviewer can recompute it.
- Whether to publish as a **technical report + repo** (reproducibility pack) or aim for a workshop
  paper (higher bar, more reach).

## 9. First execution slice — highest ROI (draft)

**Verdict.** Start with **P0 (A/B core) + P3·InjecAgent** as the headline, run **P2 (conformance
pass^k)** in parallel as a near-free win. Rationale: InjecAgent is the **cheapest path to NIL's most
differentiating, citable number** — *Unauthorized-Write Rate reduction vs the 24% base ASR* — because
it's a **static dataset** (no framework setup like AgentDojo, no schema-seeded environment like
τ-bench). P2 costs almost nothing extra (reuses our 17/17 suite) and turns the rollback fix into a
permanent property. Defer BFCL (saturated, "not sufficient"), AgentDojo (framework setup), τ-bench
(env bridge), and perf until this slice proves the loop.

### The one number we're chasing
> *"Same model, same attacks: a raw tool-using agent commits the injected malicious write **X%** of
> the time (cf. InjecAgent's 24% base for ReAct GPT-4); the **NIL-gated** agent commits it **0%** —
> while completing the **benign** tasks at parity."* — reported as the pair (UWR↓, benign success≈),
> never UWR alone.

### Workstreams

**W0 · A/B harness core** (`bench/core/`) — the reusable spine.
- `arms.py`: `ARM_RAW` (agent calls tools directly) vs `ARM_NIL` (tool calls routed as
  propose→[gate]→commit through a NIL shim). Same agent, model snapshot, RNG seed across arms.
- `gate.py`: approval oracle with modes `auto | policy | oracle`. For the safety run the gate is an
  **intent oracle** — approve a write iff it's in the *user's* authorized task, reject injected ones.
  (This is the honest control; see Risks.)
- `nil_tool_bridge.py`: generic "tool call → NIL verb" mapper (reuses the SDK + a FakeSystem/PocketBase shim).
- `report.py`: one JSON+markdown schema emitting `UWR, HVR, benign_success, pass^k, ASR` with the
  model snapshot + dataset commit stamped in.
- **DoD:** a trivial 2-task fixture runs through both arms and emits a report row.

**W1 · InjecAgent runner** (`bench/safety/injecagent_runner/`) — the headline.
- Pull InjecAgent's 1,054 cases (17 user tools × 62 attacker tools; direct-harm + exfiltration).
- Map its tools onto NIL verbs (write tools → `commerce.*`/`resource.*`; reads → `resource.read`).
- Run both arms; classify each episode: did a malicious state-changing effect **commit**?
- Compute **UWR** (NIL arm should be ~0 by construction), **HVR**, and **ASR vs 24% base**.
- **DoD:** a table — `{arm, ASR, UWR, HVR, benign_success}` — with NIL UWR ≈ 0 and benign parity.

**W2 · Conformance pass^k + Hypothesis** (`bench/conformance/`) — parallel free win.
- Wrap the existing `nilscript conformance-test` + adapter suite in a k-repeat harness → `pass^k`.
- One `RuleBasedStateMachine` over propose/commit/rollback asserting the four invariants (idempotency,
  rollback-honesty incl. **real-record-id compensation**, refusal correctness, no-side-effect-on-PROPOSE).
- **DoD:** published `conformance pass^k` (target 1.0 at k=8) + N invariants green over M sequences.

### Sequence & effort (relative)
1. **W0** (S–M) — blocks everything. Build first, thin.
2. **W2** (S) — start in parallel; no external deps, immediate number.
3. **W1** (M) — the headline, once W0 exists.
4. Write up the slice as a mini-report (UWR table + pass^k) → the first publishable artifact.

### Definition of done for the slice
- A reproducible command produces: (a) InjecAgent UWR/ASR table for both arms, (b) conformance pass^k,
  (c) a stamped report (model + commits). NIL arm: **UWR ≈ 0, benign success within noise of raw arm.**

### Risks / things that make-or-break credibility
- **The gate control is everything.** If the gate auto-approves, NIL "wins" trivially and reviewers
  dismiss it. Use the **intent oracle** (approve only the user-authorized write) so the result reflects
  *NIL surfacing the decision*, not NIL refusing everything — and report benign success to prove it.
- **Tool→verb mapping fidelity:** if NIL can't express an InjecAgent tool, document it (don't silently
  drop cases — that inflates the result). Coverage % is part of the report.
- **Don't overclaim "0 by construction":** show it's measured, and that benign tasks still complete.
- Stamp **InjecAgent commit + model snapshot**; 24% is the *base* baseline, not enhanced.

### Why not start elsewhere
- *BFCL first?* Cheapest to wire, but "necessary not sufficient" + saturated → weak as a lead number.
- *AgentDojo first?* Higher value than InjecAgent (629 cases, adaptive attacks) but it's a framework
  with more setup — do it as W3 right after this slice, reusing W0, to strengthen the safety headline.
- *τ-bench first?* Highest rigor but an env bridge + schema seeding = the most effort; sequence it after
  the safety story lands.

---

## Sources (verified, primary unless noted)
τ-bench: arXiv 2406.12045 (ICLR'25), github.com/sierra-research/tau-bench · τ²: arXiv 2506.07982 ·
BFCL: proceedings.mlr.press/v267/patil25a.html (ICML'25), github.com/ShishirPatil/gorilla, Databricks
"Unpacking Function-Calling Eval" (blog) · AgentDojo: arXiv 2406.13352 (NeurIPS'24) · InjecAgent:
arXiv 2403.02691 (ACL Findings'24) · ToolEmu: arXiv 2309.15817 (ICLR'24) · Hypothesis stateful
(docs) · Jepsen (jepsen.io) · Pact (docs.pact.io) · wrk2 (github.com/giltene/wrk2) · coordinated
omission (scylladb.com) · SPEC RG reproducibility (research.spec.org).

*Generated from a verified deep-research pass (25 sources fetched, 119 claims extracted, 25
adversarially verified 3-vote, 23 confirmed / 2 killed). Axes 1–2 rest on peer-reviewed agent
benchmarks; axes 3–4 on software-engineering rigor (no agent leaderboard covers them).*

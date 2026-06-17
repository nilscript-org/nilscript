# NIL benchmarks

Execution of [`docs/benchmarking-plan.md`](../docs/benchmarking-plan.md). The thesis: NIL isn't a
model, so we **instrument** established benchmarks and report a controlled **A/B** — same agent, same
model, same seed, run **raw-API** vs **NIL-gated** — where task-success holds and harmful writes
collapse toward zero.

## Status

| Slice | What | State |
|---|---|---|
| **W0 · core** (`core/report.py`) | one report schema (JSON+md), `pass^k`, reproducibility stamp | ✅ landed |
| **W2 · conformance** (`conformance/test_invariants.py`) | Hypothesis stateful machine over propose/commit/rollback + real-record-id regression guard | ✅ landed |
| **W1 · safety (InjecAgent)** (`safety/injecagent_runner.py`) | real 1,054-case dataset; live LLM agent (LiteLLM) or scripted upper-bound; paired ASR/UWR A/B | ✅ landed (live-capable) |
| τ-bench bridge, BFCL export, perf | see plan §1/§6 | ⏳ later |

## Run

```bash
# from the kernel root, with the reference adapter + kernel on the path
PYTHONPATH="../adapters/pocketbase-nil-adapter/src:src:." \
  pytest bench/conformance/test_invariants.py -q
```

(`pip install hypothesis` if missing.)

```bash
# W1 · InjecAgent safety A/B — structural upper bound (no model, deterministic):
PYTHONPATH=src:. python bench/safety/injecagent_runner.py \
  --data /tmp/inj_test_cases_dh_base.json /tmp/inj_test_cases_ds_base.json

# W1 · live arm — real model via LiteLLM. The key comes from the ENVIRONMENT, never a file:
export CEREBRAS_API_KEY=...        # or OPENAI_API_KEY, etc.
PYTHONPATH=src:. python bench/safety/injecagent_runner.py \
  --data /tmp/inj_test_cases_dh_base.json --agent live --model cerebras/gpt-oss-120b --limit 40
```

Dataset: `uiuc-kang-lab/InjecAgent` (`data/test_cases_{dh,ds}_base.json`). Pin a commit when publishing.

### Live result — full matrix (4,216 evaluations)

![InjecAgent: unauthorized-write rate, raw vs NIL](assets/injecagent_safety.svg)

*(regenerate: `python bench/core/chart.py bench/safety/matrix.json bench/assets/injecagent_safety.svg`)*

2 models × 2 attack settings × 1,054 cases each, single-step decision, temp 0, 16-way concurrent:

| model | setting | cases | ASR (hijack) | UWR raw | **UWR via NIL** | benign |
|---|---|---|---|---|---|---|
| cerebras/gpt-oss-120b | base | 1054 | 2.75% | 2.75% | **0.00%** | 100% |
| cerebras/gpt-oss-120b | enhanced | 1054 | 0.47% | 0.47% | **0.00%** | 100% |
| cerebras/zai-glm-4.7 | base | 1054 | 4.46% | 4.46% | **0.00%** | 100% |
| cerebras/zai-glm-4.7 | enhanced | 1054 | 0.00% | 0.00% | **0.00%** | 100% |

**Headline (model- and attack-independent):** across all 4,216 evaluations, unauthorized writes commit
**0.00% through NIL** while benign tasks stay at **100%**. Whatever fraction the model is hijacked
(2.75–4.46% raw here), NIL commits none of those writes.

**Honest caveats (do not omit when publishing):**
- *Enhanced < base ASR here* (0.47%/0.00% vs 2.75%/4.46%) — **opposite** to InjecAgent's literature
  (enhanced ≈ 2× base for GPT-4-ReAct). Likely because these reasoning models detect the blatant
  enhanced hacking-prompt more readily than the subtle base injection, and because this harness uses a
  **single-step decision, not InjecAgent's two-step ReAct**. Treat these ASRs as harness-specific, not
  comparable head-to-head with the published 24% — the NIL→0 result is the robust, comparable claim.
- Raw ASRs (2.75–4.46%) sit well below the 24% GPT-4-ReAct base — strong reasoners + single-step.
- For publication rigor (plan §7): port to two-step ReAct, add more/standard models, pin the dataset
  commit, and gate in CI. Never report UWR without the paired benign-success (§2d).

## What W2 proves (axis 3 — protocol conformance as *properties*)

The stateful machine drives random valid sequences and asserts on every reachable state:
- **idempotency** — replaying COMMIT with the same key never writes twice
- **no-side-effect-on-PROPOSE** — PROPOSE is a dry run; backend state is unchanged
- **rollback honesty** — a reversible effect mints a token; ROLLBACK *previews* then reverses;
  an unknown token is refused, never silently actioned
- **refusal correctness** — unknown verb → `UNKNOWN_VERB` (never a 500)

Plus a focused regression guard (`test_create_rollback_targets_real_id_even_after_rename`) that
locks the fix where a compensation must target the **real record id**, not a human name — so a
create→rename→rollback deletes the right record instead of 404ing.

## Metrics (defined in `core/report.py`)
- **`pass^k`** — τ-bench's reliability metric: fraction of tasks where *all k* trials pass.
- **UWR / HVR** (W1) — Unauthorized-Write Rate / Hallucinated-Verb Rate; always reported **paired**
  with benign task-success (never alone — see plan §2d, §7).

Every result carries a reproducibility **stamp** (kernel version, model snapshot, dataset commit,
seed) — no number is published without it.

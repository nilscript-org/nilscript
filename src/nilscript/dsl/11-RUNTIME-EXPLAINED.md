# 11 — Runtime Explained: From Spec to Running Code

> A teaching primer. Two parts: **(A)** the spec-vs-runtime mental model — why NIL *and* Wosool
> DSL are both just specs, and where the actual running code lives; and **(B)** a deep dive into
> the **interpreter** — the one loop that *is* the runtime. Read this if "how does the DSL run?"
> is still fuzzy.

---

> **الخلاصة بالعربي.** المواصفة (spec) = وثيقة قواعد فقط (`.md` + JSON Schema)، لا تنفّذ شيئاً.
> الـ runtime = كود حقيقي يقرأ ويُنفّذ مع احترام قواعد المواصفة. **NIL مواصفة. وnilscript DSL مواصفة
> أيضاً.** كلاهما مجرد قواعد. أمّا **wosool-cloud فهو الكود** الذي يولّد ويتحقّق ويُشغّل برامج الـ DSL،
> ويُرسل رسائل NIL أثناء التشغيل. "runtime اللغة" = شيئان داخل wosool-cloud: **المُدقّق (validator)** الذي
> يقول مقبول/مرفوض، و**المُفسّر (interpreter)** الذي يمشي على الرسم البياني عقدة-عقدة. المفسّر
> يعمل *فوق* Temporal (محرّك المتانة). الجزء (ب) في الأسفل يشرح المفسّر بالتفصيل — هو قلب الـ runtime.

---

# Part A — Spec vs Runtime (the mental model)

## A.1 The one idea

Two different kinds of thing, and conflating them is the whole confusion:

- **Spec** = rules written down (`.md` + JSON Schema). It *does nothing*. It is only the agreed
  definition of *what is legal*.
- **Runtime** = real code that *reads* something and *does* it, while obeying the spec's rules.

You already know this for NIL ("NIL is only a spec, just `.md` rules"). The leap:
**nilscript DSL is the same kind of thing — also just a spec.** And **wosool-cloud is the code that
implements both specs.**

| Thing | What it actually is | Has its own runtime? |
|---|---|---|
| **NIL** | A spec — `.md` + JSON schemas describing *messages* (PROPOSE/COMMIT/QUERY…) | **No.** It is rules. |
| **nilscript DSL** | A spec — `.md` + JSON Schema + conformance describing *programs* (the graph) | **No.** It is rules. |
| **wosool-cloud** | **Code.** Generates, validates, and runs DSL programs — and emits NIL messages while doing so | **Yes — wosool-cloud *is* the runtime.** |

Neither NIL nor the DSL has "its own runtime" sitting somewhere separate. **The runtime lives
inside wosool-cloud.** One body of code (wosool-cloud) obeys two specs (NIL + DSL).

## A.2 The analogy that makes it obvious

This exact pattern is everywhere in computing:

| Spec (a document) | Runtime (code that runs it) |
|---|---|
| **ECMAScript** (the JavaScript standard) | **V8** (Chrome's engine that runs JS) |
| **SQL standard** | **PostgreSQL** (the engine that runs SQL) |
| **HTML spec** | **Chrome** (renders HTML) |
| **NIL spec** | **wosool-cloud's NIL client** (`nilscript/sdk/client.py`) + the business OS's NIL server |
| **nilscript DSL spec** | **wosool-cloud's validator + interpreter** |

Nobody says "JavaScript implements its own runtime." JavaScript is a *spec*; V8 is a *separate
program* that runs it. **nilscript DSL plays the role of JavaScript; wosool-cloud plays the role of V8.**

## A.3 The layer stack — what runs on what

```
   A DSL program (JSON)           ← the "bytecode": inert data the VM executes
          │  is the input to
          ▼
   DynamicGraphExecutorWorkflow   ← "the Wosool VM": YOUR code that knows how to read DSL
          │  runs on top of
          ▼
   Temporal                       ← "the CPU / OS": a generic durability engine.
                                     Knows NOTHING about commerce or DSL. Just runs
                                     workflows reliably — survives crashes, retries, resumes.
          │  each `action` makes a
          ▼
   NIL sentence (PROPOSE/COMMIT)   ← "a syscall": the message sent to the business OS
```

- **Temporal** is off-the-shelf. It provides *durability* only — it has no idea what a "product"
  or "coupon" is.
- **The interpreter** is the code *you* write *for* Temporal. A generic loop that reads *any* DSL
  program and executes it. **This is the piece that does not exist yet** (🔴). Everything else
  (the per-node primitives) already exists in `wosool_worker`.
- **The DSL program** is just *input data* to that loop.

## A.4 A concrete end-to-end trace

Merchant: *"refund order 123 and tell me the result."*

1. **Generate** — the LLM writes a DSL program (`action: refund` → `await_approval` → `notify`).
   *Just JSON.*
2. **Validate** — `WosoolDSLValidator` checks schema, references, acyclicity, skill whitelist. OK.
3. **Preview & approve** — merchant sees the steps, says «نعم».
4. **Execute** — the gateway calls `start_workflow(DynamicGraphExecutorWorkflow, program)`.
   *Now Temporal is running your interpreter, with the DSL program as its argument.*
5. The interpreter **loops** over the nodes; on the `action` node it runs an activity that calls
   the **NIL client**, which sends `PROPOSE`/`COMMIT` to the business OS. On `await_approval` it
   pauses durably until a NIL `EVENT` says approved. On `notify` it sends the message.
6. Done. If the server crashed at step 5, Temporal replays and resumes exactly there.

**The DSL never "talks to" the business OS.** The DSL is inert; the *interpreter* runs it, and
when it hits an `action`, *the interpreter* speaks NIL. DSL on top, NIL underneath, interpreter
in the middle translating one into the other.

## A.5 Where NIL sits — the full sandwich

NIL is also "spec + code," same pattern: the **spec** is the message rules (`nilscript`); the
**runtime** is `nilscript/sdk/client.py` (sends sentences) on wosool-cloud's side + a **NIL server** inside
the business OS (receives them, does the real DB write).

```
nilscript DSL spec   ──defines──▶  the graph the LLM writes
        (wosool-cloud's validator + interpreter run it)
                          │  each action becomes a…
                          ▼
NIL spec          ──defines──▶  the message sent south
        (wosool-cloud's NIL client sends it; the business OS's NIL server executes it)
```

wosool-cloud implements the top half (DSL) and the *sending* side of the bottom half (NIL). The business
OS implements the *receiving* side of NIL. Three parts, two contracts, clean seams.

---

# Part B — The Interpreter: the heart of the runtime

> Earlier ([04-EXECUTION-MODEL.md §2](04-EXECUTION-MODEL.md)) the interpreter appeared as a
> ~12-line sketch. Here is the full picture: every moving part, why each exists, and a
> step-by-step trace of a program running. **This single component is "the runtime."** Build it
> and the language is alive.

All code below is **illustrative** — it shows the shape, not the final implementation. The
determinism rules in B.7 are real constraints any real version must obey.

## B.1 What the interpreter *is*

A program that takes **one input** — an admitted DSL program — and **executes its graph**. It is
a *generic* walker: it does not know about products or refunds; it only knows the eight node
*types*. Hand it any valid program and it runs. That genericity is the entire payoff: one loop
replaces every hand-coded flow (B.10).

Three responsibilities, nothing more:

1. **Track state** — remember each completed step's output (the *execution context*).
2. **Walk** — from `entry`, run the current node, choose the next, repeat until terminal.
3. **Dispatch** — for each node, do the right thing *by its `type`*.

## B.2 The execution context — the interpreter's memory

As each step finishes, its output is written into a dictionary keyed by node id. This is the
*only* state the interpreter holds, and it is **append-only and write-once**:

```python
ctx = {
    "step_1": {"output": {"id": "prod_9", "sku": "SHIRT-001"}, "status": "ok"},
    "step_2": {"output": {"stock": 4},                          "status": "ok"},
    # step_3 not run yet → not in ctx
}
```

- **Write-once:** once `ctx["step_1"]` is set it is never mutated. A "change" is always a *new*
  step producing a *new* key. (This is axiom 2 / immutability, [02 §4.3](02-GRAMMAR-AND-PRIMITIVES.md).)
- **It is what `$.step_1.output.sku` resolves against** (B.4).
- **It is the replay anchor:** because it is built deterministically from activity results,
  Temporal can rebuild it exactly on restart (B.7).

## B.3 The main loop — full version

```python
@workflow.defn
class DynamicGraphExecutorWorkflow:
    @workflow.run
    async def run(self, program: AdmittedProgram) -> RunResult:
        ctx: dict[str, dict] = {}                 # the execution context (B.2)
        node_id: str | None = program.entry       # start at the entry node
        run_id = workflow.info().workflow_id       # stable id for idempotency (B.8)

        while node_id is not None:
            node = program.nodes[node_id]          # O(1) lookup in the id→node map
            output = await self._execute(node, ctx, run_id)   # dispatch by type (B.6)
            ctx[node.id] = {"output": output, "status": "ok"} # write-once (B.2)
            node_id = self._pick_next(node, output, ctx)      # routing (B.5)

        return RunResult(context=ctx)
```

That is the spine. Everything else is `_execute` (the per-type dispatch) and `_pick_next` (the
routing). Read it as: *"while there is a node, run it, record it, find the next one."*

## B.4 Reference resolution — turning `$.step_1.output.sku` into a real value

Before an `action`/`query` runs, its `args` may contain references. The resolver walks the args
and replaces every reference string with the real value from `ctx`. **This is how data flows
between steps** — and why the LLM never has to know a value in advance.

```python
REF = re.compile(r"^\$\.(step_[0-9]+|item|input)((?:\.[A-Za-z_]\w*|\[[0-9]+\])+)$")

def resolve(value, ctx, item=None):
    # Literal → returned as-is. Reference string → looked up. Dict/list → resolved recursively.
    if isinstance(value, dict):
        return {k: resolve(v, ctx, item) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ctx, item) for v in value]
    if isinstance(value, str) and (m := REF.match(value)):
        source, path = m.group(1), m.group(2)
        root = {"output": item} if source == "item" else ctx[source]   # ctx["step_1"] = {"output": …}
        return _walk(root, path)        # follow ".output.sku" / "[0].id" into the object
    return value                        # plain literal hint (e.g. "89", "hidden")

# Example:  resolve({"sku": "$.step_1.output.sku"}, ctx)  →  {"sku": "SHIRT-001"}
```

Key properties:
- **Pure** — no I/O, fully deterministic. Safe to run inside the workflow sandbox (B.7).
- **Selection only** — a reference *points at* a value; it never computes. Computation lives in
  `condition`/`foreach` ([02 §4.1](02-GRAMMAR-AND-PRIMITIVES.md), the Reference-Path rule).
- **The validator already guaranteed** the source step exists and *precedes* this one (no forward
  refs, V6) — so `ctx[source]` is always present by the time we resolve.

## B.5 Routing — `_pick_next`

How the next node is chosen depends on the node type:

```python
def _pick_next(self, node, output, ctx):
    if node.type == "condition":
        return node.on_true if output is True else (node.on_false or node.next)
    if node.type == "await_approval":
        return {"approved": node.on_approved,
                "rejected": node.on_rejected,
                "timeout":  node.on_timeout}[output]      # output is the resolution
    # action / query / wait / notify / parallel / foreach: linear successor (or terminal)
    return node.next            # None → the loop ends
```

Most nodes just go to `next`. Only branching nodes (`condition`, `await_approval`) choose among
targets — and they choose based on the value they *just produced* (the guard result, the approval
outcome). `parallel`/`foreach` fan out *internally* (B.6) and then continue to their single
`next`.

## B.6 The node executors, one by one

`_execute(node, ctx, run_id)` dispatches on `node.type`. Here is what each does and which
existing primitive it reuses ([07-MAPPING-TO-REPO.md](07-MAPPING-TO-REPO.md)).

### `action` — the only node that *changes* the world 🟢
```python
args = resolve(node.args, ctx)                       # B.4: refs → real values
key  = f"{run_id}:{node.id}"                          # deterministic idempotency key (B.8)
result = await workflow.execute_activity(
    activities.COMMIT,                                # the activity speaks NIL: PROPOSE→COMMIT
    CommitInput(verb=node.verb, args=args, idempotency_key=key),
    start_to_close_timeout=ACTIVITY_TIMEOUT,
    retry_policy=to_temporal_retry(node.retry_policy),
)
return result["data"]                                 # the System's ResultEnvelope.data
```
Reuses `IntentTaskFlow`'s exact pattern: bounded retry + `run_id:node_id` idempotency. **NIL is
spoken only here** (and in `query`).

### `query` — read business truth, change nothing 🟢
Same shape, but calls the `QUERY` activity. Returns the fetched data into `ctx`. Business state
is read *fresh* here, never memorized in the graph. Reuses the `MorningBriefing` QUERY path.

### `condition` — route on a guard, no side effect 🔴
```python
return evaluate_guard(node.expression, ctx)          # → True / False (pure, B.7)
```
Returns a bool; `_pick_next` turns it into `on_true`/`on_false`. `evaluate_guard` is the small
**CEL-style** evaluator (B.7 / [02 §5](02-GRAMMAR-AND-PRIMITIVES.md)) — total, side-effect-free,
no `eval`. *Net-new, but tiny.*

### `wait` — durable delay 🟢
```python
await workflow.sleep(timedelta(seconds=node.seconds)); return None
```
Survives restarts (Temporal timer). Reuses the `FollowUpFlow` delay pattern.

### `notify` — send a bilingual message 🟢
```python
text = resolve(node.message, ctx)                    # refs allowed inside the message
await workflow.execute_activity(activities.SEND_OUTBOUND, OutboundInput(text_ar=text["ar"], …))
return None
```
Reuses the `SEND_OUTBOUND` activity.

### `parallel` — fan out independent branches 🟢
```python
outs = await asyncio.gather(*[self._run_subgraph(b, ctx, run_id) for b in node.branches])
return {"branches": outs}                             # join="all" = barrier
```
This *is* `MultiIntentFlow`: one durable child per branch, gather, per-branch isolation (a failed
branch becomes a failed outcome, never sinks its siblings).

### `foreach` — bounded map 🔴
```python
items = resolve(node.items, ctx)[: node.max_items]   # HARD cap — no unbounded iteration
outs = []
for element in items:
    outs.append(await self._run_subgraph(node.body, ctx, run_id, item=element))  # $.item.* binds here
return {"items": outs}
```
A bounded comprehension, not a loop — it cannot diverge (totality, [02 §3.5](02-GRAMMAR-AND-PRIMITIVES.md)).
*Net-new.*

### `await_approval` — pause for a human 🟢
```python
state = await workflow.execute_activity(activities.POLL_STATUS, resolve(node.proposal, ctx), …)
# (or: race a pushed NIL EVENT signal vs a durable timeout — exactly AwaitDecisionFlow)
return {"approved": "approved", "rejected": "rejected", …}.get(state, "timeout")
```
Reuses `AwaitDecisionFlow`. The interpreter **waits**; it never speaks the approval (that is
owner-plane `DECIDE`, which this layer cannot send).

## B.7 The activity boundary & determinism — *why it is split this way*

Notice the pattern: **pure logic runs in the workflow body; every side effect runs in an
`execute_activity` call.** That split is not stylistic — it is the law Temporal imposes, and the
reason durability works.

| Runs in the **workflow** (the interpreter loop) | Runs in an **activity** |
|---|---|
| the `while` loop, `_pick_next`, `resolve`, `evaluate_guard` | `COMMIT`, `QUERY`, `POLL_STATUS`, `SEND_OUTBOUND` |
| **must be deterministic & pure** — no clock, no randomness, no I/O | **may do anything** — network, NIL calls, DB |
| replayed from event history on restart | results recorded in history; not re-run on replay |

Why: Temporal recovers a crashed workflow by **replaying its event history** to rebuild state. If
the loop called the network or read the clock directly, replay would produce different results
and corrupt the run. So the loop stays pure and *delegates* every real-world effect to an
activity, whose result Temporal records. On replay, the loop re-executes but the recorded activity
results are handed back instead of re-running — so `ctx` rebuilds identically. This is exactly the
discipline `workflows.py` already follows (`workflow.unsafe.imports_passed_through`, deterministic
rendering, no OS-locale dependence).

**Consequence for the DSL:** `resolve` and `evaluate_guard` *must* be pure (they are — selection
and total expressions). That is *why* the expression language is constrained to CEL and references
are selection-only: anything else would break replay.

## B.8 Idempotency — exactly-once effects on at-least-once delivery

Temporal activities are **at-least-once** (an activity may run twice on retry). So every `action`
must be safe to repeat. The interpreter derives the key **deterministically from workflow
state** — `run_id:node_id` — and passes it into the NIL `COMMIT`. The business OS dedupes on that
key, so a retry or a replay re-issues *the same* sentence and never double-creates. The key is
**never** generated with `uuid()`/clock inside the activity (that would differ across retries and
break replay). Same rule the repo already encodes in `CommitBody.idempotency_key` and
`IntentTaskFlow`'s `parent_turn_id:task_id`.

## B.8.1 The Saga unwind — what happens when a multi-step program fails

If the program declares `on_error: "compensate"` and a step **terminally** fails after earlier
steps already committed, the interpreter does not stop with a half-applied program. It **walks
the committed steps in reverse commit order and undoes each** — the Saga unwind (axiom 4's
backward half, [04 §7.1](04-EXECUTION-MODEL.md)).

The loop already recorded which steps committed (they are the keys in `ctx`). Sketch:

```python
async def _unwind(self, ctx, program, run_id):
    for step_id in reversed(list(ctx)):          # newest committed step first
        node = program.nodes[step_id]
        comp = node.get("compensate_with")
        if comp is None:                          # IRREVERSIBLE → cannot undo
            return PartialResult(undone=..., blocked_at=step_id)   # honest partial, never "success"
        if step_id not in auto_compensate_grant:  # COMPENSABLE / not blessed
            await self._park_for_decide(step_id)  # wait for a human DECIDE; never auto-reverse
            continue
        args = resolve(comp["args"], ctx)         # backward-only refs (B.4): own/earlier outputs
        key  = f"{run_id}:{step_id}:rollback"     # deterministic, idempotent (B.8)
        await workflow.execute_activity(
            activities.ROLLBACK,                  # requests a reversal — does NOT write directly
            RollbackInput(verb=comp["verb"], args=args, idempotency_key=key))
        # ROLLBACK is answered by a PROPOSAL (compensation preview) → COMMIT: no silent write
```

Three rules make this safe and honest:

- **Auto only for blessed REVERSIBLE steps.** A step compensates automatically *only* if it sits
  on the `auto_compensate` grant allowlist. COMPENSABLE / non-allowlisted steps **park** for a
  human `DECIDE`; the runtime never silently reverses what was not pre-blessed.
- **An IRREVERSIBLE step (no `compensate_with`) blocks a full rollback.** The run is then reported
  as an honest **partial** — what was undone and where it stopped — never dressed up as success.
- **A reversal is itself governed.** `ROLLBACK` does not execute a write; it requests one,
  answered by a `PROPOSAL` (compensation preview) that is then `COMMIT`ted. Preview-then-confirm
  holds even while undoing. Refusals seen here: `IRREVERSIBLE`, `COMPENSATION_EXPIRED`.

Because the keys are workflow-derived (`run_id:step_id:rollback`), a crash *during* the unwind
replays and resumes the unwind at the right step — it never double-undoes.

## B.9 Worked traces — watch `ctx` evolve

### Trace 1 — `conformance/valid/01-single-action.json`
```
ctx = {}                         node_id = "step_1"
─ run step_1 (action, create_product):
     args  = resolve({"name":"قميص قطن","price":"89"}, ctx) = same (no refs)
     COMMIT activity → NIL PROPOSE→COMMIT → result.data = {"id":"prod_9"}
     ctx["step_1"] = {"output":{"id":"prod_9"}, "status":"ok"}
     next = null
─ loop ends → RunResult(ctx)
```

### Trace 2 — `conformance/valid/03-condition-dataflow.json`
```
ctx = {}                         node_id = "step_1"
─ step_1 (query commerce.product, sku=SHIRT-001):
     QUERY activity → {"stock": 4};   ctx["step_1"]={"output":{"stock":4}}        next = step_2
─ step_2 (condition "$.step_1.output.stock < 10"):
     evaluate_guard → resolve $.step_1.output.stock = 4 → 4 < 10 → True
     ctx["step_2"]={"output":True}            _pick_next → on_true = step_3
─ step_3 (action create_coupon, percent=20):
     COMMIT → {"id":"cpn_3"};  ctx["step_3"]={"output":{"id":"cpn_3"}}            next = null
─ loop ends
```
The branch that ran was *decided at runtime* by a value (`stock=4`) the LLM never knew — it only
wrote the reference. That is the whole point of the language.

## B.10 Why one loop replaces a hundred hand-coded workflows

Today, each multi-step flow is a bespoke Temporal workflow an engineer writes (`MorningBriefing`,
`FollowUpFlow`, `MultiIntentFlow`). Adding a flow = shipping Python + a deploy.

With the interpreter, the flow is **data** (a DSL program), and the *same* loop runs all of them.
Adding a flow = the LLM emitting new JSON, validated and previewed — **no deploy.** The engineer
writes the VM *once*; the LLM writes the programs *forever*. That inversion — logic moves out of
code and into validated, previewable data — is the reason the DSL exists.

## B.11 What you reuse vs. what is net-new

```
 REUSE AS-IS (🟢)                          NET-NEW FOR THE INTERPRETER (🔴)
 ───────────────────────────────          ─────────────────────────────────
 • COMMIT / QUERY / POLL_STATUS /          • the while-loop walker (B.3)
   SEND_OUTBOUND activities                • resolve() reference resolver (B.4)
 • IntentTaskFlow (action retry+idem)       • _pick_next routing (B.5)
 • MultiIntentFlow (parallel)               • evaluate_guard (small CEL evaluator)
 • AwaitDecisionFlow (await_approval)        • condition + foreach executors (B.6)
 • FollowUpFlow (wait)                        • AST models (typed program/nodes)
 • MorningBriefing (query+notify)
```

Five of eight node executors are thin wrappers over code that already passes the offline gate.
The genuinely new code is the **walker + resolver + guard evaluator** — a few hundred lines,
fully unit-testable without an LLM.

## B.12 Build checklist (this is "implement the runtime")

1. **AST models** — Pydantic `AdmittedProgram` + `Node` subtypes (typed form of the JSON), frozen
   + `extra="forbid"` like `NilModel`.
2. **`resolve()`** — the reference resolver (B.4). Pure. Unit-tested against `ctx` fixtures.
3. **`evaluate_guard()`** — a vetted CEL/JSONLogic evaluator wrapper (B.7). No `eval`.
4. **`DynamicGraphExecutorWorkflow`** — the loop (B.3) + `_execute` dispatch (B.6) + `_pick_next`
   (B.5). Register it on the worker (`worker.py`).
5. **Wire the start** — after the merchant confirms (the chat confirm→commit handler, Phase 2),
   `start_workflow(DynamicGraphExecutorWorkflow, admitted_program)`.
6. **Test** — run `conformance/valid/*` end-to-end on the Temporal test server; verify a
   crash-mid-graph resumes at the right node (replay test).

That is the entire runtime. The validator ([03](03-VALIDATION-AND-TYPES.md)) guards the door; this
interpreter walks whatever the validator admitted.

---

See [04-EXECUTION-MODEL.md](04-EXECUTION-MODEL.md) for the lifecycle around this loop, and
[08-ROADMAP.md](08-ROADMAP.md) Phase 3 for where it sits in the build order.

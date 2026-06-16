# 02 — Grammar & Primitives

> The closed vocabulary of nilscript DSL: the program envelope, the node types ("keywords"),
> the data-reference sub-language, and the constrained expression language for guards.
> Every shape here is also encoded in [`schema/nilscript-dsl.v0.1.schema.json`](schema/nilscript-dsl.v0.1.schema.json).

A language needs a fixed set of keywords. Wosool's are **node types**. The set is *closed*:
the validator rejects any `type` outside it (an unknown keyword is unrepresentable, not merely
wrong). This is what makes LLM output reliable and validation tractable.

---

## 1. The program envelope

A Wosool program is a single JSON object:

```json
{
  "wosool": "0.1",
  "workspace": "ws_demo",
  "locale": "ar",
  "entry": "step_1",
  "pipeline": [ /* nodes */ ],
  "on_error": "halt"
}
```

| Field | Type | Meaning |
|---|---|---|
| `wosool` | `"0.1"` (const) | Language version. Explicit and required — the validator and runtime branch on it. |
| `workspace` | string | The tenant. Determines which grant scopes gate the whitelist. Never carries secrets. |
| `locale` | `"ar"` \| `"en"` \| BCP-47 short | Default locale for previews/replies. Arabic-first default. |
| `entry` | node id | The start node (ASL `StartAt`). Must be a defined node. |
| `pipeline` | array of nodes | The node set. Order is *declaration* order, not execution order — execution order is the topological sort. |
| `on_error` | `"halt"` \| `"continue"` \| `"compensate"` | Program-level default failure policy; a node may override. `"compensate"` makes the program a **Saga**: on terminal failure the runtime unwinds completed steps in reverse via governed compensation (§3.1, [04 §7](04-EXECUTION-MODEL.md)). |

> **Why a flat `pipeline` array, not nested blocks?** A flat, ID-addressed node map (ASL
> `States`, Argo `dag.tasks`) keeps every branch target a first-class, validatable reference
> and keeps the graph easy to render. Branches point *by id*; they do not embed sub-trees that
> are hard to preview. (The draft also permits inline `on_true`/`on_false` arrays for small
> conditionals as a convenience — see §3.2.)

---

## 2. Node identity & common fields

Every node shares:

| Field | Type | Rule |
|---|---|---|
| `id` | string, pattern `^step_[0-9]+$` | Unique within the program. The strict `step_N` form is required (the charter's constraint). |
| `type` | enum | One of the closed node types (§3). Discriminates the node's variant. |
| `next` | node id \| null | Successor on success. `null`/absent = terminal. Branching nodes use their own targets instead. |
| `on_error` | error policy (optional) | Per-node override of the program default. See §5. |
| `retry_policy` | object (optional) | Bounded retry for this node. See §5. |

**The `step_N` discipline and forward-reference prohibition.** IDs are minted in increasing
order. A node may reference (`$.step_k…`) only steps with `k` *strictly less than its own
index in topological order*. Referring to a not-yet-defined step is a **forward reference** and
is rejected at validation — it is the graph analog of using a variable before assignment, and
it is what keeps the data flow a clean DAG. (Charter constraint §4; validator rule V-REF-2 in
[03](03-VALIDATION-AND-TYPES.md).)

---

## 3. The node types (the keywords)

The closed set. Each node lists its **runtime status** against this repo (see the legend in
[00-INDEX.md](00-INDEX.md)) — i.e. whether a Temporal primitive to execute it already exists.

### 3.1 `action` — invoke a skill 🟢

The core primitive. One node → one skill → one NIL `PROPOSE` (then, after approval, `COMMIT`).

```json
{
  "id": "step_1",
  "type": "action",
  "skill": "product",
  "verb": "commerce.create_product",
  "args": { "name": "قميص قطن", "price": "89" },
  "next": "step_2"
}
```

| Field | Rule |
|---|---|
| `skill` | Must be a registered skill name (`product`, `coupon`, `invoice`, `refund`, `order_status`, …). |
| `verb` | Must be that skill's `required_verb` **and** allowed by the workspace's grant scopes (`scope_allows`). |
| `args` | A bag of **hints** (see the box below). Validated against the skill's `hint_schema`; values may be literals *or* data references (§4). |
| `compensate_with` | *Optional.* A **compensating action** `{verb, args}` declaring how to undo this step if a later step fails. Makes the program a Saga. See below. |

**`compensate_with` — the undo handle (Saga).** An `action` may declare how to reverse
itself. The value is a compensating action with the same `{verb, args}` shape as the action it
undoes:

```json
{
  "id": "step_2",
  "type": "action",
  "skill": "invoice",
  "verb": "commerce.create_invoice",
  "args": { "order": "$.step_1.output.id" },
  "compensate_with": {
    "verb": "commerce.void_invoice",
    "args": { "invoice": "$.step_2.output.id" }
  },
  "next": "step_3"
}
```

- The compensation's `args` derive from prior results by **backward-only** reference
  (`$.step_2.output.id`, `$.step_1.output.id`) — never a forward reference, preserving the
  least-power data-flow rule (§4). A compensation may read its *own* step's `output` (the thing
  it is undoing) and any earlier step's output.
- It does **not** execute on declaration. On terminal failure of a `compensate` program, the
  runtime issues a NIL `ROLLBACK` per completed step in reverse commit order; the reversal is
  itself governed (preview → confirm), so "no silent write" holds even when undoing
  ([04 §7](04-EXECUTION-MODEL.md), [11 Part B](11-RUNTIME-EXPLAINED.md)).
- A step with no `compensate_with` declares itself *irreversible*: it cannot be auto-rolled-back
  and forces an honest partial rather than a false "success".

> **`args` are HINTS, not authoritative values** — the most important and most easily-missed
> rule. This layer forwards string hints; the **business OS resolves them authoritatively** and
> *its* preview is what the merchant confirms (conversational-layer hard rule 3). So `"price":
> "89"` is a *hint* the System re-resolves; the DSL never asserts the canonical price. This is
> why `args` carry strings/refs, and why typed business values (resolved category id, computed
> tier) appear only in the **output** of an executed step, never in its input.

Compiles to:

```json
{ "performative": "PROPOSE", "body": { "verb": "commerce.create_product",
  "args": { "name": "قميص قطن", "price": "89" } } }
```

### 3.2 `condition` — branch on a guard 🔴

```json
{
  "id": "step_3",
  "type": "condition",
  "expression": "$.step_2.output.stock < 10",
  "on_true": "step_4",
  "on_false": "step_5"
}
```

| Field | Rule |
|---|---|
| `expression` | A guard in the constrained expression language (§5). Boolean-valued, side-effect-free, total. |
| `on_true` / `on_false` | Either a node id, **or** an inline array of nodes (sugar for a small branch). At least `on_true` required; a missing `on_false` falls through to `next`. |

A `condition` performs **no** side effect — it only routes. (LangGraph conditional-edge model:
the model emits a small routing decision; deterministic code owns the transition.)

### 3.3 `query` — read business truth 🟢

A QUERY-only read. No proposal, no commit, nothing changes. Used to fetch the facts a later
branch depends on. Business state is *never* memorized in the graph — it is read fresh here.

```json
{
  "id": "step_2",
  "type": "query",
  "verb": "commerce.product",
  "args": { "sku": "$.step_1.output.sku" },
  "next": "step_3"
}
```

Compiles to a NIL `QUERY`. Runtime primitive: the `QUERY` activity + `MorningBriefing` pattern
already exist.

### 3.4 `parallel` — fan out independent branches 🟢

```json
{
  "id": "step_4",
  "type": "parallel",
  "branches": ["step_5", "step_6", "step_7"],
  "join": "all",
  "next": "step_8"
}
```

| Field | Rule |
|---|---|
| `branches` | Array of node ids, each the entry of an *independent* sub-flow. Branches must not share mutable state (axiom 2). |
| `join` | `"all"` (default) \| `"any"`. `all` = barrier; the join completes when every branch resolves. |

Runtime: this is exactly `MultiIntentFlow` — one durable child per branch, `asyncio.gather`,
per-branch isolation (a failing branch becomes a `failed` outcome, never sinks its siblings).

### 3.5 `foreach` — bounded map over a collection 🔴

```json
{
  "id": "step_5",
  "type": "foreach",
  "items": "$.step_4.output.low_stock_skus",
  "as": "sku",
  "body": "step_6",
  "max_items": 50,
  "next": "step_7"
}
```

| Field | Rule |
|---|---|
| `items` | A data reference to an **array** produced by an earlier step. Never an unbounded generator. |
| `as` | The loop variable name, referenceable inside `body` as `$.item.<field>` / `$.item`. |
| `max_items` | **Required.** A hard cap. There is no unbounded iteration in Wosool — totality is enforced here. |

`foreach` is a *bounded comprehension*, not a loop — like CEL's `map`/`filter` macros. It
cannot diverge.

### 3.6 `await_approval` — human-in-the-loop gate 🟢

```json
{
  "id": "step_7",
  "type": "await_approval",
  "proposal": "$.step_6.output.proposal_id",
  "timeout_seconds": 86400,
  "on_approved": "step_8",
  "on_rejected": "step_9",
  "on_timeout": "step_10"
}
```

Pauses the graph until the owner plane decides (approves/rejects) or the decision window
elapses. Maps directly to `AwaitDecisionFlow` (poll-or-signal a parked HIGH/CRITICAL proposal),
which is itself the durable embodiment of NIL's preview-then-confirm. The DSL **cannot** speak
the approval (`DECIDE`) — it can only *wait* for it.

### 3.7 `wait` — durable delay 🟢

```json
{ "id": "step_8", "type": "wait", "seconds": 3600, "next": "step_9" }
```

A durable timer (survives restarts). Maps to `FollowUpFlow`'s delay cadence /
`workflow.sleep`. `seconds` must be a positive integer literal (no expressions — a wait must be
previewable as a fixed duration).

### 3.8 `notify` — send an outbound message 🟢

```json
{ "id": "step_9", "type": "notify",
  "message": { "ar": "خلص المخزون!", "en": "Stock ran out!" }, "next": null }
```

Sends a bilingual chat message to the merchant. Maps to the `SEND_OUTBOUND` activity. The
`message` is bilingual (Arabic-first); inline data references are allowed
(`"ar": "باقي $.step_2.output.stock قطعة"`).

### Node-type summary

| Type | Side effect | NIL performative | Runtime primitive | Status |
|---|---|---|---|---|
| `action` | proposes a change | `PROPOSE` → `COMMIT` (undo: `ROLLBACK`) | engine `_propose` / `IntentTaskFlow` | 🟢 |
| `query` | none (read) | `QUERY` | `QUERY` activity / `MorningBriefing` | 🟢 |
| `condition` | none (route) | — | *interpreter* | 🔴 |
| `parallel` | fans out | (per branch) | `MultiIntentFlow` | 🟢 |
| `foreach` | bounded map | (per item) | *interpreter* | 🔴 |
| `await_approval` | waits | (polls `STATUS`) | `AwaitDecisionFlow` | 🟢 |
| `wait` | durable delay | — | `FollowUpFlow` / `sleep` | 🟢 |
| `notify` | sends message | — | `SEND_OUTBOUND` activity | 🟢 |

That is the entire keyword set. There is no `function`, no `assign`-to-global, no `import`, no
`eval`. **Real computation lives in skills, never in the graph.**

---

## 4. The data-reference sub-language

Steps pass data **only** by reference path. There are no variables, no global scope.

### 4.1 Reference grammar

```
$.<source>.output.<field>[.<field>…]
$.<source>.output.<array>[<index>]
$.item            # inside a foreach body — the current element
$.item.<field>
$.input.<field>   # the program's initial input, if any
```

- `<source>` is a node id (`step_2`) or `item`/`input`.
- A reference is a **Reference Path** in the ASL sense: a single-node pointer. **No filters, no
  wildcards, no functions** in the path itself (`$.step_2.output.items[?(@.x)]` is *not* valid).
  Selection only; computation belongs in a `condition`/`foreach`. This "least power for
  plumbing" rule keeps references trivially traceable.
- References resolve against an immutable, append-only **execution context** the runtime builds
  as each step completes (`ctx["step_2"]["output"] = …`). A step can read only the outputs of
  steps that *precede it in topological order* — enforcing axiom 2 and the
  forward-reference prohibition.

### 4.2 Why references, not values

The LLM **never fabricates a value it cannot yet know.** It writes
`"sku": "$.step_1.output.sku"`, and the runtime substitutes the real SKU after `step_1`
executes. This is the single highest-value anti-hallucination primitive in LLM plan languages
(LLMCompiler `$1`, ReWOO `#E1`). It also means the *shape* of the program is fixed at
validation time even though the *values* are not known until run time.

### 4.3 Immutability

Each step's `output` is written **once** and never mutated. There is no in-place update of a
shared object. A "change" is always a new step producing a new, isolated output. (Coding-style
immutability rule, lifted into the data model.)

---

## 5. The expression sub-language (guards)

`condition.expression` and `foreach` predicates use a **constrained, total, side-effect-free**
expression language — *not* arbitrary code. The draft adopts **CEL-style** semantics (Google
Common Expression Language): evaluates in linear time, mutation-free, **not Turing-complete**.

### 5.1 Allowed

| Category | Examples |
|---|---|
| Reference paths | `$.step_2.output.stock`, `$.item.price` |
| Literals | `10`, `3.5`, `"approved"`, `true`, `null` |
| Comparison | `==` `!=` `<` `<=` `>` `>=` |
| Boolean | `&&` `||` `!` |
| Membership | `$.step_1.output.status in ["approved", "executed"]` |
| Bounded macros | `has($.step_2.output.sku)`, `size($.step_3.output.items)` |

### 5.2 Forbidden (rejected by the validator)

- Assignment, mutation, or any statement that changes state.
- Function *definition*; calls to anything outside the fixed builtin set.
- Unbounded comprehension or recursion.
- Host access — no I/O, no time-of-day, no randomness (those would break replay determinism).

### 5.3 Why CEL, not JSONPath-filters or `eval`

A guard must (a) terminate, (b) be deterministic for replay, (c) carry zero injection surface.
CEL guarantees all three by construction; `eval` guarantees none. JSONPath can *select* but not
*compute* a predicate cleanly. For purely data-stored rules an alternative is **JSONLogic**
(logic-as-data, never `eval`'d) — noted as a fallback in [09-REFERENCES.md](09-REFERENCES.md).

---

## 6. Error-handling grammar (self-healing metadata)

Any node may carry failure metadata. This is axiom 4 made syntactic — and axiom 4 heals
*both* directions: forward (diagnostic → re-compile) and backward (`compensate_with` →
governed unwind). A step's `compensate_with` (§3.1) is the backward half.

```json
{
  "id": "step_2", "type": "action", "skill": "refund",
  "verb": "commerce.process_refund", "args": { "order": "$.step_1.output.id" },
  "retry_policy": { "max_attempts": 3, "backoff": "exponential", "initial_seconds": 2 },
  "on_error": { "action": "route", "to": "step_9" },
  "next": "step_3"
}
```

| Field | Values | Maps to |
|---|---|---|
| `retry_policy.max_attempts` | int ≥ 1 | Temporal `RetryPolicy(maximum_attempts=…)` (already used in `IntentTaskFlow`). |
| `retry_policy.backoff` | `"exponential"` \| `"fixed"` | Temporal backoff coefficient. |
| `on_error.action` | `"halt"` \| `"continue"` \| `"route"` \| `"compensate"` | What the runtime does after retries exhaust. `"compensate"` triggers the Saga unwind ([04 §7](04-EXECUTION-MODEL.md)). |
| `on_error.to` | node id | Target when `action: "route"`. |

On terminal failure the runtime emits a **structured diagnostic** (node id, error class,
message). That diagnostic is fed back to the Generation layer so the LLM can **re-compile a
corrected program** — the self-healing loop ([04-EXECUTION-MODEL.md](04-EXECUTION-MODEL.md) §6).
Retriable NIL refusals (`RATE_LIMITED`, `UPSTREAM_UNAVAILABLE`) are retried automatically; the
rest surface as diagnostics.

---

Next: **[03-VALIDATION-AND-TYPES.md](03-VALIDATION-AND-TYPES.md)** — how a program is admitted
or rejected.

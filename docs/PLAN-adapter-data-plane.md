# NIL Read Data Plane — Architecture (revised, ground-up)

Grounded in a real failure: `crm.list_contacts` returned **590 KB for 41 contacts** (full Odoo
`res.partner` dumps) → context flood → agent fell back to `read_file`/`execute_code` (zero business
data). The fix is **not** an Odoo patch. The read plane was never given a contract; this gives it one.

## Hard constraint (the no-shared-import reality)

Adapters are **standalone HTTP shims** — the Odoo adapter depends only on `fastapi`/`pydantic`, it does
**not** import the `nilscript` package. So "universal" cannot mean a shared library. The universal
guarantee lives in three seams that already exist:

1. **Contract** — read-verb request/answer schemas + the `/nil/v0.1/describe` shape, in
   `nilscript/nil/schemas`. The SSOT every adapter conforms to.
2. **Conformance proof** — the suite each adapter's `conformance/` dir must pass: projection, byte cap,
   refuse-not-truncate, cursor stability, capability negotiation.
3. **Relay backstop** — the MCP relay in `nilscript`, the one chokepoint that sees every adapter's
   output and re-enforces the byte cap regardless of adapter behavior.

Contract + conformance **force** all adapters to comply; the relay backstop **catches** any that don't.

## The invariant (one sentence)

> Business data reaches the agent only as **(a)** a bounded, projected, paginated page that fits a hard
> byte cap, **(b)** a server-side aggregate, or **(c)** an opaque **handle** to an artifact the agent
> processes with code — and any read that cannot be made to fit is **REFUSED, never truncated**.

A naive cap that truncates is a *bug*: it drops the row the agent needed (رغد at record #500,000) and
returns a confident wrong "not found". The cap therefore **refuses**; correctness comes from selection
(filter/count/aggregate) happening server-side, where the rows live.

## The verb network (every adapter implements; `<ns>.*`)

| Verb | Returns | Role |
|------|---------|------|
| `schema(target)` | fields `{name,type,filterable,sortable,returnable,is_key,sensitivity}` + cardinality `small\|large\|huge` + default projection + **capability profile** `{server_filter,server_sort,server_paginate,server_aggregate}` | how to query + how the edge degrades |
| `count(target,filter)` | `{count}` or `{count,approximate:true}` | first call for "how many / exists" |
| `search(target,filter,fields,sort,limit,cursor)` | lean page `{items:[{id,…proj}],total?,next_cursor}` | a few by criteria; keyset cursor stable for 1M; **refuses** over cap |
| `get(target,id,fields)` | one lean record | exact lookup by key |
| `aggregate(target,filter,group_by,metrics)` | `{groups:[{key,metrics}]}` | server-side rollup ("revenue by country") |
| `export(target,filter,fields,format)` | **handle** `{handle,format,rows,bytes,schema,expires_at}` | bulk read → artifact; **governed + audited** |

Filter is a typed predicate list `[{field,op,value}]`, ops: `eq,ne,gt,gte,lt,lte,in,contains,ilike,between`.
Writes unchanged: `create/update/delete` via `propose→commit→gate`, plus the bulk-write spine below.

## Capability negotiation + enforced fallback (the universal part)

`schema(target).capabilities` declares what the backend does server-side. The edge has a **defined
fallback per missing capability — never a silent fetch-all**:

| Capability absent | Edge behavior |
|---|---|
| `server_filter` (a field) | bounded pull (≤ cap rows) + edge-side filter; **refuse** if the unfiltered set exceeds the bound |
| `server_sort` | sort the bounded page only; declare "sort is page-local" or route to export |
| `server_paginate` | export-only for that target (no cursor promise) |
| `server_aggregate` | `aggregate` transparently does export→edge-side rollup, bounded; refuse if unbounded |

A weak backend degrades **honestly** (refuse / export), never by re-introducing the flood.

## Bulk read = governed action (closes the exfiltration hole)

"Reads are free" holds for `count/get/search/aggregate` (bounded, projected). It does **not** hold for
`export`:
- export above `BULK_THRESHOLD` (rows/bytes) or touching `sensitive` fields → requires
  **propose→approve** (a read proposal, tier by size+sensitivity) and is **always audited**
  (who, target, filter, rows, bytes).
- handles are **tenant/session-scoped**, access-controlled on fetch, **PII-at-rest**: sandbox-local,
  TTL-deleted, never logged.

Bulk extraction becomes a deliberate, attributable act — not a silent side effect of a "free" read.

## Read-side authorization

`effective_fields = requested ∩ returnable ∩ grant_visible(target)`. Sensitive fields (salary, PII)
require an explicit grant; absent it they are dropped from the projection and the response **notes the
redaction** (never silently implies completeness). Row-level policy where backend/grant defines it.

## "All the data" — the completed decision tree

```
intent
 ├─ exists / how many        → count            (bounded, may be approximate)
 ├─ one by key               → get
 ├─ a few by criteria        → search(filter,fields,sort) + cursor
 ├─ rollup / group           → aggregate(group_by,metrics)        ← server-side
 ├─ deliver the dataset      → export → handle → download/artifact (agent never reads rows)
 ├─ analyze row-level        → export → sandbox → pandas/DuckDB    (only small result in context)
 └─ act on all of them       → export id set → BATCHED propose→commit→gate (resumable, stoppable)
every branch: fits-or-REFUSED, never truncated; bulk export gated + audited + tenant-scoped
```

## Bulk-write spine (heavy ops: delete-many / update-many / email-all)

`export(filter)` → id-set handle → walk in batches of N:
- each batch: `propose→commit→gate` (existing governance, per batch),
- checkpoint after each batch (**resumable**; **idempotent** — committed batches never re-apply),
- **STOP honored between batches**,
- partial-failure policy `skip+report | stop | compensate` (declared per run; default skip+report + audit),
- bulk-delete reversibility: per-batch compensation tokens where the verb is COMPENSABLE.

Same spine powers "fetch big data", "delete many", "update many" — smart batches, multi-step, reliable.

## Failure taxonomy (refusals are answers, never filesystem fallbacks)

`RESULT_TOO_LARGE` (narrow filter / use export) · `FIELD_NOT_FILTERABLE` (which fields are) ·
`FIELD_NOT_SORTABLE` · `CAPABILITY_UNSUPPORTED` (export instead) · `HANDLE_EXPIRED` ·
`NOT_AUTHORIZED_FIELD` (grant needed) · `BULK_APPROVAL_REQUIRED`. Each is structured + actionable.

## Agent skill (the discipline)

count/schema first → get by key → search tight+projected → aggregate for rollups → export→code for
row-level over many → export→batch for actions. **ABSOLUTE:** business data lives only behind these
verbs; a large/awkward result is a `RESULT_TOO_LARGE` refusal to narrow or export — **never** a reason
to `read_file`/`execute_code` over the agent's own tree; **0 rows = "none found", never invented**.

## Build order (TDD, failing-test-first)

1. **Contract primitives** (`nilscript`, pure, no I/O): typed filter, projection, **byte-cap
   enforcement that refuses-not-truncates**, data-handle type, capability profile. ← start here
2. **Read-verb schemas + `/describe` extension** + conformance assertions.
3. **Generic edge read plane** over the `SystemClient` protocol (projection, cap-refuse, capability
   fallback, read authz, bulk-export gate) — proven against `FakeSystem`.
4. **Odoo mapping**: `search/get/count/aggregate/export` onto `search_read(fields=)` / `read_group` /
   keyset cursor; verify name-search on 41 AND synthetic 1M stays under cap.
5. **Relay byte-cap backstop** in the MCP + `nil_search/count/get/aggregate/export`.
6. **Bulk-write spine** + agent skill replaces the stopgap guardrail.

## Acceptance

- "Find رغد عبدالله" works on 41 AND 1,000,000 — via `search(name ilike …)` or `export`+Python — no
  flood, precise, deterministic. A cap hit → **refusal to narrow**, never a truncated wrong answer.
- "How many overdue?" → one `count`. "Revenue by country across all" → one `aggregate` (or export→code).
- Bulk export of all customers is **gated + audited + tenant-scoped**, not a free read.
- The agent NEVER falls back to `read_file`/`execute_code` on business data; every read bounded,
  projected, paginated; same inputs → same outputs.

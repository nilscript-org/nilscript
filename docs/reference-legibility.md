# Reference Legibility (NIL conformance clause)

**Status:** normative · **Applies to:** every NIL adapter · **Enforced by:** conformance row
`references_echoed_by_name` · **Origin:** the "country_id = 224" incident (an agent set a contact's
country to a guessed integer, narrated it as three different countries, and a bare id sailed through
approval and read-back illegibly).

## The one-line claim, and the gap it hides

> NIL guarantees you commit the value you **expressed**; it never promised you expressed the value you
> **meant**. Opaque foreign keys hide that gap, and an agent's narration papers over it.

NIL's structural guarantee — β⁻¹(a) = ∅, the committed value equals the expressed value, read-back
matched, reversible — held perfectly in the incident: `224` wrote, verified, and was reversible. NIL
governs the **effect** (which integer landed), not the **planning** (which integer to pick).
Name→id resolution happens *before* the proposal, outside NIL's mandate. Had the kernel "caught" the
wrong country, it would have reached into agent reasoning — the exact overreach the architecture
forbids. **The kernel and the transport are not at fault and are not touched by this clause.**

The fault lives entirely in the two layers *above* NIL, and they are distinct:

- **Fault A — the opaque-reference gap (wrong write).** A bare foreign key crossed the gate with no
  legibility. The agent had to manufacture an integer, guessed it from a generic Odoo demo DB in
  training memory, and NIL faithfully committed a correctly-written, semantically-wrong value. This
  lives at the seam between planning and effect — where an opaque id lets planning leak into the
  effect illegibly.
- **Fault B — observation dishonesty (the worse one).** The agent narrated "224 = Uzbekistan," then
  "the lookup is impossible" — both fabricated, neither grounded in a real read. The `verified:true`
  was honest *about the write*; the sentence around it was invented. Fault A made it commit the wrong
  thing; Fault B made it lie confidently about what it committed. **B survives a fix to A** unless the
  observation itself is constructed from ground truth.

## The requirement

A NIL adapter MUST make every **constrained** field legible — a *relation* (foreign key to another
target) or a *selection* (closed option set) — at both boundaries it crosses:

1. **On write (Fault A).** Resolve the field's value against live backend data and **echo the resolved
   human label** in the PROPOSAL — in `resolved` as `{field: {value, label}}` and in the `preview`
   text. The owner approves over *meaning* (`country → Saudi Arabia`), never a magic number.
2. **On read-back (Fault B).** After commit, the receipt/diff MUST re-resolve each written constrained
   field **from the value that actually landed in the SSOT** to its label, so the Observation reads
   `committed: country → Saudi Arabia (192)`. Because the label is produced by the adapter's
   re-resolution against live data — not by the agent — there is no raw material left for the agent to
   fabricate. **The echo is symmetric: same resolution on write and read-back.**

This is **not a convention.** A convention is something an adapter author can forget; by the
architecture's own A1 principle, *a safety property that is optional is not a guarantee.* It is a
**conformance requirement**: a non-conformant adapter fails the matrix.

### Placement (consistent with the three-layer invariant)

Resolution belongs in the **adapter**, never the kernel — only the Odoo adapter knows Odoo's country
table. The **canonical implementation lives once in the SDK** (`nilscript.sdk.legibility`); the
scaffold template wires it, so every adapter inherits it; the **conformance row enforces the
behavior** regardless of how an adapter chooses to implement it. Spec mandates it, SDK implements it
once, the matrix proves it, every FK/selection field in every backend gets it.

## Two traps (both fatal if glossed)

1. **A naive string-matcher just relocates the guess.** "Saudi Arabia → 192" by exact match is fine.
   "Türkiye / Turkey / تركيا", or "Georgia (the country) vs Georgia (the US state)", is where a dumb
   resolver silently picks wrong — the same many2one resolver that already crashed on an Arabic
   int/string value. **Resolution MUST refuse and return candidates on ambiguity, never auto-pick.**
   That is the Choice Gate (PROPOSE-time) and the terminal-failure-on-ambiguity (COMMIT-time) that
   already exist; legibility does not weaken them. **Legibility is best-effort *labeling for display*;
   it never makes the write decision.** If a value is ambiguous, the gate refuses with candidates
   before any label is shown.
2. **Echo must be symmetric and bound into the proposal.** Resolve on write *and* on read-back so the
   receipt is constructed from live re-resolution, not from the request. The proposal's content
   identity SHOULD bind the semantic label alongside the id, so the decision log records *meaning*
   (`"Saudi Arabia"`), not a bare `192` — which is also what makes the log usable as an audit/
   underwriting asset later.

## What this does and does not fix

- **Closes Fault A everywhere:** an opaque id can no longer cross approval illegibly; a wrong pick is
  visible at the gate, in the owner's language.
- **Removes the raw material for Fault B:** the Observation is the adapter's re-resolution of the
  landed value, so the agent cannot invent the country name.
- **Does not cure an agent that genuinely means the wrong entity.** If the agent asks for the wrong
  customer by name and it resolves unambiguously, NIL will faithfully commit the wrong customer.
  Nothing at this layer can fix intent. This clause closes the *illegibility* gap, not free will.

## SDK contract (`nilscript.sdk.legibility`)

```
LabelLookup = (model: str, value: Any) -> str | None
    # adapter-supplied: resolve an id-or-name on `model` to its canonical label;
    # None when it can't be labeled (best-effort — the gate, not this, refuses ambiguity)

field_label(meta, value, lookup) -> str | None
    # label for one value given its field meta ({relation} or {options}); None if unconstrained

legible(schema, fields, lookup) -> dict[str, {"value", "label"}]
    # the symmetric echo: every constrained field in `fields` that resolves to a label.
    # Used identically to enrich PROPOSAL.resolved (write) and the read-back diff (receipt).

echo_preview(text, labels) -> str
    # append a legible tail to a preview line: "Update contact 43 · country → Türkiye"
```

`legible()` is pure given the injected `lookup`, so it is unit-tested with a fake backend and adopted
unchanged by any adapter (Odoo, PocketBase, future). Selection labels come from the field's `options`
(no lookup); relation labels come from `lookup`.

## Conformance row: `references_echoed_by_name`

For an adapter whose describe skeleton marks a written field as a relation/selection, a PROPOSE that
sets that field MUST return `resolved[field]` carrying a human `label` (not a bare id), and the
committed STATUS/diff MUST carry the landed value's label. Skeletons that expose no constrained field
pass vacuously. This sits beside `exposes_describe_skeleton` and the rollback-honesty rows.

## Rollout

1. SDK: `nilscript.sdk.legibility` (canonical, tested). ✅
2. Live Odoo adapter (`odoo-crm-nil-adapter`): echo labels in PROPOSE `resolved`/preview (A) and in
   `_verify_and_diff` read-back (B). ✅ — the fix the incident demanded.
3. Conformance row `references_echoed_by_name`; scaffold template wires the SDK helper; PocketBase
   example reaches parity. → next phase.

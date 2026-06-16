<!-- Building an adapter? You don't PR here — open an "Adapter submission" issue. This repo is the protected core. -->

## What & why

<!-- One paragraph: what changes and the problem it solves. -->

## Type

- [ ] Editorial (docs/typo) — label `editorial`, fast-track
- [ ] Tooling / SDK / CLI (non-normative)
- [ ] **Normative spec change** — requires the process below

## For normative changes (GOVERNANCE.md)

- [ ] An issue was opened first using the `proposal` flow, discussed ≥ 14 days
- [ ] Includes a **§15 Security considerations** analysis ("it's convenient" is not one)
- [ ] Implementation experience exists in the reference implementation
- [ ] No invariant weakened (closed performative set, Six Guarantees, floors, tenant isolation, preview completeness)

## Checks

- [ ] `pytest` green locally
- [ ] Schemas/examples still validate (they run in CI)
- [ ] Docs/cross-links updated if behavior or commands changed

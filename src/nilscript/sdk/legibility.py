"""Reference legibility — the canonical, adapter-agnostic resolve-and-echo-by-name helper.

A NIL adapter MUST make every *constrained* field — a relation (foreign key) or a selection
(closed option set) — legible at both boundaries it crosses: echo its human label in the
PROPOSAL on write (Fault A: an opaque id never crosses approval illegibly) and re-resolve the
landed value to its label on read-back (Fault B: the Observation is built from live data, not
agent narration). The echo is symmetric — the same `legible()` call serves both sides.

This is the single source of truth referenced by docs/reference-legibility.md and enforced by the
`references_echoed_by_name` conformance row. It is pure given the injected `lookup`, so it is unit-
tested against a fake backend and adopted unchanged by every adapter.

Legibility is *labeling for display only*. It never makes the write decision and never auto-picks:
an unresolvable or ambiguous value is simply left unlabeled here — the Choice Gate (PROPOSE-time)
and terminal-failure-on-ambiguity (COMMIT-time) own correctness and refusal.
"""

from __future__ import annotations

from typing import Any, Callable

# (model, value) -> canonical label, or None when it can't be labeled (best-effort, never a guess).
LabelLookup = Callable[[str, Any], "str | None"]


def field_label(meta: dict[str, Any], value: Any, lookup: LabelLookup) -> str | None:
    """Human label for one written `value`, given its field `meta`.

    A relation field defers to `lookup(meta['relation'], value)`; a selection field reads its label
    straight from the declared `options` (value-key or label, case-insensitive). An unconstrained
    scalar, an empty value, or an unresolvable one returns None — there is nothing legible to echo.
    """
    if value in (None, ""):
        return None
    relation = meta.get("relation")
    if relation:
        return lookup(relation, value)
    options = meta.get("options")
    if options:
        v = str(value).strip().lower()
        for opt in options:
            if str(opt.get("value")).lower() == v or str(opt.get("label", "")).strip().lower() == v:
                return opt.get("label")
        return None
    return None


def legible(
    schema: list[dict[str, Any]] | None,
    fields: dict[str, Any],
    lookup: LabelLookup,
) -> dict[str, dict[str, Any]]:
    """The symmetric echo: ``{field: {"value", "label"}}`` for every constrained field in `fields`
    that resolves to a label. Plain scalars are omitted. Call it identically to enrich the
    PROPOSAL's ``resolved`` (write) and the read-back diff (receipt) — same resolution both ways."""
    meta_by_field = {f.get("name"): f for f in (schema or [])}
    out: dict[str, dict[str, Any]] = {}
    for name, value in fields.items():
        label = field_label(meta_by_field.get(name) or {}, value, lookup)
        if label is not None:
            out[name] = {"value": value, "label": label}
    return out


def echo_preview(text: str, labels: dict[str, dict[str, Any]]) -> str:
    """Append a legible tail to a preview line so the owner approves over meaning, not magic numbers:
    ``"Update contact 43"`` + ``{country_id: {label: 'Türkiye'}}`` → ``"Update contact 43 · country_id → Türkiye"``."""
    if not labels:
        return text
    tail = " · ".join(f"{field} → {echoed['label']}" for field, echoed in labels.items())
    return f"{text} · {tail}"

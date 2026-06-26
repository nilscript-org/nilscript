"""NIL Intent — the single model-facing payload. The model emits an `Intent` (a semantic description of
*what* it wants to know or change); `IntentResolver` deterministically maps it to a governed, lean
execution and returns an `Outcome`. No tool selection, no filter constructed by the model, no keyword
matching — the model fills a schema, the system owns the mechanics.

This file covers READS (the/all/count). Writes (change→propose→commit→tier) and summary→aggregate plug
into the same resolve() in later phases. The ontology mapping (about→target, attr→field) is delegated to
a pluggable `BindingResolver` — the graph supplies the real one; `IdentityResolver` is the default for
adapters without an ontology layer (about IS the target, attr IS the field).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .engine import CapabilityUnsupported, ReadPlane
from . import InvalidFilter, ResultTooLarge

# Structural relation → typed filter op. A FIXED enum map (not keyword matching): the model picks a
# relation from a closed set, the system maps it to the data-plane op. Universal across adapters.
REL_TO_OP: dict[str, str] = {
    "is": "eq", "is_not": "ne", "contains": "ilike",
    "gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte", "between": "between", "in": "in",
}

SEEK_SHAPES = frozenset({"the", "all", "count", "summary"})


@dataclass(frozen=True)
class Binding:
    """One criterion in the intent, in ontology terms: attribute `rel` value."""

    attr: str
    rel: str
    value: Any


# op → the universal generic-CRUD verb every adapter supports (no adapter-specific verb map, no
# keywords): a change intent executes through resource.* — the deterministic write spine.
OP_TO_RESOURCE: dict[str, str] = {
    "create": "resource.create",
    "update": "resource.update",
    "remove": "resource.delete",
}


@dataclass(frozen=True)
class Change:
    """The write shape of an intent: what to make true. Resolved → propose→commit→tier (governed)."""

    op: str                 # create | update | remove
    set: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Intent:
    """The single payload the model emits — *what* it wants, never *how*."""

    about: str                      # ontology type / entity (adapter-agnostic)
    where: tuple[Binding, ...] = ()
    seek: str = "all"               # the | all | count | summary  (read shapes)
    change: Change | None = None    # present → a write intent (governed via propose→commit→tier)
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True)
class Outcome:
    """The deterministic answer: a small result, a held proposal (writes), or a structured refusal
    that carries the corrective parameter so recovery needs no reasoning."""

    kind: str                       # "result" | "proposal" | "refusal"
    value: Any = None
    code: str | None = None
    fix: str = ""

    @classmethod
    def result(cls, value: Any) -> "Outcome":
        return cls("result", value=value)

    @classmethod
    def refusal(cls, code: str, fix: str = "") -> "Outcome":
        return cls("refusal", code=code, fix=fix)


class BindingResolver(Protocol):
    """Maps ontology terms to a concrete adapter target/field. The graph implements the real ontology
    mapping; the identity resolver is the default when about IS the target and attr IS the field."""

    def resolve_target(self, about: str) -> str: ...

    def resolve_attr(self, about: str, attr: str) -> str: ...


class IdentityResolver:
    def resolve_target(self, about: str) -> str:
        return about

    def resolve_attr(self, about: str, attr: str) -> str:
        return attr


class IntentResolver:
    """Resolve an Intent → Outcome over a ReadPlane (reads) deterministically."""

    def __init__(self, read_plane: ReadPlane, binding_resolver: BindingResolver | None = None) -> None:
        self._plane = read_plane
        self._bind = binding_resolver or IdentityResolver()

    def resolve(self, intent: Intent, *, grant_fields: Any = None) -> Outcome:
        if intent.seek not in SEEK_SHAPES:
            return Outcome.refusal("INVALID_SEEK", f"seek must be one of {sorted(SEEK_SHAPES)}")
        try:
            target = self._bind.resolve_target(intent.about)
            filt = [
                {"field": self._bind.resolve_attr(intent.about, b.attr), "op": REL_TO_OP[b.rel], "value": b.value}
                for b in intent.where
            ]
        except KeyError as exc:
            return Outcome.refusal("INVALID_REL", f"unknown relation {exc}; use one of {sorted(REL_TO_OP)}")
        try:
            if intent.seek == "count":
                return Outcome.result(self._plane.count(target, filter=filt))
            if intent.seek == "the":
                page = self._plane.search(target, filter=filt, fields=None, limit=1, grant_fields=grant_fields)
                items = page.get("items", [])
                return Outcome.result(items[0] if items else None)  # not found = result None, never error
            if intent.seek == "all":
                page = self._plane.search(
                    target, filter=filt, fields=None, limit=intent.limit, cursor=intent.cursor,
                    grant_fields=grant_fields,
                )
                return Outcome.result(page)
            return Outcome.refusal("NOT_IMPLEMENTED", "summary (aggregate) lands in the next phase")
        except (ResultTooLarge, CapabilityUnsupported, InvalidFilter) as exc:
            return Outcome.refusal(getattr(exc, "code", "ERROR"), getattr(exc, "message", str(exc)))

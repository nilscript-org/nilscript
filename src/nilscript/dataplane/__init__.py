"""NIL read data-plane primitives (pure, no I/O).

The architectural heart of the read contract: business data reaches the agent only as a bounded,
projected page that fits a hard byte cap — and a read that cannot be made to fit is REFUSED, never
truncated. Truncation would silently drop the row the agent needed and return a confident wrong
answer; refusal is honest, and correctness comes from server-side selection (filter/count/aggregate),
which the cap merely protects.

Used by both the adapter edge (first enforcement) and the MCP relay (the backstop that catches a
misbehaving adapter), so the invariant holds even if one layer is wrong.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# The closed set of filter operators an adapter must understand. A typed predicate list is what lets
# selection run server-side, so the result is small by construction (never trimmed after the fact).
FILTER_OPS: frozenset[str] = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "ilike", "between"}
)

# The record key always survives projection — it's how the agent does a follow-up get/update/delete.
KEY_FIELD = "id"

# Default hard cap (bytes) on a single read result entering the agent's context. A page over this is
# refused, not trimmed. The relay re-applies this as the last line of defense against any adapter.
BYTE_CAP = 256_000


class ResultTooLarge(Exception):
    """A read whose serialized result exceeds the byte cap. Surfaced as a NIL `RESULT_TOO_LARGE`
    refusal — an actionable answer ("narrow the filter or use export"), never a truncated page."""

    code = "RESULT_TOO_LARGE"

    def __init__(self, byte_size: int, cap: int) -> None:
        self.bytes = byte_size
        self.cap = cap
        self.message = (
            f"result is {byte_size} bytes, over the {cap}-byte cap — narrow the filter "
            f"(count/search with a tighter predicate) or use export for bulk analysis"
        )
        super().__init__(self.message)


def _byte_size(payload: Any) -> int:
    """UTF-8 byte length of the canonical JSON the result would cross the wire as (Arabic and other
    non-ASCII counted as their real multi-byte width, matching what reaches the context)."""
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@dataclass(frozen=True)
class Predicate:
    """One typed filter clause `{field, op, value}` — the unit of server-side selection."""

    field: str
    op: str
    value: Any


class InvalidFilter(Exception):
    """A malformed filter. Surfaced as a NIL `INVALID_FILTER` refusal — actionable (names the offending
    op/shape), never coerced into a silent unbounded scan."""

    code = "INVALID_FILTER"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def parse_filter(raw: Any) -> list[Predicate]:
    """Validate a raw filter into typed predicates, or raise `InvalidFilter`.

    Closed-set ops; `between` needs exactly two bounds; `in` needs a list. A bad filter is refused
    (so the agent corrects it), never silently dropped — which would turn a tight query into a scan."""
    if raw in (None, []):
        return []
    if not isinstance(raw, (list, tuple)):
        raise InvalidFilter(f"filter must be a list of {{field, op, value}}, got {type(raw).__name__}")
    preds: list[Predicate] = []
    for clause in raw:
        if not isinstance(clause, dict) or "field" not in clause or "op" not in clause:
            raise InvalidFilter(f"each filter clause needs 'field' and 'op': {clause!r}")
        op = clause["op"]
        if op not in FILTER_OPS:
            raise InvalidFilter(f"unknown filter op {op!r}; allowed: {', '.join(sorted(FILTER_OPS))}")
        value = clause.get("value")
        if op == "between" and (not isinstance(value, (list, tuple)) or len(value) != 2):
            raise InvalidFilter(f"'between' needs exactly two bounds [lo, hi], got {value!r}")
        if op == "in" and not isinstance(value, (list, tuple)):
            raise InvalidFilter(f"'in' needs a list value, got {value!r}")
        preds.append(Predicate(field=clause["field"], op=op, value=value))
    return preds


def project(record: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Return a lean copy of `record` with only `fields` (+ the key). Never the whole record — this is
    what keeps a list/get from dumping Odoo's 100+ `res.partner` columns into the agent's context.
    Requested fields the record lacks are simply absent (a projection is a view, not an assertion)."""
    wanted = [KEY_FIELD, *(f for f in fields if f != KEY_FIELD)]
    return {f: record[f] for f in wanted if f in record}


def project_items(items: Iterable[dict[str, Any]], fields: Sequence[str]) -> list[dict[str, Any]]:
    """Project every row of a page to the lean projection."""
    return [project(row, fields) for row in items]


def enforce_byte_cap(payload: Any, cap: int = BYTE_CAP) -> Any:
    """Return `payload` unchanged if it fits within `cap`; otherwise raise `ResultTooLarge`.

    REFUSE, never truncate: returning a trimmed subset would drop the needed row and lie about
    completeness. The only outcomes are "fits → pass" or "too big → refuse with guidance"."""
    size = _byte_size(payload)
    if size > cap:
        raise ResultTooLarge(size, cap)
    return payload


# The governed read-verb engine (built on the primitives above). Imported at the bottom to avoid a
# circular import — engine.py depends on the primitives, not the other way round.
from .engine import (  # noqa: E402
    BULK_THRESHOLD,
    EDGE_FILTER_BOUND,
    BulkApprovalRequired,
    Capabilities,
    CapabilityUnsupported,
    FieldSpec,
    ReadBackend,
    ReadPlane,
    TargetSchema,
)
from .export import (  # noqa: E402
    ExportHandle,
    ExportStore,
    HandleExpired,
    NotAuthorizedHandle,
)
from .bulk import BulkResult, run_bulk  # noqa: E402
from .intent import (  # noqa: E402
    OP_TO_RESOURCE,
    REL_TO_OP,
    Binding,
    BindingResolver,
    Change,
    IdentityResolver,
    Intent,
    IntentResolver,
    Outcome,
)

__all__ = [
    "BYTE_CAP",
    "BULK_THRESHOLD",
    "REL_TO_OP",
    "Binding",
    "BindingResolver",
    "BulkApprovalRequired",
    "Change",
    "OP_TO_RESOURCE",
    "IdentityResolver",
    "Intent",
    "IntentResolver",
    "Outcome",
    "BulkResult",
    "EDGE_FILTER_BOUND",
    "run_bulk",
    "KEY_FIELD",
    "FILTER_OPS",
    "Capabilities",
    "CapabilityUnsupported",
    "ExportHandle",
    "ExportStore",
    "FieldSpec",
    "HandleExpired",
    "InvalidFilter",
    "NotAuthorizedHandle",
    "Predicate",
    "ReadBackend",
    "ReadPlane",
    "ResultTooLarge",
    "TargetSchema",
    "enforce_byte_cap",
    "parse_filter",
    "project",
    "project_items",
]

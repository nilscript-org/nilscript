"""Intent-as-the-only-payload (reads): the model emits a semantic Intent; the system deterministically
resolves it to a lean read and returns an Outcome. No tool selection, no filter built by the model, no
keyword matching. This is what makes "find دينا" work without depending on model intelligence.
"""

from __future__ import annotations

from nilscript.dataplane import (
    Binding,
    FieldSpec,
    IdentityResolver,
    Intent,
    IntentResolver,
    ReadPlane,
    TargetSchema,
)


class _Fake:
    def __init__(self, rows):
        self.rows = rows
        self._schema = TargetSchema(
            target="res.partner",
            fields=(FieldSpec("id", "int", is_key=True), FieldSpec("name", "str"), FieldSpec("phone", "str")),
            cardinality="large",
            default_projection=("id", "name", "phone"),
        )

    def describe_target(self, target):
        return self._schema if target == "res.partner" else None

    def _match(self, r, preds):
        for p in preds:
            v = r.get(p.field)
            if p.op == "eq" and v != p.value:
                return False
            if p.op == "ilike" and str(p.value).lower() not in str(v or "").lower():
                return False
        return True

    def fetch(self, target, *, predicates, fields, sort, limit, after_id):
        rows = [r for r in self.rows if self._match(r, predicates) and (after_id is None or r["id"] > after_id)]
        return sorted(rows, key=lambda r: r["id"])[:limit]

    def count(self, target, *, predicates):
        return sum(1 for r in self.rows if self._match(r, predicates))

    def get_one(self, target, record_id, fields):
        return next((r for r in self.rows if r["id"] == record_id), None)

    def aggregate(self, target, *, predicates, group_by, metrics):
        buckets: dict = {}
        for r in self.rows:
            if self._match(r, predicates):
                buckets[r.get(group_by)] = buckets.get(r.get(group_by), 0) + 1
        return [{"key": k, "count": v} for k, v in buckets.items()]


def _contacts():
    rows = [{"id": i, "name": f"Contact {i}", "phone": f"+9745{i:07d}", "country": ("QA" if i % 2 else "SA")}
            for i in range(40)]
    rows.append({"id": 18, "name": "دينا كمال النجار", "phone": "+97455123456", "country": "QA"})
    return rows


def _resolver():
    return IntentResolver(ReadPlane(_Fake(_contacts())), IdentityResolver())


def test_seek_the_resolves_intent_to_one_lean_record() -> None:
    # the model expressed: "the contact whose name contains دينا" — nothing more.
    intent = Intent(about="res.partner", where=(Binding("name", "contains", "دينا"),), seek="the")
    out = _resolver().resolve(intent)
    assert out.kind == "result"
    assert out.value == {"id": 18, "name": "دينا كمال النجار", "phone": "+97455123456"}


def test_seek_count_resolves_to_a_count() -> None:
    out = _resolver().resolve(Intent(about="res.partner", where=(), seek="count"))
    assert out.kind == "result"
    assert out.value == {"count": 41}


def test_seek_all_returns_a_lean_bounded_page() -> None:
    intent = Intent(about="res.partner", where=(Binding("name", "contains", "دينا"),), seek="all")
    out = _resolver().resolve(intent)
    assert out.kind == "result"
    assert [r["id"] for r in out.value["items"]] == [18]


def test_seek_the_with_no_match_is_a_result_not_an_error() -> None:
    intent = Intent(about="res.partner", where=(Binding("name", "contains", "غير موجود"),), seek="the")
    out = _resolver().resolve(intent)
    assert out.kind == "result"
    assert out.value is None  # "not found" — never an error, never invented


def test_unknown_about_is_a_structured_refusal() -> None:
    out = _resolver().resolve(Intent(about="hr.salary", where=(), seek="count"))
    assert out.kind == "refusal"
    assert out.code  # carries a code the agent can act on


def test_seek_summary_groups_via_aggregate() -> None:
    intent = Intent(about="res.partner", where=(), seek="summary", by="country")
    out = _resolver().resolve(intent)
    assert out.kind == "result"
    by = {g["key"]: g["count"] for g in out.value["groups"]}
    assert by == {"SA": 20, "QA": 21}


def test_seek_summary_without_a_dimension_is_a_refusal() -> None:
    out = _resolver().resolve(Intent(about="res.partner", where=(), seek="summary"))
    assert out.kind == "refusal" and out.code == "MISSING_DIMENSION"


# ── op=method contract readiness (Odoo full-coverage gap 3) ───────────────────────────────────────
def test_change_contract_supports_method_op() -> None:
    """The intent write contract carries `method`/`params` and maps op=method → the adapter's generic
    resource.method spine, so workflow actions (post/validate/confirm) are expressible through the one
    nil_intent payload. The execution provider that consumes this is the intent-unification phase."""
    from nilscript.dataplane import OP_TO_RESOURCE, Change

    assert OP_TO_RESOURCE["method"] == "resource.method"
    change = Change(op="method", method="button_validate", params={"foo": "bar"})
    assert change.op == "method" and change.method == "button_validate" and change.params == {"foo": "bar"}

"""Typed filter predicates: the contract that lets selection happen server-side (so the result is
small by construction, not by truncation). A predicate is {field, op, value}; ops are a closed set.
Bad shapes are refused with an actionable INVALID_FILTER — never coerced into a silent full scan.
"""

from __future__ import annotations

import pytest

from nilscript.dataplane import InvalidFilter, Predicate, parse_filter


def test_parses_a_well_formed_predicate_list() -> None:
    preds = parse_filter([{"field": "name", "op": "ilike", "value": "رغد"}])
    assert preds == [Predicate(field="name", op="ilike", value="رغد")]


def test_rejects_an_unknown_operator_with_guidance() -> None:
    with pytest.raises(InvalidFilter) as exc:
        parse_filter([{"field": "amount", "op": "approx", "value": 5}])
    assert exc.value.code == "INVALID_FILTER"
    assert "approx" in exc.value.message  # names the offending op so the agent can correct it


def test_between_requires_exactly_two_bounds() -> None:
    with pytest.raises(InvalidFilter):
        parse_filter([{"field": "due", "op": "between", "value": [1]}])


def test_in_requires_a_list_value() -> None:
    with pytest.raises(InvalidFilter):
        parse_filter([{"field": "status", "op": "in", "value": "open"}])

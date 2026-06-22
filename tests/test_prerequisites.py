"""Proactive Prerequisite DAG planner (adapter-toolkit-plan §5.3).

Backward-traverse a verb's declared prerequisites from the goal, skip the ones that already exist,
and return the UNMET ones in topological order — the saga to run before the goal. Pure: it takes a
declared graph + an `is_satisfied` oracle, performs no I/O. Complements the reactive repair loop and
the saga unwind in cli/repair.py.
"""

from __future__ import annotations

import pytest

from nilscript.kernel.prerequisites import (
    Prereq,
    PlanStep,
    PrerequisiteCycle,
    plan_prerequisites,
    run_prerequisite_saga,
)

# A small commerce graph: a price needs a product; a product needs a category.
GRAPH = {
    "commerce.set_price": (Prereq(entity="product", create_verb="commerce.create_product", carry="product_id->id"),),
    "commerce.create_product": (Prereq(entity="category", create_verb="commerce.create_category", carry="category_id->id"),),
}


def _none_exist(_prereq: Prereq) -> bool:
    return False  # nothing exists yet → the whole chain is unmet


def test_chain_orders_deepest_prerequisite_first() -> None:
    # Asked only for the LAST step (set_price); the planner discovers product is missing, and that
    # product itself needs a category — and orders the chain category → product (goal runs after).
    plan = plan_prerequisites("commerce.set_price", prerequisites=GRAPH, is_satisfied=_none_exist)
    assert [s.verb for s in plan] == ["commerce.create_category", "commerce.create_product"]
    assert plan[0] == PlanStep(verb="commerce.create_category", entity="category", carry="category_id->id")


def test_satisfied_prerequisite_is_skipped_and_not_traversed() -> None:
    # The product already exists → nothing to create, and its category prerequisite is never reached.
    plan = plan_prerequisites("commerce.set_price", prerequisites=GRAPH,
                              is_satisfied=lambda p: p.entity == "product")
    assert plan == []


def test_partial_chain_creates_only_the_missing_links() -> None:
    # Category exists but the product doesn't → create only the product.
    plan = plan_prerequisites("commerce.set_price", prerequisites=GRAPH,
                              is_satisfied=lambda p: p.entity == "category")
    assert [s.verb for s in plan] == ["commerce.create_product"]


def test_no_prerequisites_returns_empty_plan() -> None:
    assert plan_prerequisites("commerce.create_category", prerequisites=GRAPH, is_satisfied=_none_exist) == []


def test_diamond_dedupes_a_shared_prerequisite() -> None:
    # goal needs A and B; both need Z. Z must be created once, before A and B.
    graph = {
        "goal": (Prereq("a", "make_a", "a_id->id"), Prereq("b", "make_b", "b_id->id")),
        "make_a": (Prereq("z", "make_z", "z_id->id"),),
        "make_b": (Prereq("z", "make_z", "z_id->id"),),
    }
    plan = plan_prerequisites("goal", prerequisites=graph, is_satisfied=_none_exist)
    verbs = [s.verb for s in plan]
    assert verbs.count("make_z") == 1                 # shared prerequisite created once
    assert verbs.index("make_z") < verbs.index("make_a")  # before its dependents
    assert verbs.index("make_z") < verbs.index("make_b")
    assert set(verbs) == {"make_z", "make_a", "make_b"}


def test_cycle_is_detected_and_raised() -> None:
    # A needs B, B needs A — an impossible saga; never loop forever, refuse honestly.
    graph = {
        "make_a": (Prereq("b", "make_b", "b_id->id"),),
        "make_b": (Prereq("a", "make_a", "a_id->id"),),
    }
    with pytest.raises(PrerequisiteCycle):
        plan_prerequisites("make_a", prerequisites=graph, is_satisfied=_none_exist)


def test_plan_step_carry_parses_arg_and_field() -> None:
    step = PlanStep(verb="commerce.create_product", entity="product", carry="product_id->id")
    assert step.target_arg == "product_id" and step.source_field == "id"


def test_saga_executes_in_order_carrying_each_created_id_forward() -> None:
    # The whole point: ask only for set_price; the planned chain runs category → product → price as
    # one sequence, each created id carried into the next step (category_id into the product, product_id
    # into the price). The agent/owner approves the chain; the kernel executes it deterministically.
    calls: list[tuple[str, dict]] = []
    ids = {"commerce.create_category": 10, "commerce.create_product": 20}

    def create(verb: str, args: dict) -> dict:
        calls.append((verb, dict(args)))
        return {"state": "executed", "result": {"entity": {"id": ids.get(verb, 99)}}}

    plan = plan_prerequisites("commerce.set_price", prerequisites=GRAPH, is_satisfied=_none_exist)
    outcome = run_prerequisite_saga(
        "commerce.set_price", {"amount": 5.0}, plan,
        create=create,
        created_id=lambda result, fld: result["result"]["entity"]["id"],
    )

    assert outcome.status == "completed"
    assert [v for v, _ in calls] == ["commerce.create_category", "commerce.create_product", "commerce.set_price"]
    assert calls[1][1]["category_id"] == 10        # category id carried into the product
    assert calls[2][1]["product_id"] == 20         # product id carried into the price
    assert calls[2][1]["amount"] == 5.0            # the goal's own args are preserved
    assert [s.verb for s in outcome.committed] == ["commerce.create_category", "commerce.create_product"]


def test_saga_stops_and_reports_the_failed_step_for_compensation() -> None:
    # If a step fails terminally, stop — don't push a broken chain forward. The committed steps are
    # returned so the caller can run_saga_unwind() them in reverse (the other arm of self-healing).
    def create(verb: str, args: dict) -> dict:
        if verb == "commerce.create_product":
            return {"state": "failed_terminal"}
        return {"state": "executed", "result": {"entity": {"id": 1}}}

    plan = plan_prerequisites("commerce.set_price", prerequisites=GRAPH, is_satisfied=_none_exist)
    outcome = run_prerequisite_saga("commerce.set_price", {}, plan, create=create,
                                    created_id=lambda r, f: r["result"]["entity"]["id"])

    assert outcome.status == "failed" and outcome.failed_step == "commerce.create_product"
    assert [s.verb for s in outcome.committed] == ["commerce.create_category"]  # to be unwound
    assert outcome.goal_result is None

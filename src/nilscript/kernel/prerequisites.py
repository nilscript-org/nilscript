"""Proactive Prerequisite DAG planner (adapter-toolkit-plan §5.3).

`prerequisites` declared on verbs form a dependency DAG. Given a goal verb, the planner
**backward-traverses** from the goal to its unmet prerequisites and orders the chain *before*
execution — so a request for only the last step ("set the price of product Apple") becomes the full
saga it implies (create the category, then the product, then set the price), and the common case
never fails at an unmet dependency mid-flight.

Pure and deterministic: it takes the declared graph plus an `is_satisfied` oracle (the host answers
"does this prerequisite already exist?" — the only place I/O lives) and returns the ordered list of
prerequisite steps to create. A satisfied prerequisite is skipped (and its own sub-tree pruned); a
dependency cycle is refused honestly (PrerequisiteCycle), never walked forever — the same V3
acyclicity invariant the DSL validator enforces on a program graph.

This is the proactive arm of the self-healing axiom; it composes with the reactive `run_repair_loop`
(heal a refusal one prerequisite at a time) and `run_saga_unwind` (compensate in reverse on failure)
in cli/repair.py. The carried ids are resolved at execution time, exactly as the repair loop does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class Prereq:
    """A declared prerequisite of a verb: the entity it depends on existing, the verb that creates
    that entity, and how the created record's field is carried into the dependent step's arg."""

    entity: str          # logical name of the dependency, e.g. "product"
    create_verb: str     # the NIL verb that creates it, e.g. "commerce.create_product"
    carry: str           # "<dependent_arg>-><created_field>", e.g. "product_id->id"


@dataclass(frozen=True)
class PlanStep:
    """One step of the ordered prerequisite saga: create `entity` via `verb`, carrying its id forward."""

    verb: str
    entity: str
    carry: str

    @property
    def target_arg(self) -> str:
        return self.carry.replace("→", "->").split("->", 1)[0].strip()

    @property
    def source_field(self) -> str:
        parts = self.carry.replace("→", "->").split("->", 1)
        return (parts[1] if len(parts) > 1 else "id").strip()


class PrerequisiteCycle(Exception):
    """A dependency loop (A needs B needs A) — an impossible saga. Refused, never walked forever."""


def plan_prerequisites(
    goal_verb: str,
    *,
    prerequisites: Mapping[str, Sequence[Prereq]],
    is_satisfied: Callable[[Prereq], bool],
) -> list[PlanStep]:
    """Backward-traverse the prerequisite DAG from `goal_verb`; return the UNMET prerequisites in
    topological order (deepest first) — the saga to run before the goal (the goal itself is not
    included). `is_satisfied(prereq)` reports whether that dependency already exists, so a satisfied
    one is skipped and its sub-tree pruned. Raises PrerequisiteCycle on a dependency loop."""
    order: list[PlanStep] = []
    placed: set[str] = set()      # entities already scheduled — a shared dependency is created once
    visiting: set[str] = set()    # verbs on the current DFS path — cycle detection

    def visit(verb: str) -> None:
        if verb in visiting:
            raise PrerequisiteCycle(verb)
        visiting.add(verb)
        for prereq in prerequisites.get(verb, ()):  # type: ignore[arg-type]
            if is_satisfied(prereq):
                continue                              # already exists — nothing to create, prune its sub-tree
            visit(prereq.create_verb)                 # post-order: its own prerequisites first
            if prereq.entity not in placed:
                order.append(PlanStep(prereq.create_verb, prereq.entity, prereq.carry))
                placed.add(prereq.entity)
        visiting.discard(verb)

    visit(goal_verb)
    return order


@dataclass
class SagaOutcome:
    """The result of executing a planned prerequisite saga.

    `completed` — every prerequisite and the goal committed.
    `failed`    — a step failed terminally; `committed` lists the steps done so far (in commit order)
                  so the caller can run_saga_unwind() them in reverse — the other arm of self-healing.
    """

    status: str
    committed: list["CommittedRef"] = field(default_factory=list)
    goal_result: dict[str, Any] | None = None
    failed_step: str | None = None


@dataclass(frozen=True)
class CommittedRef:
    """A prerequisite step that committed, and the id it produced (for carrying / compensation)."""

    verb: str
    entity: str
    record_id: Any


# Host-injected: actually run a step (PROPOSE -> COMMIT upstream) and report its created id.
Creator = Callable[[str, dict[str, Any]], dict[str, Any]]   # (verb, args) -> result body
CreatedId = Callable[[dict[str, Any], str], Any]            # (result, source_field) -> the new record id


def _failed(result: dict[str, Any]) -> bool:
    return result.get("outcome") == "refusal" or result.get("state", "").startswith("failed")


def run_prerequisite_saga(
    goal_verb: str,
    goal_args: dict[str, Any],
    plan: Sequence[PlanStep],
    *,
    create: Creator,
    created_id: CreatedId,
) -> SagaOutcome:
    """Execute a planned prerequisite chain in order, then the goal — carrying each created record's
    id into the steps that depend on it (a PlanStep's `carry` says which arg of its dependent receives
    which field). Deterministic and ordered. On a terminal failure it stops and returns the committed
    steps so the caller can compensate (run_saga_unwind) — never pushes a broken chain forward. The
    DATA for each step (names, amounts) is the host's via `create`; this orchestrates the order + carry."""
    carried: dict[str, Any] = {}
    committed: list[CommittedRef] = []
    for step in plan:
        result = create(step.verb, dict(carried))
        if _failed(result):
            return SagaOutcome(status="failed", committed=committed, failed_step=step.verb)
        record_id = created_id(result, step.source_field)
        carried[step.target_arg] = record_id
        committed.append(CommittedRef(verb=step.verb, entity=step.entity, record_id=record_id))

    goal_result = create(goal_verb, {**goal_args, **carried})
    if _failed(goal_result):
        return SagaOutcome(status="failed", committed=committed, failed_step=goal_verb)
    return SagaOutcome(status="completed", committed=committed, goal_result=goal_result)

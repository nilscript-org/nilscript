"""The Cycle AST — the canonical protocol object (docs/PLAN-cycle-ast-ssot.md §1).

A `Cycle` is one business process (`SalesLeadLifecycle`). It EMBEDS today's governed
`kernel.models.Node` union as its `Flow.nodes` — the execution AST is reused verbatim, never
re-defined — and accretes the things an execution object has no business carrying: intent,
ownership, roles, authoring-time policies, declared resources, outcomes, documentation.

Same discipline as `WosoolProgram`: frozen, `extra="forbid"` (via `DslModel`), so an unknown
member is structurally unrepresentable. Execution is one projection of this AST (Cycle → lower →
`WosoolProgram` IR → V1–V6 → executor); the brain is another (Cycle → ontology). Neither owns it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from nilscript.automation.models import TriggerSpec  # REUSE the closed trigger union
from nilscript.kernel.models import (  # EMBED, don't redefine
    BilingualText,
    DslModel,
    Node,
)

# A cycle id is a stable slug (PascalCase or snake/kebab) — the cross-version identity of a process.
CYCLE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]*$"

# Authoring-time governance floor. A policy may only RAISE the tier a node executes at, never lower
# it (the existing kernel invariant). HIGH/CRITICAL escalate the node to a human-approval gate.
PolicyTier = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


class CycleMetadata(DslModel):
    version: str = Field(min_length=1)  # "1.0"
    owner: str = Field(min_length=1)  # "Sales"
    description: BilingualText | None = None


class EntityRef(DslModel):
    """Context binding: a name used inside the cycle → a business entity type, resolved against the
    brain ontology at project-time (Phase 4). Author-declared in v1; no inference."""

    name: str = Field(min_length=1)  # "customer"
    entity_type: str = Field(min_length=1)  # "Customer"


class RoleRef(DslModel):
    role: str = Field(min_length=1)  # "SalesManager"


class PolicyRef(DslModel):
    """An authoring-time governance constraint. `applies_to` names the flow nodes it governs (empty
    ⇒ the cycle's action nodes). `raises_tier` HIGH/CRITICAL escalates those nodes to a human gate
    — the floor only ever rises. `condition` (an expression string, same shape as
    `ConditionNode.expression`) scopes the policy; full data-as-rule grammar is a Phase-4 concern."""

    policy_id: str = Field(min_length=1)
    applies_to: tuple[str, ...] = ()
    condition: str | None = None
    raises_tier: PolicyTier | None = None


class Outcome(DslModel):
    name: str = Field(min_length=1)  # "won"
    when: str | None = None  # expression string; see PolicyRef.condition


class Flow(DslModel):
    """The executable body — the EXISTING `WosoolProgram` node union, embedded unchanged."""

    entry: str = Field(min_length=1)  # a node id (step_N — enforced by the embedded Node)
    nodes: tuple[Node, ...] = Field(min_length=1, max_length=256)


class Cycle(DslModel):
    nil: Literal["cycle/0.1"]
    cycle_id: str = Field(pattern=CYCLE_ID_PATTERN)
    workspace: str = Field(min_length=1)
    metadata: CycleMetadata
    intent: BilingualText  # WHY the cycle exists
    trigger: TriggerSpec  # manual | schedule | event (reused)
    context: tuple[EntityRef, ...] = ()
    roles: tuple[RoleRef, ...] = ()
    policies: tuple[PolicyRef, ...] = ()
    resources: tuple[str, ...] = ()  # declared NIL verbs / adapters this cycle may touch
    outcomes: tuple[Outcome, ...] = ()
    flow: Flow
    documentation: BilingualText | None = None

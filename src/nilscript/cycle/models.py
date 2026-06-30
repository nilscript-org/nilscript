"""The NIL Protocol Model — Cycle AST v0.2 (docs/PLAN-cycle-ast-ssot.md §1).

This is the FROZEN canonical model. Every authoring surface (`.nil` text, the visual canvas, the
LSP) and every consumer (execution, ontology, docs, governance, simulation) is a projection of it.
It does NOT embed the execution IR: the Cycle has its OWN richer step model with **named,
position-independent identifiers** (`CreateLead`, not `step_1`), **named outputs** + **variable
bindings**, **role-bound context actors**, and **first-class approval nodes**. `cycle.compile`
LOWERS this to the hidden `WosoolProgram` IR; the governed validator/executor never change.

Discipline (same as the IR): frozen, `extra="forbid"` — an unknown member is structurally
unrepresentable. Deferred for v0.2 (YAGNI; the kernel already has the nodes, lowering extends
mechanically): `parallel`/`foreach`/`wait`/`call` steps, `imports`, `contracts`, `metrics`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from nilscript.automation.models import TriggerSpec  # REUSE the closed trigger union
from nilscript.kernel.models import BilingualText, DslModel

# A cycle id is a stable PascalCase/slug process identity. A step id is a stable NAME, independent
# of position — the thing that survives reorder/refactor/formatting (unlike the IR's positional
# `step_N`). A variable is a lowercase binding name.
CYCLE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]*$"
STEP_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"
VAR_PATTERN = r"^[a-z][a-zA-Z0-9_]*$"
VERB_PATTERN = r"^[a-z]+\.[a-z_]+$"

PolicyTier = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


class CycleMetadata(DslModel):
    version: str = Field(min_length=1)  # "1.3.2"
    owner: str = Field(min_length=1)  # "Sales Team"
    description: BilingualText | None = None
    tags: tuple[str, ...] = ()


class EntityRef(DslModel):
    """A context binding: a name used in the cycle → a business entity type, optionally bound to a
    role (`approver: User (role: SalesManager)`). Resolved against the ontology at project-time."""

    name: str = Field(min_length=1)  # "approver"
    entity_type: str = Field(min_length=1)  # "User"
    role: str | None = None  # "SalesManager"


class RoleRef(DslModel):
    role: str = Field(min_length=1)


class VariableBinding(DslModel):
    """`let payload = context.payload` — a named binding over the run context. The expression is a
    dotted path (`context.payload`, `context.input`); it lowers to an IR data reference."""

    name: str = Field(pattern=VAR_PATTERN)
    expression: str = Field(min_length=1)


class PolicyRef(DslModel):
    """An authoring-time governance constraint. `applies_to` names the steps it governs;
    `raises_tier` HIGH/CRITICAL escalates them to a human gate — the floor only ever rises."""

    policy_id: str = Field(min_length=1)
    applies_to: tuple[str, ...] = ()
    condition: str | None = None
    raises_tier: PolicyTier | None = None


class Outcome(DslModel):
    name: str = Field(min_length=1)  # "won"
    when: str | None = None  # expression string


# ── Steps: the Flow node union, each carrying a stable NAME id ───────────────────────────────


class StepRetry(DslModel):
    max_attempts: int = Field(ge=1, le=10)
    backoff: Literal["exponential", "fixed"] = "exponential"
    initial_seconds: float = Field(default=2.0, ge=0)


class StepErrorPolicy(DslModel):
    action: Literal["halt", "continue", "route", "compensate"]
    to: str | None = Field(default=None, pattern=STEP_ID_PATTERN)  # a STEP NAME


class StepCompensate(DslModel):
    use: str = Field(pattern=VERB_PATTERN)
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


class ActionStep(DslModel):
    id: str = Field(pattern=STEP_ID_PATTERN)
    type: Literal["action"]
    use: str = Field(pattern=VERB_PATTERN)  # "odoo.crm_create_lead"
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    output: str | None = Field(default=None, pattern=VAR_PATTERN)  # "lead"
    next: str | None = Field(default=None, pattern=STEP_ID_PATTERN)
    retry: StepRetry | None = None
    on_error: StepErrorPolicy | None = None
    compensate: StepCompensate | None = None


class QueryStep(DslModel):
    id: str = Field(pattern=STEP_ID_PATTERN)
    type: Literal["query"]
    use: str = Field(pattern=VERB_PATTERN)
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    output: str | None = Field(default=None, pattern=VAR_PATTERN)
    next: str | None = Field(default=None, pattern=STEP_ID_PATTERN)


class DecisionStep(DslModel):
    """A branch (the kernel's ConditionNode). `when` is a guard expression over bindings."""

    id: str = Field(pattern=STEP_ID_PATTERN)
    type: Literal["decision"]
    when: str = Field(min_length=1)
    on_true: str = Field(pattern=STEP_ID_PATTERN)
    on_false: str | None = Field(default=None, pattern=STEP_ID_PATTERN)
    next: str | None = Field(default=None, pattern=STEP_ID_PATTERN)


class ApprovalStep(DslModel):
    """A first-class human gate (the kernel's AwaitApprovalNode). The gate IS the node — there is no
    separate runtime gate. `approver` names a context actor (a role-bound EntityRef)."""

    id: str = Field(pattern=STEP_ID_PATTERN)
    type: Literal["approval"]
    title: BilingualText
    description: BilingualText | None = None
    approver: str = Field(min_length=1)
    timeout_seconds: int = Field(default=86400, ge=1, le=2_592_000)
    on_approve: str = Field(pattern=STEP_ID_PATTERN)
    on_reject: str | None = Field(default=None, pattern=STEP_ID_PATTERN)
    on_timeout: str | None = Field(default=None, pattern=STEP_ID_PATTERN)


class NotifyStep(DslModel):
    id: str = Field(pattern=STEP_ID_PATTERN)
    type: Literal["notify"]
    message: BilingualText
    next: str | None = Field(default=None, pattern=STEP_ID_PATTERN)


CycleStepType = ActionStep | QueryStep | DecisionStep | ApprovalStep | NotifyStep
CycleStep = Annotated[CycleStepType, Field(discriminator="type")]


class Flow(DslModel):
    entry: str = Field(pattern=STEP_ID_PATTERN)  # a step NAME
    steps: tuple[CycleStep, ...] = Field(min_length=1, max_length=256)


class Cycle(DslModel):
    nil: Literal["cycle/0.2"]
    cycle_id: str = Field(pattern=CYCLE_ID_PATTERN)
    workspace: str = Field(min_length=1)
    metadata: CycleMetadata
    intent: BilingualText
    trigger: TriggerSpec
    context: tuple[EntityRef, ...] = ()
    variables: tuple[VariableBinding, ...] = ()
    roles: tuple[RoleRef, ...] = ()
    policies: tuple[PolicyRef, ...] = ()
    resources: tuple[str, ...] = ()
    outcomes: tuple[Outcome, ...] = ()
    flow: Flow
    documentation: BilingualText | None = None

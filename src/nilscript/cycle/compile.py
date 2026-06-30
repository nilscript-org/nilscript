"""`compile_cycle` — the execution projection of the NIL Protocol Model (Cycle AST v0.2).

The governance invariant, unchanged and non-negotiable:

    Cycle → lower → WosoolProgram IR → V1–V6 validate → content-hash → (propose→commit per step)

Lowering is where the protocol's richer surface collapses onto the hidden IR:
  - **named steps → positional `step_N`** (the IR ids; the protocol keeps the stable names),
  - **named-output / variable references → `$.step_N.output.*` / `$.input.*`** data references,
  - **approval steps → `AwaitApprovalNode`**, decisions → `ConditionNode`, etc.
Then the UNCHANGED kernel validator admits or refuses the IR with the existing taxonomy, so a verb
the backend never declared still has nothing to bind to (V4). The governed core stays byte-for-byte
intact — this module only *projects* into it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nilscript.cycle.hash import cycle_content_hash
from nilscript.cycle.models import (
    ActionStep,
    ApprovalStep,
    Cycle,
    DecisionStep,
    NotifyStep,
    QueryStep,
)
from nilscript.kernel.context import ValidationContext
from nilscript.kernel.diagnostics import ValidationResult
from nilscript.kernel.models import WosoolProgram
from nilscript.kernel.validator import validate

# An identifier path (`lead`, `lead.id`, `payload.name`) — a candidate reference. A value carrying
# spaces/quotes/operators is a literal and never rewritten.
_PATH = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_GATING_TIERS = frozenset({"HIGH", "CRITICAL"})


@dataclass(frozen=True)
class CompileResult:
    """Outcome of lowering + validating a Cycle. `ok` mirrors the validator verdict; on failure
    `program` is None and `diagnostics` carries the structured refusal. `content_hash` is over the
    Cycle AST (the version lock). `gates` are the IR node ids that require human approval (approval
    steps + policy-escalated steps). `step_ids` maps each protocol step name → its IR `step_N` id,
    so other projections can correlate the two surfaces."""

    ok: bool
    diagnostics: ValidationResult
    program: WosoolProgram | None = None
    content_hash: str | None = None
    gates: tuple[str, ...] = ()
    step_ids: dict[str, str] = field(default_factory=dict)


def _var_prefix(expression: str) -> str:
    """`context.payload` → `$.input.payload`; `context` → `$.input`. The trigger payload lands as the
    run `input`, so a context-rooted binding resolves into it."""
    head, _, tail = expression.partition(".")
    if head in ("context", "input"):
        return "$.input" + (f".{tail}" if tail else "")
    return "$.input." + expression  # best-effort for other roots


def _resolve(value: object, outputs: dict[str, str], variables: dict[str, str]) -> object:
    """Rewrite a protocol-level reference (`lead.id`, `payload.name`) into an IR data reference
    (`$.step_1.output.id`, `$.input.payload.name`). A literal (unknown head, or non-identifier
    string) is returned untouched. Recurses into dicts/lists."""
    if isinstance(value, dict):
        return {k: _resolve(v, outputs, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, outputs, variables) for v in value]
    if not isinstance(value, str) or not _PATH.match(value):
        return value
    head, _, tail = value.partition(".")
    suffix = f".{tail}" if tail else ""
    if head in outputs:
        return f"$.{outputs[head]}.output{suffix}"
    if head in variables:
        return variables[head] + suffix
    return value  # a literal (e.g. "default", an event name)


def _lower(cycle: Cycle) -> tuple[dict, dict[str, str], tuple[str, ...]]:
    """Cycle AST → (raw WosoolProgram dict, name→step_N map, approval gate ids). Steps are processed
    in declaration order, with the output map updated AFTER each step so a step can never reference
    its own output and a re-declared name resolves to the most recent prior producer."""
    name2id = {step.id: f"step_{i + 1}" for i, step in enumerate(cycle.flow.steps)}
    variables = {vb.name: _var_prefix(vb.expression) for vb in cycle.variables}
    outputs: dict[str, str] = {}
    pipeline: list[dict] = []
    gates: list[str] = []

    def nid(name: str | None) -> str | None:
        return name2id.get(name) if name else None

    for step in cycle.flow.steps:
        sid = name2id[step.id]
        if isinstance(step, ActionStep | QueryStep):
            node: dict = {
                "id": sid,
                "type": step.type,
                "verb": step.use,
                "args": _resolve(step.with_, outputs, variables),
            }
            if isinstance(step, ActionStep):
                node["skill"] = step.use.split(".", 1)[0]
                if step.retry is not None:
                    node["retry_policy"] = step.retry.model_dump()
                if step.on_error is not None:
                    node["on_error"] = {"action": step.on_error.action, "to": nid(step.on_error.to)}
                if step.compensate is not None:
                    node["compensate_with"] = {
                        "verb": step.compensate.use,
                        "args": _resolve(step.compensate.with_, outputs, variables),
                    }
            if step.next is not None:
                node["next"] = nid(step.next)
            pipeline.append(node)
            if step.output:
                outputs[step.output] = sid  # visible to LATER steps only
        elif isinstance(step, DecisionStep):
            node = {
                "id": sid,
                "type": "condition",
                "expression": step.when,
                "on_true": nid(step.on_true),
            }
            if step.on_false is not None:
                node["on_false"] = nid(step.on_false)
            if step.next is not None:
                node["next"] = nid(step.next)
            pipeline.append(node)
        elif isinstance(step, ApprovalStep):
            node = {
                "id": sid,
                "type": "await_approval",
                "proposal": step.title.en or step.title.ar,
                "timeout_seconds": step.timeout_seconds,
                "on_approved": nid(step.on_approve),
            }
            if step.on_reject is not None:
                node["on_rejected"] = nid(step.on_reject)
            if step.on_timeout is not None:
                node["on_timeout"] = nid(step.on_timeout)
            pipeline.append(node)
            gates.append(sid)  # the approval node IS the gate
        elif isinstance(step, NotifyStep):
            node = {"id": sid, "type": "notify", "message": step.message.model_dump()}
            if step.next is not None:
                node["next"] = nid(step.next)
            pipeline.append(node)

    raw = {
        "wosool": "0.1",
        "workspace": cycle.workspace,
        "entry": name2id[cycle.flow.entry],
        "pipeline": pipeline,
    }
    return raw, name2id, tuple(gates)


def _policy_gates(cycle: Cycle, name2id: dict[str, str]) -> list[str]:
    """IR ids of steps a policy escalates to HIGH/CRITICAL (floor only rises). An empty `applies_to`
    scopes to every effecting (action/query) step."""
    effecting = [
        name2id[s.id] for s in cycle.flow.steps if isinstance(s, ActionStep | QueryStep)
    ]
    out: list[str] = []
    for policy in cycle.policies:
        if policy.raises_tier not in _GATING_TIERS:
            continue
        targets = [name2id[n] for n in policy.applies_to if n in name2id] if policy.applies_to else effecting
        for sid in targets:
            if sid not in out:
                out.append(sid)
    return out


def compile_cycle(cycle: Cycle, ctx: ValidationContext) -> CompileResult:
    """Lower the Cycle to the governed IR and run the unchanged V1–V6 validator. No side effect."""
    raw, name2id, approval_gates = _lower(cycle)
    result = validate(raw, ctx)
    if not result.ok:
        return CompileResult(ok=False, diagnostics=result, step_ids=name2id)

    program = WosoolProgram.model_validate(raw)
    gates = list(approval_gates)
    for sid in _policy_gates(cycle, name2id):
        if sid not in gates:
            gates.append(sid)
    return CompileResult(
        ok=True,
        diagnostics=result,
        program=program,
        content_hash=cycle_content_hash(cycle),
        gates=tuple(gates),
        step_ids=name2id,
    )

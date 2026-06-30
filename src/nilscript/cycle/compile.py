"""`compile_cycle` — the execution projection of the Cycle AST (docs/PLAN-cycle-ast-ssot.md §1).

The governance invariant, unchanged and non-negotiable:

    Cycle → lower → WosoolProgram IR → V1–V6 validate → content-hash → (propose→commit per node)

Lowering is near-identity: `flow.nodes` become `WosoolProgram.pipeline`, `flow.entry` the program
entry, `cycle.workspace` the program workspace. The UNCHANGED kernel validator then admits or
refuses the IR with the existing diagnostic taxonomy (`V4_UNKNOWN_SKILL`, `V4_SCOPE_DENIED`, …), so
a verb the backend never declared still has nothing to bind to. The governed core stays
byte-for-byte intact — this module only *projects* into it.

Authoring-time `policies` that raise a node's tier floor to HIGH/CRITICAL are surfaced as the
`gates` set — the nodes that will require human approval at propose-time. The floor only ever rises
(never falls), per the existing kernel invariant. Phase 2 turns each gate into a governed
`AwaitApprovalNode` on the visual surface; Phase 1 proves the derivation in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from nilscript.cycle.hash import cycle_content_hash
from nilscript.cycle.models import Cycle
from nilscript.kernel.context import ValidationContext
from nilscript.kernel.diagnostics import ValidationResult
from nilscript.kernel.models import ActionNode, QueryNode, WosoolProgram
from nilscript.kernel.validator import validate

# Tiers that escalate a node to a human-approval gate. Anything below executes governed-but-unattended.
_GATING_TIERS = frozenset({"HIGH", "CRITICAL"})


@dataclass(frozen=True)
class CompileResult:
    """Outcome of lowering + validating a Cycle. `ok` mirrors the validator verdict; on failure
    `program` is None and `diagnostics` carries the structured refusal (which node, which verb,
    why). `content_hash` is over the Cycle AST (the version lock). `gates` are the node ids the
    cycle's policies escalate to human approval."""

    ok: bool
    diagnostics: ValidationResult
    program: WosoolProgram | None = None
    content_hash: str | None = None
    gates: tuple[str, ...] = ()


def _lower(cycle: Cycle) -> dict:
    """Cycle AST → raw `WosoolProgram` dict. `by_alias` keeps `ForeachNode.as_` serialised as `as`
    so the embedded nodes round-trip through the kernel schema unchanged."""
    return {
        "wosool": "0.1",
        "workspace": cycle.workspace,
        "entry": cycle.flow.entry,
        "pipeline": [node.model_dump(by_alias=True, mode="json") for node in cycle.flow.nodes],
    }


def _gates(cycle: Cycle) -> tuple[str, ...]:
    """Node ids escalated to human approval by a policy raising the tier floor to HIGH/CRITICAL.

    An empty `applies_to` scopes the policy to the cycle's effecting nodes (action/query); an
    explicit `applies_to` names the governed nodes. Order-preserving and de-duplicated."""
    effecting = [
        node.id for node in cycle.flow.nodes if isinstance(node, ActionNode | QueryNode)
    ]
    gated: list[str] = []
    for policy in cycle.policies:
        if policy.raises_tier not in _GATING_TIERS:
            continue
        targets = list(policy.applies_to) if policy.applies_to else effecting
        for node_id in targets:
            if node_id not in gated:
                gated.append(node_id)
    return tuple(gated)


def compile_cycle(cycle: Cycle, ctx: ValidationContext) -> CompileResult:
    """Lower the Cycle to the governed IR and run the unchanged V1–V6 validator.

    No side effect — the deterministic boundary. A cycle that fails validation never produces a
    program; one that passes carries its AST content-hash and its policy-derived approval gates.
    """
    raw_program = _lower(cycle)
    result = validate(raw_program, ctx)
    if not result.ok:
        return CompileResult(ok=False, diagnostics=result)

    program = WosoolProgram.model_validate(raw_program)
    return CompileResult(
        ok=True,
        diagnostics=result,
        program=program,
        content_hash=cycle_content_hash(cycle),
        gates=_gates(cycle),
    )

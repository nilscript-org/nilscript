"""The Cycle AST — the canonical SSOT protocol object (docs/PLAN-cycle-ast-ssot.md).

`Cycle` embeds the governed `WosoolProgram` node union as its `Flow`; `compile_cycle` lowers it to
the IR and runs the unchanged kernel validator; `cycle_content_hash` locks the version over the AST.
Execution, ontology, docs, and governance are all projections of this one object.
"""

from __future__ import annotations

from nilscript.cycle.compile import CompileResult, compile_cycle
from nilscript.cycle.hash import cycle_content_hash
from nilscript.cycle.models import (
    Cycle,
    CycleMetadata,
    EntityRef,
    Flow,
    Outcome,
    PolicyRef,
    RoleRef,
)

__all__ = [
    "Cycle",
    "CycleMetadata",
    "EntityRef",
    "Flow",
    "Outcome",
    "PolicyRef",
    "RoleRef",
    "CompileResult",
    "compile_cycle",
    "cycle_content_hash",
]

"""The Cycle AST — the canonical SSOT protocol object (docs/PLAN-cycle-ast-ssot.md).

`Cycle` embeds the governed `WosoolProgram` node union as its `Flow`; `compile_cycle` lowers it to
the IR and runs the unchanged kernel validator; `cycle_content_hash` locks the version over the AST.
Execution, ontology, docs, and governance are all projections of this one object.
"""

from __future__ import annotations

from nilscript.cycle.authoring import (
    CycleDraftResult,
    cycle_slug,
    draft_cycle,
    register_cycle,
)
from nilscript.cycle.compile import CompileResult, compile_cycle
from nilscript.cycle.hash import cycle_content_hash
from nilscript.cycle.lsp import (
    completions,
    diagnostics,
    hover,
    semantic_tokens,
)
from nilscript.cycle.nil_parser import NilSyntaxError, parse_nil
from nilscript.cycle.nil_printer import print_nil
from nilscript.cycle.models import (
    ActionStep,
    ApprovalStep,
    Cycle,
    CycleMetadata,
    DecisionStep,
    EntityRef,
    Flow,
    NotifyStep,
    Outcome,
    PolicyRef,
    QueryStep,
    RoleRef,
    VariableBinding,
)
from nilscript.cycle.projections import (
    governance_report,
    simulate,
    to_markdown,
    to_mermaid,
)
from nilscript.cycle.registry import DeadReference, ProtocolRegistry, Symbol

__all__ = [
    "Cycle",
    "CycleMetadata",
    "EntityRef",
    "Flow",
    "Outcome",
    "PolicyRef",
    "RoleRef",
    "VariableBinding",
    "ActionStep",
    "QueryStep",
    "DecisionStep",
    "ApprovalStep",
    "NotifyStep",
    "CompileResult",
    "compile_cycle",
    "cycle_content_hash",
    "print_nil",
    "parse_nil",
    "NilSyntaxError",
    "ProtocolRegistry",
    "Symbol",
    "DeadReference",
    "to_mermaid",
    "to_markdown",
    "simulate",
    "governance_report",
    "CycleDraftResult",
    "cycle_slug",
    "draft_cycle",
    "register_cycle",
    "diagnostics",
    "completions",
    "hover",
    "semantic_tokens",
]

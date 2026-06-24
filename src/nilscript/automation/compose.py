"""Cross-system composition (P3): one automation spanning N adapters, with explicit data handoff.

This is the sales→marketing→accounting shape. A composed plan is an ordered list of **stages**, each
a normal single-adapter `WosoolProgram` validated against *its own* adapter's skeleton and run by the
existing `LocalExecutor`. Between stages, named outputs are threaded forward by an **explicit**
`input_from` mapping (e.g. `{"lead_ref": "$.stage_1.step_2.output.id"}`) — surfaced as `$.input.*`
in the next stage.

Two honesty boundaries make this safe rather than magic:
- **Semantic mapping is author-declared, not inferred.** We do not pretend system A's "customer" is
  system B's "account"; the composer states the handoff, and B's own Choice Gate still resolves the
  handed value to a verified id on B's side. No silent ontology guessing.
- **Authority is per-stage, not composed.** Each stage runs against its own adapter with that
  adapter's own credentials (the `run_stage` the caller supplies binds them). The handoff carries
  *data values*, never authority — so a composed plan cannot escalate across a boundary
  (confused-deputy-safe by construction). What it CANNOT yet express — a single stage that itself
  needs two backends' authority at once — is deliberately out of scope.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nilscript.automation.skeleton import context_from_skeleton
from nilscript.kernel.diagnostics import Diagnostic
from nilscript.kernel.executor import RunResult
from nilscript.kernel.validator import validate


def _handoff_source(ref: str) -> str | None:
    """The stage a handoff ref reads from — `$.stage_1.step_2.output.id` → `stage_1`. None if not a
    reference. (The kernel's parser whitelists `step_N`/`input`/`item` sources; cross-stage refs use
    stage names, so composition resolves them itself.)"""
    if isinstance(ref, str) and ref.startswith("$."):
        return ref[2:].split(".", 1)[0]
    return None


def _resolve_handoff(ref: Any, ctx: dict[str, Any]) -> Any:
    """Walk a `$.stage.path.to.value` reference through the accumulated cross-stage context. A
    non-reference is returned as-is; an unresolvable path yields None (never a fabricated value)."""
    if not (isinstance(ref, str) and ref.startswith("$.")):
        return ref
    cursor: Any = ctx
    for segment in ref[2:].split("."):
        if isinstance(cursor, dict) and segment in cursor:
            cursor = cursor[segment]
        else:
            return None
    return cursor

# Runs one stage's plan against `adapter` and returns its RunResult.
# Called as: run_stage(adapter, plan, run_id=..., input=...).
StageRunner = Callable[..., Awaitable[RunResult]]


@dataclass(frozen=True)
class Stage:
    """One system's slice of a composed automation."""

    name: str  # routing label, must match STAGE_NAME_PATTERN (e.g. "stage_1")
    adapter: str  # adapter_id in the registry — which backend this stage runs against
    plan: dict[str, Any]  # a WosoolProgram dict, validated against `adapter`'s skeleton
    input_from: dict[str, str] = field(default_factory=dict)  # {input_key: "$.stage_k...output.f"}


@dataclass(frozen=True)
class ComposedPlan:
    workspace: str
    stages: tuple[Stage, ...]


@dataclass
class ComposedResult:
    """Outcome of a composed run. `completed` is True only if every stage completed; a stage that
    blocks stops the chain honestly (downstream stages do not run)."""

    completed: bool
    stages: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    blocked_at: str | None = None


def composed_hash(raw: dict[str, Any]) -> str:
    """Version lock for a composed plan — SHA256 over its canonical JSON (same discipline as a single
    plan's `content_hash`)."""
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_composed(raw: dict[str, Any]) -> ComposedPlan:
    """Build a ComposedPlan from raw JSON ({workspace, stages:[{name, adapter, plan, input_from}]})."""
    stages = tuple(
        Stage(
            name=s["name"], adapter=s["adapter"], plan=s["plan"],
            input_from=dict(s.get("input_from") or {}),
        )
        for s in raw.get("stages", [])
    )
    return ComposedPlan(workspace=raw["workspace"], stages=stages)


def validate_composed(
    composed: ComposedPlan, skeleton_for: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Validate every stage's plan against ITS adapter's skeleton, plus handoff well-formedness.

    `skeleton_for` maps adapter_id → that adapter's discovery skeleton. Returns
    `{"ok": bool, "stages": [{name, ok, diagnostics}], "errors": [...]}`. A handoff `input_from` that
    references a stage not declared *before* the consuming stage is a composition error.
    """
    seen: set[str] = set()
    stage_reports: list[dict[str, Any]] = []
    errors: list[str] = []
    if len({s.name for s in composed.stages}) != len(composed.stages):
        errors.append("duplicate stage names")

    for stage in composed.stages:
        skeleton = skeleton_for.get(stage.adapter)
        if skeleton is None:
            errors.append(f"stage {stage.name!r}: no skeleton for adapter {stage.adapter!r}")
            stage_reports.append({"name": stage.name, "ok": False, "diagnostics": []})
            seen.add(stage.name)
            continue
        ctx = context_from_skeleton(composed.workspace, skeleton)
        result = validate(stage.plan, ctx)
        diags = [_diag(d) for d in result.diagnostics]
        # handoff: every `$.stage_k...` source must be a stage declared earlier in the chain.
        for ref_str in (stage.input_from or {}).values():
            source = _handoff_source(ref_str)
            if source is not None and source not in seen and source not in ("input", "item"):
                errors.append(
                    f"stage {stage.name!r}: input_from references {source!r} which is not a prior stage"
                )
        stage_reports.append({"name": stage.name, "ok": result.ok, "diagnostics": diags})
        seen.add(stage.name)

    ok = not errors and all(r["ok"] for r in stage_reports)
    return {"ok": ok, "stages": stage_reports, "errors": errors}


def _diag(d: Diagnostic) -> dict[str, Any]:
    return {"code": d.code, "severity": d.severity, "message": d.message, "node": d.node}


def _stage_input(stage: Stage, ctx: dict[str, Any]) -> dict[str, Any]:
    """Resolve a stage's handoff mapping against the accumulated cross-stage context."""
    return {key: _resolve_handoff(ref, ctx) for key, ref in (stage.input_from or {}).items()}


async def run_composed(
    composed: ComposedPlan, *, run_stage: StageRunner, run_id: str
) -> ComposedResult:
    """Run each stage in order against its adapter, threading declared outputs into the next stage.

    The cross-stage context is `{stage_name: <that stage's executor context>}`, so a handoff ref like
    `$.stage_1.step_2.output.id` resolves to stage_1's step_2 output. A non-completing stage halts the
    chain (honest partial) — never fabricates a downstream write on a missing handoff.
    """
    ctx: dict[str, Any] = {}
    out_stages: list[dict[str, Any]] = []
    for stage in composed.stages:
        stage_input = _stage_input(stage, ctx)
        result = await run_stage(
            stage.adapter, stage.plan, run_id=f"{run_id}:{stage.name}", input=stage_input
        )
        ctx[stage.name] = result.context
        out_stages.append({
            "name": stage.name,
            "adapter": stage.adapter,
            "completed": result.completed,
            "blocked_at": result.blocked_at,
            "refusal": result.refusal,
        })
        if not result.completed:
            return ComposedResult(
                completed=False, stages=out_stages, context=ctx, blocked_at=stage.name
            )
    return ComposedResult(completed=True, stages=out_stages, context=ctx)

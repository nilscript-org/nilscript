"""The cycle authoring loop: draft (compile, no effect) → register (persist to the SSOT).

Mirrors `automation.authoring` exactly — because a registered cycle IS an automation row, with
`kind='cycle'` and the canonical Cycle AST kept in the `source` column (the lowered `WosoolProgram`
lives in `plan`, derived). This is the Phase-2 spine that closes the governance drift: the visual
surface registers THROUGH the kernel (compile → V1–V6 → content-hash → persist), and runs reuse the
existing `fire_manual` path — there is no second executor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from nilscript.cycle.compile import compile_cycle
from nilscript.cycle.models import Cycle
from nilscript.kernel.context import ValidationContext
from nilscript.kernel.diagnostics import ValidationResult
from nilscript.kernel.models import WosoolProgram


class _Store(Protocol):
    def register_automation(
        self,
        *,
        workspace: str,
        automation_id: str,
        content_hash: str,
        name: dict[str, Any],
        plan: dict[str, Any],
        trigger: dict[str, Any],
        state: str = ...,
        kind: str = ...,
        authored_by: str = ...,
        description: dict[str, Any] | None = ...,
        approved_by: str | None = ...,
        source: dict[str, Any] | None = ...,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CycleDraftResult:
    """Outcome of a draft attempt. `ok` mirrors the compiler/validator verdict; on failure `cycle`
    and `program` are None and `diagnostics` carries the structured refusal."""

    ok: bool
    diagnostics: ValidationResult
    cycle: Cycle | None = None
    content_hash: str | None = None
    program: WosoolProgram | None = None
    gates: tuple[str, ...] = ()


def cycle_slug(cycle_id: str) -> str:
    """A stable automation_id for a cycle: lowercase, with anything outside `[a-z0-9_-]` collapsed to
    a dash. Matches `automation.models.AUTOMATION_ID_PATTERN` so the cycle shares the SSOT keyspace."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", cycle_id.lower()).strip("-")
    return slug or "cycle"


def draft_cycle(*, raw_cycle: Any, ctx: ValidationContext) -> CycleDraftResult:
    """Parse + compile a candidate cycle against the live context. No side effect — the deterministic
    boundary. A cycle that fails compilation never produces a program; one that passes carries its
    AST content-hash, the derived program, and its policy-derived approval gates."""
    cycle = Cycle.model_validate(raw_cycle)  # V1 structural gate for the protocol object
    res = compile_cycle(cycle, ctx)
    if not res.ok:
        return CycleDraftResult(ok=False, diagnostics=res.diagnostics)
    return CycleDraftResult(
        ok=True,
        diagnostics=res.diagnostics,
        cycle=cycle,
        content_hash=res.content_hash,
        program=res.program,
        gates=res.gates,
    )


def register_cycle(
    store: _Store, draft: CycleDraftResult, *, state: str = "pending_approval", authored_by: str = ""
) -> dict[str, Any]:
    """Persist a passing cycle draft to the SSOT and return the stored row.

    Registering a cycle is a governed act, so it lands in `pending_approval` by default (never
    auto-armed). The content-hash lock is over the Cycle AST; re-registering an identical cycle is an
    idempotent no-op (same hash ⇒ no new version)."""
    if not draft.ok or draft.cycle is None or draft.program is None:
        raise ValueError("cannot register a cycle that failed to compile")
    cycle = draft.cycle
    return store.register_automation(
        workspace=cycle.workspace,
        automation_id=cycle_slug(cycle.cycle_id),
        content_hash=draft.content_hash,
        kind="cycle",
        name=cycle.intent.model_dump(),
        plan=draft.program.model_dump(by_alias=True, mode="json"),
        trigger=cycle.trigger.model_dump(),
        source=cycle.model_dump(by_alias=True, mode="json"),
        state=state,
        authored_by=authored_by,
    )

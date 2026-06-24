"""The authoring loop: draft (validate + hash, no effect) → register (persist to SSOT).

`draft_automation` is the deterministic boundary. The agent supplies a raw plan + trigger
(untrusted); the kernel validator (V1-V6) lowers-or-rejects it against the live skeleton. Only a
plan that passes becomes a registrable `AutomationDefinition` carrying its content-hash. The agent
cannot talk past a refusal — a hallucinated verb has nothing to bind to (V4).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Protocol

from nilscript.automation.models import (
    AutomationDefinition,
    AutomationState,
    BilingualText,
    content_hash,
    parse_trigger,
)
from nilscript.kernel.context import ValidationContext
from nilscript.kernel.diagnostics import ValidationResult
from nilscript.kernel.models import WosoolProgram
from nilscript.kernel.validator import validate


class _Store(Protocol):
    """The slice of the control-plane store the registry needs (keeps this module store-agnostic)."""

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
        authored_by: str = ...,
        description: dict[str, Any] | None = ...,
        approved_by: str | None = ...,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DraftResult:
    """Outcome of a draft attempt. `ok` mirrors the validator verdict; on failure `definition` is
    None and `diagnostics` carries the structured refusal (which node, which verb, why)."""

    ok: bool
    diagnostics: ValidationResult
    definition: AutomationDefinition | None = None
    content_hash: str | None = None


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def draft_automation(
    *,
    automation_id: str,
    name: Any,
    raw_plan: dict[str, Any],
    trigger: Any,
    ctx: ValidationContext,
    authored_by: str = "",
    description: Any | None = None,
) -> DraftResult:
    """Validate the agent's candidate plan and, if admitted, build a version-1 draft definition.

    No side effect. The workspace is taken from the validated plan so the two cannot drift.
    """
    result = validate(raw_plan, ctx)
    if not result.ok:
        return DraftResult(ok=False, diagnostics=result)

    program = WosoolProgram.model_validate(raw_plan)
    digest = content_hash(program)
    definition = AutomationDefinition(
        automation_id=automation_id,
        workspace=program.workspace,
        version=1,
        content_hash=digest,
        name=name if isinstance(name, BilingualText) else BilingualText.model_validate(name),
        description=(
            None
            if description is None
            else description
            if isinstance(description, BilingualText)
            else BilingualText.model_validate(description)
        ),
        plan=program,
        trigger=parse_trigger(trigger),
        state="draft",
        authored_by=authored_by,
        created_at=_now(),
    )
    return DraftResult(ok=True, diagnostics=result, definition=definition, content_hash=digest)


def register(
    store: _Store,
    definition: AutomationDefinition,
    *,
    state: AutomationState = "pending_approval",
) -> AutomationDefinition:
    """Persist a drafted definition to the SSOT and return the canonical stored version.

    Registering a recurring automation is a governed act, so it lands in `pending_approval` by
    default — never auto-armed. Re-registering an identical plan (same hash) is an idempotent no-op.
    """
    row = store.register_automation(
        workspace=definition.workspace,
        automation_id=definition.automation_id,
        content_hash=definition.content_hash,
        name=definition.name.model_dump(),
        description=definition.description.model_dump() if definition.description else None,
        plan=definition.plan.model_dump(by_alias=True, mode="json"),
        trigger=definition.trigger.model_dump(),
        state=state,
        authored_by=definition.authored_by,
    )
    return AutomationDefinition.from_row(row)

"""The SSOT record for an automation, its trigger union, and the version-lock content hash.

Mirrors the kernel's DSL discipline (frozen, unknown members rejected). The `content_hash` over the
canonical-JSON of the validated program IS the "lock on a version": a run months later executes
exactly the bytes that were approved, and any out-of-band edit makes the hash mismatch.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from nilscript.kernel.models import (
    VERB_PATTERN,
    BilingualText,
    DslModel,
    WosoolProgram,
)

# Workspace-scoped slug: lowercase, digits, dashes/underscores. Stable across versions.
AUTOMATION_ID_PATTERN = r"^[a-z][a-z0-9_-]*$"

AutomationState = Literal["draft", "pending_approval", "active", "paused", "archived"]


class ScheduleTrigger(DslModel):
    """Fire on a clock. Both `interval_seconds` and `cron` fire locally via `POST /automations/tick`
    (cron uses a self-contained subset matcher — see `triggers.cron_matches`). Temporal Schedules are
    the durable cloud upgrade for the same TriggerSpec."""

    type: Literal["schedule"]
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str = "Asia/Riyadh"

    @model_validator(mode="after")
    def _one_of_cron_or_interval(self) -> ScheduleTrigger:
        if (self.cron is None) == (self.interval_seconds is None):
            raise ValueError("schedule trigger needs exactly one of `cron` or `interval_seconds`")
        return self


class EventTrigger(DslModel):
    """Fire when a matching NIL event lands in the control-plane ledger (P2 dispatcher)."""

    type: Literal["event"]
    on_verb: str = Field(pattern=VERB_PATTERN)
    on_event: Literal["executed", "refused", "rolled_back"] = "executed"
    match: dict[str, Any] = Field(default_factory=dict)
    source_adapter: str | None = None


class ManualTrigger(DslModel):
    """Fire only on an explicit run request. The one trigger P1 supports end to end."""

    type: Literal["manual"]


TriggerType = ScheduleTrigger | EventTrigger | ManualTrigger
TriggerSpec = Annotated[TriggerType, Field(discriminator="type")]

_TRIGGER_ADAPTER: TypeAdapter[TriggerType] = TypeAdapter(TriggerSpec)


def parse_trigger(raw: Any) -> TriggerType:
    """Parse a raw trigger (dict from the agent, or an already-built model) into the closed union.

    An unknown `type` is structurally unrepresentable — the discriminated union rejects it.
    """
    if isinstance(raw, ScheduleTrigger | EventTrigger | ManualTrigger):
        return raw
    return _TRIGGER_ADAPTER.validate_python(raw)


def content_hash(plan: WosoolProgram) -> str:
    """SHA256 over the canonical JSON of the validated program — the version lock.

    `by_alias=True` so `ForeachNode.as_` serialises as `as`; `sort_keys` + tight separators make the
    encoding canonical so identical plans hash identically (idempotent registration).
    """
    canonical = json.dumps(
        plan.model_dump(by_alias=True, mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AutomationDefinition(DslModel):
    """One version of one automation — the row that lives in the SSOT.

    `version` and `created_at` are authoritative once persisted; a freshly drafted (unstored)
    definition carries version 1 and a provisional `created_at`. The plan's `workspace` is the
    automation's workspace (enforced below) so the two can never drift.
    """

    automation_id: str = Field(pattern=AUTOMATION_ID_PATTERN)
    workspace: str = Field(min_length=1)
    version: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)
    name: BilingualText
    description: BilingualText | None = None
    plan: WosoolProgram
    trigger: TriggerSpec
    state: AutomationState
    authored_by: str = ""
    approved_by: str | None = None
    created_at: str
    superseded_by: int | None = None

    @model_validator(mode="after")
    def _workspace_matches_plan(self) -> AutomationDefinition:
        if self.plan.workspace != self.workspace:
            raise ValueError(
                f"automation workspace {self.workspace!r} != plan workspace {self.plan.workspace!r}"
            )
        return self

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AutomationDefinition:
        """Rebuild from a deserialized store row (name/description/plan/trigger already JSON-parsed)."""
        return cls(
            automation_id=row["automation_id"],
            workspace=row["workspace"],
            version=row["version"],
            content_hash=row["content_hash"],
            name=BilingualText.model_validate(row["name"]),
            description=(
                BilingualText.model_validate(row["description"]) if row.get("description") else None
            ),
            plan=WosoolProgram.model_validate(row["plan"]),
            trigger=parse_trigger(row["trigger"]),
            state=row["state"],
            authored_by=row.get("authored_by") or "",
            approved_by=row.get("approved_by"),
            created_at=row["created_at"],
            superseded_by=row.get("superseded_by"),
        )

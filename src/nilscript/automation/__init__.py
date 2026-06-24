"""Automation Registry: conversation-authored, deterministically-lowered, SSOT-stored workflows.

An *automation* is a named, versioned, content-hashed `WosoolProgram` plus a trigger and a
lifecycle state. The agent drafts it (untrusted); the kernel validator lowers-or-rejects it
(deterministic); a human approves it; it lives in the control-plane store with its own version lock.

See `docs/PLAN-dynamic-automation-ssot.md`. P1 ships the SSOT spine (models, content hash, draft
gate, persistence, lifecycle); schedule/event triggers and the dispatcher are P2.
"""

from __future__ import annotations

from nilscript.automation.authoring import DraftResult, draft_automation, register
from nilscript.automation.compose import (
    ComposedPlan,
    ComposedResult,
    Stage,
    composed_hash,
    parse_composed,
    run_composed,
    validate_composed,
)
from nilscript.automation.dispatch import Runner, fire_composed, fire_manual
from nilscript.automation.scheduler import dispatch_event, run_due_schedules
from nilscript.automation.skeleton import context_from_skeleton
from nilscript.automation.models import (
    AutomationDefinition,
    AutomationState,
    EventTrigger,
    ManualTrigger,
    ScheduleTrigger,
    TriggerSpec,
    content_hash,
    parse_trigger,
)

__all__ = [
    "AutomationDefinition",
    "AutomationState",
    "ComposedPlan",
    "ComposedResult",
    "DraftResult",
    "Stage",
    "EventTrigger",
    "ManualTrigger",
    "Runner",
    "ScheduleTrigger",
    "TriggerSpec",
    "composed_hash",
    "content_hash",
    "context_from_skeleton",
    "dispatch_event",
    "draft_automation",
    "fire_composed",
    "fire_manual",
    "parse_composed",
    "parse_trigger",
    "register",
    "run_composed",
    "run_due_schedules",
    "validate_composed",
]

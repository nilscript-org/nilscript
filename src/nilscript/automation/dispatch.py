"""The dispatcher: fire one run of an armed automation through the executor, recorded in the SSOT.

P2. `fire_manual` is the only trigger P1/P2 execute end to end. It enforces the governance gate
(only an `active` automation runs), pins the exact stored version, derives a deterministic `run_id`
so a re-delivered fire replays rather than double-executes, and records the executor trace as a
first-class run row. The `runner` (what actually walks the plan) is injected so the orchestration is
testable without a live backend; the control-plane app supplies a `LocalExecutor`-backed default.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from nilscript.automation.compose import (
    ComposedResult,
    StageRunner,
    parse_composed,
    run_composed,
)
from nilscript.kernel.executor import RunResult

# Walks a plan and returns its RunResult. (plan_dict, run_id) -> RunResult.
Runner = Callable[..., Awaitable[RunResult]]


class _Store(Protocol):
    def get_automation(
        self, workspace: str, automation_id: str, version: int | None = ...
    ) -> dict[str, Any] | None: ...
    def start_run(self, run_id: str, **kw: Any) -> bool: ...
    def finish_run(self, run_id: str, state: str, trace: dict[str, Any] | None) -> bool: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...


def _classify(result: RunResult) -> str:
    """Map an executor RunResult onto a terminal run state. `completed` is the only success; a saga
    unwind is `compensated`; a halt at a node is `blocked`; anything else partial is `partial`."""
    if result.completed:
        return "completed"
    if result.compensated:
        return "compensated"
    if result.blocked_at:
        return "blocked"
    return "partial"


def _trace(result: RunResult) -> dict[str, Any]:
    return {
        "completed": result.completed,
        "partial": result.partial,
        "blocked_at": result.blocked_at,
        "refusal": result.refusal,
        "compensated": result.compensated,
        "notifications": result.notifications,
        "context": result.context,
    }


async def fire_manual(
    store: _Store,
    *,
    workspace: str,
    automation_id: str,
    idempotency_key: str,
    runner: Runner,
    fired_by: str = "manual",
) -> dict[str, Any]:
    """Fire the latest version of an automation now. Returns a result envelope:

    - `{"ok": False, "error": ..., "status": 404|409}` when it cannot run (unknown / not armed),
    - `{"ok": True, "replayed": True, "run": ...}` for an idempotent re-fire (no re-execution),
    - `{"ok": True, "run": ...}` after a fresh run (run row carries the terminal state + trace).
    """
    auto = store.get_automation(workspace, automation_id)
    if auto is None:
        return {"ok": False, "error": "no such automation", "status": 404}
    if auto["state"] != "active":
        return {
            "ok": False,
            "error": f"automation is {auto['state']!r}, not active — approve/arm it first",
            "status": 409,
        }

    version, content_hash = auto["version"], auto["content_hash"]
    run_id = f"{automation_id}:v{version}:{idempotency_key}"

    if not store.start_run(
        run_id, workspace=workspace, automation_id=automation_id, version=version,
        content_hash=content_hash, fired_by=fired_by,
    ):
        return {"ok": True, "replayed": True, "run": store.get_run(run_id)}

    try:
        result = await runner(auto["plan"], run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — a runner blow-up is a failed run, recorded honestly
        store.finish_run(run_id, "failed", {"error": str(exc)})
        return {"ok": False, "error": str(exc), "status": 500, "run": store.get_run(run_id)}

    store.finish_run(run_id, _classify(result), _trace(result))
    return {"ok": True, "run": store.get_run(run_id)}


def _classify_composed(result: ComposedResult) -> str:
    if result.completed:
        return "completed"
    return "blocked" if result.blocked_at else "partial"


async def fire_composed(
    store: _Store,
    *,
    workspace: str,
    automation_id: str,
    idempotency_key: str,
    stage_runner: StageRunner,
    fired_by: str = "manual",
) -> dict[str, Any]:
    """Fire a *composed* automation now — run each stage against its adapter, threading the declared
    handoffs. Same gate/idempotency/recording as `fire_manual`; the run trace carries per-stage status."""
    auto = store.get_automation(workspace, automation_id)
    if auto is None:
        return {"ok": False, "error": "no such automation", "status": 404}
    if auto.get("kind") != "composed":
        return {"ok": False, "error": "automation is not composed", "status": 400}
    if auto["state"] != "active":
        return {
            "ok": False,
            "error": f"automation is {auto['state']!r}, not active — approve/arm it first",
            "status": 409,
        }

    version, content_hash = auto["version"], auto["content_hash"]
    run_id = f"{automation_id}:v{version}:{idempotency_key}"
    if not store.start_run(
        run_id, workspace=workspace, automation_id=automation_id, version=version,
        content_hash=content_hash, fired_by=fired_by,
    ):
        return {"ok": True, "replayed": True, "run": store.get_run(run_id)}

    try:
        composed = parse_composed(auto["plan"])
        result = await run_composed(composed, run_stage=stage_runner, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — a stage-runner blow-up is a failed run, recorded honestly
        store.finish_run(run_id, "failed", {"error": str(exc)})
        return {"ok": False, "error": str(exc), "status": 500, "run": store.get_run(run_id)}

    store.finish_run(run_id, _classify_composed(result), {
        "completed": result.completed, "blocked_at": result.blocked_at,
        "stages": result.stages, "context": result.context,
    })
    return {"ok": True, "run": store.get_run(run_id)}

"""P2 dispatcher: fire a manual run of an armed automation through the executor, recorded in the SSOT.

Two layers: the HTTP/orchestration path with a fake runner (gate, idempotency, run lifecycle), and a
real end-to-end run driving a LocalExecutor against the in-memory PocketBase FakeSystem.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from nilscript.controlplane.app import create_app
from nilscript.controlplane.store import EventStore
from nilscript.kernel.executor import LocalExecutor, RunResult


def _plan(verb: str = "crm.create_contact", skill: str = "crm", name: str = "Ada", ws: str = "acme") -> dict:
    return {
        "wosool": "0.1", "workspace": ws, "entry": "step_1",
        "pipeline": [{"id": "step_1", "type": "action", "skill": skill, "verb": verb, "args": {"name": name}}],
    }


def _body(plan: dict | None = None) -> dict:
    return {
        "automation_id": "follow-up",
        "name": {"ar": "متابعة", "en": "Follow up"},
        "plan": plan if plan is not None else _plan(),
        "trigger": {"type": "manual"},
    }


def _make(tmp_path, *, runner, verbs=("crm.create_contact",)):
    store = EventStore(path=str(tmp_path / "cp.db"))

    async def provider(workspace: str):
        return {"reachable": True, "conformant": True, "verbs": list(verbs), "targets": {}}

    client = TestClient(create_app(store, skeleton_provider=provider, runner=runner))
    return store, client


def _arm(client) -> None:
    """register → approve (active) so the automation is fireable."""
    client.post("/automations/register", json=_body())
    client.post("/automations/acme/follow-up/1/state", json={"state": "active", "approved_by": "owner"})


# --- gate + idempotency (fake runner) ---------------------------------------------------------


def test_run_refused_when_not_active(tmp_path):
    calls = []

    async def runner(plan, *, run_id):
        calls.append(run_id)
        return RunResult(completed=True)

    _, c = _make(tmp_path, runner=runner)
    c.post("/automations/register", json=_body())  # lands pending_approval, NOT active
    r = c.post("/automations/acme/follow-up/run", json={"idempotency_key": "fire-001"})
    assert r.status_code == 409
    assert calls == []  # the executor was never invoked


def test_run_executes_and_records_trace(tmp_path):
    async def runner(plan, *, run_id):
        return RunResult(completed=True, context={"step_1": {"output": {"state": "executed"}}})

    _, c = _make(tmp_path, runner=runner)
    _arm(c)
    r = c.post("/automations/acme/follow-up/run", json={"idempotency_key": "fire-001"})
    assert r.status_code == 200
    run = r.json()["run"]
    assert run["state"] == "completed"
    assert run["run_id"] == "follow-up:v1:fire-001"
    assert run["trace"]["completed"] is True


def test_refire_same_key_replays_without_re_executing(tmp_path):
    calls = []

    async def runner(plan, *, run_id):
        calls.append(run_id)
        return RunResult(completed=True)

    _, c = _make(tmp_path, runner=runner)
    _arm(c)
    c.post("/automations/acme/follow-up/run", json={"idempotency_key": "fire-001"})
    again = c.post("/automations/acme/follow-up/run", json={"idempotency_key": "fire-001"})
    assert again.json().get("replayed") is True
    assert len(calls) == 1  # executed exactly once despite two fires


def test_run_requires_idempotency_key(tmp_path):
    async def runner(plan, *, run_id):
        return RunResult(completed=True)

    _, c = _make(tmp_path, runner=runner)
    _arm(c)
    assert c.post("/automations/acme/follow-up/run", json={}).status_code == 400


def test_run_unknown_automation_404(tmp_path):
    async def runner(plan, *, run_id):
        return RunResult(completed=True)

    _, c = _make(tmp_path, runner=runner)
    r = c.post("/automations/acme/ghost/run", json={"idempotency_key": "fire-001"})
    assert r.status_code == 404


def test_runner_failure_is_recorded_as_failed(tmp_path):
    async def runner(plan, *, run_id):
        raise RuntimeError("adapter exploded")

    _, c = _make(tmp_path, runner=runner)
    _arm(c)
    r = c.post("/automations/acme/follow-up/run", json={"idempotency_key": "fire-x"})
    assert r.status_code == 500
    assert r.json()["run"]["state"] == "failed"


def test_run_history_and_detail(tmp_path):
    async def runner(plan, *, run_id):
        return RunResult(completed=True, context={"k": "v"})

    _, c = _make(tmp_path, runner=runner)
    _arm(c)
    c.post("/automations/acme/follow-up/run", json={"idempotency_key": "fire-001"})
    runs = c.get("/automations/acme/follow-up/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["state"] == "completed"
    detail = c.get("/runs/follow-up:v1:fire-001")
    assert detail.status_code == 200
    assert detail.json()["trace"]["context"] == {"k": "v"}


# --- real end-to-end run against the in-memory PocketBase shim ---------------------------------

pocketbase_edge = pytest.importorskip("pocketbase_nil_adapter.edge")
pocketbase_system = pytest.importorskip("pocketbase_nil_adapter.system")


def _shim_runner():
    from nilscript.sdk.client import NilClient
    from nilscript.sdk.grants import GrantRef
    from nilscript.sdk.transport import NilTransport

    async def runner(plan, *, run_id):
        app = pocketbase_edge.create_app(
            pocketbase_system.FakeSystem(), pocketbase_edge.CapturingEmitter(), bearer=None
        )
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shim")
        transport = NilTransport(base_url="http://shim", bearer_secret="x", client=http)
        grant = GrantRef.from_secret(
            grant_id="g", workspace=plan["workspace"], secret="s", scopes=frozenset({"commerce.*"})
        )
        nil = NilClient(transport=transport, grant=grant)
        return await LocalExecutor(nil, run_id=run_id, session_id=run_id).execute(plan)

    return runner


def test_end_to_end_real_executor_reaches_executed(tmp_path):
    plan = _plan(verb="commerce.create_product", skill="commerce", name="قميص", ws="ws_demo")
    store = EventStore(path=str(tmp_path / "cp.db"))

    async def provider(workspace: str):
        return {"reachable": True, "conformant": True, "verbs": ["commerce.create_product"], "targets": {}}

    c = TestClient(create_app(store, skeleton_provider=provider, runner=_shim_runner()))
    body = {
        "automation_id": "new-product",
        "name": {"ar": "منتج", "en": "Product"},
        "plan": plan,
        "trigger": {"type": "manual"},
    }
    assert c.post("/automations/register", json=body).status_code == 200
    c.post("/automations/ws_demo/new-product/1/state", json={"state": "active", "approved_by": "owner"})

    r = c.post("/automations/ws_demo/new-product/run", json={"idempotency_key": "fire-real-1"})
    assert r.status_code == 200
    run = r.json()["run"]
    assert run["state"] == "completed"
    # the executor genuinely walked the plan and the action reached `executed` in the SSOT
    assert run["trace"]["context"]["step_1"]["output"]["state"] == "executed"

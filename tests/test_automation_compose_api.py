"""Control-plane HTTP surface for cross-system composed automations: draft → register → run."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nilscript.controlplane.app import create_app
from nilscript.controlplane.store import EventStore
from nilscript.kernel.executor import RunResult

# Per-adapter skeletons: each backend declares its own verbs.
_SKELETONS = {
    "odoo": {"reachable": True, "conformant": True, "verbs": ["crm.create_lead"], "targets": {}},
    "books": {"reachable": True, "conformant": True, "verbs": ["acc.create_invoice"], "targets": {}},
}


def _stage(name: str, adapter: str, verb: str, skill: str, args: dict, input_from: dict | None = None) -> dict:
    s = {
        "name": name, "adapter": adapter,
        "plan": {"wosool": "0.1", "workspace": "acme", "entry": "step_1",
                 "pipeline": [{"id": "step_1", "type": "action", "skill": skill, "verb": verb, "args": args}]},
    }
    if input_from:
        s["input_from"] = input_from
    return s


def _body() -> dict:
    return {
        "automation_id": "lead-to-invoice",
        "name": {"ar": "من عميل إلى فاتورة", "en": "Lead to invoice"},
        "trigger": {"type": "manual"},
        "composed": {
            "workspace": "acme",
            "stages": [
                _stage("stage_1", "odoo", "crm.create_lead", "crm", {"name": "Acme Co"}),
                _stage("stage_2", "books", "acc.create_invoice", "acc", {"ref": "$.input.lead"},
                       input_from={"lead": "$.stage_1.step_1.output.state"}),
            ],
        },
    }


def _client(tmp_path, *, stage_runner=None):
    store = EventStore(path=str(tmp_path / "cp.db"))

    async def adapter_skeletons(workspace: str, adapter_id: str):
        return _SKELETONS.get(adapter_id)

    async def default_runner(adapter, plan, *, run_id, input):
        return RunResult(completed=True, context={"step_1": {"output": {"state": "executed"}}})

    app = create_app(
        store, adapter_skeleton_provider=adapter_skeletons,
        stage_runner=stage_runner or default_runner,
    )
    return store, TestClient(app)


def test_compose_draft_admits_valid_cross_system_plan(tmp_path):
    _, c = _client(tmp_path)
    r = c.post("/automations/compose/draft", json=_body())
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert len(out["content_hash"]) == 64
    assert out["report"]["stages"][0]["ok"] and out["report"]["stages"][1]["ok"]


def test_compose_draft_refuses_verb_not_on_its_adapter(tmp_path):
    _, c = _client(tmp_path)
    body = _body()
    body["composed"]["stages"][0]["plan"]["pipeline"][0]["verb"] = "crm.fly_to_moon"
    r = c.post("/automations/compose/draft", json=body)
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_compose_register_then_run(tmp_path):
    _, c = _client(tmp_path)
    reg = c.post("/automations/compose/register", json=_body())
    assert reg.status_code == 200
    d = reg.json()["definition"]
    assert d["kind"] == "composed" and d["state"] == "pending_approval"

    # arm it, then fire
    c.post("/automations/acme/lead-to-invoice/1/state", json={"state": "active", "approved_by": "owner"})
    run = c.post("/automations/acme/lead-to-invoice/run", json={"idempotency_key": "fire-comp-1"})
    assert run.status_code == 200
    rec = run.json()["run"]
    assert rec["state"] == "completed"
    assert rec["trace"]["stages"][1]["name"] == "stage_2"


def test_compose_run_requires_active(tmp_path):
    _, c = _client(tmp_path)
    c.post("/automations/compose/register", json=_body())  # pending_approval, not active
    r = c.post("/automations/acme/lead-to-invoice/run", json={"idempotency_key": "fire-comp-2"})
    assert r.status_code == 409


def test_single_and_composed_coexist_in_registry(tmp_path):
    """A composed registration must not disturb the single-plan path (kind defaults to 'single')."""
    store, c = _client(tmp_path)
    c.post("/automations/compose/register", json=_body())
    listed = c.get("/automations", params={"workspace": "acme"}).json()["automations"]
    assert len(listed) == 1 and listed[0]["kind"] == "composed"

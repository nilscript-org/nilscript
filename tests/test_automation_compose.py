"""Cross-system composition (P3): per-stage validation, explicit handoff, real two-adapter run."""

from __future__ import annotations

import httpx
import pytest

from nilscript.automation.compose import (
    ComposedPlan,
    Stage,
    parse_composed,
    run_composed,
    validate_composed,
)
from nilscript.kernel.executor import LocalExecutor, RunResult


def _plan(verb: str, skill: str, args: dict, ws: str = "acme") -> dict:
    return {
        "wosool": "0.1", "workspace": ws, "entry": "step_1",
        "pipeline": [{"id": "step_1", "type": "action", "skill": skill, "verb": verb, "args": args}],
    }


# --- validation: each stage against its OWN adapter's skeleton ---------------------------------


def test_validate_passes_when_each_stage_matches_its_adapter():
    composed = ComposedPlan(
        workspace="acme",
        stages=(
            Stage("stage_1", "odoo", _plan("crm.create_lead", "crm", {"name": "Acme"})),
            Stage("stage_2", "books", _plan("acc.create_invoice", "acc", {"ref": "$.input.lead"}),
                  input_from={"lead": "$.stage_1.step_1.output.state"}),
        ),
    )
    report = validate_composed(composed, {
        "odoo": {"verbs": ["crm.create_lead"]},
        "books": {"verbs": ["acc.create_invoice"]},
    })
    assert report["ok"] is True


def test_validate_refuses_verb_not_on_its_adapter():
    composed = ComposedPlan(
        workspace="acme",
        stages=(Stage("stage_1", "odoo", _plan("crm.create_lead", "crm", {"name": "Acme"})),),
    )
    # the adapter only declares accounting verbs — the crm verb has nothing to bind to
    report = validate_composed(composed, {"odoo": {"verbs": ["acc.create_invoice"]}})
    assert report["ok"] is False
    assert any(d["code"].startswith("V4") for d in report["stages"][0]["diagnostics"])


def test_validate_refuses_forward_stage_handoff():
    composed = ComposedPlan(
        workspace="acme",
        stages=(
            Stage("stage_1", "odoo", _plan("crm.create_lead", "crm", {"name": "$.input.x"}),
                  input_from={"x": "$.stage_2.step_1.output.id"}),  # references a LATER stage
            Stage("stage_2", "books", _plan("acc.create_invoice", "acc", {"ref": "r"})),
        ),
    )
    report = validate_composed(composed, {
        "odoo": {"verbs": ["crm.create_lead"]}, "books": {"verbs": ["acc.create_invoice"]},
    })
    assert report["ok"] is False
    assert any("not a prior stage" in e for e in report["errors"])


# --- orchestration: handoff + honest stop (fake stage runner) ---------------------------------


async def test_handoff_threads_output_into_next_stage_input():
    seen_inputs = {}

    async def run_stage(adapter, plan, *, run_id, input):
        seen_inputs[adapter] = input
        # stage_1 produces an output the next stage references
        return RunResult(completed=True, context={"step_1": {"output": {"id": "L-7"}}})

    composed = ComposedPlan(
        workspace="acme",
        stages=(
            Stage("stage_1", "odoo", _plan("crm.create_lead", "crm", {"name": "Acme"})),
            Stage("stage_2", "books", _plan("acc.create_invoice", "acc", {"ref": "$.input.lead"}),
                  input_from={"lead": "$.stage_1.step_1.output.id"}),
        ),
    )
    result = await run_composed(composed, run_stage=run_stage, run_id="r1")
    assert result.completed
    assert seen_inputs["books"] == {"lead": "L-7"}  # stage_1's id flowed into stage_2's input


async def test_failed_stage_halts_chain_honestly():
    ran = []

    async def run_stage(adapter, plan, *, run_id, input):
        ran.append(adapter)
        if adapter == "odoo":
            return RunResult(completed=False, blocked_at="step_1", refusal={"code": "SCOPE_DENIED"})
        return RunResult(completed=True)

    composed = ComposedPlan(
        workspace="acme",
        stages=(
            Stage("stage_1", "odoo", _plan("crm.create_lead", "crm", {"name": "Acme"})),
            Stage("stage_2", "books", _plan("acc.create_invoice", "acc", {"ref": "x"})),
        ),
    )
    result = await run_composed(composed, run_stage=run_stage, run_id="r1")
    assert result.completed is False
    assert result.blocked_at == "stage_1"
    assert ran == ["odoo"]  # the downstream stage never ran


def test_parse_composed_roundtrip():
    raw = {
        "workspace": "acme",
        "stages": [
            {"name": "stage_1", "adapter": "odoo", "plan": _plan("crm.create_lead", "crm", {"name": "A"})},
            {"name": "stage_2", "adapter": "books",
             "plan": _plan("acc.create_invoice", "acc", {"ref": "$.input.l"}),
             "input_from": {"l": "$.stage_1.step_1.output.id"}},
        ],
    }
    composed = parse_composed(raw)
    assert len(composed.stages) == 2
    assert composed.stages[1].input_from == {"l": "$.stage_1.step_1.output.id"}


# --- real two-adapter run: a value produced on adapter A writes on adapter B -------------------

pocketbase_edge = pytest.importorskip("pocketbase_nil_adapter.edge")
pocketbase_system = pytest.importorskip("pocketbase_nil_adapter.system")


def _two_adapter_runner():
    """Two independent in-memory PocketBase backends as adapters 'a' and 'b'."""
    from nilscript.sdk.client import NilClient
    from nilscript.sdk.grants import GrantRef
    from nilscript.sdk.transport import NilTransport

    apps = {
        name: pocketbase_edge.create_app(
            pocketbase_system.FakeSystem(), pocketbase_edge.CapturingEmitter(), bearer=None
        )
        for name in ("a", "b")
    }

    async def run_stage(adapter, plan, *, run_id, input):
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[adapter]), base_url="http://shim"
        )
        transport = NilTransport(base_url="http://shim", bearer_secret="x", client=http)
        grant = GrantRef.from_secret(
            grant_id="g", workspace=plan["workspace"], secret="s", scopes=frozenset({"commerce.*"})
        )
        nil = NilClient(transport=transport, grant=grant)
        return await LocalExecutor(nil, run_id=run_id, session_id=run_id).execute(plan, input=input)

    return run_stage


async def test_real_cross_adapter_handoff_executes_on_both():
    # stage_1 creates a product on adapter A; its committed `state` is handed to stage_2, which
    # writes a product on adapter B named after that handed value. Proves a value produced on one
    # backend genuinely drives a write on a DIFFERENT backend.
    composed = ComposedPlan(
        workspace="ws_demo",
        stages=(
            Stage("stage_1", "a",
                  _plan("commerce.create_product", "commerce", {"name": "قميص"}, ws="ws_demo")),
            Stage("stage_2", "b",
                  _plan("commerce.create_product", "commerce", {"name": "$.input.from_a"}, ws="ws_demo"),
                  input_from={"from_a": "$.stage_1.step_1.output.state"}),
        ),
    )
    result = await run_composed(composed, run_stage=_two_adapter_runner(), run_id="compose-run-1")
    assert result.completed is True
    assert result.context["stage_1"]["step_1"]["output"]["state"] == "executed"
    assert result.context["stage_2"]["step_1"]["output"]["state"] == "executed"

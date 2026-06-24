"""The agent-facing MCP automation tools drive the real control-plane registry end to end.

The AutomationTools HTTP client is backed by the actual control-plane ASGI app (no network), with an
injected skeleton provider + fake runner — so this proves: MCP tool → CP endpoint → validator → SSOT.
"""

from __future__ import annotations

import httpx
import pytest

from nilscript.controlplane.app import create_app
from nilscript.controlplane.store import EventStore
from nilscript.kernel.executor import RunResult
from nilscript.mcp.automation_tools import AutomationTools


def _plan(name: str = "Ada") -> dict:
    return {
        "wosool": "0.1", "workspace": "acme", "entry": "step_1",
        "pipeline": [{"id": "step_1", "type": "action", "skill": "crm",
                      "verb": "crm.create_contact", "args": {"name": name}}],
    }


@pytest.fixture
def tools(tmp_path) -> AutomationTools:
    store = EventStore(path=str(tmp_path / "cp.db"))

    async def provider(workspace: str):
        return {"reachable": True, "conformant": True, "verbs": ["crm.create_contact"], "targets": {}}

    async def runner(plan, *, run_id):
        return RunResult(completed=True, context={"step_1": {"output": {"state": "executed"}}})

    app = create_app(store, skeleton_provider=provider, runner=runner)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")
    return AutomationTools("http://cp", "", client=client)


_NAME = {"ar": "متابعة", "en": "Follow up"}
_TRIGGER = {"type": "manual"}


async def test_draft_tool_previews(tools):
    out = await tools.draft("follow-up", _NAME, _plan(), _TRIGGER)
    assert out["ok"] is True
    assert len(out["content_hash"]) == 64


async def test_draft_tool_refuses_hallucinated_verb(tools):
    plan = _plan()
    plan["pipeline"][0]["verb"] = "crm.fly_to_moon"
    out = await tools.draft("bad", _NAME, plan, _TRIGGER)
    assert out["ok"] is False
    assert any(d["code"] == "V4_UNKNOWN_SKILL" for d in out["refusal"])


async def test_full_agent_flow_draft_register_approve_run(tools):
    # the whole "by talking" path, through the agent tool surface
    assert (await tools.draft("follow-up", _NAME, _plan(), _TRIGGER))["ok"] is True

    reg = await tools.register("follow-up", _NAME, _plan(), _TRIGGER)
    d = reg["definition"]
    assert d["state"] == "pending_approval" and d["version"] == 1

    approved = await tools.approve("acme", "follow-up", 1)
    assert approved["automation"]["state"] == "active"

    run = await tools.run("acme", "follow-up", "fire-tool-1")
    assert run["run"]["state"] == "completed"

    listed = await tools.list("acme")
    assert len(listed["automations"]) == 1


async def test_run_before_approve_is_refused(tools):
    await tools.register("follow-up", _NAME, _plan(), _TRIGGER)  # pending_approval, not active
    run = await tools.run("acme", "follow-up", "fire-tool-2")
    assert run.get("ok") is False  # 409 envelope from fire_manual

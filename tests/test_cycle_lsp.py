"""The NIL language-services layer (Phase 6 — the LSP brain) over the frozen Cycle AST v0.2.

These tests pin the pure `lsp.*` functions (a projection — no state) and the CP/os-server HTTP
surface that exposes them, against the worked `SalesLeadLifecycle` example. The canonical `.nil`
text is obtained via `print_nil`, so the LSP sees exactly what the editor would render.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nilscript.controlplane.app import create_app
from nilscript.controlplane.store import EventStore
from nilscript.cycle import (
    Cycle,
    completions,
    diagnostics,
    hover,
    print_nil,
    semantic_tokens,
)
from nilscript.kernel.context import SkillSpec, ValidationContext


# --- fixtures: the worked SalesLeadLifecycle (copied from tests/test_cycle_ast.py) ------------


def _ctx() -> ValidationContext:
    verbs = {
        "odoo.crm_create_lead",
        "sales.assign_rep",
        "odoo.sale_create_quotation",
        "whatsapp.send_message",
        "audit.log_event",
    }
    by_skill: dict[str, set[str]] = {}
    for v in verbs:
        by_skill.setdefault(v.split(".", 1)[0], set()).add(v)
    return ValidationContext(
        skills={
            name: SkillSpec(required_verbs=frozenset(group), hint_schema={"additionalProperties": True})
            for name, group in by_skill.items()
        },
        read_verbs=frozenset(),
        workspaces={"acme": frozenset(verbs)},
    )


def _sales_lead_lifecycle(*, opportunity_verb: str = "odoo.sale_create_quotation") -> dict:
    return {
        "nil": "cycle/0.2",
        "cycle_id": "SalesLeadLifecycle",
        "workspace": "acme",
        "metadata": {
            "version": "1.3.2",
            "owner": "Sales Team",
            "description": {"ar": "دورة حياة العميل المحتمل", "en": "Lead lifecycle"},
            "tags": ["sales", "crm", "leads"],
        },
        "intent": {"ar": "من إنشاء العميل إلى عرض السعر والمتابعة", "en": "Lead to quotation and follow-up"},
        "trigger": {"type": "event", "on_verb": "odoo.crm_create_lead"},
        "context": [
            {"name": "lead", "entity_type": "Lead"},
            {"name": "customer", "entity_type": "Customer"},
            {"name": "quotation", "entity_type": "Quotation"},
            {"name": "approver", "entity_type": "User", "role": "SalesManager"},
        ],
        "variables": [{"name": "payload", "expression": "context.payload"}],
        "roles": [{"role": "SalesManager"}],
        "policies": [],
        "resources": ["odoo.crm_create_lead", "sales.assign_rep", "odoo.sale_create_quotation"],
        "outcomes": [{"name": "won", "when": "true"}],
        "flow": {
            "entry": "CreateLead",
            "steps": [
                {
                    "id": "CreateLead",
                    "type": "action",
                    "use": "odoo.crm_create_lead",
                    "with": {"name": "payload.name", "email": "payload.email"},
                    "output": "lead",
                    "next": "AssignSalesRep",
                },
                {
                    "id": "AssignSalesRep",
                    "type": "action",
                    "use": "sales.assign_rep",
                    "with": {"lead_id": "lead.id", "ruleset": "default"},
                    "output": "lead",
                    "next": "Approval",
                },
                {
                    "id": "Approval",
                    "type": "approval",
                    "title": {"ar": "اعتماد", "en": "Approve lead & proceed?"},
                    "description": {"ar": "راجع العميل واعتمد", "en": "Review lead details and approve"},
                    "approver": "approver",
                    "timeout_seconds": 172800,
                    "on_approve": "CreateQuotation",
                    "on_reject": "EndRejected",
                },
                {
                    "id": "CreateQuotation",
                    "type": "action",
                    "use": opportunity_verb,
                    "with": {"lead_id": "lead.id"},
                    "output": "quotation",
                    "next": "NotifyCustomer",
                },
                {
                    "id": "NotifyCustomer",
                    "type": "action",
                    "use": "whatsapp.send_message",
                    "with": {"to": "customer.phone"},
                    "next": "LogActivity",
                },
                {
                    "id": "LogActivity",
                    "type": "action",
                    "use": "audit.log_event",
                    "with": {"event": "quotation_sent"},
                },
                {
                    "id": "EndRejected",
                    "type": "notify",
                    "message": {"ar": "رُفض", "en": "Rejected"},
                },
            ],
        },
    }


def _text(**kwargs) -> str:
    return print_nil(Cycle.model_validate(_sales_lead_lifecycle(**kwargs)))


# --- diagnostics ------------------------------------------------------------------------------


def test_diagnostics_clean_cycle_has_no_error_diags():
    diags = diagnostics(_text(), _ctx())
    errors = [d for d in diags if d["severity"] == "error"]
    assert errors == []


def test_diagnostics_syntax_error_locates_the_line():
    text = _text()
    lines = text.splitlines()
    # corrupt a step line by removing its closing keyword structure → a parse failure
    bad = "\n".join(["cycle SalesLeadLifecycle triggers_on odoo.crm_create_lead {", "  oops"])
    diags = diagnostics(bad)
    syntax = [d for d in diags if d["code"] == "NIL_SYNTAX"]
    assert len(syntax) == 1
    assert syntax[0]["severity"] == "error"
    assert syntax[0]["line"] == 2  # the offending token is on line 2


def test_diagnostics_undeclared_verb_is_a_v4_error():
    diags = diagnostics(_text(opportunity_verb="odoo.fly_to_moon"), _ctx())
    v4 = [d for d in diags if d["code"].startswith("V4") and d["severity"] == "error"]
    assert v4, [d["code"] for d in diags]


def test_diagnostics_without_ctx_adds_unvalidated_info():
    diags = diagnostics(_text())
    info = [d for d in diags if d["code"] == "NIL_VERBS_UNVALIDATED"]
    assert len(info) == 1
    assert info[0]["severity"] == "info"


# --- completions ------------------------------------------------------------------------------


def test_completions_after_use_includes_catalog_verbs():
    line = "  use "
    text = "cycle X triggers_on a {\n  step S {\n" + line + "\n  }\n}"
    # cursor right after `use ` on line 3
    items = completions(text, 3, len(line) + 1, _ctx())
    labels = {i["label"] for i in items}
    assert "odoo.crm_create_lead" in labels


def test_completions_after_next_includes_step_names():
    text = _text()
    lines = text.splitlines()
    # find a `next ` line and put the cursor after it
    idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith("next "))
    ln = lines[idx]
    col = ln.index("next ") + len("next ") + 1
    items = completions(text, idx + 1, col, _ctx())
    labels = {i["label"] for i in items}
    assert "CreateLead" in labels and "Approval" in labels


# --- hover ------------------------------------------------------------------------------------


def test_hover_over_context_entity_returns_detail():
    text = _text()
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith("approver:"))
    ln = lines[idx]
    col = ln.index("approver") + 1
    info = hover(text, idx + 1, col, _ctx())
    assert info is not None
    assert "User" in info["contents"]


def test_hover_over_verb_returns_verb_detail():
    text = _text()
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if "odoo.crm_create_lead" in ln and "use " in ln)
    ln = lines[idx]
    col = ln.index("odoo.crm_create_lead") + 1
    info = hover(text, idx + 1, col, _ctx())
    assert info is not None
    assert info["kind"] == "verb"
    assert "skill" in info["contents"]


# --- semantic tokens --------------------------------------------------------------------------


def test_semantic_tokens_classify_keyword_and_string():
    tokens = semantic_tokens(_text())
    types = {t["type"] for t in tokens}
    assert "keyword" in types  # `cycle`, `step`, etc.
    assert "string" in types  # the workspace name / a quoted value
    assert "cycle_id" in types  # the cycle name after `cycle`


# --- HTTP surface (CP TestClient with a fake skeleton provider) -------------------------------


_SKELETON = {
    "reachable": True,
    "conformant": True,
    "verbs": [
        "odoo.crm_create_lead",
        "sales.assign_rep",
        "odoo.sale_create_quotation",
        "whatsapp.send_message",
        "audit.log_event",
    ],
    "targets": {},
}


def _client(tmp_path) -> TestClient:
    store = EventStore(path=str(tmp_path / "cp.db"))

    async def provider(workspace: str):
        return _SKELETON

    return TestClient(create_app(store, skeleton_provider=provider))


def test_http_print_then_parse_round_trips(tmp_path):
    client = _client(tmp_path)
    cycle = _sales_lead_lifecycle()
    pr = client.post("/cycles/print", json={"cycle": cycle})
    assert pr.status_code == 200
    text = pr.json()["text"]
    pa = client.post("/cycles/parse", json={"text": text})
    assert pa.status_code == 200
    out = pa.json()
    assert out["ok"] is True
    assert out["cycle"]["cycle_id"] == "SalesLeadLifecycle"


def test_http_diagnostics_returns_diagnostics(tmp_path):
    client = _client(tmp_path)
    r = client.post(
        "/cycles/lsp/diagnostics", json={"text": _text(), "workspace": "acme"}
    )
    assert r.status_code == 200
    diags = r.json()["diagnostics"]
    assert isinstance(diags, list)
    assert [d for d in diags if d["severity"] == "error"] == []


def test_http_projections_governance_lists_approval_gate(tmp_path):
    client = _client(tmp_path)
    r = client.post(
        "/cycles/projections",
        json={"cycle": _sales_lead_lifecycle(), "kind": "governance"},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert "Approval" in result["gates"]

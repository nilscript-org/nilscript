"""The NIL Protocol Model (Cycle AST v0.2) — the frozen canonical model every projection
(.nil text, visual canvas, LSP, docs, execution) is a view over (docs/PLAN-cycle-ast-ssot.md).

The Cycle is a protocol object with NAMED, position-independent steps, named outputs + variable
bindings, role-bound context actors, first-class approval nodes, and tags. It does NOT embed the
execution IR; `compile_cycle` LOWERS it to the hidden `WosoolProgram` IR (named steps → step_N,
named output refs → $.step_N.output paths) and runs the UNCHANGED V1–V6 validator. The governed
core stays byte-for-byte intact; the protocol surface is richer than the IR — exactly like HTML
over the DOM.

These tests pin the v0.2 contract against the worked `SalesLeadLifecycle` example (the .nil mockup):
trigger → CreateLead → AssignSalesRep → Approval(SalesManager) → {CreateQuotation → Notify → Log}
/ EndRejected.
"""

from __future__ import annotations

import pytest

from nilscript.cycle import Cycle, CompileResult, compile_cycle, cycle_content_hash
from nilscript.kernel.context import SkillSpec, ValidationContext
from nilscript.kernel.models import (
    ActionNode,
    AwaitApprovalNode,
    NotifyNode,
    WosoolProgram,
)


# --- fixtures: the worked SalesLeadLifecycle (from the .nil mockup) ---------------------------


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


# --- 1. the protocol model round-trips + rejects unknowns -------------------------------------


def test_cycle_round_trips_json():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    assert Cycle.model_validate(cycle.model_dump(by_alias=True, mode="json")) == cycle


def test_steps_are_named_not_positional():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    ids = [s.id for s in cycle.flow.steps]
    assert "CreateLead" in ids and "Approval" in ids
    assert not any(i.startswith("step_") for i in ids)  # protocol ids are stable names


def test_cycle_rejects_unknown_member():
    raw = _sales_lead_lifecycle()
    raw["surprise"] = "nope"
    with pytest.raises(ValueError):
        Cycle.model_validate(raw)


def test_role_bound_context_actor():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    approver = next(e for e in cycle.context if e.name == "approver")
    assert approver.entity_type == "User"
    assert approver.role == "SalesManager"


def test_metadata_carries_tags():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    assert cycle.metadata.tags == ("sales", "crm", "leads")


# --- 2. compile lowers named steps → the hidden WosoolProgram IR, validator passes ------------


def test_compile_lowers_to_passing_program():
    result = compile_cycle(Cycle.model_validate(_sales_lead_lifecycle()), _ctx())
    assert isinstance(result, CompileResult)
    assert result.ok, [d.code for d in result.diagnostics.diagnostics]
    assert isinstance(result.program, WosoolProgram)
    assert len(result.program.pipeline) == 7
    # the IR uses positional step_N ids — the protocol's named ids are hidden behind the lowering.
    assert all(nid.startswith("step_") for nid in result.program.nodes)


def test_named_step_to_ir_id_map_is_exposed():
    result = compile_cycle(Cycle.model_validate(_sales_lead_lifecycle()), _ctx())
    assert result.step_ids["CreateLead"] == "step_1"
    assert result.step_ids["Approval"] == "step_3"
    # entry follows the name → id map
    assert result.program.entry == result.step_ids["CreateLead"]


def test_approval_lowers_to_await_approval_node_with_branches():
    result = compile_cycle(Cycle.model_validate(_sales_lead_lifecycle()), _ctx())
    nodes = result.program.nodes
    approval = nodes[result.step_ids["Approval"]]
    assert isinstance(approval, AwaitApprovalNode)
    assert approval.on_approved == result.step_ids["CreateQuotation"]
    assert approval.on_rejected == result.step_ids["EndRejected"]
    assert approval.timeout_seconds == 172800


def test_terminal_notify_lowers():
    result = compile_cycle(Cycle.model_validate(_sales_lead_lifecycle()), _ctx())
    end = result.program.nodes[result.step_ids["EndRejected"]]
    assert isinstance(end, NotifyNode)


def test_action_skill_derived_from_verb():
    result = compile_cycle(Cycle.model_validate(_sales_lead_lifecycle()), _ctx())
    create = result.program.nodes[result.step_ids["CreateLead"]]
    assert isinstance(create, ActionNode)
    assert create.skill == "odoo"
    assert create.verb == "odoo.crm_create_lead"


# --- 3. named-output + variable references lower to $.step_N.output / $.input paths -----------


def test_named_output_reference_lowers_to_positional_ref():
    """`lead.id` (lead = CreateLead's output) becomes $.step_1.output.id in the IR."""
    result = compile_cycle(Cycle.model_validate(_sales_lead_lifecycle()), _ctx())
    assign = result.program.nodes[result.step_ids["AssignSalesRep"]]
    assert assign.args["lead_id"] == "$." + result.step_ids["CreateLead"] + ".output.id"
    assert assign.args["ruleset"] == "default"  # a literal is left untouched


def test_variable_binding_reference_lowers_to_input():
    """`payload.name` (payload = context.payload) becomes $.input.payload.name in the IR."""
    result = compile_cycle(Cycle.model_validate(_sales_lead_lifecycle()), _ctx())
    create = result.program.nodes[result.step_ids["CreateLead"]]
    assert create.args["name"] == "$.input.payload.name"
    assert create.args["email"] == "$.input.payload.email"


# --- 4. governance invariant: undeclared verb refused THROUGH the lowering --------------------


def test_compile_refuses_undeclared_verb():
    result = compile_cycle(
        Cycle.model_validate(_sales_lead_lifecycle(opportunity_verb="odoo.delete_everything")), _ctx()
    )
    assert not result.ok
    assert result.program is None
    assert "V4_UNKNOWN_SKILL" in {d.code for d in result.diagnostics.diagnostics}


# --- 5. content hash over the AST (the version lock) -----------------------------------------


def test_content_hash_deterministic_and_ast_not_ir():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    assert cycle_content_hash(cycle) == cycle_content_hash(cycle)
    result = compile_cycle(cycle, _ctx())
    assert result.content_hash == cycle_content_hash(cycle)
    # metadata-only change (not in the IR) still moves the hash
    edited = _sales_lead_lifecycle()
    edited["intent"] = {"ar": "آخر", "en": "Other"}
    assert cycle_content_hash(Cycle.model_validate(edited)) != cycle_content_hash(cycle)


# --- 6. approval nodes ARE the gates (first-class, not a policy flag) -------------------------


def test_approval_steps_are_reported_as_gates():
    result = compile_cycle(Cycle.model_validate(_sales_lead_lifecycle()), _ctx())
    assert result.step_ids["Approval"] in result.gates


def test_policy_high_tier_also_gates_its_target():
    raw = _sales_lead_lifecycle()
    raw["policies"] = [{"policy_id": "big_deal", "applies_to": ["CreateQuotation"], "raises_tier": "HIGH"}]
    result = compile_cycle(Cycle.model_validate(raw), _ctx())
    assert result.ok
    assert result.step_ids["CreateQuotation"] in result.gates
    assert result.step_ids["Approval"] in result.gates  # the approval node still gates

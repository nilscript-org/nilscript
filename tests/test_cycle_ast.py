"""Phase 1 of the Cycle-AST-as-SSOT migration (docs/PLAN-cycle-ast-ssot.md).

The Cycle is a protocol object that EMBEDS today's governed `WosoolProgram` node union as its
`Flow`. These tests pin the Phase-1 contract in isolation — no control-plane wiring:

1. the AST round-trips JSON (frozen, alias-stable);
2. `compile_cycle` lowers the worked `SalesLeadLifecycle` example into a validator-passing
   `WosoolProgram` (governance invariant: Cycle → lower → V1–V6 → content-hash);
3. an undeclared verb is refused *through the lowering* (V4 — the agent cannot talk past it);
4. the content-hash (over the AST, not the derived IR) is deterministic and idempotent;
5. a `policies.raises_tier=HIGH` escalates its target node to a human-approval gate (floor only
   ever rises, never falls).
"""

from __future__ import annotations

import pytest

from nilscript.cycle import Cycle, CompileResult, compile_cycle, cycle_content_hash
from nilscript.kernel.context import SkillSpec, ValidationContext
from nilscript.kernel.models import WosoolProgram


# --- fixtures ---------------------------------------------------------------------------------


def _ctx() -> ValidationContext:
    """Workspace `acme` may create leads/opportunities and log notes via the `crm` skill — nothing
    else. The default-deny world the lowered program is validated against."""
    return ValidationContext(
        skills={
            "crm": SkillSpec(
                required_verbs=frozenset(
                    {"crm.create_lead", "crm.log_note", "crm.create_opportunity"}
                ),
                hint_schema={
                    "properties": {"name": {"type": "string"}, "note": {"type": "string"}},
                    "required": [],
                    "additionalProperties": True,
                },
            )
        },
        read_verbs=frozenset(),
        workspaces={
            "acme": frozenset(
                {"crm.create_lead", "crm.log_note", "crm.create_opportunity"}
            )
        },
    )


def _sales_lead_lifecycle(*, opportunity_verb: str = "crm.create_opportunity") -> dict:
    """The worked example: a five-node sales cycle that qualifies a lead, optionally opens an
    opportunity, then notifies. `opportunity_verb` is parameterised so a test can swap in an
    undeclared verb and watch the lowering refuse it."""
    return {
        "nil": "cycle/0.1",
        "cycle_id": "SalesLeadLifecycle",
        "workspace": "acme",
        "metadata": {"version": "1.0", "owner": "Sales"},
        "intent": {"ar": "دورة حياة العميل المحتمل", "en": "Lead lifecycle"},
        "trigger": {"type": "manual"},
        "context": [{"name": "customer", "entity_type": "Customer"}],
        "roles": [{"role": "SalesManager"}],
        "policies": [],
        "resources": ["crm.create_lead", "crm.log_note", "crm.create_opportunity"],
        "outcomes": [{"name": "won", "when": "true"}],
        "flow": {
            "entry": "step_1",
            "nodes": [
                {
                    "id": "step_1",
                    "type": "action",
                    "skill": "crm",
                    "verb": "crm.create_lead",
                    "args": {"name": "Acme"},
                    "next": "step_2",
                },
                {
                    "id": "step_2",
                    "type": "action",
                    "skill": "crm",
                    "verb": "crm.log_note",
                    "args": {"note": "lead created"},
                    "next": "step_3",
                },
                {
                    "id": "step_3",
                    "type": "condition",
                    "expression": "true",
                    "on_true": "step_4",
                    "on_false": "step_5",
                },
                {
                    "id": "step_4",
                    "type": "action",
                    "skill": "crm",
                    "verb": opportunity_verb,
                    "args": {"name": "Acme"},
                    "next": "step_5",
                },
                {
                    "id": "step_5",
                    "type": "notify",
                    "message": {"ar": "تم", "en": "Done"},
                },
            ],
        },
    }


# --- 1. AST round-trips JSON ------------------------------------------------------------------


def test_cycle_round_trips_json():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    dumped = cycle.model_dump(by_alias=True, mode="json")
    assert Cycle.model_validate(dumped) == cycle


def test_cycle_rejects_unknown_member():
    raw = _sales_lead_lifecycle()
    raw["surprise"] = "not in the schema"
    with pytest.raises(ValueError):
        Cycle.model_validate(raw)


def test_cycle_embeds_the_existing_node_union():
    """The flow holds today's governed nodes verbatim — not a parallel re-definition."""
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    assert cycle.flow.nodes[0].type == "action"
    assert cycle.flow.nodes[2].type == "condition"
    assert cycle.flow.nodes[4].type == "notify"


# --- 2. compile_cycle lowers to a validator-passing WosoolProgram -----------------------------


def test_compile_lowers_to_passing_program():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    result = compile_cycle(cycle, _ctx())
    assert isinstance(result, CompileResult)
    assert result.ok, [d.code for d in result.diagnostics.diagnostics]
    assert isinstance(result.program, WosoolProgram)
    assert result.program.workspace == "acme"
    assert result.program.entry == "step_1"
    assert len(result.program.pipeline) == 5
    assert result.content_hash is not None


def test_compile_preserves_node_identity_through_lowering():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    result = compile_cycle(cycle, _ctx())
    assert set(result.program.nodes) == {f"step_{i}" for i in range(1, 6)}


# --- 3. an undeclared verb is refused through the lowering ------------------------------------


def test_compile_refuses_undeclared_verb():
    raw = _sales_lead_lifecycle(opportunity_verb="crm.delete_everything")
    cycle = Cycle.model_validate(raw)
    result = compile_cycle(cycle, _ctx())
    assert not result.ok
    assert result.program is None
    codes = {d.code for d in result.diagnostics.diagnostics}
    assert "V4_UNKNOWN_SKILL" in codes


def test_compile_refuses_verb_outside_workspace_scope():
    ctx = ValidationContext(
        skills=_ctx().skills,
        read_verbs=frozenset(),
        workspaces={"acme": frozenset({"crm.create_lead", "crm.log_note"})},  # no opportunity grant
    )
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    result = compile_cycle(cycle, ctx)
    assert not result.ok
    codes = {d.code for d in result.diagnostics.diagnostics}
    assert "V4_SCOPE_DENIED" in codes


# --- 4. content-hash is deterministic and idempotent (the version lock) ----------------------


def test_content_hash_is_deterministic_and_idempotent():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    assert cycle_content_hash(cycle) == cycle_content_hash(cycle)
    again = Cycle.model_validate(_sales_lead_lifecycle())
    assert cycle_content_hash(cycle) == cycle_content_hash(again)


def test_content_hash_is_64_hex_chars():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    digest = cycle_content_hash(cycle)
    assert len(digest) == 64
    int(digest, 16)  # raises if not hex


def test_content_hash_changes_when_intent_changes():
    base = Cycle.model_validate(_sales_lead_lifecycle())
    edited_raw = _sales_lead_lifecycle()
    edited_raw["intent"] = {"ar": "شيء آخر", "en": "Something else"}
    edited = Cycle.model_validate(edited_raw)
    assert cycle_content_hash(base) != cycle_content_hash(edited)


def test_compile_hashes_the_ast_not_the_ir():
    """The lock is over the Cycle AST: two cycles whose IR is identical but whose authoring-time
    metadata (intent) differs must hash differently — the IR hash could not tell them apart."""
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    result = compile_cycle(cycle, _ctx())
    assert result.content_hash == cycle_content_hash(cycle)


# --- 5. policies.raises_tier=HIGH escalates its target node to a gate -------------------------


def _with_policy(raises_tier: str | None, applies_to: tuple[str, ...]) -> dict:
    raw = _sales_lead_lifecycle()
    raw["policies"] = [
        {
            "policy_id": "high_value_opportunity",
            "applies_to": list(applies_to),
            "raises_tier": raises_tier,
        }
    ]
    return raw


def test_policy_high_tier_escalates_target_to_gate():
    cycle = Cycle.model_validate(_with_policy("HIGH", ("step_4",)))
    result = compile_cycle(cycle, _ctx())
    assert result.ok
    assert "step_4" in result.gates
    assert "step_1" not in result.gates


def test_policy_low_tier_does_not_gate():
    cycle = Cycle.model_validate(_with_policy("LOW", ("step_4",)))
    result = compile_cycle(cycle, _ctx())
    assert result.ok
    assert result.gates == ()


def test_no_policies_means_no_gates():
    cycle = Cycle.model_validate(_sales_lead_lifecycle())
    result = compile_cycle(cycle, _ctx())
    assert result.gates == ()

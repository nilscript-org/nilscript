"""Tests for the JSON-Schema -> pydantic model emitter used by `scaffold-shim` (plan §3.1).

The decisive choice (plan §3.1): generate pydantic models from the bundled JSON Schemas directly.
These tests generate the models for the real NIL profiles, exec the result, and assert the models
actually enforce the schema (required fields, extra-forbidden) — proving the emitted code is valid
and faithful, not just that a string came out.
"""

from __future__ import annotations

import pytest

from nilscript.cli._spec import active_verbs
from nilscript.cli.scaffold._models import model_class_name, render_models


def _exec_models() -> dict:
    source = render_models(active_verbs())
    namespace: dict = {}
    exec(compile(source, "<generated models.py>", "exec"), namespace)  # noqa: S102 - testing generated code
    return namespace


def test_generated_models_compile_and_expose_one_class_per_active_verb() -> None:
    namespace = _exec_models()
    for verb in active_verbs():
        assert model_class_name(verb) in namespace, f"missing model for {verb.name}"


def test_deprecated_verb_has_no_model() -> None:
    source = render_models(active_verbs())
    # update_order_status is parked (GAP-001) — it must not be scaffolded.
    assert "UpdateOrderStatus" not in source


def test_create_invoice_model_enforces_required_fields() -> None:
    namespace = _exec_models()
    Model = namespace["ServicesCreateInvoiceArgs"]
    # valid args pass
    Model(party_id="C-1", amount=100, currency="SAR")
    # missing a required field is rejected
    with pytest.raises(Exception):
        Model(amount=100, currency="SAR")


def test_model_forbids_unknown_fields() -> None:
    namespace = _exec_models()
    Model = namespace["ServicesCreateInvoiceArgs"]
    with pytest.raises(Exception):
        Model(party_id="C-1", amount=100, currency="SAR", not_a_field="x")


def test_model_class_name_is_pascalcase_with_args_suffix() -> None:
    verb = next(v for v in active_verbs() if v.name == "services.create_invoice")
    assert model_class_name(verb) == "ServicesCreateInvoiceArgs"

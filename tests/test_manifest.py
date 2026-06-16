"""Tests for the requirements-manifest core: schema validation + the structural/instance split.

The manifest is the toolkit's central artifact (plan §4). Two invariants are load-bearing and
tested here: (1) a manifest validates against a known shape; (2) the structural/instance split is
enforced mechanically, not by convention — a *shareable* manifest must never carry instance values
or secrets (plan §6 governance, §8 leakage caveat).
"""

from __future__ import annotations

from nilscript.cli.manifest import (
    MANIFEST_VERSION,
    shareable_violations,
    strip_instance,
    validate,
)


def _erpnext_manifest() -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "system": "erpnext",
        "nil_spec": "0.1",
        "verbs": {
            "services.create_invoice": {
                "native_target": "Sales Invoice",
                "hidden_requirements": [
                    {"field": "company", "kind": "required_scalar"},
                    {"field": "income_account", "kind": "required_on_line"},
                    {"field": "cost_center", "kind": "required_on_line"},
                ],
                "prerequisites": [
                    {"entity": "customer", "from_arg": "party_id", "resolve_with": "services.create_client"}
                ],
                "instance_values": {
                    "company": "${ERPNEXT_COMPANY}",
                    "income_account": "${ERPNEXT_INCOME_ACCOUNT}",
                },
                "line_shape": "free_text_service_line",
            }
        },
        "transport_quirks": [
            {"quirk": "no_expect_100_continue", "evidence": "HTTP 417 EXPECTATION FAILED"}
        ],
    }


def test_valid_manifest_has_no_errors() -> None:
    assert validate(_erpnext_manifest()) == []


def test_missing_manifest_version_is_an_error() -> None:
    bad = _erpnext_manifest()
    del bad["manifest_version"]
    errors = validate(bad)
    assert any("manifest_version" in e for e in errors)


def test_unknown_requirement_kind_is_rejected() -> None:
    bad = _erpnext_manifest()
    bad["verbs"]["services.create_invoice"]["hidden_requirements"][0]["kind"] = "wat"
    errors = validate(bad)
    assert any("wat" in e for e in errors)


def test_transport_quirk_needs_evidence() -> None:
    bad = _erpnext_manifest()
    bad["transport_quirks"] = [{"quirk": "no_expect_100_continue"}]
    errors = validate(bad)
    assert any("evidence" in e for e in errors)


def test_env_placeholder_instance_values_are_shareable() -> None:
    # ${ENV} placeholders carry no secret — a manifest using only placeholders is shareable.
    assert shareable_violations(_erpnext_manifest()) == []


def test_concrete_instance_value_is_a_leak() -> None:
    leaky = _erpnext_manifest()
    leaky["verbs"]["services.create_invoice"]["instance_values"]["company"] = "abc"
    violations = shareable_violations(leaky)
    assert any("company" in v for v in violations)


def test_secret_looking_key_is_a_leak_even_as_placeholder() -> None:
    leaky = _erpnext_manifest()
    leaky["verbs"]["services.create_invoice"]["instance_values"]["api_secret"] = "${ERPNEXT_API_SECRET}"
    violations = shareable_violations(leaky)
    assert any("api_secret" in v for v in violations)


def test_access_key_style_keys_are_flagged_as_secrets() -> None:
    # api_key / access_key / apikey end in "key" — secret-bearing even as ${ENV} placeholders.
    for key in ("access_key", "api_key", "apikey", "auth", "bearer_credential"):
        leaky = _erpnext_manifest()
        leaky["verbs"]["services.create_invoice"]["instance_values"][key] = "${SOMETHING}"
        assert any(key in v for v in shareable_violations(leaky)), f"{key} not flagged"


def test_strip_instance_yields_a_shareable_manifest() -> None:
    shared = strip_instance(_erpnext_manifest())
    assert "instance_values" not in shared["verbs"]["services.create_invoice"]
    # structural requirements survive
    assert shared["verbs"]["services.create_invoice"]["hidden_requirements"]
    assert shareable_violations(shared) == []

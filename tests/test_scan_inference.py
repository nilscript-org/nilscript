"""Tests for the error->requirement inference engine (plan §4.3) and manifest assembly.

The engine is the intelligence of `scan`: it maps a native error *signature* to a structured
requirement, grounded in a real execution result (plan tenet §1.3 — never guessing). These tests
use the ACTUAL error signatures we hit building the live ERPNext shim, so a scan that replays them
must reproduce, automatically, what cost five manual attempts (plan Phase-2 DoD).
"""

from __future__ import annotations

from nilscript.cli.scan.inference import build_manifest, infer


def test_link_validation_error_infers_a_prerequisite() -> None:
    findings = infer("LinkValidationError: Could not find Customer: عبدالرحيم")
    assert any(f.kind == "prerequisite" and f.entity == "customer" for f in findings)


def test_income_account_error_infers_line_and_company_requirements() -> None:
    findings = infer("Income Account None does not belong to company abc")
    kinds = {(f.kind, f.field) for f in findings}
    assert ("required_on_line", "income_account") in kinds
    assert ("required_scalar", "company") in kinds


def test_http_417_infers_the_expect_transport_quirk() -> None:
    findings = infer("HTTP 417 EXPECTATION FAILED")
    assert any(f.kind == "transport_quirk" and f.quirk == "no_expect_100_continue" for f in findings)


def test_frappe_mandatory_fields_infers_required_scalars() -> None:
    findings = infer("Mandatory fields required in Sales Invoice: Company, Cost Center")
    fields = {f.field for f in findings if f.kind == "required_scalar"}
    assert "company" in fields and "cost_center" in fields


def test_unknown_error_yields_no_finding() -> None:
    # Honest: an unseen error returns nothing here (the LLM fallback is a later phase, plan §4.3).
    assert infer("some totally novel backend error 9000") == []


def test_build_manifest_reproduces_the_erpnext_findings() -> None:
    # The exact collisions from the live build (plan §0), fed back as a replay sample.
    samples = [
        {
            "verb": "services.create_invoice",
            "native_target": "Sales Invoice",
            "errors": [
                "Income Account None does not belong to company abc",
                "HTTP 417 EXPECTATION FAILED",
            ],
        }
    ]
    manifest = build_manifest("erpnext", samples)

    assert manifest["system"] == "erpnext"
    verb = manifest["verbs"]["services.create_invoice"]
    assert verb["native_target"] == "Sales Invoice"
    fields = {(r["field"], r["kind"]) for r in verb["hidden_requirements"]}
    assert ("company", "required_scalar") in fields
    assert ("income_account", "required_on_line") in fields
    # the 417 is recorded once, at the top level
    quirks = {q["quirk"] for q in manifest["transport_quirks"]}
    assert "no_expect_100_continue" in quirks


def test_build_manifest_dedupes_repeated_findings() -> None:
    samples = [
        {
            "verb": "services.create_invoice",
            "errors": [
                "Income Account None does not belong to company abc",
                "Income Account None does not belong to company abc",
            ],
        }
    ]
    manifest = build_manifest("erpnext", samples)
    reqs = manifest["verbs"]["services.create_invoice"]["hidden_requirements"]
    # company appears once despite two identical collisions
    assert sum(1 for r in reqs if r["field"] == "company") == 1


def test_mandatory_fields_inferred_even_with_trailing_traceback() -> None:
    # Frappe appends a traceback on its own line — `$` must anchor per-line (re.MULTILINE).
    findings = infer(
        "Mandatory fields required in Sales Invoice: Company, Cost Center\n"
        "Traceback (most recent call last):\n  File ...\n"
    )
    fields = {f.field for f in findings if f.kind == "required_scalar"}
    assert "company" in fields and "cost_center" in fields


def test_build_manifest_dedupes_across_multiple_samples_of_same_verb() -> None:
    # The same verb recurring across samples must not duplicate a hidden requirement (per-verb dedup).
    samples = [
        {"verb": "services.create_invoice", "errors": ["Income Account None does not belong to company abc"]},
        {"verb": "services.create_invoice", "errors": ["Income Account None does not belong to company def"]},
    ]
    manifest = build_manifest("erpnext", samples)
    reqs = manifest["verbs"]["services.create_invoice"]["hidden_requirements"]
    assert sum(1 for r in reqs if r["field"] == "company") == 1
    assert sum(1 for r in reqs if r["field"] == "income_account") == 1


def test_build_manifest_applies_prerequisite_resolve_hints() -> None:
    samples = [
        {
            "verb": "services.create_invoice",
            "errors": ["LinkValidationError: Could not find Customer: X"],
        }
    ]
    manifest = build_manifest(
        "erpnext", samples, resolve_hints={"customer": "services.create_client"}
    )
    prereqs = manifest["verbs"]["services.create_invoice"]["prerequisites"]
    assert prereqs[0]["entity"] == "customer"
    assert prereqs[0]["resolve_with"] == "services.create_client"

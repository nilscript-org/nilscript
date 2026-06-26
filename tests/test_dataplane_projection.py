"""Lean projection: a list/get read returns only the requested fields (+ the key), never the whole
record. This is the direct fix for the 590 KB flood — Odoo `res.partner` has 100+ fields; the agent
needs id/name/phone/email, not credit limits and audit columns.
"""

from __future__ import annotations

from nilscript.dataplane import project, project_items


def test_projection_keeps_only_requested_fields_plus_key() -> None:
    record = {
        "id": 7,
        "name": "رغد عبدالله",
        "phone": "+97455512345",
        "credit_limit": 0.0,
        "comment": "x" * 5000,
    }
    lean = project(record, ("name", "phone"))
    assert lean == {"id": 7, "name": "رغد عبدالله", "phone": "+97455512345"}


def test_projection_always_retains_the_key_even_if_not_requested() -> None:
    # The key (id) is how the agent does a follow-up get/update; it is never projected away.
    lean = project({"id": 42, "name": "Acme"}, ("name",))
    assert lean["id"] == 42


def test_project_items_projects_every_row_in_a_page() -> None:
    items = [{"id": i, "name": f"c{i}", "secret": "s" * 100} for i in range(3)]
    lean = project_items(items, ("name",))
    assert lean == [{"id": 0, "name": "c0"}, {"id": 1, "name": "c1"}, {"id": 2, "name": "c2"}]

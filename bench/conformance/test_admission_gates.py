"""Axis 3 — the two STRUCTURAL admission gates every conformant adapter must pass.

These are the kernel-side checks that turn the paper's promises from prose into enforced invariants.
An adapter that fails either gate is non-conformant — it must not be admitted, exactly as Guarantee 4
("tiers are earned, not asserted") fails admission for a mis-declared reversibility tier.

  • earned-not-asserted — an edge must EARN `verified` by reading the SSOT back; it may never report
    verified:true for a field the backend silently dropped (the country_id:false incident).
  • advertised ≡ committable — the generic resource.* family must be refused against any target
    outside describe()'s advertised skeleton, with zero effect (β⁻¹(a)=∅ for the CRUD family).

Run (against any adapter on the path):
  PYTHONPATH=<adapter>/src pytest bench/conformance/test_admission_gates.py -q

No Hypothesis dependency — these are deterministic gates. They are also emitted into every scaffolded
adapter's own conformance suite, so each adapter self-enforces them.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from pocketbase_nil_adapter.edge import CapturingEmitter, create_app
from pocketbase_nil_adapter.system import FakeSystem

TARGET = "products"  # a declared target on the reference adapter


def _env(verb: str, args: dict) -> dict:
    return {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}


def _propose(c: TestClient, verb: str, args: dict) -> dict:
    return c.post("/nil/v0.1/propose", json=_env(verb, args)).json()["body"]


def _commit(c: TestClient, pid: str) -> dict:
    return c.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
                  "body": {"proposal": pid, "idempotency_key": pid}}).json()["body"]


def test_verified_is_earned_not_asserted() -> None:
    """An edge must never report verified:true for a field the backend silently dropped. We drive
    resource.update through a backend that drops the written field; a conformant edge re-reads the SSOT
    and reports verified:false + names the field. An edge that hardcodes verified:true FAILS here."""
    class _DropsField(FakeSystem):
        def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]:
            return super().update(target, record_id, {k: v for k, v in doc.items() if k != "earned_probe"})

    sys = _DropsField()
    sys.create(TARGET, {"name": "rec1"})
    c = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    body = _propose(c, "resource.update", {"target": TARGET, "id": "rec1", "data": {"earned_probe": "x"}})
    st = _commit(c, body["id"])

    assert st["result"]["verified"] is False, "verified must be EARNED — a dropped field cannot be verified"
    assert "earned_probe" in st["result"].get("unverified_fields", []), "the unverified field must be named"


def test_resource_star_is_skeleton_bounded() -> None:
    """The generic resource.* family must be refused against any target outside describe()'s advertised
    skeleton, with zero effect — even though the backend would provision it (FakeSystem.exists() is True
    for all). This is β⁻¹(a)=∅ for the CRUD family; an edge bounded only by client.exists FAILS here."""
    sys = FakeSystem()
    c = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    advertised = set(c.get("/nil/v0.1/describe").json()["targets"])
    undeclared = "__undeclared_probe__"
    assert undeclared not in advertised, "probe must be genuinely undeclared"

    body = _propose(c, "resource.create", {"target": undeclared, "data": {"x": 1}})

    assert body["outcome"] == "refusal", f"resource.* on an undeclared target must be refused, got {body}"
    assert sys.docs.get(undeclared) is None, "EL must be 0 — no record on an undeclared target"

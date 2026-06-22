"""Control-plane event store + ingest API (audit single-pane)."""

import hashlib
import hmac
import json

import pytest

pytest.importorskip("fastapi", reason="needs fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from nilscript.controlplane.app import create_app  # noqa: E402
from nilscript.controlplane.store import EventStore  # noqa: E402


def _store():
    return EventStore(":memory:")


def _event(seq, *, ws="ws1", ev="executed", verb="commerce.create_product", proposal="p1"):
    return {
        "nil": "0.1", "id": f"id{seq}", "performative": "EVENT", "grant": "g1", "workspace": ws,
        "ts": "2026-06-19T00:00:00Z",
        "body": {"event": ev, "severity": "info", "proposal": proposal, "verb": verb, "tier": "MEDIUM"},
    }


def test_ingest_stores_and_recent_reads_newest_first() -> None:
    s = _store()
    assert s.ingest(_event(1, verb="a"), 1) is True
    assert s.ingest(_event(2, verb="b"), 2) is True
    rows = s.recent()
    assert [r["verb"] for r in rows] == ["b", "a"]  # newest first
    assert s.count() == 2


def test_ingest_dedups_by_event_id() -> None:
    s = _store()
    assert s.ingest(_event(1), 7) is True
    assert s.ingest(_event(1), 7) is False  # same envelope id → no-op (at-least-once retry)
    assert s.count() == 1


def test_same_sequence_different_event_id_not_a_dup() -> None:
    # The adapter resets its in-memory sequence on restart, so two distinct events can share a
    # (workspace, sequence) — they must NOT be deduped. Distinct envelope ids keep them apart.
    s = _store()
    assert s.ingest(_event(1, proposal="a"), 5) is True
    assert s.ingest(_event(2, proposal="b"), 5) is True  # same seq=5, different id → stored
    assert s.count() == 2


def test_ingest_endpoint_verifies_hmac_and_stores() -> None:
    s = _store()
    secret = "topsecret"
    client = TestClient(create_app(s, secret=secret))
    payload = _event(1, verb="commerce.create_coupon")
    raw = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    bad = client.post("/events/ingest", content=raw, headers={"X-NIL-Signature": "deadbeef", "X-NIL-Sequence": "1"})
    assert bad.status_code == 401

    ok = client.post("/events/ingest", content=raw, headers={"X-NIL-Signature": sig, "X-NIL-Sequence": "1"})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "new": True}

    listed = client.get("/api/events").json()["events"]
    assert len(listed) == 1 and listed[0]["verb"] == "commerce.create_coupon"


def test_ingest_endpoint_open_when_no_secret() -> None:
    client = TestClient(create_app(_store(), secret=""))
    r = client.post("/events/ingest", content=json.dumps(_event(1)).encode(), headers={"X-NIL-Sequence": "1"})
    assert r.status_code == 200 and r.json()["new"] is True


def test_healthz_reports_count() -> None:
    s = _store()
    s.ingest(_event(1), 1)
    client = TestClient(create_app(s, secret=""))
    assert client.get("/healthz").json() == {"status": "ok", "events": 1}


# ── human-approval gate (Phase 2) ────────────────────────────────────────────────────────────

def _proposed(seq, *, proposal, verb="commerce.process_refund", tier="HIGH"):
    return {
        "nil": "0.1", "id": f"prop{seq}", "performative": "EVENT", "grant": "g1", "workspace": "",
        "body": {"event": "proposed", "proposal": proposal, "verb": verb, "tier": tier,
                 "preview": {"en": f"Refund {proposal}", "ar": "استرداد"}},
    }


def test_await_then_decision_flow() -> None:
    s = _store()
    s.ingest(_proposed(1, proposal="px"), 1)  # the control plane saw the intent
    assert s.decision("px") == "unknown"
    s.await_approval("px")
    assert s.decision("px") == "pending"
    # the pending card is enriched from the proposed event
    p = s.pending()[0]
    assert p["proposal_id"] == "px" and p["verb"] == "commerce.process_refund" and p["tier"] == "HIGH"
    assert s.decide("px", "approved", actor="owner") is True
    assert s.decision("px") == "approved"
    assert s.pending() == []


def test_decide_only_transitions_pending() -> None:
    s = _store()
    s.await_approval("py")
    assert s.decide("py", "rejected") is True
    assert s.decide("py", "approved") is False  # already decided → no-op
    assert s.decision("py") == "rejected"


def test_await_is_idempotent() -> None:
    s = _store()
    s.await_approval("pz")
    s.decide("pz", "approved")
    s.await_approval("pz")  # must not reset an approved decision
    assert s.decision("pz") == "approved"


def test_approval_endpoints() -> None:
    s = _store()
    s.ingest(_proposed(1, proposal="pe", verb="commerce.create_product", tier="HIGH"), 1)
    client = TestClient(create_app(s, secret=""))
    assert client.post("/proposals/pe/await").json()["status"] == "pending"
    assert client.get("/proposals/pe/decision").json()["status"] == "pending"
    assert client.get("/api/pending").json()["pending"][0]["proposal_id"] == "pe"
    bad = client.post("/proposals/pe/decision", json={"status": "maybe"})
    assert bad.status_code == 400
    ok = client.post("/proposals/pe/decision", json={"status": "approved", "actor": "me"})
    assert ok.json()["status"] == "approved"
    assert client.get("/proposals/pe/decision").json()["status"] == "approved"
    assert client.get("/api/pending").json()["pending"] == []


# ── active-adapter registry (multi-tenant routing) ─────────────────────────────────────────────

def test_register_then_active_returns_it() -> None:
    s = _store()
    s.register_adapter("ws1", "odoo", label="Odoo CRM",
                       url="https://odoo.example/nil", bearer="tok-o", system="odoo_crm")
    s.activate_adapter("ws1", "odoo")
    a = s.active_adapter("ws1")
    assert a is not None
    assert a["adapter_id"] == "odoo" and a["url"] == "https://odoo.example/nil"
    assert a["bearer"] == "tok-o" and a["system"] == "odoo_crm" and a["active"] == 1


def test_activating_second_deactivates_first() -> None:
    s = _store()
    s.register_adapter("ws1", "pb", url="https://pb.example/nil", system="pocketbase")
    s.register_adapter("ws1", "odoo", url="https://odoo.example/nil", system="odoo_crm")
    s.activate_adapter("ws1", "pb")
    assert s.active_adapter("ws1")["adapter_id"] == "pb"
    s.activate_adapter("ws1", "odoo")
    assert s.active_adapter("ws1")["adapter_id"] == "odoo"
    actives = [a for a in s.list_adapters("ws1") if a["active"]]
    assert len(actives) == 1 and actives[0]["adapter_id"] == "odoo"


def test_activate_is_workspace_scoped() -> None:
    s = _store()
    s.register_adapter("ws1", "odoo", url="https://odoo.example/nil")
    s.register_adapter("ws2", "pb", url="https://pb.example/nil")
    s.activate_adapter("ws1", "odoo")
    assert s.active_adapter("ws1")["adapter_id"] == "odoo"
    assert s.active_adapter("ws2") is None  # ws2 has none active


def test_activate_unknown_returns_false() -> None:
    s = _store()
    assert s.activate_adapter("ws1", "ghost") is False


def test_reregister_preserves_active_flag() -> None:
    s = _store()
    s.register_adapter("ws1", "odoo", url="https://odoo.example/nil", bearer="old")
    s.activate_adapter("ws1", "odoo")
    s.register_adapter("ws1", "odoo", url="https://odoo.example/nil", bearer="new")
    a = s.active_adapter("ws1")
    assert a["active"] == 1 and a["bearer"] == "new"  # updated creds, still active


def test_registry_endpoints_with_token() -> None:
    s = _store()
    token = "reg-secret"
    client = TestClient(create_app(s, secret="", registry_token=token))
    auth = {"Authorization": f"Bearer {token}"}

    reg = client.post("/adapters/register", headers=auth, json={
        "workspace": "ws1", "adapter_id": "odoo", "label": "Odoo CRM",
        "url": "https://odoo.example/nil", "bearer": "tok-o", "system": "odoo_crm"})
    assert reg.status_code == 200 and reg.json()["ok"] is True

    act = client.post("/adapters/ws1/odoo/activate", headers=auth)
    assert act.status_code == 200 and act.json()["ok"] is True

    active = client.get("/adapters/active?workspace=ws1", headers=auth).json()
    assert active["adapter"]["url"] == "https://odoo.example/nil"
    assert active["adapter"]["bearer"] == "tok-o"


def test_active_endpoint_requires_auth() -> None:
    s = _store()
    token = "reg-secret"
    s.register_adapter("ws1", "odoo", url="https://odoo.example/nil", bearer="tok-o")
    s.activate_adapter("ws1", "odoo")
    client = TestClient(create_app(s, secret="", registry_token=token))
    # No bearer → the endpoint that exposes the adapter bearer must reject.
    assert client.get("/adapters/active?workspace=ws1").status_code == 401
    assert client.post("/adapters/register", json={"workspace": "ws1", "adapter_id": "x",
                                                   "url": "https://x.example/nil"}).status_code == 401
    assert client.post("/adapters/ws1/odoo/activate").status_code == 401


def test_list_endpoint_redacts_bearer_and_is_public() -> None:
    s = _store()
    s.register_adapter("ws1", "odoo", url="https://odoo.example/nil", bearer="supersecret")
    s.activate_adapter("ws1", "odoo")
    client = TestClient(create_app(s, secret="", registry_token="reg-secret"))
    # List is for the UI: public read, but the bearer must never appear.
    listed = client.get("/adapters?workspace=ws1")
    assert listed.status_code == 200
    rows = listed.json()["adapters"]
    assert rows[0]["adapter_id"] == "odoo" and rows[0]["active"] == 1
    assert "supersecret" not in json.dumps(rows)
    assert rows[0].get("bearer") in (None, "", "***")


def test_active_endpoint_404_when_none_active() -> None:
    s = _store()
    client = TestClient(create_app(s, secret="", registry_token=""))
    # No token configured → open; no active adapter → 404 (not a 500, not a silent null).
    assert client.get("/adapters/active?workspace=ws1").status_code == 404


def test_recent_enriches_executed_event_with_entity_detail() -> None:
    # An executed event omits verb/tier from the body (they live on the proposal) but carries a
    # rich result.entity + ssot — the timeline must derive verb + surface system/entity, not show blank.
    s = _store()
    env = {
        "nil": "0.1", "id": "ex1", "performative": "EVENT", "grant": "odoo", "workspace": "owner",
        "body": {"event": "executed", "severity": "info", "proposal": "p9", "result": {
            "claim": "success", "changed": True,
            "entity": {"type": "crm.create_lead", "id": "42", "url": "/leads/42"},
            "ssot": {"system": "odoo_crm"},
            "compensation": {"reversibility": "REVERSIBLE", "token": "tok42"}}},
    }
    assert s.ingest(env, 1) is True
    row = s.recent()[0]
    assert row["verb"] == "crm.create_lead"          # derived from result.entity.type
    assert row["system"] == "odoo_crm"               # surfaced from ssot
    assert row["entity_id"] == "42" and row["entity_url"] == "/leads/42"
    assert row["summary"] == "leads/42"              # human one-liner
    assert row["reversibility"] == "REVERSIBLE" and row["compensation_token"] == "tok42"


def test_recent_joins_executed_to_proposed_for_verb_tier_and_name() -> None:
    # The executed event has no verb/tier/preview; its matching 'proposed' event does. The timeline
    # must surface the real verb, tier, and the human one-liner (with the name) from the proposal.
    s = _store()
    s.ingest({
        "nil": "0.1", "id": "pp1", "performative": "EVENT", "workspace": "owner",
        "body": {"event": "proposed", "proposal": "px", "verb": "crm.create_contact", "tier": "MEDIUM",
                 "preview": {"en": "Create contact «Ahmad Saleh»", "ar": "إنشاء جهة اتصال"}},
    }, 1)
    s.ingest({
        "nil": "0.1", "id": "ee1", "performative": "EVENT", "workspace": "owner",
        "body": {"event": "executed", "proposal": "px", "result": {
            "entity": {"type": "contact", "id": "18", "url": "/res-partner/18"},
            "ssot": {"system": "odoo_crm"}}},
    }, 2)
    ex = next(r for r in s.recent() if r["event"] == "executed")
    assert ex["verb"] == "crm.create_contact"   # from the proposal, not the bare entity type
    assert ex["tier"] == "MEDIUM"               # fills the empty tier column
    assert ex["summary"] == "Create contact «Ahmad Saleh»"  # the human one-liner with the name


def test_recent_surfaces_partial_verification_when_a_field_dropped() -> None:
    # The headline fix: a commit that CLAIMS success but left a field unwritten must read 'partial',
    # derived from claim + ssot.unverified_fields — NOT the bare `verified` flag that lied when
    # country_id silently dropped. The green-on-a-broken-write is exactly what this column catches.
    s = _store()
    s.ingest({
        "nil": "0.1", "id": "v1", "performative": "EVENT", "workspace": "owner",
        "body": {"event": "executed", "proposal": "pc", "result": {
            "claim": "success", "changed": True, "verified": True,
            "entity": {"type": "contact", "id": "39", "url": "/res-partner/39"},
            "ssot": {"system": "odoo_crm", "read_after_write": True, "unverified_fields": ["country_id"]}}},
    }, 1)
    row = s.recent()[0]
    assert row["verify"] == "partial"   # unverified_fields non-empty overrides the green claim
    assert "id" in row                  # a stable handle for the detail fetch


def test_recent_verify_is_verified_on_a_clean_readback() -> None:
    s = _store()
    s.ingest({"nil": "0.1", "id": "v2", "performative": "EVENT", "workspace": "owner",
              "body": {"event": "executed", "proposal": "pd", "result": {
                  "claim": "success", "verified": True,
                  "ssot": {"system": "odoo_crm", "unverified_fields": []}}}}, 1)
    assert s.recent()[0]["verify"] == "verified"


def test_recent_verify_is_failed_on_failure_claim() -> None:
    s = _store()
    s.ingest({"nil": "0.1", "id": "v3", "performative": "EVENT", "workspace": "owner",
              "body": {"event": "executed", "proposal": "pf",
                       "result": {"claim": "failure", "verified": False, "ssot": {}}}}, 1)
    assert s.recent()[0]["verify"] == "failed"


def test_recent_verify_is_none_for_a_proposed_event() -> None:
    # No write happened yet → no verification verdict to show (the column stays blank, not green).
    s = _store()
    s.ingest(_proposed(1, proposal="pp"), 1)
    assert s.recent()[0]["verify"] is None


def test_detail_assembles_the_full_payload_journey_with_field_diff() -> None:
    # Clicking a row must reconstruct the action end-to-end from the log alone: raw intent → resolved
    # values → field-level SSOT verdict → effect — without opening Odoo. This is the expand contract.
    s = _store()
    s.ingest({"nil": "0.1", "id": "pj1", "performative": "EVENT", "workspace": "owner",
              "body": {"event": "proposed", "proposal": "pj", "verb": "crm.update_contact", "tier": "MEDIUM",
                       "preview": {"en": "Update contact", "ar": "تحديث جهة اتصال"},
                       "resolved": {"name": "Ahmad", "country_id": 190},
                       "expires_at": "2026-06-22T01:00:00Z"}}, 1)
    s.ingest({"nil": "0.1", "id": "pj2", "performative": "EVENT", "workspace": "owner",
              "body": {"event": "executed", "proposal": "pj", "args": {"name": "Ahmad", "country": "السعودية"},
                       "result": {"claim": "success", "verified": True,
                                  "entity": {"type": "contact", "id": "39", "url": "/res-partner/39"},
                                  "ssot": {"system": "odoo_crm", "read_after_write": True,
                                           "unverified_fields": ["country_id"]},
                                  "compensation": {"reversibility": "COMPENSABLE", "token": "tok"}}}}, 2)
    ex = next(r for r in s.recent() if r["event"] == "executed")
    d = s.detail(ex["id"])
    assert d is not None
    assert d["verb"] == "crm.update_contact" and d["tier"] == "MEDIUM"   # joined from the proposal
    assert d["verify"] == "partial"
    assert d["raw_args"] == {"name": "Ahmad", "country": "السعودية"}     # what the agent actually sent
    assert d["resolved"] == {"name": "Ahmad", "country_id": 190}          # after resolution
    by = {f["field"]: f for f in d["fields"]}
    assert by["country_id"]["verified"] is False and by["country_id"]["requested"] == 190  # dropped
    assert by["name"]["verified"] is True                                # confirmed in SSOT
    assert d["result"]["compensation"]["token"] == "tok"
    assert [j["event"] for j in d["journey"]] == ["proposed", "executed"]  # ordered saga


def test_detail_uses_adapter_emitted_before_after_diff_when_present() -> None:
    # When the adapter emits result.ssot.fields (real before→after read-back), the detail must
    # surface those true values — not re-derive a thinner diff from the proposal's resolved args.
    s = _store()
    s.ingest({"nil": "0.1", "id": "fd1", "performative": "EVENT", "workspace": "owner",
              "body": {"event": "executed", "proposal": "pq", "result": {
                  "claim": "partial", "verified": False,
                  "ssot": {"system": "odoo_crm", "read_after_write": True,
                           "unverified_fields": ["country_id"],
                           "fields": [
                               {"field": "name", "before": "Ahmad", "requested": "Ahmad Saleh",
                                "after": "Ahmad Saleh", "verified": True},
                               {"field": "country_id", "before": False, "requested": 190,
                                "after": False, "verified": False}]}}}}, 1)
    d = s.detail(s.recent()[0]["id"])
    by = {f["field"]: f for f in d["fields"]}
    assert by["country_id"]["before"] is False and by["country_id"]["after"] is False
    assert by["country_id"]["requested"] == 190 and by["country_id"]["verified"] is False
    assert by["name"]["before"] == "Ahmad" and by["name"]["after"] == "Ahmad Saleh"


def test_detail_endpoint_returns_journey_and_404s_unknown() -> None:
    s = _store()
    s.ingest(_event(1, ev="executed", proposal="pp"), 1)
    client = TestClient(create_app(s, secret=""))
    ex = client.get("/api/events").json()["events"][0]
    d = client.get(f"/api/events/{ex['id']}").json()
    assert d["id"] == ex["id"]
    assert client.get("/api/events/999999").status_code == 404


def test_registry_view_is_public_and_redacted(monkeypatch) -> None:
    monkeypatch.setenv("NIL_WORKSPACE", "owner")
    s = _store()
    s.register_adapter("owner", "odoo", url="https://odoo/nil", bearer="supersecret", system="odoo_crm")
    s.activate_adapter("owner", "odoo")
    client = TestClient(create_app(s, secret="", registry_token="reg-tok"))
    # Public read (no auth) for the cp page — scoped to NIL_WORKSPACE, bearer never present.
    body = client.get("/api/registry").json()
    assert body["workspace"] == "owner"
    assert body["adapters"][0]["adapter_id"] == "odoo" and body["adapters"][0]["active"] == 1
    assert "supersecret" not in json.dumps(body)

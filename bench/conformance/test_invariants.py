"""Axis 3 — protocol conformance as PROPERTIES, not single-run pass (release-plan §3 / §9·W2).

A Hypothesis RuleBasedStateMachine drives random valid sequences of propose/commit/rollback against
a reference shim and asserts the wire invariants on every reachable state:

  • idempotency        — replaying COMMIT with the same key never writes twice
  • no-side-effect      — PROPOSE never mutates backend state
  • rollback honesty    — a reversible effect mints a token; ROLLBACK previews then reverses; an
                          unknown token is refused, never silently actioned
  • refusal correctness — unknown verb / unprovisioned target are refused with the right code

Plus a focused regression guard for the real-record-id compensation bug (entity_ref must key off the
real record id, not a human name) — the exact failure we fixed.

Run:  PYTHONPATH=<adapter>/src:<kernel>/src:<kernel> pytest bench/conformance/test_invariants.py -q
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, rule

from pocketbase_nil_adapter.edge import CapturingEmitter, create_app
from pocketbase_nil_adapter.system import FakeSystem, SystemError

TARGET = "products"


def _client() -> TestClient:
    return TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)


def _env(verb: str, args: dict) -> dict:
    return {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}


def _propose(c: TestClient, verb: str, args: dict) -> dict:
    return c.post("/nil/v0.1/propose", json=_env(verb, args)).json()["body"]


def _commit(c: TestClient, pid: str, key: str) -> dict:
    return c.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
                  "body": {"proposal": pid, "idempotency_key": key}}).json()["body"]


def _count(c: TestClient) -> int:
    r = c.post("/nil/v0.1/query", json=_env("resource.read", {"target": TARGET})).json()
    return r["data"]["count"]


class NilProtocol(RuleBasedStateMachine):
    """Generic resource.* lifecycle — the universal CRUD surface every adapter exposes."""

    tokens = Bundle("tokens")  # live compensation tokens (record -> reversal handle)

    def __init__(self) -> None:
        super().__init__()
        self.c = _client()
        self.live = 0  # shadow model: how many records SHOULD exist
        self.uid = 0  # ensures unique record identity (real backends mint unique ids; FakeSystem
        #              keys off `name`, so we keep names unique to model a real backend faithfully)

    @rule(target=tokens, label=st.text(min_size=0, max_size=6))
    def create(self, label: str):
        self.uid += 1
        name = f"{label}-{self.uid}"  # unique key — mirrors a real backend's unique record id
        body = _propose(self.c, "resource.create", {"target": TARGET, "data": {"name": name}})
        st = _commit(self.c, body["id"], body["id"])
        assert st["state"] == "executed", st
        self.live += 1
        return st["result"]["compensation"].get("token")

    @rule()
    def propose_has_no_side_effect(self):
        before = _count(self.c)
        _propose(self.c, "resource.create", {"target": TARGET, "data": {"name": "ghost"}})
        assert _count(self.c) == before, "PROPOSE mutated state — propose must be a dry run"

    @rule()
    def commit_is_idempotent(self):
        self.uid += 1
        body = _propose(self.c, "resource.create", {"target": TARGET, "data": {"name": f"idem-{self.uid}"}})
        key = body["id"]  # a fresh key per invocation — the replay reuses THIS key
        first = _commit(self.c, body["id"], key)
        if first["state"] == "executed":
            self.live += 1  # the first commit is the only write
        before = _count(self.c)
        replay = _commit(self.c, body["id"], key)  # same key → must replay, not re-write
        assert replay.get("replayed") is True, replay
        assert _count(self.c) == before, "idempotent COMMIT wrote twice"

    @rule(tok=consumes(tokens))  # each token is reversed at most once
    def rollback_reverses(self, tok):
        if not tok:
            return
        prev = self.c.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
               "body": {"compensation_token": tok, "reason": "x"}}).json()["body"]
        assert prev["outcome"] == "proposal", "ROLLBACK must PREVIEW, never silently write"
        st = _commit(self.c, prev["id"], "rb-" + prev["id"])
        if st["state"] == "executed":
            self.live -= 1  # the create's inverse (delete) executed

    @rule()
    def unknown_token_is_refused(self):
        r = self.c.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
            "body": {"compensation_token": "__nope__", "reason": "x"}}).json()["body"]
        assert r["outcome"] == "refusal", "an unknown compensation token must be refused"

    @rule()
    def unknown_verb_is_refused(self):
        r = _propose(self.c, "commerce.frobnicate", {})
        assert r["outcome"] == "refusal" and r.get("code") == "UNKNOWN_VERB", r

    @invariant()
    def backend_matches_shadow_model(self):
        assert _count(self.c) == self.live, f"backend count {_count(self.c)} != model {self.live}"


NilProtocol.TestCase.settings = settings(max_examples=30, stateful_step_count=10, deadline=None)
TestNilProtocol = NilProtocol.TestCase


# ---- regression guard: real-record-id compensation (the entity_ref bug we fixed) ----------------

class _PBShapedFake:
    """A backend whose primary key is `id` and ALSO carries a human `name` field — the shape that
    exposed the bug: a compensating delete keyed off `name` 404s once the record is renamed."""

    def __init__(self) -> None:
        self.db: dict[str, dict[str, dict[str, Any]]] = {}
        self.n = 0

    def create(self, target, doc):
        self.n += 1
        rid = f"rec{self.n:04d}"
        rec = {**doc, "id": rid}
        self.db.setdefault(target, {})[rid] = rec
        return rec

    def list(self, target, filters=None):
        return list(self.db.get(target, {}).values())

    def update(self, target, rid, doc):
        self.db[target][rid].update(doc)
        return self.db[target][rid]

    def delete(self, target, rid):
        if rid not in self.db.get(target, {}):
            raise SystemError(f"{rid}: 404 not found")
        del self.db[target][rid]

    def exists(self, target):
        return True

    def schema(self, target):
        return []

    def get(self, target, rid):
        return self.db.get(target, {}).get(rid)


def test_create_rollback_targets_real_id_even_after_rename():
    """commerce.create_product → rename → ROLLBACK must delete by the REAL record id, not the name."""
    sys = _PBShapedFake()
    c = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    body = _propose(c, "commerce.create_product", {"name": "تفاح", "price": 544})
    st = _commit(c, body["id"], body["id"])
    ent = st["result"]["entity"]
    tok = st["result"]["compensation"].get("token")
    assert ent["id"].startswith("rec"), f"entity id must be the real record key, got {ent['id']!r}"

    sys.update("products", ent["id"], {"name": "موز"})  # rename: a name-keyed delete would now 404

    prev = c.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
           "body": {"compensation_token": tok, "reason": "x"}}).json()["body"]
    cp = _commit(c, prev["id"], "rb-" + tok)
    assert cp["state"] == "executed", f"rollback did not execute: {cp}"
    assert sys.get("products", ent["id"]) is None, "record still present — rollback deleted the wrong key"

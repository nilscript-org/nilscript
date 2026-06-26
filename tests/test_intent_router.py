"""The universal intent router: one nil_intent surface, many execution domains. `about` is routed to
the provider that OWNS it (adapter / graph / automation) — a structural ownership check, never keyword
matching of the user's words. This is what collapses nil_graph/nil_automation/nil_propose/... into the
single Intent payload while keeping each domain's execution layer behind its own provider.
"""

from __future__ import annotations

import pytest

from nilscript.dataplane import Intent, IntentRouter, Outcome


class _FakeProvider:
    def __init__(self, owns_types: set[str], value) -> None:
        self._owns = owns_types
        self._value = value
        self.seen: Intent | None = None

    def owns(self, about: str) -> bool:
        return about in self._owns

    async def resolve(self, intent: Intent) -> Outcome:
        self.seen = intent
        return Outcome.result(self._value)


async def test_router_delegates_to_the_owning_provider() -> None:
    adapter = _FakeProvider({"res.partner"}, {"items": []})
    graph = _FakeProvider({"policy", "cycle"}, {"policies": ["payment-approval"]})
    router = IntentRouter([graph, adapter])

    out = await router.resolve(Intent(about="policy", seek="all"))

    assert out.kind == "result" and out.value == {"policies": ["payment-approval"]}
    assert graph.seen is not None and adapter.seen is None  # routed by ownership, not order


async def test_router_routes_business_entity_to_the_adapter_provider() -> None:
    adapter = _FakeProvider({"res.partner"}, {"items": [{"id": 18}]})
    graph = _FakeProvider({"policy"}, {})
    router = IntentRouter([graph, adapter])

    out = await router.resolve(Intent(about="res.partner", seek="the"))

    assert out.value == {"items": [{"id": 18}]}
    assert adapter.seen is not None and graph.seen is None


async def test_router_refuses_an_about_no_provider_owns() -> None:
    router = IntentRouter([_FakeProvider({"res.partner"}, {})])
    out = await router.resolve(Intent(about="nonsense.thing", seek="all"))
    assert out.kind == "refusal" and out.code == "UNKNOWN_ABOUT"


async def test_first_owning_provider_wins() -> None:
    a = _FakeProvider({"shared"}, {"from": "a"})
    b = _FakeProvider({"shared"}, {"from": "b"})
    out = await IntentRouter([a, b]).resolve(Intent(about="shared", seek="all"))
    assert out.value == {"from": "a"}

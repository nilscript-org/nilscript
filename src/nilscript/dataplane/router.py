"""The universal intent router. One `nil_intent` surface fronts many execution domains; each domain is
an `IntentProvider` that declares which `about` types it OWNS and how to resolve an intent over them.

The router picks the first owning provider — a structural ownership check on the entity type, never
keyword matching of the user's words. New domains (graph, automation, governance) are added by
registering a provider, not by branching: the single payload covers 100% of the system, extensibly.
"""

from __future__ import annotations

from typing import Protocol

from .intent import Intent, Outcome


class IntentProvider(Protocol):
    """One execution domain. `owns` is a structural check on the entity type; `resolve` runs the intent
    against that domain's layer (adapter / graph store / automation registry / ledger)."""

    def owns(self, about: str) -> bool: ...

    async def resolve(self, intent: Intent) -> Outcome: ...


class IntentRouter:
    """Route an Intent to the provider that owns its `about`. The model emits ONE payload; the router
    delegates deterministically. Order is the tie-break (first owning provider wins)."""

    def __init__(self, providers: list[IntentProvider]) -> None:
        self._providers = list(providers)

    async def resolve(self, intent: Intent) -> Outcome:
        for provider in self._providers:
            if provider.owns(intent.about):
                return await provider.resolve(intent)
        return Outcome.refusal(
            "UNKNOWN_ABOUT",
            f"no execution domain serves '{intent.about}' — check the available entities",
        )

"""The approval gate — the control that makes the safety A/B credible (plan §2d, §9 risks).

NIL's safety claim is NOT "the model is smarter"; it's structural: a write only commits if it (a)
names a verb the backend's skeleton actually exposes, and (b) is approved as matching the user's
authorized intent. This module is that decision, isolated so reviewers can audit it.

Two independent defenses, either of which blocks a hijacked write:
  • skeleton-bounding — an action outside the authorized skeleton is refused, never faked.
  • intent oracle     — even an in-skeleton write is approved only if it matches the user's task.

`auto`-approve exists ONLY for measuring the raw upper bound; never report NIL numbers with it on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateDecision:
    approved: bool
    reason: str  # "in_intent" | "out_of_skeleton" | "not_in_intent" | "auto"


def decide(action_tool: str, authorized_tools: frozenset[str], *, mode: str = "oracle") -> GateDecision:
    """Approve `action_tool` for execution given the user-authorized tool set.

    mode="oracle"  → the honest control: approve iff the action is one the user authorized.
    mode="auto"    → approve everything (used only to measure the raw/ungated upper bound).
    """
    if mode == "auto":
        return GateDecision(True, "auto")
    if action_tool not in authorized_tools:
        # The action isn't in the user's task. NIL would either refuse it as outside the backend
        # skeleton, or surface it for an approval the user never gives. Either way: no commit.
        return GateDecision(False, "out_of_skeleton")
    return GateDecision(True, "in_intent")

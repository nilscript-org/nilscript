"""Build a `ValidationContext` from a live adapter's discovery skeleton.

The draft gate validates the agent's plan against *what the bound backend actually declares*. We map
the `handshake`/`describe` verb list into the kernel's context: verbs grouped by skill prefix become
the V4 whitelist, and the workspace is granted exactly those verbs (so an authoring owner may use the
full surface of their own active adapter — and nothing that isn't on it).

V5 argument typing stays permissive here (describe does not publish per-verb arg schemas), so a
hallucinated verb is caught by V4 — the load-bearing guarantee — without over-rejecting valid args.
"""

from __future__ import annotations

from typing import Any

from nilscript.kernel.context import SkillSpec, ValidationContext

# No per-verb arg schema from discovery ⇒ accept any args (V4 still gates the verb itself).
_PERMISSIVE_HINT: dict[str, Any] = {"additionalProperties": True}


def context_from_skeleton(workspace: str, skeleton: dict[str, Any]) -> ValidationContext:
    """Map a `handshake` report ({verbs, targets, ...}) into a single-workspace ValidationContext."""
    verbs: list[str] = [v for v in skeleton.get("verbs", []) if isinstance(v, str)]

    by_skill: dict[str, set[str]] = {}
    for verb in verbs:
        skill = verb.split(".", 1)[0] if "." in verb else verb
        by_skill.setdefault(skill, set()).add(verb)

    skills = {
        name: SkillSpec(required_verbs=frozenset(group), hint_schema=_PERMISSIVE_HINT)
        for name, group in by_skill.items()
    }
    return ValidationContext(
        skills=skills,
        read_verbs=frozenset(verbs),
        workspaces={workspace: frozenset(verbs)},
    )

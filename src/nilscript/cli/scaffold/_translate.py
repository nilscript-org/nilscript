"""Render `translate.py` for a scaffolded shim — the one file (besides system.py) the developer
fills (plan §3.1).

Active verbs get a fillable stub that raises `NotImplementedError`. Deprecated/parked verbs get a
`# PARKED — do not implement` marker and NO stub, so the toolkit carries the parking decision and no
one builds on a verb the standard retired (plan §3.1, GAP-001).
"""

from __future__ import annotations

from nilscript.cli._spec import Verb

_HEADER = '''\
"""The translation core: NIL verb args ⇄ your backend's native documents. GENERATED skeleton.

Pure mapping, no I/O — the only module (besides system.py) that knows backend specifics. Fill each
`to_native` / `run` stub. The NIL edge in `edge.py` never changes when you add a verb.

PARKED verbs (deprecated in the standard) are intentionally ABSENT below — the standard says do not
implement them, so the shim cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from {pkg}.system import SystemClient

Bilingual = dict[str, str]


@dataclass(frozen=True)
class WriteVerb:
    verb: str
    tier: str
    doctype: str  # native target type (rename per your backend\'s vocabulary)
    required: tuple[str, ...]
    to_native: Callable[[dict[str, Any]], dict[str, Any]]
    preview: Callable[[dict[str, Any]], Bilingual]
    entity_type: str

    def missing(self, args: dict[str, Any]) -> list[str]:
        return [field for field in self.required if not args.get(field)]


@dataclass(frozen=True)
class QueryVerb:
    verb: str
    run: Callable[[SystemClient, dict[str, Any]], dict[str, Any]]
'''


def _title_target(action: str) -> str:
    """A placeholder native target name derived from the verb action: create_invoice -> Invoice."""
    base = action.split("_", 1)[-1] if "_" in action else action
    return base.replace("_", " ").title()


def _write_stub(verb: Verb) -> str:
    required = tuple(verb.required)
    fn = f"_to_native_{verb.action}"
    return f'''\

def {fn}(args: dict[str, Any]) -> dict[str, Any]:
    # TODO: map NIL args -> a native {verb.namespace} document for {verb.name!r}.
    # Hidden requirements (company, accounts, …) are pre-filled from the manifest — see manifest.py.
    raise NotImplementedError("fill {fn}: build the native doc for {verb.name}")
'''


def _query_stub(verb: Verb) -> str:
    fn = f"_run_{verb.action}"
    return f'''\

def {fn}(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    # TODO: read business truth fresh for {verb.name!r} via client.list(...).
    raise NotImplementedError("fill {fn}: read-through for {verb.name}")
'''


def _write_entry(verb: Verb) -> str:
    tier = verb.tier_floor or "MEDIUM"
    return (
        f'    "{verb.name}": WriteVerb(\n'
        f'        verb="{verb.name}",\n'
        f'        tier="{tier}",\n'
        f'        doctype="{_title_target(verb.action)}",  # TODO: your backend\'s native type\n'
        f"        required={verb.required!r},\n"
        f"        to_native=_to_native_{verb.action},\n"
        f'        preview=lambda a: {{"en": "{verb.name}", "ar": "{verb.name}"}},  # TODO: human preview\n'
        f'        entity_type="{verb.action}",\n'
        f"    ),\n"
    )


def render_translate(
    pkg: str,
    write_verbs: tuple[Verb, ...],
    query_verbs: tuple[Verb, ...],
    parked: tuple[Verb, ...],
) -> str:
    parts = [_HEADER.format(pkg=pkg)]

    for verb in write_verbs:
        parts.append(_write_stub(verb))
    for verb in query_verbs:
        parts.append(_query_stub(verb))

    parts.append("\n\nWRITE_VERBS: dict[str, WriteVerb] = {\n")
    for verb in write_verbs:
        parts.append(_write_entry(verb))
    parts.append("}\n")

    parts.append("\n\nQUERY_VERBS: dict[str, QueryVerb] = {\n")
    for verb in query_verbs:
        parts.append(f'    "{verb.name}": QueryVerb(verb="{verb.name}", run=_run_{verb.action}),\n')
    parts.append("}\n")

    if parked:
        parts.append("\n\n# PARKED — do not implement (deprecated in the standard):\n")
        for verb in parked:
            ref = f" ({verb.gap_ref})" if verb.gap_ref else ""
            parts.append(f"#   {verb.name}{ref}\n")

    parts.append(
        "\n\ndef entity_ref(verb: WriteVerb, created: dict[str, Any]) -> dict[str, Any]:\n"
        "    # The SSOT entity id MUST be the backend's real record key, so a compensating\n"
        "    # update/delete (ROLLBACK) targets the record itself — never a human attribute that\n"
        "    # can collide or change. Same precedence the generic resource.* path uses: id, then\n"
        "    # name (Frappe-style backends whose primary key IS `name` fall through correctly).\n"
        '    rid = created.get("id") or created.get("name") or ""\n'
        '    slug = verb.doctype.lower().replace(" ", "-")\n'
        '    return {"type": verb.entity_type, "id": rid, "url": f"/{slug}/{rid}"}\n'
    )
    return "".join(parts)

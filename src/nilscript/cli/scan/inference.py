"""The error->requirement inference engine (plan §4.3).

A library of **signature -> structured-requirement** rules. Each rule is *data* (a compiled regex
over the native error text plus a function that turns the match into findings), so a new system's
error dialect is a new rule, contributable without touching the engine. The rules here cover the
exact signatures hit building the live ERPNext shim (plan §0); an LLM-assisted fallback for unseen
errors is a later phase — until then an unseen error honestly yields nothing (never a guess).

Every finding is grounded in a real execution result: this module is only ever handed an error a
system actually returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from nilscript.cli.manifest import MANIFEST_VERSION


@dataclass(frozen=True)
class Finding:
    """One structured requirement inferred from a native error.

    `kind` selects which fields are meaningful:
      - "required_scalar" / "required_on_line" / "required_nested" -> `field`
      - "prerequisite"                                             -> `entity` (+ resolve hint later)
      - "transport_quirk"                                          -> `quirk`
    `evidence` is always the verbatim native error that produced it (kept for the manifest).
    """

    kind: str
    field: str | None = None
    entity: str | None = None
    quirk: str | None = None
    evidence: str | None = None


# --- the rule set: (compiled signature, match -> findings) ------------------------------------

def _link_validation(match: re.Match[str], error: str) -> list[Finding]:
    doctype = match.group("doctype").strip()
    return [Finding(kind="prerequisite", entity=_entity_slug(doctype), evidence=error)]


def _income_account(match: re.Match[str], error: str) -> list[Finding]:
    # ERPNext's "Income Account None does not belong to company X" teaches two requirements at once
    # (plan §4.3): the line needs an income_account, and the doc needs a company.
    return [
        Finding(kind="required_on_line", field="income_account", evidence=error),
        Finding(kind="required_scalar", field="company", evidence=error),
    ]


def _expect_100(match: re.Match[str], error: str) -> list[Finding]:
    return [Finding(kind="transport_quirk", quirk="no_expect_100_continue", evidence=error)]


def _mandatory_fields(match: re.Match[str], error: str) -> list[Finding]:
    raw = match.group("fields")
    fields = [_field_slug(part) for part in re.split(r"[,;]", raw) if part.strip()]
    return [Finding(kind="required_scalar", field=f, evidence=error) for f in fields]


# Order matters only for readability; rules are independent and all are tried.
INFERENCE_RULES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str], str], list[Finding]]], ...] = (
    (re.compile(r"Could not find\s+(?P<doctype>[A-Z][A-Za-z ]+?)\s*(?::|$)"), _link_validation),
    (re.compile(r"Income Account\b.*\bdoes not belong to company", re.IGNORECASE), _income_account),
    (re.compile(r"\b417\b|EXPECTATION FAILED", re.IGNORECASE), _expect_100),
    (re.compile(r"Mandatory fields required(?:[^:]*)?:\s*(?P<fields>.+)$", re.IGNORECASE | re.MULTILINE), _mandatory_fields),
)


def _field_slug(name: str) -> str:
    """"Cost Center" -> "cost_center"; leaves an already-snake field untouched."""
    return re.sub(r"[\s\-]+", "_", name.strip()).lower()


def _entity_slug(doctype: str) -> str:
    """ERPNext DocType -> NIL entity slug: "Customer" -> "customer", "Sales Invoice" -> "sales_invoice"."""
    return _field_slug(doctype)


def infer(error: str) -> list[Finding]:
    """Map a single native error string to zero or more structured findings (plan §4.3).

    Returns [] for an error no rule recognizes — honest non-inference, not a guess.
    """
    findings: list[Finding] = []
    for pattern, handler in INFERENCE_RULES:
        match = pattern.search(error)
        if match:
            findings.extend(handler(match, error))
    return findings


# --- assembling findings into a manifest (plan §4.2) ------------------------------------------

_LINE_KINDS = {"required_on_line", "required_nested"}


def build_manifest(
    system: str,
    samples: list[dict[str, Any]],
    *,
    resolve_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble a `requirements-manifest.json` (as a dict) from replayed collision samples.

    `samples` is a list of `{verb, errors: [native error strings], native_target?}` — exactly what a
    live probe captures, replayable deterministically. `resolve_hints` maps an inferred entity slug
    to the NIL verb that creates it (e.g. `{"customer": "services.create_client"}`), filling the
    prerequisite's `resolve_with`. Findings are deduped; transport quirks bubble to the top level.

    The result is shape-valid against `manifest.validate` and carries STRUCTURAL requirements only —
    instance values are never inferred from an error (plan §5 separation).
    """
    hints = resolve_hints or {}
    verbs: dict[str, Any] = {}
    quirks: dict[str, dict[str, Any]] = {}  # quirk name -> entry (dedup by name)
    # Dedup is per VERB across all samples (a verb can recur in many samples) — not per sample,
    # or the same hidden requirement would be recorded once per sample that surfaced it.
    seen_fields: dict[str, set[str]] = {}
    seen_entities: dict[str, set[str]] = {}

    for sample in samples:
        verb_name = sample["verb"]
        verb_entry = verbs.setdefault(
            verb_name, {"hidden_requirements": [], "prerequisites": []}
        )
        if sample.get("native_target"):
            verb_entry["native_target"] = sample["native_target"]

        fields_seen = seen_fields.setdefault(verb_name, set())
        entities_seen = seen_entities.setdefault(verb_name, set())

        for error in sample.get("errors", []):
            for finding in infer(error):
                if finding.kind == "transport_quirk":
                    quirks.setdefault(
                        finding.quirk or "",
                        {"quirk": finding.quirk, "evidence": finding.evidence},
                    )
                elif finding.kind == "prerequisite":
                    if finding.entity in entities_seen:
                        continue
                    entities_seen.add(finding.entity or "")
                    prereq: dict[str, Any] = {"entity": finding.entity}
                    if finding.entity in hints:
                        prereq["resolve_with"] = hints[finding.entity]
                    verb_entry["prerequisites"].append(prereq)
                else:  # a field requirement
                    if finding.field in fields_seen:
                        continue
                    fields_seen.add(finding.field or "")
                    verb_entry["hidden_requirements"].append(
                        {"field": finding.field, "kind": finding.kind}
                    )

    # Drop empty prerequisite/requirement lists for a tidy manifest.
    for entry in verbs.values():
        if not entry["prerequisites"]:
            entry.pop("prerequisites")
        if not entry["hidden_requirements"]:
            entry.pop("hidden_requirements")

    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "system": system,
        "nil_spec": "0.1",
        "verbs": verbs,
    }
    if quirks:
        manifest["transport_quirks"] = list(quirks.values())
    return manifest

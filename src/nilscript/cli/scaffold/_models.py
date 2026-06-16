"""Generate pydantic models from the NIL standard's JSON-Schema arg profiles (plan §3.1).

A focused emitter for the JSON-Schema subset the NIL profiles actually use (object / string /
number / integer / boolean / array, plus `required`, `pattern`, numeric bounds, `description`,
`additionalProperties: false`). Generating straight from JSON Schema sidesteps the OpenAPI 3.0/3.1
codegen trap (plan §3.1) and keeps the toolkit dependency-light and the output deterministic — the
same standard always emits byte-identical models, so the scaffold is reviewable and diffable.
"""

from __future__ import annotations

import json
from typing import Any

from nilscript.cli._spec import Verb

_HEADER = '''\
"""Pydantic models GENERATED from the NIL standard\'s JSON-Schema arg profiles.

Do NOT edit by hand — regenerate with `nilscript scaffold-shim`. One model per ACTIVE verb;
deprecated/parked verbs are intentionally absent (the standard says do not implement them).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
'''

_SCALAR_TYPES = {"string": "str", "number": "float", "integer": "int", "boolean": "bool"}


def model_class_name(verb: Verb) -> str:
    """`services.create_invoice` -> `ServicesCreateInvoiceArgs` (PascalCase + `Args`)."""
    parts = verb.name.replace(".", "_").split("_")
    return "".join(part[:1].upper() + part[1:] for part in parts if part) + "Args"


def _py_type(schema: dict[str, Any]) -> str:
    kind = schema.get("type")
    if kind in _SCALAR_TYPES:
        return _SCALAR_TYPES[kind]
    if kind == "array":
        return f"list[{_py_type(schema.get('items', {}))}]"
    if kind == "object":
        return "dict[str, Any]"
    return "Any"


def _field_constraints(schema: dict[str, Any]) -> list[str]:
    """pydantic `Field(...)` kwargs derived from JSON-Schema keywords present on the property."""
    kwargs: list[str] = []
    mapping = {
        "exclusiveMinimum": "gt",
        "minimum": "ge",
        "exclusiveMaximum": "lt",
        "maximum": "le",
        "minLength": "min_length",
        "maxLength": "max_length",
    }
    for json_key, py_key in mapping.items():
        if json_key in schema:
            kwargs.append(f"{py_key}={schema[json_key]!r}")
    if "pattern" in schema:
        kwargs.append(f"pattern={schema['pattern']!r}")
    if schema.get("description"):
        kwargs.append(f"description={schema['description']!r}")
    return kwargs


def _field_line(name: str, schema: dict[str, Any], *, required: bool) -> str:
    py_type = _py_type(schema)
    constraints = _field_constraints(schema)
    if required:
        if constraints:
            return f"    {name}: {py_type} = Field(..., {', '.join(constraints)})"
        return f"    {name}: {py_type}"
    if constraints:
        return f"    {name}: {py_type} | None = Field(None, {', '.join(constraints)})"
    return f"    {name}: {py_type} | None = None"


def _render_one(verb: Verb, profile: dict[str, Any]) -> str:
    required = set(profile.get("required", []))
    properties: dict[str, Any] = profile.get("properties", {})
    lines = [f"class {model_class_name(verb)}(BaseModel):"]
    title = profile.get("title") or verb.name
    lines.append(f'    """{verb.name} args. {title}"""')
    # additionalProperties: false -> forbid unknown fields (the profile default for NIL verbs).
    forbid = profile.get("additionalProperties", True) is False
    if forbid:
        lines.append('    model_config = ConfigDict(extra="forbid")')
    lines.append("")
    if not properties:
        lines.append("    pass")
        return "\n".join(lines)
    # required first (no defaults), then optional — keeps a valid field order.
    for name in list(properties):
        if name in required:
            lines.append(_field_line(name, properties[name], required=True))
    for name in list(properties):
        if name not in required:
            lines.append(_field_line(name, properties[name], required=False))
    return "\n".join(lines)


def render_models(verbs: tuple[Verb, ...]) -> str:
    """Return the source of a `models.py` with one pydantic model per (active) verb in `verbs`."""
    blocks = [_HEADER]
    for verb in verbs:
        profile = json.loads(verb.path.read_text(encoding="utf-8"))
        blocks.append(_render_one(verb, profile))
    return "\n\n".join(blocks) + "\n"

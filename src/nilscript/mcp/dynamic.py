"""Skeleton-driven dynamic tools: one `propose_<verb>` per verb the backend actually exposes.

The differentiator made literal — the MCP tool list IS the adapter's skeleton. An agent is never
presented a verb the backend doesn't declare. Each tool is a typed shortcut to `NilTools.propose`:
still a preview, still requires `nil_commit` to write.

Each tool carries a **rich inputSchema synthesized from the verb's profile** — the agent gets typed,
named, validated arguments (not an opaque `args` blob). We do this by giving the registered function
a synthesized `__signature__` from the profile's properties; FastMCP derives the schema and validates
against it. A hidden `ctx` parameter carries the MCP Context for per-connection session isolation.
"""

from __future__ import annotations

import inspect
import json
from importlib import resources
from typing import Any

from nilscript.mcp.tools import NilTools, session_key

_JSON_TO_PY: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def load_profiles() -> dict[str, dict[str, Any]]:
    """Map every verb in the bundled profiles to its JSON-Schema profile."""
    out: dict[str, dict[str, Any]] = {}
    spec = resources.files("nilscript.sdk").joinpath("spec/0.1/profiles")
    for profile_dir in spec.iterdir():
        if not profile_dir.is_dir():
            continue
        family = profile_dir.name.replace("-v1", "")
        for f in profile_dir.iterdir():
            if f.name.endswith(".json") and ".response" not in f.name:
                out[f"{family}.{f.name[:-5]}"] = json.loads(f.read_text())
    return out


def tool_name_for(verb: str) -> str:
    """`commerce.create_product` -> `propose_commerce_create_product` (MCP-tool-name-safe)."""
    return "propose_" + verb.replace(".", "_")


def describe_verb(verb: str, schema: dict[str, Any] | None) -> str:
    head = f"PROPOSE {verb} — preview only, no write (commit the returned proposal with nil_commit)."
    if not schema:
        return head + " Args: see nil_describe."
    props = schema.get("properties", {})
    required = list(schema.get("required", []))
    optional = [k for k in props if k not in required]
    req = ", ".join(required) if required else "—"
    opt = ", ".join(optional) if optional else "—"
    return f"{head} required: {req}; optional: {opt}."


def _typed_propose_fn(tools: NilTools, verb: str, schema: dict[str, Any] | None, context_cls: Any):
    """Build an async fn whose signature mirrors the verb's profile, so FastMCP emits a rich schema.

    A trailing keyword-only `ctx` (annotated as the FastMCP Context) is injected by the server and
    used for per-connection session isolation; it never appears in the tool's inputSchema.
    """
    props: dict[str, Any] = (schema or {}).get("properties", {})
    required = set((schema or {}).get("required", []))

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    if props:
        # required first (no default), then optional (default None) — both keyword-only.
        for name in list(required) + [k for k in props if k not in required]:
            ann = _JSON_TO_PY.get(props.get(name, {}).get("type"), Any)
            annotations[name] = ann
            if name in required:
                params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=ann))
            else:
                params.append(
                    inspect.Parameter(
                        name, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=None
                    )
                )
    else:
        # no profile: fall back to a single free-form args object.
        annotations["args"] = dict
        params.append(
            inspect.Parameter("args", inspect.Parameter.KEYWORD_ONLY, annotation=dict, default=None)
        )

    params.append(
        inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=context_cls, default=None)
    )
    annotations["ctx"] = context_cls
    annotations["return"] = dict

    async def _impl(**kwargs: Any) -> dict[str, Any]:
        ctx = kwargs.pop("ctx", None)
        if "args" in kwargs and len(kwargs) == 1:
            args = kwargs["args"] or {}
        else:
            args = {k: v for k, v in kwargs.items() if v is not None}
        return await tools.propose(verb, args, session_id=session_key(ctx))

    _impl.__name__ = tool_name_for(verb)
    _impl.__doc__ = describe_verb(verb, schema)
    _impl.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    _impl.__annotations__ = annotations
    return _impl


def register_dynamic_tools(
    server: Any,
    tools: NilTools,
    verbs: list[str],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Bind one richly-typed `propose_<verb>` tool per skeleton verb onto `server`."""
    from mcp.server.fastmcp import Context

    catalog = profiles if profiles is not None else load_profiles()
    registered: list[str] = []
    for verb in verbs:
        name = tool_name_for(verb)
        fn = _typed_propose_fn(tools, verb, catalog.get(verb), Context)
        server.add_tool(fn, name=name, description=describe_verb(verb, catalog.get(verb)))
        registered.append(name)
    return registered

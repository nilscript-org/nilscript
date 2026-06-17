"""Connect handshake — the universal way a kernel client attaches to an adapter.

Any consumer (UI, CLI, another service) connects the same way instead of reinventing it:
reachable -> conformant -> readiness of native targets. It speaks only NIL (the adapter's
`/nil/v0.1/describe` discovery endpoint), so it works against ANY conformant adapter with no
backend-specific knowledge — the adapter reports its own verbs and per-target readiness.
"""

from __future__ import annotations

from typing import Any

from nilscript.sdk.transport import NilTransport

DESCRIBE_PATH = "/nil/v0.1/describe"


async def handshake(transport: NilTransport) -> dict[str, Any]:
    """Return a structured connection report for the adapter behind `transport`.

    Keys: reachable, conformant, system, nil, verbs, targets{name: ready},
    ready[], missing[]. `reachable=False` means the shim didn't answer; `conformant=False`
    means it answered but not with a valid NIL describe shape.
    """
    try:
        d = await transport.get(DESCRIBE_PATH)
    except Exception:  # noqa: BLE001 — any transport/parse failure means "not reachable"
        return {"reachable": False, "conformant": False, "verbs": [], "targets": {},
                "ready": [], "missing": []}

    targets = d.get("targets", {}) if isinstance(d, dict) else {}
    conformant = isinstance(d, dict) and d.get("nil") == "0.1" and bool(d.get("verbs"))

    def _ready(v: Any) -> bool:
        # targets carry the skeleton {exists, fields}; tolerate the older bool shape too.
        return v.get("exists", False) if isinstance(v, dict) else bool(v)

    return {
        "reachable": True,
        "conformant": conformant,
        "system": d.get("system"),
        "nil": d.get("nil"),
        "verbs": d.get("verbs", []),
        "targets": targets,  # {name: {exists, fields:[{name,type,required}]}}
        "ready": [t for t, v in targets.items() if _ready(v)],
        "missing": [t for t, v in targets.items() if not _ready(v)],
    }

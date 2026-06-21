"""`nilscript adapters …` — the AUTHENTICATED surface for the active-adapter registry.

Activation must never be a public browser button (the control plane is a public URL, and the
registry holds adapter bearers). Instead the single operator drives it from here with the token in
an env var:

    NIL_REGISTRY_URL=https://cp.nilscript.org NIL_REGISTRY_TOKEN=… \
        nilscript adapters list --workspace owner
    nilscript adapters activate odoo --workspace owner
    nilscript adapters register odoo --url https://… --bearer … --system odoo_crm --activate

Talks to the control plane over plain HTTP (stdlib only) so it has no MCP/SDK dependency.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _cp_base(args: argparse.Namespace) -> str | None:
    base = args.cp or os.environ.get("NIL_REGISTRY_URL", "")
    return base.rstrip("/") if base else None


def _call(method: str, url: str, *, token: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - operator-supplied CP URL
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "null")
        except (ValueError, TypeError):
            return exc.code, {"error": exc.reason}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": str(exc)}


def _cmd_adapters(args: argparse.Namespace) -> int:
    base = _cp_base(args)
    if not base:
        print("set --cp or NIL_REGISTRY_URL (the control-plane base URL)", file=sys.stderr)
        return 2
    token = os.environ.get("NIL_REGISTRY_TOKEN", "")
    ws = args.workspace or ""

    if args.adapters_command == "list":
        status, data = _call("GET", f"{base}/adapters?workspace={urllib.parse.quote(ws)}", token=token)
        if status != 200:
            print(f"list failed ({status}): {(data or {}).get('error', data)}", file=sys.stderr)
            return 1
        rows = (data or {}).get("adapters", [])
        if not rows:
            print(f"no adapters registered for workspace '{ws}'")
            return 0
        for a in rows:
            mark = "● active" if a.get("active") else "  ·"
            print(f"{mark}  {a.get('adapter_id'):16} {a.get('system') or '-':14} {a.get('url')}")
        return 0

    if args.adapters_command == "activate":
        status, data = _call(
            "POST", f"{base}/adapters/{urllib.parse.quote(ws)}/{urllib.parse.quote(args.adapter_id)}/activate",
            token=token,
        )
        if status != 200:
            print(f"activate failed ({status}): {(data or {}).get('error', data)}", file=sys.stderr)
            return 1
        print(f"activated '{args.adapter_id}' for workspace '{ws}' — the MCP now routes there")
        return 0

    if args.adapters_command == "register":
        body = {
            "workspace": ws, "adapter_id": args.adapter_id, "url": args.url,
            "label": args.label or "", "bearer": args.bearer or "", "system": args.system or "",
        }
        status, data = _call("POST", f"{base}/adapters/register", token=token, body=body)
        if status != 200:
            print(f"register failed ({status}): {(data or {}).get('error', data)}", file=sys.stderr)
            return 1
        print(f"registered '{args.adapter_id}'")
        if args.activate:
            return _cmd_adapters(argparse.Namespace(
                adapters_command="activate", cp=args.cp, workspace=ws, adapter_id=args.adapter_id))
        return 0

    print("usage: nilscript adapters {list|activate|register} …", file=sys.stderr)
    return 2


def add_adapters_parser(sub: Any) -> None:
    """Wire `nilscript adapters …` onto the top-level subparsers."""
    p = sub.add_parser("adapters", help="manage the control-plane active-adapter registry (operator)")
    p.add_argument("--cp", default=None, help="control-plane base URL (or NIL_REGISTRY_URL env)")
    p.add_argument("--workspace", default="", help="workspace the adapter belongs to")
    asub = p.add_subparsers(dest="adapters_command", required=True)

    asub.add_parser("list", help="list registered adapters (bearer redacted)")

    a = asub.add_parser("activate", help="make an adapter the workspace's active MCP backend")
    a.add_argument("adapter_id", help="the adapter id to activate")

    r = asub.add_parser("register", help="register/refresh an adapter the MCP can route to")
    r.add_argument("adapter_id", help="a short id, e.g. 'odoo'")
    r.add_argument("--url", required=True, help="the adapter's NIL edge URL")
    r.add_argument("--bearer", default="", help="bearer to reach the adapter (never the backend creds)")
    r.add_argument("--label", default="", help="human label")
    r.add_argument("--system", default="", help="backend system id, e.g. 'odoo_crm'")
    r.add_argument("--activate", action="store_true", help="activate it immediately after registering")

    p.set_defaults(func=_cmd_adapters)

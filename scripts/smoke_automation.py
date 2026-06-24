#!/usr/bin/env python3
"""Live smoke test for the Automation Registry — exercises the production paths the unit suite mocks.

The test suite injects fake skeleton providers and runners, so the *live default* glue is NOT covered:
the real `describe()` handshake to a live adapter, and the control-plane grant minting
(`GrantRef.from_secret(grant_id="control-plane", secret=<bearer>)`) that drives `LocalExecutor` against
a real backend. This script drives that glue end to end against a running control plane + adapter, so
an operator can confirm the demo path before relying on it.

It does NOT depend on the package — stdlib only — so it can run anywhere the control plane is reachable.

    NIL_REGISTRY_URL=https://cp.example.com \
    NIL_REGISTRY_TOKEN=... \
    NIL_SMOKE_WORKSPACE=ws_demo \
    python scripts/smoke_automation.py

The workspace must already have an ACTIVE adapter. The plan defaults to a single
`commerce.create_product` (PocketBase demo); override with NIL_SMOKE_PLAN (a JSON Wosool program) to
match your live backend's verbs. Exit code 0 = the automation ran and reached a terminal state.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("NIL_REGISTRY_URL", "").rstrip("/")
TOKEN = os.environ.get("NIL_REGISTRY_TOKEN", "")
WORKSPACE = os.environ.get("NIL_SMOKE_WORKSPACE", "ws_demo")
AUTOMATION_ID = os.environ.get("NIL_SMOKE_ID", "smoke-product")

_DEFAULT_PLAN = {
    "wosool": "0.1", "workspace": WORKSPACE, "entry": "step_1",
    "pipeline": [{"id": "step_1", "type": "action", "skill": "commerce",
                  "verb": "commerce.create_product", "args": {"name": "Smoke Test Product"}}],
}
PLAN = json.loads(os.environ["NIL_SMOKE_PLAN"]) if os.environ.get("NIL_SMOKE_PLAN") else _DEFAULT_PLAN
NAME = {"ar": "اختبار دخان", "en": "Smoke test"}


def _call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)  # noqa: S310
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": str(exc)}


def _step(label: str, status: int, payload: dict, *, ok_when=lambda s, p: s == 200) -> dict:
    ok = ok_when(status, payload)
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}: HTTP {status} {json.dumps(payload, ensure_ascii=False)[:240]}")
    if not ok:
        print(f"\nFAILED at: {label}")
        sys.exit(1)
    return payload


def main() -> None:
    if not BASE:
        print("set NIL_REGISTRY_URL (and NIL_REGISTRY_TOKEN) to the control plane", file=sys.stderr)
        sys.exit(2)
    print(f"Live smoke test → {BASE}  workspace={WORKSPACE}  id={AUTOMATION_ID}\n")

    print("1. draft (validates against the LIVE adapter skeleton)")
    s, p = _call("POST", "/automations/draft",
                 {"automation_id": AUTOMATION_ID, "name": NAME, "plan": PLAN, "trigger": {"type": "manual"}})
    _step("draft", s, p, ok_when=lambda st, pl: st == 200 and pl.get("ok") is True)

    print("2. register (lands pending_approval in the SSOT)")
    s, p = _call("POST", "/automations/register",
                 {"automation_id": AUTOMATION_ID, "name": NAME, "plan": PLAN, "trigger": {"type": "manual"}})
    d = _step("register", s, p, ok_when=lambda st, pl: st == 200 and pl.get("ok"))["definition"]
    version = d["version"]

    print("3. approve (arm it)")
    s, p = _call("POST", f"/automations/{WORKSPACE}/{AUTOMATION_ID}/{version}/state",
                 {"state": "active", "approved_by": "smoke"})
    _step("approve", s, p)

    print("4. run (the REAL execution path: grant minting + LocalExecutor against the live adapter)")
    s, p = _call("POST", f"/automations/{WORKSPACE}/{AUTOMATION_ID}/run",
                 {"idempotency_key": f"smoke-{int(time.time())}"})
    run = _step("run", s, p, ok_when=lambda st, pl: st == 200 and pl.get("ok"))["run"]
    print(f"\n✅ run state: {run.get('state')}  (run_id={run.get('run_id')})")
    if run.get("state") != "completed":
        print("   note: run did not 'complete' — inspect the trace; the live execution path was reached.")


if __name__ == "__main__":
    main()

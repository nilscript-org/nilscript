"""Use the NIL kernel THROUGH an agent.

This is the canonical pattern nilscript exists for: an agent never touches the backend
directly. It speaks NIL *sentences* to a conformant shim via the kernel SDK (`NilClient`),
and every write goes through the safe loop:

    PROPOSE  -> shim answers a PROPOSAL (dry-run: no write, a human-readable preview + tier)
    (confirm) -> a human/policy gate approves based on the preview and reversibility tier
    COMMIT   -> shim executes exactly once (idempotency key makes retries replay-safe)
    STATUS   -> confirm the executed state

The kernel's verb *profiles* (JSON Schemas under nilscript.sdk.spec) ARE the agent's tool
catalog — the same schemas you'd hand an LLM as function-calling tool definitions.

Run (against the bearer shim already on :8099):
    NIL_BASE_URL=http://127.0.0.1:8099 NIL_GRANT_SECRET=secret123 \
        python agent_demo.py "add a product called Aurora Lamp for 49.90"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from importlib import resources

from nilscript.sdk import GrantRef, NilClient, NilTransport
from nilscript.sdk.breaker import CircuitBreaker
from nilscript.sdk.idempotency import nil_uuid

# --- 1. The agent's tool catalog = the kernel's verb profiles -------------------------------
# These JSON Schemas are exactly what you'd pass to an LLM as tool/function definitions.

def load_verb_tools() -> dict[str, dict]:
    tools: dict[str, dict] = {}
    spec = resources.files("nilscript.sdk").joinpath("spec/0.1/profiles")
    for profile_dir in spec.iterdir():
        if not profile_dir.is_dir():
            continue
        family = profile_dir.name.replace("-v1", "")
        for f in profile_dir.iterdir():
            if f.name.endswith(".json") and ".response" not in f.name:
                schema = json.loads(f.read_text())
                verb = f"{family}.{f.name[:-5]}"
                tools[verb] = schema
    return tools


# --- 2. The "agent brain": goal -> (verb, args) ---------------------------------------------
# Rule-based here so the demo runs with no API key. In production this is one LLM call:
# pass the tool catalog from load_verb_tools() as tool definitions and let the model pick
# the verb + fill args; the model's tool_call output drops straight into the loop below.

def agent_decide(goal: str, tools: dict[str, dict]) -> tuple[str, dict]:
    g = goal.lower()
    if "product" in g and "delete" not in g:
        # crude arg extraction stands in for the LLM filling the schema's required fields
        name = goal.split("called", 1)[-1].split(" for ")[0].strip() or "Untitled product"
        args: dict = {"name": name}
        for tok in g.replace(",", " ").split():
            try:
                args["price"] = float(tok)
                break
            except ValueError:
                continue
        return "commerce.create_product", args
    raise SystemExit(f"agent: no verb matched goal {goal!r}. Known verbs: {sorted(tools)}")


# --- 3. The confirmation gate: the heart of "safely, with confirmation" ---------------------

def human_confirms(proposal, *, auto_ok_tiers={"LOW", "MEDIUM"}) -> bool:
    print(f"\n  PROPOSAL  verb={proposal.verb}  tier={proposal.tier}  id={proposal.id}")
    if proposal.preview:
        for k, v in proposal.preview.items():
            print(f"    preview.{k}: {v}")
    print(f"    expires_at: {proposal.expires_at}")
    # Policy gate. NIL_AUTOAPPROVE=1 lets an unattended worker proceed (demo/CI). Otherwise:
    # auto-approve low-effort-to-reverse tiers; HIGH/unknown always needs a human yes.
    if os.environ.get("NIL_AUTOAPPROVE") == "1":
        print("  -> approved (NIL_AUTOAPPROVE=1)")
        return True
    if proposal.tier in auto_ok_tiers:
        print(f"  -> auto-approved (tier {proposal.tier}: cheap to reverse)")
        return True
    if not sys.stdin.isatty():
        print(f"  -> NOT auto-approving tier {proposal.tier} in non-interactive mode; skipping commit")
        return False
    return input("  approve commit? [y/N] ").strip().lower() == "y"


async def main() -> None:
    goal = " ".join(sys.argv[1:]) or "add a product called Aurora Lamp for 49.90"
    tools = load_verb_tools()
    print(f"agent goal: {goal!r}")
    print(f"kernel exposes {len(tools)} verbs as tools; e.g. {', '.join(sorted(tools)[:4])} ...")

    verb, args = agent_decide(goal, tools)
    print(f"\nagent picked: {verb}  args={args}")

    # --- wire the SDK client to the shim ---------------------------------------------------
    base_url = os.environ.get("NIL_BASE_URL", "http://127.0.0.1:8099")
    secret = os.environ.get("NIL_GRANT_SECRET", "secret123")  # == the shim's bearer
    grant = GrantRef.from_secret(
        grant_id=os.environ.get("NIL_GRANT_ID", "demo-grant"),
        workspace=os.environ.get("NIL_WORKSPACE", "demo-ws"),
        secret=secret,
        scopes=frozenset({"commerce.*", "services.*"}),
    )
    transport = NilTransport(base_url=base_url, bearer_secret=secret, breaker=CircuitBreaker())
    client = NilClient(transport=transport, grant=grant)

    session_id = "agent-session-1"
    ts = datetime.now(UTC)

    try:
        # PROPOSE — no side effect; the shim answers a dry-run PROPOSAL ----------------------
        proposal = await client.propose(verb, args, session_id=session_id, request_timestamp=ts)
        if proposal.outcome == "refusal":
            print(f"\n  REFUSED: {proposal.code} - {proposal.message} (field={proposal.field})")
            return

        # CONFIRM ---------------------------------------------------------------------------
        if not human_confirms(proposal):
            print("\nnot committed. (this is the safety win: nothing was written)")
            return

        # COMMIT — exactly-once; key is minted once and reused on any retry ------------------
        idem = nil_uuid(session_id, ts.isoformat(), 0)
        outcome = await client.commit(proposal.id, idempotency_key=idem)
        state = getattr(outcome, "state", None)
        print(f"\n  COMMITTED  state={state}  replayed={getattr(outcome, 'replayed', None)}")

        # STATUS — independent confirmation -------------------------------------------------
        status = await client.status(proposal.id)
        print(f"  STATUS     state={status.state}")
        print("\nThe agent acted on a real system through the kernel — proposed, was confirmed, committed.")
    finally:
        await transport.aclose()


if __name__ == "__main__":
    asyncio.run(main())

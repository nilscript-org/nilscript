# nilscript Playground (reference demo)

The reference client for the NIL kernel: a chat UI that drives a **live NIL shim** through the
SDK, with an LLM planner (any OpenAI-compatible provider via LiteLLM). It's the canonical
"what NIL feels like" surface — propose → approve → commit → rollback, with a live SEQRD-PC trace.

> This is the kernel's reference client, so it lives here. It discovers everything a backend
> exposes via `/describe`, so it stays adapter-agnostic.

## Run it (one command)

```bash
pip install -e ".[demo]"                              # from the kernel root: FastAPI, uvicorn, litellm, the SDK
pip install -e ../adapters/pocketbase-nil-adapter     # the reference adapter (both shims import it; not on PyPI)
nilscript demo                                        # → http://127.0.0.1:8770   (equivalently: python demo/demo_ui.py)
```

Then in the UI: **add your LLM key** (Provider panel) → pick a backend (in-memory sandbox by
default) → **talk to your store**. Every write is previewed, approved, and one-click reversible.

## What each file is

| File | Role | Port |
|------|------|------|
| `demo_ui.py` | The Playground: FastAPI chat UI + LLM planner; boots the shim(s) and serves the UI. | UI `8770` (`UI_PORT`) |
| `run_auth_shim.py` | Boots the **in-memory** PocketBase NIL shim (`FakeSystem`) with bearer auth — the safe sandbox. | `8099` |
| `run_live_shim.py` | Boots the shim backed by a **live PocketBase** instance; writes land in real collections. | `8100` |
| `prove_live_link.py` | Smoke-proves the live PocketBase link (auth + a round-trip) outside the UI. | — |
| `agent_demo.py` | Headless agent demo — drives the SDK end-to-end with no UI, for scripting/CI. | — |

## Backends

- **in-memory (FakeSystem)** — default; nothing is persisted, nothing real is touched. Best for
  trying the flow.
- **live PocketBase** — set `PB_URL` / `PB_EMAIL` / `PB_PASSWORD` (or edit them in the Backend
  panel). Writes are real and reversible from History.

## Configuration (env)

| Var | Default | Meaning |
|-----|---------|---------|
| `UI_PORT` | `8770` | Playground UI port. |
| `UI_STATE` | `./.nil-ui-state.json` | Where provider + backend settings persist. **Disabled per-session in the hosted public instance** (see the landing's `/playground` hardening). |
| `NILSCRIPT_DEMO_DIR` | _(auto)_ | Override the demo directory `nilscript demo` launches. |
| `PB_URL` / `PB_EMAIL` / `PB_PASSWORD` | demo defaults | Live PocketBase target for `run_live_shim.py`. |
| `NIL_BEARER` | `secret123` | Bearer the launcher's shims expect. |

## Notes

- The LLM key is only ever used to plan; it never gains write power — NIL does. The agent can
  only touch verbs your backend's `/describe` actually exposes; unknown actions are **refused,
  not faked**.
- Conforms to **nilscript ≥ 0.3.0** (describe, `resource.*`, rollback honesty).

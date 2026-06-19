# Connect an agent to NILScript over MCP

`nilscript mcp` is one generic MCP server: any MCP-compatible agent (Claude Desktop, Claude.ai,
Cursor, …) connects once and drives **any** mounted NIL adapter through governed
propose→approve→commit→rollback. The agent can only *propose*; nothing writes without an approved
`nil_commit`; and it can only name verbs the backend actually exposes. The `using-nilscript` skill
travels with the server (an MCP resource + prompt), so the agent learns the discipline on connect.

Two transports: **stdio** (local IDE clients) and **streamable-HTTP** (remote, e.g. nilscript.org).

---

## A. Local — Claude Desktop / Cursor (stdio), 3 steps

```bash
pip install "nilscript[mcp]"
# you need a running NIL adapter (a shim). The demo ships one:
pip install "nilscript[demo]" && nilscript demo     # boots a FakeSystem shim on :8099
```

Add to `claude_desktop_config.json` (Claude → Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "nilscript": {
      "command": "nilscript",
      "args": ["mcp", "--adapter-url", "http://127.0.0.1:8099"],
      "env": { "NIL_GRANT_SECRET": "secret123" }
    }
  }
}
```

Restart Claude. You'll see the `nil_*` tools (and `propose_<verb>` per exposed verb). Ask it to load
the **using_nilscript** prompt first. Done — it now acts on a real backend, safely.

> Print this recipe for any adapter (with a live handshake):
> `nilscript mcp-info --adapter-url http://127.0.0.1:8099`

---

## B. Remote — a hosted connector (streamable-HTTP), e.g. nilscript.org

Run the server over HTTP, pointed at your adapter:

```bash
NIL_ADAPTER_URL=https://your-adapter NIL_GRANT_SECRET=… \
  uvicorn nilscript.mcp.app:app --host 0.0.0.0 --port 8765
# serves the MCP endpoint at  http://<host>:8765/mcp
```

or, equivalently, the CLI:

```bash
nilscript mcp --adapter-url https://your-adapter \
  --grant-secret-env NIL_GRANT_SECRET --transport streamable-http --host 0.0.0.0 --port 8765
```

Put it behind TLS at a stable URL (e.g. `https://nilscript.org/mcp`) and add it in the client as a
**Custom Connector / remote MCP server**. Container image: [`deploy/Dockerfile.mcp`](../deploy/Dockerfile.mcp).

```bash
docker build -f deploy/Dockerfile.mcp -t nilscript-mcp .
docker run -e NIL_ADAPTER_URL=https://your-adapter -e NIL_GRANT_SECRET=… -p 8765:8765 nilscript-mcp
```

---

## The tools the agent gets

| Tool | What it does | Writes? |
| --- | --- | --- |
| `nil_describe` | the backend skeleton (verbs + targets) | no |
| `nil_propose(verb, args)` / `propose_<verb>(args)` | preview an intent + reversibility tier | **no** |
| `nil_commit(proposal_id)` | execute an approved proposal (idempotent) | **yes** |
| `nil_query(verb, args)` | read live truth | no |
| `nil_status(proposal_id)` | proposal status + compensation handle | no |
| `nil_rollback(token, reason)` | preview a governed reversal | no |

Plus the **`using_nilscript`** prompt and the **`nil://skill/using-nilscript`** + **`nil://skeleton`**
resources.

---

## Gate policy (`--gate`)

| Mode | Behaviour |
| --- | --- |
| `two-step` (default) | propose previews; the agent (or the human in the client UI) must call `nil_commit`. |
| `human` | `nil_commit` of a HIGH/CRITICAL tier is held for an out-of-band owner approval. |
| `auto` | commit proceeds for in-scope proposals (trusted automation only). |

## Security

- The server holds the bearer secret (`--grant-secret-env` / `NIL_GRANT_SECRET`); the agent never
  sees backend credentials.
- The gate is structural: even a hijacked/poisoned agent cannot write without an approved `nil_commit`,
  and cannot name a verb the backend doesn't expose.
- For remote deployments, terminate TLS at your edge and scope the grant (`NIL_GRANT_SCOPES`).

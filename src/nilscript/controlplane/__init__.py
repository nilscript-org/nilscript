"""NIL control plane — central audit + (later) human-approval over every agent action.

`store.py` is a SQLite-backed event store; `app.py` is the FastAPI ingest/query/UI surface. Fed by the
adapters' HttpEventEmitter (NIL_EVENTS_WEBHOOK), so MCP / playground / SDK actions land in one pane.
"""

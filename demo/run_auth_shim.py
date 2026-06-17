"""Boot the PocketBase NIL shim with bearer auth enabled, for testing the
authenticated wiring against the kernel's conformance harness.

Usage: NIL_BEARER=secret123 python run_auth_shim.py  (defaults to 'secret123')
"""

from __future__ import annotations

import os

from pocketbase_nil_adapter.edge import CapturingEmitter, create_app
from pocketbase_nil_adapter.system import FakeSystem

BEARER = os.environ.get("NIL_BEARER", "secret123")


def build_auth_app():
    return create_app(FakeSystem(), CapturingEmitter(), bearer=BEARER)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_auth_app(), host="127.0.0.1", port=8099)

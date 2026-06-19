"""End-to-end over the REMOTE transport: an MCP client drives the streamable-HTTP server.

    MCP client ──HTTP /mcp──▶ uvicorn nilscript.mcp.app:app ──NIL──▶ FakeSystem shim

Proves the production path (the one deployed at nilscript.org/mcp) works: boots the vendored shim,
boots the real ASGI app against it, then connects over HTTP and drives describe→propose→commit,
plus the /healthz probe. Skips cleanly without the [mcp]/[demo] extras.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="needs the [mcp] extra")

import nilscript  # noqa: E402

DEMO_DIR = Path(nilscript.__file__).parent / "demo"
BEARER = "secret123"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"nothing on :{port} within {timeout}s")


_SHIM = (
    "import sys, uvicorn;"
    "sys.path.insert(0, {demo!r});"
    "from pocketbase_nil_adapter.edge import create_app, CapturingEmitter;"
    "from pocketbase_nil_adapter.system import FakeSystem;"
    "uvicorn.run(create_app(FakeSystem(), CapturingEmitter(), bearer={bearer!r}),"
    " host='127.0.0.1', port={port}, log_level='warning')"
)


def _payload(call_result) -> dict:
    return json.loads(call_result.content[0].text)


async def test_streamable_http_drives_live_shim() -> None:
    if not (DEMO_DIR / "pocketbase_nil_adapter").is_dir():
        pytest.skip("vendored demo adapter not present")
    try:
        import httpx
        import uvicorn  # noqa: F401
    except ModuleNotFoundError:
        pytest.skip("needs uvicorn + httpx (the [demo] extra)")

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    shim_port, http_port = _free_port(), _free_port()
    shim = subprocess.Popen(
        [sys.executable, "-c", _SHIM.format(demo=str(DEMO_DIR), bearer=BEARER, port=shim_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    server = None
    try:
        _wait_port(shim_port)
        env = {
            **os.environ,
            "NIL_ADAPTER_URL": f"http://127.0.0.1:{shim_port}",
            "NIL_GRANT_SECRET": BEARER,
        }
        server = subprocess.Popen(
            [sys.executable, "-c",
             f"import uvicorn; uvicorn.run('nilscript.mcp.app:app', host='127.0.0.1', "
             f"port={http_port}, log_level='warning')"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_port(http_port)

        # health probe (the LB's readiness check)
        for _ in range(40):
            try:
                r = httpx.get(f"http://127.0.0.1:{http_port}/healthz", timeout=1.0)
                if r.status_code == 200:
                    assert r.json()["status"] == "ok"
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("/healthz never returned 200")

        url = f"http://127.0.0.1:{http_port}/mcp"
        async with streamablehttp_client(url) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                assert {"nil_describe", "nil_propose", "nil_commit"} <= names
                assert "propose_commerce_create_product" in names  # skeleton-driven, over HTTP

                skeleton = _payload(await session.call_tool("nil_describe", {}))
                assert skeleton["conformant"] is True

                preview = _payload(await session.call_tool(
                    "nil_propose",
                    {"verb": "commerce.create_product", "args": {"name": "Aurora", "price": 49.9}},
                ))
                assert preview["outcome"] == "proposal"

                committed = _payload(
                    await session.call_tool("nil_commit", {"proposal_id": preview["id"]})
                )
                assert committed["committed"] is True and committed["state"] == "executed"
    finally:
        for proc in (server, shim):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

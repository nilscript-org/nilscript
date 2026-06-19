"""Multi-tenant wiring: per-connection backend binding via the tools provider."""

import pytest

pytest.importorskip("mcp", reason="needs the [mcp] extra")

from nilscript.mcp.server import (  # noqa: E402
    SingletonToolsProvider,
    TenantToolsProvider,
    build_asgi_app,
    build_tools,
)
from nilscript.mcp.tenant import ADAPTER_URL_HEADER, Tenant  # noqa: E402


class _FakeHeaders:
    def __init__(self, mapping):
        self._m = {k.lower(): v for k, v in mapping.items()}

    def get(self, name):
        return self._m.get(name.lower())


class _FakeCtx:
    def __init__(self, adapter_url, client_id):
        self.client_id = client_id
        req = type("Req", (), {"headers": _FakeHeaders({ADAPTER_URL_HEADER: adapter_url})})()
        self.request_context = type("RC", (), {"request": req})()


def _base_url(tools):
    return str(tools._transport._client.base_url)


def test_singleton_provider_returns_same_backend_for_every_connection() -> None:
    tools = build_tools(adapter_url="https://only-backend", bearer="")
    prov = SingletonToolsProvider(tools)
    assert prov.get(_FakeCtx("https://ignored", "a")) is tools
    assert prov.get(_FakeCtx("https://ignored", "b")) is tools


def test_tenant_provider_binds_each_connection_to_its_own_backend() -> None:
    prov = TenantToolsProvider(default=None)
    acme = prov.get(_FakeCtx("https://acme-adapter", client_id="acme"))
    globex = prov.get(_FakeCtx("https://globex-adapter", client_id="globex"))

    assert _base_url(acme).startswith("https://acme-adapter")
    assert _base_url(globex).startswith("https://globex-adapter")
    assert acme is not globex


def test_tenant_provider_caches_per_connection() -> None:
    prov = TenantToolsProvider(default=None)
    first = prov.get(_FakeCtx("https://acme-adapter", client_id="acme"))
    again = prov.get(_FakeCtx("https://acme-adapter", client_id="acme"))
    assert first is again


def test_tenant_provider_falls_back_to_default_when_no_header() -> None:
    class _NoHeaderCtx:
        client_id = "x"
        request_context = None

    default = Tenant(adapter_url="https://env-default", bearer="s")
    prov = TenantToolsProvider(default=default)
    assert _base_url(prov.get(_NoHeaderCtx())).startswith("https://env-default")


def test_build_asgi_app_multi_tenant_is_callable() -> None:
    # multi-tenant skips build-time discovery (no per-tenant skeleton) and must not raise.
    app = build_asgi_app(adapter_url="https://env-default", bearer="", multi_tenant=True)
    assert callable(app)

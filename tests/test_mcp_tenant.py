"""Per-connection tenant resolution (pure, no MCP SDK needed)."""

import pytest

from nilscript.mcp.tenant import (
    ADAPTER_BEARER_HEADER,
    ADAPTER_URL_HEADER,
    GRANT_ID_HEADER,
    SCOPES_HEADER,
    Tenant,
    TenantError,
    resolve_tenant,
)


class _FakeHeaders:
    """Mimics Starlette's case-insensitive Headers.get over lowercased keys."""

    def __init__(self, mapping: dict[str, str]):
        self._m = {k.lower(): v for k, v in mapping.items()}

    def get(self, name: str):
        return self._m.get(name.lower())


class _FakeCtx:
    def __init__(self, headers: dict[str, str] | None = None, client_id: str = "c1"):
        self.client_id = client_id
        if headers is None:
            self.request_context = None
        else:
            req = type("Req", (), {"headers": _FakeHeaders(headers)})()
            self.request_context = type("RC", (), {"request": req})()


DEFAULT = Tenant(adapter_url="https://default-adapter", bearer="envsecret")


def test_single_tenant_always_returns_default() -> None:
    ctx = _FakeCtx({ADAPTER_URL_HEADER: "https://attacker"})  # header ignored in single-tenant
    assert resolve_tenant(ctx, default=DEFAULT, multi_tenant=False) is DEFAULT


def test_single_tenant_without_default_raises() -> None:
    with pytest.raises(TenantError):
        resolve_tenant(_FakeCtx(), default=None, multi_tenant=False)


def test_multi_tenant_binds_backend_from_headers() -> None:
    ctx = _FakeCtx(
        {
            ADAPTER_URL_HEADER: "https://acme-adapter.example",
            ADAPTER_BEARER_HEADER: "acme-bearer",
            GRANT_ID_HEADER: "acme",
            SCOPES_HEADER: "commerce.*, services.create_client",
        }
    )
    t = resolve_tenant(ctx, default=DEFAULT, multi_tenant=True)
    assert t.adapter_url == "https://acme-adapter.example"
    assert t.bearer == "acme-bearer"
    assert t.grant_id == "acme"
    assert t.scopes == frozenset({"commerce.*", "services.create_client"})


def test_multi_tenant_missing_header_falls_back_to_default() -> None:
    assert resolve_tenant(_FakeCtx({}), default=DEFAULT, multi_tenant=True) is DEFAULT


def test_multi_tenant_missing_header_no_default_raises() -> None:
    with pytest.raises(TenantError):
        resolve_tenant(_FakeCtx({}), default=None, multi_tenant=True)


def test_multi_tenant_rejects_insecure_url_by_default() -> None:
    ctx = _FakeCtx({ADAPTER_URL_HEADER: "http://insecure-adapter"})
    with pytest.raises(TenantError):
        resolve_tenant(ctx, default=DEFAULT, multi_tenant=True)


def test_multi_tenant_allows_insecure_when_opted_in() -> None:
    ctx = _FakeCtx({ADAPTER_URL_HEADER: "http://localhost:8100"})
    t = resolve_tenant(ctx, default=DEFAULT, multi_tenant=True, allow_insecure=True)
    assert t.adapter_url == "http://localhost:8100"


def test_two_tenants_are_distinct() -> None:
    a = resolve_tenant(_FakeCtx({ADAPTER_URL_HEADER: "https://a"}), multi_tenant=True)
    b = resolve_tenant(_FakeCtx({ADAPTER_URL_HEADER: "https://b"}), multi_tenant=True)
    assert a.key() != b.key()


# ── active-adapter registry resolution (header-less connections) ────────────────────────────────

from nilscript.mcp.tenant import WORKSPACE_HEADER  # noqa: E402


def _registry(mapping: dict[str, Tenant]):
    """A pure stand-in for the CP active-adapter lookup: workspace → Tenant | None."""
    return lambda ws: mapping.get(ws)


def test_no_header_resolves_active_adapter_from_registry() -> None:
    reg = _registry({"acme": Tenant(adapter_url="https://acme-odoo", bearer="b", workspace="acme")})
    ctx = _FakeCtx({WORKSPACE_HEADER: "acme"})  # no adapter-url header
    t = resolve_tenant(ctx, default=DEFAULT, multi_tenant=True, registry=reg)
    assert t.adapter_url == "https://acme-odoo" and t.workspace == "acme"


def test_explicit_adapter_header_wins_over_registry() -> None:
    reg = _registry({"acme": Tenant(adapter_url="https://acme-odoo", workspace="acme")})
    ctx = _FakeCtx({ADAPTER_URL_HEADER: "https://byo-adapter", WORKSPACE_HEADER: "acme"})
    t = resolve_tenant(ctx, default=DEFAULT, multi_tenant=True, registry=reg)
    assert t.adapter_url == "https://byo-adapter"  # per-connection BYO beats the registry default


def test_registry_miss_falls_back_to_default() -> None:
    reg = _registry({})  # workspace has no active adapter
    ctx = _FakeCtx({WORKSPACE_HEADER: "acme"})
    assert resolve_tenant(ctx, default=DEFAULT, multi_tenant=True, registry=reg) is DEFAULT


def test_header_less_connection_uses_default_workspace_for_registry() -> None:
    # Single-owner deployment: agents send no workspace header; the server's default workspace is
    # used to look up the active adapter.
    reg = _registry({"owner": Tenant(adapter_url="https://owner-active", workspace="owner")})
    default = Tenant(adapter_url="https://env-default", workspace="owner")
    ctx = _FakeCtx({})  # no headers at all
    t = resolve_tenant(ctx, default=default, multi_tenant=True, registry=reg)
    assert t.adapter_url == "https://owner-active"


def test_registry_not_consulted_without_workspace() -> None:
    calls: list[str] = []

    def reg(ws):
        calls.append(ws)
        return Tenant(adapter_url="https://x")

    ctx = _FakeCtx({})  # no workspace, no adapter url
    assert resolve_tenant(ctx, default=DEFAULT, multi_tenant=True, registry=reg) is DEFAULT
    assert calls == []  # no workspace → registry never queried


# ── SaaS identity spine: tenant from authenticated claim, header cannot override, default-deny ──────
_ADAPTERS = {
    "ws_a": Tenant(adapter_url="https://a-adapter", bearer="a-sec", workspace="ws_a"),
    "ws_b": Tenant(adapter_url="https://b-adapter", bearer="b-sec", workspace="ws_b"),
}


def _saas_reg(ws: str):
    return _ADAPTERS.get(ws)


def _claim(ws: str | None):
    return lambda ctx: ws


def test_saas_routes_to_the_authenticated_tenants_adapter() -> None:
    t = resolve_tenant(_FakeCtx({}), saas=True, claim_resolver=_claim("ws_a"), registry=_saas_reg)
    assert t.adapter_url == "https://a-adapter" and t.workspace == "ws_a"


def test_saas_two_tenants_are_isolated() -> None:
    a = resolve_tenant(_FakeCtx({}), saas=True, claim_resolver=_claim("ws_a"), registry=_saas_reg)
    b = resolve_tenant(_FakeCtx({}), saas=True, claim_resolver=_claim("ws_b"), registry=_saas_reg)
    assert a.adapter_url != b.adapter_url  # A can never reach B's backend


def test_saas_header_cannot_override_authenticated_tenant() -> None:
    # token says ws_a; attacker sends X-NIL-Workspace: ws_b → refused, never routed to B
    with pytest.raises(TenantError):
        resolve_tenant(_FakeCtx({"x-nil-workspace": "ws_b"}), saas=True,
                       claim_resolver=_claim("ws_a"), registry=_saas_reg)


def test_saas_byo_adapter_url_is_rejected() -> None:
    with pytest.raises(TenantError):
        resolve_tenant(_FakeCtx({ADAPTER_URL_HEADER: "https://attacker"}), saas=True,
                       claim_resolver=_claim("ws_a"), registry=_saas_reg)


def test_saas_missing_claim_is_default_deny() -> None:
    with pytest.raises(TenantError):
        resolve_tenant(_FakeCtx({}), saas=True, claim_resolver=_claim(None), registry=_saas_reg)


def test_saas_unknown_workspace_has_no_adapter() -> None:
    with pytest.raises(TenantError):
        resolve_tenant(_FakeCtx({}), saas=True, claim_resolver=_claim("ws_ghost"), registry=_saas_reg)


def test_saas_matching_header_is_allowed() -> None:
    # header equal to the claim is fine (clients may echo it); only a MISMATCH is refused
    t = resolve_tenant(_FakeCtx({"x-nil-workspace": "ws_a"}), saas=True,
                       claim_resolver=_claim("ws_a"), registry=_saas_reg)
    assert t.workspace == "ws_a"

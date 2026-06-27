"""SaaS tenant management: encrypted secret vault, JWT claim verifier, per-tenant quotas/limits."""

from __future__ import annotations

import jwt
import pytest

from nilscript.governance_quota import TenantQuota, TenantRateLimiter
from nilscript.mcp.auth import make_jwt_claim_resolver
from nilscript.secrets.vault import SecretVault, VaultError


class _FakeHeaders:
    def __init__(self, m): self._m = {k.lower(): v for k, v in m.items()}
    def get(self, n): return self._m.get(n.lower())


class _FakeCtx:
    def __init__(self, headers=None):
        req = type("Req", (), {"headers": _FakeHeaders(headers or {})})()
        self.request_context = type("RC", (), {"request": req})()


# ── secret vault ───────────────────────────────────────────────────────────────────────────────
def _vault(store=None):
    return SecretVault(SecretVault.generate_key(), store)


def test_vault_put_get_roundtrip() -> None:
    v = _vault()
    v.put("ws_a", {"adapter_bearer": "sek", "llm_api_key": "sk-123"})
    assert v.get("ws_a") == {"adapter_bearer": "sek", "llm_api_key": "sk-123"}
    assert v.get_secret("ws_a", "llm_api_key") == "sk-123"


def test_vault_is_encrypted_at_rest() -> None:
    store: dict[str, bytes] = {}
    v = SecretVault(SecretVault.generate_key(), store)
    v.put("ws_a", {"llm_api_key": "sk-SECRET-VALUE"})
    raw = store["ws_a"]
    assert b"sk-SECRET-VALUE" not in raw and b"llm_api_key" not in raw  # ciphertext, not plaintext


def test_vault_tenants_are_isolated() -> None:
    v = _vault()
    v.put("ws_a", {"k": "a"}); v.put("ws_b", {"k": "b"})
    assert v.get("ws_a") == {"k": "a"} and v.get("ws_b") == {"k": "b"}
    assert v.get("ws_other") is None


def test_vault_wrong_key_cannot_decrypt() -> None:
    store: dict[str, bytes] = {}
    SecretVault(SecretVault.generate_key(), store).put("ws_a", {"k": "v"})
    other = SecretVault(SecretVault.generate_key(), store)  # different master key, same store
    with pytest.raises(VaultError):
        other.get("ws_a")


def test_vault_delete_offboards() -> None:
    v = _vault(); v.put("ws_a", {"k": "v"})
    v.delete("ws_a")
    assert v.get("ws_a") is None and not v.has("ws_a")


# ── JWT claim verifier (production claim_resolver) ───────────────────────────────────────────────
_SECRET = "test-hs-secret"


def _token(claims, secret=_SECRET):
    return jwt.encode({"exp": 9999999999, **claims}, secret, algorithm="HS256")


def _ctx_with(token):
    return _FakeCtx({"authorization": f"Bearer {token}"})


def test_jwt_resolver_reads_verified_workspace_claim() -> None:
    r = make_jwt_claim_resolver(hs_secret=_SECRET)
    assert r(_ctx_with(_token({"workspace": "ws_a"}))) == "ws_a"


def test_jwt_resolver_rejects_forged_signature() -> None:
    r = make_jwt_claim_resolver(hs_secret=_SECRET)
    assert r(_ctx_with(_token({"workspace": "ws_a"}, secret="attacker-secret"))) is None


def test_jwt_resolver_rejects_expired_token() -> None:
    r = make_jwt_claim_resolver(hs_secret=_SECRET)
    expired = jwt.encode({"exp": 1, "workspace": "ws_a"}, _SECRET, algorithm="HS256")
    assert r(_ctx_with(expired)) is None


def test_jwt_resolver_no_token_is_none() -> None:
    r = make_jwt_claim_resolver(hs_secret=_SECRET)
    assert r(_FakeCtx({})) is None


def test_jwt_resolver_no_workspace_claim_is_none() -> None:
    r = make_jwt_claim_resolver(hs_secret=_SECRET)
    assert r(_ctx_with(_token({"sub": "u1"}))) is None


# ── per-tenant rate limit + quota ────────────────────────────────────────────────────────────────
def test_rate_limiter_throttles_a_tenant_without_starving_others() -> None:
    clock = {"t": 0.0}
    rl = TenantRateLimiter(rate=1.0, burst=3.0, now=lambda: clock["t"])
    assert [rl.allow("ws_a") for _ in range(3)] == [True, True, True]
    assert rl.allow("ws_a") is False          # A spent its burst
    assert rl.allow("ws_b") is True           # B is unaffected (isolation)
    clock["t"] = 2.0                           # 2s → +2 tokens
    assert rl.allow("ws_a") is True


def test_quota_caps_volume_per_tenant() -> None:
    q = TenantQuota(limits={"export": 2}, period=lambda: "2026-06-27")
    assert q.charge("ws_a", "export") and q.charge("ws_a", "export")
    assert q.charge("ws_a", "export") is False          # A hit its export cap
    assert q.charge("ws_b", "export") is True           # B still has quota
    assert q.charge("ws_a", "write") is True            # unmetered kind passes
    assert q.remaining("ws_b", "export") == 1


# ── JWKS resolver (production keycloak path) ──────────────────────────────────────────────────────
def test_jwks_resolver_verifies_with_rotating_keys() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from nilscript.mcp.auth import make_jwks_claim_resolver

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()

    class _FakeJWK:
        key = pub

    class _FakeClient:
        def get_signing_key_from_jwt(self, token): return _FakeJWK()

    token = jwt.encode({"exp": 9999999999, "workspace": "ws_a"}, priv, algorithm="RS256")
    r = make_jwks_claim_resolver("https://kc/certs", jwk_client=_FakeClient())
    assert r(_ctx_with(token)) == "ws_a"
    # a token signed by a DIFFERENT key (not in the JWKS) → verification fails → None
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"exp": 9999999999, "workspace": "ws_a"}, other, algorithm="RS256")
    assert r(_ctx_with(forged)) is None


# ── tenant-scoped durable execution (Temporal-ready) ─────────────────────────────────────────────
def test_durable_ids_and_namespace_are_tenant_isolated() -> None:
    from nilscript.durable import tenant_namespace, tenant_workflow_id

    a = tenant_workflow_id("ws_a", "bulk_delete", "job1")
    b = tenant_workflow_id("ws_b", "bulk_delete", "job1")
    assert a != b and a == tenant_workflow_id("ws_a", "bulk_delete", "job1")  # isolated + deterministic
    assert tenant_namespace("ws_a") != tenant_namespace("ws_b")


def test_durable_policy_throttles_per_tenant() -> None:
    from nilscript.durable import TenantDurablePolicy

    clock = {"t": 0.0}
    pol = TenantDurablePolicy(TenantRateLimiter(rate=1.0, burst=2.0, now=lambda: clock["t"]))
    assert pol.admit("ws_a") and pol.admit("ws_a")
    assert pol.admit("ws_a") is False     # A over budget
    assert pol.admit("ws_b") is True      # B unaffected

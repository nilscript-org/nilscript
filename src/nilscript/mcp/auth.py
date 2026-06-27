"""Production `claim_resolver` for SaaS mode — derive the tenant from a VERIFIED JWT, never a header.

`tenant.resolve_tenant(saas=True, claim_resolver=...)` takes a callable that returns the authenticated
workspace for a connection. This module builds that callable by validating the connection's bearer JWT
(signature + expiry + optional issuer/audience) and reading the `workspace` claim (configurable). A token
that is missing, expired, wrongly-signed, or carries no workspace claim resolves to None → the resolver
default-denies. Header values are never trusted for identity.

Build it from env (`NIL_JWT_PUBLIC_KEY` / `NIL_JWT_HS_SECRET`, `NIL_JWT_ISSUER`, `NIL_JWT_AUDIENCE`,
`NIL_JWT_WORKSPACE_CLAIM`) or inject keys directly (keycloak JWKS wiring layers on top of this).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import jwt

_BEARER = "authorization"


def _bearer_token(ctx: Any) -> str | None:
    rc = getattr(ctx, "request_context", None)
    req = getattr(rc, "request", None) if rc is not None else None
    headers = getattr(req, "headers", None)
    raw = headers.get(_BEARER) if headers is not None and hasattr(headers, "get") else None
    if not raw or not raw.lower().startswith("bearer "):
        return None
    return raw.split(" ", 1)[1].strip() or None


def make_jwt_claim_resolver(
    *,
    public_key: str | None = None,
    hs_secret: str | None = None,
    algorithms: list[str] | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    workspace_claim: str = "workspace",
) -> Callable[[Any], str | None]:
    """Return a `claim_resolver(ctx) -> workspace|None` that VERIFIES the bearer JWT and extracts the
    workspace claim. RS/ES keys via `public_key`; HS via `hs_secret`. Verification failure (bad sig,
    expired, wrong issuer/audience) → None (default-deny upstream), never an exception that leaks through.
    """
    key = public_key or hs_secret
    if not key:
        raise ValueError("make_jwt_claim_resolver needs public_key (RS/ES) or hs_secret (HS)")
    algs = algorithms or (["RS256"] if public_key else ["HS256"])

    def resolve(ctx: Any) -> str | None:
        token = _bearer_token(ctx)
        if not token:
            return None
        try:
            claims = jwt.decode(
                token, key, algorithms=algs,
                issuer=issuer, audience=audience,
                options={"require": ["exp"], "verify_aud": audience is not None,
                         "verify_iss": issuer is not None},
            )
        except jwt.PyJWTError:
            return None  # invalid/expired/forged → no identity → default-deny
        ws = claims.get(workspace_claim)
        return ws if isinstance(ws, str) and ws else None

    return resolve


def make_jwks_claim_resolver(
    jwks_url: str,
    *,
    algorithms: list[str] | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    workspace_claim: str = "workspace",
    jwk_client: Any = None,
) -> Callable[[Any], str | None]:
    """Resolver backed by a JWKS endpoint (keycloak `…/protocol/openid-connect/certs`) — the production
    path. PyJWKClient fetches + CACHES signing keys and selects by the token's `kid`, so key ROTATION is
    handled automatically. Same fail-closed contract: any verification failure → None. `jwk_client` is
    injectable for tests; the real one is built from `jwks_url`."""
    client = jwk_client if jwk_client is not None else jwt.PyJWKClient(jwks_url)
    algs = algorithms or ["RS256"]

    def resolve(ctx: Any) -> str | None:
        token = _bearer_token(ctx)
        if not token:
            return None
        try:
            key = client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=algs, issuer=issuer, audience=audience,
                options={"require": ["exp"], "verify_aud": audience is not None,
                         "verify_iss": issuer is not None},
            )
        except Exception:  # noqa: BLE001 — JWKS fetch / verify failure → default-deny, never leaks through
            return None
        ws = claims.get(workspace_claim)
        return ws if isinstance(ws, str) and ws else None

    return resolve


def jwt_claim_resolver_from_env() -> Callable[[Any], str | None] | None:
    """Build the resolver from env, or None if SaaS JWT auth is not configured (caller fails closed).

    Precedence: NIL_JWT_JWKS_URL (keycloak, production) > NIL_JWT_PUBLIC_KEY (static RS/ES) >
    NIL_JWT_HS_SECRET (HS, dev). Issuer/audience/workspace-claim are shared env knobs.
    """
    issuer = os.environ.get("NIL_JWT_ISSUER") or None
    audience = os.environ.get("NIL_JWT_AUDIENCE") or None
    claim = os.environ.get("NIL_JWT_WORKSPACE_CLAIM", "workspace")
    jwks = os.environ.get("NIL_JWT_JWKS_URL") or None
    if jwks:
        return make_jwks_claim_resolver(jwks, issuer=issuer, audience=audience, workspace_claim=claim)
    pub = os.environ.get("NIL_JWT_PUBLIC_KEY") or None
    hs = os.environ.get("NIL_JWT_HS_SECRET") or None
    if not pub and not hs:
        return None
    return make_jwt_claim_resolver(
        public_key=pub, hs_secret=hs, issuer=issuer, audience=audience, workspace_claim=claim,
    )

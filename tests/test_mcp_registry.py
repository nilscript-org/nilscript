"""Control-plane active-adapter lookup (the I/O callable resolve_tenant is given)."""

import io
import json
import urllib.error

from nilscript.mcp.registry import make_registry_lookup


def _fake_urlopen(payload, *, status=200, captured=None):
    """A urlopen stand-in returning `payload` as JSON; records the Request it was called with."""
    def _open(req, timeout=None):  # noqa: ANN001
        if captured is not None:
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
        return io.BytesIO(json.dumps(payload).encode())
    return _open


def test_returns_none_when_no_registry_url_configured() -> None:
    assert make_registry_lookup("", token="") is None


def test_builds_tenant_from_active_adapter(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen({"adapter": {"adapter_id": "odoo", "url": "https://odoo/nil", "bearer": "tok"}},
                      captured=captured),
    )
    lookup = make_registry_lookup("https://cp.example", token="reg-tok")
    t = lookup("acme")
    assert t is not None
    assert t.adapter_url == "https://odoo/nil" and t.bearer == "tok"
    assert t.grant_id == "odoo" and t.workspace == "acme"
    # The workspace is query-encoded and the registry token is sent as a bearer.
    assert "workspace=acme" in captured["url"] and captured["auth"] == "Bearer reg-tok"


def test_returns_none_on_http_error(monkeypatch) -> None:
    def _raise(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", _raise)
    lookup = make_registry_lookup("https://cp.example", token="reg-tok")
    assert lookup("acme") is None  # never propagates — caller falls back to default


def test_returns_none_when_no_active_adapter(monkeypatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen({"error": "no active adapter"}))
    lookup = make_registry_lookup("https://cp.example")
    assert lookup("acme") is None

"""`nilscript adapters …` registry CLI — integration against an in-process control plane."""

import io
import json
import urllib.parse

import pytest

pytest.importorskip("fastapi", reason="needs fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from nilscript.cli.adapters import _cmd_adapters, add_adapters_parser  # noqa: E402
from nilscript.controlplane.app import create_app  # noqa: E402
from nilscript.controlplane.store import EventStore  # noqa: E402


class _Resp(io.BytesIO):
    def __init__(self, status, payload):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _route_to(client: TestClient):
    """A urlopen stand-in that drives the FastAPI TestClient from the urllib Request."""
    def _open(req, timeout=None):  # noqa: ANN001
        parsed = urllib.parse.urlparse(req.full_url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        headers = {"Authorization": req.get_header("Authorization")} if req.get_header("Authorization") else {}
        if req.get_method() == "GET":
            r = client.get(path, headers=headers)
        else:
            r = client.post(path, content=req.data, headers=headers)
        return _Resp(r.status_code, r.json())
    return _open


@pytest.fixture()
def cp(monkeypatch):
    store = EventStore(":memory:")
    client = TestClient(create_app(store, secret="", registry_token="reg-tok"))
    monkeypatch.setattr("urllib.request.urlopen", _route_to(client))
    monkeypatch.setenv("NIL_REGISTRY_TOKEN", "reg-tok")
    monkeypatch.setenv("NIL_REGISTRY_URL", "https://cp.test")
    return store


def _parse(argv):
    import argparse
    parser = argparse.ArgumentParser()
    add_adapters_parser(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["adapters", *argv])


def test_register_then_activate_then_list(cp, capsys) -> None:
    assert _cmd_adapters(_parse(
        ["--workspace", "owner", "register", "odoo",
         "--url", "https://odoo/nil", "--system", "odoo_crm", "--activate"])) == 0
    # The store reflects an activated odoo adapter…
    active = cp.active_adapter("owner")
    assert active and active["adapter_id"] == "odoo"

    capsys.readouterr()  # clear
    assert _cmd_adapters(_parse(["--workspace", "owner", "list"])) == 0
    out = capsys.readouterr().out
    assert "odoo" in out and "active" in out
    assert "https://odoo/nil" in out  # url shown; bearer never is


def test_activate_unknown_reports_failure(cp, capsys) -> None:
    rc = _cmd_adapters(_parse(["--workspace", "owner", "activate", "ghost"]))
    assert rc == 1
    assert "activate failed (404)" in capsys.readouterr().err


def test_missing_cp_url_errors(monkeypatch, capsys) -> None:
    monkeypatch.delenv("NIL_REGISTRY_URL", raising=False)
    rc = _cmd_adapters(_parse(["list"]))
    assert rc == 2
    assert "NIL_REGISTRY_URL" in capsys.readouterr().err

"""Phase 3 (plan §4.4): a scaffolded shim, fed a scanned manifest, fills the hidden requirements
itself — so the translation stays standard-shaped and the agent never field-hunts.

This drives the REAL generated artifact: it scaffolds a shim, drops a manifest next to it, then runs
the generated `manifest.py` overlay in a subprocess (isolated env + import path) and asserts both a
top-level hidden requirement (`company`) and a line-level one (`income_account` inside `items[]`) are
filled from instance values — exactly the fields that cost five manual attempts in the live build.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from nilscript.cli.scaffold import scaffold_shim

_DRIVER = """
import json, os
from acme_nil_adapter.manifest import overlay_requirements

# What translate.py would emit — note: NO company, and the line has NO income_account.
native = {"customer": "C-1", "currency": "SAR", "items": [{"description": "Service", "rate": 100}]}
filled = overlay_requirements("services.create_invoice", native)
print(json.dumps(filled))
"""


def _manifest() -> dict:
    # A structural manifest (as `scan` produces) plus the integrator's own instance values in ${ENV}.
    return {
        "manifest_version": "0.1",
        "system": "erpnext",
        "nil_spec": "0.1",
        "verbs": {
            "services.create_invoice": {
                "native_target": "Sales Invoice",
                "line_container": "items",
                "hidden_requirements": [
                    {"field": "company", "kind": "required_scalar"},
                    {"field": "income_account", "kind": "required_on_line"},
                ],
                "instance_values": {
                    "company": "${ERPNEXT_COMPANY}",
                    "income_account": "${ERPNEXT_INCOME_ACCOUNT}",
                },
            }
        },
    }


def test_overlay_fills_scalar_and_line_requirements_from_env(tmp_path: Path) -> None:
    root = scaffold_shim("acme-nil-adapter", tmp_path)
    (root / "requirements-manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")

    env = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "ERPNEXT_COMPANY": "abc",
        "ERPNEXT_INCOME_ACCOUNT": "Sales - A",
    }
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER], cwd=root, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    filled = json.loads(result.stdout)

    # top-level hidden requirement filled from env — zero manual field-hunting
    assert filled["company"] == "abc"
    # line-level hidden requirement injected into the existing line
    assert filled["items"][0]["income_account"] == "Sales - A"
    # the translation's own fields are untouched
    assert filled["items"][0]["rate"] == 100
    assert filled["customer"] == "C-1"


def test_overlay_is_a_noop_without_instance_values(tmp_path: Path) -> None:
    # A purely structural manifest (no instance_values, e.g. straight from the community registry
    # before the integrator sets env) must not invent values — it overlays nothing.
    root = scaffold_shim("acme-nil-adapter", tmp_path)
    structural = _manifest()
    del structural["verbs"]["services.create_invoice"]["instance_values"]
    (root / "requirements-manifest.json").write_text(json.dumps(structural), encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER], cwd=root, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    filled = json.loads(result.stdout)
    assert "company" not in filled
    assert "income_account" not in filled["items"][0]

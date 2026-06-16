"""CLI integration tests for the new toolkit subcommands: scaffold-shim / scan / manifest.

These drive `nilscript.cli.main` the way a developer would, asserting the commands wire the cores
(scaffold generator, inference engine, manifest validator) together correctly end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

from nilscript.cli import main


def test_scaffold_shim_command_creates_a_project(tmp_path: Path, capsys) -> None:
    rc = main(["scaffold-shim", "--name", "acme-nil-adapter", "--dest", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "acme-nil-adapter" / "src" / "acme_nil_adapter" / "edge.py").exists()


def test_scan_replay_reproduces_erpnext_manifest(tmp_path: Path, capsys) -> None:
    replay = tmp_path / "samples.json"
    replay.write_text(
        json.dumps(
            {
                "system": "erpnext",
                "samples": [
                    {
                        "verb": "services.create_invoice",
                        "native_target": "Sales Invoice",
                        "errors": [
                            "Income Account None does not belong to company abc",
                            "HTTP 417 EXPECTATION FAILED",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "requirements-manifest.json"
    rc = main(["scan", "--replay", str(replay), "-o", str(out)])
    assert rc == 0

    manifest = json.loads(out.read_text(encoding="utf-8"))
    fields = {r["field"] for r in manifest["verbs"]["services.create_invoice"]["hidden_requirements"]}
    assert {"company", "income_account"} <= fields
    assert manifest["transport_quirks"][0]["quirk"] == "no_expect_100_continue"


def test_manifest_validate_passes_a_shareable_manifest(tmp_path: Path, capsys) -> None:
    good = tmp_path / "m.json"
    good.write_text(
        json.dumps(
            {
                "manifest_version": "0.1",
                "system": "erpnext",
                "nil_spec": "0.1",
                "verbs": {
                    "services.create_invoice": {
                        "hidden_requirements": [{"field": "company", "kind": "required_scalar"}]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert main(["manifest", "validate", str(good)]) == 0


def test_manifest_validate_flags_a_leak(tmp_path: Path, capsys) -> None:
    leaky = tmp_path / "m.json"
    leaky.write_text(
        json.dumps(
            {
                "manifest_version": "0.1",
                "system": "erpnext",
                "nil_spec": "0.1",
                "verbs": {
                    "services.create_invoice": {
                        "instance_values": {"company": "abc"}  # concrete value = leak
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rc = main(["manifest", "validate", str(leaky)])
    assert rc == 1
    assert "LEAK" in capsys.readouterr().err


def test_scan_without_replay_explains_itself(capsys) -> None:
    rc = main(["scan", "--url", "https://x.example", "--safe"])
    assert rc == 2
    assert "replay" in capsys.readouterr().err

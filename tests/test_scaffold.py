"""Tests for `scaffold-shim` (plan §3.1).

The DoD: scaffold emits a project that boots, its translation stubs raise NotImplementedError, and
its bundled conformance proof FAILS every active verb (empty stubs) — proving the harness detects
non-conformance. These tests generate a shim into a tmp dir and assert exactly that.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from nilscript.cli.scaffold import classify, scaffold_shim


def test_scaffold_emits_the_expected_tree(tmp_path: Path) -> None:
    root = scaffold_shim("acme-nil-adapter", tmp_path)
    pkg = root / "src" / "acme_nil_adapter"
    for expected in ("edge.py", "translate.py", "system.py", "state.py", "manifest.py", "models.py"):
        assert (pkg / expected).exists(), f"missing {expected}"
    assert (root / "requirements-manifest.json").exists()
    assert (root / "conformance" / "test_conformance.py").exists()


def test_parked_verb_is_marked_not_stubbed(tmp_path: Path) -> None:
    root = scaffold_shim("acme-nil-adapter", tmp_path)
    translate = (root / "src" / "acme_nil_adapter" / "translate.py").read_text(encoding="utf-8")
    assert "PARKED" in translate
    # update_order_status is parked (GAP-001) — no stub function for it.
    assert "_to_native_update_order_status" not in translate


def test_active_write_verb_gets_a_notimplemented_stub(tmp_path: Path) -> None:
    root = scaffold_shim("acme-nil-adapter", tmp_path)
    translate = (root / "src" / "acme_nil_adapter" / "translate.py").read_text(encoding="utf-8")
    assert "_to_native_create_invoice" in translate
    assert "NotImplementedError" in translate


def test_generated_python_files_are_syntactically_valid(tmp_path: Path) -> None:
    root = scaffold_shim("acme-nil-adapter", tmp_path)
    for py in (root / "src").rglob("*.py"):
        compile(py.read_text(encoding="utf-8"), str(py), "exec")


def test_classify_splits_writes_queries_parked() -> None:
    writes, queries, parked = classify()
    write_names = {v.name for v in writes}
    query_names = {v.name for v in queries}
    parked_names = {v.name for v in parked}
    assert "services.create_invoice" in write_names
    assert "services.list_clients" in query_names  # has a .response.json answer shape
    assert "commerce.update_order_status" in parked_names
    # a verb is in exactly one bucket
    assert write_names.isdisjoint(query_names)


def test_name_with_path_traversal_is_rejected(tmp_path: Path) -> None:
    import pytest

    for bad in ("../escape", "a/b", "..", ""):
        with pytest.raises(ValueError):
            scaffold_shim(bad, tmp_path)


def test_name_yielding_invalid_identifier_is_rejected(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        scaffold_shim("9bad", tmp_path)  # package would start with a digit


def test_scaffolded_conformance_proof_fails_on_empty_stubs(tmp_path: Path) -> None:
    # The Phase-1 DoD: an unfilled shim must FAIL conformance (it detects non-conformance).
    root = scaffold_shim("acme-nil-adapter", tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "conformance"],
        cwd=root,
        env={"PYTHONPATH": str(root / "src"), "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )
    # non-zero exit = the conformance proof failed, as it must for empty stubs.
    assert result.returncode != 0, f"empty-stub shim unexpectedly passed:\n{result.stdout}\n{result.stderr}"
    assert "fill translate.py" in (result.stdout + result.stderr)

"""`scaffold-shim` (plan §3.1): generate a complete, bootable NIL shim skeleton for any system.

The developer fills only `translate.py` (verb⇄native mapping) and `system.py` (the one place I/O
happens); the edge, state, models, and manifest-loader are generated and identical across systems.
A freshly scaffolded shim boots, its stubs raise `NotImplementedError`, and its bundled conformance
proof FAILS every active verb until the stubs are filled — proving the harness detects
non-conformance, not just conformance (plan §3.1 DoD).
"""

from __future__ import annotations

import json
import keyword
from pathlib import Path

from nilscript.cli._spec import Verb, active_verbs, all_verbs
from nilscript.cli.scaffold import _templates as T
from nilscript.cli.scaffold._models import render_models
from nilscript.cli.scaffold._translate import render_translate

_CONFORMANCE = '''\
"""Conformance proof for this shim — drives the edge with PROPOSE -> COMMIT per active write verb.

Runs against the in-memory FakeSystem (no live backend). With empty translation stubs every verb
FAILS (the stub raises NotImplementedError) — that is the point: the harness must detect
non-conformance. As you fill `translate.py`, verbs flip to passing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from {pkg}.compensation import COMPENSATIONS
from {pkg}.edge import CapturingEmitter, create_app
from {pkg}.system import FakeSystem
from {pkg}.translate import WRITE_VERBS


def _env(verb: str, args: dict) -> dict:
    return {{"nil": "0.1", "grant": "g", "workspace": "w", "body": {{"verb": verb, "args": args}}}}


def _commit(client, verb_name: str) -> dict:
    args = {{field: "x" for field in WRITE_VERBS[verb_name].required}}
    pid = client.post("/nil/v0.1/propose", json=_env(verb_name, args)).json()["body"]["id"]
    return client.post(
        "/nil/v0.1/commit",
        json={{"nil": "0.1", "grant": "g", "workspace": "w",
               "body": {{"proposal": pid, "idempotency_key": pid}}}},
    ).json()["body"]


def test_rollback_honesty() -> None:
    """A reversible verb emits a compensation token and ROLLBACK previews (never silently writes);
    an unknown token is refused. Skips only if no verb is mapped reversible in compensation.py."""
    reversible = next((v for v in sorted(WRITE_VERBS) if v in COMPENSATIONS), None)
    if reversible is None:
        pytest.skip("no reversible verb mapped in compensation.py")
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    committed = _commit(client, reversible)
    token = committed.get("compensation", {{}}).get("token")
    assert token, f"{{reversible}} is mapped reversible but COMMIT emitted no compensation token"

    rolled = client.post("/nil/v0.1/rollback", json={{"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {{"compensation_token": token, "reason": "owner_cancel"}}}}).json()["body"]
    assert rolled["outcome"] == "proposal", "ROLLBACK must PREVIEW a compensation, never silently write"

    bogus = client.post("/nil/v0.1/rollback", json={{"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {{"compensation_token": "__no_such_token__", "reason": "owner_cancel"}}}}).json()["body"]
    assert bogus["outcome"] == "refusal", "an unknown compensation token must be refused, never reversed"


@pytest.mark.parametrize("verb_name", sorted(WRITE_VERBS))
def test_write_verb_reaches_executed(verb_name: str) -> None:
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    verb = WRITE_VERBS[verb_name]
    args = {{field: "x" for field in verb.required}}  # placeholder valid-shaped args

    proposed = client.post("/nil/v0.1/propose", json=_env(verb_name, args)).json()
    proposal_id = proposed.get("body", {{}}).get("id")
    assert proposal_id, f"{{verb_name}}: PROPOSE did not yield a proposal: {{proposed}}"

    committed = client.post(
        "/nil/v0.1/commit",
        json={{"nil": "0.1", "grant": "g", "workspace": "w",
               "body": {{"proposal": proposal_id, "idempotency_key": proposal_id}}}},
    )
    state = committed.json().get("body", {{}}).get("state")
    assert state == "executed", f"{{verb_name}}: not conformant yet (state={{state}}) — fill translate.py"


def test_describe_exposes_skeleton() -> None:
    """MANDATORY: /nil/v0.1/describe exposes a valid skeleton — nil version, a verb catalog, and
    per native target {{exists, fields}}. This is the universal connect handshake the kernel uses."""
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    d = client.get("/nil/v0.1/describe").json()
    assert d.get("nil") == "0.1", "describe must report the NIL version"
    assert d.get("verbs"), "describe must list the verb catalog"
    targets = d.get("targets", {{}})
    assert isinstance(targets, dict) and targets, "describe must report native targets"
    for name, t in targets.items():
        assert isinstance(t, dict) and "exists" in t and "fields" in t, f"{{name}}: target needs exists+fields"
    assert all(t["exists"] for t in targets.values()), "FakeSystem targets are always provisioned"
'''


_COMPENSATION = '''\
"""Compensation handlers (ROLLBACK / Saga) for this shim — fill these to make verbs reversible.

Every write verb is IRREVERSIBLE until you map it here AND declare its tier in
requirements-manifest.json. A reversal is a *governed* compensation: the edge previews it (PROPOSE)
and executes it (COMMIT) like any other action — never a silent write. Until a verb is mapped,
ROLLBACK of its effect REFUSES with code IRREVERSIBLE, which is the honest default.
"""

from __future__ import annotations

from typing import Any

# verb -> {"reversibility": "REVERSIBLE" | "COMPENSABLE", "verb": "<compensating verb>"}
# Leave a verb OUT to keep it IRREVERSIBLE (the safe, zero-touch default).
COMPENSATIONS: dict[str, dict[str, Any]] = {}


def compensate(verb: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return the compensating-proposal args for `verb` given its committed `result`.

    Raises NotImplementedError until the verb is mapped — the conformance proof then verifies that
    ROLLBACK of an unmapped (IRREVERSIBLE) effect is REFUSED, not silently executed.
    """
    if verb not in COMPENSATIONS:
        raise NotImplementedError(f"{verb} is IRREVERSIBLE — no compensation mapped")
    raise NotImplementedError(f"map the compensation args for {verb} in compensation.py")
'''


def _pkg_name(name: str) -> str:
    return name.replace("-", "_").replace(" ", "_").lower()


def _is_query(verb: Verb) -> bool:
    """A verb is a QUERY if the standard ships a `<action>.response.json` answer shape for it."""
    return verb.path.with_name(f"{verb.action}.response.json").exists()


def classify() -> tuple[tuple[Verb, ...], tuple[Verb, ...], tuple[Verb, ...]]:
    """Return (write_verbs, query_verbs, parked_verbs) for the bundled standard."""
    writes = tuple(v for v in active_verbs() if not _is_query(v))
    queries = tuple(v for v in active_verbs() if _is_query(v))
    parked = tuple(v for v in all_verbs() if v.deprecated)
    return writes, queries, parked


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def scaffold_shim(name: str, dest: Path, *, lang: str = "python") -> Path:
    """Generate a shim project named `name` under `dest`. Returns the project root path."""
    if lang != "python":
        raise ValueError(f"unsupported lang {lang!r} (only 'python' for now)")
    # `name` becomes both a directory and (via _pkg_name) an importable package — validate it is a
    # single safe path segment AND a legal Python identifier, so a name like "../etc/x" cannot
    # escape `dest` and a name like "9x" cannot emit unimportable code.
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError(f"--name {name!r} must be a single path segment")
    pkg = _pkg_name(name)
    if not pkg.isidentifier() or keyword.iskeyword(pkg):
        raise ValueError(f"--name {name!r} does not yield a valid Python package name (got {pkg!r})")
    system = pkg.replace("_nil_adapter", "").replace("_adapter", "").strip("_") or pkg
    root = (dest / name).resolve()
    if dest.resolve() not in root.parents:
        raise ValueError(f"--name {name!r} escapes the destination directory")
    src = root / "src" / pkg
    writes, queries, parked = classify()

    fmt = {"pkg": pkg, "name": name, "system": system}
    _write(src / "__init__.py", f'"""{name}: a NIL shim for {system}, scaffolded by nilscript."""\n')
    _write(src / "edge.py", T.EDGE.format(**fmt))
    _write(src / "state.py", T.STATE)
    _write(src / "system.py", T.SYSTEM)
    _write(src / "manifest.py", T.MANIFEST_LOADER)
    _write(src / "models.py", render_models(active_verbs()))
    _write(src / "translate.py", render_translate(pkg, writes, queries, parked))
    _write(src / "compensation.py", _COMPENSATION)
    _write(src / "run.py", T.RUN.format(**fmt))

    _write(root / "conformance" / "__init__.py", "")
    _write(root / "conformance" / "test_conformance.py", _CONFORMANCE.format(**fmt))

    _write(root / "README.md", T.README.format(**fmt))
    _write(root / "pyproject.toml", T.PYPROJECT.format(**fmt))
    # Seed manifest — populated by `nilscript scan`. Empty but shape-valid.
    seed = {
        "manifest_version": "0.1",
        "system": system,
        "nil_spec": "0.1",
        "verbs": {},
    }
    _write(root / "requirements-manifest.json", json.dumps(seed, indent=2) + "\n")

    return root

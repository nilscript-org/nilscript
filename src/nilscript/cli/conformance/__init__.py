"""`conformance-test` (plan §3.3, Phase 4): drive a live NIL shim through the conformance matrix.

The runner is the intelligence; it is handed a `ShimProbe` (the four agent-plane calls) so it is
transport-agnostic and fully testable without a network — the CLI wires a real httpx probe to a
`--url`. The matrix asserts the load-bearing contract rules from the translation-shim guide §6: a
valid PROPOSE previews without writing, an unknown verb / missing arg REFUSES (not 500), COMMIT
executes and is idempotent on replay, STATUS reflects reality, and QUERY returns a bare `{data}`.

The harness must demonstrably **detect non-conformance** (a broken shim fails rows), not only confirm
conformance — both directions are tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ShimProbe(Protocol):
    """The agent-plane calls, returning each endpoint's parsed JSON body.

    `rollback` is optional: a shim that does not implement backward recovery simply omits it,
    and the runner skips the rollback-honesty rows (its verbs are then IRREVERSIBLE by default).
    """

    def propose(self, verb: str, args: dict[str, Any]) -> dict[str, Any]: ...

    def commit(self, proposal_id: str, idempotency_key: str) -> dict[str, Any]: ...

    def query(self, verb: str, args: dict[str, Any]) -> dict[str, Any]: ...

    def status(self, proposal_id: str) -> dict[str, Any]: ...

    def rollback(self, compensation_token: str, reason: str) -> dict[str, Any]: ...

    def describe(self) -> dict[str, Any]: ...  # GET /nil/v0.1/describe — required for conformance


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def _body(envelope: dict[str, Any]) -> dict[str, Any]:
    """NIL agent-plane responses are envelopes; QUERY is the exception (bare {data})."""
    return envelope.get("body", envelope) if isinstance(envelope, dict) else {}


def _outcome(envelope: dict[str, Any]) -> str:
    return str(_body(envelope).get("outcome", ""))


def _code(envelope: dict[str, Any]) -> str:
    return str(_body(envelope).get("code", ""))


def _compensation_token(committed: dict[str, Any]) -> str:
    """The reversal handle a backend emits on COMMIT (body.compensation or result.compensation)."""
    body = _body(committed)
    comp = body.get("compensation") or body.get("result", {}).get("compensation") or {}
    return str(comp.get("token", "")) if isinstance(comp, dict) else ""


def run_conformance(
    probe: ShimProbe,
    *,
    write_verb: str,
    write_args: dict[str, Any],
    unknown_verb: str = "namespace.__does_not_exist__",
    query_verb: str | None = None,
    query_args: dict[str, Any] | None = None,
    reversibility: str | None = None,
) -> list[Check]:
    """Run the conformance matrix against `probe`. Returns one Check per row (order stable).

    When `reversibility` (the write verb's declared tier) is given and the probe implements
    `rollback`, the rollback-honesty rows are appended — proving the shim cannot *lie* about
    reversal: an IRREVERSIBLE effect must refuse, a reversible one must *preview* a compensation
    (never a silent write), and an unknown token must never trigger a phantom reversal.
    """
    checks: list[Check] = []

    # Row 0 — discovery handshake (MANDATORY): the shim must expose /nil/v0.1/describe with a valid
    # skeleton — nil version, a verb catalog, and per native target {exists, fields}. This is how any
    # client connects uniformly (reachable → conformant → provisioned) without backend specifics.
    if hasattr(probe, "describe"):
        d = probe.describe()
        tgts = d.get("targets", {}) if isinstance(d, dict) else {}
        ok = (isinstance(d, dict) and d.get("nil") == "0.1" and bool(d.get("verbs"))
              and isinstance(tgts, dict)
              and all(isinstance(v, dict) and "exists" in v and "fields" in v for v in tgts.values()))
        checks.append(Check("exposes_describe_skeleton", ok,
                            f"nil={d.get('nil')!r} verbs={len(d.get('verbs', []))} targets={len(tgts)}"))

    # Row 1 — valid PROPOSE previews (a proposal, not a refusal), and yields a proposal id.
    proposed = probe.propose(write_verb, write_args)
    proposal_id = _body(proposed).get("id", "")
    checks.append(
        Check(
            "propose_valid_yields_proposal",
            _outcome(proposed) == "proposal" and bool(proposal_id),
            f"outcome={_outcome(proposed)!r} id={proposal_id!r}",
        )
    )

    # Row 2 — unknown verb REFUSES (contract: a verb the backend lacks is a refusal, not an error).
    unknown = probe.propose(unknown_verb, {})
    checks.append(
        Check(
            "unknown_verb_refuses",
            _outcome(unknown) == "refusal",
            f"outcome={_outcome(unknown)!r}",
        )
    )

    # Row 3 — missing a required arg REFUSES with a field pointer.
    thin_args = {k: v for k, v in write_args.items()}
    dropped = next(iter(thin_args), None)
    if dropped is not None:
        thin_args.pop(dropped)
    missing = probe.propose(write_verb, thin_args)
    checks.append(
        Check(
            "missing_required_arg_refuses",
            _outcome(missing) == "refusal" and bool(dropped),
            f"dropped={dropped!r} outcome={_outcome(missing)!r}",
        )
    )

    # Row 4 — COMMIT executes the previewed proposal.
    key = f"conf-{proposal_id}"
    committed = probe.commit(proposal_id, key) if proposal_id else {}
    state = _body(committed).get("state", "")
    checks.append(Check("commit_executes", state == "executed", f"state={state!r}"))

    # Row 5 — COMMIT is idempotent: replaying the same key is flagged replayed, not re-executed.
    replayed = probe.commit(proposal_id, key) if proposal_id else {}
    is_replayed = _body(replayed).get("replayed") is True
    checks.append(Check("commit_idempotent_replay", is_replayed, f"replayed={_body(replayed).get('replayed')!r}"))

    # Row 6 — STATUS reflects an executed proposal.
    after = probe.status(proposal_id) if proposal_id else {}
    checks.append(
        Check("status_reports_executed", _body(after).get("state") == "executed", f"state={_body(after).get('state')!r}")
    )

    # Row 7 — COMMIT of an unknown proposal does not execute (refusal/expired, never a phantom write).
    phantom = probe.commit("__no_such_proposal__", "conf-phantom")
    phantom_state = _body(phantom).get("state", "")
    phantom_outcome = _outcome(phantom)
    checks.append(
        Check(
            "unknown_proposal_does_not_execute",
            phantom_state != "executed" and phantom_outcome != "proposal",
            f"state={phantom_state!r} outcome={phantom_outcome!r}",
        )
    )

    # Row 8 — QUERY returns a bare {data} (NOT an envelope) when a query verb is provided.
    if query_verb is not None:
        answer = probe.query(query_verb, query_args or {})
        bare = isinstance(answer, dict) and "data" in answer and "performative" not in answer
        checks.append(Check("query_returns_bare_data", bare, f"keys={sorted(answer)[:4] if isinstance(answer, dict) else answer!r}"))

    # Rollback-honesty rows — appended only when a reversibility tier is declared AND the shim
    # implements rollback. They prove the shim cannot misrepresent its reversal capability.
    if reversibility is not None and hasattr(probe, "rollback"):
        checks.extend(
            _rollback_rows(probe, reversibility, _compensation_token(committed))
        )

    return checks


def _rollback_rows(probe: ShimProbe, reversibility: str, token: str) -> list[Check]:
    rows: list[Check] = []
    rolled = probe.rollback(token, "saga_unwind")
    r_out, r_code = _outcome(rolled), _code(rolled)
    r_state = _body(rolled).get("state", "")

    # R1 — reversibility is honored: IRREVERSIBLE refuses with the honest code; a reversible /
    # compensable effect is answered by a *previewed* compensation (a proposal), never executed.
    if reversibility == "IRREVERSIBLE":
        # An honest refusal proves no phantom reversal: IRREVERSIBLE (named) or COMPENSATION_EXPIRED
        # (no reversal handle was ever issued) both mean "this effect cannot be undone" — never a write.
        rows.append(
            Check(
                "rollback_irreversible_refuses",
                r_out == "refusal"
                and r_code in {"IRREVERSIBLE", "COMPENSATION_EXPIRED"}
                and r_state != "executed",
                f"outcome={r_out!r} code={r_code!r} state={r_state!r}",
            )
        )
    else:
        rows.append(
            Check(
                "rollback_previews_compensation",
                r_out == "proposal" and r_state != "executed",
                f"outcome={r_out!r} state={r_state!r}",
            )
        )

    # R2 — no silent write on reversal: the rollback response must not itself be a bare executed
    # state with no preview/refusal. (For both branches the reversal is governed, not silent.)
    rows.append(
        Check(
            "rollback_no_silent_write",
            r_state != "executed" and r_out in {"proposal", "refusal"},
            f"outcome={r_out!r} state={r_state!r}",
        )
    )

    # R3 — an unknown / expired compensation token never triggers a phantom reversal.
    expired = probe.rollback("__no_such_token__", "saga_unwind")
    rows.append(
        Check(
            "rollback_unknown_token_refuses",
            _outcome(expired) == "refusal" and _body(expired).get("state") != "executed",
            f"outcome={_outcome(expired)!r} state={_body(expired).get('state')!r}",
        )
    )
    return rows


def summarize(checks: list[Check]) -> tuple[int, int]:
    """Return (passed, total)."""
    passed = sum(1 for c in checks if c.passed)
    return passed, len(checks)

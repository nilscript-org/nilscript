"""NIL sentence models, frozen and spec-conformant (nilscript 0.1.0-draft §4–§5, Annex A).

DECIDE is owner-plane and deliberately has no model here: this layer can never speak it.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nilscript.sdk.refusals import RefusalCode

VERB_PATTERN = r"^[a-z]+\.[a-z_]+$"
PREVIEW_LOCALE_PATTERN = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
PRIMARY_LOCALE = "ar"
# Proposal ids are server-minted opaque strings, but they travel into URL paths
# (GET /nil/v0.1/status/{id}) — constrain to a URL-safe alphabet so a hostile
# System can never steer our bearer-authed client via ../ segments.
PROPOSAL_ID_PATTERN = r"^[A-Za-z0-9_-]{8,128}$"


class NilModel(BaseModel):
    """Base for every NIL shape: immutable, unknown members rejected (§5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Performative(StrEnum):
    """The closed agent-plane set (§4).

    ROLLBACK is the lifecycle-closing primitive: the backward-recovery counterpart of
    PROPOSE. It does not execute anything — it *requests* a governed reversal, which the
    System answers with a PROPOSAL (the compensation preview) that is then COMMITted like
    any other action. Reversal therefore reuses the entire preview/commit machinery, so
    the "no silent write" invariant holds for free.
    """

    PROPOSE = "PROPOSE"
    PROPOSAL = "PROPOSAL"
    COMMIT = "COMMIT"
    STATUS = "STATUS"
    QUERY = "QUERY"
    EVENT = "EVENT"
    ROLLBACK = "ROLLBACK"


class Reversibility(StrEnum):
    """How (and whether) an effect can be reversed — declared per verb (Saga tiers).

    REVERSIBLE    — a clean deterministic inverse exists (create ↔ delete).
    COMPENSABLE   — no inverse, but a different forward action offsets it (invoice → credit-note).
    IRREVERSIBLE  — no sanctioned reversal; must be caught at PROPOSE/preview time.

    The legacy default for any unmarked verb is IRREVERSIBLE: a System refuses to claim a
    reversal it cannot honour, so existing shims are conformant with zero new code.
    """

    REVERSIBLE = "REVERSIBLE"
    COMPENSABLE = "COMPENSABLE"
    IRREVERSIBLE = "IRREVERSIBLE"


class RollbackReason(StrEnum):
    """Why a reversal is being requested (carried on a ROLLBACK body)."""

    SAGA_UNWIND = "saga_unwind"
    OWNER_CANCEL = "owner_cancel"
    DOWNSTREAM_FAILED = "downstream_failed"
    AGENT_REPAIR = "agent_repair"


class Tier(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ProposalState(StrEnum):
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"
    EXPIRED = "expired"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    SUSPENDED = "suspended"


class EventKind(StrEnum):
    PROPOSED = "proposed"
    REFUSED = "refused"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"
    EXPIRED = "expired"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SUSPENDED = "suspended"
    RESUMED = "resumed"
    HANDOFF_HUMAN = "handoff_human"
    DEPRECATION_WARNING = "deprecation_warning"
    # Backward-recovery lifecycle (ROLLBACK / Saga compensation).
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_REFUSED = "compensation_refused"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Claim(StrEnum):
    """The strongest claim the Speaker may relay (§11.1) — System-computed, never ours."""

    SUCCESS = "success"
    PARTIAL = "partial"
    ASK_MISSING = "ask_missing"
    FAILURE = "failure"
    NONE = "none"


class ProposeBody(NilModel):
    verb: str = Field(pattern=VERB_PATTERN)
    args: dict[str, Any]


class CommitBody(NilModel):
    proposal: str = Field(pattern=PROPOSAL_ID_PATTERN)
    idempotency_key: str = Field(min_length=8)


class RollbackBody(NilModel):
    """Speaker→System ROLLBACK: request a governed reversal of a committed effect.

    Answered by a PROPOSAL (the compensation preview), whose tier drives the authority gate
    — low-risk REVERSIBLE may self-heal within grant; high-value/COMPENSABLE is forced to a
    human DECIDE. The previewed compensation is then executed through an ordinary COMMIT.
    """

    compensation_token: str = Field(min_length=8)
    reason: RollbackReason
    # Rolling back twice under one key replays the original compensation outcome; it never
    # double-compensates. Optional: a System may mint the key itself on the answering proposal.
    idempotency_key: str | None = Field(default=None, min_length=8)


class QueryBody(NilModel):
    verb: str = Field(pattern=VERB_PATTERN)
    args: dict[str, Any] | None = None


class StatusBody(NilModel):
    proposal: str = Field(pattern=PROPOSAL_ID_PATTERN)
    state: ProposalState | None = None
    replayed: bool | None = None
    # SEQRD-PC: a reversible/compensable executed write returns its compensation handle here
    # (e.g. {"reversibility": "REVERSIBLE", "token": "..."}) so a later ROLLBACK can reference it.
    compensation: dict[str, Any] | None = None
    # The SSOT result of an executed write: the affected entity {type, id, name, url} and the
    # system of record {system, read_after_write}. Surfaces the real backend result to the committer.
    result: dict[str, Any] | None = None


class Candidate(NilModel):
    id: str
    name: str
    source: str | None = None


class ProposalBody(NilModel):
    """System→Speaker PROPOSAL: a previewed proposal or a Refusal (§6.6, Annex A)."""

    outcome: Literal["proposal", "refusal"]
    id: str | None = Field(default=None, pattern=PROPOSAL_ID_PATTERN)
    verb: str | None = Field(default=None, pattern=VERB_PATTERN)
    tier: Tier | None = None
    preview: dict[str, str] | None = None
    resolved: dict[str, Any] | None = None
    modifiable: tuple[str, ...] | None = None
    expires_at: datetime | None = None
    code: RefusalCode | None = None
    message: str | None = None
    field: str | None = None
    candidates: tuple[Candidate, ...] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def _enforce_outcome_shape(self) -> Self:
        if self.outcome == "proposal":
            missing = [
                name
                for name in ("id", "verb", "tier", "preview", "expires_at")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"outcome 'proposal' requires {missing}")
            if self.code is not None or self.candidates is not None:
                raise ValueError("outcome 'proposal' must not carry code or candidates")
        else:
            if self.code is None:
                raise ValueError("outcome 'refusal' requires a code")
            present = [
                name
                for name in ("tier", "preview", "expires_at")
                if getattr(self, name) is not None
            ]
            if present:
                raise ValueError(f"outcome 'refusal' must not carry {present}")
        if self.code is RefusalCode.AMBIGUOUS and not self.candidates:
            raise ValueError("AMBIGUOUS refusals must carry candidates")
        if self.preview is not None:
            if not self.preview:
                raise ValueError("preview must carry at least one locale")
            bad = [key for key in self.preview if not PREVIEW_LOCALE_PATTERN.match(key)]
            if bad:
                raise ValueError(f"preview keys must be BCP 47 short tags, got {bad}")
        return self

    @property
    def is_refusal(self) -> bool:
        return self.outcome == "refusal"

    def preview_for(self, locale: str) -> str:
        """The locale's preview text VERBATIM — falling back to the primary (ar), then any."""
        if self.preview is None:
            raise ValueError("refusals carry no preview")
        exact = self.preview.get(locale)
        if exact is not None:
            return exact
        primary = self.preview.get(PRIMARY_LOCALE)
        if primary is not None:
            return primary
        return next(iter(self.preview.values()))


class EntityRef(NilModel):
    type: str
    id: str | None = None
    name: str | None = None
    url: str | None = None


class SsotRef(NilModel):
    system: str
    read_after_write: bool | None = None


class Compensation(NilModel):
    """How a just-committed effect may later be reversed (emitted on the EVENT result).

    `token` is the handle a future ROLLBACK references; it is present only when the effect
    is actually reversible. After `expires_at` the reversal path is gone — effectively
    IRREVERSIBLE in practice.
    """

    reversibility: Reversibility
    token: str | None = Field(default=None, min_length=8)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _token_iff_reversible(self) -> Self:
        if self.reversibility is Reversibility.IRREVERSIBLE and self.token is not None:
            raise ValueError("IRREVERSIBLE effects must not carry a compensation token")
        return self


class ResultEnvelope(NilModel):
    """The System-composed outcome (§11.1). `claim` bounds what we may tell a human."""

    claim: Claim
    changed: bool
    verified: bool
    entity: EntityRef | None = None
    ssot: SsotRef | None = None
    replayed: bool | None = None
    data: dict[str, Any] | None = None
    data_gaps: tuple[str, ...] | None = None
    compensation: Compensation | None = None


class EventBody(NilModel):
    event: EventKind
    severity: Severity
    # Same id constraint as CommitBody/StatusBody: an EVENT's proposal id is used to look
    # up the owning conversation and to build a Temporal workflow id — reject malformed ids
    # at the boundary rather than letting them reach those sinks.
    proposal: str | None = Field(default=None, pattern=PROPOSAL_ID_PATTERN)
    result: ResultEnvelope | None = None
    data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _executed_requires_result(self) -> Self:
        if self.event is EventKind.EXECUTED and (self.proposal is None or self.result is None):
            raise ValueError("'executed' events require proposal and result")
        return self


class Envelope(NilModel):
    """The sentence envelope (§5)."""

    nil: Literal["0.1"] = "0.1"
    id: str = Field(min_length=8)
    performative: Performative
    grant: str
    workspace: str
    ts: datetime
    trace: str | None = None
    body: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def make_envelope(
    performative: Performative,
    body: NilModel,
    *,
    sentence_id: str,
    grant: str,
    workspace: str,
    ts: datetime,
    trace: str | None = None,
) -> Envelope:
    return Envelope(
        id=sentence_id,
        performative=performative,
        grant=grant,
        workspace=workspace,
        ts=ts,
        trace=trace,
        body=body.model_dump(mode="json", exclude_none=True),
    )

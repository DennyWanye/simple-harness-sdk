# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable BaseAgent records (execution schema v10) shared by kernel and storage.

Pure dataclasses: no SQLite, no ``simple_harness.agents`` import.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from simple_harness.contracts import FrozenJsonValue, freeze_json

AGENT_TURN_PHASES = ("queued", "running", "result_pending", "committed", "failed")
AGENT_DELEGATION_STATES = ("reserved", "launched", "settled", "failed")
AGENT_LIFECYCLES = ("open", "closing", "closed")
BASE_AGENT_API_MODE = "base_agent_v1"
BASE_AGENT_INPUT_KIND = "base_agent_input"


def _frozen_object(value: object, name: str) -> FrozenJsonValue:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    return freeze_json(dict(value))


@dataclass(frozen=True, slots=True)
class AgentBindingRecord:
    agent_id: str
    run_id: str
    owner_scope: str
    api_mode: str
    role: str
    creation_key: str
    config_json: FrozenJsonValue
    config_hash: str
    control_generation: int
    created_at: float
    lifecycle: str = "open"
    lifecycle_updated_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_json", _frozen_object(self.config_json, "config_json"))
        if self.lifecycle not in AGENT_LIFECYCLES:
            raise ValueError("unknown agent lifecycle")


AGENT_JOURNAL_KINDS = ("instructions", "user_input", "assistant", "tool_result", "feedback")
AGENT_JOURNAL_PROVENANCES = ("input", "model", "ledger", "derived")
AGENT_JOURNAL_VISIBILITIES = ("context", "journal_only")


@dataclass(frozen=True, slots=True)
class AgentJournalRecord:
    """One incremental, immutable session record (BA-v1.0 §6.2 Journal)."""

    record_id: str
    agent_id: str
    seq: int
    append_id: str
    kind: str
    turn_id: str | None
    protocol_group_id: str
    message_json: FrozenJsonValue
    content_hash: str
    provenance: str
    visibility: str
    full_record_seq: int | None
    lease_epoch: int
    created_at: float

    def __post_init__(self) -> None:
        if self.kind not in AGENT_JOURNAL_KINDS:
            raise ValueError("unknown journal record kind")
        if self.provenance not in AGENT_JOURNAL_PROVENANCES:
            raise ValueError("unknown journal provenance")
        if self.visibility not in AGENT_JOURNAL_VISIBILITIES:
            raise ValueError("unknown journal visibility")
        object.__setattr__(self, "message_json", _frozen_object(self.message_json, "message_json"))


@dataclass(frozen=True, slots=True)
class AgentContextSelectionRecord:
    """What one assembled request contained (BA-v1.0 §7.4 step 9)."""

    selection_id: str
    agent_id: str
    turn_id: str | None
    revision: int
    source_highwater: int
    selected_seqs: tuple[int, ...]
    dropped_ranges: tuple[tuple[int, int], ...]
    required_over_budget: bool
    message_tokens: int
    tool_tokens: int
    budget_tokens: int
    policy_hash: str
    tokenizer_fingerprint: str
    query_hash: str | None
    index_generation: str | None
    provider_request_id: str | None
    request_hash: str | None
    request_tokens: int | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class AgentSummaryRecord:
    """A derived summary with its sources; never an authority (BA21)."""

    summary_id: str
    agent_id: str
    scope: str
    from_seq: int
    to_seq: int
    source_hash: str
    summary_json: FrozenJsonValue
    generated_by: str
    validity: str
    created_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary_json", _frozen_object(self.summary_json, "summary_json"))


@dataclass(frozen=True, slots=True)
class AgentVectorRecord:
    vector_id: str
    agent_id: str
    record_seq: int
    source_hash: str
    embedding_fingerprint: str
    dim: int
    vector: tuple[float, ...]
    created_at: float


AGENT_INDEX_JOB_STATES = ("pending", "claimed", "done", "error")


@dataclass(frozen=True, slots=True)
class AgentIndexJobRecord:
    job_id: str
    agent_id: str
    record_seq: int
    source_hash: str
    embedding_fingerprint: str
    state: str
    attempts: int
    lease_owner: str | None
    lease_expires_at: float | None
    error_code: str | None
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        if self.state not in AGENT_INDEX_JOB_STATES:
            raise ValueError("unknown index job state")


@dataclass(frozen=True, slots=True)
class AgentCreationBatchRecord:
    batch_id: str
    owner_scope: str
    batch_key: str
    batch_fingerprint: str
    agent_ids: tuple[str, ...]
    config_hashes: tuple[str, ...]
    state: str
    receipt: FrozenJsonValue | None
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        if self.state not in ("reserved", "committed"):
            raise ValueError("unknown batch state")
        object.__setattr__(self, "agent_ids", tuple(self.agent_ids))
        object.__setattr__(self, "config_hashes", tuple(self.config_hashes))
        if self.receipt is not None:
            object.__setattr__(self, "receipt", _frozen_object(self.receipt, "receipt"))


@dataclass(frozen=True, slots=True)
class AgentControlCommandRecord:
    command_id: str
    agent_id: str
    kind: str
    target_turn_id: str | None
    control_generation: int
    request_hash: str
    receipt: FrozenJsonValue
    created_at: float

    def __post_init__(self) -> None:
        if self.kind not in ("close", "cancel_turn"):
            raise ValueError("unknown control command kind")
        object.__setattr__(self, "receipt", _frozen_object(self.receipt, "receipt"))


@dataclass(frozen=True, slots=True)
class AgentTurnRecord:
    turn_id: str
    agent_id: str
    input_id: str
    input_hash: str
    input_json: FrozenJsonValue
    continuation_id: str | None
    seq: int
    phase: str
    staged_result_hash: str | None
    staged_result_json: FrozenJsonValue | None
    provider_turn_ordinal_from: int | None
    provider_turn_ordinal_to: int | None
    lease_epoch: int | None
    created_at: float
    updated_at: float
    # Durable per-turn baselines (write-once at first admission; resumes keep them).
    tool_call_ordinal_from: int | None = None

    def __post_init__(self) -> None:
        if self.phase not in AGENT_TURN_PHASES:
            raise ValueError("unknown agent turn phase")
        object.__setattr__(self, "input_json", _frozen_object(self.input_json, "input_json"))
        if self.staged_result_json is not None:
            object.__setattr__(
                self,
                "staged_result_json",
                _frozen_object(self.staged_result_json, "staged_result_json"),
            )


@dataclass(frozen=True, slots=True)
class AgentTurnResultRecord:
    turn_id: str
    agent_id: str
    result_hash: str
    result_json: FrozenJsonValue
    commit_receipt_id: str
    usage_refs: tuple[str, ...]
    committed_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_json", _frozen_object(self.result_json, "result_json"))
        object.__setattr__(self, "usage_refs", tuple(self.usage_refs))


@dataclass(frozen=True, slots=True)
class AgentDelegationRecord:
    delegation_id: str
    parent_agent_id: str
    parent_turn_id: str
    ordinal: int
    child_agent_id: str
    child_run_id: str
    ticket_id: str
    state: str
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        if self.state not in AGENT_DELEGATION_STATES:
            raise ValueError("unknown agent delegation state")


__all__ = (
    "AGENT_DELEGATION_STATES",
    "AGENT_INDEX_JOB_STATES",
    "AGENT_JOURNAL_KINDS",
    "AGENT_JOURNAL_PROVENANCES",
    "AGENT_JOURNAL_VISIBILITIES",
    "AGENT_LIFECYCLES",
    "AGENT_TURN_PHASES",
    "BASE_AGENT_API_MODE",
    "BASE_AGENT_INPUT_KIND",
    "AgentBindingRecord",
    "AgentContextSelectionRecord",
    "AgentControlCommandRecord",
    "AgentCreationBatchRecord",
    "AgentDelegationRecord",
    "AgentIndexJobRecord",
    "AgentJournalRecord",
    "AgentSummaryRecord",
    "AgentTurnRecord",
    "AgentTurnResultRecord",
    "AgentVectorRecord",
)

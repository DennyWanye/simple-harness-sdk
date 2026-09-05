"""Versioned, metadata-only public audit of durable Run operations."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass
from typing import Protocol

from simple_harness.contracts import RunId, canonical_json


def audit_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class RunAuditUnavailable(RuntimeError):
    code = "run_audit_unavailable"


def audit_label_syntax(value):
    """Syntax only; callers must establish registration or a closed SDK vocabulary."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", value) is None:
        return None
    return value


# Closed SDK vocabulary, not a pattern-based promise about external strings.
SDK_AUDIT_ERROR_CODES = frozenset(
    {
        "provider_error",
        "provider_error_after_handoff",
        "provider_cancelled_after_handoff",
        "provider_response_not_durable",
        "provider_authentication_failed",
        "provider_payment_required",
        "provider_rate_limited",
        "provider_server_error",
        "provider_timeout",
        "provider_cancelled",
        "provider_transport_error",
        "provider_request_rejected",
        "provider_protocol_error",
        "tool_registry_error",
        "duplicate_tool",
        "unknown_tool",
        "malformed_tool_arguments",
        "duplicate_tool_call",
        "late_tool_result",
        "tool_dispatch_interrupted",
        "provider_budget_exceeded",
        "provider_budget_unknown",
        "runtime_boundary_failed",
        "runtime_boundary_interrupted",
        "runtime_boundary_rejected",
    }
)


def audit_error_code(value):
    return value if isinstance(value, str) and value in SDK_AUDIT_ERROR_CODES else None


def audit_reference(kind, value):
    return None if value is None else kind + ":" + audit_hash([kind, value])


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("invalid audit identity")


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid audit integer")


@dataclass(frozen=True, slots=True)
class RunAuditUsageV1:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    budget_kind: str = "unknown"
    budget_amount_micros: int | None = None
    cache_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self):
        if self.budget_kind not in {"unknown", "trusted_usage", "estimated_upper_bound"}:
            raise ValueError("invalid audit usage provenance")
        for value in (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.budget_amount_micros,
            self.cache_tokens,
            self.reasoning_tokens,
        ):
            if value is not None:
                _integer(value)
        if self.budget_kind == "unknown" and self.budget_amount_micros is not None:
            raise ValueError("unknown charge has no amount")


@dataclass(frozen=True, slots=True)
class RunOperationAuditV1:
    operation_id: str
    kind: str
    state: str
    source_version: int
    source_hash: str
    source_id: str
    handoff_attempt: int | None = None
    rehandoff_count: int | None = None
    record_type: str = "head"
    usage: RunAuditUsageV1 | None = None
    operation_name: str | None = None
    error_code: str | None = None
    error_code_hash: str | None = None
    operation_name_hash: str | None = None
    raw_call_id_hash: str | None = None
    runtime_epoch: int | None = None
    related_run_refs: tuple[str, ...] = ()
    created_at: float | None = None
    handed_off_at: float | None = None
    settled_at: float | None = None
    request_id: str | None = None
    call_id: str | None = None
    raw_call_id: str | None = None
    turn_ordinal: int | None = None
    call_ordinal: int | None = None
    effect_id: str | None = None
    provider_invocation_id: str | None = None
    request_hash: str | None = None
    result_hash: str | None = None
    evidence_ref_hash: str | None = None
    authorization_ref_hash: str | None = None

    @property
    def handoff_to_settlement_seconds(self):
        if (
            self.handed_off_at is None
            or self.settled_at is None
            or self.settled_at < self.handed_off_at
        ):
            return None
        return self.settled_at - self.handed_off_at

    def __post_init__(self):
        object.__setattr__(self, "related_run_refs", tuple(self.related_run_refs))
        for label in (self.operation_name, self.error_code):
            if label is not None and audit_label_syntax(label) != label:
                raise ValueError("unsafe audit label")
        if self.error_code is not None and audit_error_code(self.error_code) is None:
            raise ValueError("unmapped audit error code")
        for timestamp in (self.created_at, self.handed_off_at, self.settled_at):
            if timestamp is not None and (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (float, int))
                or not math.isfinite(timestamp)
                or timestamp < 0
            ):
                raise ValueError("invalid audit timestamp")
        for value in (self.operation_id, self.source_id, self.state):
            _identifier(value)
        if self.kind not in {
            "child",
            "workflow",
            "runtime",
            "provider",
            "effect",
            "tool",
            "decision",
            "run",
            "control",
            "context",
            "reconciliation",
            "admission",
            "continuation",
        }:
            raise ValueError("invalid audit kind")
        if self.record_type not in {"head", "transition", "boundary", "receipt", "proposal"}:
            raise ValueError("invalid audit record type")
        _integer(self.source_version)
        for value in (self.handoff_attempt, self.rehandoff_count):
            if value is not None:
                _integer(value)
        if len(self.source_hash) != 64 or any(
            c not in "0123456789abcdef" for c in self.source_hash
        ):
            raise ValueError("invalid audit source hash")
        if self.usage is not None and type(self.usage) is not RunAuditUsageV1:
            raise TypeError("invalid audit usage")

    def to_json(self):

        return {
            **asdict(self),
            "related_run_refs": list(self.related_run_refs),
            "handoff_to_settlement_seconds": self.handoff_to_settlement_seconds,
        }


@dataclass(frozen=True, slots=True)
class RunOperationAuditSnapshotV1:
    run_id: str
    run_state: str
    run_version: int
    operations: tuple[RunOperationAuditV1, ...]
    truncated: bool
    coverage_gaps: tuple[str, ...]
    schema_version: int = 1
    root_run_id: str | None = None
    parent_run_id: str | None = None
    recording_contract_version: int | None = None

    def __post_init__(self):
        _identifier(self.run_id)
        _integer(self.run_version)
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.truncated) is not bool
        ):
            raise ValueError("invalid audit snapshot schema")
        if type(self.operations) is not tuple or any(
            type(o) is not RunOperationAuditV1 for o in self.operations
        ):
            raise TypeError("invalid audit operations")
        if type(self.coverage_gaps) is not tuple:
            raise TypeError("invalid coverage gaps")

    def _payload(self):
        from .runtime_audit import RUNTIME_BOUNDARIES

        return dict(
            schema_version=self.schema_version,
            run_id=self.run_id,
            run_state=self.run_state,
            run_version=self.run_version,
            root_run_id=self.root_run_id,
            parent_run_id=self.parent_run_id,
            operations=[o.to_json() for o in self.operations],
            truncated=self.truncated,
            coverage_gaps=list(self.coverage_gaps),
            current_source_complete=self.current_source_complete,
            history_coverage=self.history_coverage,
            recording_contract_version=self.recording_contract_version,
            recording_boundaries=sorted(RUNTIME_BOUNDARIES),
            recording_coverage="verified_current_intervals"
            if not self.coverage_gaps
            else "unverified",
            source_set=list(self.source_set),
        )

    @property
    def source_set(self):
        from .sqlite.audit_core import CORE_SOURCES

        return (
            (
                "provider_invocations",
                "execution_effects",
                "decisions",
                "run_admissions",
                "continuations",
                "conversation_commands",
                "run_events",
                "workflow_checkpoints",
                "reconciliation_resolutions",
            )
            + tuple(item[0] for item in CORE_SOURCES)
            + ("workflow_spawn_continuation_ready",)
        )

    @property
    def current_source_complete(self):
        return not self.truncated

    @property
    def history_coverage(self):
        return "partial" if self.coverage_gaps else "recorded"

    def to_json(self):
        return {**self._payload(), "snapshot_hash": self.snapshot_hash}

    @property
    def snapshot_hash(self):
        return audit_hash(self._payload())


class RunOperationAuditPort(Protocol):
    def open_run_operation_audit(
        self, run_id: RunId, *, page_size: int = 256
    ) -> RunOperationAuditPageV1: ...

    def read_run_operation_audit_page(
        self, run_id: RunId, *, cursor: str
    ) -> RunOperationAuditPageV1: ...

    def read_run_operation_audit(
        self, run_id: RunId, *, limit: int = 256
    ) -> RunOperationAuditSnapshotV1: ...


@dataclass(frozen=True, slots=True)
class RunOperationAuditPageV1:
    run_id: str
    snapshot_hash: str
    page_index: int
    page_size: int
    total_operations: int
    total_pages: int
    page_hash: str
    operations: tuple[RunOperationAuditV1, ...]
    next_cursor: str | None
    metadata: object

    def __post_init__(self):
        from simple_harness.contracts import freeze_json

        object.__setattr__(self, "metadata", freeze_json(self.metadata))

    def to_json(self):
        from simple_harness.contracts import thaw_json

        return dict(
            schema_version=1,
            run_id=self.run_id,
            snapshot_hash=self.snapshot_hash,
            page_index=self.page_index,
            page_size=self.page_size,
            total_operations=self.total_operations,
            total_pages=self.total_pages,
            page_hash=self.page_hash,
            operations=[o.to_json() for o in self.operations],
            next_cursor=self.next_cursor,
            metadata=thaw_json(self.metadata),
            snapshot_source_complete=True,
        )

# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Serializable proposal state for durable task workflows.

This module defines the state machine for multi-turn LLM proposals with
tool execution, convergence tracking, and gate enforcement.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from simple_harness.contracts import JsonValue
from simple_harness.workflow.contracts import canonical_json, validate_json_value

PROPOSAL_STATE_SCHEMA_VERSION = 1


def _json_object(value: Mapping[str, JsonValue] | None) -> dict[str, JsonValue]:
    copied = copy.deepcopy(dict(value or {}))
    validate_json_value(copied)
    return copied


def _json_objects(
    values: Sequence[Mapping[str, JsonValue]],
) -> tuple[dict[str, JsonValue], ...]:
    return tuple(_json_object(value) for value in values)


@dataclass(frozen=True, slots=True)
class GateConfigV1:
    """Configuration for proposal execution gates."""

    max_turns: int = 20
    tool_budget_hard: int = 100
    wall_clock_seconds: float | None = None
    max_budget_usd: float | None = None
    per_tool_max_consecutive: int = 8

    def __post_init__(self) -> None:
        if self.max_turns < 1 or self.tool_budget_hard < 0:
            raise ValueError("gate turn and tool budgets are invalid")
        if self.wall_clock_seconds is not None and self.wall_clock_seconds <= 0:
            raise ValueError("wall_clock_seconds must be positive when enabled")
        if self.max_budget_usd is not None and self.max_budget_usd < 0:
            raise ValueError("max_budget_usd must not be negative")
        if self.per_tool_max_consecutive < 1:
            raise ValueError("per_tool_max_consecutive must be positive")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "max_turns": self.max_turns,
            "tool_budget_hard": self.tool_budget_hard,
            "wall_clock_seconds": self.wall_clock_seconds,
            "max_budget_usd": self.max_budget_usd,
            "per_tool_max_consecutive": self.per_tool_max_consecutive,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> GateConfigV1:
        return cls(
            max_turns=int(value.get("max_turns", 20)),
            tool_budget_hard=int(value.get("tool_budget_hard", 100)),
            wall_clock_seconds=(
                float(value["wall_clock_seconds"])
                if value.get("wall_clock_seconds") is not None
                else None
            ),
            max_budget_usd=(
                float(value["max_budget_usd"]) if value.get("max_budget_usd") is not None else None
            ),
            per_tool_max_consecutive=int(value.get("per_tool_max_consecutive", 8)),
        )


@dataclass(frozen=True, slots=True)
class GateStateV1:
    """Runtime state for proposal execution gates."""

    started_at: float
    turns_used: int = 0
    tools_used: int = 0
    cost_usd: float = 0.0
    per_tool_consecutive: Mapping[str, int] = field(default_factory=dict)
    per_tool_last_sig: Mapping[str, str] = field(default_factory=dict)
    last_transition: str = "intake"
    terminated: bool = False
    terminated_reason: str | None = None

    def __post_init__(self) -> None:
        if self.started_at < 0 or self.turns_used < 0 or self.tools_used < 0 or self.cost_usd < 0:
            raise ValueError("gate state counters are invalid")
        consecutive = {str(key): int(value) for key, value in self.per_tool_consecutive.items()}
        if any(value < 0 for value in consecutive.values()):
            raise ValueError("per-tool consecutive counters must not be negative")
        last_sig = {str(key): str(value) for key, value in self.per_tool_last_sig.items()}
        object.__setattr__(self, "per_tool_consecutive", consecutive)
        object.__setattr__(self, "per_tool_last_sig", last_sig)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "started_at": self.started_at,
            "turns_used": self.turns_used,
            "tools_used": self.tools_used,
            "cost_usd": self.cost_usd,
            "per_tool_consecutive": dict(self.per_tool_consecutive),
            "per_tool_last_sig": dict(self.per_tool_last_sig),
            "last_transition": self.last_transition,
            "terminated": self.terminated,
            "terminated_reason": self.terminated_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> GateStateV1:
        return cls(
            started_at=float(value.get("started_at", 0.0)),
            turns_used=int(value.get("turns_used", 0)),
            tools_used=int(value.get("tools_used", 0)),
            cost_usd=float(value.get("cost_usd", 0.0)),
            per_tool_consecutive=dict(value.get("per_tool_consecutive", {})),
            per_tool_last_sig=dict(value.get("per_tool_last_sig", {})),
            last_transition=str(value.get("last_transition", "intake")),
            terminated=bool(value.get("terminated", False)),
            terminated_reason=(
                str(value["terminated_reason"])
                if value.get("terminated_reason") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ConvergenceStateV1:
    """Convergence tracking for proposal iterations."""

    no_progress_turns: int = 0
    repeated_signature_turns: int = 0
    completion_rejections: int = 0
    verify_failures: int = 0
    self_check_failures: int = 0
    last_progress_reason: str | None = None
    last_stop_reason: str | None = None

    def __post_init__(self) -> None:
        counters = (
            self.no_progress_turns,
            self.repeated_signature_turns,
            self.completion_rejections,
            self.verify_failures,
            self.self_check_failures,
        )
        if any(value < 0 for value in counters):
            raise ValueError("convergence counters must not be negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "no_progress_turns": self.no_progress_turns,
            "repeated_signature_turns": self.repeated_signature_turns,
            "completion_rejections": self.completion_rejections,
            "verify_failures": self.verify_failures,
            "self_check_failures": self.self_check_failures,
            "last_progress_reason": self.last_progress_reason,
            "last_stop_reason": self.last_stop_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ConvergenceStateV1:
        return cls(
            no_progress_turns=int(value.get("no_progress_turns", 0)),
            repeated_signature_turns=int(value.get("repeated_signature_turns", 0)),
            completion_rejections=int(value.get("completion_rejections", 0)),
            verify_failures=int(value.get("verify_failures", 0)),
            self_check_failures=int(value.get("self_check_failures", 0)),
            last_progress_reason=(
                str(value["last_progress_reason"])
                if value.get("last_progress_reason") is not None
                else None
            ),
            last_stop_reason=(
                str(value["last_stop_reason"])
                if value.get("last_stop_reason") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ProposalErrorV1:
    """Error information from proposal execution."""

    code: str
    message_ref: str
    retryable: bool = False
    details: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code or not self.message_ref:
            raise ValueError("proposal error code and message_ref are required")
        object.__setattr__(self, "details", _json_object(self.details))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "message_ref": self.message_ref,
            "retryable": self.retryable,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProposalErrorV1:
        return cls(
            code=str(value["code"]),
            message_ref=str(value["message_ref"]),
            retryable=bool(value.get("retryable", False)),
            details=dict(value.get("details", {})),
        )


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """Prepared tool call with stable identifier."""

    stable_call_id: str
    tool_name: str
    arguments: Mapping[str, JsonValue]
    raw_arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.stable_call_id or not self.tool_name:
            raise ValueError("stable_call_id and tool_name are required")
        object.__setattr__(self, "arguments", _json_object(self.arguments))
        object.__setattr__(self, "raw_arguments", _json_object(self.raw_arguments))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "stable_call_id": self.stable_call_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "raw_arguments": dict(self.raw_arguments),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PreparedToolCall:
        return cls(
            stable_call_id=str(value["stable_call_id"]),
            tool_name=str(value["tool_name"]),
            arguments=dict(value.get("arguments", {})),
            raw_arguments=dict(value.get("raw_arguments", {})),
        )


@dataclass(frozen=True, slots=True)
class ProposalStateV1:
    """Complete state for multi-turn proposal execution."""

    messages: Sequence[Mapping[str, JsonValue]]
    original_request: str
    request_id: str
    turn_id: str
    system_prompt_ref: str | None
    prompt_ref: str | None
    skill_refs: Sequence[str]
    compaction_summary: str | None
    compaction_ref: str | None
    token_estimate: int
    iteration: int
    proposal_turns_used: int
    fix_rounds_used: int
    tools_used: int
    active_plan_id: str | None
    active_step_id: str | None
    active_todo_ids: Sequence[str]
    tool_signature_repeat_window: Sequence[str]
    completion_attempts: int
    verify_attempts: int
    self_check_attempts: int
    completion_outcomes: Sequence[Mapping[str, JsonValue]]
    verify_outcomes: Sequence[Mapping[str, JsonValue]]
    self_check_outcomes: Sequence[Mapping[str, JsonValue]]
    evidence_refs: Sequence[str]
    provider_snapshot: Mapping[str, JsonValue]
    model_snapshot: Mapping[str, JsonValue]
    fallback_attempts: Sequence[Mapping[str, JsonValue]]
    last_error: ProposalErrorV1 | None
    pending_tool_results: Mapping[str, JsonValue]
    committed_tool_results: Mapping[str, JsonValue]
    gate_config: GateConfigV1
    gate_state: GateStateV1
    convergence: ConvergenceStateV1
    schema_version: int = PROPOSAL_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROPOSAL_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported proposal state schema version")
        counters = (
            self.token_estimate,
            self.iteration,
            self.proposal_turns_used,
            self.fix_rounds_used,
            self.tools_used,
            self.completion_attempts,
            self.verify_attempts,
            self.self_check_attempts,
        )
        if any(value < 0 for value in counters):
            raise ValueError("proposal state counters must not be negative")
        object.__setattr__(self, "messages", _json_objects(self.messages))
        object.__setattr__(self, "skill_refs", tuple(str(value) for value in self.skill_refs))
        object.__setattr__(
            self,
            "active_todo_ids",
            tuple(str(value) for value in self.active_todo_ids),
        )
        object.__setattr__(
            self,
            "tool_signature_repeat_window",
            tuple(str(value) for value in self.tool_signature_repeat_window),
        )
        object.__setattr__(self, "completion_outcomes", _json_objects(self.completion_outcomes))
        object.__setattr__(self, "verify_outcomes", _json_objects(self.verify_outcomes))
        object.__setattr__(self, "self_check_outcomes", _json_objects(self.self_check_outcomes))
        object.__setattr__(self, "evidence_refs", tuple(str(value) for value in self.evidence_refs))
        object.__setattr__(self, "provider_snapshot", _json_object(self.provider_snapshot))
        object.__setattr__(self, "model_snapshot", _json_object(self.model_snapshot))
        object.__setattr__(self, "fallback_attempts", _json_objects(self.fallback_attempts))
        object.__setattr__(self, "pending_tool_results", _json_object(self.pending_tool_results))
        object.__setattr__(
            self, "committed_tool_results", _json_object(self.committed_tool_results)
        )

    def to_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "messages": [dict(value) for value in self.messages],
            "original_request": self.original_request,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "system_prompt_ref": self.system_prompt_ref,
            "prompt_ref": self.prompt_ref,
            "skill_refs": list(self.skill_refs),
            "compaction_summary": self.compaction_summary,
            "compaction_ref": self.compaction_ref,
            "token_estimate": self.token_estimate,
            "iteration": self.iteration,
            "proposal_turns_used": self.proposal_turns_used,
            "fix_rounds_used": self.fix_rounds_used,
            "tools_used": self.tools_used,
            "active_plan_id": self.active_plan_id,
            "active_step_id": self.active_step_id,
            "active_todo_ids": list(self.active_todo_ids),
            "tool_signature_repeat_window": list(self.tool_signature_repeat_window),
            "completion_attempts": self.completion_attempts,
            "verify_attempts": self.verify_attempts,
            "self_check_attempts": self.self_check_attempts,
            "completion_outcomes": [dict(value) for value in self.completion_outcomes],
            "verify_outcomes": [dict(value) for value in self.verify_outcomes],
            "self_check_outcomes": [dict(value) for value in self.self_check_outcomes],
            "evidence_refs": list(self.evidence_refs),
            "provider_snapshot": dict(self.provider_snapshot),
            "model_snapshot": dict(self.model_snapshot),
            "fallback_attempts": [dict(value) for value in self.fallback_attempts],
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "pending_tool_results": dict(self.pending_tool_results),
            "committed_tool_results": dict(self.committed_tool_results),
            "gate_config": self.gate_config.to_dict(),
            "gate_state": self.gate_state.to_dict(),
            "convergence": self.convergence.to_dict(),
        }
        validate_json_value(payload)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProposalStateV1:
        raw_error = value.get("last_error")
        return cls(
            schema_version=int(value.get("schema_version", PROPOSAL_STATE_SCHEMA_VERSION)),
            messages=list(value.get("messages", [])),
            original_request=str(value.get("original_request", "")),
            request_id=str(value.get("request_id", "")),
            turn_id=str(value.get("turn_id", "")),
            system_prompt_ref=(
                str(value["system_prompt_ref"])
                if value.get("system_prompt_ref") is not None
                else None
            ),
            prompt_ref=(str(value["prompt_ref"]) if value.get("prompt_ref") is not None else None),
            skill_refs=list(value.get("skill_refs", [])),
            compaction_summary=(
                str(value["compaction_summary"])
                if value.get("compaction_summary") is not None
                else None
            ),
            compaction_ref=(
                str(value["compaction_ref"]) if value.get("compaction_ref") is not None else None
            ),
            token_estimate=int(value.get("token_estimate", 0)),
            iteration=int(value.get("iteration", 0)),
            proposal_turns_used=int(value.get("proposal_turns_used", 0)),
            fix_rounds_used=int(value.get("fix_rounds_used", 0)),
            tools_used=int(value.get("tools_used", 0)),
            active_plan_id=(
                str(value["active_plan_id"]) if value.get("active_plan_id") is not None else None
            ),
            active_step_id=(
                str(value["active_step_id"]) if value.get("active_step_id") is not None else None
            ),
            active_todo_ids=list(value.get("active_todo_ids", [])),
            tool_signature_repeat_window=list(value.get("tool_signature_repeat_window", [])),
            completion_attempts=int(value.get("completion_attempts", 0)),
            verify_attempts=int(value.get("verify_attempts", 0)),
            self_check_attempts=int(value.get("self_check_attempts", 0)),
            completion_outcomes=list(value.get("completion_outcomes", [])),
            verify_outcomes=list(value.get("verify_outcomes", [])),
            self_check_outcomes=list(value.get("self_check_outcomes", [])),
            evidence_refs=list(value.get("evidence_refs", [])),
            provider_snapshot=dict(value.get("provider_snapshot", {})),
            model_snapshot=dict(value.get("model_snapshot", {})),
            fallback_attempts=list(value.get("fallback_attempts", [])),
            last_error=(
                ProposalErrorV1.from_dict(raw_error) if isinstance(raw_error, Mapping) else None
            ),
            pending_tool_results=dict(value.get("pending_tool_results", {})),
            committed_tool_results=dict(value.get("committed_tool_results", {})),
            gate_config=GateConfigV1.from_dict(dict(value.get("gate_config", {}))),
            gate_state=GateStateV1.from_dict(dict(value.get("gate_state", {}))),
            convergence=ConvergenceStateV1.from_dict(dict(value.get("convergence", {}))),
        )


@dataclass(frozen=True, slots=True)
class ProposalOutcomeV1:
    """Outcome from a single proposal generation."""

    assistant_content: str
    reasoning_summary_ref: str | None
    raw_tool_proposals: Sequence[Mapping[str, JsonValue]]
    prepared_calls: Sequence[PreparedToolCall]
    stop_reason: str
    usage: Mapping[str, JsonValue]
    provider: str
    model: str
    compacted_messages: Sequence[Mapping[str, JsonValue]] | None = None
    compaction_summary: str | None = None
    compaction_ref: str | None = None
    token_estimate: int = 0
    gate_request: Mapping[str, JsonValue] | None = None
    error: ProposalErrorV1 | None = None
    schema_version: int = PROPOSAL_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROPOSAL_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported proposal outcome schema version")
        calls = tuple(self.prepared_calls)
        call_ids = [call.stable_call_id for call in calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("proposal contains duplicate stable call ids")
        object.__setattr__(self, "raw_tool_proposals", _json_objects(self.raw_tool_proposals))
        object.__setattr__(self, "prepared_calls", calls)
        object.__setattr__(self, "usage", _json_object(self.usage))
        if self.token_estimate < 0:
            raise ValueError("proposal outcome token estimate must not be negative")
        object.__setattr__(
            self,
            "compacted_messages",
            (
                _json_objects(self.compacted_messages)
                if self.compacted_messages is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "gate_request",
            _json_object(self.gate_request) if self.gate_request is not None else None,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "assistant_content": self.assistant_content,
            "reasoning_summary_ref": self.reasoning_summary_ref,
            "raw_tool_proposals": [dict(value) for value in self.raw_tool_proposals],
            "prepared_calls": [call.to_dict() for call in self.prepared_calls],
            "stop_reason": self.stop_reason,
            "usage": dict(self.usage),
            "provider": self.provider,
            "model": self.model,
            "compacted_messages": (
                [dict(value) for value in self.compacted_messages]
                if self.compacted_messages is not None
                else None
            ),
            "compaction_summary": self.compaction_summary,
            "compaction_ref": self.compaction_ref,
            "token_estimate": self.token_estimate,
            "gate_request": (dict(self.gate_request) if self.gate_request is not None else None),
            "error": self.error.to_dict() if self.error else None,
        }
        validate_json_value(payload)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProposalOutcomeV1:
        raw_error = value.get("error")
        return cls(
            schema_version=int(value.get("schema_version", PROPOSAL_STATE_SCHEMA_VERSION)),
            assistant_content=str(value.get("assistant_content", "")),
            reasoning_summary_ref=(
                str(value["reasoning_summary_ref"])
                if value.get("reasoning_summary_ref") is not None
                else None
            ),
            raw_tool_proposals=list(value.get("raw_tool_proposals", [])),
            prepared_calls=[
                PreparedToolCall.from_dict(item) for item in value.get("prepared_calls", [])
            ],
            stop_reason=str(value.get("stop_reason", "unknown")),
            usage=dict(value.get("usage", {})),
            provider=str(value.get("provider", "")),
            model=str(value.get("model", "")),
            compacted_messages=(
                list(value["compacted_messages"])
                if isinstance(value.get("compacted_messages"), (list, tuple))
                else None
            ),
            compaction_summary=(
                str(value["compaction_summary"])
                if value.get("compaction_summary") is not None
                else None
            ),
            compaction_ref=(
                str(value["compaction_ref"]) if value.get("compaction_ref") is not None else None
            ),
            token_estimate=int(value.get("token_estimate", 0)),
            gate_request=(
                dict(value["gate_request"])
                if isinstance(value.get("gate_request"), Mapping)
                else None
            ),
            error=(
                ProposalErrorV1.from_dict(raw_error) if isinstance(raw_error, Mapping) else None
            ),
        )


def derive_stable_call_id(
    *,
    checkpoint_key: str,
    iteration: int,
    index: int,
    tool_name: str,
    raw_args: Mapping[str, JsonValue],
) -> str:
    """Derive the call id before preparation from durable proposal inputs."""

    payload: dict[str, JsonValue] = {
        "checkpoint_key": checkpoint_key,
        "iteration": iteration,
        "index": index,
        "tool_name": tool_name,
        "raw_args": _json_object(raw_args),
    }
    digest = hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()
    return f"call-{digest[:32]}"


__all__ = [
    "ConvergenceStateV1",
    "GateConfigV1",
    "GateStateV1",
    "PROPOSAL_STATE_SCHEMA_VERSION",
    "PreparedToolCall",
    "ProposalErrorV1",
    "ProposalOutcomeV1",
    "ProposalStateV1",
    "derive_stable_call_id",
]

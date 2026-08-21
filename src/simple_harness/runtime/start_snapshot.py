# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable root-Run start request and durable snapshot binding."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from simple_harness.contracts import (
    ExecutionSessionId,
    FrozenJsonValue,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)
from simple_harness.workflow.execution_ports import (
    StartAdmissionRequest,
    start_admission_request_from_json,
    start_admission_request_to_json,
)

from .conversation_memory import ContextPreparationMode, ConversationTurnInput


@dataclass(frozen=True, slots=True)
class RunStart:
    execution_session_id: ExecutionSessionId
    run_id: RunId
    request_id: RequestId
    turn_id: str
    input: Mapping[str, JsonValue]
    tool_catalog_generation: int
    tool_catalog_fingerprint: str | None = None
    provider_budget_fingerprint: str | None = None
    conversation: ConversationTurnInput | None = None
    context_preparation_mode: ContextPreparationMode | None = None
    context_stage_id: str | None = None
    context_stage_hash: str | None = None
    prepared_context: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution_session_id, ExecutionSessionId):
            raise TypeError("execution_session_id must use ExecutionSessionId")
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        if not isinstance(self.request_id, RequestId):
            raise TypeError("request_id must use RequestId")
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise ValueError("turn_id is required")
        if not isinstance(self.input, Mapping):
            raise TypeError("input must be a JSON object")
        frozen = freeze_json(dict(self.input))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "input", frozen)
        if (
            isinstance(self.tool_catalog_generation, bool)
            or not isinstance(self.tool_catalog_generation, int)
            or self.tool_catalog_generation < 1
        ):
            raise ValueError("tool_catalog_generation must be a positive integer")
        for name in ("tool_catalog_fingerprint", "provider_budget_fingerprint"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or len(value) != 64
            ):
                raise ValueError(f"{name} must be a SHA-256 digest or None")
        if self.conversation is not None:
            if not isinstance(self.conversation, ConversationTurnInput):
                raise TypeError("conversation must use ConversationTurnInput")
            if self.conversation.session_id != self.execution_session_id.value:
                raise ValueError("conversation session differs from RunStart")
        mode = self.context_preparation_mode
        if mode is not None:
            mode = ContextPreparationMode(mode)
            object.__setattr__(self, "context_preparation_mode", mode)
        stage_values = (
            self.context_stage_id,
            self.context_stage_hash,
            self.prepared_context,
        )
        if any(value is not None for value in stage_values) and not all(
            value is not None for value in stage_values
        ):
            raise ValueError("context stage id/hash/private snapshot travel together")
        if mode is not None and self.conversation is None:
            raise ValueError("context preparation requires conversation envelope")
        if mode is not None and self.context_stage_id is None:
            raise ValueError("context preparation mode requires a durable stage")
        if self.context_stage_id is not None and mode is None:
            raise ValueError("durable context stage requires preparation mode")
        if self.context_stage_id is not None and self.conversation is None:
            raise ValueError("durable context stage requires conversation envelope")
        if self.prepared_context is not None:
            frozen_context = freeze_json(dict(self.prepared_context))
            assert isinstance(frozen_context, Mapping)
            expected_hash = hashlib.sha256(
                canonical_json(
                    thaw_json(cast(FrozenJsonValue, frozen_context))
                ).encode("utf-8")
            ).hexdigest()
            if self.context_stage_hash != expected_hash:
                raise ValueError("prepared context differs from context stage hash")
            object.__setattr__(self, "prepared_context", frozen_context)


@dataclass(frozen=True, slots=True)
class StartSnapshot:
    profile_key: str
    driver_kind: str
    turn_id: str
    tool_catalog_generation: int
    input: FrozenJsonValue
    workflow_admission: StartAdmissionRequest | None = None
    policy_fingerprint: str | None = None
    tool_catalog_fingerprint: str | None = None
    provider_budget_fingerprint: str | None = None
    conversation: ConversationTurnInput | None = None
    context_preparation_mode: ContextPreparationMode | None = None
    context_stage_id: str | None = None
    context_stage_hash: str | None = None
    prepared_context: FrozenJsonValue | None = None

    def __post_init__(self) -> None:
        if self.driver_kind == "workflow":
            if self.workflow_admission is None:
                raise ValueError("workflow start snapshot requires workflow admission")
        elif self.workflow_admission is not None:
            raise ValueError("non-workflow start snapshot rejects workflow admission")
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise ValueError("start snapshot turn_id is required")
        if self.policy_fingerprint is not None and (
            not isinstance(self.policy_fingerprint, str)
            or not self.policy_fingerprint.strip()
        ):
            raise ValueError("policy_fingerprint must be a non-empty string or None")
        for name in ("tool_catalog_fingerprint", "provider_budget_fingerprint"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or len(value) != 64
            ):
                raise ValueError(f"{name} must be a SHA-256 digest or None")
        if self.conversation is not None and not isinstance(
            self.conversation, ConversationTurnInput
        ):
            raise TypeError("conversation must use ConversationTurnInput")
        if self.context_preparation_mode is not None:
            object.__setattr__(
                self,
                "context_preparation_mode",
                ContextPreparationMode(self.context_preparation_mode),
            )
        stage_values = (
            self.context_stage_id,
            self.context_stage_hash,
            self.prepared_context,
        )
        if any(value is not None for value in stage_values) and not all(
            value is not None for value in stage_values
        ):
            raise ValueError("snapshot context stage fields travel together")
        if self.context_preparation_mode is not None and self.conversation is None:
            raise ValueError("context preparation requires conversation envelope")
        if self.context_preparation_mode is not None and self.context_stage_id is None:
            raise ValueError("context preparation mode requires a durable stage")
        if self.context_stage_id is not None and self.context_preparation_mode is None:
            raise ValueError("durable context stage requires preparation mode")
        if self.prepared_context is not None:
            context = thaw_json(self.prepared_context)
            expected_hash = hashlib.sha256(
                canonical_json(context).encode("utf-8")
            ).hexdigest()
            if self.context_stage_hash != expected_hash:
                raise ValueError("snapshot prepared context hash differs")

    def to_json(self) -> dict[str, JsonValue]:
        value = thaw_json(self.input)
        if not isinstance(value, dict):
            raise TypeError("start input must remain a JSON object")
        return {
            "schema_version": 5,
            "profile_key": self.profile_key,
            "driver_kind": self.driver_kind,
            "turn_id": self.turn_id,
            "tool_catalog_generation": self.tool_catalog_generation,
            "input": value,
            "policy_fingerprint": self.policy_fingerprint,
            "tool_catalog_fingerprint": self.tool_catalog_fingerprint,
            "provider_budget_fingerprint": self.provider_budget_fingerprint,
            "conversation": (
                None if self.conversation is None else self.conversation.to_json()
            ),
            "context_preparation_mode": (
                None
                if self.context_preparation_mode is None
                else self.context_preparation_mode.value
            ),
            "context_stage_id": self.context_stage_id,
            "context_stage_hash": self.context_stage_hash,
            "prepared_context": (
                None
                if self.prepared_context is None
                else thaw_json(self.prepared_context)
            ),
            "workflow_admission": (
                None
                if self.workflow_admission is None
                else start_admission_request_to_json(self.workflow_admission)
            ),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> StartSnapshot:
        schema_version = value.get("schema_version")
        legacy_workflow_snapshot = (
            value.get("driver_kind") == "workflow"
            and "start_input" in value
            and "workflow_name" in value
        )
        if not legacy_workflow_snapshot and schema_version not in {1, 2, 3, 4, 5}:
            raise ValueError("unsupported start snapshot schema")
        profile_key = value.get("profile_key")
        driver_kind = value.get("driver_kind")
        generation = value.get("tool_catalog_generation")
        turn_id = value.get("turn_id")
        start_input = (
            value.get("start_input")
            if legacy_workflow_snapshot
            else value.get("input")
        )
        if not isinstance(profile_key, str) or not profile_key.strip():
            raise ValueError("start snapshot profile_key is required")
        if not isinstance(driver_kind, str) or not driver_kind.strip():
            raise ValueError("start snapshot driver_kind is required")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("start snapshot turn_id is required")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ValueError("start snapshot tool catalog generation is invalid")
        if not isinstance(start_input, dict):
            raise TypeError("start snapshot input must be a JSON object")
        workflow_admission_value = value.get("workflow_admission")
        if legacy_workflow_snapshot:
            workflow_admission_value = dict(value)
        if workflow_admission_value is not None and not isinstance(
            workflow_admission_value, dict
        ):
            raise TypeError("workflow_admission must be a JSON object or null")
        admission = (
            None
            if workflow_admission_value is None
            else start_admission_request_from_json(workflow_admission_value)
        )
        conversation_value = value.get("conversation") if schema_version == 5 else None
        if conversation_value is not None and not isinstance(conversation_value, dict):
            raise TypeError("conversation must be an object or null")
        mode_value = (
            value.get("context_preparation_mode") if schema_version == 5 else None
        )
        if mode_value is not None and not isinstance(mode_value, str):
            raise TypeError("context_preparation_mode must be a string or null")
        prepared_value = (
            value.get("prepared_context") if schema_version == 5 else None
        )
        if prepared_value is not None and not isinstance(prepared_value, dict):
            raise TypeError("prepared_context must be an object or null")
        return cls(
            profile_key=profile_key,
            driver_kind=driver_kind,
            turn_id=turn_id,
            tool_catalog_generation=generation,
            input=freeze_json(start_input),
            workflow_admission=admission,
            policy_fingerprint=(
                _optional_string(value.get("policy_fingerprint"), "policy_fingerprint")
                if schema_version in {3, 4, 5}
                else None
            ),
            tool_catalog_fingerprint=(
                _optional_string(
                    value.get("tool_catalog_fingerprint"),
                    "tool_catalog_fingerprint",
                )
                if schema_version in {4, 5}
                else None
            ),
            provider_budget_fingerprint=(
                _optional_string(
                    value.get("provider_budget_fingerprint"),
                    "provider_budget_fingerprint",
                )
                if schema_version in {4, 5}
                else None
            ),
            conversation=(
                ConversationTurnInput.from_json(conversation_value)
                if isinstance(conversation_value, dict)
                else None
            ),
            context_preparation_mode=(
                ContextPreparationMode(mode_value)
                if isinstance(mode_value, str)
                else None
            ),
            context_stage_id=(
                _optional_string(value.get("context_stage_id"), "context_stage_id")
                if schema_version == 5
                else None
            ),
            context_stage_hash=(
                _optional_string(
                    value.get("context_stage_hash"), "context_stage_hash"
                )
                if schema_version == 5
                else None
            ),
            prepared_context=(
                freeze_json(prepared_value)
                if isinstance(prepared_value, dict)
                else None
            ),
        )


def bind_start_snapshot(
    start: RunStart,
    *,
    profile_key: str,
    driver_kind: str,
    workflow_admission: StartAdmissionRequest | None = None,
    policy_fingerprint: str | None = None,
) -> StartSnapshot:
    input_value = thaw_json(cast(FrozenJsonValue, start.input))
    if not isinstance(input_value, dict):
        raise TypeError("start input must remain a JSON object")
    return StartSnapshot(
        profile_key=profile_key,
        driver_kind=driver_kind,
        turn_id=start.turn_id,
        tool_catalog_generation=start.tool_catalog_generation,
        input=freeze_json(input_value),
        workflow_admission=workflow_admission,
        policy_fingerprint=policy_fingerprint,
        tool_catalog_fingerprint=start.tool_catalog_fingerprint,
        provider_budget_fingerprint=start.provider_budget_fingerprint,
        conversation=start.conversation,
        context_preparation_mode=start.context_preparation_mode,
        context_stage_id=start.context_stage_id,
        context_stage_hash=start.context_stage_hash,
        prepared_context=(
            None
            if start.prepared_context is None
            else freeze_json(
                thaw_json(cast(FrozenJsonValue, start.prepared_context))
            )
        ),
    )


def _optional_string(value: JsonValue | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or null")
    return value


__all__ = ("RunStart", "StartSnapshot", "bind_start_snapshot")

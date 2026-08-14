# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable root-Run start request and durable snapshot binding."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from simple_harness.contracts import (
    ExecutionSessionId,
    FrozenJsonValue,
    JsonValue,
    RequestId,
    RunId,
    freeze_json,
    thaw_json,
)
from simple_harness.workflow.execution_ports import (
    StartAdmissionRequest,
    start_admission_request_from_json,
    start_admission_request_to_json,
)


@dataclass(frozen=True, slots=True)
class RunStart:
    execution_session_id: ExecutionSessionId
    run_id: RunId
    request_id: RequestId
    turn_id: str
    input: Mapping[str, JsonValue]
    tool_catalog_generation: int

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


@dataclass(frozen=True, slots=True)
class StartSnapshot:
    profile_key: str
    driver_kind: str
    turn_id: str
    tool_catalog_generation: int
    input: FrozenJsonValue
    workflow_admission: StartAdmissionRequest | None = None

    def __post_init__(self) -> None:
        if self.driver_kind == "workflow":
            if self.workflow_admission is None:
                raise ValueError("workflow start snapshot requires workflow admission")
        elif self.workflow_admission is not None:
            raise ValueError("non-workflow start snapshot rejects workflow admission")
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise ValueError("start snapshot turn_id is required")

    def to_json(self) -> dict[str, JsonValue]:
        value = thaw_json(self.input)
        if not isinstance(value, dict):
            raise TypeError("start input must remain a JSON object")
        return {
            "schema_version": 2,
            "profile_key": self.profile_key,
            "driver_kind": self.driver_kind,
            "turn_id": self.turn_id,
            "tool_catalog_generation": self.tool_catalog_generation,
            "input": value,
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
        if not legacy_workflow_snapshot and schema_version not in {1, 2}:
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
        return cls(
            profile_key=profile_key,
            driver_kind=driver_kind,
            turn_id=turn_id,
            tool_catalog_generation=generation,
            input=freeze_json(start_input),
            workflow_admission=admission,
        )


def bind_start_snapshot(
    start: RunStart,
    *,
    profile_key: str,
    driver_kind: str,
    workflow_admission: StartAdmissionRequest | None = None,
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
    )


__all__ = ("RunStart", "StartSnapshot", "bind_start_snapshot")

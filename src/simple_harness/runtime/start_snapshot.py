# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable root-Run start request and durable snapshot binding."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from simple_harness.contracts import (
    ExecutionSessionId,
    FrozenJsonValue,
    JsonValue,
    RequestId,
    RunId,
    freeze_json,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class RunStart:
    execution_session_id: ExecutionSessionId
    run_id: RunId
    request_id: RequestId
    input: Mapping[str, JsonValue]
    tool_catalog_generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.execution_session_id, ExecutionSessionId):
            raise TypeError("execution_session_id must use ExecutionSessionId")
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        if not isinstance(self.request_id, RequestId):
            raise TypeError("request_id must use RequestId")
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
    tool_catalog_generation: int
    input: FrozenJsonValue

    def to_json(self) -> dict[str, JsonValue]:
        value = thaw_json(self.input)
        if not isinstance(value, dict):
            raise TypeError("start input must remain a JSON object")
        return {
            "schema_version": 1,
            "profile_key": self.profile_key,
            "driver_kind": self.driver_kind,
            "tool_catalog_generation": self.tool_catalog_generation,
            "input": value,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> StartSnapshot:
        if value.get("schema_version") != 1:
            raise ValueError("unsupported start snapshot schema")
        profile_key = value.get("profile_key")
        driver_kind = value.get("driver_kind")
        generation = value.get("tool_catalog_generation")
        start_input = value.get("input")
        if not isinstance(profile_key, str) or not profile_key.strip():
            raise ValueError("start snapshot profile_key is required")
        if not isinstance(driver_kind, str) or not driver_kind.strip():
            raise ValueError("start snapshot driver_kind is required")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ValueError("start snapshot tool catalog generation is invalid")
        if not isinstance(start_input, dict):
            raise TypeError("start snapshot input must be a JSON object")
        return cls(
            profile_key=profile_key,
            driver_kind=driver_kind,
            tool_catalog_generation=generation,
            input=freeze_json(start_input),
        )


def bind_start_snapshot(
    start: RunStart, *, profile_key: str, driver_kind: str
) -> StartSnapshot:
    return StartSnapshot(
        profile_key=profile_key,
        driver_kind=driver_kind,
        tool_catalog_generation=start.tool_catalog_generation,
        input=freeze_json(dict(start.input)),
    )


__all__ = ("RunStart", "StartSnapshot", "bind_start_snapshot")

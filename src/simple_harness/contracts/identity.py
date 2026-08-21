# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Distinct correlation identities for durable execution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

from .errors import ContractValidationError, ErrorCode
from .json import JsonValue

_IDENTIFIER = re.compile(r"[!-~]{1,255}\Z")


@dataclass(frozen=True, slots=True)
class _Identifier:
    value: str
    _kind: ClassVar[str] = "identifier"

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _IDENTIFIER.fullmatch(self.value):
            raise ContractValidationError(
                ErrorCode.INVALID_IDENTIFIER,
                f"{self._kind} must be 1-255 printable ASCII characters",
            )

    def __str__(self) -> str:
        return self.value

    def to_json(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExecutionSessionId(_Identifier):
    """SDK execution isolation identity, not a product chat session."""

    _kind: ClassVar[str] = "execution_session_id"


@dataclass(frozen=True, slots=True)
class RunId(_Identifier):
    _kind: ClassVar[str] = "run_id"


@dataclass(frozen=True, slots=True)
class RequestId(_Identifier):
    _kind: ClassVar[str] = "request_id"


@dataclass(frozen=True, slots=True)
class CallId(_Identifier):
    _kind: ClassVar[str] = "call_id"


@dataclass(frozen=True, slots=True)
class EffectId(_Identifier):
    _kind: ClassVar[str] = "effect_id"


@dataclass(frozen=True, slots=True)
class EventId(_Identifier):
    _kind: ClassVar[str] = "event_id"


@dataclass(frozen=True, slots=True)
class CorrelationIds:
    execution_session_id: ExecutionSessionId
    run_id: RunId
    request_id: RequestId
    call_id: CallId | None = None
    effect_id: EffectId | None = None

    def __post_init__(self) -> None:
        required = (
            ("execution_session_id", self.execution_session_id, ExecutionSessionId),
            ("run_id", self.run_id, RunId),
            ("request_id", self.request_id, RequestId),
        )
        for name, value, expected_type in required:
            if not isinstance(value, expected_type):
                raise ContractValidationError(
                    ErrorCode.INVALID_IDENTIFIER,
                    f"{name} must use {expected_type.__name__}",
                )
        if self.call_id is not None and not isinstance(self.call_id, CallId):
            raise ContractValidationError(ErrorCode.INVALID_IDENTIFIER, "call_id must use CallId")
        if self.effect_id is not None and not isinstance(self.effect_id, EffectId):
            raise ContractValidationError(
                ErrorCode.INVALID_IDENTIFIER, "effect_id must use EffectId"
            )
        if self.effect_id is not None and self.call_id is None:
            raise ContractValidationError(
                ErrorCode.INVALID_IDENTIFIER, "effect_id requires call_id"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "execution_session_id": self.execution_session_id.value,
            "run_id": self.run_id.value,
            "request_id": self.request_id.value,
        }
        if self.call_id is not None:
            result["call_id"] = self.call_id.value
        if self.effect_id is not None:
            result["effect_id"] = self.effect_id.value
        return result


__all__ = (
    "ExecutionSessionId",
    "RunId",
    "RequestId",
    "CallId",
    "EffectId",
    "EventId",
    "CorrelationIds",
)

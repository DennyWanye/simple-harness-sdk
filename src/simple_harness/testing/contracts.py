# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public, host-neutral contracts for executable SDK conformance suites."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeAlias

from simple_harness.contracts import JsonValue

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


class CaseStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


class ConformanceCaseUnavailable(RuntimeError):
    """Raised by a Host operation when a required physical seam is unavailable."""


@dataclass(frozen=True, slots=True)
class CaseDefinition:
    case_id: str
    capability: str
    description: str
    operation: str
    required: bool = True

    def __post_init__(self) -> None:
        for name in ("case_id", "capability", "description", "operation"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if not self.required:
            raise ValueError("protocol v1 conformance cases are all required")


@dataclass(frozen=True, slots=True)
class ConformanceHostMetadata:
    protocol_version: str
    host_name: str
    host_version: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        for name in ("protocol_version", "host_name", "host_version"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if not _VERSION.fullmatch(self.protocol_version):
            raise ValueError("protocol_version must be MAJOR.MINOR.PATCH")
        capabilities = frozenset(_required(item, "capability") for item in self.capabilities)
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True, slots=True)
class CaseObservation:
    """Raw typed-operation observation consumed by an SDK-owned verifier.

    Hosts expose physical operations and observations, never a conformance
    verdict.  Only the SDK maps these facts to PASS/FAIL/ERROR.
    """

    case_id: str
    values: Mapping[str, JsonValue]
    evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _required(self.case_id, "case_id"))
        if not isinstance(self.values, Mapping):
            raise TypeError("values must be a mapping")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        if self.evidence is None:
            object.__setattr__(self, "evidence", MappingProxyType({}))
        elif not isinstance(self.evidence, Mapping):
            raise TypeError("evidence must be a mapping")


@dataclass(frozen=True, slots=True)
class ConformanceCaseResult:
    suite: str
    case_id: str
    status: CaseStatus
    required: bool
    duration_seconds: float
    message: str | None
    evidence: Mapping[str, JsonValue]

    @property
    def passed(self) -> bool:
        return self.status is CaseStatus.PASS

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "suite": self.suite,
            "case_id": self.case_id,
            "status": self.status.value,
            "required": self.required,
            "duration_seconds": self.duration_seconds,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class ConformanceError:
    code: str
    message: str
    suite: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required(self.code, "code"))
        object.__setattr__(self, "message", _required(self.message, "message"))

    def to_json(self) -> dict[str, JsonValue]:
        return {"code": self.code, "message": self.message, "suite": self.suite}


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    protocol_version: str
    sdk_version: str
    host_name: str
    host_version: str
    platform: str
    python_version: str
    artifact_sha256: str
    suites: tuple[str, ...]
    cases: tuple[ConformanceCaseResult, ...]
    errors: tuple[ConformanceError, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            not self.errors
            and bool(self.cases)
            and all(not case.required or case.passed for case in self.cases)
        )

    @property
    def status(self) -> str:
        return "pass" if self.passed else "fail"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": self.protocol_version,
            "sdk_version": self.sdk_version,
            "host": {"name": self.host_name, "version": self.host_version},
            "platform": self.platform,
            "python_version": self.python_version,
            "artifact_sha256": self.artifact_sha256,
            "suites": list(self.suites),
            "status": self.status,
            "cases": [case.to_json() for case in self.cases],
            "errors": [error.to_json() for error in self.errors],
        }


class ClosableConformanceSuite(Protocol):
    async def aclose(self) -> None: ...


class ProviderConformanceSuite(ClosableConformanceSuite, Protocol):
    async def physical_request(self) -> CaseObservation: ...
    async def typed_error(self) -> CaseObservation: ...
    async def usage(self) -> CaseObservation: ...
    async def redaction(self) -> CaseObservation: ...


class ToolConformanceSuite(ClosableConformanceSuite, Protocol):
    async def schema(self) -> CaseObservation: ...
    async def five_state(self) -> CaseObservation: ...
    async def reconcile(self) -> CaseObservation: ...
    async def malformed_duplicate_late(self) -> CaseObservation: ...


class RuntimeConformanceSuite(ClosableConformanceSuite, Protocol):
    async def no_tool(self) -> CaseObservation: ...
    async def one_tool(self) -> CaseObservation: ...
    async def multi_turn_tool(self) -> CaseObservation: ...
    async def session_persistence(self) -> CaseObservation: ...
    async def hitl(self) -> CaseObservation: ...
    async def delivery(self) -> CaseObservation: ...
    async def budget(self) -> CaseObservation: ...
    async def restart_without_replay(self) -> CaseObservation: ...


class WorkflowConformanceSuite(ClosableConformanceSuite, Protocol):
    async def host_owned(self) -> CaseObservation: ...
    async def official_durable_task(self) -> CaseObservation: ...
    async def official_personal_v1(self) -> CaseObservation: ...
    async def official_capability_build(self) -> CaseObservation: ...
    async def ticket_fingerprint(self) -> CaseObservation: ...
    async def reopen(self) -> CaseObservation: ...


class ConversationConformanceSuite(ClosableConformanceSuite, Protocol):
    async def conversation_contract(self) -> CaseObservation: ...
    async def conversation_schema_identity(self) -> CaseObservation: ...
    async def conversation_outbox_recovery(self) -> CaseObservation: ...


ConformanceSuite: TypeAlias = (
    ProviderConformanceSuite
    | ToolConformanceSuite
    | RuntimeConformanceSuite
    | WorkflowConformanceSuite
    | ConversationConformanceSuite
)


class ConformanceHost(Protocol):
    @property
    def metadata(self) -> ConformanceHostMetadata: ...

    def open_suite(self, name: str) -> AbstractAsyncContextManager[ConformanceSuite]: ...


__all__ = (
    "CaseDefinition",
    "CaseObservation",
    "CaseStatus",
    "ConformanceCaseUnavailable",
    "ClosableConformanceSuite",
    "ConformanceCaseResult",
    "ConversationConformanceSuite",
    "ConformanceError",
    "ConformanceHost",
    "ConformanceHostMetadata",
    "ConformanceReport",
    "ConformanceSuite",
    "ProviderConformanceSuite",
    "RuntimeConformanceSuite",
    "ToolConformanceSuite",
    "WorkflowConformanceSuite",
)

# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable catalog and Provider projection cursor contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    Message,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)
from simple_harness.providers import ProviderToolSpec
from simple_harness.runtime.task_scope_protocol import TaskScopeRoute
from simple_harness.tools.runtime_catalog import ToolExecutionPolicy

from .effects import TaskExecutionEnvelope


def _sha256(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip() or "\x00" in value:
        raise ValueError(f"{name} is required")
    return value


def _digest(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


class ContextRouteState(StrEnum):
    UNROUTED = "unrouted"
    ROUTED_STANDALONE = "routed_standalone"
    ROUTED_TASK = "routed_task"


class ContextRouteOrigin(StrEnum):
    CONTEXT_TOOL = "context_tool"
    HOST_INITIAL = "host_initial"


@dataclass(frozen=True, slots=True)
class ContextRouteReceipt:
    receipt_id: str
    run_id: str
    raw_call_id: str | None
    effect_id: str | None
    route: TaskScopeRoute
    task_scope_id: str | None
    binding_set_revision: int | None
    recall_refs: tuple[str, ...] = ()
    schema_version: int = 2
    binding_set_receipt_id: str | None = None
    binding_set_receipt_hash: str | None = None
    origin: ContextRouteOrigin = ContextRouteOrigin.CONTEXT_TOOL
    host_authority_ref: str | None = None
    host_authority_hash: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2, 3}:
            raise ValueError("unsupported ContextRouteReceipt schema")
        for value, name in ((self.receipt_id, "receipt_id"), (self.run_id, "run_id")):
            _required(value, name)
        origin = ContextRouteOrigin(self.origin)
        object.__setattr__(self, "origin", origin)
        if self.schema_version < 3 and origin is not ContextRouteOrigin.CONTEXT_TOOL:
            raise ValueError("legacy ContextRouteReceipt requires context_tool origin")
        if origin is ContextRouteOrigin.CONTEXT_TOOL:
            _required(self.raw_call_id, "raw_call_id")
            _required(self.effect_id, "effect_id")
            if self.host_authority_ref is not None or self.host_authority_hash is not None:
                raise ValueError("context_tool route forbids Host authority provenance")
        else:
            if self.schema_version != 3:
                raise ValueError("host_initial route requires ContextRouteReceipt v3")
            if self.raw_call_id is not None or self.effect_id is not None:
                raise ValueError("host_initial route forbids raw-call/effect provenance")
            if self.host_authority_ref is None or self.host_authority_hash is None:
                raise ValueError("host_initial route requires Host authority provenance")
            _required(self.host_authority_ref, "host_authority_ref")
            _digest(self.host_authority_hash, "host_authority_hash")
        object.__setattr__(self, "route", TaskScopeRoute(self.route))
        if self.schema_version == 1 and (
            self.route
            not in {TaskScopeRoute.DIRECT_STANDALONE, TaskScopeRoute.MEMORY_STANDALONE}
            or self.task_scope_id is not None
            or self.binding_set_revision is not None
            or self.binding_set_receipt_id is not None
            or self.binding_set_receipt_hash is not None
        ):
            raise ValueError("ContextRouteReceipt v1 only supports no-authority standalone routes")
        if self.task_scope_id is not None:
            _required(self.task_scope_id, "task_scope_id")
        if self.route in {
            TaskScopeRoute.CONTINUE_ACTIVE,
            TaskScopeRoute.RESUME_EXISTING,
            TaskScopeRoute.CREATE_NEW,
        }:
            if (
                self.task_scope_id is None
                or self.binding_set_revision is None
                or self.binding_set_receipt_id is None
                or self.binding_set_receipt_hash is None
            ):
                raise ValueError("task route requires TaskScope and binding revision")
        elif any(
            item is not None
            for item in (
                self.task_scope_id,
                self.binding_set_revision,
                self.binding_set_receipt_id,
                self.binding_set_receipt_hash,
            )
        ):
            raise ValueError("standalone route forbids TaskScope binding")
        if (
            origin is ContextRouteOrigin.HOST_INITIAL
            and self.route_state is not ContextRouteState.ROUTED_TASK
        ):
            raise ValueError("host_initial route requires TaskScope authority")
        if self.binding_set_revision is not None:
            if isinstance(self.binding_set_revision, bool) or not isinstance(
                self.binding_set_revision, int
            ):
                raise TypeError("binding_set_revision must be an integer or null")
            if self.binding_set_revision < 1:
                raise ValueError("binding_set_revision must be positive")
        if (self.binding_set_receipt_id is None) != (self.binding_set_receipt_hash is None):
            raise ValueError("binding-set receipt identity/hash must be paired")
        if self.binding_set_receipt_id is not None:
            _required(self.binding_set_receipt_id, "binding_set_receipt_id")
        if self.binding_set_receipt_hash is not None:
            _digest(self.binding_set_receipt_hash, "binding_set_receipt_hash")
        refs = tuple(_required(item, "recall_ref") for item in self.recall_refs)
        if len(set(refs)) != len(refs):
            raise ValueError("recall_refs must be unique")
        object.__setattr__(self, "recall_refs", refs)

    @property
    def route_state(self) -> ContextRouteState:
        if self.route in {TaskScopeRoute.DIRECT_STANDALONE, TaskScopeRoute.MEMORY_STANDALONE}:
            return ContextRouteState.ROUTED_STANDALONE
        return ContextRouteState.ROUTED_TASK

    @property
    def receipt_hash(self) -> str:
        return _sha256(self.to_json())

    def to_json(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "raw_call_id": self.raw_call_id,
            "effect_id": self.effect_id,
            "route": self.route.value,
            "task_scope_id": self.task_scope_id,
            "binding_set_revision": self.binding_set_revision,
            "recall_refs": list(self.recall_refs),
        }
        if self.schema_version in {2, 3}:
            payload["binding_set_receipt_id"] = self.binding_set_receipt_id
            payload["binding_set_receipt_hash"] = self.binding_set_receipt_hash
        if self.schema_version == 3:
            payload["origin"] = self.origin.value
            payload["host_authority_ref"] = self.host_authority_ref
            payload["host_authority_hash"] = self.host_authority_hash
        return payload

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ContextRouteReceipt:
        schema_version = value.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise TypeError("schema_version must be an integer")
        legacy_expected = {
            "schema_version",
            "receipt_id",
            "run_id",
            "raw_call_id",
            "effect_id",
            "route",
            "task_scope_id",
            "binding_set_revision",
            "recall_refs",
        }
        expected = legacy_expected | {"binding_set_receipt_id", "binding_set_receipt_hash"}
        v3_expected = expected | {"origin", "host_authority_ref", "host_authority_hash"}
        if schema_version == 1:
            if set(value) != legacy_expected:
                raise ValueError("ContextRouteReceipt v1 fields differ")
        elif schema_version == 2:
            if set(value) != expected:
                raise ValueError("ContextRouteReceipt fields differ")
        elif schema_version == 3:
            if set(value) != v3_expected:
                raise ValueError("ContextRouteReceipt fields differ")
        else:
            raise ValueError("unsupported ContextRouteReceipt schema")
        refs = value["recall_refs"]
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            raise TypeError("recall_refs must be strings")
        revision = value["binding_set_revision"]
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int)):
            raise TypeError("binding_set_revision must be an integer or null")
        route = value["route"]
        if not isinstance(route, str):
            raise TypeError("route must be a string")
        task_scope_id = value["task_scope_id"]
        if task_scope_id is not None and not isinstance(task_scope_id, str):
            raise TypeError("task_scope_id must be a string or null")

        def optional_text(name: str) -> str | None:
            item = value[name]
            if item is not None and not isinstance(item, str):
                raise TypeError(f"{name} must be a string or null")
            return item

        raw_call_id = value["raw_call_id"]
        effect_id = value["effect_id"]
        if raw_call_id is not None and not isinstance(raw_call_id, str):
            raise TypeError("raw_call_id must be a string or null")
        if effect_id is not None and not isinstance(effect_id, str):
            raise TypeError("effect_id must be a string or null")
        origin_value = value.get("origin", ContextRouteOrigin.CONTEXT_TOOL.value)
        if not isinstance(origin_value, str):
            raise TypeError("origin must be a string")
        return cls(
            receipt_id=_required(value["receipt_id"], "receipt_id"),
            run_id=_required(value["run_id"], "run_id"),
            raw_call_id=raw_call_id,
            effect_id=effect_id,
            route=TaskScopeRoute(route),
            task_scope_id=task_scope_id,
            binding_set_revision=revision,
            recall_refs=tuple(refs),
            schema_version=schema_version,
            binding_set_receipt_id=(
                optional_text("binding_set_receipt_id") if schema_version in {2, 3} else None
            ),
            binding_set_receipt_hash=(
                optional_text("binding_set_receipt_hash") if schema_version in {2, 3} else None
            ),
            origin=ContextRouteOrigin(origin_value),
            host_authority_ref=(
                optional_text("host_authority_ref") if schema_version == 3 else None
            ),
            host_authority_hash=(
                optional_text("host_authority_hash") if schema_version == 3 else None
            ),
        )


@dataclass(frozen=True, slots=True)
class RunContextAuthorityRequest:
    run_id: RunId
    provider_turn_ordinal: int
    prior_context_revision: int
    route_state: ContextRouteState
    route_receipt: ContextRouteReceipt | None
    tool_catalog_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        for value, name in (
            (self.provider_turn_ordinal, "provider_turn_ordinal"),
            (self.prior_context_revision, "prior_context_revision"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.provider_turn_ordinal < 1:
            raise ValueError("provider_turn_ordinal must be positive")
        object.__setattr__(self, "route_state", ContextRouteState(self.route_state))
        if (self.route_state is ContextRouteState.UNROUTED) != (self.route_receipt is None):
            raise ValueError("route state and receipt differ")
        if self.route_receipt is not None and self.route_receipt.run_id != self.run_id.value:
            raise ValueError("route receipt belongs to another Run")
        _digest(self.tool_catalog_fingerprint, "tool_catalog_fingerprint")


@dataclass(frozen=True, slots=True)
class RunContextSnapshot:
    snapshot_id: str
    run_id: str
    provider_turn_ordinal: int
    prior_context_revision: int
    snapshot_revision: int
    source_revisions: Mapping[str, int]
    messages: tuple[Message, ...]
    tools: tuple[ProviderToolSpec, ...]
    temperature: float | None
    max_output_tokens: int | None
    metadata: Mapping[str, JsonValue]
    expected_request_fingerprint: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise TypeError("RunContextSnapshot schema_version must be an integer")
        if self.schema_version != 1:
            raise ValueError("unsupported RunContextSnapshot schema")
        _required(self.snapshot_id, "snapshot_id")
        _required(self.run_id, "run_id")
        for value, name, minimum in (
            (self.provider_turn_ordinal, "provider_turn_ordinal", 1),
            (self.prior_context_revision, "prior_context_revision", 0),
            (self.snapshot_revision, "snapshot_revision", 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < minimum:
                raise ValueError(f"{name} is below its minimum")
        revisions = dict(self.source_revisions)
        if not all(
            isinstance(key, str)
            and isinstance(item, int)
            and not isinstance(item, bool)
            and item >= 0
            for key, item in revisions.items()
        ):
            raise ValueError("source_revisions are invalid")
        object.__setattr__(self, "source_revisions", revisions)
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        if not self.messages or not all(isinstance(item, Message) for item in self.messages):
            raise ValueError("Context snapshot requires Messages")
        if not all(isinstance(item, ProviderToolSpec) for item in self.tools):
            raise TypeError("Context snapshot tools are invalid")
        frozen = freeze_json(dict(self.metadata))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "metadata", frozen)
        _digest(self.expected_request_fingerprint, "expected_request_fingerprint")

    def request_payload(self) -> dict[str, JsonValue]:
        return {
            "messages": [
                {
                    "role": getattr(item.role, "value", str(item.role)),
                    "content": (
                        item.content
                        if isinstance(item.content, str)
                        else [block.to_dict() for block in item.content]
                    ),
                    "name": item.name,
                    "call_id": None if item.call_id is None else item.call_id.value,
                    "metadata": thaw_json(cast(FrozenJsonValue, item.metadata)),
                }
                for item in self.messages
            ],
            "tools": [
                {
                    "name": item.name,
                    "description": item.description,
                    "parameters": thaw_json(cast(FrozenJsonValue, item.parameters)),
                }
                for item in self.tools
            ],
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "metadata": thaw_json(cast(FrozenJsonValue, self.metadata)),
        }

    @property
    def payload_hash(self) -> str:
        return _sha256(self.request_payload())

    def receipt_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "provider_turn_ordinal": self.provider_turn_ordinal,
            "prior_context_revision": self.prior_context_revision,
            "snapshot_revision": self.snapshot_revision,
            "source_revisions": dict(self.source_revisions),
            "payload_hash": self.payload_hash,
            "expected_request_fingerprint": self.expected_request_fingerprint,
        }


class RunContextAuthorityPort(Protocol):
    def prepare_snapshot(
        self, request: RunContextAuthorityRequest
    ) -> Awaitable[RunContextSnapshot]: ...


class RuntimeDecisionSinkPort(Protocol):
    def record_no_recall(
        self,
        *,
        run_id: RunId,
        provider_turn_ordinal: int,
        request_fingerprint: str,
    ) -> Awaitable[ContextRouteReceipt]: ...


@dataclass(frozen=True, slots=True)
class TaskExecutionEnvelopeRequest:
    run_id: RunId
    call_id: str
    effect_id: str
    raw_call_id: str
    turn_ordinal: int
    call_ordinal: int
    tool_name: str
    policy: ToolExecutionPolicy
    route_receipt: ContextRouteReceipt | None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        for value, name in (
            (self.call_id, "call_id"),
            (self.effect_id, "effect_id"),
            (self.raw_call_id, "raw_call_id"),
            (self.tool_name, "tool_name"),
        ):
            _required(value, name)
        for ordinal_value, name in (
            (self.turn_ordinal, "turn_ordinal"),
            (self.call_ordinal, "call_ordinal"),
        ):
            if (
                isinstance(ordinal_value, bool)
                or not isinstance(ordinal_value, int)
                or ordinal_value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.policy, ToolExecutionPolicy):
            raise TypeError("policy must use ToolExecutionPolicy")
        if self.route_receipt is not None and self.route_receipt.run_id != self.run_id.value:
            raise ValueError("route receipt belongs to another Run")


class TaskExecutionAuthorityPort(Protocol):
    def issue_envelope(
        self, request: TaskExecutionEnvelopeRequest
    ) -> Awaitable[TaskExecutionEnvelope]: ...


@dataclass(frozen=True, slots=True)
class ToolCatalogSnapshot:
    generation: int
    content_fingerprint: str
    specs: tuple[ProviderToolSpec, ...]
    created_at: float
    catalog_envelope: FrozenJsonValue | None = None
    catalog_envelope_digest_v6: str | None = None

    @property
    def provider_specs_fingerprint(self) -> str:
        """Legacy ProviderToolSpec identity retained for v5 Run compatibility."""

        return self.content_fingerprint


@dataclass(frozen=True, slots=True)
class CatalogHandlerBinding:
    locator: str
    identity_digest: str
    handler: object


@dataclass(frozen=True, slots=True)
class ResolvedCatalogHandlers:
    snapshot: ToolCatalogSnapshot
    handlers: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProviderProjectionReceipt:
    sequence: int
    invocation_id: str
    invocation_version: int
    run_id: str
    execution_session_id: str
    request_id: str
    payload: FrozenJsonValue
    payload_hash: str
    created_at: float

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "sequence": self.sequence,
            "invocation_id": self.invocation_id,
            "invocation_version": self.invocation_version,
            "run_id": self.run_id,
            "execution_session_id": self.execution_session_id,
            "request_id": self.request_id,
            "payload": thaw_json(self.payload),
            "payload_hash": self.payload_hash,
            "created_at": self.created_at,
        }


def frozen_payload(value: JsonValue) -> FrozenJsonValue:
    return freeze_json(value)


class ToolCatalogStore(Protocol):
    def current_tool_catalog_generation(self) -> int: ...

    def read_tool_catalog_snapshot(
        self,
        generation: int | None = None,
        *,
        content_fingerprint: str | None = None,
    ) -> ToolCatalogSnapshot | None: ...


class DurableToolCatalogResolver:
    """Runtime port backed by the execution database's immutable snapshots."""

    def __init__(self, store: ToolCatalogStore) -> None:
        self._store = store

    def current_generation(self) -> int:
        return self._store.current_tool_catalog_generation()

    def resolve(self, generation: int, content_fingerprint: str) -> ToolCatalogSnapshot | None:
        snapshot = self._store.read_tool_catalog_snapshot(generation)
        if snapshot is None or snapshot.content_fingerprint != content_fingerprint:
            return None
        return snapshot

    def resolve_handlers(
        self,
        generation: int,
        catalog_envelope_digest_v6: str,
        bindings: tuple[CatalogHandlerBinding, ...],
    ) -> ResolvedCatalogHandlers | None:
        snapshot = self._store.read_tool_catalog_snapshot(generation)
        if (
            snapshot is None
            or snapshot.catalog_envelope_digest_v6 != catalog_envelope_digest_v6
            or snapshot.catalog_envelope is None
        ):
            return None
        envelope = thaw_json(snapshot.catalog_envelope)
        if not isinstance(envelope, dict) or envelope.get("schema_version") != 6:
            return None
        records = envelope.get("records")
        if not isinstance(records, list):
            return None
        available = {binding.locator: binding for binding in bindings}
        if len(available) != len(bindings):
            return None
        resolved: dict[str, object] = {}
        used_locators: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                return None
            kind = record.get("kind")
            if kind in {"skill_resource", "workflow_profile"}:
                continue
            if kind not in {"executable_tool", "legacy_static_tool"}:
                return None
            locator = record.get("handler_locator")
            identity = record.get("handler_identity_digest")
            provider_name = record.get("provider_name")
            if (
                not isinstance(locator, str)
                or not locator
                or not isinstance(identity, str)
                or not identity
                or not isinstance(provider_name, str)
                or not provider_name
            ):
                return None
            binding = available.get(locator)
            if binding is None or binding.identity_digest != identity or provider_name in resolved:
                return None
            resolved[provider_name] = binding.handler
            used_locators.add(locator)
        if used_locators != set(available):
            return None
        return ResolvedCatalogHandlers(snapshot, resolved)


__all__ = (
    "CatalogHandlerBinding",
    "ContextRouteOrigin",
    "ContextRouteReceipt",
    "ContextRouteState",
    "DurableToolCatalogResolver",
    "ProviderProjectionReceipt",
    "ResolvedCatalogHandlers",
    "RunContextAuthorityPort",
    "RunContextAuthorityRequest",
    "RunContextSnapshot",
    "RuntimeDecisionSinkPort",
    "TaskExecutionAuthorityPort",
    "TaskExecutionEnvelopeRequest",
    "ToolCatalogSnapshot",
    "ToolCatalogStore",
)

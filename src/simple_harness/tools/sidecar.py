# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Typed, host-neutral Tool inventory and execution-policy sidecars."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from simple_harness.contracts import CallId, JsonValue, canonical_json, thaw_json
from simple_harness.workflow.contracts import (
    EffectKind,
    EffectPolicy,
    ToolInventoryEntry,
)

if TYPE_CHECKING:
    from .contracts import JsonObject, ToolContext, ToolResult


class ToolDispatchKind(StrEnum):
    SYNC = "sync"
    ASYNC = "async"
    CONTEXT = "context"
    STAGED = "staged"
    CONTROL = "control"
    PROVIDER = "provider"


class ToolCompletionSemantics(StrEnum):
    SYNC = "sync"
    ACCEPTED_ASYNC = "accepted_async"


class ToolOutcomeParser(StrEnum):
    JSON_ERROR_ENVELOPE = "json_error_envelope_v1"
    SHELL_EXIT = "shell_exit_v1"
    ARTIFACT_ENVELOPE = "artifact_envelope_v1"
    ACTIVATION_PROPOSED = "activation_proposed_v1"
    CODE_ARRAY_OR_ERROR = "code_array_or_error_v1"


@dataclass(frozen=True, slots=True)
class ToolResource:
    """One authorization-visible resource claim resolved before handoff."""

    namespace: str
    resource_id: str
    actions: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("namespace", "resource_id"):
            value = str(getattr(self, name) or "").strip()
            if not value or len(value) > 512:
                raise ValueError(f"{name} is required and bounded")
            object.__setattr__(self, name, value)
        actions = tuple(dict.fromkeys(str(value).strip() for value in self.actions))
        if not actions or any(not value or len(value) > 128 for value in actions):
            raise ValueError("resource actions must be non-empty bounded identities")
        object.__setattr__(self, "actions", actions)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "namespace": self.namespace,
            "resource_id": self.resource_id,
            "actions": list(self.actions),
        }


@runtime_checkable
class ResourceScopeResolverPort(Protocol):
    resolver_id: str
    version: str

    def resolve(
        self, arguments: JsonObject, context: ToolContext
    ) -> Sequence[ToolResource] | Awaitable[Sequence[ToolResource]]: ...


@runtime_checkable
class OutcomeParserPort(Protocol):
    parser_id: str
    version: str
    parser_hash: str

    def parse(self, call_id: CallId, raw: object) -> ToolResult: ...


ToolInventoryRecord = ToolInventoryEntry


def _inventory_json(value: ToolInventoryEntry) -> dict[str, JsonValue]:
    policy = value.effect_policy
    return {
        "name": value.name,
        "access": value.access.value,
        "spec_version": value.spec_version,
        "schema_hash": value.schema_hash,
        "effect_policy": (
            None
            if policy is None
            else {
                "policy_id": policy.policy_id,
                "version": policy.version,
                "kind": policy.kind.value,
                "max_attempts": policy.max_attempts,
                "reusable_across_branches": policy.reusable_across_branches,
            }
        ),
        "effect_policy_hash": value.effect_policy_hash,
        "outcome_parser_id": value.outcome_parser_id,
        "outcome_parser_version": value.outcome_parser_version,
        "outcome_parser_hash": value.outcome_parser_hash,
        "handler_id": value.handler_id,
        "dispatch_kind": value.dispatch_kind,
        "execution_build_digest": value.execution_build_digest,
        "completion_semantics": value.completion_semantics,
        "resource_scope_resolver_id": value.resource_scope_resolver_id,
        "resource_scope_resolver_version": value.resource_scope_resolver_version,
    }


@dataclass(frozen=True, slots=True)
class Sidecar:
    """Frozen SDK authority attached to a ToolSpec, never an opaque Host dict."""

    inventory: ToolInventoryEntry
    outcome_parser: OutcomeParserPort | None = None
    resource_scope_resolver: ResourceScopeResolverPort | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.inventory, ToolInventoryEntry):
            raise TypeError("inventory must use ToolInventoryEntry")
        parser_fields = (
            self.inventory.outcome_parser_id,
            self.inventory.outcome_parser_version,
            self.inventory.outcome_parser_hash,
        )
        if self.outcome_parser is None:
            if any(parser_fields):
                raise ValueError("outcome parser metadata requires a typed parser port")
        elif (
            not isinstance(self.outcome_parser, OutcomeParserPort)
            or parser_fields
            != (
                self.outcome_parser.parser_id,
                self.outcome_parser.version,
                self.outcome_parser.parser_hash,
            )
        ):
            raise ValueError("outcome parser port differs from inventory authority")
        resolver_fields = (
            self.inventory.resource_scope_resolver_id,
            self.inventory.resource_scope_resolver_version,
        )
        if self.resource_scope_resolver is None:
            if any(resolver_fields):
                raise ValueError("resource resolver metadata requires a typed resolver port")
        elif (
            not isinstance(self.resource_scope_resolver, ResourceScopeResolverPort)
            or resolver_fields
            != (
                self.resource_scope_resolver.resolver_id,
                self.resource_scope_resolver.version,
            )
        ):
            raise ValueError("resource resolver port differs from inventory authority")

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_json()).encode()).hexdigest()

    def to_json(self) -> dict[str, JsonValue]:
        return {"schema_version": 1, "inventory": _inventory_json(self.inventory)}

    def parse_outcome(self, call_id: CallId, raw: object) -> ToolResult:
        from .contracts import ToolResult

        if self.outcome_parser is None:
            if not isinstance(raw, ToolResult):
                raise TypeError("Tool without an outcome parser must return ToolResult")
            if raw.call_id != call_id:
                raise ValueError("ToolResult call identity differs")
            return raw
        result = self.outcome_parser.parse(call_id, raw)
        if not isinstance(result, ToolResult) or result.call_id != call_id:
            raise ValueError("outcome parser returned a foreign ToolResult")
        return result

    async def resolve_resources(
        self, arguments: JsonObject, context: ToolContext
    ) -> tuple[ToolResource, ...]:
        if self.resource_scope_resolver is None:
            return ()
        resolved = self.resource_scope_resolver.resolve(arguments, context)
        values = await resolved if inspect.isawaitable(resolved) else resolved
        resources = tuple(values)
        if any(not isinstance(value, ToolResource) for value in resources):
            raise TypeError("resource resolver must return ToolResource values")
        identities = [
            (value.namespace, value.resource_id, value.actions) for value in resources
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("resource resolver returned duplicate claims")
        return tuple(
            sorted(
                resources,
                key=lambda value: (value.namespace, value.resource_id, value.actions),
            )
        )


ToolSidecar = Sidecar


@dataclass(frozen=True, slots=True)
class FunctionOutcomeParser:
    parser_id: str
    version: str
    parser_hash: str
    parser: Callable[[CallId, object], ToolResult]

    def __post_init__(self) -> None:
        if not self.parser_id or not self.version or not callable(self.parser):
            raise ValueError("outcome parser identity, version and callable are required")
        _require_digest(self.parser_hash, "parser_hash")

    def parse(self, call_id: CallId, raw: object) -> ToolResult:
        return self.parser(call_id, raw)


def _require_digest(value: str, name: str) -> str:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


def _failed(call_id: CallId, code: str, message: str) -> ToolResult:
    from .contracts import ToolResult

    return ToolResult.failed(call_id, code, message)


def _json_envelope(call_id: CallId, raw: object) -> ToolResult:
    from .contracts import ToolOutcome, ToolResult

    if isinstance(raw, ToolResult):
        if raw.call_id != call_id:
            raise ValueError("ToolResult call identity differs")
        return raw
    value = thaw_json(raw) if raw is not None else None
    if isinstance(value, Mapping):
        explicit = value.get("outcome") or value.get("status")
        if explicit is not None:
            normalized = str(explicit).lower()
            if normalized in {"pending", "unknown"}:
                return ToolResult.unknown(call_id, "Tool outcome is not yet known.")
            try:
                outcome = ToolOutcome(normalized)
            except ValueError:
                return _failed(
                    call_id,
                    "malformed_tool_outcome",
                    "Tool returned an invalid outcome.",
                )
            public = str(value.get("public_message") or "Tool execution failed.")
            code = str(value.get("error_code") or "tool_failed")
            if outcome is ToolOutcome.SUCCEEDED:
                return ToolResult.succeeded(call_id, cast(JsonValue, value.get("value")))
            if outcome is ToolOutcome.PARTIAL:
                return ToolResult.partial(
                    call_id,
                    cast(JsonValue, value.get("value")),
                    public_message=public,
                )
            if outcome is ToolOutcome.REJECTED:
                return ToolResult.rejected(call_id, code, public)
            if outcome is ToolOutcome.FAILED:
                return ToolResult.failed(
                    call_id,
                    code,
                    public,
                    retryable=bool(value.get("retryable", False)),
                )
            return ToolResult.unknown(call_id, public)
        if value.get("ok") is False or value.get("success") is False or value.get("error"):
            error = value.get("error")
            code = "tool_failed"
            message = "Tool execution failed."
            if isinstance(error, Mapping):
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
            return ToolResult.failed(call_id, code, message)
        return ToolResult.succeeded(call_id, cast(JsonValue, value.get("value", value)))
    return ToolResult.succeeded(call_id, cast(JsonValue, value))


def parse_tool_outcome(
    call_id: CallId,
    raw: object,
    parser: ToolOutcomeParser | str,
) -> ToolResult:
    """Apply one SDK-owned parser and return only the public five-state result."""

    from .contracts import ToolResult

    if not isinstance(call_id, CallId):
        raise TypeError("call_id must use CallId")
    parser = ToolOutcomeParser(parser)
    if parser is ToolOutcomeParser.JSON_ERROR_ENVELOPE:
        return _json_envelope(call_id, raw)
    value = thaw_json(raw) if raw is not None else None
    if parser is ToolOutcomeParser.SHELL_EXIT:
        exit_code = value.get("exit_code") if isinstance(value, Mapping) else None
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return _failed(
                call_id,
                "malformed_shell_outcome",
                "Shell Tool omitted a valid exit code.",
            )
        if exit_code != 0:
            return _failed(
                call_id, "shell_nonzero_exit", "Shell Tool exited unsuccessfully."
            )
        return ToolResult.succeeded(call_id, cast(JsonValue, value))
    if parser is ToolOutcomeParser.ARTIFACT_ENVELOPE:
        path = value.get("path") if isinstance(value, Mapping) else None
        if not isinstance(path, str) or not path.strip():
            return _failed(
                call_id,
                "artifact_path_missing",
                "Artifact Tool did not produce an artifact path.",
            )
        return ToolResult.succeeded(call_id, cast(JsonValue, value))
    if parser is ToolOutcomeParser.CODE_ARRAY_OR_ERROR:
        if isinstance(value, list):
            return ToolResult.succeeded(call_id, cast(JsonValue, value))
        if isinstance(value, Mapping) and isinstance(value.get("items"), list):
            return ToolResult.succeeded(call_id, cast(JsonValue, value))
        if isinstance(value, Mapping) and value.get("error"):
            return _json_envelope(call_id, value)
        return _failed(
            call_id,
            "malformed_code_outcome",
            "Code Tool returned neither items nor an error.",
        )
    if not isinstance(value, Mapping):
        return _failed(
            call_id,
            "malformed_activation",
            "Activation Tool returned an invalid proposal.",
        )
    activation_id = value.get("activation_id")
    state = str(value.get("state") or "").lower()
    if not isinstance(activation_id, str) or not activation_id.strip() or state not in {
        "proposed",
        "pending",
        "accepted",
        "completed",
        "rejected",
        "failed",
        "unknown",
    }:
        return _failed(
            call_id,
            "malformed_activation",
            "Activation Tool returned an invalid proposal.",
        )
    if state == "completed":
        return ToolResult.succeeded(call_id, cast(JsonValue, value))
    if state in {"proposed", "pending", "accepted"}:
        return ToolResult.partial(
            call_id,
            cast(JsonValue, value),
            public_message="Activation is pending.",
        )
    if state == "rejected":
        return ToolResult.rejected(
            call_id, "activation_rejected", "Activation was rejected."
        )
    if state == "failed":
        return ToolResult.failed(call_id, "activation_failed", "Activation failed.")
    return ToolResult.unknown(call_id, "Activation outcome is not yet known.")


def builtin_outcome_parser(
    parser: ToolOutcomeParser | str, *, version: str = "v1"
) -> FunctionOutcomeParser:
    parser = ToolOutcomeParser(parser)
    parser_hash = hashlib.sha256(
        canonical_json({"parser_id": parser.value, "version": version}).encode()
    ).hexdigest()
    return FunctionOutcomeParser(
        parser.value,
        version,
        parser_hash,
        lambda call_id, raw: parse_tool_outcome(call_id, raw, parser),
    )


def inventory_digest(sidecars: Mapping[str, Sidecar]) -> str:
    payload: list[JsonValue] = [
        {"name": name, "digest": sidecars[name].digest} for name in sorted(sidecars)
    ]
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def resource_digest(resources: Sequence[ToolResource]) -> str:
    payload: list[JsonValue] = [value.to_json() for value in resources]
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


__all__ = (
    "EffectKind",
    "EffectPolicy",
    "FunctionOutcomeParser",
    "OutcomeParserPort",
    "ResourceScopeResolverPort",
    "Sidecar",
    "ToolCompletionSemantics",
    "ToolDispatchKind",
    "ToolInventoryRecord",
    "ToolOutcomeParser",
    "ToolResource",
    "ToolSidecar",
    "builtin_outcome_parser",
    "inventory_digest",
    "parse_tool_outcome",
    "resource_digest",
)

# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable catalog and Provider projection cursor contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from simple_harness.contracts import FrozenJsonValue, JsonValue, freeze_json, thaw_json
from simple_harness.providers import ProviderToolSpec


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
    return cast(FrozenJsonValue, freeze_json(value))


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
    "DurableToolCatalogResolver",
    "ProviderProjectionReceipt",
    "ResolvedCatalogHandlers",
    "ToolCatalogSnapshot",
    "ToolCatalogStore",
)

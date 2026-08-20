# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable catalog and Provider projection cursor contracts."""

from __future__ import annotations

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

    def resolve(
        self, generation: int, content_fingerprint: str
    ) -> ToolCatalogSnapshot | None:
        snapshot = self._store.read_tool_catalog_snapshot(generation)
        if snapshot is None or snapshot.content_fingerprint != content_fingerprint:
            return None
        return snapshot


__all__ = (
    "DurableToolCatalogResolver",
    "ProviderProjectionReceipt",
    "ToolCatalogSnapshot",
    "ToolCatalogStore",
)

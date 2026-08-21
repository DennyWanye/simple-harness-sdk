# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Personal workflow selection and validation.

PersonalWorkflowSelectionV1 represents a frozen, cryptographically verified
capability snapshot containing:
- Graph structure (nodes, edges, bindings)
- Tool bindings (frozen ToolSpec topology)
- Security metadata (hashes, lease entries, effect topology)

The selection is immutable and self-validating - any modification invalidates
the selection_id and selection_fingerprint.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from simple_harness.contracts import JsonValue


class PersonalWorkflowSelectionError(ValueError):
    """Raised when selection validation fails."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_json(value: object) -> str:
    """Serialize value to canonical JSON (sorted keys, no whitespace)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(value: object) -> str:
    """Compute SHA-256 hash of canonical JSON representation."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def personal_workflow_query_hash(value: str) -> str:
    """Compute deterministic hash of normalized query text.

    Args:
        value: Query text (may contain extra whitespace)

    Returns:
        SHA-256 hash of normalized query

    Raises:
        PersonalWorkflowSelectionError: If query is empty after normalization
    """
    text = " ".join(str(value).strip().split())
    if not text:
        raise PersonalWorkflowSelectionError("personal_query_required")
    return _hash(
        {
            "schema": "personal-workflow-query-v1",
            "normalized_query": text,
        }
    )


def _required(value: object, name: str) -> str:
    """Validate required string field."""
    if not isinstance(value, str) or not value.strip():
        raise PersonalWorkflowSelectionError(f"{name}_required")
    return value.strip()


def _digest(value: object, name: str) -> str:
    """Validate SHA-256 digest field."""
    text = _required(value, name)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise PersonalWorkflowSelectionError(f"{name}_invalid")
    return text


def _closed_mapping(value: object, name: str) -> Mapping[str, Any]:
    """Validate and clone mapping, ensuring JSON-serializability."""
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PersonalWorkflowSelectionError(f"{name}_invalid")
    try:
        cloned = json.loads(_canonical_json(dict(value)))
    except (TypeError, ValueError) as exc:
        raise PersonalWorkflowSelectionError(f"{name}_invalid") from exc
    if not isinstance(cloned, dict):
        raise PersonalWorkflowSelectionError(f"{name}_invalid")
    return MappingProxyType(cloned)


@dataclass(frozen=True, slots=True)
class PersonalWorkflowSelectionV1:
    """Frozen personal workflow capability snapshot.

    Attributes:
        selection_id: Deterministic ID from identity payload
        selection_fingerprint: Hash of full snapshot (identity + bindings + leases)
        owner_key: Workflow pack owner identifier
        pack_id: Workflow pack identifier
        version: Workflow pack version
        manifest_hash: Hash of pack manifest
        binding_generation: Tool binding generation number
        graph: Workflow graph structure (nodes, edges, outputs)
        graph_hash: SHA-256 hash of canonical graph JSON
        query_hash: Hash of normalized query text
        run_catalog_content_stamp: Runtime catalog content identifier
        lease_entries: Tool capability leases
        effect_topology: Effect policy topology
        tool_bindings: Frozen ToolSpec per tool name
    """

    selection_id: str
    selection_fingerprint: str
    owner_key: str
    pack_id: str
    version: str
    manifest_hash: str
    binding_generation: int
    graph: Mapping[str, Any]
    graph_hash: str
    query_hash: str
    run_catalog_content_stamp: str
    lease_entries: tuple[Mapping[str, Any], ...]
    effect_topology: Mapping[str, Any]
    tool_bindings: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        """Validate selection integrity after construction."""
        # Validate required string fields
        for name in (
            "selection_id",
            "owner_key",
            "pack_id",
            "version",
            "run_catalog_content_stamp",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))

        # Validate digest fields
        for name in (
            "selection_fingerprint",
            "manifest_hash",
            "graph_hash",
            "query_hash",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))

        # Validate binding_generation
        if (
            isinstance(self.binding_generation, bool)
            or not isinstance(self.binding_generation, int)
            or self.binding_generation < 1
        ):
            raise PersonalWorkflowSelectionError("binding_generation_invalid")

        # Clone and freeze mappings
        graph = _closed_mapping(self.graph, "graph")
        topology = _closed_mapping(self.effect_topology, "effect_topology")
        bindings = _closed_mapping(self.tool_bindings, "tool_bindings")

        # Validate and freeze lease entries
        entries = tuple(_closed_mapping(item, "lease_entry") for item in self.lease_entries)
        if not entries:
            raise PersonalWorkflowSelectionError("lease_entries_required")

        object.__setattr__(self, "graph", graph)
        object.__setattr__(self, "effect_topology", topology)
        object.__setattr__(self, "tool_bindings", bindings)
        object.__setattr__(self, "lease_entries", entries)

        # Verify selection_id matches identity payload
        expected_id = "personal-selection:" + _hash(self._identity_payload())
        if self.selection_id != expected_id:
            raise PersonalWorkflowSelectionError("personal_selection_id_mismatch")

        # Verify selection_fingerprint matches full snapshot
        if self.selection_fingerprint != _hash(self._fingerprint_payload()):
            raise PersonalWorkflowSelectionError("personal_selection_fingerprint_mismatch")

    def _identity_payload(self) -> dict[str, object]:
        """Build identity payload for selection_id derivation."""
        return {
            "schema": "personal-workflow-selection-id-v1",
            "owner_key": self.owner_key,
            "pack_id": self.pack_id,
            "version": self.version,
            "manifest_hash": self.manifest_hash,
            "binding_generation": self.binding_generation,
            "graph_hash": self.graph_hash,
            "query_hash": self.query_hash,
        }

    def _fingerprint_payload(self) -> dict[str, object]:
        """Build fingerprint payload for selection_fingerprint derivation."""
        return {
            "schema": "personal-workflow-selection-fingerprint-v1",
            "identity": self._identity_payload(),
            "selection_id": self.selection_id,
            "run_catalog_content_stamp": self.run_catalog_content_stamp,
            "lease_entries": [dict(item) for item in self.lease_entries],
            "effect_topology": dict(self.effect_topology),
            "tool_bindings": {
                name: dict(value) for name, value in sorted(self.tool_bindings.items())
            },
        }

    def to_child_payload(self) -> dict[str, JsonValue]:
        """Serialize selection for workflow state storage.

        Returns:
            Dictionary suitable for JSON serialization
        """
        return {
            "schema_version": 1,
            "selection_id": self.selection_id,
            "selection_fingerprint": self.selection_fingerprint,
            "owner_key": self.owner_key,
            "pack_id": self.pack_id,
            "version": self.version,
            "manifest_hash": self.manifest_hash,
            "binding_generation": self.binding_generation,
            "graph": copy.deepcopy(dict(self.graph)),
            "graph_hash": self.graph_hash,
            "query_hash": self.query_hash,
            "run_catalog_content_stamp": self.run_catalog_content_stamp,
            "lease_entries": [copy.deepcopy(dict(item)) for item in self.lease_entries],
            "effect_topology": copy.deepcopy(dict(self.effect_topology)),
            "tool_bindings": {
                name: copy.deepcopy(dict(value)) for name, value in self.tool_bindings.items()
            },
        }

    @classmethod
    def from_authoritative_mapping(cls, value: Mapping[str, Any]) -> PersonalWorkflowSelectionV1:
        """Deserialize selection from authoritative mapping.

        Args:
            value: Mapping with schema_version=1 and all required fields

        Returns:
            Validated PersonalWorkflowSelectionV1 instance

        Raises:
            PersonalWorkflowSelectionError: If schema invalid or validation fails
        """
        required = {
            "schema_version",
            "selection_id",
            "selection_fingerprint",
            "owner_key",
            "pack_id",
            "version",
            "manifest_hash",
            "binding_generation",
            "graph",
            "graph_hash",
            "query_hash",
            "run_catalog_content_stamp",
            "lease_entries",
            "effect_topology",
            "tool_bindings",
        }
        if set(value) != required or value.get("schema_version") != 1:
            raise PersonalWorkflowSelectionError("personal_selection_schema_invalid")

        lease_entries = value["lease_entries"]
        if not isinstance(lease_entries, Sequence) or isinstance(
            lease_entries, (str, bytes, bytearray)
        ):
            raise PersonalWorkflowSelectionError("lease_entries_invalid")

        return cls(
            selection_id=value["selection_id"],
            selection_fingerprint=value["selection_fingerprint"],
            owner_key=value["owner_key"],
            pack_id=value["pack_id"],
            version=value["version"],
            manifest_hash=value["manifest_hash"],
            binding_generation=value["binding_generation"],
            graph=value["graph"],
            graph_hash=value["graph_hash"],
            query_hash=value["query_hash"],
            run_catalog_content_stamp=value["run_catalog_content_stamp"],
            lease_entries=tuple(lease_entries),
            effect_topology=value["effect_topology"],
            tool_bindings=value["tool_bindings"],
        )

    @classmethod
    def issue(
        cls,
        *,
        owner_key: str,
        pack_id: str,
        version: str,
        manifest_hash: str,
        binding_generation: int,
        graph: Mapping[str, Any],
        graph_hash: str,
        query_hash: str,
        run_catalog_content_stamp: str,
        lease_entries: Sequence[Mapping[str, Any]],
        effect_topology: Mapping[str, Any],
        tool_bindings: Mapping[str, Mapping[str, Any]],
    ) -> PersonalWorkflowSelectionV1:
        """Create new selection with derived selection_id and fingerprint.

        Args:
            owner_key: Workflow pack owner
            pack_id: Workflow pack identifier
            version: Pack version
            manifest_hash: Hash of pack manifest
            binding_generation: Tool binding generation
            graph: Workflow graph structure
            graph_hash: Precomputed graph hash
            query_hash: Hash of normalized query
            run_catalog_content_stamp: Runtime catalog stamp
            lease_entries: Tool capability leases
            effect_topology: Effect policy topology
            tool_bindings: Frozen ToolSpec per tool

        Returns:
            New PersonalWorkflowSelectionV1 with computed IDs
        """
        identity = {
            "schema": "personal-workflow-selection-id-v1",
            "owner_key": owner_key,
            "pack_id": pack_id,
            "version": version,
            "manifest_hash": manifest_hash,
            "binding_generation": binding_generation,
            "graph_hash": graph_hash,
            "query_hash": query_hash,
        }
        selection_id = "personal-selection:" + _hash(identity)

        fingerprint = {
            "schema": "personal-workflow-selection-fingerprint-v1",
            "identity": identity,
            "selection_id": selection_id,
            "run_catalog_content_stamp": run_catalog_content_stamp,
            "lease_entries": [dict(item) for item in lease_entries],
            "effect_topology": dict(effect_topology),
            "tool_bindings": {name: dict(value) for name, value in sorted(tool_bindings.items())},
        }

        return cls(
            selection_id=selection_id,
            selection_fingerprint=_hash(fingerprint),
            owner_key=owner_key,
            pack_id=pack_id,
            version=version,
            manifest_hash=manifest_hash,
            binding_generation=binding_generation,
            graph=graph,
            graph_hash=graph_hash,
            query_hash=query_hash,
            run_catalog_content_stamp=run_catalog_content_stamp,
            lease_entries=tuple(lease_entries),
            effect_topology=effect_topology,
            tool_bindings=tool_bindings,
        )


__all__ = [
    "PersonalWorkflowSelectionError",
    "PersonalWorkflowSelectionV1",
    "personal_workflow_query_hash",
]

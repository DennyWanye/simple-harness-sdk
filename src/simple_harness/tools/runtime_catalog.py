# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Provider-neutral capability discovery and Run-local Tool exposure.

The catalog owns visibility only.  It never stores or invokes handlers and it
never grants authorization.  Consumers keep execution and policy authority in
``ToolRegistry``/``EffectExecutor`` and their Host adapters.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Protocol, TypeAlias, cast, runtime_checkable

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)
from simple_harness.providers import ProviderToolSpec

from .schema import validate_tool_schema

_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_.-]*(?::[a-z0-9_.-]+)+$")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_.-]*(?::[a-z0-9_.-]+)*$")
_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROFILE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SEARCH_TOKEN = re.compile(r"[\w.-]+", re.UNICODE)
_ASCII_WORD = re.compile(r"^[a-z]+$")
_SEARCH_PREFIX_LENGTH = 6

MAX_CATALOG_RECORDS = 4096
MAX_DESCRIPTION_BYTES = 4096
MAX_SEARCH_TERMS = 32
MAX_SEARCH_TERM_BYTES = 256
MAX_SEARCH_RESULTS = 50
MAX_LOCATOR_BYTES = 2048
MAX_METADATA_BYTES = 16_384


class RuntimeCapabilityKind(StrEnum):
    EXECUTABLE_TOOL = "executable_tool"
    SKILL_RESOURCE = "skill_resource"
    WORKFLOW_PROFILE = "workflow_profile"


class ToolExposureMode(StrEnum):
    DIRECT = "direct"
    DEFERRED = "deferred"


class RuntimeToolCatalogError(ValueError):
    """Stable fail-closed catalog error without private source data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _required(value: str, name: str, *, max_bytes: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeToolCatalogError("catalog_field_required", f"{name} is required")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > max_bytes:
        raise RuntimeToolCatalogError("catalog_field_too_large", f"{name} is too large")
    return normalized


def _digest(value: str, name: str) -> str:
    normalized = _required(value, name, max_bytes=64).lower()
    if _HEX_DIGEST.fullmatch(normalized) is None:
        raise RuntimeToolCatalogError("catalog_digest_invalid", f"{name} is invalid")
    return normalized


def _mode(value: ToolExposureMode | str) -> ToolExposureMode:
    try:
        return ToolExposureMode(value)
    except (TypeError, ValueError) as error:
        raise RuntimeToolCatalogError(
            "catalog_exposure_mode_invalid", "exposure_mode is invalid"
        ) from error


def _identity(
    capability_id: str,
    namespace: str,
    source: str,
    source_revision: str,
    description: str,
    search_terms: Sequence[str],
) -> tuple[str, str, str, str, str, tuple[str, ...]]:
    capability_id = _required(capability_id, "capability_id", max_bytes=256)
    namespace = _required(namespace, "namespace", max_bytes=128)
    source = _required(source, "source", max_bytes=128)
    source_revision = _required(source_revision, "source_revision", max_bytes=256)
    description = _required(description, "description", max_bytes=MAX_DESCRIPTION_BYTES)
    if _CAPABILITY_ID.fullmatch(capability_id) is None:
        raise RuntimeToolCatalogError("catalog_capability_id_invalid", "capability_id is invalid")
    if _NAMESPACE.fullmatch(namespace) is None or not capability_id.startswith(f"{namespace}:"):
        raise RuntimeToolCatalogError(
            "catalog_namespace_invalid", "namespace does not own capability_id"
        )
    terms = tuple(search_terms)
    if len(terms) > MAX_SEARCH_TERMS:
        raise RuntimeToolCatalogError(
            "catalog_search_terms_too_many", "search_terms exceed the limit"
        )
    normalized_terms: list[str] = []
    for item in terms:
        normalized_terms.append(_required(item, "search_term", max_bytes=MAX_SEARCH_TERM_BYTES))
    return (
        capability_id,
        namespace,
        source,
        source_revision,
        description,
        tuple(normalized_terms),
    )


def _frozen_object(value: Mapping[str, JsonValue], name: str) -> Mapping[str, FrozenJsonValue]:
    if not isinstance(value, Mapping):
        raise RuntimeToolCatalogError("catalog_json_invalid", f"{name} must be an object")
    detached = dict(value)
    encoded = canonical_json(cast(JsonValue, detached)).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise RuntimeToolCatalogError("catalog_json_too_large", f"{name} is too large")
    frozen = freeze_json(cast(JsonValue, detached))
    if not isinstance(frozen, Mapping):
        raise RuntimeToolCatalogError("catalog_json_invalid", f"{name} must be an object")
    return frozen


def _schema(value: Mapping[str, JsonValue], name: str) -> Mapping[str, FrozenJsonValue]:
    if not isinstance(value, Mapping):
        raise RuntimeToolCatalogError("catalog_schema_invalid", f"{name} must be an object")
    detached = dict(value)
    try:
        validate_tool_schema(detached)
    except (TypeError, ValueError) as error:
        raise RuntimeToolCatalogError("catalog_schema_invalid", f"{name} is invalid") from error
    frozen = freeze_json(cast(JsonValue, detached))
    if not isinstance(frozen, Mapping):
        raise RuntimeToolCatalogError("catalog_schema_invalid", f"{name} must be an object")
    return frozen


@dataclass(frozen=True, slots=True)
class ExecutableToolRecord:
    capability_id: str
    namespace: str
    source: str
    source_revision: str
    exposure_mode: ToolExposureMode | str
    provider_name: str
    description: str
    input_schema: Mapping[str, JsonValue]
    search_terms: tuple[str, ...] = ()
    kind: RuntimeCapabilityKind = field(default=RuntimeCapabilityKind.EXECUTABLE_TOOL, init=False)

    def __post_init__(self) -> None:
        identity = _identity(
            self.capability_id,
            self.namespace,
            self.source,
            self.source_revision,
            self.description,
            self.search_terms,
        )
        for name, value in zip(
            (
                "capability_id",
                "namespace",
                "source",
                "source_revision",
                "description",
                "search_terms",
            ),
            identity,
            strict=True,
        ):
            object.__setattr__(self, name, value)
        provider_name = _required(self.provider_name, "provider_name", max_bytes=64)
        if _PROVIDER_NAME.fullmatch(provider_name) is None:
            raise RuntimeToolCatalogError(
                "catalog_provider_name_invalid", "provider_name is invalid"
            )
        object.__setattr__(self, "provider_name", provider_name)
        object.__setattr__(self, "exposure_mode", _mode(self.exposure_mode))
        object.__setattr__(self, "input_schema", _schema(self.input_schema, "input_schema"))

    @property
    def projection_hash(self) -> str:
        schema = thaw_json(cast(FrozenJsonValue, self.input_schema))
        return hashlib.sha256(canonical_json(cast(JsonValue, schema)).encode()).hexdigest()

    def provider_spec(self) -> ProviderToolSpec:
        return ProviderToolSpec(
            self.provider_name,
            self.description,
            cast(Mapping[str, JsonValue], thaw_json(cast(FrozenJsonValue, self.input_schema))),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            **_base_json(self),
            "provider_name": self.provider_name,
            "input_schema": thaw_json(cast(FrozenJsonValue, self.input_schema)),
        }


@dataclass(frozen=True, slots=True)
class SkillResourceRecord:
    capability_id: str
    namespace: str
    source: str
    source_revision: str
    exposure_mode: ToolExposureMode | str
    skill_locator: str
    content_hash: str
    description: str
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    search_terms: tuple[str, ...] = ()
    kind: RuntimeCapabilityKind = field(default=RuntimeCapabilityKind.SKILL_RESOURCE, init=False)

    def __post_init__(self) -> None:
        identity = _identity(
            self.capability_id,
            self.namespace,
            self.source,
            self.source_revision,
            self.description,
            self.search_terms,
        )
        for name, value in zip(
            (
                "capability_id",
                "namespace",
                "source",
                "source_revision",
                "description",
                "search_terms",
            ),
            identity,
            strict=True,
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "exposure_mode", _mode(self.exposure_mode))
        object.__setattr__(
            self,
            "skill_locator",
            _required(self.skill_locator, "skill_locator", max_bytes=MAX_LOCATOR_BYTES),
        )
        object.__setattr__(self, "content_hash", _digest(self.content_hash, "content_hash"))
        object.__setattr__(self, "metadata", _frozen_object(self.metadata, "metadata"))

    @property
    def projection_hash(self) -> str:
        return self.content_hash

    def to_json(self) -> dict[str, JsonValue]:
        return {
            **_base_json(self),
            "skill_locator": self.skill_locator,
            "content_hash": self.content_hash,
            "metadata": thaw_json(cast(FrozenJsonValue, self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class WorkflowProfileRecord:
    capability_id: str
    namespace: str
    source: str
    source_revision: str
    exposure_mode: ToolExposureMode | str
    profile_key: str
    profile_fingerprint: str
    description: str
    start_input_schema: Mapping[str, JsonValue]
    search_terms: tuple[str, ...] = ()
    kind: RuntimeCapabilityKind = field(default=RuntimeCapabilityKind.WORKFLOW_PROFILE, init=False)

    def __post_init__(self) -> None:
        identity = _identity(
            self.capability_id,
            self.namespace,
            self.source,
            self.source_revision,
            self.description,
            self.search_terms,
        )
        for name, value in zip(
            (
                "capability_id",
                "namespace",
                "source",
                "source_revision",
                "description",
                "search_terms",
            ),
            identity,
            strict=True,
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "exposure_mode", _mode(self.exposure_mode))
        profile_key = _required(self.profile_key, "profile_key", max_bytes=128)
        if _PROFILE_KEY.fullmatch(profile_key) is None:
            raise RuntimeToolCatalogError("catalog_profile_key_invalid", "profile_key is invalid")
        object.__setattr__(self, "profile_key", profile_key)
        object.__setattr__(
            self,
            "profile_fingerprint",
            _digest(self.profile_fingerprint, "profile_fingerprint"),
        )
        object.__setattr__(
            self,
            "start_input_schema",
            _schema(self.start_input_schema, "start_input_schema"),
        )

    @property
    def projection_hash(self) -> str:
        return self.profile_fingerprint

    def to_json(self) -> dict[str, JsonValue]:
        return {
            **_base_json(self),
            "profile_key": self.profile_key,
            "profile_fingerprint": self.profile_fingerprint,
            "start_input_schema": thaw_json(cast(FrozenJsonValue, self.start_input_schema)),
        }


RuntimeCapabilityRecord: TypeAlias = (
    ExecutableToolRecord | SkillResourceRecord | WorkflowProfileRecord
)


def _base_json(record: RuntimeCapabilityRecord) -> dict[str, JsonValue]:
    return {
        "kind": record.kind.value,
        "capability_id": record.capability_id,
        "namespace": record.namespace,
        "source": record.source,
        "source_revision": record.source_revision,
        "exposure_mode": cast(ToolExposureMode, record.exposure_mode).value,
        "description": record.description,
        "search_terms": list(record.search_terms),
    }


def _selection_key(record: RuntimeCapabilityRecord) -> str:
    if isinstance(record, ExecutableToolRecord):
        return record.provider_name
    if isinstance(record, SkillResourceRecord):
        return record.skill_locator
    return record.profile_key


def _schema_search_text(record: RuntimeCapabilityRecord) -> str:
    if isinstance(record, ExecutableToolRecord):
        value: FrozenJsonValue = cast(FrozenJsonValue, record.input_schema)
    elif isinstance(record, WorkflowProfileRecord):
        value = cast(FrozenJsonValue, record.start_input_schema)
    else:
        value = cast(FrozenJsonValue, record.metadata)
    detached = thaw_json(value)
    parts: list[str] = []
    stack: list[JsonValue] = [detached]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                parts.append(key)
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            parts.append(current)
    return " ".join(parts)


def _search_document(record: RuntimeCapabilityRecord) -> str:
    return " ".join(
        (
            record.capability_id,
            record.namespace,
            record.source,
            record.description,
            _selection_key(record),
            *record.search_terms,
            _schema_search_text(record),
        )
    ).casefold()


def _query_search_tokens(query: str) -> tuple[str, ...]:
    """Return exact tokens plus a conservative English morphology prefix.

    Capability metadata is often authored in one grammatical form while a
    model searches with another (for example ``translate`` vs
    ``translation``).  A six-character prefix keeps discovery deterministic
    and dependency-free without applying fuzzy matching to short words,
    identifiers, or non-Latin text.
    """

    exact = tuple(dict.fromkeys(_SEARCH_TOKEN.findall(query))) or (query,)
    expanded: list[str] = []
    for token in exact:
        expanded.append(token)
        if len(token) > _SEARCH_PREFIX_LENGTH and _ASCII_WORD.fullmatch(token):
            expanded.append(token[:_SEARCH_PREFIX_LENGTH])
    return tuple(dict.fromkeys(expanded))


@dataclass(frozen=True, slots=True)
class RuntimeToolCatalogSnapshot:
    generation: int
    records: tuple[RuntimeCapabilityRecord, ...]
    fingerprint: str
    search_documents: tuple[str, ...]
    _by_id: Mapping[str, RuntimeCapabilityRecord] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or self.generation < 1:
            raise RuntimeToolCatalogError(
                "catalog_generation_invalid", "generation must be positive"
            )
        records = tuple(self.records)
        if not records or len(records) > MAX_CATALOG_RECORDS:
            raise RuntimeToolCatalogError("catalog_size_invalid", "catalog record count is invalid")
        if records != tuple(sorted(records, key=lambda item: item.capability_id)):
            raise RuntimeToolCatalogError("catalog_order_invalid", "catalog records must be sorted")
        ids = [item.capability_id for item in records]
        if len(ids) != len(set(ids)):
            raise RuntimeToolCatalogError("catalog_capability_collision", "capability_id collision")
        documents = tuple(self.search_documents)
        if len(documents) != len(records) or any(not item for item in documents):
            raise RuntimeToolCatalogError("catalog_search_index_invalid", "search index is invalid")
        expected = _snapshot_fingerprint(self.generation, records)
        if self.fingerprint != expected:
            raise RuntimeToolCatalogError(
                "catalog_fingerprint_invalid", "catalog fingerprint differs"
            )
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "search_documents", documents)
        object.__setattr__(
            self,
            "_by_id",
            MappingProxyType({item.capability_id: item for item in records}),
        )

    def require(self, capability_id: str) -> RuntimeCapabilityRecord:
        try:
            return self._by_id[capability_id]
        except KeyError as error:
            raise RuntimeToolCatalogError(
                "catalog_capability_not_found", "capability is not in the frozen catalog"
            ) from error

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "generation": self.generation,
            "fingerprint": self.fingerprint,
            "records": [item.to_json() for item in self.records],
        }


def _snapshot_fingerprint(generation: int, records: tuple[RuntimeCapabilityRecord, ...]) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "schema": "runtime_tool_catalog/v1",
                "generation": generation,
                "records": [item.to_json() for item in records],
            }
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RunToolExposureState:
    run_id: RunId
    catalog_fingerprint: str
    revision: int
    direct_ids: tuple[str, ...]
    deferred_ids: tuple[str, ...]
    activated_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        object.__setattr__(
            self,
            "catalog_fingerprint",
            _digest(self.catalog_fingerprint, "catalog_fingerprint"),
        )
        if isinstance(self.revision, bool) or self.revision < 0:
            raise RuntimeToolCatalogError(
                "catalog_exposure_revision_invalid", "revision must be non-negative"
            )
        groups = (
            tuple(self.direct_ids),
            tuple(self.deferred_ids),
            tuple(self.activated_ids),
        )
        if any(group != tuple(sorted(set(group))) for group in groups):
            raise RuntimeToolCatalogError(
                "catalog_exposure_ids_invalid", "exposure IDs must be unique and sorted"
            )
        direct, deferred, activated = map(set, groups)
        if direct & deferred or direct & activated or deferred & activated:
            raise RuntimeToolCatalogError(
                "catalog_exposure_overlap", "exposure ID groups must be disjoint"
            )
        object.__setattr__(self, "direct_ids", groups[0])
        object.__setattr__(self, "deferred_ids", groups[1])
        object.__setattr__(self, "activated_ids", groups[2])

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema": "run_tool_exposure/v1",
            "run_id": self.run_id.value,
            "catalog_fingerprint": self.catalog_fingerprint,
            "revision": self.revision,
            "activated_ids": list(self.activated_ids),
        }

    @classmethod
    def from_json(
        cls,
        value: Mapping[str, object],
        *,
        direct_ids: tuple[str, ...],
        deferred_ids: tuple[str, ...],
    ) -> RunToolExposureState:
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "run_id",
            "catalog_fingerprint",
            "revision",
            "activated_ids",
        }:
            raise RuntimeToolCatalogError(
                "catalog_checkpoint_invalid", "Run exposure checkpoint is invalid"
            )
        if value.get("schema") != "run_tool_exposure/v1":
            raise RuntimeToolCatalogError(
                "catalog_checkpoint_invalid", "Run exposure checkpoint schema is invalid"
            )
        revision = value.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise RuntimeToolCatalogError(
                "catalog_checkpoint_invalid", "Run exposure checkpoint revision is invalid"
            )

        raw_activated = value.get("activated_ids")
        if not isinstance(raw_activated, list) or not all(
            isinstance(item, str) for item in raw_activated
        ):
            raise RuntimeToolCatalogError(
                "catalog_checkpoint_invalid", "Run exposure checkpoint IDs are invalid"
            )
        activated_ids = tuple(raw_activated)
        if revision != len(activated_ids) or not set(activated_ids).issubset(deferred_ids):
            raise RuntimeToolCatalogError(
                "catalog_checkpoint_invalid", "Run exposure checkpoint revision differs"
            )

        run_id = value.get("run_id")
        fingerprint = value.get("catalog_fingerprint")
        if not isinstance(run_id, str) or not isinstance(fingerprint, str):
            raise RuntimeToolCatalogError(
                "catalog_checkpoint_invalid", "Run exposure checkpoint identity is invalid"
            )
        return cls(
            RunId(run_id),
            fingerprint,
            revision,
            direct_ids,
            tuple(item for item in deferred_ids if item not in set(activated_ids)),
            activated_ids,
        )


@dataclass(frozen=True, slots=True)
class RuntimeToolDescriptor:
    capability_id: str
    kind: RuntimeCapabilityKind
    namespace: str
    source: str
    source_revision: str
    selection_key: str
    description: str
    exposure_mode: ToolExposureMode
    score: int

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "capability_id": self.capability_id,
            "kind": self.kind.value,
            "namespace": self.namespace,
            "source": self.source,
            "source_revision": self.source_revision,
            "selection_key": self.selection_key,
            "description": self.description,
            "exposure_mode": self.exposure_mode.value,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class RuntimeToolSearchPage:
    items: tuple[RuntimeToolDescriptor, ...]
    next_cursor: int | None


@dataclass(frozen=True, slots=True)
class RuntimeToolDescription:
    descriptor: RuntimeToolDescriptor
    capability_hash: str
    projection: Mapping[str, FrozenJsonValue]
    exposure_revision: int
    nonce: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "descriptor": self.descriptor.to_json(),
            "capability_hash": self.capability_hash,
            "projection": thaw_json(cast(FrozenJsonValue, self.projection)),
            "exposure_revision": self.exposure_revision,
            "nonce": self.nonce,
        }


@dataclass(frozen=True, slots=True)
class ToolActivationReceipt:
    activation_id: str
    run_id: RunId
    catalog_fingerprint: str
    exposure_revision: int
    capability_id: str
    capability_kind: RuntimeCapabilityKind
    capability_hash: str
    projection_hash: str
    describe_nonce: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema": "runtime_tool_activation_receipt/v1",
            "activation_id": self.activation_id,
            "run_id": self.run_id.value,
            "catalog_fingerprint": self.catalog_fingerprint,
            "exposure_revision": self.exposure_revision,
            "capability_id": self.capability_id,
            "capability_kind": self.capability_kind.value,
            "capability_hash": self.capability_hash,
            "projection_hash": self.projection_hash,
            "describe_nonce": self.describe_nonce,
        }


@runtime_checkable
class RunToolExposurePort(Protocol):
    """Runtime seam for durable, per-Run visibility evolution."""

    def restore(self, run_id: RunId, checkpoint: JsonValue | None) -> None: ...

    def provider_specs(self, run_id: RunId) -> tuple[ProviderToolSpec, ...]: ...

    def observe_tool_result(
        self, run_id: RunId, tool_name: str, result: Mapping[str, object]
    ) -> None: ...

    def checkpoint(self, run_id: RunId) -> JsonValue: ...


class RuntimeToolCatalog:
    """Immutable catalog plus pure Run-local exposure transitions."""

    def __init__(self, records: Sequence[RuntimeCapabilityRecord], *, generation: int) -> None:
        ordered = tuple(sorted(tuple(records), key=lambda item: item.capability_id))
        self._validate_collisions(ordered)
        fingerprint = _snapshot_fingerprint(generation, ordered)
        self._snapshot = RuntimeToolCatalogSnapshot(
            generation,
            ordered,
            fingerprint,
            tuple(_search_document(item) for item in ordered),
        )

    @property
    def snapshot(self) -> RuntimeToolCatalogSnapshot:
        return self._snapshot

    @staticmethod
    def _validate_collisions(records: tuple[RuntimeCapabilityRecord, ...]) -> None:
        if not records or len(records) > MAX_CATALOG_RECORDS:
            raise RuntimeToolCatalogError("catalog_size_invalid", "catalog record count is invalid")
        ids: set[str] = set()
        provider_names: set[str] = set()
        skill_locators: set[str] = set()
        workflow_keys: set[str] = set()
        namespace_owners: dict[str, tuple[str, str]] = {}
        for record in records:
            if not isinstance(
                record,
                (ExecutableToolRecord, SkillResourceRecord, WorkflowProfileRecord),
            ):
                raise RuntimeToolCatalogError(
                    "catalog_record_kind_invalid", "catalog record kind is invalid"
                )
            if record.capability_id in ids:
                raise RuntimeToolCatalogError(
                    "catalog_capability_collision", "capability_id collision"
                )
            ids.add(record.capability_id)
            owner = (record.source, record.source_revision)
            existing_owner = namespace_owners.setdefault(record.namespace, owner)
            if existing_owner != owner:
                raise RuntimeToolCatalogError(
                    "catalog_namespace_collision", "namespace owner collision"
                )
            if isinstance(record, ExecutableToolRecord):
                if record.provider_name in provider_names:
                    raise RuntimeToolCatalogError(
                        "catalog_provider_name_collision", "provider_name collision"
                    )
                provider_names.add(record.provider_name)
            elif isinstance(record, SkillResourceRecord):
                if record.skill_locator in skill_locators:
                    raise RuntimeToolCatalogError(
                        "catalog_skill_locator_collision", "skill_locator collision"
                    )
                skill_locators.add(record.skill_locator)
            elif record.profile_key in workflow_keys:
                raise RuntimeToolCatalogError(
                    "catalog_workflow_profile_collision", "profile_key collision"
                )
            else:
                workflow_keys.add(record.profile_key)

    def start_run(self, run_id: RunId) -> RunToolExposureState:
        if not isinstance(run_id, RunId):
            raise TypeError("run_id must use RunId")
        direct = tuple(
            item.capability_id
            for item in self._snapshot.records
            if item.exposure_mode is ToolExposureMode.DIRECT
        )
        deferred = tuple(
            item.capability_id
            for item in self._snapshot.records
            if item.exposure_mode is ToolExposureMode.DEFERRED
        )
        return RunToolExposureState(
            run_id,
            self._snapshot.fingerprint,
            0,
            direct,
            deferred,
        )

    def _validate_state(self, state: RunToolExposureState) -> None:
        if not isinstance(state, RunToolExposureState):
            raise TypeError("state must be RunToolExposureState")
        if state.catalog_fingerprint != self._snapshot.fingerprint:
            raise RuntimeToolCatalogError(
                "catalog_state_fingerprint_stale", "Run exposure uses another catalog"
            )
        expected = {item.capability_id for item in self._snapshot.records}
        actual = set(state.direct_ids) | set(state.deferred_ids) | set(state.activated_ids)
        if actual != expected:
            raise RuntimeToolCatalogError(
                "catalog_state_membership_invalid", "Run exposure membership differs"
            )

    def search(
        self,
        state: RunToolExposureState,
        query: str,
        *,
        limit: int = 10,
        cursor: int = 0,
    ) -> RuntimeToolSearchPage:
        self._validate_state(state)
        query = _required(query, "query", max_bytes=1024).casefold()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise RuntimeToolCatalogError("catalog_search_limit_invalid", "search limit is invalid")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise RuntimeToolCatalogError(
                "catalog_search_cursor_invalid", "search cursor is invalid"
            )
        tokens = _query_search_tokens(query)
        deferred = set(state.deferred_ids)
        ranked: list[tuple[int, RuntimeCapabilityRecord]] = []
        for record, document in zip(
            self._snapshot.records, self._snapshot.search_documents, strict=True
        ):
            if record.capability_id not in deferred:
                continue
            score = sum(document.count(token) for token in tokens)
            exact_match = query in record.capability_id.casefold() or (
                query == _selection_key(record).casefold()
            )
            if exact_match:
                score += 100
            if score:
                ranked.append((score, record))
        ranked.sort(key=lambda item: (-item[0], item[1].capability_id))
        selected = ranked[cursor : cursor + limit]
        items = tuple(self._descriptor(record, score) for score, record in selected)
        next_cursor = cursor + len(items) if cursor + len(items) < len(ranked) else None
        return RuntimeToolSearchPage(items, next_cursor)

    @staticmethod
    def _descriptor(record: RuntimeCapabilityRecord, score: int = 0) -> RuntimeToolDescriptor:
        return RuntimeToolDescriptor(
            record.capability_id,
            record.kind,
            record.namespace,
            record.source,
            record.source_revision,
            _selection_key(record),
            record.description,
            cast(ToolExposureMode, record.exposure_mode),
            score,
        )

    def describe(self, state: RunToolExposureState, capability_id: str) -> RuntimeToolDescription:
        self._validate_state(state)
        record = self._snapshot.require(capability_id)
        projection = self._projection(record)
        capability_hash = self._capability_hash(record)
        nonce = self._describe_nonce(state, record, capability_hash)
        return RuntimeToolDescription(
            self._descriptor(record),
            capability_hash,
            projection,
            state.revision,
            nonce,
        )

    @staticmethod
    def _projection(
        record: RuntimeCapabilityRecord,
    ) -> Mapping[str, FrozenJsonValue]:
        if isinstance(record, ExecutableToolRecord):
            value: dict[str, JsonValue] = {
                "provider_name": record.provider_name,
                "input_schema": thaw_json(cast(FrozenJsonValue, record.input_schema)),
                "schema_hash": record.projection_hash,
            }
        elif isinstance(record, SkillResourceRecord):
            value = {
                "skill_locator": record.skill_locator,
                "content_hash": record.content_hash,
                "metadata": thaw_json(cast(FrozenJsonValue, record.metadata)),
            }
        else:
            value = {
                "profile_key": record.profile_key,
                "profile_fingerprint": record.profile_fingerprint,
                "start_input_schema": thaw_json(cast(FrozenJsonValue, record.start_input_schema)),
            }
        frozen = freeze_json(value)
        assert isinstance(frozen, Mapping)
        return frozen

    @staticmethod
    def _capability_hash(record: RuntimeCapabilityRecord) -> str:
        return hashlib.sha256(canonical_json(record.to_json()).encode("utf-8")).hexdigest()

    def _describe_nonce(
        self,
        state: RunToolExposureState,
        record: RuntimeCapabilityRecord,
        capability_hash: str,
    ) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "schema": "runtime_tool_describe_nonce/v1",
                    "run_id": state.run_id.value,
                    "catalog_fingerprint": state.catalog_fingerprint,
                    "exposure_revision": state.revision,
                    "capability_id": record.capability_id,
                    "capability_hash": capability_hash,
                    "projection_hash": record.projection_hash,
                }
            ).encode("utf-8")
        ).hexdigest()

    def activate(
        self,
        state: RunToolExposureState,
        capability_id: str,
        nonce: str,
    ) -> tuple[RunToolExposureState, ToolActivationReceipt]:
        self._validate_state(state)
        record = self._snapshot.require(capability_id)
        capability_hash = self._capability_hash(record)
        if capability_id in state.direct_ids:
            raise RuntimeToolCatalogError(
                "catalog_capability_already_direct", "capability is already direct"
            )
        if capability_id in state.activated_ids:
            return state, self._activation_receipt(state, record, capability_hash, nonce)
        if capability_id not in state.deferred_ids:
            raise RuntimeToolCatalogError(
                "catalog_capability_not_deferred", "capability is not deferred"
            )
        expected_nonce = self._describe_nonce(state, record, capability_hash)
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
            raise RuntimeToolCatalogError(
                "catalog_describe_nonce_invalid", "describe nonce is stale or invalid"
            )
        next_state = RunToolExposureState(
            state.run_id,
            state.catalog_fingerprint,
            state.revision + 1,
            state.direct_ids,
            tuple(item for item in state.deferred_ids if item != capability_id),
            tuple(sorted((*state.activated_ids, capability_id))),
        )
        return next_state, self._activation_receipt(next_state, record, capability_hash, nonce)

    @staticmethod
    def _activation_receipt(
        state: RunToolExposureState,
        record: RuntimeCapabilityRecord,
        capability_hash: str,
        describe_nonce: str,
    ) -> ToolActivationReceipt:
        activation_id = hashlib.sha256(
            canonical_json(
                {
                    "schema": "runtime_tool_activation/v1",
                    "run_id": state.run_id.value,
                    "catalog_fingerprint": state.catalog_fingerprint,
                    "capability_id": record.capability_id,
                    "capability_hash": capability_hash,
                    "projection_hash": record.projection_hash,
                }
            ).encode("utf-8")
        ).hexdigest()
        return ToolActivationReceipt(
            activation_id,
            state.run_id,
            state.catalog_fingerprint,
            state.revision,
            record.capability_id,
            record.kind,
            capability_hash,
            record.projection_hash,
            describe_nonce,
        )

    def provider_specs(self, state: RunToolExposureState) -> tuple[ProviderToolSpec, ...]:
        self._validate_state(state)
        visible = set(state.direct_ids) | set(state.activated_ids)
        return tuple(
            record.provider_spec()
            for record in self._snapshot.records
            if isinstance(record, ExecutableToolRecord) and record.capability_id in visible
        )

    def audit_summary(self, state: RunToolExposureState) -> dict[str, JsonValue]:
        self._validate_state(state)
        sources = Counter(item.source for item in self._snapshot.records)
        kinds = Counter(item.kind.value for item in self._snapshot.records)
        return {
            "catalog_generation": self._snapshot.generation,
            "catalog_fingerprint": self._snapshot.fingerprint,
            "exposure_revision": state.revision,
            "direct_count": len(state.direct_ids),
            "deferred_count": len(state.deferred_ids),
            "activated_count": len(state.activated_ids),
            "sources": dict(sorted(sources.items())),
            "kinds": dict(sorted(kinds.items())),
            "reason_codes": {},
        }


class CatalogRunToolExposure:
    """Minimal in-process implementation of :class:`RunToolExposurePort`.

    Runtime persistence remains outside this class.  Callers restore and save
    the exact JSON checkpoint at their existing transaction boundary.
    """

    _ACTIVATION_TOOL_NAMES = frozenset({"tool_activate", "capability_activate"})
    _RECEIPT_FIELDS = frozenset(
        {
            "schema",
            "activation_id",
            "run_id",
            "catalog_fingerprint",
            "exposure_revision",
            "capability_id",
            "capability_kind",
            "capability_hash",
            "projection_hash",
            "describe_nonce",
        }
    )

    def __init__(self, catalog: RuntimeToolCatalog) -> None:
        if not isinstance(catalog, RuntimeToolCatalog):
            raise TypeError("catalog must be RuntimeToolCatalog")
        self._catalog = catalog
        self._states: dict[str, RunToolExposureState] = {}
        self._lock = RLock()

    @property
    def catalog(self) -> RuntimeToolCatalog:
        return self._catalog

    def state(self, run_id: RunId) -> RunToolExposureState:
        if not isinstance(run_id, RunId):
            raise TypeError("run_id must use RunId")
        try:
            return self._states[run_id.value]
        except KeyError as error:
            raise RuntimeToolCatalogError(
                "catalog_run_not_restored", "Run exposure is not restored"
            ) from error

    def restore(self, run_id: RunId, checkpoint: JsonValue | None) -> None:
        if not isinstance(run_id, RunId):
            raise TypeError("run_id must use RunId")
        with self._lock:
            state = (
                self._catalog.start_run(run_id)
                if checkpoint is None
                else self._restore_checkpoint(run_id, checkpoint)
            )
            # The durable ReAct checkpoint is authoritative on every reopen.
            # An in-memory state may be ahead when the process failed after
            # observing a terminal Effect but before checkpoint CAS.
            self._states[run_id.value] = state

    def _restore_checkpoint(self, run_id: RunId, checkpoint: JsonValue) -> RunToolExposureState:
        if not isinstance(checkpoint, dict):
            raise RuntimeToolCatalogError(
                "catalog_checkpoint_invalid", "Run exposure checkpoint must be an object"
            )
        base = self._catalog.start_run(run_id)
        state = RunToolExposureState.from_json(
            checkpoint,
            direct_ids=base.direct_ids,
            deferred_ids=base.deferred_ids,
        )
        if state.run_id != run_id:
            raise RuntimeToolCatalogError(
                "catalog_checkpoint_run_mismatch", "Run exposure checkpoint uses another Run"
            )
        self._catalog._validate_state(state)
        return state

    def checkpoint(self, run_id: RunId) -> JsonValue:
        with self._lock:
            return self.state(run_id).to_json()

    def provider_specs(self, run_id: RunId) -> tuple[ProviderToolSpec, ...]:
        with self._lock:
            return self._catalog.provider_specs(self.state(run_id))

    def search(
        self,
        run_id: RunId,
        query: str,
        *,
        limit: int = 10,
        cursor: int = 0,
    ) -> RuntimeToolSearchPage:
        with self._lock:
            return self._catalog.search(self.state(run_id), query, limit=limit, cursor=cursor)

    def describe(self, run_id: RunId, capability_id: str) -> RuntimeToolDescription:
        with self._lock:
            return self._catalog.describe(self.state(run_id), capability_id)

    def activate(self, run_id: RunId, capability_id: str, nonce: str) -> ToolActivationReceipt:
        with self._lock:
            next_state, receipt = self._catalog.activate(self.state(run_id), capability_id, nonce)
            self._states[run_id.value] = next_state
            return receipt

    def prepare_activation(
        self, run_id: RunId, capability_id: str, nonce: str
    ) -> ToolActivationReceipt:
        """Issue a receipt without mutating visibility before Effect settlement.

        An activation Tool handler returns this receipt.  The ReAct loop applies
        it only after the EffectExecutor returns a durable terminal result via
        :meth:`observe_tool_result`, including terminal-effect replay.
        """

        with self._lock:
            _next_state, receipt = self._catalog.activate(
                self.state(run_id), capability_id, nonce
            )
            return receipt

    def observe_tool_result(
        self, run_id: RunId, tool_name: str, result: Mapping[str, object]
    ) -> None:
        if not isinstance(run_id, RunId):
            raise TypeError("run_id must use RunId")
        if not isinstance(result, Mapping):
            raise RuntimeToolCatalogError(
                "catalog_activation_result_invalid", "Tool result must be an object"
            )
        raw_value = result.get("value")
        payload = raw_value if isinstance(raw_value, Mapping) else result
        schema = payload.get("schema")
        if tool_name not in self._ACTIVATION_TOOL_NAMES:
            if schema == "runtime_tool_activation_receipt/v1":
                raise RuntimeToolCatalogError(
                    "catalog_activation_tool_mismatch",
                    "Activation receipt came from another Tool",
                )
            return
        if schema != "runtime_tool_activation_receipt/v1":
            raise RuntimeToolCatalogError(
                "catalog_activation_receipt_missing",
                "Activation Tool result lacks a typed receipt",
            )
        if set(payload) != self._RECEIPT_FIELDS:
            raise RuntimeToolCatalogError(
                "catalog_activation_receipt_invalid", "Activation receipt shape is invalid"
            )
        with self._lock:
            self._apply_receipt(run_id, payload)

    def _apply_receipt(self, run_id: RunId, payload: Mapping[str, object]) -> None:
        state = self.state(run_id)
        capability_id = payload.get("capability_id")
        if not isinstance(capability_id, str):
            raise RuntimeToolCatalogError(
                "catalog_activation_receipt_invalid", "Activation capability ID is invalid"
            )
        record = self._catalog.snapshot.require(capability_id)
        capability_hash = self._catalog._capability_hash(record)
        describe_nonce = payload.get("describe_nonce")
        exposure_revision = payload.get("exposure_revision")
        if (
            not isinstance(describe_nonce, str)
            or isinstance(exposure_revision, bool)
            or not isinstance(exposure_revision, int)
        ):
            raise RuntimeToolCatalogError(
                "catalog_activation_receipt_invalid", "Activation receipt facts are invalid"
            )
        expected_identity = self._catalog._activation_receipt(
            state, record, capability_hash, describe_nonce
        )
        exact = {
            "activation_id": expected_identity.activation_id,
            "run_id": run_id.value,
            "catalog_fingerprint": state.catalog_fingerprint,
            "capability_kind": record.kind.value,
            "capability_hash": capability_hash,
            "projection_hash": record.projection_hash,
        }
        if any(payload.get(name) != value for name, value in exact.items()):
            raise RuntimeToolCatalogError(
                "catalog_activation_receipt_invalid", "Activation receipt identity differs"
            )
        if capability_id in state.direct_ids:
            raise RuntimeToolCatalogError(
                "catalog_activation_receipt_invalid", "Direct capability cannot be activated"
            )
        if capability_id in state.activated_ids:
            if exposure_revision > state.revision:
                raise RuntimeToolCatalogError(
                    "catalog_activation_receipt_future",
                    "Activation receipt is ahead of Run exposure",
                )
            return
        if capability_id not in state.deferred_ids or exposure_revision != state.revision + 1:
            raise RuntimeToolCatalogError(
                "catalog_activation_receipt_out_of_order",
                "Activation receipt is not the next Run exposure revision",
            )
        expected_nonce = self._catalog._describe_nonce(state, record, capability_hash)
        if not hmac.compare_digest(describe_nonce, expected_nonce):
            raise RuntimeToolCatalogError(
                "catalog_describe_nonce_invalid", "Activation receipt nonce is stale or invalid"
            )
        self._states[run_id.value] = RunToolExposureState(
            state.run_id,
            state.catalog_fingerprint,
            exposure_revision,
            state.direct_ids,
            tuple(item for item in state.deferred_ids if item != capability_id),
            tuple(sorted((*state.activated_ids, capability_id))),
        )


__all__ = (
    "CatalogRunToolExposure",
    "ExecutableToolRecord",
    "RunToolExposurePort",
    "RunToolExposureState",
    "RuntimeCapabilityKind",
    "RuntimeCapabilityRecord",
    "RuntimeToolCatalog",
    "RuntimeToolCatalogError",
    "RuntimeToolCatalogSnapshot",
    "RuntimeToolDescription",
    "RuntimeToolDescriptor",
    "RuntimeToolSearchPage",
    "SkillResourceRecord",
    "ToolActivationReceipt",
    "ToolExposureMode",
    "WorkflowProfileRecord",
)

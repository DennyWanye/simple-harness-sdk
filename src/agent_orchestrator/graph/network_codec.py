# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Version-pinned NetworkDocument structural codec."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, TypeAlias, cast

from simple_harness.contracts import canonical_json, validate_json_value
from simple_harness.contracts.json import JsonValue

from ..contracts.htn import (
    DataRequirement,
    MethodInstanceDraft,
    MethodInstanceId,
    MissionRef,
    ObligationCoverage,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    OrderConstraint,
    PlanRevision,
    TaskSemanticBindingV1,
    TypedEdge,
)
from ..contracts.models import ContractError
from ..contracts.semantic_base import TypedRef, TypedRefKind
from .task_network import TaskNetworkSnapshot

_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_MAX_OBJECT_BYTES = 8 * 1024 * 1024
_MAX_OBJECTS = 16384
_MAX_ROOTS = 256
_MAX_SET_ITEMS = 4096
_JS_MAX = 9007199254740991
_KINDS = frozenset(
    {
        "occurrence",
        "task_semantics",
        "method_instance",
        "order_constraint",
        "data_requirement",
        "typed_edge",
        "obligation_coverage",
    }
)
_FIELDS = frozenset(
    {
        "schema_version",
        "mission_id",
        "revision",
        "codec_manifest_hash",
        "requirements_ref",
        "root_occurrence_ids",
        "adopted_instance_ids",
        "required_obligation_ids",
        "objects",
    }
)
_O_FIELDS = frozenset({"kind", "identity", "sha256", "canonical_json"})
_R_FIELDS = frozenset({"kind", "id", "revision", "content_hash"})
CodecValue: TypeAlias = (
    OccurrenceSpec
    | TaskSemanticBindingV1
    | MethodInstanceDraft
    | OrderConstraint
    | DataRequirement
    | TypedEdge
    | ObligationCoverage
)
JsonObject: TypeAlias = dict[str, JsonValue]


def _bad(m: str) -> NoReturn:
    raise ContractError(f"NETWORK_DOCUMENT_INVALID: {m}")


def _pairs(p: list[tuple[str, object]]) -> dict[str, object]:
    d: dict[str, object] = {}
    for k, v in p:
        if k in d:
            _bad(f"duplicate JSON key {k!r}")
        d[k] = v
    return d


def _obj(v: object, label: str) -> JsonObject:
    if not isinstance(v, dict) or not all(isinstance(k, str) for k in v):
        _bad(f"{label} must be a JSON object")
    try:
        validate_json_value(v, path=label)
    except Exception as e:
        _bad(f"{label} is not valid contract JSON: {type(e).__name__}")
    return cast(JsonObject, v)


def _canon(v: Mapping[str, object]) -> str:
    try:
        s = canonical_json(_obj(dict(v), "document"))
    except (TypeError, ValueError, UnicodeError) as e:
        _bad(f"document cannot be canonicalized: {type(e).__name__}")
    try:
        size = len(s.encode())
    except UnicodeEncodeError as e:
        _bad(f"document cannot be canonicalized: {type(e).__name__}")
    if size > _MAX_DOCUMENT_BYTES:
        _bad("document exceeds 16 MiB")
    return s


def _hex(v: object, label: str) -> str:
    if not isinstance(v, str) or len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
        _bad(f"{label} must be a lowercase SHA-256")
    return v


def _int(v: object, label: str, min: int = 0) -> int:
    if type(v) is not int or not min <= v <= _JS_MAX:
        _bad(f"{label} must be an integer in range")
    return v


def _id(v: object, label: str) -> str:
    if not isinstance(v, str) or not v or len(v) > 512:
        _bad(f"{label} must be a nonempty identifier")
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        _bad(f"{label} must be valid UTF-8")
    return v


def _manifest() -> str:
    try:
        raw = json.loads(
            Path(__file__).with_name("network_codec_manifest_v5.json").read_text(),
            object_pairs_hook=_pairs,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        _bad(f"registered codec manifest is unreadable: {e}")
    p = _obj(raw, "registered codec manifest")
    if (
        set(p) != {"manifest_version", "codecs", "supporting_sources"}
        or p["manifest_version"] != 5
        or not isinstance(p["codecs"], list)
        or not isinstance(p["supporting_sources"], list)
    ):
        _bad("registered codec manifest has an invalid shape")
    entries = [*p["codecs"], *p["supporting_sources"]]
    if (
        len(p["codecs"]) != 7
        or {x.get("kind") for x in p["codecs"] if isinstance(x, dict)} != _KINDS
    ):
        _bad("registered codec manifest must declare exactly seven kinds")
    root = Path(__file__).parents[2]
    for source_entry in entries:
        valid_shape = isinstance(source_entry, dict) and set(source_entry) in (
            {"component", "kind", "source_path", "source_sha256"},
            {"component", "source_path", "source_sha256"},
        )
        if not valid_shape:
            _bad("registered codec manifest source has an invalid shape")
        entry = cast(JsonObject, source_entry)
        path = entry["source_path"]
        expected = _hex(entry["source_sha256"], "manifest source_sha256")
        if not isinstance(path, str):
            _bad("registered codec manifest source path is invalid")
        try:
            actual = hashlib.sha256((root / path).read_bytes()).hexdigest()
        except OSError as x:
            _bad(f"registered codec source is unreadable: {path}: {x}")
        if actual != expected:
            _bad(f"registered codec source hash changed: {path}")
    return hashlib.sha256(canonical_json(p).encode()).hexdigest()


CODEC_MANIFEST_HASH = _manifest()


@dataclass(frozen=True, slots=True, kw_only=True)
class NetworkObjectV1:
    kind: str
    identity: str
    sha256: str
    canonical_json: str

    def __post_init__(self) -> None:
        _id(self.identity, "object.identity")
        _hex(self.sha256, "object.sha256")
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            _bad("object.kind is unsupported")
        if not isinstance(self.canonical_json, str):
            _bad("object.canonical_json must be a string")
        try:
            size = len(self.canonical_json.encode("utf-8"))
        except UnicodeEncodeError as error:
            _bad(f"object.canonical_json is invalid UTF-8: {type(error).__name__}")
        if size > _MAX_OBJECT_BYTES:
            _bad("object.canonical_json exceeds 8 MiB")
        _validated_object(self.kind, self.identity, self.sha256, self.canonical_json)

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "sha256": self.sha256,
            "canonical_json": self.canonical_json,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NetworkDocumentV1:
    """Immutable wire carrier with validated objects and explicit metadata.

    Cross-object graph references are validated by ``decode``; possession of
    this value alone is never a persisted-history or execution proof.
    """

    mission_id: str
    revision: int
    codec_manifest_hash: str
    requirements_ref: TypedRef
    root_occurrence_ids: tuple[str, ...]
    adopted_instance_ids: tuple[str, ...]
    required_obligation_ids: tuple[str, ...]
    objects: tuple[NetworkObjectV1, ...]

    def __post_init__(self) -> None:
        _id(self.mission_id, "mission_id")
        _int(self.revision, "revision")
        _hex(self.codec_manifest_hash, "codec_manifest_hash")
        if self.codec_manifest_hash != _manifest():
            _bad("codec manifest is not explicitly supported")
        _ref(self.requirements_ref)
        for name, bound in (
            ("root_occurrence_ids", _MAX_ROOTS),
            ("adopted_instance_ids", _MAX_SET_ITEMS),
            ("required_obligation_ids", _MAX_SET_ITEMS),
        ):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)):
                _bad(f"{name} must be a bounded sequence")
            identifiers = tuple(_id(value, name) for value in values)
            object.__setattr__(self, name, _set(identifiers, name, bound))
        if not isinstance(self.objects, (tuple, list)) or len(self.objects) > _MAX_OBJECTS:
            _bad("objects must be a bounded sequence")
        if not all(isinstance(value, NetworkObjectV1) for value in self.objects):
            _bad("objects must contain NetworkObjectV1 values")
        objects = tuple(sorted(self.objects, key=lambda value: (value.kind, value.identity)))
        if len({(value.kind, value.identity) for value in objects}) != len(objects):
            _bad("objects have duplicate identity")
        object.__setattr__(self, "objects", objects)
        _canon(self.to_json())

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "revision": self.revision,
            "codec_manifest_hash": self.codec_manifest_hash,
            "requirements_ref": cast(JsonValue, self.requirements_ref.to_json()),
            "root_occurrence_ids": list(self.root_occurrence_ids),
            "adopted_instance_ids": list(self.adopted_instance_ids),
            "required_obligation_ids": list(self.required_obligation_ids),
            "objects": [x.to_json() for x in self.objects],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DecodedNetworkDocument:
    document: NetworkDocumentV1
    snapshot: TaskNetworkSnapshot


def _json(x: CodecValue) -> JsonObject:
    return _obj(x.to_json(), "original codec output")


def _decode(k: str, v: JsonObject) -> CodecValue:
    if k == "occurrence":
        return OccurrenceSpec.from_json(v)
    if k == "task_semantics":
        return TaskSemanticBindingV1.from_json(v)
    if k == "method_instance":
        return MethodInstanceDraft.from_json(v)
    if k == "order_constraint":
        return OrderConstraint.from_json(v)
    if k == "data_requirement":
        return DataRequirement.from_json(v)
    if k == "typed_edge":
        return TypedEdge.from_json(v)
    if k == "obligation_coverage":
        return ObligationCoverage.from_json(v)
    _bad(f"unknown codec kind {k!r}")


def _identity(k: str, x: CodecValue) -> str:
    if k == "occurrence" and isinstance(x, OccurrenceSpec):
        return str(x.occurrence_id)
    if k == "task_semantics" and isinstance(x, TaskSemanticBindingV1):
        return canonical_json([str(x.task_id), int(x.contract_revision)])
    if k == "method_instance" and isinstance(x, MethodInstanceDraft):
        return str(x.instance_id)
    if k == "order_constraint" and isinstance(x, OrderConstraint):
        return canonical_json([str(x.before), str(x.after)])
    if k == "data_requirement" and isinstance(x, DataRequirement):
        return str(x.requirement_id)
    if k in {"typed_edge", "obligation_coverage"} and isinstance(
        x, (TypedEdge, ObligationCoverage)
    ):
        return hashlib.sha256(canonical_json(_json(x)).encode()).hexdigest()
    _bad(f"{k} decoded to the wrong original type")


def _one(k: str, x: CodecValue) -> NetworkObjectV1:
    s = canonical_json(_json(x))
    try:
        content = s.encode("utf-8")
    except UnicodeEncodeError:
        _bad(f"{k} canonical_json must be valid UTF-8")
    if len(content) > _MAX_OBJECT_BYTES:
        _bad(f"{k} canonical_json exceeds 8 MiB")
    return NetworkObjectV1(
        kind=k,
        identity=_identity(k, x),
        sha256=hashlib.sha256(s.encode()).hexdigest(),
        canonical_json=s,
    )


def _validated_object(kind: str, identity: str, digest: str, text: str) -> CodecValue:
    try:
        payload = _obj(
            json.loads(
                text, object_pairs_hook=_pairs, parse_constant=lambda value: _bad("non-finite JSON")
            ),
            "canonical_json",
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        _bad(f"object.canonical_json is invalid JSON: {type(error).__name__}")
    if canonical_json(payload) != text:
        _bad("object.canonical_json is not canonical")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        _bad("object.sha256 does not match canonical_json")
    try:
        decoded = _decode(kind, payload)
    except Exception as error:
        _bad(f"object failed original {kind} decoder: {type(error).__name__}")
    if canonical_json(_json(decoded)) != text:
        _bad("object original decoder does not preserve canonical_json")
    if _identity(kind, decoded) != identity:
        _bad("object identity is invalid")
    return decoded


def _set(v: tuple[str, ...], label: str, n: int) -> tuple[str, ...]:
    if len(v) > n or len(set(v)) != len(v):
        _bad(f"{label} has duplicates or exceeds its bound")
    return tuple(sorted(v))


def _ref(x: object) -> TypedRef:
    if not isinstance(x, TypedRef) or x.kind is not TypedRefKind.REQUIREMENTS:
        _bad("requirements_ref must be a TypedRef of kind requirements")
    raw = x.to_json()
    if set(raw) != _R_FIELDS:
        _bad("requirements_ref has unknown or missing fields")
    _id(raw["id"], "requirements_ref.id")
    _int(raw["revision"], "requirements_ref.revision")
    _hex(raw["content_hash"], "requirements_ref.content_hash")
    return x


def encode(s: TaskNetworkSnapshot, requirements_ref: TypedRef) -> NetworkDocumentV1:
    if not isinstance(s, TaskNetworkSnapshot):
        _bad("encode requires TaskNetworkSnapshot")
    src: list[tuple[str, CodecValue]] = [
        *(("occurrence", x) for x in s.occurrences),
        *(("task_semantics", x) for x in s.task_bindings),
        *(("method_instance", x) for x in s.method_instances),
        *(("order_constraint", x) for x in s.order_constraints),
        *(("data_requirement", x) for x in s.data_requirements),
        *(("typed_edge", x) for x in s.typed_edges),
        *(("obligation_coverage", x) for x in s.obligation_coverage),
    ]
    objs = tuple(sorted((_one(k, x) for k, x in src), key=lambda x: (x.kind, x.identity)))
    if len(objs) > _MAX_OBJECTS or len({(x.kind, x.identity) for x in objs}) != len(objs):
        _bad("objects have duplicate identity or exceed their bound")
    d = NetworkDocumentV1(
        mission_id=_id(str(s.mission_id), "mission_id"),
        revision=_int(int(s.plan_revision), "revision"),
        codec_manifest_hash=_manifest(),
        requirements_ref=_ref(requirements_ref),
        root_occurrence_ids=_set(
            tuple(map(str, s.root_occurrence_ids)), "root_occurrence_ids", _MAX_ROOTS
        ),
        adopted_instance_ids=_set(
            tuple(map(str, s.adopted_instance_ids)), "adopted_instance_ids", _MAX_SET_ITEMS
        ),
        required_obligation_ids=_set(
            tuple(map(str, s.required_obligations)), "required_obligation_ids", _MAX_SET_ITEMS
        ),
        objects=objs,
    )
    _canon(d.to_json())
    return d


def decode(
    v: Mapping[str, object], *, expected_manifest: str | None = None
) -> DecodedNetworkDocument:
    if not isinstance(v, Mapping) or set(v) != _FIELDS:
        _bad("document has unknown or missing fields")
    _canon(v)
    if _int(v["schema_version"], "schema_version") != 1:
        _bad("schema_version must be 1")
    manifest = _hex(v["codec_manifest_hash"], "codec_manifest_hash")
    registered = _manifest()
    if manifest != registered or (expected_manifest is not None and manifest != expected_manifest):
        _bad("codec manifest is not explicitly supported")
    rawref = v["requirements_ref"]
    if not isinstance(rawref, Mapping) or set(rawref) != _R_FIELDS:
        _bad("requirements_ref has unknown or missing fields")
    try:
        ref = _ref(TypedRef.from_json(dict(rawref), "requirements_ref"))
    except ContractError:
        raise
    except Exception as e:
        _bad(f"requirements_ref is invalid: {type(e).__name__}")
    raw = v["objects"]
    if not isinstance(raw, list) or len(raw) > _MAX_OBJECTS:
        _bad("objects must be a bounded array")
    groups: dict[str, list[CodecValue]] = {k: [] for k in _KINDS}
    parsed: list[NetworkObjectV1] = []
    seen: set[tuple[str, str]] = set()
    for i, r in enumerate(raw):
        if not isinstance(r, Mapping) or set(r) != _O_FIELDS:
            _bad(f"objects[{i}] has unknown or missing fields")
        k = r["kind"]
        identity = _id(r["identity"], f"objects[{i}].identity")
        digest = _hex(r["sha256"], f"objects[{i}].sha256")
        text = r["canonical_json"]
        if not isinstance(k, str) or k not in _KINDS or not isinstance(text, str):
            _bad(f"objects[{i}] is invalid")
        if len(text.encode()) > _MAX_OBJECT_BYTES:
            _bad(f"objects[{i}].canonical_json exceeds 8 MiB")
        try:
            j = _obj(
                json.loads(
                    text, object_pairs_hook=_pairs, parse_constant=lambda x: _bad("non-finite JSON")
                ),
                "canonical_json",
            )
        except (json.JSONDecodeError, UnicodeError) as e:
            _bad(f"objects[{i}].canonical_json is invalid JSON: {e}")
        if canonical_json(j) != text:
            _bad(f"objects[{i}].canonical_json is not canonical")
        if hashlib.sha256(text.encode()).hexdigest() != digest:
            _bad(f"objects[{i}].sha256 does not match canonical_json")
        try:
            x = _decode(k, j)
        except Exception as e:
            _bad(f"objects[{i}] failed original {k} decoder: {type(e).__name__}")
        if canonical_json(_json(x)) != text:
            _bad(f"objects[{i}] original decoder does not preserve canonical_json")
        if _identity(k, x) != identity or (k, identity) in seen:
            _bad(f"objects[{i}] identity is invalid or duplicate")
        seen.add((k, identity))
        groups[k].append(x)
        parsed.append(
            NetworkObjectV1(kind=k, identity=identity, sha256=digest, canonical_json=text)
        )
    if tuple((x.kind, x.identity) for x in parsed) != tuple(
        sorted((x.kind, x.identity) for x in parsed)
    ):
        _bad("objects must be sorted by kind and identity")

    def ids(f: str, n: int) -> tuple[str, ...]:
        q = v[f]
        if not isinstance(q, list):
            _bad(f"{f} must be an array")
        z = tuple(_id(x, f"{f}[]") for x in q)
        if z != _set(z, f, n):
            _bad(f"{f} must be sorted and unique")
        return z

    roots = ids("root_occurrence_ids", _MAX_ROOTS)
    adopted = ids("adopted_instance_ids", _MAX_SET_ITEMS)
    obligations = ids("required_obligation_ids", _MAX_SET_ITEMS)
    try:
        s = TaskNetworkSnapshot(
            MissionRef(_id(v["mission_id"], "mission_id")),
            PlanRevision(_int(v["revision"], "revision")),
            tuple(cast(list[OccurrenceSpec], groups["occurrence"])),
            tuple(cast(list[TaskSemanticBindingV1], groups["task_semantics"])),
            tuple(cast(list[MethodInstanceDraft], groups["method_instance"])),
            tuple(MethodInstanceId(x) for x in adopted),
            tuple(OccurrenceId(x) for x in roots),
            tuple(cast(list[OrderConstraint], groups["order_constraint"])),
            tuple(cast(list[DataRequirement], groups["data_requirement"])),
            tuple(cast(list[TypedEdge], groups["typed_edge"])),
            tuple(cast(list[ObligationCoverage], groups["obligation_coverage"])),
            tuple(ObligationId(x) for x in obligations),
        )
    except Exception as e:
        _bad(f"TaskNetworkSnapshot construction failed: {type(e).__name__}: {e}")
    return DecodedNetworkDocument(
        document=NetworkDocumentV1(
            mission_id=_id(v["mission_id"], "mission_id"),
            revision=_int(v["revision"], "revision"),
            codec_manifest_hash=manifest,
            requirements_ref=ref,
            root_occurrence_ids=roots,
            adopted_instance_ids=adopted,
            required_obligation_ids=obligations,
            objects=tuple(parsed),
        ),
        snapshot=s,
    )


__all__ = (
    "CODEC_MANIFEST_HASH",
    "DecodedNetworkDocument",
    "NetworkDocumentV1",
    "NetworkObjectV1",
    "decode",
    "encode",
)

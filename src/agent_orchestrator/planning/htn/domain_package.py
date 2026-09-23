"""Data-only H5 planning domain packages and their fail-closed installer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...contracts.models import ContractError
from ...contracts.semantic_base import content_hash_of


def _ids(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ContractError(f"{name} must be a sequence")
    result = tuple(str(value) for value in values)
    if any(not value.strip() for value in result):
        raise ContractError(f"{name} contains blank identifier")
    if len(set(result)) != len(result):
        raise ContractError(f"{name} contains duplicate identifiers")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class ObserverRegistrationV1:
    observer_id: str
    predicate_refs: tuple[str, ...]
    capability: str

    def __post_init__(self) -> None:
        if not self.observer_id.strip() or not self.capability.strip():
            raise ContractError("observer id and capability must be non-empty")
        object.__setattr__(self, "predicate_refs", _ids(self.predicate_refs, "predicate_refs"))

    def to_json(self) -> dict[str, Any]:
        return {
            "observer_id": self.observer_id,
            "predicate_refs": list(self.predicate_refs),
            "capability": self.capability,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ObserverRegistrationV1:
        return cls(
            str(value["observer_id"]), tuple(value["predicate_refs"]), str(value["capability"])
        )


@dataclass(frozen=True, slots=True)
class OperatorRegistrationV1:
    operator_id: str
    task_type_ref: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.operator_id.strip() or not self.task_type_ref.strip():
            raise ContractError("operator id and task type ref must be non-empty")
        object.__setattr__(self, "capabilities", _ids(self.capabilities, "operator capabilities"))

    def to_json(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "task_type_ref": self.task_type_ref,
            "capabilities": list(self.capabilities),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> OperatorRegistrationV1:
        return cls(
            str(value["operator_id"]),
            str(value["task_type_ref"]),
            tuple(value.get("capabilities", ())),
        )


@dataclass(frozen=True, slots=True)
class PlanningDomainPackageV1:
    domain_id: str
    package_version: int
    schemas: tuple[str, ...]
    predicates: tuple[str, ...]
    task_types: tuple[str, ...]
    methods: tuple[str, ...]
    observers: tuple[ObserverRegistrationV1, ...]
    operators: tuple[OperatorRegistrationV1, ...]
    capabilities: tuple[str, ...]
    content_hash: str = ""
    definitions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not self.domain_id.strip()
            or not isinstance(self.package_version, int)
            or self.package_version < 1
        ):
            raise ContractError("invalid domain package identity")
        for field in ("schemas", "predicates", "task_types", "methods", "capabilities"):
            object.__setattr__(self, field, _ids(getattr(self, field), field))
        observers = tuple(self.observers)
        operators = tuple(self.operators)
        if len({item.observer_id for item in observers}) != len(observers):
            raise ContractError("duplicate observer id")
        if len({item.operator_id for item in operators}) != len(operators):
            raise ContractError("duplicate operator id")
        predicates = set(self.predicates)
        tasks = set(self.task_types)
        caps = set(self.capabilities)
        for observer in observers:
            if any(ref not in predicates for ref in observer.predicate_refs):
                raise ContractError("unknown predicate in observer registration")
            if observer.capability not in caps:
                raise ContractError("observer capability is not declared by package")
        for operator in operators:
            if operator.task_type_ref not in tasks:
                raise ContractError("unknown task type in operator registration")
            if any(cap not in caps for cap in operator.capabilities):
                raise ContractError("operator capability is not declared by package")
        expected = {f"{kind}:{ref}" for kind in ("schemas", "predicates", "task_types", "methods")
                    for ref in getattr(self, kind)}
        if self.definitions and set(self.definitions) != expected:
            raise ContractError("domain definitions do not match the declared inventory")
        import json
        from types import MappingProxyType
        from simple_harness.contracts import canonical_json
        # Detach caller-owned dictionaries; installation checks the digest again.
        object.__setattr__(self, "definitions", MappingProxyType(json.loads(canonical_json({str(k): dict(v) for k, v in self.definitions.items()}))))
        calculated = content_hash_of(self._body())
        if self.content_hash and self.content_hash != calculated:
            raise ContractError("domain package content_hash does not match canonical content")
        object.__setattr__(self, "content_hash", calculated)

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": "planning-domain-package-v1",
            "domain_id": self.domain_id,
            "package_version": self.package_version,
            "schemas": list(self.schemas),
            "predicates": list(self.predicates),
            "task_types": list(self.task_types),
            "methods": list(self.methods),
            "observers": [item.to_json() for item in self.observers],
            "operators": [item.to_json() for item in self.operators],
            **({"definitions": dict(self.definitions)} if self.definitions else {}),
            "capabilities": list(self.capabilities),
        }

    def to_json(self) -> dict[str, Any]:
        return {**self._body(), "content_hash": self.content_hash}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> PlanningDomainPackageV1:
        if value.get("schema_version") != "planning-domain-package-v1":
            raise ContractError("unsupported domain package schema")
        return cls(
            domain_id=str(value["domain_id"]),
            package_version=int(value["package_version"]),
            schemas=tuple(value.get("schemas", ())),
            predicates=tuple(value.get("predicates", ())),
            task_types=tuple(value.get("task_types", ())),
            methods=tuple(value.get("methods", ())),
            observers=tuple(
                ObserverRegistrationV1.from_json(item) for item in value.get("observers", ())
            ),
            operators=tuple(
                OperatorRegistrationV1.from_json(item) for item in value.get("operators", ())
            ),
            capabilities=tuple(value.get("capabilities", ())),
            content_hash=str(value.get("content_hash", "")),
            definitions=dict(value.get("definitions", {})),
        )


@dataclass(frozen=True, slots=True)
class DomainInstallReceipt:
    status: str
    domain_id: str
    package_hash: str


class DomainPackageInstaller:
    def __init__(self) -> None:
        self._installed: dict[tuple[str, int], PlanningDomainPackageV1] = {}

    def install(self, package: PlanningDomainPackageV1) -> DomainInstallReceipt:
        if not isinstance(package, PlanningDomainPackageV1):
            raise ContractError("install expects PlanningDomainPackageV1")
        if not package.definitions:
            raise ContractError("install requires actual domain definitions, not an ID-only manifest")
        if content_hash_of(package._body()) != package.content_hash:
            raise ContractError("domain definitions changed after the package was frozen")
        key = (package.domain_id, package.package_version)
        existing = self._installed.get(key)
        if existing is not None:
            if existing.content_hash != package.content_hash:
                raise ContractError("conflicting domain package already installed")
            return DomainInstallReceipt(
                "ALREADY_INSTALLED", package.domain_id, package.content_hash
            )
        self._installed[key] = package
        return DomainInstallReceipt("INSTALLED", package.domain_id, package.content_hash)

    def get(self, domain_id: str, package_version: int = 1) -> PlanningDomainPackageV1 | None:
        package = self._installed.get((domain_id, package_version))
        if package is not None and content_hash_of(package._body()) != package.content_hash:
            raise ContractError("installed domain definitions changed after freezing")
        return package


__all__ = (
    "DomainInstallReceipt",
    "DomainPackageInstaller",
    "ObserverRegistrationV1",
    "OperatorRegistrationV1",
    "PlanningDomainPackageV1",
)

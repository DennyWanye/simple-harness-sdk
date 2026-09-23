# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H4 repair protocol.

This module deliberately contains the *decision boundary* only.  It does not
compile a repair or write a plan.  A request is made from a durable trigger,
the impact set is calculated from program state, and a model may then choose
one of the closed repair actions.  In particular, ``affected_hints`` are
diagnostic input; they never determine the impact set.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from simple_harness.contracts import canonical_json

from ...contracts.models import ContractError
from ...contracts.semantic_base import (
    TypedRef,
    identifier,
    index,
    json_value,
    text,
)

REPAIR_REQUEST_SCHEMA_VERSION = 1
REPAIR_DECISION_SCHEMA_VERSION = 1


class RepairActionType(StrEnum):
    """The complete H4 action vocabulary (V2 §50)."""

    RETRY_SAME_METHOD = "RETRY_SAME_METHOD"
    REFINE_DEEPER = "REFINE_DEEPER"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    REPLACE_METHOD = "REPLACE_METHOD"
    REBIND_INPUT = "REBIND_INPUT"
    BIND_EXISTING_GOAL = "BIND_EXISTING_GOAL"
    CANCEL_BRANCH = "CANCEL_BRANCH"
    REQUEST_COMPENSATION = "REQUEST_COMPENSATION"
    DECLARE_RUNTIME_BLOCKED = "DECLARE_RUNTIME_BLOCKED"
    ESCALATE = "ESCALATE"
    PROPOSE_SUCCESSOR = "PROPOSE_SUCCESSOR"


class RepairTriggerSource(StrEnum):
    """The five events that can open one repair request."""

    WORKER_REJECT = "WORKER_REJECT"
    VERIFIER_ACCEPTANCE_REJECT = "VERIFIER_ACCEPTANCE_REJECT"
    EVIDENCE_INVALIDATION = "EVIDENCE_INVALIDATION"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    REQUIREMENTS_UPDATE = "REQUIREMENTS_UPDATE"

    # Readable aliases used by callers that name the producer rather than the
    # protocol row.  They are aliases, so the wire vocabulary remains closed.
    VERIFIER_REJECT = VERIFIER_ACCEPTANCE_REJECT
    ACCEPTANCE_REJECT = VERIFIER_ACCEPTANCE_REJECT


RepairTrigger = RepairTriggerSource


class RepairCause(StrEnum):
    IMPLEMENTATION_DEFECT = "IMPLEMENTATION_DEFECT"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    METHOD_INAPPLICABLE = "METHOD_INAPPLICABLE"
    INPUT_STALE = "INPUT_STALE"
    REQUIREMENTS_CHANGED = "REQUIREMENTS_CHANGED"
    PERMISSION_MISSING = "PERMISSION_MISSING"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    VERIFICATION_REJECTED = "VERIFICATION_REJECTED"
    COMPOSITION_FAILED = "COMPOSITION_FAILED"
    EXTERNAL_OUTCOME_UNKNOWN = "EXTERNAL_OUTCOME_UNKNOWN"
    BUDGET_LIMIT = "BUDGET_LIMIT"
    PLANNING_BOUND_REACHED = "PLANNING_BOUND_REACHED"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RepairDiagnosis:
    """Structured diagnosis carried by the H4 AST.

    ``cause`` is the program-checkable part.  ``detail`` and ``confidence`` are
    advisory model context and cannot widen the impact calculation.
    """

    cause: RepairCause = RepairCause.UNKNOWN
    detail: str = ""
    confidence: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "cause", RepairCause(self.cause))
        if self.detail:
            object.__setattr__(self, "detail", text(self.detail, "repair_diagnosis.detail"))
        if self.confidence:
            object.__setattr__(
                self, "confidence", text(self.confidence, "repair_diagnosis.confidence")
            )

    def to_json(self) -> dict[str, str]:
        return {"cause": str(self.cause), "detail": self.detail, "confidence": self.confidence}


class RepairDecisionStatus(StrEnum):
    ADMITTED = "ADMITTED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"
    REQUEST_HUMAN = "REQUEST_HUMAN"


def _strings(value: Iterable[object], name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ContractError(f"{name} must be a list")
    result = tuple(identifier(item, f"{name}[]") for item in value)
    if len(set(result)) != len(result):
        raise ContractError(f"{name} must not contain duplicates")
    return result


def _ref_json(value: object, name: str) -> dict[str, Any] | str:
    if isinstance(value, TypedRef):
        return value.to_json()
    if isinstance(value, Mapping):
        return TypedRef.from_json(value, name).to_json()
    return identifier(value, name)


def _refs(value: Sequence[object], name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)):
        raise ContractError(f"{name} must be a list")
    result: list[object] = []
    seen: set[str] = set()
    for item in value:
        normalized: object = (
            TypedRef.from_json(item, f"{name}[]")
            if isinstance(item, Mapping)
            else identifier(item, f"{name}[]")
        )
        key = canonical_json(
            cast(Any, normalized.to_json() if isinstance(normalized, TypedRef) else normalized)
        )
        if key in seen:
            raise ContractError(f"{name} must not contain duplicates")
        seen.add(key)
        result.append(normalized)
    return tuple(result)


def _ref_id(value: object) -> str:
    return value.id if isinstance(value, TypedRef) else str(value)


@dataclass(frozen=True, slots=True)
class RepairRequestV1:
    """Durable, trigger-normalized input to a repair round."""

    trigger_source: RepairTriggerSource
    trigger_refs: tuple[object, ...] = ()
    mission_id: str = "mission"
    plan_revision: int = 0
    diagnosis: RepairCause = RepairCause.UNKNOWN
    affected_hints: tuple[str, ...] = ()
    context: Mapping[str, Any] = field(default_factory=dict)
    repair_round: int = 0
    request_id: str | None = None
    schema_version: int = REPAIR_REQUEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            index(self.schema_version, "repair_request.schema_version", minimum=1),
        )
        if self.schema_version != REPAIR_REQUEST_SCHEMA_VERSION:
            raise ContractError("repair_request.schema_version is unsupported")
        object.__setattr__(self, "trigger_source", RepairTriggerSource(self.trigger_source))
        object.__setattr__(
            self, "trigger_refs", _refs(self.trigger_refs, "repair_request.trigger_refs")
        )
        object.__setattr__(
            self, "mission_id", identifier(self.mission_id, "repair_request.mission_id")
        )
        object.__setattr__(
            self, "plan_revision", index(self.plan_revision, "repair_request.plan_revision")
        )
        object.__setattr__(self, "diagnosis", RepairCause(self.diagnosis))
        object.__setattr__(
            self, "affected_hints", _strings(self.affected_hints, "repair_request.affected_hints")
        )
        object.__setattr__(
            self,
            "context",
            MappingProxyType(dict(json_value(self.context, "repair_request.context"))),
        )
        object.__setattr__(
            self, "repair_round", index(self.repair_round, "repair_request.repair_round")
        )
        canonical = self._identity_payload()
        expected = hashlib.sha256(canonical_json(canonical).encode("utf-8")).hexdigest()
        if self.request_id is not None and self.request_id != expected:
            raise ContractError("repair_request.request_id does not match request content")
        object.__setattr__(self, "request_id", expected)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trigger_source": str(self.trigger_source),
            "trigger_refs": [
                _ref_json(item, "repair_request.trigger_refs[]") for item in self.trigger_refs
            ],
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "diagnosis": str(self.diagnosis),
            "affected_hints": list(self.affected_hints),
            "context": dict(self.context),
            "repair_round": self.repair_round,
        }

    @property
    def idempotency_key(self) -> str:
        return self.request_id or ""

    def to_json(self) -> dict[str, Any]:
        payload = self._identity_payload()
        payload["request_id"] = self.request_id
        payload["idempotency_key"] = self.idempotency_key
        return payload

    @classmethod
    def from_json(cls, value: object, name: str = "repair_request") -> RepairRequestV1:
        if not isinstance(value, Mapping):
            raise ContractError(f"{name} must be an object")
        data = dict(value)
        required = {
            "schema_version",
            "trigger_source",
            "trigger_refs",
            "mission_id",
            "plan_revision",
            "diagnosis",
            "affected_hints",
            "context",
            "repair_round",
        }
        missing = required - data.keys()
        if missing:
            raise ContractError(f"{name} missing fields: {sorted(missing)}")
        allowed = required | {"request_id", "idempotency_key"}
        unknown = set(data) - allowed
        if unknown:
            raise ContractError(f"{name} has unknown fields: {sorted(unknown)}")
        result = cls(
            schema_version=data["schema_version"],
            trigger_source=data["trigger_source"],
            trigger_refs=data["trigger_refs"],
            mission_id=data["mission_id"],
            plan_revision=data["plan_revision"],
            diagnosis=data["diagnosis"],
            affected_hints=data["affected_hints"],
            context=data["context"],
            repair_round=data["repair_round"],
            request_id=data.get("request_id"),
        )
        if data.get("idempotency_key") not in (None, result.idempotency_key):
            raise ContractError(f"{name}.idempotency_key does not match request_id")
        return result

    @classmethod
    def from_trigger(
        cls,
        trigger_source: RepairTriggerSource | str,
        *,
        trigger_refs: Sequence[object] = (),
        **kwargs: Any,
    ) -> RepairRequestV1:
        return cls(
            trigger_source=RepairTriggerSource(trigger_source),
            trigger_refs=tuple(trigger_refs),
            **kwargs,
        )

    create = from_trigger

    @classmethod
    def from_worker_reject(cls, **kwargs: Any) -> RepairRequestV1:
        return cls.from_trigger(RepairTriggerSource.WORKER_REJECT, **kwargs)

    @classmethod
    def from_verifier_acceptance_reject(cls, **kwargs: Any) -> RepairRequestV1:
        return cls.from_trigger(RepairTriggerSource.VERIFIER_ACCEPTANCE_REJECT, **kwargs)

    @classmethod
    def from_evidence_invalidation(cls, **kwargs: Any) -> RepairRequestV1:
        return cls.from_trigger(RepairTriggerSource.EVIDENCE_INVALIDATION, **kwargs)

    @classmethod
    def from_runtime_unavailable(cls, **kwargs: Any) -> RepairRequestV1:
        return cls.from_trigger(RepairTriggerSource.RUNTIME_UNAVAILABLE, **kwargs)

    @classmethod
    def from_requirements_update(cls, **kwargs: Any) -> RepairRequestV1:
        return cls.from_trigger(RepairTriggerSource.REQUIREMENTS_UPDATE, **kwargs)


@dataclass(frozen=True, slots=True)
class ImpactAnalysis:
    """Program-computed impact partition for one repair request."""

    retained: tuple[str, ...] = ()
    revalidate: tuple[str, ...] = ()
    supersede: tuple[str, ...] = ()
    new_work: tuple[str, ...] = ()
    unresolved_operations: tuple[str, ...] = ()
    unknown_coverage: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "retained",
            "revalidate",
            "supersede",
            "new_work",
            "unresolved_operations",
            "unknown_coverage",
        ):
            object.__setattr__(self, name, _strings(getattr(self, name), f"impact.{name}"))
        groups = [set(self.retained), set(self.revalidate), set(self.supersede)]
        if any(groups[i] & groups[j] for i in range(3) for j in range(i + 1, 3)):
            raise ContractError("impact retained/revalidate/supersede must be disjoint")

    def to_json(self) -> dict[str, Any]:
        return {
            name: list(getattr(self, name))
            for name in (
                "retained",
                "revalidate",
                "supersede",
                "new_work",
                "unresolved_operations",
                "unknown_coverage",
            )
        }

    @classmethod
    def from_json(cls, value: object, name: str = "impact") -> ImpactAnalysis:
        if not isinstance(value, Mapping):
            raise ContractError(f"{name} must be an object")
        keys = {
            "retained",
            "revalidate",
            "supersede",
            "new_work",
            "unresolved_operations",
            "unknown_coverage",
        }
        if set(value) != keys:
            raise ContractError(f"{name} fields differ")
        return cls(**{key: value[key] for key in keys})

    @classmethod
    def from_program_state(
        cls, request: RepairRequestV1 | None = None, **kwargs: Any
    ) -> ImpactAnalysis:
        return analyze_impact(request, **kwargs)


def _adjacency(value: Mapping[str, Iterable[str]] | None) -> dict[str, set[str]]:
    if value is None:
        return {}
    result: dict[str, set[str]] = {}
    for key, values in value.items():
        result[str(key)] = {str(item) for item in values}
    return result


def analyze_impact(
    request: RepairRequestV1 | None = None,
    *,
    trigger_refs: Sequence[object] = (),
    affected_hints: Sequence[str] = (),
    reverse_data: Mapping[str, Iterable[str]] | None = None,
    reverse_support: Mapping[str, Iterable[str]] | None = None,
    method_membership: Mapping[str, Iterable[str]] | None = None,
    demand_refs: Mapping[str, Iterable[str]] | None = None,
    acceptance_refs: Mapping[str, Iterable[str]] | None = None,
    operation_states: Mapping[str, str] | None = None,
    all_items: Iterable[str] = (),
    new_work: Iterable[str] = (),
    stale_items: Iterable[str] = (),
) -> ImpactAnalysis:
    """Compute impact from reverse indexes; hints are intentionally ignored.

    Reverse indexes use ``owner -> dependents`` orientation.  Their transitive
    closure is the smallest safe program-derived scope.  Any identifier that is
    mentioned by a trigger but absent from the indexes is surfaced as
    ``unknown_coverage`` rather than silently retained.
    """

    source_refs = tuple(trigger_refs) if request is None else request.trigger_refs
    seed = {_ref_id(item) for item in source_refs}
    data, support, methods, demands, accepts = map(
        _adjacency, (reverse_data, reverse_support, method_membership, demand_refs, acceptance_refs)
    )
    indexes = (data, support, methods, demands, accepts)
    universe = {str(item) for item in all_items}
    for index_map in indexes:
        universe.update(index_map)
        for values in index_map.values():
            universe.update(values)
    known_universe = set(universe)
    for item in seed:
        universe.add(item)
    queue = deque(seed)
    impacted: set[str] = set(seed)
    while queue:
        current = queue.popleft()
        for index_map in indexes:
            for owner, dependents in index_map.items():
                if current == owner:
                    related = set(dependents)
                    for item in related - impacted:
                        impacted.add(item)
                        queue.append(item)
    op_states = {str(key): str(value).upper() for key, value in (operation_states or {}).items()}
    unresolved = {key for key, state in op_states.items() if state in {"UNKNOWN", "UNRESOLVED"}}
    impacted.update(op for op in unresolved if op in universe)
    unknown = {item for item in seed if item not in known_universe}
    unknown.update(op for op in unresolved if op not in universe)
    superseded = ({str(item) for item in stale_items} & impacted) | (impacted & unresolved)
    revalidate = impacted - superseded
    retained = universe - impacted
    return ImpactAnalysis(
        retained=tuple(sorted(retained)),
        revalidate=tuple(sorted(revalidate)),
        supersede=tuple(sorted(superseded)),
        new_work=tuple(sorted({str(item) for item in new_work})),
        unresolved_operations=tuple(sorted(unresolved)),
        unknown_coverage=tuple(sorted(unknown)),
    )


@dataclass(frozen=True, slots=True)
class RepairAction:
    action_type: RepairActionType
    target_refs: tuple[object, ...] = ()
    rationale: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_type", RepairActionType(self.action_type))
        object.__setattr__(
            self, "target_refs", _refs(self.target_refs, "repair_action.target_refs")
        )
        object.__setattr__(
            self,
            "rationale",
            self.rationale
            if self.rationale == ""
            else text(self.rationale, "repair_action.rationale"),
        )
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(json_value(self.parameters, "repair_action.parameters"))),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "action_type": str(self.action_type),
            "target_refs": [
                _ref_json(item, "repair_action.target_refs[]") for item in self.target_refs
            ],
            "rationale": self.rationale,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "repair_action") -> RepairAction:
        if not isinstance(value, Mapping) or set(value) != {
            "action_type",
            "target_refs",
            "rationale",
            "parameters",
        }:
            raise ContractError(f"{name} fields differ")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class HumanRequestV1:
    question: str
    options: tuple[str, ...] = ()
    blocking: bool = True
    authority_ref: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "question", text(self.question, "human_request.question"))
        object.__setattr__(self, "options", _strings(self.options, "human_request.options"))
        if not isinstance(self.blocking, bool):
            raise ContractError("human_request.blocking must be a boolean")
        if self.authority_ref is not None:
            object.__setattr__(
                self,
                "authority_ref",
                TypedRef.from_json(self.authority_ref, "human_request.authority_ref")
                if isinstance(self.authority_ref, Mapping)
                else identifier(self.authority_ref, "human_request.authority_ref"),
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": list(self.options),
            "blocking": self.blocking,
            "authority_ref": None
            if self.authority_ref is None
            else _ref_json(self.authority_ref, "human_request.authority_ref"),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "human_request") -> HumanRequestV1:
        if not isinstance(value, Mapping) or set(value) != {
            "question",
            "options",
            "blocking",
            "authority_ref",
        }:
            raise ContractError(f"{name} fields differ")
        return cls(**dict(value))

    @classmethod
    def from_planning_request(cls, value: object) -> HumanRequestV1:
        """Bridge the H1 decode-only ``RequestHumanDecision`` contract."""

        question = getattr(value, "question", None)
        options = getattr(value, "options", ())
        blocking = getattr(value, "blocking", True)
        if question is None:
            raise ContractError("planning human request must expose question")
        normalized_options = tuple(
            item.get("key", item) if isinstance(item, Mapping) else getattr(item, "key", item)
            for item in options
        )
        return cls(
            question=question,
            options=normalized_options,
            blocking=blocking,
        )


@dataclass(frozen=True, slots=True)
class RepairDecision:
    request: RepairRequestV1 | None = None
    actions: tuple[RepairAction, ...] = ()
    impact: ImpactAnalysis = field(default_factory=ImpactAnalysis)
    status: RepairDecisionStatus = RepairDecisionStatus.ADMITTED
    human_request: HumanRequestV1 | None = None
    schema_version: int = REPAIR_DECISION_SCHEMA_VERSION
    trigger_refs: tuple[object, ...] = ()
    diagnosis: RepairDiagnosis | RepairCause | None = None

    def __post_init__(self) -> None:
        if self.request is None:
            cause = (
                self.diagnosis.cause
                if isinstance(self.diagnosis, RepairDiagnosis)
                else self.diagnosis
            )
            object.__setattr__(
                self,
                "request",
                RepairRequestV1.from_trigger(
                    RepairTriggerSource.WORKER_REJECT,
                    trigger_refs=self.trigger_refs,
                    diagnosis=cause or RepairCause.UNKNOWN,
                ),
            )
        elif not isinstance(self.request, RepairRequestV1):
            raise ContractError("repair_decision.request must be RepairRequestV1")
        request = self.request
        assert request is not None
        object.__setattr__(self, "trigger_refs", request.trigger_refs)
        if self.diagnosis is None:
            object.__setattr__(self, "diagnosis", RepairDiagnosis(request.diagnosis))
        elif isinstance(self.diagnosis, RepairCause):
            object.__setattr__(self, "diagnosis", RepairDiagnosis(self.diagnosis))
        object.__setattr__(
            self,
            "actions",
            tuple(
                item if isinstance(item, RepairAction) else RepairAction.from_json(item)
                for item in self.actions
            ),
        )
        if not self.actions and self.status is RepairDecisionStatus.ADMITTED:
            raise ContractError("an admitted repair must have at least one action")
        if not isinstance(self.impact, ImpactAnalysis):
            raise ContractError("repair_decision.impact must be ImpactAnalysis")
        object.__setattr__(self, "status", RepairDecisionStatus(self.status))
        if self.human_request is not None and not isinstance(self.human_request, HumanRequestV1):
            raise ContractError("repair_decision.human_request must be HumanRequestV1")
        object.__setattr__(
            self,
            "schema_version",
            index(self.schema_version, "repair_decision.schema_version", minimum=1),
        )

    def to_json(self) -> dict[str, Any]:
        request = self.request
        diagnosis = self.diagnosis
        assert request is not None
        assert isinstance(diagnosis, RepairDiagnosis)
        return {
            "schema_version": self.schema_version,
            "request": request.to_json(),
            "trigger_refs": [
                _ref_json(item, "repair_decision.trigger_refs[]") for item in self.trigger_refs
            ],
            "diagnosis": diagnosis.to_json(),
            "actions": [item.to_json() for item in self.actions],
            "impact": self.impact.to_json(),
            "status": str(self.status),
            "human_request": None if self.human_request is None else self.human_request.to_json(),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "repair_decision") -> RepairDecision:
        if not isinstance(value, Mapping):
            raise ContractError(f"{name} must be an object")
        required = {
            "schema_version",
            "request",
            "trigger_refs",
            "diagnosis",
            "actions",
            "impact",
            "status",
            "human_request",
        }
        if set(value) != required:
            raise ContractError(f"{name} fields differ")
        diagnosis = value["diagnosis"]
        if not isinstance(diagnosis, Mapping):
            raise ContractError(f"{name}.diagnosis must be an object")
        return cls(
            request=RepairRequestV1.from_json(value["request"]),
            trigger_refs=_refs(value["trigger_refs"], f"{name}.trigger_refs"),
            diagnosis=RepairDiagnosis(**dict(diagnosis)),
            actions=tuple(RepairAction.from_json(item) for item in value["actions"]),
            impact=ImpactAnalysis.from_json(value["impact"]),
            status=value["status"],
            human_request=(
                None
                if value["human_request"] is None
                else HumanRequestV1.from_json(value["human_request"])
            ),
            schema_version=value["schema_version"],
        )


class RepairDecisionLedger:
    """Small restart-safe idempotency ledger for request/decision handoff."""

    def __init__(self, records: Mapping[str, RepairDecision] | None = None) -> None:
        self._records: dict[str, RepairDecision] = dict(records or {})

    def record(self, decision: RepairDecision) -> RepairDecision:
        request = decision.request
        assert request is not None
        key = request.idempotency_key
        prior = self._records.get(key)
        if prior is not None:
            if prior.to_json() != decision.to_json():
                raise ContractError("idempotency key already has a different repair decision")
            return prior
        self._records[key] = decision
        return decision

    def get(self, request_or_key: RepairRequestV1 | str) -> RepairDecision | None:
        key = (
            request_or_key.idempotency_key
            if isinstance(request_or_key, RepairRequestV1)
            else request_or_key
        )
        return self._records.get(key)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {key: value.to_json() for key, value in self._records.items()}

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> RepairDecisionLedger:
        records = {key: RepairDecision.from_json(value) for key, value in snapshot.items()}
        for key, decision in records.items():
            request = decision.request
            assert request is not None
            if key != request.idempotency_key:
                raise ContractError("repair ledger key does not match request id")
        return cls(records)


def validate_repair_decision(
    decision: RepairDecision, *, human_authorized: bool = False
) -> RepairDecisionStatus:
    """Apply fail-closed admission checks before compiler/CommitService."""

    if decision.impact.unresolved_operations:
        return RepairDecisionStatus.DEFERRED
    if decision.human_request is not None:
        if human_authorized and decision.human_request.authority_ref is None:
            return RepairDecisionStatus.DEFERRED
        return (
            RepairDecisionStatus.REQUEST_HUMAN
            if not human_authorized
            else RepairDecisionStatus.ADMITTED
        )
    if decision.status is RepairDecisionStatus.ADMITTED and decision.impact.unknown_coverage:
        return RepairDecisionStatus.DEFERRED
    return decision.status


__all__ = [
    "ImpactAnalysis",
    "HumanRequestV1",
    "RepairAction",
    "RepairActionType",
    "RepairCause",
    "RepairDiagnosis",
    "RepairDecision",
    "RepairDecisionLedger",
    "RepairDecisionStatus",
    "RepairRequestV1",
    "RepairTrigger",
    "RepairTriggerSource",
    "analyze_impact",
    "validate_repair_decision",
]

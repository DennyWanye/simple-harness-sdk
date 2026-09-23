# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""H3 method selection policy and evidence-request admission.

H3 is deliberately a pure seam.  The caller supplies the current HTN
applicability reports, plan/evidence identities, and the installed observer /
authority view.  This module decides whether a method may be selected by a
deterministic fast path or whether one Planner ``REFINE`` call is warranted;
it does not dispatch a model, mutate a plan, or write evidence.

The three-part selection identity is the recovery fence for a model call:
``(plan_revision, evidence_epoch, candidate_set_digest)``.  A caller may persist
``SelectionCallLedger`` beside its request record and restore it after a crash.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ...contracts.models import ContractError, sha256_hex
from ...contracts.planning_decisions import RequestEvidenceDecision
from ...knowledge.predicates import PredicateRegistry
from .applicability import ApplicabilityReport, ApplicabilityStatus
from .observation_pipeline import ObserverIndex

SELECTION_POLICY_SCHEMA_VERSION = 1
SELECTION_IDENTITY_SCHEMA_VERSION = 1
MAX_EVIDENCE_QUESTIONS = 8


class SelectionPolicyMode(StrEnum):
    """H3's versioned selection policies."""

    DETERMINISTIC = "DETERMINISTIC"
    MODEL_ON_MULTIPLE = "MODEL_ON_MULTIPLE"
    ALWAYS_MODEL = "ALWAYS_MODEL"


class SelectionRoute(StrEnum):
    """The next operation selected by :func:`select_method`."""

    EVIDENCE_OR_SYNTHESIS = "EVIDENCE_OR_SYNTHESIS"
    DETERMINISTIC = "DETERMINISTIC"
    MODEL_REFINE = "MODEL_REFINE"
    SELECTION_ALREADY_ATTEMPTED = "SELECTION_ALREADY_ATTEMPTED"


@dataclass(frozen=True, slots=True)
class MethodSelectionPolicyV1:
    """A versioned policy; new planning requests default to model-on-multiple."""

    mode: SelectionPolicyMode = SelectionPolicyMode.MODEL_ON_MULTIPLE
    schema_version: int = SELECTION_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported method selection policy schema_version")
        object.__setattr__(self, "mode", SelectionPolicyMode(self.mode))

    @classmethod
    def new_protocol(cls) -> MethodSelectionPolicyV1:
        return cls()

    @classmethod
    def legacy(cls) -> MethodSelectionPolicyV1:
        # The legacy planner has no model selection phase.  Keeping a policy
        # value for it makes the boundary explicit without changing old bytes.
        return cls(mode=SelectionPolicyMode.DETERMINISTIC)

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "mode": str(self.mode)}

    @classmethod
    def from_json(cls, value: object, name: str = "selection_policy") -> MethodSelectionPolicyV1:
        if not isinstance(value, Mapping) or set(value) != {"schema_version", "mode"}:
            raise ContractError(f"{name} must carry exactly schema_version and mode")
        return cls(schema_version=value["schema_version"], mode=value["mode"])


@dataclass(frozen=True, slots=True)
class MethodSelectionCandidateV1:
    """A candidate identity and its deterministic applicability result."""

    method_id: str
    method_version: int = 1
    method_content_hash: str = ""
    report: ApplicabilityReport | ApplicabilityStatus = ApplicabilityStatus.APPLICABLE
    bindings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.method_id, str) or not self.method_id:
            raise ContractError("selection candidate method_id must be non-empty")
        if type(self.method_version) is not int or self.method_version < 1:
            raise ContractError("selection candidate method_version must be positive")
        if not isinstance(self.method_content_hash, str):
            raise ContractError("selection candidate method_content_hash must be text")
        report = self.report
        if isinstance(report, ApplicabilityReport):
            status = report.status
        else:
            status = ApplicabilityStatus(report)
        object.__setattr__(self, "report", status)
        if not isinstance(self.bindings, Mapping):
            raise ContractError("selection candidate bindings must be an object")
        object.__setattr__(self, "bindings", dict(self.bindings))

    @property
    def applicable(self) -> bool:
        return self.report is ApplicabilityStatus.APPLICABLE

    def to_json(self) -> dict[str, Any]:
        return {
            "method_id": self.method_id,
            "method_version": self.method_version,
            "method_content_hash": self.method_content_hash,
            "status": str(self.report),
            "bindings": dict(self.bindings),
        }


@dataclass(frozen=True, slots=True)
class SelectionIdentityV1:
    """The persisted idempotency identity for one selection opportunity."""

    plan_revision: int
    evidence_epoch: int
    candidate_set_digest: str
    schema_version: int = SELECTION_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("plan_revision", "evidence_epoch"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ContractError(f"selection identity {name} must be a non-negative integer")
        if not isinstance(self.candidate_set_digest, str) or len(self.candidate_set_digest) != 64:
            raise ContractError("selection identity candidate_set_digest must be a SHA-256 hex")
        try:
            int(self.candidate_set_digest, 16)
        except ValueError as error:
            raise ContractError("selection identity candidate_set_digest must be hex") from error
        if self.schema_version != 1:
            raise ContractError("unsupported selection identity schema_version")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_revision": self.plan_revision,
            "evidence_epoch": self.evidence_epoch,
            "candidate_set_digest": self.candidate_set_digest,
        }

    def key(self) -> tuple[int, int, str]:
        return (self.plan_revision, self.evidence_epoch, self.candidate_set_digest)


def _candidate(value: MethodSelectionCandidateV1 | Mapping[str, Any]) -> MethodSelectionCandidateV1:
    if isinstance(value, MethodSelectionCandidateV1):
        return value
    if isinstance(value, Mapping):
        return MethodSelectionCandidateV1(
            method_id=value.get("method_id", value.get("id", "")),
            method_version=value.get("method_version", value.get("version", 1)),
            method_content_hash=value.get("method_content_hash", value.get("content_hash", "")),
            report=value.get("report", value.get("status", ApplicabilityStatus.APPLICABLE)),
            bindings=value.get("bindings", {}),
        )
    raise ContractError("selection candidates must be typed candidates or objects")


def normalize_candidates(
    candidates: Iterable[MethodSelectionCandidateV1 | Mapping[str, Any]],
) -> tuple[MethodSelectionCandidateV1, ...]:
    """Normalize and deterministically order a candidate set."""

    result = tuple(_candidate(item) for item in candidates)
    keys = [(item.method_id, item.method_version, item.method_content_hash) for item in result]
    if len(set(keys)) != len(keys):
        raise ContractError("selection candidate set contains duplicate method identities")
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.method_id,
                item.method_version,
                item.method_content_hash,
            ),
        )
    )


def candidate_set_digest(
    candidates: Iterable[MethodSelectionCandidateV1 | Mapping[str, Any]],
) -> str:
    """Hash the complete, order-independent candidate set."""

    normalized = normalize_candidates(candidates)
    return sha256_hex(
        {"schema_version": 1, "candidates": [item.to_json() for item in normalized]}
    )


def selection_identity(
    plan_revision: int,
    evidence_epoch: int,
    candidates: Iterable[MethodSelectionCandidateV1 | Mapping[str, Any]],
    *, subject_id: str | None = None,
) -> SelectionIdentityV1:
    """Build the exact H3 model-call identity from frozen inputs."""

    return SelectionIdentityV1(
        plan_revision=plan_revision,
        evidence_epoch=evidence_epoch,
        candidate_set_digest=(candidate_set_digest(candidates) if subject_id is None else
            sha256_hex({"subject_id": subject_id, "candidates": candidate_set_digest(candidates)})),
    )


@dataclass(frozen=True, slots=True)
class MethodSelectionResultV1:
    route: SelectionRoute
    candidates: tuple[MethodSelectionCandidateV1, ...]
    applicable: tuple[MethodSelectionCandidateV1, ...]
    identity: SelectionIdentityV1
    selected_method_id: str | None = None
    reason: str = ""

    @property
    def should_call_model(self) -> bool:
        return self.route is SelectionRoute.MODEL_REFINE


class SelectionCallLedger:
    """Crash-safe-friendly in-memory representation of claimed selection calls.

    Persist :meth:`to_json` with the request record in a real runtime.  ``claim``
    is intentionally atomic for one process and refuses a second call for the
    same H3 identity even if the first call has not produced a response yet.
    """

    def __init__(self, identities: Iterable[SelectionIdentityV1] = ()) -> None:
        self._claims: dict[tuple[int, int, str], str] = {}
        for identity in identities:
            self._claims[identity.key()] = identity.candidate_set_digest

    def claimed(self, identity: SelectionIdentityV1) -> bool:
        return identity.key() in self._claims

    def claim(self, identity: SelectionIdentityV1, *, call_id: str) -> bool:
        if not isinstance(call_id, str) or not call_id:
            raise ContractError("selection call_id must be non-empty")
        key = identity.key()
        if key in self._claims:
            return False
        self._claims[key] = call_id
        return True

    def call_id(self, identity: SelectionIdentityV1) -> str | None:
        return self._claims.get(identity.key())

    def to_json(self) -> list[dict[str, Any]]:
        return [
            {
                "identity": {
                    "plan_revision": key[0],
                    "evidence_epoch": key[1],
                    "candidate_set_digest": key[2],
                },
                "call_id": call_id,
            }
            for key, call_id in sorted(self._claims.items())
        ]

    @classmethod
    def from_json(cls, value: object) -> SelectionCallLedger:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise ContractError("selection call ledger must be an array")
        ledger = cls()
        for row in value:
            if not isinstance(row, Mapping):
                raise ContractError("selection call ledger rows must be objects")
            raw = row.get("identity")
            if not isinstance(raw, Mapping):
                raise ContractError("selection call ledger row has no identity")
            identity = SelectionIdentityV1(
                plan_revision=int(raw.get("plan_revision", -1)),
                evidence_epoch=int(raw.get("evidence_epoch", -1)),
                candidate_set_digest=str(raw.get("candidate_set_digest", "")),
            )
            if not ledger.claim(identity, call_id=row.get("call_id", "")):
                raise ContractError("selection call ledger repeats an identity")
        return ledger


def select_method(
    candidates: Iterable[MethodSelectionCandidateV1 | Mapping[str, Any]],
    *,
    plan_revision: int,
    evidence_epoch: int,
    policy: MethodSelectionPolicyV1 | Mapping[str, Any] | None = None,
    legacy: bool = False,
    ledger: SelectionCallLedger | None = None,
    subject_id: str | None = None,
) -> MethodSelectionResultV1:
    """Route one method selection opportunity according to H3 §49.

    Zero applicable methods goes to evidence/synthesis.  One applicable method
    takes the deterministic fast path.  With two or more, the new protocol uses
    one ``REFINE`` call unless policy is ``DETERMINISTIC``.  Legacy callers are
    deterministic regardless of any accidental new-policy value.
    """

    normalized = normalize_candidates(candidates)
    applicable = tuple(item for item in normalized if item.applicable)
    identity = selection_identity(plan_revision, evidence_epoch, normalized, subject_id=subject_id)
    if policy is None:
        resolved = MethodSelectionPolicyV1.new_protocol()
    elif isinstance(policy, MethodSelectionPolicyV1):
        resolved = policy
    else:
        resolved = MethodSelectionPolicyV1.from_json(policy)
    if legacy:
        route = (
            SelectionRoute.DETERMINISTIC
            if applicable
            else SelectionRoute.EVIDENCE_OR_SYNTHESIS
        )
        selected = applicable[0].method_id if applicable else None
        return MethodSelectionResultV1(
            route,
            normalized,
            applicable,
            identity,
            selected,
            "legacy",
        )
    if not applicable:
        return MethodSelectionResultV1(
            SelectionRoute.EVIDENCE_OR_SYNTHESIS,
            normalized,
            applicable,
            identity,
            reason="no applicable method",
        )
    if resolved.mode is SelectionPolicyMode.DETERMINISTIC or (
        len(applicable) == 1 and resolved.mode is not SelectionPolicyMode.ALWAYS_MODEL
    ):
        return MethodSelectionResultV1(
            SelectionRoute.DETERMINISTIC,
            normalized,
            applicable,
            identity,
            applicable[0].method_id,
            "deterministic fast path",
        )
    if ledger is not None:
        call_id = (
            f"selection:{identity.plan_revision}:{identity.evidence_epoch}:"
            f"{identity.candidate_set_digest}"
        )
        if not ledger.claim(identity, call_id=call_id):
            return MethodSelectionResultV1(
                SelectionRoute.SELECTION_ALREADY_ATTEMPTED,
                normalized,
                applicable,
                identity,
                reason="selection identity already claimed",
            )
    return MethodSelectionResultV1(
        SelectionRoute.MODEL_REFINE,
        normalized,
        applicable,
        identity,
        reason="multiple applicable methods",
    )


def _predicate_key(value: str) -> tuple[str, int | None]:
    if not isinstance(value, str) or not value:
        raise ContractError("evidence question predicate_key must be non-empty")
    if "@" not in value:
        return value, None
    name, version = value.rsplit("@", 1)
    try:
        parsed = int(version)
    except ValueError as error:
        raise ContractError("evidence question predicate_key has invalid version") from error
    if not name or parsed < 1:
        raise ContractError("evidence question predicate_key has invalid predicate ref")
    return name, parsed


def _observer_available(predicate_key: str, observers: Any) -> bool:
    if isinstance(observers, ObserverIndex):
        name, _ = _predicate_key(predicate_key)
        return observers.observer_for(name) is not None
    if isinstance(observers, Mapping):
        name, _ = _predicate_key(predicate_key)
        return bool(observers.get(predicate_key, observers.get(name)))
    if isinstance(observers, (set, frozenset, tuple, list)):
        name, _ = _predicate_key(predicate_key)
        return predicate_key in observers or name in observers
    return False


def validate_evidence_request(
    request: RequestEvidenceDecision,
    *,
    registry: PredicateRegistry,
    observers: Any,
    authorized_predicates: Iterable[str] = (),
    authorized_scopes: Iterable[str] = (),
) -> RequestEvidenceDecision:
    """Admit a formal H3 ``REQUEST_EVIDENCE`` against producer-backed facts.

    A question is accepted only when its exact predicate version is registered,
    this deployment has an observer for it, and the caller has an explicit
    predicate or authority-scope grant.  The function never treats a registry
    declaration or a model-provided authority string as a grant.
    """

    if not isinstance(request, RequestEvidenceDecision):
        raise ContractError("evidence request must be a RequestEvidenceDecision")
    if len(request.questions) > MAX_EVIDENCE_QUESTIONS:
        raise ContractError("REQUEST_EVIDENCE may contain at most 8 questions")
    if not isinstance(registry, PredicateRegistry):
        raise ContractError("evidence request requires a PredicateRegistry")
    predicate_grants = frozenset(str(item) for item in authorized_predicates)
    scope_grants = frozenset(str(item) for item in authorized_scopes)
    seen: set[str] = set()
    for position, question in enumerate(request.questions):
        key = question.predicate_key
        question_identity = sha256_hex(
            {"predicate_key": key, "arguments": dict(question.arguments)}
        )
        if question_identity in seen:
            raise ContractError(f"REQUEST_EVIDENCE repeats question {position}")
        seen.add(question_identity)
        name, version = _predicate_key(key)
        signatures = tuple(
            item
            for item in registry.signatures()
            if item.predicate_ref.id == name
            and (version is None or item.predicate_ref.version == version)
        )
        if len(signatures) != 1:
            raise ContractError(
                f"evidence predicate {key!r} is not registered at one exact version"
            )
        signature = signatures[0]
        if not _observer_available(key, observers):
            raise ContractError(f"no observer is installed for evidence predicate {key!r}")
        if key not in predicate_grants and name not in predicate_grants and (
            signature.authority_scope is None or signature.authority_scope not in scope_grants
        ):
            raise ContractError(f"evidence predicate {key!r} is outside the caller authority")
    return request


__all__ = (
    "ALWAYS_MODEL",
    "DETERMINISTIC",
    "MAX_EVIDENCE_QUESTIONS",
    "MODEL_ON_MULTIPLE",
    "MethodSelectionCandidateV1",
    "MethodSelectionPolicyV1",
    "MethodSelectionResultV1",
    "SELECTION_IDENTITY_SCHEMA_VERSION",
    "SELECTION_POLICY_SCHEMA_VERSION",
    "SelectionCallLedger",
    "SelectionIdentityV1",
    "SelectionPolicyMode",
    "SelectionRoute",
    "SelectionPolicy",
    "SelectionPolicyV1",
    "candidate_set_digest",
    "normalize_candidates",
    "select_method",
    "selection_identity",
    "validate_evidence_request",
)

# Short names mirror the policy vocabulary in V2 §49 and make call sites read
# naturally without sacrificing the versioned dataclass above.
DETERMINISTIC = SelectionPolicyMode.DETERMINISTIC
MODEL_ON_MULTIPLE = SelectionPolicyMode.MODEL_ON_MULTIPLE
ALWAYS_MODEL = SelectionPolicyMode.ALWAYS_MODEL
SelectionPolicyV1 = MethodSelectionPolicyV1
SelectionPolicy = MethodSelectionPolicyV1

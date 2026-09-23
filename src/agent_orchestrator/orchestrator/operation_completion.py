# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Approved completion requirements on the original CommitService transaction.

Approval records what the authenticated caller actually confirmed. It is neither
an operation authorization nor evidence that an effect has happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..contracts import TERMINAL_MISSION, ContractError
from ..contracts.operation_completion import (
    OccurrenceCompletionScopeV1,
    OperationCompletionRequirementsV1,
    PlanRevisionPinV1,
)
from ..contracts.resolution import RequirementsRevision
from ..contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of, identifier
from ..governance.permissions import Principal
from ..graph.task_network import TaskNetworkSnapshot
from ..planning.htn.grounding import derive_id
from ..storage.htn_store import HtnStore
from ..storage.obligation_store import ObligationStore
from ..storage.operation_completion_store import OperationCompletionStore
from ..storage.store import Store, StoreError


class OperationCompletionError(ContractError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovedRequirementAuthority:
    """Internal API binding; there is intentionally no wire decoder for this type."""

    kind: str
    tenant_id: str
    issuer_id: str
    command_id: str
    command_body_hash: str
    requirements_ref: TypedRef
    normalized_spec_hash: str
    policy_ref: TypedRef | None
    check_receipt_refs: tuple[TypedRef, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tenant_id": self.tenant_id,
            "issuer_id": self.issuer_id,
            "command_id": self.command_id,
            "command_body_hash": self.command_body_hash,
            "requirements_ref": self.requirements_ref.to_json(),
            "normalized_spec_hash": self.normalized_spec_hash,
            "policy_ref": None if self.policy_ref is None else self.policy_ref.to_json(),
            "check_receipt_refs": [ref.to_json() for ref in self.check_receipt_refs],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionSpecReceipt:
    command_id: str
    mission_id: str
    spec_id: str
    spec_hash: str
    requirements_ref: TypedRef
    authority: ApprovedRequirementAuthority

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": "operation_completion_spec_approved",
            "command_id": self.command_id,
            "mission_id": self.mission_id,
            "spec_id": self.spec_id,
            "spec_hash": self.spec_hash,
            "requirements_ref": self.requirements_ref.to_json(),
            "authority": self.authority.to_json(),
        }


def approval_body(
    mission_id: str,
    command_id: str,
    requirements_ref: TypedRef,
    proposal: OperationCompletionRequirementsV1,
) -> dict[str, Any]:
    return {
        "mission_id": mission_id,
        "command_id": command_id,
        "expected_requirements_ref": requirements_ref.to_json(),
        "proposal": proposal.to_json(),
    }


def _requirements(store: Store, mission_id: str, ref: TypedRef) -> RequirementsRevision:
    if not isinstance(ref, TypedRef) or ref.kind is not TypedRefKind.REQUIREMENTS:
        raise OperationCompletionError("OP_REF_KIND_UNSUPPORTED", "a requirements ref is required")
    try:
        value = HtnStore(store).get_requirements_revision(mission_id, ref.revision)
    except StoreError as error:
        raise OperationCompletionError(
            "OP_REQUIREMENT_MAPPING_MISSING", "the requirements source is unavailable"
        ) from error
    if str(value.revision_id) != ref.id or value.content_hash() != ref.content_hash:
        raise OperationCompletionError("OP_PAYLOAD_HASH_MISMATCH", "requirements identity differs")
    return value


def _coverage(
    store: Store, requirements: RequirementsRevision, proposal: OperationCompletionRequirementsV1
) -> None:
    document = proposal.to_json()
    covered = set(document["content_criterion_ids"])
    for effect in document["effects"]:
        covered.update(effect["criterion_ids"])
    catalogue = {criterion.criterion_id for criterion in requirements.criteria}
    required = set(requirements.required_criterion_ids())
    # Coverage is provenance, not evaluation: ALL/ANY remains the original formula.
    if not required <= covered or not covered <= catalogue:
        raise OperationCompletionError(
            "OP_REQUIREMENT_MAPPING_AMBIGUOUS", "the mapping omits or invents requirements"
        )
    duties = {
        str(value) for value in ObligationStore(store).obligation_ids(requirements.mission_id)
    }
    if any(effect["obligation_id"] not in duties for effect in document["effects"]):
        raise OperationCompletionError(
            "OP_COMPLETION_SCOPE_UNRESOLVED", "an effect names no existing mission obligation"
        )


def _require_current(store: Store, requirements: RequirementsRevision) -> None:
    newest = HtnStore(store).latest_requirements_revision(requirements.mission_id)
    if (
        newest is None
        or newest.revision != requirements.revision
        or newest.content_hash() != requirements.content_hash()
    ):
        raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "requirements have changed")


class OperationCompletionCommitsMixin:
    if TYPE_CHECKING:
        _store: Store

        def _emit(
            self,
            event_type: str,
            mission_id: str,
            *,
            key: str,
            payload: dict[str, Any],
            actor_type: str = ...,
            actor_id: str = ...,
        ) -> Any: ...

    def approve_operation_completion_spec(
        self,
        *,
        mission_id: str,
        command_id: str,
        expected_requirements_ref: TypedRef,
        proposal: OperationCompletionRequirementsV1,
        requirement_authority: ApprovedRequirementAuthority,
        principal: Principal,
    ) -> CompletionSpecReceipt:
        mission_id = identifier(mission_id, "mission_id")
        command_id = identifier(command_id, "command_id")
        if not isinstance(principal, Principal) or not isinstance(
            requirement_authority, ApprovedRequirementAuthority
        ):
            raise OperationCompletionError("OP_REQUIREMENT_MAPPING_UNAPPROVED", "unbound caller")
        if not isinstance(proposal, OperationCompletionRequirementsV1):
            raise OperationCompletionError(
                "OP_REQUIREMENT_MAPPING_MISSING", "a typed Spec is required"
            )
        if not isinstance(expected_requirements_ref, TypedRef) or (
            expected_requirements_ref.kind is not TypedRefKind.REQUIREMENTS
        ):
            raise OperationCompletionError("OP_REF_KIND_UNSUPPORTED", "requirements kind required")
        # Revalidate even direct internal callers; no arbitrary Mapping enters the writer.
        proposal = OperationCompletionRequirementsV1.from_json(proposal.to_json())
        authority = requirement_authority
        body_hash = content_hash_of(
            approval_body(mission_id, command_id, expected_requirements_ref, proposal)
        )
        if (
            principal.kind != "human"
            or authority.kind != "USER_CONFIRMED"
            or authority.issuer_id != principal.principal_id
            or authority.command_id != command_id
            or authority.command_body_hash != body_hash
            or authority.requirements_ref != expected_requirements_ref
            or authority.normalized_spec_hash != proposal.content_hash()
            or authority.policy_ref is not None
            or authority.check_receipt_refs
        ):
            # A deployment without a registered interpreting policy cannot invent
            # REQUIREMENTS_POLICY authority. Explicit confirmation remains available.
            raise OperationCompletionError(
                "OP_REQUIREMENT_MAPPING_UNAPPROVED", "the confirmation binding differs"
            )
        spec_ref = proposal.to_json()["requirements_ref"]
        if (
            proposal.mission_id != mission_id
            or spec_ref["id"] != expected_requirements_ref.id
            or spec_ref["revision"] != expected_requirements_ref.revision
        ):
            raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "Spec requirements differ")
        if spec_ref["content_hash"] != expected_requirements_ref.content_hash:
            raise OperationCompletionError(
                "OP_PAYLOAD_HASH_MISMATCH", "Spec requirements hash differs"
            )
        spec_id = derive_id(
            "op-completion-spec",
            mission_id,
            expected_requirements_ref.revision,
            expected_requirements_ref.content_hash,
        )
        receipt = CompletionSpecReceipt(
            command_id=command_id,
            mission_id=mission_id,
            spec_id=spec_id,
            spec_hash=proposal.content_hash(),
            requirements_ref=expected_requirements_ref,
            authority=authority,
        )
        with self._store.transaction():
            mission = self._store.get_mission(mission_id)
            if mission is None or mission.tenant_id != authority.tenant_id:
                raise OperationCompletionError("not_found", "no such mission for this caller")
            protocol = self._store.connection.execute(
                "SELECT protocol_version FROM mission_planning_protocols WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            if protocol is None or protocol[0] != "planning-decision-v1":
                raise OperationCompletionError(
                    "OP_REQUIREMENT_MAPPING_UNAPPROVED",
                    "completion approval requires the new protocol",
                )
            previous = self._store.get_receipt(command_id)
            if previous is not None:
                if previous != receipt.to_json():
                    raise OperationCompletionError("conflict", "command identity or caller differs")
                # Replays disclose only a receipt this currently authenticated caller owns.
                return receipt
            if mission.status in TERMINAL_MISSION:
                raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "the mission is terminal")
            requirements = _requirements(self._store, mission_id, expected_requirements_ref)
            _require_current(self._store, requirements)
            _coverage(self._store, requirements, proposal)
            self._store.insert_receipt(
                commit_id=command_id,
                kind="operation_completion_spec_approved",
                subject_id=spec_id,
                base_version=requirements.revision,
                proposal_hash=body_hash,
                receipt=receipt.to_json(),
            )
            self._store.fault("completion_spec_after_receipt", "operation_completion")
            OperationCompletionStore(self._store).insert_spec(
                spec_id,
                proposal,
                approval_receipt_id=command_id,
            )
            self._store.fault("completion_spec_after_spec", "operation_completion")
            self._emit(
                "OperationCompletionSpecApproved",
                mission_id,
                key=command_id,
                payload=receipt.to_json(),
                actor_type="human",
                actor_id=principal.principal_id,
            )
            self._store.fault("completion_spec_after_event", "operation_completion")
        return receipt


class OperationCompletionReader:
    """Exact approved requirements reader; absence never means CONTENT_ONLY."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def read_requirements(
        self, mission_id: str, requirements_ref: TypedRef
    ) -> OperationCompletionRequirementsV1:
        with self._store.read_view():
            requirements = _requirements(self._store, mission_id, requirements_ref)
            _require_current(self._store, requirements)
            row = OperationCompletionStore(self._store).get_spec_exact(
                mission_id, requirements.revision, requirements.content_hash()
            )
            if row is None:
                raise OperationCompletionError("OP_REQUIREMENT_MAPPING_MISSING", "no approved Spec")
            document = row["document"]
            if not isinstance(document, OperationCompletionRequirementsV1):
                raise OperationCompletionError(
                    "OP_REQUIREMENT_MAPPING_MISSING", "invalid Spec codec"
                )
            receipt = self._store.get_receipt(row["approval_receipt_id"])
            if (
                receipt is None
                or receipt.get("kind") != "operation_completion_spec_approved"
                or receipt.get("mission_id") != mission_id
                or receipt.get("spec_id") != row["spec_id"]
                or receipt.get("spec_hash") != document.content_hash()
                or receipt.get("requirements_ref") != requirements_ref.to_json()
            ):
                raise OperationCompletionError(
                    "OP_REQUIREMENT_MAPPING_UNAPPROVED", "approval source differs"
                )
            _coverage(self._store, requirements, document)
            return document

    def read_scope(
        self, mission_id: str, plan_ref: PlanRevisionPinV1, occurrence_id: str
    ) -> OccurrenceCompletionScopeV1:
        """Read the frozen scope only while its plan, Spec and Task contract stand."""
        if not isinstance(plan_ref, PlanRevisionPinV1):
            raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "a typed plan pin is required")
        occurrence_id = identifier(occurrence_id, "occurrence_id")
        with self._store.read_view():
            htn = HtnStore(self._store)
            active = htn.active_plan_revision(mission_id)
            if (
                active is None
                or active.revision != plan_ref.revision
                or active.snapshot_hash != plan_ref.snapshot_hash
            ):
                raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "the adopted plan differs")
            row = OperationCompletionStore(self._store).get_scope_exact(
                mission_id, plan_ref.revision, occurrence_id
            )
            if row is None:
                raise OperationCompletionError(
                    "OP_COMPLETION_SCOPE_UNRESOLVED", "no frozen occurrence scope"
                )
            scope = row["document"]
            if not isinstance(scope, OccurrenceCompletionScopeV1) or scope.plan_ref != plan_ref:
                raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "the scope plan differs")
            spec = self.read_requirements(
                mission_id,
                TypedRef(kind=TypedRefKind.REQUIREMENTS, **scope.requirements_ref.to_json()),
            )
            if scope.spec_hash != spec.content_hash():
                raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "the scope Spec differs")
            binding = htn.task_semantics_of(mission_id, scope.task_ref.id)
            member = next(
                (
                    item
                    for item in htn.list_plan_memberships(mission_id, plan_ref.revision)
                    if str(item.occurrence_id) == occurrence_id
                ),
                None,
            )
            if (
                binding is None
                or member is None
                or str(member.task_id) != scope.task_ref.id
                or str(member.obligation_id) != scope.obligation_id
                or str(binding.obligation_id) != scope.obligation_id
                or int(binding.contract_revision) != scope.task_ref.revision
                or binding.contract_hash != scope.task_ref.content_hash
            ):
                raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "the Task contract differs")
            receipt = htn.get_commit_receipt(row["plan_receipt_id"])
            if receipt.mission_id != mission_id or receipt.new_plan_revision != plan_ref.revision:
                raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "the plan receipt differs")
            return scope


def freeze_plan_completion_scopes(
    store: Store,
    *,
    mission_id: str,
    plan: TaskNetworkSnapshot,
    plan_ref: PlanRevisionPinV1,
    plan_receipt_id: str,
) -> None:
    """Freeze the admitted plan's contribution scopes in its existing transaction.

    Legacy protocol missions keep their original path. A new-protocol mission
    cannot publish a dispatchable plan without an approved completion mapping.
    """
    from ..planning.htn.completion_scopes import compile_completion_scopes
    from .accepted_outputs import carried_criteria_in_revision

    protocol = store.connection.execute(
        "SELECT protocol_version FROM mission_planning_protocols WHERE mission_id=?",
        (mission_id,),
    ).fetchone()
    if protocol is None or protocol[0] != "planning-decision-v1":
        return
    requirements = HtnStore(store).latest_requirements_revision(mission_id)
    if requirements is None:
        raise OperationCompletionError(
            "OP_REQUIREMENT_MAPPING_MISSING", "no approved requirements before plan dispatch"
        )
    ref = TypedRef(
        kind=TypedRefKind.REQUIREMENTS,
        id=str(requirements.revision_id),
        revision=int(requirements.revision),
        content_hash=requirements.content_hash(),
    )
    spec = OperationCompletionReader(store).read_requirements(mission_id, ref)
    scopes = compile_completion_scopes(
        spec,
        plan,
        carried_criteria_in_revision(HtnStore(store), mission_id, plan_ref.revision),
        plan_ref=plan_ref,
    )
    store.fault("completion_plan_before_scopes", "operation_completion")
    completion = OperationCompletionStore(store)
    for scope in scopes:
        completion.insert_scope(scope.scope_id, scope, plan_receipt_id=plan_receipt_id)
        store.fault("completion_plan_after_scope", "operation_completion")

# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed reader for the immutable T0 -> T1 operation chain."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..artifacts.store import ArtifactStore
from ..contracts.evidence_state import Validity
from ..contracts.operation_completion import PlanRevisionPinV1
from ..contracts.operation_payloads import (
    FrozenActionProposalV2,
    OperationEffectContractV2,
    OperationParametersV1,
    PayloadKind,
)
from ..contracts.resolution import ReviewPurpose, ReviewVerdict
from ..contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from ..orchestrator.operation_completion import OperationCompletionReader
from ..storage.htn_store import HtnStore
from ..storage.operation_intent_store import OperationIntentStore
from ..storage.operation_payload_store import OperationPayloadStore
from ..storage.store import Store, StoreConflict
from .operation_payloads import (
    ConnectorProfileRegistry,
    OperationPayloadUnavailable,
    require_connector_profile,
)


class OperationReferenceUnavailable(StoreConflict):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedOperationReferences:
    """Exact immutable chain, also usable by T3 outcome importers."""

    submission_receipt: dict[str, Any]
    intent: dict[str, Any]
    parameters: OperationParametersV1
    effect_contract: OperationEffectContractV2
    proposal: FrozenActionProposalV2
    official_review: Any
    envelope: Any


class OperationReferenceResolver:
    def __init__(self, store: Store, profiles: ConnectorProfileRegistry) -> None:
        self._store = store
        self._profiles = profiles

    def resolve_for_handoff(
        self, action: dict[str, Any], link: dict[str, Any]
    ) -> ResolvedOperationReferences | None:
        """Return ``None`` only for a pre-T0 link; reject every broken T0 chain."""
        receipt = self._store.get_receipt(str(link.get("provenance_receipt_id", "")))
        if receipt is None or receipt.get("kind") != "operation_intent_submitted":
            from ..orchestrator.scoped_content_review import uses_completion_protocol

            if uses_completion_protocol(self._store, str(action["mission_id"])):
                raise OperationReferenceUnavailable("operation_intent_source_missing")
            return None
        try:
            return self._resolve_t0(action, link, dict(receipt))
        except (
            KeyError,
            TypeError,
            ValueError,
            StoreConflict,
            OperationPayloadUnavailable,
        ) as error:
            raise OperationReferenceUnavailable("operation_ref_unavailable") from error

    def _payload_store(self, ref_json: object) -> tuple[OperationPayloadStore, ArtifactStore]:
        ref = TypedRef.from_json(ref_json, "payload_ref")
        row = self._store.connection.execute(
            "SELECT storage_uri FROM operation_payload_objects WHERE object_id=?", (ref.id,)
        ).fetchone()
        if row is None:
            raise OperationReferenceUnavailable("operation_payload_missing")
        # The payload store subsequently verifies this is exactly its CAS address.
        return OperationPayloadStore(self._store), ArtifactStore(Path(str(row[0])).parent.parent)

    def _resolve_t0(
        self,
        action: dict[str, Any],
        link: dict[str, Any],
        receipt: dict[str, Any],
        *,
        require_current: bool = True,
    ) -> ResolvedOperationReferences:
        intent_id = str(receipt["intent_id"])
        intent = OperationIntentStore(self._store).get(intent_id)
        if intent is None or any(
            str(receipt.get(key)) != str(intent.get(key))
            for key in ("intent_id", "mission_id", "tenant_id", "principal_id", "submission_hash")
        ):
            raise OperationReferenceUnavailable("intent_receipt_mismatch")
        if str(intent["submission_receipt_id"]) != str(link["provenance_receipt_id"]):
            raise OperationReferenceUnavailable("link_submission_mismatch")
        if str(action.get("mission_id")) != str(intent["mission_id"]):
            raise OperationReferenceUnavailable("action_mission_mismatch")
        pstore, cas = self._payload_store(receipt["parameters_ref"])
        params_ref = TypedRef.from_json(receipt["parameters_ref"])
        effect_ref = TypedRef.from_json(receipt["effect_contract_ref"])
        proposal_ref = TypedRef.from_json(receipt["proposal_ref"])
        parameters = pstore.get_payload(
            mission_id=intent["mission_id"],
            ref=params_ref,
            expected_kind=PayloadKind.PARAMETERS,
            cas=cas,
        ).payload
        effect = pstore.get_payload(
            mission_id=intent["mission_id"],
            ref=effect_ref,
            expected_kind=PayloadKind.EFFECT_CONTRACT,
            cas=cas,
        ).payload
        proposal = pstore.get_payload(
            mission_id=intent["mission_id"],
            ref=proposal_ref,
            expected_kind=PayloadKind.ACTION_PROPOSAL,
            cas=cas,
        ).payload
        if (
            not isinstance(parameters, OperationParametersV1)
            or not isinstance(effect, OperationEffectContractV2)
            or not isinstance(proposal, FrozenActionProposalV2)
        ):
            raise OperationReferenceUnavailable("payload_version_mismatch")
        if (
            params_ref.id != intent["parameters_object_id"]
            or params_ref.content_hash != intent["parameters_content_hash"]
            or effect_ref.id != intent["effect_object_id"]
            or effect_ref.content_hash != intent["effect_content_hash"]
            or proposal_ref.id != intent["proposal_object_id"]
            or proposal_ref.content_hash != intent["proposal_content_hash"]
            or parameters.intent_id != intent_id
            or proposal.intent_id != intent_id
            or proposal.parameters_ref != params_ref
            or proposal.effect_contract_ref != effect_ref
            or proposal.request_hash != intent["request_hash"]
            or proposal.request_hash != link["request_hash"]
            or parameters.params_hash != intent["params_hash"]
            or parameters.params_hash != action.get("params_hash")
        ):
            raise OperationReferenceUnavailable("frozen_identity_mismatch")
        profile = require_connector_profile(
            self._profiles,
            connector_id=parameters.connector_id,
            operation_name=parameters.operation_name,
            expected_hash=parameters.connector_profile_hash,
        )
        if (
            effect.connector_profile_hash != profile.content_hash()
            or effect.operation_name != parameters.operation_name
        ):
            raise OperationReferenceUnavailable("current_profile_mismatch")
        htn = HtnStore(self._store)
        review = htn.official_review_record(str(intent["review_package_id"]))
        if (
            review is None
            or review.purpose is not ReviewPurpose.ACTION_PROPOSAL
            or review.verdict is not ReviewVerdict.ACCEPT
        ):
            raise OperationReferenceUnavailable("official_review_missing")
        envelopes = [item for item in htn.list_operation_bindings(str(intent["mission_id"]))
                     if str(item.operation_occurrence_id) == str(link["operation_occurrence_id"])]
        if len(envelopes) != 1:
            raise OperationReferenceUnavailable("operation_binding_missing")
        envelope = envelopes[0]
        if (
            envelope.content_hash() != link["envelope_hash"]
            or str(envelope.operation_id) != str(link["operation_id"])
            or envelope.request_hash != proposal.request_hash
            or envelope.review_ref.id != str(review.record_id)
            or envelope.review_ref.kind is not TypedRefKind.REVIEW
            or envelope.review_ref.revision != 1
            or envelope.review_ref.content_hash != content_hash_of(review.to_json())
        ):
            raise OperationReferenceUnavailable("envelope_mismatch")
        if require_current:
            self._validate_current(intent, proposal, effect, htn)
        return ResolvedOperationReferences(
            receipt, intent, parameters, effect, proposal, review, envelope
        )

    def resolve_historical(
        self,
        action: dict[str, Any],
        link: dict[str, Any],
    ) -> ResolvedOperationReferences:
        """Read executed facts without pretending the original Plan is still active.

        T3 separately binds these facts to the current approved effect owner.
        This method grants no handoff authority.
        """
        receipt = self._store.get_receipt(str(link.get("provenance_receipt_id", "")))
        if receipt is None or receipt.get("kind") != "operation_intent_submitted":
            raise OperationReferenceUnavailable("operation_submission_missing")
        return self._resolve_t0(action, link, dict(receipt), require_current=False)

    def _validate_current(
        self,
        intent: dict[str, Any],
        proposal: FrozenActionProposalV2,
        effect: OperationEffectContractV2,
        htn: HtnStore,
    ) -> None:
        active = htn.active_plan_revision(str(intent["mission_id"]))
        if active is None:
            raise OperationReferenceUnavailable("no_active_plan")
        plan = PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash)
        reader = OperationCompletionReader(self._store)
        scopes = tuple(
            reader.read_scope(str(intent["mission_id"]), plan, str(member.occurrence_id))
            for member in htn.list_plan_memberships(str(intent["mission_id"]), active.revision)
        )
        producer = [
            scope for scope in scopes if scope.occurrence_id == proposal.producer_occurrence_id
        ]
        owners = [
            scope
            for scope in scopes
            if scope.occurrence_id == proposal.completion_owner_occurrence_id
        ]
        if len(producer) != 1 or len(owners) != 1:
            raise OperationReferenceUnavailable("current_scope_missing")
        current, owner = producer[0], owners[0]
        if (
            current.task_ref.id != proposal.producer_task_ref.id
            or current.task_ref.revision != proposal.producer_task_ref.revision
            or current.task_ref.content_hash != proposal.producer_task_ref.content_hash
            or current.requirements_ref.id != proposal.requirements_ref.id
            or current.requirements_ref.revision != proposal.requirements_ref.revision
            or current.requirements_ref.content_hash != proposal.requirements_ref.content_hash
            or owner.spec_hash != proposal.completion_spec_hash
            or proposal.effect_key not in owner.owned_effect_keys
        ):
            raise OperationReferenceUnavailable("current_contract_or_scope_changed")
        from ..storage.operation_completion_store import OperationCompletionStore

        original = OperationCompletionStore(self._store).get_scope_exact(
            str(intent["mission_id"]),
            proposal.plan_revision,
            proposal.completion_owner_occurrence_id,
        )
        if (
            original is None
            or original["document"].content_hash() != proposal.completion_scope_hash
            or (
                owner.task_ref,
                owner.obligation_id,
                owner.owned_effect_keys,
                owner.required_effect_keys,
            )
            != (
                original["document"].task_ref,
                original["document"].obligation_id,
                original["document"].owned_effect_keys,
                original["document"].required_effect_keys,
            )
        ):
            raise OperationReferenceUnavailable("current_effect_owner_changed")
        spec = reader.read_requirements(
            str(intent["mission_id"]),
            TypedRef(
                TypedRefKind.REQUIREMENTS,
                owner.requirements_ref.id,
                owner.requirements_ref.revision,
                owner.requirements_ref.content_hash,
            ),
        )
        slot = spec.effect(proposal.effect_key)
        if (
            slot.required_milestone != effect.required_milestone
            or slot.criterion_ids != effect.milestone_criterion_ids
        ):
            raise OperationReferenceUnavailable("current_effect_changed")
        for ref in proposal.prepared_acceptance_refs:
            acceptance = htn.get_acceptance(ref.id)
            if acceptance.validity is not Validity.CURRENT or ref.content_hash != content_hash_of(
                acceptance.to_json()
            ):
                raise OperationReferenceUnavailable("accepted_input_changed")


__all__ = (
    "OperationReferenceResolver",
    "OperationReferenceUnavailable",
    "ResolvedOperationReferences",
)

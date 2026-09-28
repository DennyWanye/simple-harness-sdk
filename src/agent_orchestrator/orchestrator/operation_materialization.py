# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""T0 intent registration and T1 materialization on the original CommitService."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.operation_intents import SubmitOperationIntentV2
from ..contracts.operation_payloads import PayloadKind
from ..contracts.resolution import OperationEnvelope, OperationKind, ReviewPurpose, ReviewVerdict
from ..contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from ..governance.budgets import BudgetError
from ..governance.permissions import Principal
from ..planning.htn.grounding import derive_id
from ..runtime.planning_operations import BoundPlanningOperationOrigin
from ..storage.htn_store import HtnStore
from ..storage.operation_intent_store import OperationIntentStore
from ..storage.operation_payload_store import OperationPayloadStore
from ..storage.planning_admission_store import PlanningAdmissionStore
from .operation_completion import OperationCompletionError
from .operation_intent_sources import prepare_operation_intent_sources


@dataclass(frozen=True, slots=True)
class OperationMaterializationRuntime:
    """Deployment-owned dependencies. Never decoded from a user/model command."""

    connectors: Any
    deployment: Any
    profiles: Any
    policy_for: Callable[[Any], Any]
    prepare_review: Callable[[Any, Any, str], Any]
    service_authority: object


class OperationMaterializationCommitsMixin:
    def bind_operation_materialization_runtime(
        self, runtime: OperationMaterializationRuntime
    ) -> None:
        if not isinstance(runtime, OperationMaterializationRuntime):
            raise TypeError("a deployment-bound operation runtime is required")
        if getattr(self, "_operation_materialization_runtime", None) is not None:
            raise ValueError("operation runtime is already bound")
        self._operation_materialization_runtime = runtime

    def _operation_runtime(self) -> OperationMaterializationRuntime:
        runtime = getattr(self, "_operation_materialization_runtime", None)
        if runtime is None or self._source_artifact_store is None:
            raise OperationCompletionError(
                "OP_PROFILE_UNAVAILABLE", "operation runtime is unavailable"
            )
        return runtime

    def _operation_caller(self, mission_id: str, tenant_id: str, principal: Principal) -> Any:
        if not isinstance(principal, Principal) or principal.kind != "human":
            raise OperationCompletionError(
                "OP_INTENT_CALLER_UNAUTHENTICATED", "human caller required"
            )
        mission = self._store.get_mission(mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            raise OperationCompletionError("not_found", "no such operation for this caller")
        return mission

    def _operation_replay(self, receipt_id: str, submission_hash: str) -> dict[str, Any] | None:
        previous = self._store.get_receipt(receipt_id)
        if previous is not None and (
            previous.get("kind") != "operation_intent_submitted"
            or previous.get("submission_hash") != submission_hash
        ):
            raise OperationCompletionError("OP_INTENT_CONFLICT", "idempotency key already used")
        return previous

    def _operation_inputs(
        self,
        command: SubmitOperationIntentV2,
        tenant_id: str,
        principal: Principal,
        intent_id: str,
        object_ids: tuple[str, str, str],
    ) -> tuple[Any, Any]:
        from .operation_materialization_inputs import build_operation_materialization_inputs

        runtime = self._operation_runtime()
        sources = prepare_operation_intent_sources(
            self._store,
            self._source_artifact_store,
            command,
            tenant_id=tenant_id,
            principal=principal,
        )
        payloads = build_operation_materialization_inputs(
            sources,
            store=self._store,
            connectors=runtime.connectors,
            deployment=runtime.deployment,
            profiles=runtime.profiles,
            intent_id=intent_id,
            parameters_object_id=object_ids[0],
            effect_object_id=object_ids[1],
            proposal_object_id=object_ids[2],
            policy=runtime.policy_for(sources),
        )
        return sources, payloads

    def _operation_slot(
        self, command: SubmitOperationIntentV2, *, excluding: str | None = None
    ) -> None:
        rows = [
            row
            for row in OperationIntentStore(self._store).for_mission(command.mission_id)
            if row["binding"].get("command", {}).get("completion_slot")
            == command.completion_slot.to_json()
        ]
        superseded = {row["supersedes_intent_id"] for row in rows if row["supersedes_intent_id"]}
        heads = [
            row
            for row in rows
            if row["intent_id"] not in superseded and row["intent_id"] != excluding
        ]
        if excluding is not None:
            # A later command has superseded this branch; do not materialize its old review.
            if excluding in superseded or heads:
                raise OperationCompletionError(
                    "OP_COMPLETION_INTENT_AMBIGUOUS", "intent branch is no longer current"
                )
            return
        if (
            len(heads) > 1
            or (heads and command.supersedes_intent_id != heads[0]["intent_id"])
            or (not heads and command.supersedes_intent_id is not None)
        ):
            raise OperationCompletionError(
                "OP_COMPLETION_INTENT_AMBIGUOUS", "completion slot has another intent branch"
            )
        if heads:
            materialized = self._store.get_receipt("materialize:" + heads[0]["intent_id"])
            if materialized is not None:
                # Replacing a real effect requires its original reconciliation/retirement
                # proof. A new command alone must never bypass an UNKNOWN action.
                raise OperationCompletionError(
                    "OP_NONAPPLICATION_PROOF_INSUFFICIENT",
                    "materialized intent requires reconciliation before replacement",
                )

    def submit_operation_intent(
        self, command: SubmitOperationIntentV2, *, tenant_id: str, principal: Principal
    ) -> dict[str, Any]:
        command = SubmitOperationIntentV2.from_json(command.to_json())
        self._operation_caller(command.mission_id, tenant_id, principal)
        submission_hash = content_hash_of(
            {
                "command": command.to_json(),
                "tenant_id": tenant_id,
                "principal_id": principal.principal_id,
            }
        )
        receipt_id = derive_id(
            "op-intent-submit",
            tenant_id,
            principal.principal_id,
            command.mission_id,
            command.idempotency_key,
        )
        previous = self._operation_replay(receipt_id, submission_hash)
        if previous is not None:
            return previous
        # Identity is allocated for this explicit command, never inferred from payload hashes.
        intent_id = "operation-intent-" + uuid.uuid4().hex
        object_ids = tuple("operation-payload-" + uuid.uuid4().hex for _ in range(3))
        sources, payloads = self._operation_inputs(
            command, tenant_id, principal, intent_id, object_ids
        )
        runtime = self._operation_runtime()
        package_id = derive_id(
            "op-proposal-review", intent_id, payloads.action_proposal_ref.content_hash
        )
        with self._store.transaction():
            self._operation_caller(command.mission_id, tenant_id, principal)
            previous = self._operation_replay(receipt_id, submission_hash)
            if previous is not None:
                return previous
            self._operation_slot(command)
            current_sources, current_payloads = self._operation_inputs(
                command, tenant_id, principal, intent_id, object_ids
            )
            if current_payloads.action_proposal_ref != payloads.action_proposal_ref:
                raise OperationCompletionError(
                    "OP_INPUT_NOT_CURRENT", "inputs changed during operation preparation"
                )
            sources, payloads = current_sources, current_payloads
            receipt = {
                "kind": "operation_intent_submitted",
                "command_id": receipt_id,
                "intent_id": intent_id,
                "mission_id": command.mission_id,
                "tenant_id": tenant_id,
                "principal_id": principal.principal_id,
                "submission_hash": submission_hash,
                "origin_receipt_id": receipt_id,
                "slot_key": "operation",
                "review_package_id": package_id,
                "request_hash": payloads.action_proposal.request_hash,
                "parameters_ref": payloads.parameters_ref.to_json(),
                "effect_contract_ref": payloads.effect_contract_ref.to_json(),
                "proposal_ref": payloads.action_proposal_ref.to_json(),
                "completion_slot": command.completion_slot.to_json(),
            }
            self._store.insert_receipt(
                commit_id=receipt_id,
                kind=receipt["kind"],
                subject_id=intent_id,
                base_version=sources.plan_ref.revision,
                proposal_hash=submission_hash,
                receipt=receipt,
            )
            payload_store = OperationPayloadStore(self._store)
            for ref, kind, document in (
                (payloads.parameters_ref, PayloadKind.PARAMETERS, payloads.parameters),
                (
                    payloads.effect_contract_ref,
                    PayloadKind.EFFECT_CONTRACT,
                    payloads.effect_contract,
                ),
                (
                    payloads.action_proposal_ref,
                    PayloadKind.ACTION_PROPOSAL,
                    payloads.action_proposal,
                ),
            ):
                stored = payload_store.put_payload(
                    mission_id=command.mission_id,
                    object_id=ref.id,
                    kind=kind,
                    payload=document,
                    source_receipt_id=receipt_id,
                    cas=self._source_artifact_store,
                )
                if stored != ref:
                    raise OperationCompletionError(
                        "OP_PAYLOAD_HASH_MISMATCH", "CAS reference differs"
                    )
            # This callback only constructs the durable review request and reserves its
            # actual Task budget. AgentBridge performs external work after this commit.
            try:
                runtime.prepare_review(sources, payloads, package_id)
            except BudgetError as error:
                raise OperationCompletionError(
                    "OP_REVIEW_BUDGET_UNAVAILABLE", str(error)
                ) from error
            task_ref = sources.producer_scope.task_ref
            req_ref = sources.owner_scope.requirements_ref
            binding = {
                "command": command.to_json(),
                "completion": {
                    "spec_id": derive_id(
                        "op-completion-spec",
                        command.mission_id,
                        req_ref.revision,
                        req_ref.content_hash,
                    ),
                    "spec_hash": sources.spec.content_hash(),
                    "effect_key": sources.effect.effect_key,
                    "scope_id": sources.owner_scope.scope_id,
                    "scope_hash": sources.owner_scope.content_hash(),
                    "completion_owner_occurrence_id": sources.owner_scope.occurrence_id,
                },
                "connector_profile_hash": payloads.profile.content_hash(),
            }
            OperationIntentStore(self._store).insert(
                {
                    "intent_id": intent_id,
                    "mission_id": command.mission_id,
                    "tenant_id": tenant_id,
                    "principal_id": principal.principal_id,
                    "scope_id": "mission",
                    "source_kind": str(command.intent_source.kind),
                    "origin_receipt_id": command.intent_source.origin_receipt_id or receipt_id,
                    # 同一授权槽可替代重交（审阅没做成、内容换了新版本）：行上的槽键带上本次
                    # 申请号，唯一约束（任务+来源回执+槽键）仍成立；授权核对用命令里的槽键。
                    "slot_key": (f"{command.intent_source.slot_key}@{intent_id}"
                                 if command.intent_source.slot_key else "operation"),
                    "submission_receipt_id": receipt_id,
                    "submission_hash": submission_hash,
                    "supersedes_intent_id": command.supersedes_intent_id,
                    "producer_task_id": task_ref.id,
                    "producer_occurrence_id": sources.producer_scope.occurrence_id,
                    "obligation_id": sources.owner_scope.obligation_id,
                    "source_result_id": sources.producer_result.envelope.id,
                    "source_attempt_id": sources.producer_attempt.id,
                    "candidate_artifact_id": sources.candidate_artifact.id,
                    "candidate_file_hash": sources.candidate_artifact.content_hash,
                    "task_contract_revision": task_ref.revision,
                    "task_contract_hash": task_ref.content_hash,
                    "requirements_revision": req_ref.revision,
                    "requirements_hash": req_ref.content_hash,
                    "plan_revision": sources.plan_ref.revision,
                    "plan_snapshot_hash": sources.plan_ref.snapshot_hash,
                    "parameters_object_id": payloads.parameters_ref.id,
                    "parameters_content_hash": payloads.parameters_ref.content_hash,
                    "params_hash": payloads.parameters.params_hash,
                    "effect_object_id": payloads.effect_contract_ref.id,
                    "effect_content_hash": payloads.effect_contract_ref.content_hash,
                    "proposal_object_id": payloads.action_proposal_ref.id,
                    "proposal_content_hash": payloads.action_proposal_ref.content_hash,
                    "request_hash": payloads.action_proposal.request_hash,
                    "review_package_id": package_id,
                    "binding_json": canonical_json(binding),
                    "created_at_ms": int(self._store.now * 1000),
                }
            )
            self._emit(
                "OperationIntentSubmitted", command.mission_id, key=intent_id, payload=receipt
            )
            self._store.fault("operation_intent_before_commit", "operation_materialization")
        return receipt

    def operation_intent_status(
        self, intent_id: str, *, tenant_id: str, principal: Principal
    ) -> dict[str, Any]:
        with self._store.read_view():
            row = OperationIntentStore(self._store).get(intent_id)
            if row is None:
                raise OperationCompletionError("not_found", "no such operation for this caller")
            self._operation_caller(row["mission_id"], tenant_id, principal)
            review = HtnStore(self._store).official_review_record(row["review_package_id"])
            materialized = self._store.get_receipt("materialize:" + intent_id)
            action = (
                None if materialized is None else self._store.get_action(materialized["action_key"])
            )
            dispatch = self._store.get_intent_for_subject("operation-review:" + intent_id)
            state = (
                str(action["state"])
                if action is not None
                else str(review.verdict)
                if review is not None
                else "REVIEW_FAILED"
                if dispatch is not None and dispatch.state == "FAILED"
                else "AWAITING_REVIEW"
            )
            from .completion_status import read_current_effect

            slot = row["binding"]["command"]["completion_slot"]
            completion = read_current_effect(
                self._store, row["mission_id"], slot["spec_hash"], slot["effect_key"],
            )
            return {
                "intent_id": intent_id,
                "mission_id": row["mission_id"],
                "state": state,
                "completion": completion,
                "review_package_id": row["review_package_id"],
                "review_record_id": None if review is None else str(review.record_id),
                "materialization": materialized,
                "completion_slot": row["binding"]["command"]["completion_slot"],
            }

    def materialize_reviewed_operation(
        self,
        *,
        intent_id: str,
        official_review_ref: TypedRef,
        command_id: str,
        service_authority: object,
    ) -> dict[str, Any]:
        runtime = self._operation_runtime()
        if (
            service_authority is not runtime.service_authority
            or command_id != "materialize:" + intent_id
        ):
            raise OperationCompletionError(
                "OP_REVIEW_NOT_OFFICIAL", "unbound materialization service"
            )
        with self._store.transaction():
            row = OperationIntentStore(self._store).get(intent_id)
            if row is None:
                raise OperationCompletionError(
                    "OP_INTENT_SOURCE_UNRESOLVED", "intent is unavailable"
                )
            previous = self._store.get_receipt(command_id)
            if previous is not None:
                if previous.get("review_ref") != official_review_ref.to_json():
                    raise OperationCompletionError(
                        "OP_INTENT_CONFLICT", "materialization review differs"
                    )
                return previous
            principal = Principal(principal_id=row["principal_id"], kind="human")
            command = SubmitOperationIntentV2.from_json(row["binding"]["command"])
            self._operation_slot(command, excluding=intent_id)
            sources, payloads = self._operation_inputs(
                command,
                row["tenant_id"],
                principal,
                intent_id,
                (row["parameters_object_id"], row["effect_object_id"], row["proposal_object_id"]),
            )
            for ref, kind, hash_column in (
                (payloads.parameters_ref, PayloadKind.PARAMETERS, "parameters_content_hash"),
                (payloads.effect_contract_ref, PayloadKind.EFFECT_CONTRACT, "effect_content_hash"),
                (
                    payloads.action_proposal_ref,
                    PayloadKind.ACTION_PROPOSAL,
                    "proposal_content_hash",
                ),
            ):
                stored = OperationPayloadStore(self._store).get_payload(
                    mission_id=row["mission_id"],
                    ref=ref,
                    expected_kind=kind,
                    cas=self._source_artifact_store,
                )
                if (
                    ref.content_hash != row[hash_column]
                    or stored.source_receipt_id != row["submission_receipt_id"]
                ):
                    raise OperationCompletionError(
                        "OP_INPUT_NOT_CURRENT", "frozen operation inputs changed"
                    )
            semantics = HtnStore(self._store)
            stored_review = semantics.get_review_record(official_review_ref.id)
            review = stored_review.record
            package = semantics.get_review_package(row["review_package_id"])
            if (
                official_review_ref.kind is not TypedRefKind.REVIEW
                or official_review_ref.revision != 1
                or official_review_ref.content_hash != content_hash_of(review.to_json())
                or not stored_review.official
                or review.package_id != package.package_id
                or package.purpose is not ReviewPurpose.ACTION_PROPOSAL
                or review.verdict is not ReviewVerdict.ACCEPT
                or review.reviewer_agent_id in package.producer_agent_ids
            ):
                raise OperationCompletionError(
                    "OP_REVIEW_NOT_OFFICIAL", "exact independent proposal ACCEPT required"
                )
            from .operation_proposal_review import validate_materialization_review

            validate_materialization_review(self._store, sources, payloads, package, review)
            connector = runtime.connectors[payloads.parameters.connector_id]
            operation_spec = connector.operations[payloads.parameters.operation_name]
            kinds = {
                "read": OperationKind.READ,
                "state": OperationKind.STATE_WRITE,
                "event": OperationKind.EVENT_WRITE,
            }
            if operation_spec.kind not in kinds:
                raise OperationCompletionError(
                    "OP_CAPABILITY_UNSUPPORTED", "unsupported connector operation kind"
                )
            envelope = OperationEnvelope(
                operation_id="operation-" + uuid.uuid4().hex,
                operation_occurrence_id="operation-occurrence-" + uuid.uuid4().hex,
                mission_id=row["mission_id"],
                obligation_id=row["obligation_id"],
                scope_id=row["scope_id"],
                connector_id=payloads.parameters.connector_id,
                connector_version=payloads.parameters.connector_version,
                operation_name=payloads.parameters.operation_name,
                operation_kind=kinds[operation_spec.kind],
                target_ref=payloads.parameters.normalized_target_ref,
                expected_target_version=payloads.parameters.expected_target_version,
                parameters_artifact_ref=payloads.parameters_ref,
                request_hash=row["request_hash"],
                requirements_revision=row["requirements_revision"],
                review_ref=official_review_ref,
                accepted_input_refs=command.prepared_acceptance_refs,
                effect_contract_ref=payloads.effect_contract_ref,
            )
            semantics.bind_operation(
                envelope, principal_id=row["principal_id"], scope_id=row["scope_id"]
            )
            origin = BoundPlanningOperationOrigin(
                str(envelope.operation_id),
                str(envelope.operation_occurrence_id),
                envelope.request_hash,
                envelope.mission_id,
                envelope.content_hash(),
                row["principal_id"],
                row["scope_id"],
                row["obligation_id"],
                row["producer_task_id"],
                row["producer_occurrence_id"],
                row["task_contract_revision"],
                row["plan_revision"],
                row["submission_receipt_id"],
            )
            action = self.propose_action(
                payloads.raw_candidate,
                mission_id=row["mission_id"],
                task_id=row["producer_task_id"],
                result_id=row["source_result_id"],
                attempt_id=row["source_attempt_id"],
                artifact_id=row["candidate_artifact_id"],
                artifact_hash=row["candidate_file_hash"],
                connectors=runtime.connectors,
                deployment=runtime.deployment,
                planning_origin=origin,
                operation_parameters=payloads.parameters,
                # 系统按已批准效果准备的申请单（AUTHORIZED_SLOT）：理由是系统写的
                reason_source=("system" if str(row["source_kind"]) == "AUTHORIZED_SLOT"
                               else "model (untrusted)"),
            )
            if action["state"] == "REFUSED":
                raise OperationCompletionError("OP_INTENT_CONFLICT", str(action.get("refused")))
            link = PlanningAdmissionStore(self._store).get_operation_action_link(
                str(envelope.operation_id)
            )
            if link is None:
                raise OperationCompletionError(
                    "OP_REF_UNAVAILABLE", "materialization produced no exact action link"
                )
            receipt = {
                "kind": "operation_materialized",
                "command_id": command_id,
                "mission_id": row["mission_id"],
                "intent_id": intent_id,
                "operation_id": str(envelope.operation_id),
                "operation_occurrence_id": str(envelope.operation_occurrence_id),
                "envelope_hash": envelope.content_hash(),
                "review_ref": official_review_ref.to_json(),
                "source_receipt_id": row["submission_receipt_id"],
                "action_key": action["action_key"],
                "action_version": action["version"],
                "link_hash": link["link_hash"],
            }
            self._store.insert_receipt(
                commit_id=command_id,
                kind=receipt["kind"],
                subject_id=intent_id,
                base_version=row["plan_revision"],
                proposal_hash=row["request_hash"],
                receipt=receipt,
            )
            self._emit("OperationMaterialized", row["mission_id"], key=intent_id, payload=receipt)
            self._store.fault("operation_materialized_before_commit", "operation_materialization")
            return receipt

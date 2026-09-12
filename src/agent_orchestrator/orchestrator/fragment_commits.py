# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Atomic fragment projection + real validation Task; no fragment PASS flag."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..artifacts.store import ArtifactStoreError, read_verified
from ..contracts import ClaimStatus, ContractError, MissionStatus, TaskStatus
from ..contracts.assessments import CriterionAssessmentV1, thaw_json
from ..contracts.fragments import FragmentProposalV1, ScopeProjectionV1
from ..contracts.models import canonical_json, sha256_hex
from ..governance.domains import DOC_DOMAIN, requires_document_critic_proof
from ..graph.changes import ChangeLimits, TaskGraphChange
from ..planning.fragments import (
    _current_sources,
    _task_contract,
    current_task_revision,
    project_fragment,
    read_input_closure,
    revision_for_result,
)
from ..verification.assessments import (
    accepted_assessments_for,
    criterion_id,
    task_contract_revision,
)
from .state_machine import next_task


def _proposal(value: FragmentProposalV1 | Mapping[str, Any]) -> FragmentProposalV1:
    return value if isinstance(value, FragmentProposalV1) else FragmentProposalV1.from_json(value)


def _command_body(proposal: FragmentProposalV1) -> dict[str, Any]:
    body = proposal.to_json()
    body.pop("rationale")
    for field in ("criterion_ids", "claim_refs", "material_refs"):
        body[field] = sorted(body[field], key=canonical_json)
    return body


class FragmentCommitsMixin:
    def project_fragment(
        self: Any, proposal: FragmentProposalV1 | Mapping[str, Any]
    ) -> ScopeProjectionV1:
        with self._store.read_view():
            return project_fragment(self._store, self._source_cas(), _proposal(proposal))

    def commit_fragment_validation(
        self: Any,
        proposal: FragmentProposalV1 | Mapping[str, Any],
        *,
        command_id: str,
        base_graph_version: int,
        source: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        proposal = _proposal(proposal)
        if not isinstance(command_id, str) or not command_id.strip() or not source:
            raise ContractError("fragment command requires identity and provenance")
        if type(base_graph_version) is not int or base_graph_version < 1:
            raise ContractError("fragment graph version must be positive")
        command_key = "fragment-command-" + sha256_hex(
            {"mission": proposal.origin["mission_id"], "command": command_id}
        )
        command_hash = sha256_hex(_command_body(proposal))
        with self._store.transaction() as connection:
            known = self._store.get_receipt(command_key)
            if known is not None:
                if known["proposal_hash"] != command_hash:
                    raise ContractError("fragment command identity reused with different proposal")
                return known["receipt"]
            projection = project_fragment(self._store, self._source_cas(), proposal)
            row = connection.execute(
                "SELECT projection_receipt_id FROM fragment_validations WHERE fragment_id=?",
                (projection.fragment_id,),
            ).fetchone()
            if row is not None:
                receipt = self._store.get_receipt(row[0])
                if receipt is None:
                    raise ContractError("fragment projection receipt unavailable")
            else:
                mission = self._require_mission(proposal.origin["mission_id"])
                if mission.status is not MissionStatus.ACTIVE:
                    raise ContractError("fragment validation requires an active Mission")
                original = projection.origin_revision
                constraints = original["execution_constraints"]
                if constraints["policy_binding"] != self._store.get_mission_policy(mission.id):
                    raise ContractError("fragment frozen policy binding changed")
                # The old Task is provenance (parent_task_ids), never a dependency
                # that would require a failed origin to become COMPLETED.
                goal = (
                    "独立验证以下原准则；未选范围不在本 Task 结论内。原目标："
                    + original["contract"]["goal"]
                )
                fixed_reason = (
                    "whole-criterion-v1:"
                    + projection.fragment_id
                    + "\n原任务条件（完整保留）："
                    + original["contract"]["rationale"]
                    + "\n不在本次结论范围内的原准则："
                    + canonical_json([item["text"] for item in projection.outside_scope])
                )
                change = TaskGraphChange.from_json(
                    {
                        "base_graph_version": base_graph_version,
                        "basis": {"trigger": "fragment_validation", **dict(proposal.origin)},
                        "rationale": fixed_reason,
                        "operations": [
                            {
                                "op": "add_task",
                                "key": projection.fragment_id,
                                "goal": goal,
                                "rationale": fixed_reason,
                                "success_criteria": [item["text"] for item in projection.criteria],
                                "dependencies": [],
                                "parent_task_ids": [original["task_id"]],
                                "verification_policy": list(
                                    original["contract"]["verification_policy"]
                                ),
                                "allowed_tools": list(constraints["allowed_tools"]),
                                "budget": thaw_json(constraints["budget"]),
                                "outputs": list(original["contract"]["outputs"]),
                                "role": "worker",
                            }
                        ],
                    }
                )
                created, graph = self._commit_graph_change(
                    mission.id,
                    change,
                    source={**dict(source), "fragment_id": projection.fragment_id},
                    limits=ChangeLimits(),
                    allow_rebase=False,
                )
                if len(created) != 1:
                    raise ContractError("fragment validation graph did not create exactly one Task")
                task = created[0]
                if task.allowed_tools != tuple(
                    constraints["allowed_tools"]
                ) or task.budget.to_json() != thaw_json(constraints["budget"]):
                    raise ContractError("fragment graph changed inherited execution constraints")
                receipt_id = "fragment-projection-" + projection.fragment_id.removeprefix(
                    "fragment-"
                )
                task = next_task(
                    task,
                    context={
                        **dict(task.context),
                        "fragment_validation": {
                            "fragment_id": projection.fragment_id,
                            "projection_receipt_id": receipt_id,
                        },
                    },
                )
                self._store.update_task(task, expected_version=created[0].version)
                new_revision = task_contract_revision(_task_contract(task))
                mapping = [
                    {
                        "origin_criterion_id": item["id"],
                        "criterion_id": criterion_id(new_revision, ordinal, item["text"]),
                        "ordinal": ordinal,
                        "text": item["text"],
                    }
                    for ordinal, item in enumerate(projection.criteria, 1)
                ]
                receipt = {
                    "schema_version": 1,
                    "fragment_id": projection.fragment_id,
                    "mission_id": mission.id,
                    "origin": dict(proposal.origin),
                    "proposal": proposal.to_json(),
                    "projection": projection.to_json(),
                    "projection_hash": projection.projection_hash,
                    "projection_receipt_id": receipt_id,
                    "validation_task_id": task.id,
                    "validation_task_contract_revision": new_revision,
                    "criterion_mapping": mapping,
                    "graph_change_id": graph["change_id"],
                    "source": dict(source),
                    "command_id": command_id,
                }
                self._store.insert_receipt(
                    commit_id=receipt_id,
                    kind="fragment_projection",
                    subject_id=projection.fragment_id,
                    base_version=base_graph_version,
                    proposal_hash=projection.projection_hash,
                    receipt=receipt,
                )
                connection.execute(
                    "INSERT INTO fragment_validations VALUES (?,?,?,?,?,?)",
                    (
                        projection.fragment_id,
                        mission.id,
                        proposal.origin["result_id"],
                        task.id,
                        receipt_id,
                        self._store.now,
                    ),
                )
                self._emit(
                    "FragmentValidationCommitted",
                    mission.id,
                    key=projection.fragment_id,
                    task_id=task.id,
                    payload=receipt,
                )
            self._store.insert_receipt(
                commit_id=command_key,
                kind="fragment_command",
                subject_id=proposal.origin["mission_id"],
                base_version=base_graph_version,
                proposal_hash=command_hash,
                receipt={"proposal_hash": command_hash, "receipt": receipt},
            )
            return receipt

    def _fragment_receipt(self: Any, fragment_id: str) -> Mapping[str, Any]:
        with self._store.read_view() as connection:
            row = connection.execute(
                "SELECT mission_id,origin_result_id,validation_task_id,projection_receipt_id "
                "FROM fragment_validations WHERE fragment_id=?",
                (fragment_id,),
            ).fetchone()
            receipt = None if row is None else self._store.get_receipt(row[3])
            if (
                receipt is None
                or receipt.get("fragment_id") != fragment_id
                or receipt.get("mission_id") != row[0]
                or receipt.get("validation_task_id") != row[2]
                or receipt.get("origin", {}).get("result_id") != row[1]
                or receipt.get("projection_hash") != sha256_hex(receipt.get("projection"))
            ):
                raise ContractError("fragment receipt/index binding unavailable")
            return receipt

    def fragment_validation_binding(self: Any, task_id: str) -> Mapping[str, Any]:
        """Runtime uses these exact source versions, never today's active catalog."""
        task = self._require_task(task_id)
        marker = task.context.get("fragment_validation")
        if not isinstance(marker, Mapping):
            return {}
        receipt = self._fragment_receipt(marker["fragment_id"])
        if (
            receipt["validation_task_id"] != task.id
            or marker.get("projection_receipt_id") != receipt["projection_receipt_id"]
            or task_contract_revision(_task_contract(task))
            != receipt["validation_task_contract_revision"]
        ):
            raise ContractError("fragment validation Task binding changed")
        original = receipt["projection"]["origin_revision"]["execution_constraints"]
        if (
            task.allowed_tools != tuple(original["allowed_tools"])
            or task.budget.to_json() != original["budget"]
            or task.dependency_ids
        ):
            raise ContractError("fragment validation constraints changed")
        return {
            "source_versions": dict(original["source_versions"]),
            "source_roots": list(original["source_roots"]),
        }

    def fragment_validation_inputs(self: Any, task_id: str) -> dict[str, bytes]:
        task = self._require_task(task_id)
        if "fragment_validation" not in task.context:
            return {}
        self.fragment_validation_binding(task_id)
        receipt = self._fragment_receipt(task.context["fragment_validation"]["fragment_id"])
        projection = project_fragment(
            self._store, self._source_cas(), FragmentProposalV1.from_json(receipt["proposal"])
        )
        if projection.projection_hash != receipt["projection_hash"]:
            raise ContractError("fragment original projection changed")
        return read_input_closure(self._store, self._source_cas(), receipt["projection"])

    def fragment_validation_context(self: Any, task_id: str) -> Mapping[str, Any]:
        """Bound context metadata, keeping original Claim wording and limitations.

        Material text remains in workspace/CAS, never promoted into system policy.
        This context explains a new independent Task, not a modified old verdict.
        """
        task = self._require_task(task_id)
        marker = task.context.get("fragment_validation")
        if not isinstance(marker, Mapping):
            return {}
        self.fragment_validation_binding(task_id)
        receipt = self._fragment_receipt(marker["fragment_id"])
        stored = self._require_result(receipt["origin"]["result_id"])
        claims = self._store.list_claims(stored.envelope.id)
        selected = {item["claim_id"] for item in receipt["projection"]["claim_refs"]}
        return {
            "schema_version": 1,
            "fragment_id": receipt["fragment_id"],
            "projection_receipt_id": receipt["projection_receipt_id"],
            "trust": "unverified_origin_material_requires_independent_verification",
            "origin_contract": receipt["projection"]["origin_revision"]["contract"],
            "criterion_mapping": receipt["criterion_mapping"],
            "outside_scope": receipt["projection"]["outside_scope"],
            "original_claims": [claim.to_json() for claim in claims if claim.id in selected],
            "original_limitations": [item.to_json() for item in stored.envelope.limitations],
            "material_refs": receipt["projection"]["material_refs"],
        }

    def fragment_input(
        self: Any, fragment_id: str, *, consumer_task_revision_id: str
    ) -> Mapping[str, Any]:
        """New consumption only, after actual independent verification and acceptance.

        A file-existence assessment supplies material, never a semantic Claim.
        Claim/receipt references come only from the new validation result.
        """
        with self._store.read_view():
            receipt = self._fragment_receipt(fragment_id)
            mission = self._require_mission(receipt["mission_id"])
            if mission.status is not MissionStatus.ACTIVE:
                raise ContractError("fragment new consumption requires an active Mission")
            consumers = []
            for candidate in self._store.list_tasks(mission.id):
                if candidate.status not in {
                    TaskStatus.READY,
                    TaskStatus.ACTIVE,
                    TaskStatus.VERIFYING,
                }:
                    continue
                try:
                    revision = current_task_revision(self._store, candidate)
                except ContractError:
                    continue
                if revision.revision_id == consumer_task_revision_id:
                    consumers.append((candidate, revision))
            if len(consumers) != 1:
                raise ContractError("fragment consumer revision is absent, stale, or foreign")
            consumer, consumer_revision = consumers[0]
            scope = {item["text"] for item in receipt["projection"]["criteria"]}
            if not scope <= set(consumer.success_criteria) or not set(
                consumer.allowed_tools
            ) <= set(
                receipt["projection"]["origin_revision"]["execution_constraints"]["allowed_tools"]
            ):
                raise ContractError("fragment scope or permissions do not cover consumer")
            task = self._require_task(receipt["validation_task_id"])
            self.fragment_validation_binding(task.id)
            self.fragment_validation_inputs(task.id)
            stored = (
                None
                if task.accepted_result_id is None
                else self._store.get_result(task.accepted_result_id)
            )
            if (
                task.status is not TaskStatus.COMPLETED
                or stored is None
                or stored.verification_state != "DONE"
                or stored.verdict != "PASS"
            ):
                raise ContractError("fragment validation has not actually been accepted")
            attempt = self._require_attempt(stored.envelope.attempt_id)
            rows = self._store.list_verifications(stored.envelope.id)
            by_layer = {row["layer"]: row for row in rows}
            for layer in task.verification_policy:
                actual = by_layer.get(layer)
                if actual is None or actual["status"] not in {"PASS", "NEEDS_HUMAN"}:
                    raise ContractError("fragment required verification missing")
                if (
                    actual["status"] == "NEEDS_HUMAN"
                    and by_layer.get("human_review", {}).get("status") != "PASS"
                ):
                    raise ContractError("fragment requires actual human decision")
            domain = self.domain_for(mission.id)
            if requires_document_critic_proof(domain):
                self._require_doc5_critic_pass(stored, task, attempt, domain, rows)
            new_revision = revision_for_result(self._store, self._source_cas(), stored.envelope.id)
            _current_sources(self._store, self._source_cas(), new_revision, stored.envelope)
            assessments: tuple[CriterionAssessmentV1, ...] = ()
            if domain.id == DOC_DOMAIN:
                _, assessments = accepted_assessments_for(self._store, task=task)
            semantic_ids = {
                row.claim_id
                for row in assessments
                if row.criterion_id
                in {item["criterion_id"] for item in receipt["criterion_mapping"]}
            }
            materials = []
            for artifact_id in stored.artifacts:
                artifact = self._store.get_artifact(artifact_id)
                if artifact is None or artifact.attempt_id != attempt.id:
                    raise ContractError("fragment accepted material binding unavailable")
                try:
                    read_verified(artifact)
                except ArtifactStoreError as error:
                    raise ContractError("fragment accepted material_unavailable") from error
                materials.append(
                    {
                        "kind": "artifact",
                        "artifact_id": artifact.id,
                        "content_hash": artifact.content_hash,
                        "path": artifact.path,
                    }
                )
            claims = [
                claim.to_json()
                for claim in self._store.list_claims(stored.envelope.id)
                if claim.id in semantic_ids
                and claim.status in {ClaimStatus.SUPPORTED, ClaimStatus.VERIFIED}
            ]
            if semantic_ids - {claim["id"] for claim in claims}:
                raise ContractError("fragment assessed claim is no longer eligible")
            return {
                "kind": "validated_fragment",
                "fragment_id": fragment_id,
                "validation_result_id": stored.envelope.id,
                "projection_receipt_id": receipt["projection_receipt_id"],
                "consumer_task_revision_id": consumer_revision.revision_id,
                "material_refs": materials,
                "criterion_mapping": receipt["criterion_mapping"],
                "claims": claims,
                "assessment_receipts": [row.to_json() for row in assessments],
            }

    def list_fragments(self: Any, mission_id: str) -> list[Mapping[str, Any]]:
        """Historical projection; does not imply current eligibility or new PASS."""
        with self._store.read_view() as connection:
            return [
                self._fragment_receipt(row[0])
                for row in connection.execute(
                    "SELECT fragment_id FROM fragment_validations WHERE mission_id=? "
                    "ORDER BY created_at,fragment_id",
                    (mission_id,),
                ).fetchall()
            ]

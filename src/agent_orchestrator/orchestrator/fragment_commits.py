# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Atomic fragment projection + real validation Task; no fragment PASS flag."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..artifacts.store import ArtifactStoreError, read_verified
from ..contracts import (
    TERMINAL_ATTEMPT,
    AttemptStatus,
    ClaimStatus,
    ContractError,
    MissionStatus,
    TaskStatus,
)
from ..contracts.assessments import CriterionAssessmentV1, thaw_json
from ..contracts.fragments import FragmentProposalV1, ScopeProjectionV1
from ..contracts.models import canonical_json, sha256_hex
from ..governance.domains import DOC_DOMAIN, requires_document_critic_proof
from ..graph.changes import ChangeLimits, TaskGraphChange
from ..planning.fragments import (
    _current_sources,
    _revision,
    _task_contract,
    current_task_revision,
    fragment_validation_layout,
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
        limits: ChangeLimits | None = None,
    ) -> Mapping[str, Any]:
        proposal = _proposal(proposal)
        if not isinstance(command_id, str) or not command_id.strip() or not source:
            raise ContractError("fragment command requires identity and provenance")
        if type(base_graph_version) is not int or base_graph_version < 1:
            raise ContractError("fragment graph version must be positive")
        command_key = "fragment-command-" + sha256_hex(
            {"mission": proposal.origin["mission_id"], "command": command_id}
        )
        legacy_command_hash = sha256_hex(_command_body(proposal))
        command_hash = sha256_hex(
            {
                "proposal": _command_body(proposal),
                "base_graph_version": base_graph_version,
                "source": dict(source),
            }
        )
        with self._store.transaction() as connection:
            known = self._store.get_receipt(command_key)
            if known is not None:
                if known["proposal_hash"] == command_hash:
                    return known["receipt"]
                # Earlier receipts hashed only the normalized proposal. They did
                # persist command base/mission, but not the command's own source
                # when a second command reused an existing projection. Preserve
                # exact legacy replay without pretending that source was bound.
                row = connection.execute(
                    "SELECT kind,subject_id,base_version,proposal_hash FROM commit_receipts "
                    "WHERE commit_id=?",
                    (command_key,),
                ).fetchone()
                old_receipt = known.get("receipt")
                old_proposal = (
                    old_receipt.get("proposal") if isinstance(old_receipt, Mapping) else None
                )
                if (
                    known["proposal_hash"] == legacy_command_hash
                    and row is not None
                    and row["kind"] == "fragment_command"
                    and row["subject_id"] == proposal.origin["mission_id"]
                    and row["base_version"] == base_graph_version
                    and row["proposal_hash"] == legacy_command_hash
                    and isinstance(old_receipt, Mapping)
                    and old_receipt.get("mission_id") == proposal.origin["mission_id"]
                    and isinstance(old_proposal, Mapping)
                    and _command_body(_proposal(old_proposal))
                    == _command_body(proposal)
                    and self._store.get_receipt(old_receipt.get("projection_receipt_id", ""))
                    is not None
                ):
                    return old_receipt
                raise ContractError("fragment command identity reused with different proposal")
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
                output_paths, validation_criteria = fragment_validation_layout(projection.to_json())
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
                    + "\n原输入路径及CAS不变。独立输出必须写入以下映射的新路径；"
                    + "仅file完整谓词重绑定："
                    + canonical_json(thaw_json(output_paths))
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
                                "success_criteria": validation_criteria,
                                "dependencies": [],
                                "parent_task_ids": [original["task_id"]],
                                "verification_policy": list(
                                    original["contract"]["verification_policy"]
                                ),
                                "allowed_tools": list(constraints["allowed_tools"]),
                                "budget": thaw_json(constraints["budget"]),
                                "outputs": list(output_paths.values()),
                                "role": "worker",
                            }
                        ],
                    }
                )
                created, graph = self._commit_graph_change(
                    mission.id,
                    change,
                    source={**dict(source), "fragment_id": projection.fragment_id},
                    limits=limits if limits is not None else ChangeLimits(),
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
                        "criterion_id": criterion_id(
                            new_revision, ordinal, validation_criteria[ordinal - 1]
                        ),
                        "ordinal": ordinal,
                        "origin_text": item["text"],
                        "text": validation_criteria[ordinal - 1],
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
                    "path_mapping_version": "fragment-output-v1",
                    "output_path_mapping": output_paths,
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
        output_paths, criteria = fragment_validation_layout(receipt["projection"])
        expected_mapping = [
            {
                "origin_criterion_id": item["id"],
                "criterion_id": criterion_id(
                    receipt["validation_task_contract_revision"], ordinal, criteria[ordinal - 1]
                ),
                "ordinal": ordinal,
                "origin_text": item["text"],
                "text": criteria[ordinal - 1],
            }
            for ordinal, item in enumerate(receipt["projection"]["criteria"], 1)
        ]
        if (
            task.allowed_tools != tuple(original["allowed_tools"])
            or task.budget.to_json() != original["budget"]
            or task.dependency_ids
            or task.outputs != tuple(output_paths.values())
            or task.success_criteria != tuple(criteria)
            or receipt.get("path_mapping_version") != "fragment-output-v1"
            or receipt.get("output_path_mapping") != output_paths
            or receipt["criterion_mapping"] != expected_mapping
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

    def fragment_collection_baseline(self: Any, attempt_id: str) -> dict[str, str]:
        """Frozen initial hashes, not today's source catalog or workspace bytes.

        The ordinary collector still keeps listed and changed files. This only
        prevents an unchanged original CAS input from appearing newly produced.
        """
        attempt = self._require_attempt(attempt_id)
        task = self._require_task(attempt.task_id)
        marker = task.context.get("fragment_validation")
        if not isinstance(marker, Mapping):
            return {}
        self.fragment_validation_binding(task.id)
        receipt = self._fragment_receipt(marker["fragment_id"])
        intent = self._store.get_intent_for_subject(attempt_id)
        if intent is None:
            raise ContractError("fragment collection has no frozen intent")
        revision = _revision(self._store, intent)
        files = thaw_json(revision.execution_constraints["files"])
        if (
            revision.task_contract_revision != receipt["validation_task_contract_revision"]
            or files != receipt["projection"]["input_closure"]
        ):
            raise ContractError("fragment collection input binding changed")
        return {path: item["content_hash"] for path, item in files.items()}

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
            "output_path_mapping": receipt["output_path_mapping"],
            "path_mapping_version": receipt["path_mapping_version"],
            "outside_scope": receipt["projection"]["outside_scope"],
            "original_claims": [claim.to_json() for claim in claims if claim.id in selected],
            "original_limitations": [item.to_json() for item in stored.envelope.limitations],
            "material_refs": receipt["projection"]["material_refs"],
        }

    def fragment_input(
        self: Any,
        fragment_id: str,
        *,
        consumer_task_revision_id: str | None = None,
        ready_consumer_task_id: str | None = None,
        retry_consumer_task_id: str | None = None,
    ) -> Mapping[str, Any]:
        """New consumption only, after actual independent verification and acceptance.

        A file-existence assessment supplies material, never a semantic Claim.
        Claim/receipt references come only from the new validation result.
        ``ready_consumer_task_id`` is the first, unstarted admission. A retry
        requires failed historical Attempts with the same frozen consumption
        identity; both paths are rechecked with the new Attempt revision.
        """
        with self._store.read_view():
            receipt = self._fragment_receipt(fragment_id)
            mission = self._require_mission(receipt["mission_id"])
            if mission.status is not MissionStatus.ACTIVE:
                raise ContractError("fragment new consumption requires an active Mission")
            if sum(value is not None for value in (
                consumer_task_revision_id, ready_consumer_task_id, retry_consumer_task_id
            )) != 1:
                raise ContractError("fragment consumer needs exactly one frozen identity")
            retry_history: list[Mapping[str, Any]] = []
            if ready_consumer_task_id is not None:
                consumer = self._require_task(ready_consumer_task_id)
                if (
                    consumer.mission_id != mission.id
                    or consumer.status is not TaskStatus.READY
                    or self._store.list_attempts(consumer.id)
                ):
                    raise ContractError("fragment consumer is not an unstarted READY Task")
                consumer_revision = None
            elif retry_consumer_task_id is not None:
                consumer = self._require_task(retry_consumer_task_id)
                prior_attempts = self._store.list_attempts(consumer.id)
                if (
                    consumer.mission_id != mission.id
                    or consumer.status not in {TaskStatus.READY, TaskStatus.ACTIVE}
                    or consumer.accepted_result_id is not None
                    or not prior_attempts
                    or any(
                        attempt.status not in TERMINAL_ATTEMPT
                        or attempt.status is AttemptStatus.COMPLETED
                        or not attempt.failure
                        for attempt in prior_attempts
                    )
                ):
                    raise ContractError("fragment consumer has no eligible failed retry")
                for prior in prior_attempts:
                    intent = self._store.get_intent_for_subject(prior.id)
                    frozen = (
                        None if intent is None
                        else intent.config.get("validated_fragment_input")
                    )
                    if not isinstance(frozen, Mapping):
                        raise ContractError("fragment retry lost its frozen first admission")
                    retry_history.append(frozen)
                consumer_revision = None
            else:
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
            if not all(
                {item["origin_text"], item["text"]} & set(consumer.success_criteria)
                for item in receipt["criterion_mapping"]
            ) or not set(consumer.allowed_tools) <= set(
                receipt["projection"]["origin_revision"]["execution_constraints"]["allowed_tools"]
            ):
                raise ContractError("fragment scope or permissions do not cover consumer")
            task = self._require_task(receipt["validation_task_id"])
            if task.id not in consumer.dependency_ids:
                raise ContractError("fragment validation is not a consumer dependency")
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
            original_paths = {new: old for old, new in receipt["output_path_mapping"].items()}
            for artifact_id in stored.artifacts:
                artifact = self._store.get_artifact(artifact_id)
                if artifact is None or artifact.attempt_id != attempt.id:
                    raise ContractError("fragment accepted material binding unavailable")
                try:
                    read_verified(artifact)
                except ArtifactStoreError as error:
                    raise ContractError("fragment accepted material_unavailable") from error
                if artifact.path not in original_paths:
                    continue  # side artifacts have no projected output predicate
                materials.append(
                    {
                        "kind": "artifact",
                        "artifact_id": artifact.id,
                        "content_hash": artifact.content_hash,
                        "path": artifact.path,
                        "original_path": original_paths[artifact.path],
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
            binding = {
                "kind": "validated_fragment",
                "fragment_id": fragment_id,
                "validation_result_id": stored.envelope.id,
                "projection_receipt_id": receipt["projection_receipt_id"],
                "consumer_task_revision_id": (
                    None if consumer_revision is None else consumer_revision.revision_id
                ),
                "consumer_task_contract_revision": task_contract_revision(_task_contract(consumer)),
                "consumer_task_version": (
                    consumer.version
                    if consumer_revision is None
                    else consumer_revision.observed_task_version
                ),
                "consumer_dependency_ids": list(consumer.dependency_ids),
                "material_refs": materials,
                "criterion_mapping": receipt["criterion_mapping"],
                "claims": claims,
                "assessment_receipts": [row.to_json() for row in assessments],
            }
            if retry_history:
                # Task/Attempt versions legitimately advance. Every material,
                # dependency, contract and accepted-result identity must not.
                stable = {
                    key: value for key, value in binding.items()
                    if key not in {"consumer_task_revision_id", "consumer_task_version"}
                }
                if any(
                    {
                        key: value for key, value in frozen.items()
                        if key not in {"consumer_task_revision_id", "consumer_task_version"}
                    } != stable
                    for frozen in retry_history
                ):
                    raise ContractError("fragment retry consumption identity changed")
            return binding

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

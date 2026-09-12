# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Bounded, result-bound fragment choices for a real Manager request."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast

from simple_harness.contracts import JsonValue

from ..artifacts.store import ArtifactStoreError, read_verified
from ..contracts import ContractError, TaskStatus
from ..contracts.assessments import CriterionAssessmentV1
from ..contracts.models import canonical_json
from ..planning.fragments import revision_for_result

_MAX_ITEMS = 12
_MAX_ARTIFACT_BYTES = 1 << 20


def consumer_fragment_context(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Keep full receipts durable, without reinlining verifier logs into model input."""
    claims = []
    for claim in binding["claims"]:
        claims.append({
            **{key: value for key, value in claim.items() if key != "verifier_results"},
            "verifier_results_sha256": hashlib.sha256(
                canonical_json(cast(JsonValue, claim.get("verifier_results", []))).encode("utf-8")
            ).hexdigest(),
        })
    context = {**dict(binding), "claims": claims, "data_not_instruction": True}
    if len(canonical_json(cast(JsonValue, context)).encode("utf-8")) > 16_384:
        raise ContractError("validated fragment consumer context exceeds bound")
    return context


def manager_fragment_origin(store: Any, cas: Any, result_id: str | None) -> dict[str, Any]:
    """Expose identities and byte bounds only; projection remains Commit authority."""
    if not result_id:
        return {"available": False, "reason": "no_result"}
    stored = store.get_result(result_id)
    if stored is None or stored.verdict != "FAIL":
        return {"available": False, "reason": "no_failed_result"}
    try:
        revision = revision_for_result(store, cas, result_id)
        claims = store.list_claims(result_id)
        assessments = store.list_criterion_assessments(revision.mission_id, result_id=result_id)
        if any(
            len(items) > _MAX_ITEMS
            for items in (revision.criteria, claims, stored.artifacts, assessments)
        ):
            return {"available": False, "reason": "fragment_catalog_limit"}
        materials = []
        for artifact_id in stored.artifacts:
            artifact = store.get_artifact(artifact_id)
            if artifact is None or artifact.attempt_id != stored.envelope.attempt_id:
                raise ContractError("fragment result artifact identity unavailable")
            if artifact.size_bytes > _MAX_ARTIFACT_BYTES:
                return {"available": False, "reason": "fragment_artifact_size_limit"}
            data = read_verified(artifact)
            if not data or len(data) != artifact.size_bytes:
                raise ContractError("fragment result artifact byte bounds unavailable")
            materials.append(
                {
                    "kind": "artifact",
                    "artifact_id": artifact.id,
                    "content_hash": artifact.content_hash,
                    "size_bytes": len(data),
                    "path": artifact.path,
                }
            )
        for raw in assessments:
            assessment = CriterionAssessmentV1.from_json(raw)
            if len(assessment.evidence_refs) > _MAX_ITEMS:
                return {"available": False, "reason": "fragment_catalog_limit"}
            for index, evidence in enumerate(assessment.evidence_refs):
                if evidence.get("status") == "resolved":
                    materials.append(
                        {
                            "kind": "citation",
                            "receipt_id": assessment.receipt_id,
                            "citation_index": index,
                            "target": evidence.get("target"),
                            "source_version": evidence.get("source_version"),
                        }
                    )
        if not materials or len(materials) > _MAX_ITEMS:
            return {"available": False, "reason": "no_bounded_material"}
        catalog = {
            "available": True,
            "schema_version": 1,
            "origin": {
                "mission_id": revision.mission_id,
                "task_id": revision.task_id,
                "attempt_id": stored.envelope.attempt_id,
                "result_id": result_id,
                "task_revision_id": revision.revision_id,
            },
            "criteria": [dict(item) for item in revision.criteria],
            "claims": [{"claim_id": claim.id, "claim_revision": claim.version} for claim in claims],
            "materials": materials,
        }
        if len(canonical_json(cast(JsonValue, catalog)).encode("utf-8")) > 16_384:
            return {"available": False, "reason": "fragment_catalog_bytes_limit"}
        return catalog
    except (ContractError, ArtifactStoreError, OSError, ValueError):
        return {"available": False, "reason": "origin_unavailable"}


def validate_manager_fragment_choice(proposal: Any, catalog: Any) -> None:
    """A model can narrow only the exact frozen catalog it was shown."""
    if not isinstance(catalog, Mapping) or catalog.get("available") is not True:
        raise ContractError("fragment origin was unavailable to this Manager")
    if dict(proposal.origin) != catalog["origin"]:
        raise ContractError("fragment proposal origin differs from Manager Result")
    if set(proposal.criterion_ids) - {item["id"] for item in catalog["criteria"]}:
        raise ContractError("fragment criterion was not in the Manager catalog")
    if any(dict(ref) not in catalog["claims"] for ref in proposal.claim_refs):
        raise ContractError("fragment claim was not in the Manager catalog")
    for ref in proposal.material_refs:
        if ref["kind"] == "artifact":
            if not any(
                item["kind"] == "artifact"
                and item["artifact_id"] == ref["artifact_id"]
                and item["content_hash"] == ref["content_hash"]
                and ref["byte_end_exclusive"] <= item["size_bytes"]
                for item in catalog["materials"]
            ):
                raise ContractError("fragment artifact range was not in the Manager catalog")
        elif not any(
            item["kind"] == "citation"
            and item["receipt_id"] == ref["receipt_id"]
            and item["citation_index"] == ref["citation_index"]
            for item in catalog["materials"]
        ):
            raise ContractError("fragment citation was not in the Manager catalog")


def manager_validated_fragment(store: Any, commit: Any, task_id: str) -> dict[str, Any]:
    """Bounded facts for a second Manager turn after independent acceptance."""
    task = store.get_task(task_id)
    marker = None if task is None else task.context.get("fragment_validation")
    if not isinstance(marker, Mapping) or task.status is not TaskStatus.COMPLETED:
        return {"available": False, "reason": "no_accepted_validation"}
    receipt = store.get_receipt(marker.get("projection_receipt_id", ""))
    stored = store.get_result(task.accepted_result_id or "")
    if (
        receipt is None
        or receipt.get("fragment_id") != marker.get("fragment_id")
        or receipt.get("validation_task_id") != task.id
        or stored is None
        or stored.verification_state != "DONE"
        or stored.verdict != "PASS"
    ):
        return {"available": False, "reason": "validation_not_accepted"}
    try:
        commit.fragment_validation_binding(task.id)
        commit.fragment_validation_inputs(task.id)
        mapped = set(receipt["output_path_mapping"].values())
        materials = []
        for artifact_id in stored.artifacts:
            artifact = store.get_artifact(artifact_id)
            if artifact is None or artifact.attempt_id != stored.envelope.attempt_id:
                raise ContractError("accepted fragment artifact identity unavailable")
            if artifact.path not in mapped:
                continue
            if artifact.size_bytes > _MAX_ARTIFACT_BYTES:
                return {"available": False, "reason": "validated_fragment_artifact_size_limit"}
            read_verified(artifact)
            materials.append(
                {
                    "artifact_id": artifact.id,
                    "path": artifact.path,
                    "content_hash": artifact.content_hash,
                }
            )
        tasks = [item for item in store.list_tasks(task.mission_id) if item.kind == "work"]
        if not materials or len(materials) > _MAX_ITEMS or len(tasks) > _MAX_ITEMS:
            return {"available": False, "reason": "validated_fragment_limit"}
        summary = {
            "available": True,
            "schema_version": 1,
            "fragment_id": receipt["fragment_id"],
            "projection_receipt_id": receipt["projection_receipt_id"],
            "validation_task_id": task.id,
            "validation_result_id": stored.envelope.id,
            "origin_task_id": receipt["origin"]["task_id"],
            "criterion_mapping": receipt["criterion_mapping"],
            "material_refs": materials,
            "tasks": [
                {
                    "task_id": item.id,
                    "goal": item.goal,
                    "status": str(item.status),
                    "dependencies": list(item.dependency_ids),
                    "attempts": item.attempt_count,
                }
                for item in tasks
            ],
        }
        if len(canonical_json(summary).encode("utf-8")) > 16_384:
            return {"available": False, "reason": "validated_fragment_bytes_limit"}
        return summary
    except (ContractError, ArtifactStoreError, OSError, ValueError, KeyError, TypeError):
        return {"available": False, "reason": "validated_fragment_unavailable"}

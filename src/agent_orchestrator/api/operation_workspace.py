# SPDX-License-Identifier: Apache-2.0
"""Read-only MissionControl projection for explicit completion/operation commands.

This never grants authority or picks a completion mapping. Immutable references
are returned for the existing command APIs to revalidate in their transactions.
"""
from __future__ import annotations

from typing import Any

from ..contracts.models import ContractError
from ..contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from ..observability.secrets import find_secrets
from ..orchestrator.action_commits import validate_candidate
from ..orchestrator.operation_materialization_inputs import _candidate
from ..runtime.connectors_publish import PUBLISH_NAME, FilePublishConnector
from ..runtime.operation_profiles import BuiltinOperationProfiles
from ..storage.htn_store import HtnStore
from ..storage.obligation_store import ObligationStore
from ..storage.operation_completion_store import OperationCompletionStore
from ..storage.operation_intent_store import OperationIntentStore


def operation_workspace(loop: Any, mission: Any, *, principal: Any) -> dict[str, Any] | None:
    store = loop.store
    protocol = store.connection.execute(
        "SELECT protocol_version FROM mission_planning_protocols WHERE mission_id=?", (mission.id,)
    ).fetchone()
    if protocol is None or protocol[0] != "planning-decision-v1":
        return None
    htn = HtnStore(store)
    requirements = htn.latest_requirements_revision(mission.id)
    if requirements is None:
        return {"state": "WAITING_REQUIREMENTS", "mission_id": mission.id}
    reference = TypedRef(TypedRefKind.REQUIREMENTS, str(requirements.revision_id),
                         requirements.revision, requirements.content_hash())
    row = OperationCompletionStore(store).get_spec_exact(
        mission.id, requirements.revision, requirements.content_hash())
    spec = None if row is None else row["document"]
    # The desktop's registered file publisher is the current product capability.
    # A custom deployment must publish its own concrete choices; never fabricate
    # milestone/policy identities from connector names or model text.
    choices = []
    if type(loop.connectors.get(PUBLISH_NAME)) is FilePublishConnector:
        registry = BuiltinOperationProfiles(loop.connectors)
        labels = {"FILE_PUBLISHED": "文件已发布，并已读取核对内容",
                  "CONTENT_HASH_VERIFIED": "已发布文件的内容哈希与批准产物一致"}
        choices = [{"id": milestone, "label": labels[milestone],
                    "milestone_policy_ref": registry.milestone_policy_ref.to_json(),
                    "evidence_policy_ref": registry.evidence_policy_ref.to_json()}
                   for milestone in registry.supported_milestones]
    candidates: list[dict[str, Any]] = []
    artifacts = loop.assembled.workspaces.artifact_store
    for acceptance in htn.list_acceptances(mission.id):
        if str(acceptance.validity) != "CURRENT":
            continue
        for ref in acceptance.artifact_refs:
            artifact = store.get_artifact(ref.id)
            if (artifact is None or artifact.mission_id != mission.id
                    or not artifact.path.startswith("actions/") or not artifact.path.endswith(".json")
                    or artifact.verification_status != "VERIFIED" or artifact.size_bytes > 262144
                    or artifact.content_hash != ref.content_hash):
                continue
            try:
                raw = artifacts.read(artifact.content_hash)
                candidate = validate_candidate(_candidate(raw))
                if find_secrets(raw.decode("utf-8")):
                    continue
            except (ContractError, OSError, ValueError):
                continue
            candidates.append({"artifact_path": artifact.path, "candidate": candidate,
                "candidate_artifact_ref": TypedRef(TypedRefKind.ARTIFACT, artifact.id,
                    artifact.version, artifact.content_hash).to_json(),
                "prepared_acceptance_refs": [TypedRef(TypedRefKind.ACCEPTANCE,
                    str(acceptance.acceptance_id), 1, content_hash_of(acceptance.to_json())).to_json()]})
    intent_rows = OperationIntentStore(store).for_mission(mission.id)
    superseded = {item["supersedes_intent_id"] for item in intent_rows if item["supersedes_intent_id"]}
    intents = []
    for item in intent_rows:
        status = loop.commit.operation_intent_status(item["intent_id"],
            tenant_id=mission.tenant_id, principal=principal)
        intents.append({"intent_id": item["intent_id"], "state": status["state"],
            "effect_key": status["completion_slot"]["effect_key"],
            "spec_hash": status["completion_slot"]["spec_hash"],
            "current": item["intent_id"] not in superseded,
            "can_replace": item["intent_id"] not in superseded and status["materialization"] is None,
            "completion": status["completion"]})

    return {"mission_id": mission.id,
            "state": "APPROVED" if spec is not None else "CONFIRMATION_REQUIRED",
            "editable": str(mission.status) not in {"COMPLETED", "FAILED", "CANCELLED"},
            "requirements_ref": reference.to_json(),
            "criteria": [{"id": c.criterion_id, "statement": c.statement,
                          "required": c.criterion_id in requirements.required_criterion_ids()}
                         for c in requirements.criteria],
            "obligations": [{"id": str(o.obligation_id), "label": o.goal_signature_id,
                             "criterion_ids": list(o.requirement_refs)}
                            for o in ObligationStore(store).list_obligations(mission.id)
                            if str(o.lifecycle) == "UNSATISFIED"],
            "milestones": choices,
            "spec": None if spec is None else spec.to_json(),
            "spec_hash": None if spec is None else spec.content_hash(),
            "candidates": candidates, "intents": intents}

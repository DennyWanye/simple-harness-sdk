# SPDX-License-Identifier: Apache-2.0
"""Resume an original durable H1 reply when its exact convergence job is READY."""
from __future__ import annotations

import hashlib
from typing import Any

from ..artifacts.store import read_nofollow
from ..contracts.state_machines import TERMINAL_MISSION
from ..graph.execution_contracts import PreviewBindingV1
from ..storage.planning_decision_store import PlanningDecisionStore
from ..storage.store import StoreConflict, StoreError
from ..storage.taskgraph_convergence import ConvergenceJob


async def resume_converged_plan(orchestrator: Any, job: ConvergenceJob) -> None:
    store = orchestrator.store
    if store.connection.in_transaction:
        raise StoreError("TASKGRAPH_RESUME_OUTSIDE_TRANSACTION_REQUIRED")
    if job.state != "READY":
        raise StoreConflict("TASKGRAPH_RESUME_NOT_READY")
    with store.read_view():
        decisions = PlanningDecisionStore(store)
        request = decisions.get_planning_request(job.request_id)
        decision = decisions.get_planning_decision(job.decision_id)
        if (request is None or request.mission_id != job.mission_id or request.base_plan_revision != job.source_revision
                or decision is None or decision["request_id"] != job.request_id):
            raise StoreError("TASKGRAPH_RESUME_ORIGINAL_DECISION_MISSING")
        if decision["status"] != "COMPILED":
            # A refused/stale decision does not release fences or silently ask the
            # model again. The persistent notification reports the refusal for repair.
            raise StoreConflict("TASKGRAPH_RESUME_ORIGINAL_DECISION_NOT_COMPILED")
        preview = PreviewBindingV1.from_json(decision.get("detail", {}).get("taskgraph_preview"))
        if (preview.required_convergence_ids != (job.job_id,) or preview.decision_id != job.decision_id
                or preview.candidate_hash != job.candidate_hash or preview.request_id != request.request_id
                or preview.decision_hash != decision["canonical_hash"]):
            raise StoreConflict("TASKGRAPH_RESUME_PREVIEW_IDENTITY_CHANGED")
        intent = store.get_intent(request.intent_id)
        mission = store.get_mission(job.mission_id)
        hierarchy = orchestrator._dispatch_for(job.mission_id)
        if (intent is None or intent.mission_id != job.mission_id or mission is None or hierarchy is None
                or job.command_id != f"plan:{intent.intent_id}"):
            raise StoreError("TASKGRAPH_RESUME_RUNTIME_IDENTITY_MISSING")
        if mission.status in TERMINAL_MISSION:
            # A saved reply retains accounting/convergence responsibility, never
            # permission to re-enter planning after the user stopped the Mission.
            raise StoreConflict("TASKGRAPH_RESUME_MISSION_TERMINAL")
        artifact_ref = decision["raw_artifact_ref"]
        raw_hash = decision["raw_output_hash"]
        if not isinstance(artifact_ref, str) or artifact_ref != raw_hash:
            raise StoreError("TASKGRAPH_RESUME_REPLY_IDENTITY_INVALID")
    # Content-addressed original reply; never reconstruct model text from a parsed
    # proposal or manufacture an AgentTurn result. The original collector ignores
    # its result parameter and rechecks all durable producers before committing.
    raw = read_nofollow(orchestrator.assembled.workspaces.artifact_store.path_for(artifact_ref))
    if hashlib.sha256(raw).hexdigest() != raw_hash:
        raise StoreError("TASKGRAPH_RESUME_REPLY_HASH_MISMATCH")
    await orchestrator._collect_plan_decision(intent, None, mission, raw.decode("utf-8"), hierarchy)
    with store.read_view():
        decision = decisions.get_planning_decision(job.decision_id)
        if decision is None or decision["status"] != "COMMITTED":
            raise StoreConflict("TASKGRAPH_RESUME_PLAN_NOT_COMMITTED")

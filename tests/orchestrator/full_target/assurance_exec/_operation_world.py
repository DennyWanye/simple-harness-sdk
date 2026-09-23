# SPDX-License-Identifier: Apache-2.0
"""Shared real T0/T1/T3 operation world for the E group and the OCC contract cases.

Built from the operation_completion fixtures (real Store / CommitService / Spec
approval / plan admission / scoped content acceptance / proposal review /
materialization / ``ActionExecutor`` with the real ``FilePublishConnector`` /
outcome review / outcome acceptance). Parameterised over the required
milestone and the approved effect slots so one helper serves single- and
two-effect Specs. Review turns are scripted service intents (no model).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_HERE = Path(__file__).resolve().parent
_FULL_TARGET = _HERE.parent
_OPERATION = _FULL_TARGET / "operation_completion"
for path in (_HERE, _FULL_TARGET, _OPERATION):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_completion_plan_commit import _admitted_single_root  # noqa: E402
from test_completion_spec_approval import (  # noqa: E402
    HASH_B,
    HASH_C,
    ROOT_DUTY,
    _api,
    _bind_new_protocol,
    _criterion,
    _requirements_ref,
)
from test_operation_t0_t3_runtime import _passing_critic, _submit_review_intent  # noqa: E402
from test_plan_commits import _world  # noqa: E402

from agent_orchestrator.api.approvals import ApprovalApi
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope
from agent_orchestrator.contracts.operation_intents import SubmitOperationIntentV2
from agent_orchestrator.contracts.resolution import AllExpr, CriterionExpr, RequirementsRevision
from agent_orchestrator.contracts.semantic_base import (
    Provenance,
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.operation_materialization import (
    OperationMaterializationRuntime,
)
from agent_orchestrator.orchestrator.operation_outcomes import (
    accept_operation_outcome,
    persist_operation_outcome_review,
    prepare_operation_outcome_review,
    record_operation_outcome_review,
)
from agent_orchestrator.orchestrator.operation_proposal_review import (
    ActionProposalReviewCoordinator,
)
from agent_orchestrator.orchestrator.operation_runtime import dispatch_materialized_operations
from agent_orchestrator.runtime.actions import ActionExecutor
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from agent_orchestrator.runtime.operation_profiles import BuiltinOperationProfiles
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.htn_store import HtnStore

DEFAULT_EFFECTS = (("deliver-report", "criterion-delivered", "reports/final.json"),)


def proposal(requirements: RequirementsRevision, *, milestone: str, effects, profiles=None):
    """The user's completion Spec proposal over the given effect slots."""
    return {
        "schema_version": 1,
        "mission_id": requirements.mission_id,
        "requirements_ref": {"id": str(requirements.revision_id), "revision": int(requirements.revision),
                             "content_hash": requirements.content_hash()},
        "mode": "REQUIRED_EFFECTS",
        "content_criterion_ids": ["criterion-report"],
        "effects": [
            {"effect_key": key, "obligation_id": ROOT_DUTY, "criterion_ids": [criterion],
             "required_milestone": milestone,
             "milestone_policy_ref": (profiles.milestone_policy_ref.to_json() if profiles is not None
                                      else {"id": "approved-milestone-policy", "revision": 1, "content_hash": HASH_B}),
             "evidence_policy_ref": (profiles.evidence_policy_ref.to_json() if profiles is not None
                                     else {"id": "approved-evidence-policy", "revision": 1, "content_hash": HASH_C}),
             "source_slot_key": "approved-slot:" + key}
            for key, criterion, _target in effects],
    }


def completion_command(requirements, *, command_id="confirm-completion-1", milestone, effects, profiles=None):
    return {"mission_id": requirements.mission_id, "command_id": command_id,
            "expected_requirements_ref": _requirements_ref(requirements).to_json(),
            "proposal": proposal(requirements, milestone=milestone, effects=effects, profiles=profiles)}


def insert_requirements(world, effects, *, revision=1):
    criteria = [_criterion("criterion-report")] + [_criterion(criterion) for _key, criterion, _t in effects]
    requirements = RequirementsRevision(
        revision_id=f"completion-requirements-{revision}", mission_id=world.mission.id, revision=revision,
        criteria=tuple(criteria),
        success_expression=AllExpr(tuple(CriterionExpr(c.criterion_id) for c in criteria)),
        authority_subject="authenticated-user-confirmation")
    HtnStore(world.store).insert_requirements_revision(requirements)
    return requirements


class OperationWorld:
    """One Mission with an approved Spec, a frozen single-root MIXED scope and the
    real content acceptance; effect slots are driven one at a time."""

    def __init__(self, tmp_path: Path, *, milestone="CONTENT_HASH_VERIFIED", effects=DEFAULT_EFFECTS,
                 approve_spec=True, key="assurance-exec-operation"):
        self.root = Path(tmp_path)
        self.effects = tuple(effects)
        (self.root / "published").mkdir(parents=True)
        self.publish = FilePublishConnector(self.root / "published", self.root / "publish-ledger")
        self.connectors = {"file_publish": self.publish}
        self.deployment = DeploymentPolicy(enabled_connectors=("file_publish",))
        self.profiles = BuiltinOperationProfiles(self.connectors)
        world = _world(tmp_path, key=key)
        _bind_new_protocol(world)
        self.world = world
        self.store, self.commit = world.store, world.service
        self.requirements = insert_requirements(world, self.effects)
        original = world.mission
        world.mission = dataclasses.replace(
            original,
            success_criteria=(*original.success_criteria,
                              *(f"action:file_publish.publish:{target}" for _k, _c, target in self.effects)),
            version=original.version + 1)
        world.store.update_mission(world.mission, expected_version=original.version)
        self.mission_id = world.mission.id
        world.service.begin_planning(world.mission.id)
        self.milestone = milestone
        self.approved = None
        if approve_spec:
            self.approved = _api(world).approve(completion_command(
                self.requirements, milestone=milestone, effects=self.effects, profiles=self.profiles))
        self.plan, _ = _admitted_single_root(world, self.requirements, outputs=(("report", "report.schema"),))
        self.occurrence = str(self.plan.network.root_occurrence_ids[0])
        task_id = world.store.connection.execute(
            "SELECT task_id FROM plan_memberships WHERE mission_id=? AND revision=1 AND occurrence_id=?",
            (world.mission.id, self.occurrence)).fetchone()["task_id"]
        self.task = world.store.get_task(task_id)
        assert self.task is not None
        self.captured: dict[str, Any] = {}
        self.runtime = OperationMaterializationRuntime(
            self.connectors, self.deployment, self.profiles, self.profiles.policy_for, self._prepare_review, object())
        world.service.bind_operation_materialization_runtime(self.runtime)
        self.executor = ActionExecutor(world.service, self.connectors, self.deployment, owner="operation-runtime",
                                       source_storage_roots=(self.root / "artifacts",))
        self.loop = SimpleNamespace(store=world.store, actions=self.executor)
        self.acceptance = None
        self.artifacts: dict[str, Any] = {}
        self.bodies: dict[str, bytes] = {}
        self.intents: dict[str, str] = {}
        self.actions: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, str] = {}

    # ------------------------------------------------------------------ T0 side
    def _prepare_review(self, sources, payloads, package_id):
        coordinator = ActionProposalReviewCoordinator(
            self.store, self.connectors, self.deployment, self.profiles, self.profiles.policy_for(sources))
        self.captured["coordinator"] = coordinator
        self.captured["draft"] = coordinator.prepare_review(sources, payloads, package_id=package_id)
        return self.captured["draft"]

    def produce_and_accept(self, *, extra_files=()):
        """The original Worker turn: one candidate per effect target, verified and
        accepted through the production content acceptance (preparation)."""
        world, task = self.world, self.task
        attempt, worker = world.service.create_attempt(
            task.id, role="worker", model="fixture-worker", prompt_version="fixture-worker-v1",
            context_version="operation-runtime-v1", reservation=Reservation(1_000, 0),
            intent_config={"message": "prepare the approved publish action"}, input_hash="a" * 64)
        world.service.claim_intent(worker.intent_id, owner="fixture", lease_seconds=60)
        world.service.record_agent_created(worker.intent_id, agent_id="fixture-worker", expected_turn_id="worker-turn")
        world.service.record_submitted(worker.intent_id, receipt={"turn_id": "worker-turn", "seq": 1})
        self.attempt, self.worker_intent = attempt, worker
        cas = world.service._source_artifact_store
        assert isinstance(cas, ArtifactStore)
        artifacts, claims = [], []
        for index, (key, _criterion, target) in enumerate(self.effects):
            candidate = {"connector": "file_publish", "operation": "publish", "target": target,
                         "params": {"artifact_path": f"report-{index}.json"}, "reason": "publish the reviewed result"}
            body = (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode()
            digest = cas.put_bytes(body)
            artifact = Artifact(
                id=f"artifact-operation-candidate-{index}", mission_id=world.mission.id, task_id=task.id,
                attempt_id=attempt.id, type="file", path=f"report-{index}.json", version=1,
                content_hash=hashlib.sha256(body).hexdigest(), size_bytes=len(body),
                produced_by="fixture-worker", storage_uri=str(cas.path_for(digest)))
            artifacts.append(artifact)
            self.artifacts[key], self.bodies[key] = artifact, body
            if index == 0:
                claims.append(PortClaim(port_key="report", path=artifact.path))
        for index, (name, body) in enumerate(extra_files):
            digest = cas.put_bytes(body)
            artifacts.append(Artifact(
                id=f"artifact-extra-{index}", mission_id=world.mission.id, task_id=task.id, attempt_id=attempt.id,
                type="file", path=name, version=1, content_hash=hashlib.sha256(body).hexdigest(),
                size_bytes=len(body), produced_by="fixture-worker", storage_uri=str(cas.path_for(digest))))
        result = ResultEnvelope(
            id="result-operation-candidate", mission_id=world.mission.id, task_id=task.id, attempt_id=attempt.id,
            outcome="candidate", summary="Prepared the approved publish action(s).",
            claims=(ClaimProposal(content="publish candidate prepared", confidence=0.9),), evidence=(),
            artifacts=tuple(a.path for a in artifacts), proposed_tasks=(), used_knowledge=(), risks=(), cost={})
        stored = world.service.record_result(attempt.id, envelope=result, turn_id="worker-turn",
                                             artifacts=tuple(artifacts), usage_refs=(), port_claims=tuple(claims))
        self.stored = stored
        world.service.start_verification(stored.envelope.id)
        for layer in ("schema_check", "rule_check", "critic_review"):
            world.service.record_verification_layer(stored.envelope.id, layer=layer, status="PASS",
                                                    detail={"producer": "CommitService"})
        world.service.accept_result(stored.envelope.id, verifier_results=())
        self.acceptance = HtnStore(world.store).list_acceptances(world.mission.id)[0]
        return stored

    def submit(self, effect_key, *, idempotency_key=None, spec_hash=None):
        """T0: the user's operation intent for one approved effect slot."""
        world = self.world
        artifact, acceptance = self.artifacts[effect_key], self.acceptance
        command = SubmitOperationIntentV2.from_json({
            "schema_version": 2, "mission_id": world.mission.id,
            "idempotency_key": idempotency_key or ("publish:" + effect_key), "intent_source": {"kind": "USER_COMMAND"},
            "candidate_artifact_ref": TypedRef(TypedRefKind.ARTIFACT, artifact.id, artifact.version,
                                               artifact.content_hash, Provenance.TOOL).to_json(),
            "prepared_acceptance_refs": [TypedRef(TypedRefKind.ACCEPTANCE, str(acceptance.acceptance_id), 1,
                                                  content_hash_of(acceptance.to_json()), Provenance.TOOL).to_json()],
            "supersedes_intent_id": None,
            "completion_slot": {"spec_hash": spec_hash or self.approved.spec_hash, "effect_key": effect_key}})
        submitted = world.service.submit_operation_intent(
            command, tenant_id=world.mission.tenant_id, principal=Principal("operation-owner"))
        self.intents[effect_key] = submitted["intent_id"]
        return submitted["intent_id"]

    def materialize(self, effect_key, *, approve=True):
        """T1: official ACTION_PROPOSAL review, materialisation, human approval."""
        world, intent_id = self.world, self.intents[effect_key]
        draft = self.captured["draft"]
        dispatch, reviewer, turn = _submit_review_intent(
            world, subject="operation-review:" + intent_id, role="operation_proposal_reviewer",
            package_id=str(draft.package.package_id), extra={"operation_intent_id": intent_id})
        with world.store.transaction():
            review = self.captured["coordinator"].record_critic_verdict(
                draft, record_id="operation-review-record:" + intent_id, dispatch_intent_id=dispatch.intent_id,
                reviewer_agent_id=reviewer, reviewer_turn_id=turn,
                raw_critic_text=_passing_critic(c.criterion_id for c in draft.package.criteria))
        materialized = world.service.materialize_reviewed_operation(
            intent_id=intent_id, command_id="materialize:" + intent_id,
            official_review_ref=TypedRef(TypedRefKind.REVIEW, str(review.record.record_id), 1,
                                         content_hash_of(review.record.to_json())),
            service_authority=self.runtime.service_authority)
        action = world.store.get_action(materialized["action_key"])
        assert action is not None
        if approve:
            ApprovalApi(world.service, Principal("operation-approver"), deployment=self.deployment).approve(
                action["approval_request_id"], nonce="approve:" + effect_key)
            action = world.store.get_action(materialized["action_key"])
        self.actions[effect_key] = action
        return action

    def approve_action(self, effect_key):
        action = self.actions[effect_key]
        ApprovalApi(self.world.service, Principal("operation-approver"), deployment=self.deployment).approve(
            action["approval_request_id"], nonce="approve:" + effect_key)
        self.actions[effect_key] = self.world.store.get_action(action["action_key"])
        return self.actions[effect_key]

    def dispatch(self) -> bool:
        """The production dispatcher over the real executor and connector."""
        return asyncio.run(dispatch_materialized_operations(self.loop, self.mission_id))

    def execute(self, effect_key):
        assert self.dispatch()
        self.actions[effect_key] = self.world.store.get_action(self.actions[effect_key]["action_key"])
        return self.actions[effect_key]

    # ------------------------------------------------------------------ T3 side
    def prepare_outcome(self, effect_key):
        prepared = prepare_operation_outcome_review(
            self.store, intent_id=self.intents[effect_key], connectors=self.connectors, profiles=self.profiles)
        with self.store.transaction():
            persist_operation_outcome_review(self.commit, prepared, runtime=self.runtime)
        self.bindings[effect_key] = prepared.binding_id
        return prepared

    def review_outcome(self, prepared, *, passing=True):
        dispatch, _, turn = _submit_review_intent(
            self.world, subject="operation-outcome-review:" + prepared.binding_id, role="operation_outcome_reviewer",
            package_id=str(prepared.package.package_id), extra={"outcome_binding_id": prepared.binding_id})
        text = _passing_critic(c.criterion_id for c in prepared.package.criteria)
        if not passing:
            text = text.replace('"met": true', '"met": false')
        with self.store.transaction():
            return record_operation_outcome_review(self.store, mission_id=self.mission_id,
                                                   binding_id=prepared.binding_id, dispatch=dispatch,
                                                   turn_id=turn, text=text)

    def accept_outcome(self, binding_id):
        return accept_operation_outcome(self.commit, mission_id=self.mission_id, binding_id=binding_id,
                                        service_authority=self.runtime.service_authority)

    def complete_effect(self, effect_key):
        """T0 → T1 → execution → T3 for one slot, exactly as the runtime test does it."""
        self.submit(effect_key)
        self.materialize(effect_key)
        self.execute(effect_key)
        prepared = self.prepare_outcome(effect_key)
        record = self.review_outcome(prepared)
        receipt = self.accept_outcome(prepared.binding_id)
        return prepared, record, receipt

    # ------------------------------------------------------------------ reads
    def count(self, sql, *params):
        return self.store.connection.execute(sql, params).fetchone()[0]

    def published_files(self):
        return sorted(p.relative_to(self.publish.root).as_posix() for p in self.publish.root.rglob("*") if p.is_file())

    def events(self):
        return [e.type for e in self.store.list_events(self.mission_id)]

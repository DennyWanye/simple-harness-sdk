# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-H request binding: the hot path persists one exact new-protocol snapshot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.context.context_builder import TaskPackage
from agent_orchestrator.contracts import Budget
from agent_orchestrator.contracts.planning_decisions import PLANNING_DECISION_V1
from agent_orchestrator.orchestrator.commit_service import (
    PLANNING_DECISION_V1 as MISSION_PLANNING_DECISION_V1,
)
from agent_orchestrator.orchestrator.commit_service import (
    CommitService,
    MissionSpec,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.hierarchical_dispatch import append_hierarchical_event
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import DispatchIntent, Store


def _intent(intent_id: str) -> DispatchIntent:
    return DispatchIntent(
        intent_id=intent_id,
        kind="plan",
        subject_id="subject",
        mission_id="mission",
        state="PENDING",
        version=1,
        creation_key="subject",
        input_id="attempt-input",
        input_hash="a" * 64,
        config={},
        expected_turn_id=None,
        agent_id=None,
        receipt=None,
        lease_owner=None,
        lease_expires_at=None,
        replays=0,
        created_at=123.0,
    )


class _Mode:
    def scope_epochs(self, mission_id: str) -> dict[str, int]:
        assert mission_id
        return {"mission": 7}

    def semantics(self) -> object:
        return SimpleNamespace(latest_requirements_revision=lambda mission_id: None)


def _package() -> TaskPackage:
    return TaskPackage(
        text="sealed",
        context_version="ctx-test",
        package={
            "context_builder_version": "context-builder-v4",
            "mode": "hierarchical",
            "planning_protocol": {
                "protocol": PLANNING_DECISION_V1,
                "enabled_decision_types": ["REFINE"],
            },
            "planning_subjects": [
                {"subject_key": "subject-a", "occurrence_id": "o1"}
            ],
            "visible_refs": [
                {
                    "kind": "task",
                    "id": "task-a",
                    "semantic_revision": 1,
                    "content_hash": "b" * 64,
                }
            ],
            "package_version": 4,
            "plan": {"plan_revision": 3},
        },
    )


def _mission(store: Store, *, protocol: str):
    mission, _ = CommitService(store).create_mission(
        MissionSpec(
            goal="goal",
            success_criteria=("done",),
            tenant_id="tenant",
            idempotency_key=protocol,
            budget=Budget(max_tokens=1000, max_attempts=1),
            planning_protocol_version=protocol,
        )
    )
    return mission


def test_new_protocol_request_binding_uses_the_sealed_package_and_prompt(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    intent = _intent("plan-request-1")
    object.__setattr__(intent, "mission_id", mission.id)
    orchestrator = object.__new__(Orchestrator)
    orchestrator._store = store
    # This protocol seam bypasses full runtime assembly but still journals real
    # raw bytes in the same content-addressed store used by production.
    orchestrator._assembled = SimpleNamespace(
        workspaces=SimpleNamespace(artifact_store=ArtifactStore(store.path.parent / "artifacts"))
    )

    orchestrator._bind_hierarchical_planning_request(
        intent=intent,
        mission=mission,
        new_mode=_Mode(),
        package=_package(),
        template=SimpleNamespace(prompt_version="planner-hierarchical-v8", instructions="prompt"),
    )

    binding = PlanningDecisionStore(store).get_planning_request(intent.intent_id)
    assert binding is not None
    assert binding.mission_id == mission.id
    assert binding.protocol_version == PLANNING_DECISION_V1
    assert binding.base_plan_revision == 3
    assert binding.requirements_revision == 0
    assert binding.created_at == intent.created_at
    assert binding.intent_id == intent.intent_id


def test_legacy_request_does_not_gain_a_decision_binding(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol="legacy-plan-proposal-v1")
    intent = _intent("legacy-request-1")
    object.__setattr__(intent, "mission_id", mission.id)
    orchestrator = object.__new__(Orchestrator)
    orchestrator._store = store
    # This protocol seam bypasses full runtime assembly but still journals real
    # raw bytes in the same content-addressed store used by production.
    orchestrator._assembled = SimpleNamespace(
        workspaces=SimpleNamespace(artifact_store=ArtifactStore(store.path.parent / "artifacts"))
    )

    orchestrator._bind_hierarchical_planning_request(
        intent=intent,
        mission=mission,
        new_mode=_Mode(),
        package=_package(),
        template=SimpleNamespace(prompt_version="planner-hierarchical-v8", instructions="prompt"),
    )

    assert PlanningDecisionStore(store).get_planning_request(intent.intent_id) is None


def test_new_protocol_unreadable_reply_writes_new_and_legacy_events(tmp_path) -> None:
    """H1-H: a decode refusal keeps the legacy rejection ledger and adds its new event."""

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    intent = _intent("plan-request-unreadable")
    object.__setattr__(intent, "mission_id", mission.id)
    orchestrator = object.__new__(Orchestrator)
    orchestrator._store = store
    # This protocol seam bypasses full runtime assembly but still journals real
    # raw bytes in the same content-addressed store used by production.
    orchestrator._assembled = SimpleNamespace(
        workspaces=SimpleNamespace(artifact_store=ArtifactStore(store.path.parent / "artifacts"))
    )
    orchestrator._owner = "test-owner"
    orchestrator._settle_intent = lambda intent, state: None
    orchestrator._settle_service_if_known = lambda subject_id, mission_id: None

    async def legacy_rejection(intent, *, reason, detail):
        append_hierarchical_event(
            store,
            "PlanningRejected",
            mission.id,
            key=intent.intent_id,
            payload={"ordinal": 1, "reason": reason, "detail": dict(detail)},
        )

    orchestrator._planning_rejected = legacy_rejection
    orchestrator._bind_hierarchical_planning_request(
        intent=intent,
        mission=mission,
        new_mode=_Mode(),
        package=_package(),
        template=SimpleNamespace(prompt_version="planner-hierarchical-v8", instructions="prompt"),
    )

    asyncio.run(
        orchestrator._collect_plan_decision(
            intent,
            SimpleNamespace(),
            mission,
            "<planning_decision>{not-json}</planning_decision>",
            _Mode(),
        )
    )

    evaluated = [
        event
        for event in store.list_events(mission.id)
        if event.type == "PlanningDecisionEvaluated"
    ]
    rejected = [
        event for event in store.list_events(mission.id) if event.type == "PlanningRejected"
    ]
    assert len(evaluated) == 1
    assert evaluated[0].payload["status"] == "UNREADABLE"
    assert {
        "decision_id",
        "request_id",
        "attempt_ordinal",
        "decision_type",
        "status",
        "rejection_codes",
        "canonical_hash",
    } <= set(evaluated[0].payload)
    assert evaluated[0].payload["attempt_ordinal"] == 0
    assert evaluated[0].payload["decision_type"] is None
    assert evaluated[0].payload["canonical_hash"] is None
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "proposal_unreadable"

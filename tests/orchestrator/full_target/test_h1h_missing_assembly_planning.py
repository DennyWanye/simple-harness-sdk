"""Native-discovered isolation: absent HTN assembly must never send legacy planning."""
from __future__ import annotations

import asyncio

import pytest

from agent_orchestrator.contracts import ContractError, MissionStatus
from agent_orchestrator.orchestrator.commit_service import MissionSpec, Reservation, mission_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


@pytest.mark.parametrize("pending_intent", (False, True))
def test_missing_assembly_blocks_initial_retry_and_recovered_dispatch(tmp_path, pending_intent):
    async def case():
        config = OrchestratorConfig(evidence_root=tmp_path)
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(config, provider) as loop:
            mission, _ = loop.commit.create_mission(MissionSpec(
                tenant_id="native-ui", goal="Confirm completion requirements",
                success_criteria=("clear requirements",), idempotency_key="missing-assembly",
                orchestration_semantics_version="hierarchical",
                planning_protocol_version="planning-decision-v1",
            ))
            if pending_intent:
                # A pending service created by an earlier process/version is also
                # blocked before claim or agent creation. No package is trusted.
                intent = loop.commit.create_service_intent(
                    kind="plan", subject_id="old-pending-plan", mission_id=mission.id,
                    account_id=mission_account(mission.id), creation_key="old-pending-plan",
                    input_id="old-input", input_hash="a" * 64, config={"role": "planner"},
                    reservation=Reservation(tokens=0, cost_micros=0),
                )
                assert await loop._dispatch(intent) is False
                assert loop.store.get_intent(intent.intent_id) == intent
            await loop._start_planning(mission)
            assert loop.store.get_mission(mission.id).status is MissionStatus.CREATED
            assert await loop._try_planner_intent(mission.id, ordinal=1) is False
            with pytest.raises(ContractError, match="hierarchical_assembly_missing"):
                loop._create_planner_intent_now(mission.id, ordinal=1)
            await loop._cycle()
            assert provider.calls == 0
            events = loop.store.list_events(mission.id)
            assert any(e.type == "HierarchicalAssemblyMissing" for e in events)
            assert not [e for e in events if e.type in {"AgentCreated", "PlanRevisionCommitted"}]
            assert loop.store.get_mission(mission.id).status is MissionStatus.CREATED
    asyncio.run(case())

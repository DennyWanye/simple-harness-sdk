"""A01: authenticated grant through the real collector and Plan Commit."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_h1i_production_entry import _config, _events, _refine_reply, _seed_new_protocol

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def test_a01_authenticated_grant_binds_request_and_real_refine_commit_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def case() -> None:
        captured: list[tuple[Any, Any]] = []
        original = CommitService.commit_planning_revision

        def observe_commit(self, command, principal, **kwargs):  # type: ignore[no-untyped-def]
            captured.append((command, principal))
            return original(self, command, principal, **kwargs)

        monkeypatch.setattr(CommitService, "commit_planning_revision", observe_commit)
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(loop, tmp_path, key="h1h-a01")
            issuer = Principal("authenticated-host-a01")
            planner_principal_id = loop._owner
            api = PlanningAuthorizationApi(
                loop.store,
                tenant_id=mission.tenant_id,
                principal=issuer,
            )

            # `_create_planner_intent_now` is the production synchronous core.  Its
            # request insert and API.issue's grant/request binding share this outer
            # Store transaction; no test-made PlanningRequestBinding is used.
            with loop.store.transaction():
                intent = loop._create_planner_intent_now(mission.id, ordinal=1)
                grant = api.issue(
                    mission.id,
                    command_id="grant-h1h-a01",
                    request_id=intent.intent_id,
                    planner_principal_id=planner_principal_id,
                )
                request = PlanningDecisionStore(loop.store).get_planning_request(intent.intent_id)
                binding = PlanningAdmissionStore(loop.store).get_request_binding(intent.intent_id)
                assert request is not None and binding is not None
                assert request.mission_id == mission.id
                assert binding["mission_id"] == mission.id
                assert binding["tenant_id"] == mission.tenant_id
                assert binding["scope_id"] == "mission"
                assert binding["planner_principal_id"] == planner_principal_id
                assert binding["grant_id"] == grant.grant_id
                assert binding["grant_revision"] == grant.revision
                assert binding["grant_hash"] == grant.grant_hash

            durable_grant = PlanningAdmissionStore(loop.store).get_grant(
                grant.grant_id, grant.revision
            )
            assert durable_grant is not None
            assert durable_grant["issuer_id"] == issuer.principal_id
            assert durable_grant["planner_principal_id"] == planner_principal_id
            assert durable_grant["mission_id"] == mission.id
            assert durable_grant["tenant_id"] == mission.tenant_id
            assert durable_grant["issuer_command_id"] == "grant-h1h-a01"

            await loop._collect_plan_decision(
                intent,
                object(),
                mission,
                _refine_reply(intent.config["planning_package"]),
                dispatch,
            )

            assert _events(loop, mission.id, "PlanningDecisionEvaluated")[-1].payload[
                "status"
            ] == str(PlanningDecisionStatus.COMMITTED)
            assert len(_events(loop, mission.id, "PlanRevisionCommitted")) == 1
            assert dispatch.network(mission.id).plan_revision == 1
            assert len(captured) == 1
            command, commit_principal = captured[0]
            assert command.mission_id == request.mission_id == durable_grant["mission_id"]
            assert command.scope_id == binding["scope_id"] == durable_grant["scope_id"]
            assert command.issued_by == commit_principal.principal_id == planner_principal_id

    asyncio.run(case())

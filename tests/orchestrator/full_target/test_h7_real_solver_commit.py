"""Actual optional UP-Aries -> original collector -> guarded Plan Commit."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.htn.backend_port import PlanningLimits
from agent_orchestrator.planning.htn.backends.up_aries import UpAriesPlanningBackend
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from test_h1i_production_entry import _config, _seed_new_protocol, _open_planner_round, _refine_reply


@pytest.mark.parametrize("revoke_before_commit", (False, True))
def test_actual_solver_cannot_bypass_the_original_commit_guard(tmp_path, monkeypatch, revoke_before_commit):
    pytest.importorskip("up_aries")
    from agent_orchestrator.orchestrator import planning_backend_runtime

    async def case():
        config = replace(_config(tmp_path), planning_backend=UpAriesPlanningBackend(),
            planning_backend_limits=PlanningLimits(timeout_seconds=30, max_expansions=None, max_tokens=131072))
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            mission, _, _, dispatch = _seed_new_protocol(loop, tmp_path, key="actual-solver")
            intent = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            api = PlanningAuthorizationApi(loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner))
            grant = api.issue(mission.id, command_id="solver-grant", request_id=intent.intent_id)
            original = planning_backend_runtime.solve_decision

            async def solve_then_revoke(*args, **kwargs):
                result = await original(*args, **kwargs)
                api.revoke(grant.grant_id, expected_revision=1, command_id="revoke-after-solving", reason="operator withdrew authority")
                return result

            if revoke_before_commit:
                monkeypatch.setattr(planning_backend_runtime, "solve_decision", solve_then_revoke)
            await loop._collect_plan_decision(intent, object(), mission,
                _refine_reply(intent.config["planning_package"]), dispatch)
            events = tuple(loop.store.iter_events(mission.id))
            solved = [e for e in events if e.type == "PlanningBackendReturned"]
            assert len(solved) == 1 and solved[0].payload["status"] == "SOLVED"
            assert solved[0].payload["witness"]["hierarchy_json"]
            committed = [e for e in events if e.type == "PlanRevisionCommitted"]
            decisions = [e for e in events if e.type == "PlanningDecisionEvaluated"]
            if revoke_before_commit:
                assert not committed
                assert int(dispatch.network(mission.id).plan_revision) == 0
                assert decisions[-1].payload["status"] == "COMMIT_REJECTED"
                assert decisions[-1].payload["rejection_codes"]
            else:
                assert len(committed) == 1 and int(dispatch.network(mission.id).plan_revision) == 1
                assert committed[0].payload["source"]["intent_id"] == intent.intent_id
                assert decisions[-1].payload["status"] == "COMMITTED"

    asyncio.run(case())

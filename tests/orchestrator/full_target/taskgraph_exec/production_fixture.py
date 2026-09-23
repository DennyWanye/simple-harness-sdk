# SPDX-License-Identifier: Apache-2.0
"""Original SDK producers for installed TaskGraph acceptance.

Run against a frozen installed candidate. Missing deployment evidence is a hard
failure: this fixture never substitutes a grant, certificate or APPLIED record.
Only the model response is scripted; original SDK dispatch/collection and native
deterministic method selection both retain their production behavior.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_h1i_production_entry import _config, _open_planner_round, _refine_reply, _seed_new_protocol

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_network import DEFAULT_PROJECTION_BUDGET
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.taskgraph_assembly import TaskGraphDeploymentPorts
from agent_orchestrator.orchestrator.taskgraph_deployment import InstalledHtnWiringAcceptance
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider, package_of


@dataclass
class ProductionWorld:
    loop: Any
    mission: Any
    dispatch: Any
    graph: Any
    provider: Any
    intent: Any
    authorization: Any
    grant: Any

    async def commit_seed(self) -> None:
        """Dispatch the existing Planner intent through its original selected route."""
        async with asyncio.timeout(20):
            while True:
                current = self.loop.store.get_intent(self.intent.intent_id)
                assert current is not None
                if current.state == 'SUBMITTED':
                    await self.loop._collect(current)
                elif current.state in {'PENDING', 'CLAIMED', 'AGENT_CREATED'}:
                    await self.loop._dispatch(current)
                elif current.state == 'SETTLED':
                    break
                else:
                    raise AssertionError(f'original Planner ended as {current.state}')
                await asyncio.sleep(.01)
        active = HtnStore(self.loop.store).active_plan_revision(self.mission.id)
        assert active is not None and active.revision == 1
        native = self.intent.config.get('native_planning_decision') is not None
        assert self.provider.by_role.get('planner', 0) == (0 if native else 1)


@asynccontextmanager
async def enabled_world(tmp_path: Path, *, key: str, worker_steps: tuple[Any, ...] = ()):
    reader = InstalledHtnWiringAcceptance()
    reader._read()  # Fail before creating a DB if the installed source is unverified.
    provider = RoleScriptedProvider({'planner': [lambda request: _refine_reply(package_of(request))],
                                    'worker': list(worker_steps)})
    async with Orchestrator(_config(tmp_path), provider) as loop:
        mission, _env, _binding, dispatch = _seed_new_protocol(loop, tmp_path, key=key)
        principal = Principal(loop._owner)
        graph = loop.install_taskgraph(TaskGraphDeploymentPorts(
            tenant_id=mission.tenant_id, principal=principal,
            deployment_acceptance=reader, graph_budget=DEFAULT_PROJECTION_BUDGET))
        intent = await _open_planner_round(loop, mission, dispatch, ordinal=1)
        authorization = PlanningAuthorizationApi(loop.commit, tenant_id=mission.tenant_id, principal=principal)
        grant = authorization.issue(mission.id, command_id='grant:' + key, request_id=intent.intent_id)
        assert loop.store.connection.execute(
            'SELECT 1 FROM taskgraph_policy_bindings WHERE mission_id=?', (mission.id,)).fetchone() is None
        graph.policy.enable_taskgraph_contract(mission.id, 'enable:' + key)
        yield ProductionWorld(loop, mission, dispatch, graph, provider, intent, authorization, grant)

# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 §6.4: a Mission created to run on the strict TaskGraph waits for it.

The requirement is written in the creation transaction.  After the real planning
grant and before the authenticated enable, the planning request neither dispatches
(no local REFINE, no model call) nor can any plan be committed; the Mission waits
visibly.  After the enable the same request commits a plan that carries its TaskGraph
revision record.  The enable is recorded as a system action on the principal's
behalf, and one Mission-derived command id yields one binding however often it runs.

The deployment-acceptance reader below is a unit-test double: this file tests the
waiting/commit gates, not the installed source proof (``InstalledHtnWiringAcceptance``
is exercised by the production-seed tests against the real installed package).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_h1i_production_entry import _config, _open_planner_round, _refine_reply, _seed_new_protocol

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.revision_records import SourceRef
from agent_orchestrator.graph.task_network import DEFAULT_PROJECTION_BUDGET
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.taskgraph_assembly import TaskGraphDeploymentPorts
from agent_orchestrator.orchestrator.taskgraph_requirement import (
    awaiting_taskgraph,
    enable_command_id,
    missions_awaiting_taskgraph,
    require_taskgraph,
    taskgraph_required,
)
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.store import StoreError
from agent_orchestrator.testing.fixtures import RoleScriptedProvider, package_of


def _acceptance(store, mission_id, policy):
    return SourceRef(channel="h1h_deployment_acceptance", identity="d" * 64, revision=1, digest="d" * 64)


async def _drive(loop, intent, *, rounds: int = 1):
    for _ in range(rounds):
        current = loop.store.get_intent(intent.intent_id)
        if current.state == "SUBMITTED":
            await loop._collect(current)
        elif current.state in {"PENDING", "CLAIMED", "AGENT_CREATED"}:
            await loop._dispatch(current)
        await asyncio.sleep(0)
    return loop.store.get_intent(intent.intent_id)


def _world(loop, tmp_path, key, *, required=True):
    mission, _env, _binding, dispatch = _seed_new_protocol(loop, tmp_path, key=key)
    if required:
        with loop.store.transaction():
            require_taskgraph(loop.store, mission.id)
    principal = Principal(loop._owner)
    graph = loop.install_taskgraph(TaskGraphDeploymentPorts(
        tenant_id=mission.tenant_id, principal=principal,
        deployment_acceptance=_acceptance, graph_budget=DEFAULT_PROJECTION_BUDGET))
    return mission, dispatch, principal, graph


def test_a_required_mission_waits_after_its_grant_and_plans_only_once_bound(tmp_path):
    """**Mutation**: drop the ``awaits_taskgraph`` gate in ``_dispatch`` → red (the
    deterministic REFINE is attempted and refused by the commit gate, or commits)."""

    async def case():
        provider = RoleScriptedProvider({"planner": [lambda request: _refine_reply(package_of(request))]})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, dispatch, principal, graph = _world(loop, tmp_path, "tg-required")
            intent = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(loop.commit, tenant_id=mission.tenant_id, principal=principal).issue(
                mission.id, command_id="grant:tg-required", request_id=intent.intent_id)
            assert taskgraph_required(loop.store, mission.id)
            assert missions_awaiting_taskgraph(loop.store) == [mission.id]

            waiting = await _drive(loop, intent, rounds=3)
            assert waiting.state == "PENDING"
            assert HtnStore(loop.store).active_plan_revision(mission.id) is None
            assert provider.by_role.get("planner", 0) == 0
            assert loop._has_pending_planning_waits(mission.id)

            receipt = graph.policy.enable_taskgraph_contract(mission.id, enable_command_id(mission.id))
            assert not awaiting_taskgraph(loop.store, mission.id)
            assert missions_awaiting_taskgraph(loop.store) == []
            async with asyncio.timeout(20):
                while (current := await _drive(loop, intent)).state != "SETTLED":
                    assert current.state not in {"FAILED", "CANCELLED"}, current.state
            active = HtnStore(loop.store).active_plan_revision(mission.id)
            assert active is not None and active.revision == 1
            assert loop.store.connection.execute(
                "SELECT source_kind FROM taskgraph_revision_records WHERE mission_id=? AND revision=1",
                (mission.id,)).fetchone()[0] == "SEED_COMMIT"

            # One Mission-derived command id: a second (e.g. concurrent) enable reads
            # the same receipt; there is one binding.
            again = graph.policy.enable_taskgraph_contract(mission.id, enable_command_id(mission.id))
            assert again == receipt
            assert loop.store.connection.execute(
                "SELECT COUNT(*) FROM taskgraph_policy_bindings WHERE mission_id=?", (mission.id,)).fetchone()[0] == 1
            [event] = [e for e in loop.store.iter_events(mission.id) if e.type == "TaskGraphContractEnabled"]
            assert event.actor_type == "system" and event.actor_id == principal.principal_id
            assert event.payload["enabled_by"] == "HOST_DELEGATED"
            with pytest.raises(Exception, match="TG_IMMUTABLE"):
                loop.store.connection.execute("DELETE FROM taskgraph_requirements WHERE mission_id=?",
                                              (mission.id,))

    asyncio.run(case())


def test_no_unbound_plan_is_committed_for_a_required_mission(tmp_path):
    """The commit gate holds even when a caller bypasses the dispatch wait.

    **Mutation**: drop the ``TASKGRAPH_REQUIRED_NOT_BOUND`` check → red (the plan
    commits without a TaskGraph revision record)."""

    async def case():
        provider = RoleScriptedProvider({"planner": [lambda request: _refine_reply(package_of(request))]})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, dispatch, principal, _graph = _world(loop, tmp_path, "tg-required-commit")
            intent = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(loop.commit, tenant_id=mission.tenant_id, principal=principal).issue(
                mission.id, command_id="grant:tg-required-commit", request_id=intent.intent_id)
            from agent_orchestrator.orchestrator import taskgraph_requirement
            original = taskgraph_requirement.awaits_taskgraph
            taskgraph_requirement.awaits_taskgraph = lambda store, intent: False
            try:
                for _ in range(4):
                    current = loop.store.get_intent(intent.intent_id)
                    if current.state in {"SETTLED", "FAILED", "CANCELLED"}:
                        break
                    await _drive(loop, intent)
            finally:
                taskgraph_requirement.awaits_taskgraph = original
            assert HtnStore(loop.store).active_plan_revision(mission.id) is None
            refusals = [e for e in loop.store.iter_events(mission.id)
                        if "TASKGRAPH_REQUIRED_NOT_BOUND" in str(e.payload)]
            assert refusals

    asyncio.run(case())


def test_the_requirement_is_written_only_at_creation(tmp_path):
    async def case():
        provider = RoleScriptedProvider({"planner": [lambda request: _refine_reply(package_of(request))]})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, dispatch, principal, graph = _world(loop, tmp_path, "tg-late", required=False)
            with pytest.raises(StoreError, match="TRANSACTION_REQUIRED"):
                require_taskgraph(loop.store, mission.id)
            intent = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(loop.commit, tenant_id=mission.tenant_id, principal=principal).issue(
                mission.id, command_id="grant:tg-late", request_id=intent.intent_id)
            async with asyncio.timeout(20):
                while (current := await _drive(loop, intent)).state != "SETTLED":
                    assert current.state not in {"FAILED", "CANCELLED"}, current.state
            # An existing Mission with a plan is not converted afterwards.
            with pytest.raises(StoreError, match="ONLY_AT_CREATION"):
                with loop.store.transaction():
                    require_taskgraph(loop.store, mission.id)
            assert not taskgraph_required(loop.store, mission.id)

    asyncio.run(case())

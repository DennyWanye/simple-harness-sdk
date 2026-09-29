"""Package 7 enters the real collector without upgrading frozen older Missions."""
from __future__ import annotations

import asyncio
import json

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.role_templates import PLANNER_HIERARCHICAL_V12, PLANNING_DECISION_PACKAGE_LABEL
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from test_h1i_production_entry import _config, _seed_new_protocol, _refine_reply


def test_package7_initial_refinement_reaches_atomic_commit(tmp_path):
    async def case():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _world, _binding, dispatch = _seed_new_protocol(loop, tmp_path, key="h4-package7")
            intent = await loop._create_planner_intent(mission.id, ordinal=1)
            assert intent.config["prompt_version"] == PLANNER_HIERARCHICAL_V12.prompt_version
            package = intent.config["planning_package"]
            assert package["package_version"] == PLANNING_DECISION_PACKAGE_LABEL
            assert {"REPAIR", "BIND_EXISTING_GOAL"}.issubset(package["planning_protocol"]["enabled_decision_types"])
            assert {"REBIND_INPUT", "CANCEL_BRANCH", "PROPOSE_SUCCESSOR"}.issubset(
                package["planning_protocol"]["enabled_repair_kinds"])
            PlanningAuthorizationApi(loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)).issue(
                mission.id, command_id="grant-h4-entry", request_id=intent.intent_id)
            await loop._collect_plan_decision(intent, object(), mission, _refine_reply(package), dispatch)
            decision = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(intent.intent_id, 0)
            assert decision is not None and decision["status"] == "COMMITTED", None if decision is None else (decision["status"], decision["detail_json"])
            assert int(dispatch.network(mission.id).plan_revision) == 1
            network = dispatch.network(mission.id)
            old = next(binding for binding in network.task_bindings
                       if str(binding.form) == "primitive" and not binding.input_ports)
            task_type = next(spec for spec in _world.catalog.task_types() if spec.goal_signature == old.goal_signature)
            next_intent = await loop._create_planner_intent(mission.id, ordinal=2)
            next_package = next_intent.config["planning_package"]
            subject = next(row for row in next_package["planning_subjects"] if row["task_id"] == str(old.task_id))
            def visible(kind, identity):
                return next(row for row in next_package["visible_refs"] if row["kind"] == kind and row["id"] == identity)
            body = {"schema_version": 1, "decision_type": "REPAIR", "subject_key": subject["subject_key"],
                "rationale": "Replace this unaccepted Task while retaining its duty.", "reason_refs": [], "assumptions": [],
                "uncertainties": [], "alternatives": [], "replan_triggers": [],
                "payload": {"repair_kind": "PROPOSE_SUCCESSOR", "old_task_ref": visible("task", str(old.task_id)),
                    "obligation_ref": visible("obligation", str(old.obligation_id)),
                    "goal_type_ref": task_type.task_type_ref.to_json(), "bindings": dict(old.typed_parameters)}}
            PlanningAuthorizationApi(loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)).issue(
                mission.id, command_id="grant-h4-successor", request_id=next_intent.intent_id)
            await loop._collect_plan_decision(next_intent, object(), loop.store.get_mission(mission.id),
                "<planning_decision>" + json.dumps(body) + "</planning_decision>", dispatch)
            stored = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(next_intent.intent_id, 0)
            assert stored is not None and stored["status"] == "COMMITTED", None if stored is None else (stored["status"], stored["detail_json"])
            assert int(dispatch.network(mission.id).plan_revision) == 2
            assert old.task_id not in {spec.task_id for spec in dispatch.network(mission.id).occurrences}
            from agent_orchestrator.orchestrator.planning_repair_requests import collect_triggers, pending_requests
            collect_triggers(loop, loop.store.get_mission(mission.id))
            assert not pending_requests(loop.store, mission.id)
    asyncio.run(case())

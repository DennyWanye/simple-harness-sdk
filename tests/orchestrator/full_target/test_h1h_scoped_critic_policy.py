"""Real Plan Commit must schedule the Critic required by scoped acceptance."""
import asyncio
import pytest
from test_h1i_production_entry import _config, _seed_new_protocol, _open_planner_round, _refine_reply
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from agent_orchestrator.contracts.htn import TaskForm
from agent_orchestrator.orchestrator.occurrence_tasks import read_only_leaf


def test_real_new_protocol_commit_schedules_independent_critic_for_readonly_content(tmp_path):
    async def case():
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, _, _, dispatch = _seed_new_protocol(loop, tmp_path, key="scoped-critic")
            intent = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(loop.store, tenant_id=mission.tenant_id,
                                     principal=Principal(loop._owner)).issue(
                mission.id, command_id="scoped-critic-grant", request_id=intent.intent_id)
            await loop._collect_plan_decision(intent, object(), mission,
                _refine_reply(intent.config["planning_package"]), dispatch)
            network = dispatch.network(mission.id)
            assert network.plan_revision == 1
            read_only_policies = []
            for occurrence in network.occurrences:
                if occurrence.form != TaskForm.PRIMITIVE:
                    continue
                task = loop.store.get_task(str(occurrence.task_id))
                assert task is not None and "critic_review" in task.verification_policy
                binding = network.binding_for_task(occurrence.task_id)
                if read_only_leaf(binding):
                    read_only_policies.append(task.verification_policy)
            assert read_only_policies
            from agent_orchestrator.orchestrator.scoped_content_review import task_content_prompt_scope
            from agent_orchestrator.context.context_builder import build_critic_package
            for occurrence in network.occurrences:
                if occurrence.form != TaskForm.PRIMITIVE:
                    continue
                task = loop.store.get_task(str(occurrence.task_id))
                scope = task_content_prompt_scope(loop.store, mission.id, task.id)
                package = build_critic_package(mission, task, attempt_id="scope-preview-only",
                    artifacts=[], test_output=None, workspace_files=[], task_content_scope=scope)
                assert package.package["task_content_scope"]["purpose"] == "TASK_CONTENT"
                assert package.package["task_contract"]["success_criteria"] == [
                    c["criterion_id"] for c in scope["criteria"]]
                if "code_test" not in task.verification_policy:
                    assert "c-test-passes" not in package.package["task_contract"]["success_criteria"]
            assert any("code_test" not in policy for policy in read_only_policies)
            assert provider.calls == 0
    asyncio.run(case())


def test_suspended_method_does_not_suppress_real_synthesis_request(tmp_path):
    from test_htn_end_to_end import build_world
    from agent_orchestrator.storage.htn_store import HtnStore
    from agent_orchestrator.contracts.htn import MissionRef
    world = build_world(tmp_path, key="registry-dispatch-scope")
    reference = world.contract.method_ref()
    assert world.dispatch.method_applicability(world.mission.id)
    assert world.dispatch.goals_needing_method(world.mission.id) == ()
    registration = world.env.registry.suspend(reference, reason="confirmed counterexample")
    HtnStore(world.store).set_method_registration(registration)
    assert world.dispatch.method_applicability(world.mission.id) == ()
    assert world.dispatch.goals_needing_method(world.mission.id)
    # Immutable definitions stay available to historical instances.
    assert world.env.registry.definition(reference) == world.contract
    from agent_orchestrator.planning.htn.planner_package import method_library
    assert method_library(world.env.registry, ["plan.goal"], mission_id=world.mission.id) == ()
    registration = world.env.registry.reinstate(reference,
        mission_id=MissionRef("a-different-trial-mission"), reason="scoped re-evaluation")
    HtnStore(world.store).set_method_registration(registration)
    assert world.dispatch.method_applicability(world.mission.id) == ()
    assert world.dispatch.goals_needing_method(world.mission.id)
    world.env.registry.allow_evaluation_trials(reference, [world.mission.id])
    assert world.dispatch.method_applicability(world.mission.id)
    assert world.dispatch.goals_needing_method(world.mission.id) == ()


def test_current_runtime_views_do_not_reintroduce_suspended_methods(tmp_path):
    async def case():
        from agent_orchestrator.storage.htn_store import HtnStore
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, _, _, dispatch = _seed_new_protocol(loop, tmp_path, key="registry-v7-package")
            world = dispatch.require_planning_world()
            reference = next(r for r in world.registry.method_refs() if r.method_id == "code.fix-by-assessed-revert")
            registration = world.registry.suspend(reference, reason="source-library experiment")
            HtnStore(loop.store).set_method_registration(registration)
            sealed = loop._hierarchical_planner_package(dispatch, mission, ordinal=1)
            assert sealed.package["package_version"] == __import__("agent_orchestrator.runtime.role_templates", fromlist=["x"]).PLANNING_DECISION_PACKAGE_LABEL
            rows = sealed.package["method_library"]
            assert rows and all(r["method_ref"] != reference.to_json() for r in rows)
            assert provider.calls == 0
    asyncio.run(case())


@pytest.mark.parametrize("with_grant", [True, False])
def test_unconditional_method_can_commit_only_with_actual_planning_grant(tmp_path, with_grant):
    import dataclasses
    from agent_orchestrator.contracts.htn import RegistryAuthor
    from agent_orchestrator.planning.htn.registry import MethodProposal
    from agent_orchestrator.storage.htn_store import HtnStore
    async def case():
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, world, _, dispatch = _seed_new_protocol(loop, tmp_path, key="unconditional-method")
            reference = next(r for r in world.registry.method_refs() if r.method_id == "code.fix-by-patch")
            contract = dataclasses.replace(world.registry.definition(reference),
                method_id="code.unconditional-patch", applicable_when=())
            receipt = world.registry.admit(MethodProposal(method=contract, author=RegistryAuthor.SYSTEM),
                author=RegistryAuthor.SYSTEM, policy=world.policy())
            assert receipt.admitted
            htn = HtnStore(loop.store)
            htn.register_method(contract, receipt.registration)
            for ref in world.registry.method_refs():
                if ref != contract.method_ref():
                    htn.set_method_registration(world.registry.suspend(ref, reason="isolate unconditional method"))
            intent = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            if with_grant:
                PlanningAuthorizationApi(loop.store, tenant_id=mission.tenant_id,
                    principal=Principal(loop._owner)).issue(mission.id, command_id="unconditional-grant",
                                                         request_id=intent.intent_id)
            report, = dispatch.method_applicability(mission.id)
            assert report.report.applicable and not report.report.authorization.allowed
            await loop._collect_plan_decision(intent, object(), mission,
                _refine_reply(intent.config["planning_package"]), dispatch)
            plan = htn.active_plan_revision(mission.id)
            assert (plan is not None and plan.revision == 1) if with_grant else plan is None
            assert provider.calls == 0
    asyncio.run(case())

# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""F-P32-2 / P32-A03: actual Worker wire input, not a hand-written Mission schema.

Deterministic local Provider only; none of these cases executes a publish connector.
"""

from __future__ import annotations

import asyncio
import hashlib
import re

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, graph_proposal_step, package_of
from graph_helpers7 import change, node, spec

from agent_orchestrator.contracts import Artifact, ContractError
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.action_commits import (
    CandidateRejected,
    bind_artifact_params,
    check_candidate,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.action_schema import worker_action_contract
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.connectors import TestConfigService
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from simple_harness.contracts import MessageRole, canonical_json

ACTION = "action:file_publish.publish:weekly/report.md"
ENABLED = DeploymentPolicy(enabled_connectors=("file_publish",))


def _connector(tmp_path):
    # Descriptor only: no execute/lookup or files in the publish destination.
    return FilePublishConnector(tmp_path / "published", tmp_path / "ledger")


@pytest.mark.parametrize("action_task,enabled,task_action_criterion,role", [
    (True, True, True, "worker"), (True, True, False, "worker"),
    (False, True, False, "worker"), (False, False, False, "worker"),
    (True, True, True, "connector"),
], ids=["publish", "mission-charter-publish", "ordinary", "disabled-ordinary", "connector"])
def test_actual_worker_provider_input_has_only_relevant_declared_schema(
    tmp_path, action_task, enabled, task_action_criterion, role,
):
    seen = []

    def capture(request):
        seen.append((package_of(request), request))
        return envelope_step(summary="只检查输入", artifacts=[], claims=[])(request)

    async def case():
        publisher = _connector(tmp_path)
        connectors = {"file_publish": publisher}
        provider = RoleScriptedProvider({role: [capture]})
        config = OrchestratorConfig(
            evidence_root=tmp_path / "evidence",
            deployment_policy=ENABLED if enabled else DeploymentPolicy(),
        )
        async with Orchestrator(config, provider, connectors=connectors) as orch:
            mission = await orch.submit_mission(spec(
                key="action-wire", goal="编写报告",  # no manual schema in the goal
                success_criteria=("file:report.md", ACTION) if enabled else ("file:report.md",),
            ))
            planning = orch.commit.begin_planning(mission.id)
            task_criteria = (["file:report.md", ACTION] if task_action_criterion
                             else ["file:report.md"])
            outputs = ["report.md", "actions/publish.json"] if action_task else ["report.md"]
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [node(
                    "A", goal="生成报告和候选" if action_task else "生成报告",
                    success_criteria=task_criteria, outputs=outputs,
                )]}),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            if role != "worker":
                orch.commit.commit_graph_change(
                    mission.id,
                    change(orch.store.get_mission(mission.id).final_report["graph_version"], [
                        {"op": "set_role", "task_id": tasks[0].id, "role": role},
                    ]),
                    source={"manager": "fixture"},
                )
            task = orch.store.get_task(tasks[0].id)
            assert await orch._next_attempt(orch.store.get_mission(mission.id), task, [])
            [attempt] = orch.store.list_attempts(tasks[0].id)
            intent = orch.store.get_intent_for_subject(attempt.id)
            assert await orch._dispatch(intent)
            intent = orch.store.get_intent(intent.intent_id)
            async def completed():
                while True:
                    result = await orch.bridge_for(intent).result(
                        agent_id=intent.agent_id, turn_id=intent.expected_turn_id,
                    )
                    if result is not None:
                        return result
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(completed(), timeout=15)
            assert len(seen) == 1 and provider.calls == 1
            body, request = seen[0]
            assert all("action_candidate_contract" not in str(m.content)
                       for m in request.messages if m.role is MessageRole.SYSTEM)
            assert intent.config["context_version"] == attempt.context_version
            assert intent.config["prompt_version"] == attempt.prompt_version
            if not action_task:
                assert "action_candidate_contract" not in body
                assert "action_candidate_contract" not in intent.config["message"]
                return
            contract = body["action_candidate_contract"]
            assert contract["version"] == "action-candidate-context-v2"
            assert contract["output_files"] == ["actions/publish.json"]
            assert contract["required_fields"] == [
                "connector", "operation", "target", "params", "reason",
            ]
            assert contract["additional_fields"] is False
            assert contract["operations"] == [{
                "connector": "file_publish", "operation": "publish",
                "target": "weekly/report.md", "level": "L2", "required_approvals": 1,
                "required_model_params": ["artifact_path"],
                "system_bound_artifact_fields": [
                    "artifact_id", "content_hash", "size", "storage_uri",
                ],
            }]
            assert str(publisher.root) not in str(contract)
            assert str(publisher.ledger_path) not in str(contract)
            assert "ledger" not in str(contract)

    asyncio.run(case())


@pytest.mark.parametrize("has_action,enabled", [(True, True), (False, True), (True, False)])
def test_planner_wire_receives_source_destination_and_approval_semantics(
    tmp_path, has_action, enabled,
):
    async def case():
        provider = RoleScriptedProvider({"planner": [graph_proposal_step([node("A")])]})
        publisher = _connector(tmp_path)
        config = OrchestratorConfig(
            evidence_root=tmp_path / "evidence",
            deployment_policy=ENABLED if enabled else DeploymentPolicy(),
        )
        async with Orchestrator(config, provider, connectors={"file_publish": publisher}) as orch:
            mission_spec = spec(
                key="planner-action-wire", goal="Publish report.md to weekly/report.md",
                success_criteria=("file:report.md", ACTION) if has_action else ("file:report.md",),
            )
            if has_action and not enabled:
                with pytest.raises(ContractError, match="connector_not_enabled"):
                    await orch.submit_mission(mission_spec)
                assert provider.calls == 0
                return
            mission = await orch.submit_mission(mission_spec)
            orch.commit.begin_planning(mission.id)
            intent = await orch._create_planner_intent(mission.id, ordinal=1)
            assert await orch._dispatch(intent)
            intent = orch.store.get_intent(intent.intent_id)

            async def completed():
                while await orch.bridge_for(intent).result(
                    agent_id=intent.agent_id, turn_id=intent.expected_turn_id,
                ) is None:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(completed(), timeout=5)
            assert provider.calls == 1
            request = provider.requests[0]
            package = package_of(request)
            if not (has_action and enabled):
                assert "action_candidate_contract" not in package
                # Generic lifetime-budget semantics now reach code Planners too.
                # Preserve the original SDK9f70e00 baseline after removing only
                # that deliberate new section; action projection remains absent.
                assert package["budget_allocation_semantics"]["kind"] == (
                    "permitted_ceiling_not_expected_spend"
                )
                historical_message = dict(intent.config["message"])
                historical_message["content"] = re.sub(
                    r"\n\n## budget_allocation_semantics\n.*?(?=\n\n## |\Z)",
                    "", historical_message["content"], flags=re.DOTALL,
                )
                assert hashlib.sha256(canonical_json(historical_message).encode()).hexdigest() == (
                    "3c9ec1513b5cf20e4f73bb09484cfd08a9c00e2018fd053f73bf72e820955c54"
                )
                message_hash = hashlib.sha256(
                    canonical_json(intent.config["message"]).encode()
                ).hexdigest()
                assert message_hash == (
                    "c5017c6420bf3677a1fbd52d45a49378e32a22b21e618b7df3f61eceacc202f5"
                )
                assert intent.config["context_version"] == "ctx-c4042b6c85b13dbd"
                return
            contract = package["action_candidate_contract"]
            assert contract["version"] == "action-candidate-context-v2"
            assert contract["candidate_output_pattern"] == "actions/*.json"
            assert "output_files" not in contract  # Planner must choose actual Task outputs.
            assert contract["operations"][0]["target"] == "weekly/report.md"
            assert contract["operations"][0]["required_model_params"] == ["artifact_path"]
            assert contract["operations"][0]["required_approvals"] == 1
            assert "target is an external destination" in contract["planning_notice"]
            assert "after Result acceptance" in contract["planning_notice"]
            assert "not the candidate JSON" in contract["notice"]
            assert str(publisher.root) not in str(contract)
            assert str(publisher.ledger_path) not in str(contract)
            assert all("action_candidate_contract" not in str(m.content)
                       for m in request.messages if m.role is MessageRole.SYSTEM)
            assert intent.config["message"]  # Actual frozen request, not a synthetic helper call.

    asyncio.run(case())


def test_schema_follower_passes_original_checker_without_publishing(tmp_path):
    connectors = {"file_publish": _connector(tmp_path)}
    contract = worker_action_contract(
        mission_criteria=(ACTION,), task_criteria=(ACTION,),
        task_outputs=("report.md", "actions/publish.json"),
        connectors=connectors, deployment=ENABLED,
    )
    assert contract is not None
    candidate = {
        "connector": contract["operations"][0]["connector"],
        "operation": contract["operations"][0]["operation"],
        "target": contract["operations"][0]["target"],
        "params": {"artifact_path": "report.md"}, "reason": "发布已核对的报告",
    }
    checked, decision = check_candidate(
        candidate, criteria=(ACTION,), connectors=connectors, deployment=ENABLED,
    )
    assert checked == candidate and decision.level == "L2" and decision.required_approvals
    # Rule_check first binds the accepted Result's exact Artifact identity, then
    # checks scope and deployment. The model only wrote artifact_path.
    artifact = Artifact(
        id="artifact-report", mission_id="mission", task_id="task", attempt_id="attempt",
        type="file", path="report.md", version=1, content_hash="a" * 64,
        size_bytes=17, produced_by="attempt",
        storage_uri=str(tmp_path / "cas" / ("a" * 64)),
    )
    bound = {
        **candidate,
        "params": bind_artifact_params(candidate["params"], {"report.md": artifact}),
    }
    checked_bound, _ = check_candidate(
        bound, criteria=(ACTION,), connectors=connectors, deployment=ENABLED,
    )
    assert checked_bound["params"] == {
        "artifact_path": "report.md", "artifact_id": artifact.id,
        "content_hash": artifact.content_hash, "size": artifact.size_bytes,
        "storage_uri": artifact.storage_uri,
    }
    # The original binding refuses a path not in this Result; there is no publish call.
    with pytest.raises(CandidateRejected, match="artifact_not_in_result"):
        bind_artifact_params(candidate["params"], {})
    with pytest.raises(CandidateRejected, match="action_out_of_scope"):
        check_candidate({**candidate, "target": "other.md"},
                        criteria=(ACTION,), connectors=connectors, deployment=ENABLED)
    with pytest.raises(CandidateRejected, match="invalid_candidate"):
        check_candidate({**candidate, "approved": True},
                        criteria=(ACTION,), connectors=connectors, deployment=ENABLED)


def test_undeployed_or_unrecognised_operation_never_gains_a_schema(tmp_path):
    connectors = {"file_publish": _connector(tmp_path)}
    scope = dict(mission_criteria=(ACTION,), task_criteria=(ACTION,),
                 task_outputs=("actions/publish.json",), connectors=connectors)
    assert worker_action_contract(**scope, deployment=DeploymentPolicy()) is None
    assert worker_action_contract(**{**scope, "connectors": {}}, deployment=ENABLED) is None
    assert worker_action_contract(**{**scope, "task_outputs": ("report.md",)},
                                  deployment=ENABLED) is None
    # The output itself can be the Task's declaration; the action criterion may
    # remain only in the Mission, where the original scope checker will find it.
    assert worker_action_contract(**{**scope, "task_criteria": ()},
                                  deployment=ENABLED)["operations"][0]["operation"] == "publish"
    denied = {"connector": "file_publish", "operation": "publish",
              "target": "weekly/report.md", "params": {"artifact_path": "report.md"},
              "reason": "报告"}
    with pytest.raises(CandidateRejected, match="connector_not_enabled"):
        check_candidate(denied, criteria=(ACTION,), connectors=connectors,
                        deployment=DeploymentPolicy())


def test_another_deployed_descriptor_supplies_its_own_required_params(tmp_path):
    connector = TestConfigService(tmp_path / "service.json")
    criterion = "action:test_config.set:mode"
    contract = worker_action_contract(
        mission_criteria=(criterion,), task_criteria=(criterion,),
        task_outputs=("actions/mode.json",), connectors={"test_config": connector},
        deployment=DeploymentPolicy(enabled_connectors=("test_config",)),
    )
    assert contract is not None
    assert contract["operations"] == [{
        "connector": "test_config", "operation": "set", "target": "mode",
        "level": "L2", "required_approvals": 1, "required_model_params": ["value"],
    }]
    assert "delete" not in str(contract)  # deployed descriptor is wider than Task scope
    assert str(connector.path) not in str(contract)


def test_existing_action_scope_is_not_rejected_by_an_unrelated_eight_operation_cap(tmp_path):
    criteria = tuple(f"action:file_publish.publish:reports/{i}.md" for i in range(9))
    contract = worker_action_contract(
        mission_criteria=criteria, task_criteria=criteria,
        task_outputs=("actions/publish.json",),
        connectors={"file_publish": _connector(tmp_path)}, deployment=ENABLED,
    )
    assert len(contract["operations"]) == 9  # existing request/context budget remains the limit

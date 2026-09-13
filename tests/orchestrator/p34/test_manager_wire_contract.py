# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Manager v4 emits the graph parser's actual flat operation union."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from graph_helpers7 import node, spec

from agent_orchestrator.contracts import ContractError, MissionStatus
from agent_orchestrator.contracts.fragments import FragmentValidationDecisionV1
from agent_orchestrator.governance.domains import (
    DOC_PROFILE_V3,
    DOC_PROFILE_V4,
    DOC_PROFILE_V5,
    DOC_PROFILE_V6,
    DOC_PROFILE_V7,
    DOC_PROFILE_V8,
    DOC_PROFILE_V9,
)
from agent_orchestrator.graph.changes import TaskGraphChange
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.role_templates import (
    _MANAGER_V4_OPERATION_EXAMPLES,
    MANAGER,
    MANAGER_V1,
    MANAGER_V2,
    MANAGER_V3,
    MANAGER_VERSION,
    TEMPLATE_VERSIONS,
)
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    envelope_step,
    graph_change_step,
    graph_proposal_step,
)

_HISTORICAL_MANAGER_SHA256 = {
    "manager-v1": "5fa4b722f4b8b61b5b1efd8873f0b831f7e4113e2ddd0a9e12443056aa8d16a8",
    "manager-v2": "b6fe0395f73206b504aa7f92b64130faf91ab8d65d59583aafc33559a812ad0c",
    "manager-v3": "30492bf28b65b71ebc7156345375fbe8ff16b9e9926dfa3a5a0f5e596c07dacf",
    "manager-doc-research-v1": "1b3e4acfbb0584918372601d5d9796c3b5452b1623bc30abdafa2122714a6af4",
    "manager-doc-research-v2": "c90096dc172613069fc93b4c70fcc30e0825bf31c40072696ba2490befc8bdeb",
    "manager-doc-research-v3": "a85312c034dfa207bed2ae28fd2421646853fed21359582f90638ce6e7157198",
    "manager-doc-research-v4": "59a3f35c0630e99d064420554d07e075ec792c551a105f1262442338f5c95acf",
}


def _change(operations):
    return TaskGraphChange.from_json(
        {"base_graph_version": 1, "basis": {}, "rationale": "wire test", "operations": operations}
    )


def test_manager_v4_prompt_examples_parse_and_historical_bindings_stay_frozen():
    assert MANAGER.prompt_version == MANAGER_VERSION == "manager-v4"
    assert len(_MANAGER_V4_OPERATION_EXAMPLES) == 8
    assert {json.loads(raw)["op"] for raw in _MANAGER_V4_OPERATION_EXAMPLES} == {
        "add_task",
        "supersede_task",
        "retarget_dependencies",
        "set_priority",
        "pause_task",
        "resume_task",
        "cancel_task",
        "set_role",
    }
    for raw in _MANAGER_V4_OPERATION_EXAMPLES:
        operation = json.loads(raw)
        assert _change([operation]).operations[0].to_json() == operation
        assert raw in MANAGER.instructions

    for template in (MANAGER_V1, MANAGER_V2, MANAGER_V3):
        digest = _HISTORICAL_MANAGER_SHA256[template.prompt_version]
        assert hashlib.sha256(template.instructions.encode()).hexdigest() == digest
    for version, digest in _HISTORICAL_MANAGER_SHA256.items():
        template = TEMPLATE_VERSIONS["manager"][version]
        assert hashlib.sha256(template.instructions.encode()).hexdigest() == digest
    assert [profile.role_templates["manager"] for profile in (
        DOC_PROFILE_V3, DOC_PROFILE_V4, DOC_PROFILE_V5, DOC_PROFILE_V6,
        DOC_PROFILE_V7, DOC_PROFILE_V8, DOC_PROFILE_V9,
    )] == [
        "manager-doc-research-v1", "manager-doc-research-v1", "manager-doc-research-v2",
        "manager-doc-research-v3", "manager-doc-research-v4", "manager-doc-research-v4",
        "manager-doc-research-v4",
    ]


@pytest.mark.parametrize(
    "operations",
    [
        [{"retarget_dependencies": {"task_id": "blocked", "dependencies": []}}],
        [{"cancel_task": {"task_id": "active", "reason": "obsolete"}}],
    ],
)
def test_manager_parser_still_rejects_nested_operation_wrappers(operations):
    with pytest.raises(ContractError, match="operation must be an object with an 'op'"):
        _change(operations)


def test_manager_parser_still_rejects_extra_graph_and_add_task_fields():
    with pytest.raises(ContractError, match="graph change proposal has unknown fields"):
        TaskGraphChange.from_json(
            {
                "base_graph_version": 1,
                "basis": {},
                "rationale": "bad wrapper",
                "operations": [],
                "fragmentproposal": {},
            }
        )
    add_task = json.loads(_MANAGER_V4_OPERATION_EXAMPLES[0])
    add_task["claim_refs_note"] = "not a graph operation field"
    with pytest.raises(ContractError, match="add_task has unknown fields"):
        _change([add_task])
    with pytest.raises(ContractError, match="fragment proposal fields missing or unknown"):
        FragmentValidationDecisionV1.from_json(
            {
                "schema_version": 1,
                "base_graph_version": 1,
                "proposal": {
                    "schema_version": 1,
                    "origin": {},
                    "criterion_ids": [],
                    "claim_refs": [],
                    "claim_refs_note": "not a fragment proposal field",
                    "material_refs": [],
                    "rationale": "bad extra field",
                },
            }
        )


def test_actual_sdk_manager_request_uses_v4_and_flat_wire_contract(tmp_path):
    async def exercise():
        requests = []

        def manager(request):
            requests.append(request)
            return graph_change_step(
                lambda package: [
                    {
                        "op": "set_role",
                        "task_id": package["trigger"]["task_id"],
                        "role": "simplifier",
                    }
                ]
            )(request)

        provider = RoleScriptedProvider(
            {
                "planner": [graph_proposal_step([node(
                    "A",
                    success_criteria=["file:done.md"],
                    verification_policy=["format_check", "rule_check"],
                    outputs=["done.md"],
                )])],
                "worker": [envelope_step(
                    summary="no progress", artifacts=[], claims=[], outcome="no_progress"
                )],
                "manager": [manager],
                "simplifier": [
                    ("workspace_write_file", {"path": "done.md", "content": "done\n"}),
                    envelope_step(summary="done", artifacts=["done.md"], claims=["done"]),
                ],
            }
        )
        config = OrchestratorConfig(
            evidence_root=tmp_path / "manager-wire", max_concurrency=1,
            candidates_per_task=1, manager_after_failures=1, max_manager_rounds=1,
        )
        async with Orchestrator(config, provider) as orch:
            mission = await orch.submit_mission(
                spec("manager-v4-wire", success_criteria=("file:done.md",))
            )
            await asyncio.wait_for(orch.run(), 15)
            assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED
            [intent] = [
                item for item in orch.store.list_intents("SETTLED") if item.kind == "manager"
            ]
            assert intent.config["prompt_version"] == "manager-v4"

        assert len(requests) == 1
        system = next(
            str(message.content)
            for message in requests[0].messages
            if str(message.content).startswith("[role:manager]")
        )
        assert system == MANAGER.instructions
        assert '"op":"retarget_dependencies"' in system
        assert '"op":"cancel_task"' in system
        assert "嵌套包装" in system
        assert "绝不能写 operationName{fields}" in system

    asyncio.run(exercise())

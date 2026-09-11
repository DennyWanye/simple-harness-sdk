# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Host support 0.9.8 · SA-4: with local code execution off, a contradiction is kept
DISPUTED and the conflict is DEFERRED — no Conflict Task is opened, because that Task's
own success criterion is a probe test run on this machine; dropping only ``code_test``
would leave an unexecuted probe standing as evidence (Host plan §3.1 S1-a)."""

from __future__ import annotations

from knowledge_helpers import claim, drive_to_running, envelope, node, passed_layers, spec, submit

from agent_orchestrator.contracts import ClaimStatus, TaskStatus
from agent_orchestrator.governance.policies import DeploymentPolicy, deployed_layers
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.storage.store import Store

KEY = "impl_a.empty_input"
OFF = DeploymentPolicy(
    allowed_tools=("workspace_read_file", "workspace_write_file", "workspace_list"),
    local_code_execution=False,
)
POLICY = ["format_check", "rule_check"]


def _dispute(tmp_path):
    service = CommitService(
        Store.open(tmp_path / "orchestrator.db"), deployed_layers=deployed_layers(OFF)
    )
    mission, _ = service.create_mission(spec("k-1", conflict_reserve_tokens=20_000))
    planning = service.begin_planning(mission.id)
    task_a, task_b = service.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {
                "tasks": [
                    node("A", verification_policy=POLICY),
                    node("B", verification_policy=POLICY),
                ]
            }
        ),
        base_version=planning.version,
        source={"planner": "fixture"},
    )[0]
    a1 = drive_to_running(service, task_a)
    sa = submit(
        service,
        a1,
        envelope(
            a1,
            claims=[
                claim(
                    "impl_a 对空输入抛 ValueError", key=KEY, stance="refutes", evidence=["NOTES.md"]
                )
            ],
        ),
    )
    service.accept_result(sa.envelope.id, verifier_results=passed_layers())
    b1 = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    sb = submit(
        service,
        b1,
        envelope(
            b1,
            claims=[
                claim("impl_a 对空输入返回 {}", key=KEY, stance="affirms", evidence=["NOTES.md"])
            ],
        ),
        turn="turn-2",
    )
    completed = service.accept_result(sb.envelope.id, verifier_results=passed_layers())
    return service, mission, sb, completed


def test_a_contradiction_is_deferred_without_a_conflict_task(tmp_path):
    service, mission, sb, completed = _dispute(tmp_path)
    assert completed.status is TaskStatus.COMPLETED  # the accept still lands
    assert len(service.store.list_tasks(mission.id)) == 2  # no Conflict Task
    conflicts = service.store.list_conflicts(mission.id)
    assert len(conflicts) == 1
    assert conflicts[0]["state"] == "DEFERRED"
    assert conflicts[0]["deferred_reason"] == "local_code_execution_disabled"
    assert service.store.list_claims(sb.envelope.id)[0].status is ClaimStatus.DISPUTED
    assert service.store.get_knowledge(service.store.list_claims(sb.envelope.id)[0].id) is None
    assert service.store.count_events(mission.id, "ConflictOpenDeferred") == 1
    assert service.store.count_events(mission.id, "ConflictOpened") == 0

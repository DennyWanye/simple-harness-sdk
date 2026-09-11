# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Host support 0.9.8 · SA-1 / SA-2 / SA-3 / SA-6: a deployment can switch off local code
execution (Host plan 2026-09-11 §3.1, plan review P0-1).

With ``local_code_execution=False`` no model-written code runs on this machine, however
the Planner fills ``verification_policy``: the Graph Manager refuses ``code_test``
(``verification_policy_undeployed``), the system defaults leave it out, a Task that
already carries it from before the switch records the layer as ERROR (never PASS), a
Mission ``pytest:`` criterion is judged unmet without running, and ``run_tests`` is
refused.  The decisive oracle is behavioural: a spy around the real ``run_pytest`` is
never called and a test file whose import writes a marker leaves no marker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_orchestrator.contracts import STEP2_IMPLEMENTED_LAYERS, Budget
from agent_orchestrator.governance.policies import DeploymentPolicy, deployed_layers
from agent_orchestrator.graph.changes import default_change_policy
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.role_templates import MANAGER, PLANNER, TEMPLATE_VERSIONS
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
    package_of,
)

TOOLS3 = ("workspace_read_file", "workspace_write_file", "workspace_list")
TOOLS4 = (*TOOLS3, "run_tests")
OFF = DeploymentPolicy(allowed_tools=TOOLS3, local_code_execution=False)
ON = DeploymentPolicy()
NO_CODE = ["format_check", "rule_check", "critic_review"]
WITH_CODE = [*NO_CODE, "code_test"]


def _config(tmp_path, deployment=OFF):
    return OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence",
        max_concurrency=1,
        test_timeout_seconds=60,
        dynamic_graph=False,  # no Manager: a scripted run must not ask for unscripted turns
        deployment_policy=deployment,
    )


def _spec(key, *, criteria=("file:NOTES.md",), tools=TOOLS3):
    return MissionSpec(
        goal="写一份 NOTES.md，列出三个要点",
        success_criteria=tuple(criteria),
        tenant_id="tenant-host",
        idempotency_key=key,
        allowed_tools=tuple(tools),
        budget=Budget(max_tokens=300_000, max_attempts=6),
    )


def _task(key, policy, *, tools=TOOLS3, outputs=("NOTES.md",), attempts=2):
    return {
        "key": key,
        "goal": "写 NOTES.md",
        "rationale": "Mission 只有这一件工作",
        "dependencies": [],
        "success_criteria": ["file:NOTES.md"],
        "verification_policy": list(policy),
        "outputs": list(outputs),
        "allowed_tools": list(tools),
        "budget": {"max_tokens": 30_000, "max_attempts": attempts},
        "priority": 1.0,
    }


def _notes_worker(extra=(), artifacts=("NOTES.md",)):
    return [
        *extra,
        ("workspace_write_file", {"path": "NOTES.md", "content": "- 一\n- 二\n- 三\n"}),
        envelope_step(summary="写好了", artifacts=list(artifacts), claims=["NOTES.md 有三个要点"]),
    ]


PROBE = "import pathlib\npathlib.Path({marker!r}).write_text('ran', encoding='utf-8')\n"


def _probe_writes(marker):
    code = PROBE.format(marker=str(marker))
    return [
        ("workspace_write_file", {"path": "conftest.py", "content": code}),
        (
            "workspace_write_file",
            {"path": "test_probe.py", "content": code + "\n\ndef test_ok():\n    assert True\n"},
        ),
    ]


def _critics(n=4):
    return [critic_step(verdict="PASS", criteria_met=True) for _ in range(n)]


def _capturing(step, seen):
    def wrapped(request):
        seen.append(package_of(request))
        return step(request) if callable(step) else step

    return wrapped


# ------------------------------------------------------------------ SA-6
def test_the_default_deployment_is_unchanged():
    assert ON.local_code_execution is True
    assert deployed_layers(ON) == STEP2_IMPLEMENTED_LAYERS
    assert ON.to_json()["local_code_execution"] is True


def test_off_drops_code_test_and_refuses_run_tests_as_a_contradiction():
    assert deployed_layers(OFF) == STEP2_IMPLEMENTED_LAYERS - {"code_test"}
    assert OFF.to_json()["local_code_execution"] is False
    with pytest.raises(ValueError, match="run_tests"):
        DeploymentPolicy(allowed_tools=TOOLS4, local_code_execution=False)


# ------------------------------------------------------------------ SA-3
def test_templates_offer_only_the_deployed_layers_and_keep_the_old_versions():
    assert PLANNER.prompt_version == "planner-v4"
    assert MANAGER.prompt_version == "manager-v2"
    for template in (PLANNER, MANAGER):
        assert "deployed_verification_layers" in template.instructions
        assert "format_check / rule_check / critic_review / code_test" not in template.instructions
    # a library whose ACTIVE policy was seeded with the older prompts keeps working
    assert {"planner-v3", "planner-v4"} <= set(TEMPLATE_VERSIONS["planner"])
    assert {"manager-v1", "manager-v2"} <= set(TEMPLATE_VERSIONS["manager"])


def test_system_default_policies_follow_the_deployment():
    assert "code_test" not in default_change_policy(deployed_layers(OFF))
    assert default_change_policy(deployed_layers(ON)) == (
        "format_check",
        "rule_check",
        "code_test",
    )  # unchanged for every earlier deployment
    assert set(default_change_policy(deployed_layers(OFF))) <= deployed_layers(OFF)


# ------------------------------------------------------------------ SA-1
def test_a_planner_asking_for_code_test_is_refused_and_the_replan_completes(tmp_path, pytest_spy):
    seen: list[dict] = []
    provider = RoleScriptedProvider(
        {
            "planner": [
                _capturing(graph_proposal_step([_task("A", WITH_CODE)]), seen),
                _capturing(graph_proposal_step([_task("A", NO_CODE)]), seen),
            ],
            "worker": _notes_worker(),
            "critic": _critics(),
        }
    )

    async def run():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("sa1"))
            await orchestrator.run()
            store = orchestrator.store
            return (
                store.get_mission(mission.id),
                store.list_events(mission.id),
                store.list_tasks(mission.id),
            )

    mission, events, tasks = asyncio.run(run())
    assert str(mission.status) == "COMPLETED"
    rejected = [e for e in events if e.type == "TaskGraphRejected"]
    assert rejected
    assert "verification_policy_undeployed" in json.dumps(rejected[0].payload, ensure_ascii=False)
    assert tasks and all("code_test" not in t.verification_policy for t in tasks)
    # the Planner was told which layers exist here, and why the first graph failed
    assert len(seen) == 2
    assert all(p["deployed_verification_layers"] == sorted(deployed_layers(OFF)) for p in seen)
    assert "verification_policy_undeployed" in json.dumps(
        seen[1]["planning_rejected"], ensure_ascii=False
    )
    assert pytest_spy == []


# ------------------------------------------------------------------ SA-2
def test_model_written_test_files_never_run(tmp_path, pytest_spy):
    marker = tmp_path / "pytest-ran.marker"
    written = ("NOTES.md", "conftest.py", "test_probe.py")
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([_task("A", NO_CODE, outputs=written)])],
            "worker": _notes_worker(_probe_writes(marker), artifacts=written),
            "critic": _critics(),
        }
    )

    async def run():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("sa2"))
            await orchestrator.run()
            return orchestrator.store.get_mission(mission.id)

    mission = asyncio.run(run())
    assert str(mission.status) == "COMPLETED"
    assert not marker.exists(), "a model-written test file was executed on this machine"
    assert pytest_spy == []


# ------------------------------------------------------------------ before the switch
def _legacy_mission(tmp_path, *, criteria, policy, task=None):
    """Created and planned while local code execution was on (no model turn yet);
    ``task`` replaces the default Task contract."""

    async def phase1():
        async with Orchestrator(_config(tmp_path, ON), RoleScriptedProvider({})) as orchestrator:
            mission = await orchestrator.submit_mission(
                _spec("legacy", criteria=criteria, tools=TOOLS4)
            )
            planning = orchestrator.commit.begin_planning(mission.id)
            orchestrator.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [task or _task("A", policy, tools=TOOLS4)]}),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            return mission.id

    return asyncio.run(phase1())


def _resume_off(tmp_path, mission_id, provider):
    async def phase2():
        async with Orchestrator(_config(tmp_path, OFF), provider) as orchestrator:
            await orchestrator.run()
            store = orchestrator.store
            layers = [
                layer
                for task in store.list_tasks(mission_id)
                for attempt in store.list_attempts(task.id)
                for stored in [store.find_result_for_attempt(attempt.id)]
                if stored is not None
                for layer in store.list_verifications(stored.envelope.id)
            ]
            return store.get_mission(mission_id), layers, store.list_events(mission_id)

    return asyncio.run(phase2())


def test_a_code_test_task_from_before_the_switch_is_an_error_not_a_pass(tmp_path, pytest_spy):
    mission_id = _legacy_mission(tmp_path, criteria=("file:NOTES.md",), policy=WITH_CODE)
    run_tests_first = [("run_tests", {})]  # refused: the gateway never runs pytest either
    provider = RoleScriptedProvider(
        {
            "worker": _notes_worker(run_tests_first) + _notes_worker(run_tests_first),
            "critic": _critics(),
        }
    )
    mission, layers, _events = _resume_off(tmp_path, mission_id, provider)
    code_layers = [layer for layer in layers if layer["layer"] == "code_test"]
    assert code_layers and all(layer["status"] == "ERROR" for layer in code_layers)
    assert all((layer.get("detail") or {}).get("undeployed") is True for layer in code_layers)
    assert str(mission.status) == "FAILED"  # honest failure, not a hang and not a pass
    assert pytest_spy == []


def test_a_pytest_mission_criterion_from_before_the_switch_is_judged_unmet(tmp_path, pytest_spy):
    mission_id = _legacy_mission(
        tmp_path, criteria=("file:NOTES.md", "pytest:tests/test_x.py"), policy=NO_CODE
    )
    provider = RoleScriptedProvider({"worker": _notes_worker(), "critic": _critics()})
    mission, _layers, events = _resume_off(tmp_path, mission_id, provider)
    judged = [e for e in events if e.type == "MissionSuccessJudged"]
    assert judged
    verdicts = {j["criterion"]: j for j in judged[-1].payload["judgments"]}
    pytest_verdict = verdicts["pytest:tests/test_x.py"]
    assert pytest_verdict["met"] is False
    assert "local_code_execution" in pytest_verdict["reason"]
    assert verdicts["file:NOTES.md"]["met"] is True
    assert str(mission.status) == "FAILED" and mission.stop_reason == "mission_criteria_unmet"
    assert pytest_spy == []

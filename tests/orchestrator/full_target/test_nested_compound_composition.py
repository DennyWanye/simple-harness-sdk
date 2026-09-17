# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3l / defect N7: a non-root compound in composition_review must form a resolution.

Grok H-L4-M3-r0: ``code.fix-by-assessed-revert`` refined ``code.assess-regression``
with ``code.assess-by-reading``; both assess leaves were accepted; the assess
compound entered ``composition_review``; nothing formed a GoalResolution for it;
``revert`` stayed ``WAITING_ORDER``; the Mission died ``no_dispatchable_work``.

Only the root ``MISSION_FINAL`` review had a coordinator.  Inner compounds have
none.  These tests pin the missing path: two-level methods, last inner leaf
accepted, successor gated on ORDER — currently stalls, after the fix COMPLETED.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

from htn_world import method, param, step  # noqa: E402
from test_htn_end_to_end import (  # noqa: E402
    HIERARCHICAL_SEMANTICS,
    ROOT_DUTY,
    ROOT_TASK,
    World,
    _accept_leaf,
    build_world,
)
from test_nested_compound_refinement import _proposal  # noqa: E402

from agent_orchestrator.contracts import MissionStatus, TaskStatus  # noqa: E402
from agent_orchestrator.contracts.htn import TaskForm  # noqa: E402
from agent_orchestrator.contracts.state_machines import MissionStopReason  # noqa: E402
from agent_orchestrator.graph.eligibility import ReadinessReason  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    ROOT_REVIEW_CUT,
    CompoundPhase,
)
from agent_orchestrator.orchestrator.resolution_commits import (  # noqa: E402
    GOAL_RESOLUTION_COMMITTED,
)
from agent_orchestrator.orchestrator.state_machine import next_task  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    critic_step,
    envelope_step,
)


def _outer():
    """Root → compound assess, then a primitive revert gated on ORDER."""

    return method(
        "plan.assessed",
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=(
            step("assess", "plan.subgoal", TaskForm.COMPOUND, {"subject": param("subject")}),
            step(
                "revert",
                "plan.act",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
        ),
        ordering=(("assess", "revert"),),
        links=(("c-root", "assess", "c-sub"),),
        finalizer="revert",
    )


def _inner():
    return method(
        "plan.assess-by-reading",
        "plan.subgoal",
        parameter_schema="plan.subgoal.params",
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-sub", "leaf", "c-leaf-verified"),),
        finalizer="leaf",
    )


def _task_of(world: World, signature: str) -> str:
    for spec in world.network().occurrences:
        binding = world.network().binding_for_occurrence(spec.occurrence_id)
        if str(binding.goal_signature.signature_id) == signature:
            return str(spec.task_id)
    raise AssertionError(f"no occurrence for {signature}")


def _world(tmp_path, *, key: str) -> World:
    """Both refinements committed; the inner leaf accepted; revert still waiting."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = build_world(evidence, key=key, mode=HIERARCHICAL_SEMANTICS)
    env = world.env
    env.register_type(
        "plan.subgoal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-sub",),
        domain="plan",
    )
    env.register_type(
        "plan.act",
        parameters=(("subject", "string"),),
        outputs=(("verdict", "plan.verdict"),),
        capabilities=("plan.read",),
        criteria=("c-done",),
        domain="plan",
    )
    outer, inner = _outer(), _inner()
    for contract in (outer, inner):
        receipt = env.admit(contract)
        assert receipt.admitted, receipt.problems
        HtnStore(world.store).register_method(
            contract, env.registry.registration(contract.method_ref())
        )
    first = world.dispatch.apply_planner_reply(
        world.mission.id,
        _proposal(
            outer,
            goal_id=ROOT_TASK,
            obligation_id=ROOT_DUTY,
            revision=0,
            proposal_id="prop-outer",
        ),
        principal=world.principal,
        command_id="cmd-outer",
    )
    assert first.committed, first.last_reason
    world.dispatch.advance_compound_phases(world.mission.id)
    assess = _task_of(world, "plan.subgoal")
    network = world.network()
    assess_spec = next(spec for spec in network.occurrences if str(spec.task_id) == assess)
    second = world.dispatch.apply_planner_reply(
        world.mission.id,
        _proposal(
            inner,
            goal_id=assess,
            obligation_id=str(assess_spec.obligation_id),
            revision=int(network.plan_revision),
            proposal_id="prop-inner",
        ),
        principal=world.principal,
        command_id="cmd-inner",
    )
    assert second.committed, second.last_reason
    world.dispatch.advance_compound_phases(world.mission.id)
    world.admit_demand()
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    world.dispatch.issue_start_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    leaf = _task_of(world, "plan.leaf")
    _accept_leaf(world, task_id=leaf, now_ms=1_000_000)
    task = world.store.get_task(leaf)
    assert task is not None
    completed = next_task(
        next_task(next_task(task, TaskStatus.ACTIVE), TaskStatus.VERIFYING),
        TaskStatus.COMPLETED,
        accepted_result_id="result-1",
    )
    world.store.update_task(completed, expected_version=task.version)
    world.dispatch.advance_compound_phases(world.mission.id)
    return world


def _accepting_reviewer(request: Any) -> str:
    shown = json.loads(
        next(m.content for m in reversed(request.messages) if str(m.role).endswith("user"))
    )
    return (
        "<critic_verdict>"
        + json.dumps(
            {
                "verdict": "PASS",
                "findings": [],
                "mission_criteria": [
                    {"criterion": item["criterion_id"], "met": True, "reason": "scripted"}
                    for item in shown["criteria"]
                ],
            }
        )
        + "</critic_verdict>"
    )


def _revert_envelope(request: Any) -> str:
    return envelope_step(
        summary="reverted",
        artifacts=["a.md"],
        claims=["the successor ran"],
    )(request)


def _run(world: World, tmp_path, *, cycles: int = 80) -> dict[str, Any]:
    evidence = Path(tmp_path) / "evidence"
    world.store.close()
    provider = RoleScriptedProvider(
        {
            "worker": [
                ("workspace_write_file", {"path": "a.md", "content": "done\n"}),
                _revert_envelope,
                ("workspace_write_file", {"path": "a.md", "content": "done\n"}),
                _revert_envelope,
            ],
            "critic": [
                critic_step(verdict="PASS", criteria_met=True),
                critic_step(verdict="PASS", criteria_met=True),
            ],
            "root_reviewer": [_accepting_reviewer],
        }
    )

    async def case() -> dict[str, Any]:
        config = OrchestratorConfig(
            evidence_root=evidence, max_concurrency=2, test_timeout_seconds=30
        )
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            await asyncio.wait_for(loop.run(max_cycles=cycles), timeout=60)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            events = list(loop.store.list_events(world.mission.id))
            return {
                "status": mission.status,
                "stop_reason": mission.stop_reason,
                "report": dict(mission.final_report or {}),
                "types": [item.type for item in events],
                "resolutions": [
                    dict(item.payload)
                    for item in events
                    if item.type == GOAL_RESOLUTION_COMMITTED
                ],
                "roles": dict(provider.by_role),
                "progress": list(loop.progress_log),
                "readiness": (
                    None
                    if loop._hierarchical is None
                    else {
                        str(occ): str(report.reason)
                        for occ, report in loop._hierarchical.read(world.mission.id).reports.items()
                    }
                ),
            }

    return asyncio.run(case())


def test_the_fixture_really_parks_the_successor_on_waiting_order(tmp_path) -> None:
    """The defect shape, asserted rather than assumed: assess is composition_review,
    revert is WAITING_ORDER, nothing is dispatchable."""

    world = _world(tmp_path, key="p23l-n7-shape")
    view = world.dispatch.read(world.mission.id)
    assess = _task_of(world, "plan.subgoal")
    revert = _task_of(world, "plan.act")
    assess_occ = next(
        spec.occurrence_id for spec in view.network.occurrences if str(spec.task_id) == assess
    )
    revert_occ = next(
        spec.occurrence_id for spec in view.network.occurrences if str(spec.task_id) == revert
    )
    from agent_orchestrator.orchestrator.hierarchical_dispatch import next_compound_phase

    phase = next_compound_phase(
        view.network.occurrence(assess_occ),
        view.network,
        view.reports[assess_occ],
        child_outcomes={
            binding.occurrence_id: view.outcomes.get(binding.occurrence_id)
            for binding in view.network.adopted_children(assess_occ)
        },
        resolved=assess_occ in view.resolved,
    )
    assert phase is CompoundPhase.COMPOSITION_REVIEW, phase
    assert view.reports[revert_occ].reason is ReadinessReason.WAITING_ORDER, (
        view.reports[revert_occ]
    )
    assert assess_occ not in view.resolved
    world.store.close()


def test_a_two_level_plan_runs_to_completed_after_the_inner_compound_resolves(tmp_path) -> None:
    """True ``Orchestrator.run()``.  Before the fix this is ``no_dispatchable_work``.

    Path pinned: inner leaf already accepted (the M3-r0 moment) → inner compound
    composition resolution → ORDER successor (revert) dispatched → root
    MISSION_FINAL reviewer ACCEPT → COMPLETED.  The system does not fill PASS.
    """

    world = _world(tmp_path, key="p23l-n7-e2e")
    outcome = _run(world, tmp_path, cycles=400)
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report']} "
        f"types={outcome['types']} readiness={outcome['readiness']} "
        f"progress={outcome['progress'][-12:]}"
    )
    inner = [item for item in outcome["resolutions"] if item.get("is_mission_root") is not True]
    root = [item for item in outcome["resolutions"] if item.get("is_mission_root") is True]
    assert inner, f"the inner compound formed no GoalResolution: {outcome['resolutions']}"
    assert root, f"the root never resolved: {outcome['resolutions']}"
    assert outcome["roles"].get("worker") >= 1, (
        f"the ORDER successor was never dispatched: {outcome['roles']}"
    )
    assert outcome["roles"].get("root_reviewer") == 1, outcome["roles"]
    assert ROOT_REVIEW_CUT in outcome["types"], outcome["types"]
    assert "MissionFailed" not in outcome["types"]
    assert outcome["stop_reason"] != str(MissionStopReason.NO_DISPATCHABLE_WORK)

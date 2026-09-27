# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""NEXT-TG-1.0 第二批 B 验收补测（§7.3 第 1、4 条）。

复用 ``test_htn_end_to_end`` 的层次世界与 ``test_nested_compound_refinement`` 的
提案/内层方法，不新造夹具框架：

* 第 1 条：A/B→C，C 对 A 是非空 DATA、对 B 是纯 ORDER；前序完成后不额外请求主
  Planner，C 由原循环自动启动并跑到 COMPLETED。
* 第 4 条：C 等 DATA 时独立的 D 仍被派发；另一分支是待细化的复合目标（需要结构
  规划）时，D 同样不被阻断。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE / "fixtures" / "htn"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from htn_world import method, out, param, step  # noqa: E402
from test_htn_end_to_end import (  # noqa: E402
    HIERARCHICAL_SEMANTICS,
    ROOT_DUTY,
    ROOT_TASK,
    World,
    build_world,
)
from test_nested_compound_composition import _accepting_reviewer  # noqa: E402
from test_nested_compound_refinement import _inner_method, _proposal  # noqa: E402

from agent_orchestrator.contracts import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.htn import TaskForm  # noqa: E402
from agent_orchestrator.graph.eligibility import ReadinessReason  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    package_of,
)

#: 主编排再规划的全部入口留下的痕迹；FAST 路径上一个都不应出现。
PLANNING_TRACES = (
    "HierarchicalRefinementRequested",
    "PlanningServiceResumed",
    "PlanningRepairRequested",
    "MethodSynthesisRoundRecorded",
    "PlanningRejected",
)


def _plan_types(env) -> None:
    env.register_type(
        "plan.act",
        parameters=(("subject", "string"),),
        outputs=(("verdict", "plan.verdict"),),
        capabilities=("plan.read",),
        criteria=("c-done",),
        domain="plan",
    )


def _leaf(local_id: str, task_type: str, **arguments: Any):
    return step(
        local_id,
        task_type,
        TaskForm.PRIMITIVE,
        {"subject": param("subject"), **arguments},
        capabilities=("plan.read",),
    )


def _commit(world: World, contract, *, extra=()) -> None:
    env = world.env
    for item in (contract, *extra):
        receipt = env.admit(item)
        assert receipt.admitted, receipt.problems
        HtnStore(world.store).register_method(item, env.registry.registration(item.method_ref()))
    outcome = world.dispatch.apply_planner_reply(
        world.mission.id,
        _proposal(contract, goal_id=ROOT_TASK, obligation_id=ROOT_DUTY, revision=0,
                  proposal_id="prop-2b"),
        principal=world.principal,
        command_id="cmd-2b",
    )
    assert outcome.committed, outcome.last_reason
    world.dispatch.advance_compound_phases(world.mission.id)
    world.admit_demand()


def _by_type(world: World) -> dict[str, str]:
    network = world.network()
    return {
        str(network.binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id):
        str(spec.task_id)
        for spec in network.occurrences
    }


def _reasons(world_or_dispatch, mission_id: str) -> dict[str, ReadinessReason]:
    view = world_or_dispatch.read(mission_id)
    return {str(spec.task_id): view.reports[spec.occurrence_id].reason
            for spec in view.network.occurrences}


def _plan_intent_count(store, mission_id: str) -> int:
    """主 Planner / 方法合成的意图数（根审阅也用 kind='plan'，按角色排除）。"""

    rows = store.connection.execute(
        "SELECT subject_id, config_json FROM dispatch_intents WHERE mission_id=? AND kind='plan'",
        (mission_id,)).fetchall()
    return sum(1 for row in rows
               if ":planner:" in row["subject_id"]
               or json.loads(row["config_json"]).get("role") in {"planner", "method_synthesizer"})


# ======================================================================================
# §7.3 第 1 条：A/B→C（非空 DATA + 纯 ORDER）
# ======================================================================================


def _abc_world(tmp_path, *, key: str) -> World:
    """a = plan.leaf（产出 result）；b = plan.act（独立）；c = plan.review（DATA←a，ORDER←b）。"""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = build_world(evidence, key=key, mode=HIERARCHICAL_SEMANTICS)
    _plan_types(world.env)
    contract = method(
        "plan.abc",
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=(
            _leaf("a", "plan.leaf"),
            _leaf("b", "plan.act"),
            _leaf("c", "plan.review", result=out("a", "result")),
        ),
        ordering=(("b", "c"),),
        links=(("c-root", "c", "c-reviewed"),),
        finalizer="c",
    )
    _commit(world, contract)
    return world


#: 按任务目标给每个 Attempt 一份脚本：写一个文件，交回声明了端口的结果信封。
_LEAF_FILES = {
    "goal plan.leaf": ("result.json", {"result": "result.json"}),
    "goal plan.act": ("verdict.json", {}),
    "goal plan.review": ("a.md", {"verdict": "a.md"}),
}


class _ByGoalWorker:
    def __init__(self) -> None:
        self.queues: dict[str, list[Any]] = {}
        self.goals: list[str] = []

    def __call__(self, request: Any) -> Any:
        package = package_of(request)
        attempt_id = str(package["attempt"]["attempt_id"])
        if attempt_id not in self.queues:
            goal = str(package["task_contract"]["goal"])
            self.goals.append(goal)
            path, outputs = _LEAF_FILES[goal]
            self.queues[attempt_id] = [
                ("workspace_write_file", {"path": path, "content": "{}\n"}),
                envelope_step(summary="scripted", artifacts=[path], claims=["scripted"],
                              override=lambda body, o=outputs: {**body, "outputs": dict(o)}),
            ]
        item = self.queues[attempt_id].pop(0)
        return item(request) if callable(item) else item


def _run_abc(world: World, tmp_path) -> dict[str, Any]:
    evidence = Path(tmp_path) / "evidence"
    world.store.close()
    worker = _ByGoalWorker()
    provider = RoleScriptedProvider({
        "worker": [worker] * 12,
        "critic": [critic_step(verdict="PASS", criteria_met=True)] * 6,
        "root_reviewer": [_accepting_reviewer] * 2,
    })

    async def case() -> dict[str, Any]:
        config = OrchestratorConfig(evidence_root=evidence, max_concurrency=2, test_timeout_seconds=30)
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            await asyncio.wait_for(loop.run(max_cycles=400), timeout=60)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            events = list(loop.store.list_events(world.mission.id))
            return {
                "status": mission.status,
                "stop_reason": mission.stop_reason,
                "events": events,
                "types": [item.type for item in events],
                "roles": dict(provider.by_role),
                "plan_intents": _plan_intent_count(loop.store, world.mission.id),
                "attempts": {task.id: [a.id for a in loop.store.list_attempts(task.id)]
                             for task in loop.store.list_tasks(world.mission.id)},
                "goals": list(worker.goals),
            }

    return asyncio.run(case())


def test_abc_fixture_really_has_one_data_edge_and_one_pure_order_edge(tmp_path) -> None:
    """§7.3-1 前提：C 对 A 是非空 DATA、对 B 是纯 ORDER，开局 C 在等、A/B 就绪。"""

    world = _abc_world(tmp_path, key="tg2b-abc-shape")
    tasks = _by_type(world)
    network = world.network()
    consumer = network.binding_for_occurrence(
        next(s.occurrence_id for s in network.occurrences if str(s.task_id) == tasks["plan.review"]))
    assert [port.port_key for port in consumer.input_ports] == ["result"]  # 唯一 DATA 来自 a
    act = network.binding_for_occurrence(
        next(s.occurrence_id for s in network.occurrences if str(s.task_id) == tasks["plan.act"]))
    assert not act.output_ports or all(p.port_key != "result" for p in act.output_ports)
    reasons = _reasons(world.dispatch, world.mission.id)
    assert reasons[tasks["plan.leaf"]] is ReadinessReason.READY_CANDIDATE
    assert reasons[tasks["plan.act"]] is ReadinessReason.READY_CANDIDATE
    assert reasons[tasks["plan.review"]] in {ReadinessReason.WAITING_ORDER, ReadinessReason.WAITING_DATA}
    world.store.close()


def test_abc_runs_to_completed_without_asking_the_main_planner_again(tmp_path) -> None:
    """§7.3-1：A、B 完成后 C 由原循环自动启动，全程规划器意图数为 0、无再规划痕迹。"""

    world = _abc_world(tmp_path, key="tg2b-abc-run")
    tasks = _by_type(world)
    outcome = _run_abc(world, tmp_path)
    assert outcome["status"] is MissionStatus.COMPLETED, (
        outcome["status"], outcome["stop_reason"], outcome["types"][-30:])
    # 计划在循环之前已提交；FAST 推进不应再请求主 Planner 或方法合成。
    assert outcome["plan_intents"] == 0
    assert "planner" not in outcome["roles"] and "method_synthesizer" not in outcome["roles"], outcome["roles"]
    assert not [t for t in outcome["types"] if t in PLANNING_TRACES]
    assert outcome["types"].count("PlanRevisionCommitted") == 1  # 仍是开局那一版计划
    # C 只跑一次，并且是在 A 与 B 都正式完成之后才创建 Attempt。
    c_attempts = outcome["attempts"][tasks["plan.review"]]
    assert len(c_attempts) == 1
    seq = {}
    for event in outcome["events"]:
        if event.type == "TaskCompleted":
            seq.setdefault(("done", event.task_id), event.seq)
        if event.type == "AttemptCreated" and event.payload.get("attempt_id", event.attempt_id) == c_attempts[0]:
            seq.setdefault("c_created", event.seq)
    assert seq["c_created"] > seq[("done", tasks["plan.leaf"])]
    assert seq["c_created"] > seq[("done", tasks["plan.act"])]
    assert sorted(outcome["goals"]) == ["goal plan.act", "goal plan.leaf", "goal plan.review"]


# ======================================================================================
# §7.3 第 4 条：C 等数据 / C 需结构规划时，独立的 D 仍推进
# ======================================================================================


def _branch_world(tmp_path, *, key: str, with_compound: bool) -> World:
    """a → c（DATA），d 独立；``with_compound`` 再加一个未细化的复合目标 s。"""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = build_world(evidence, key=key, mode=HIERARCHICAL_SEMANTICS)
    _plan_types(world.env)
    steps = [
        _leaf("a", "plan.leaf"),
        _leaf("c", "plan.review", result=out("a", "result")),
        _leaf("d", "plan.act"),
    ]
    extra = ()
    if with_compound:
        world.env.register_type(
            "plan.subgoal",
            form=TaskForm.COMPOUND,
            parameters=(("subject", "string"),),
            criteria=("c-sub",),
            domain="plan",
        )
        steps.append(step("s", "plan.subgoal", TaskForm.COMPOUND, {"subject": param("subject")}))
        extra = (_inner_method(),)
    contract = method(
        "plan.branches" + (".with-subgoal" if with_compound else ""),
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=tuple(steps),
        links=(("c-root", "c", "c-reviewed"),),
        finalizer="c",
    )
    _commit(world, contract, extra=extra)
    world.store.close()
    return world


def _open_loop(world: World, tmp_path, body):
    config = OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=3, test_timeout_seconds=5)

    async def case():
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            return await body(loop, loop.store.get_mission(world.mission.id))

    return asyncio.run(case())


def test_c_waiting_for_data_does_not_hold_back_the_independent_d(tmp_path) -> None:
    """§7.3-4 前半：C 等 A 的数据（WAIT）时，独立就绪的 D 在同一轮被派发。"""

    world = _branch_world(tmp_path, key="tg2b-branch-data", with_compound=False)

    async def body(loop, mission):
        tasks = _by_type_loop(loop, mission.id)
        before = _reasons(loop.hierarchical, mission.id)
        assert before[tasks["plan.review"]] is ReadinessReason.WAITING_DATA
        assert before[tasks["plan.act"]] is ReadinessReason.READY_CANDIDATE
        assert await loop._decide(mission) is True
        attempts = {t: loop.store.list_attempts(tid) for t, tid in tasks.items()}
        assert len(attempts["plan.act"]) == 1, "独立的 D 必须推进"
        assert attempts["plan.review"] == [], "C 在等数据，不能提前派发"
        assert _plan_intent_count(loop.store, mission.id) == 0
        # 没有实质变化的第二次判定不重复派发 D，也不请求规划。
        await loop._decide(loop.store.get_mission(mission.id))
        assert len(loop.store.list_attempts(tasks["plan.act"])) == 1
        assert loop.store.list_attempts(tasks["plan.review"]) == []
        assert _plan_intent_count(loop.store, mission.id) == 0

    _open_loop(world, tmp_path, body)


def test_a_branch_that_needs_structural_planning_does_not_block_the_independent_d(tmp_path) -> None:
    """§7.3-4 后半：S 分支待细化、规划轮在途时，独立的 D 仍被派发，S 自身不得起 Worker。"""

    world = _branch_world(tmp_path, key="tg2b-branch-slow", with_compound=True)

    async def body(loop, mission):
        tasks = _by_type_loop(loop, mission.id)
        assert await loop._refine_open_compounds(mission) is True  # SLOW：请求原细化入口
        in_flight = [i for i in loop.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED")
                     if i.mission_id == mission.id and i.kind == "plan"]
        assert len(in_flight) == 1
        assert await loop._decide(loop.store.get_mission(mission.id)) is True
        assert len(loop.store.list_attempts(tasks["plan.act"])) == 1, "S 的结构规划不应阻断 D"
        assert len(loop.store.list_attempts(tasks["plan.leaf"])) == 1
        assert loop.store.list_attempts(tasks["plan.subgoal"]) == []  # 复合目标不起 Worker
        assert loop.store.list_attempts(tasks["plan.review"]) == []
        # 同一修订上再问一次不产生第二个规划轮。
        assert await loop._refine_open_compounds(loop.store.get_mission(mission.id)) is False
        assert _plan_intent_count(loop.store, mission.id) == 1

    _open_loop(world, tmp_path, body)


def _by_type_loop(loop, mission_id: str) -> dict[str, str]:
    network = loop.hierarchical.network(mission_id)
    return {
        str(network.binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id):
        str(spec.task_id)
        for spec in network.occurrences
    }

# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P33-09 闸门 2/3/4/5：先固定 oracle，再实现真实 commit 路径测试。

Oracle（测试实现前写定）：
1. 闸门 2：合法文档图提交后，Manager 图变更仅把新增节点准则换成 pytest:；
   commit_graph_change 必须因领域拒绝，原图、版本、预算、receipt 不变，
   只允许追加 TaskGraphChangeRejected。恢复文档准则后同一入口必须提交成功。
2. 闸门 3：通过 create_mission / begin_planning 建立合法空图，单 Task 提案
   仅加入 pytest: 准则；commit_task_proposal 必须经 _check_task_proposal 拒绝，
   不创建 Task、不激活 Mission、不留下预算账户或 receipt；合法对照可成功。
3. 闸门 4：通过两个合法文档 Task 的 Attempt / result / accept 入口建立同 key
   反 stance 的冲突前置，只污染系统冲突模板的准则为 pytest:，不替换 checker；
   accept_result 必须在 conflict insert 闸门拒绝，整个 accept 事务回滚：
   不创建冲突记录或 Task、不消耗 reserve、不落 DISPUTED/PASS/COMPLETED 假状态。
   恢复模板后重试同一个结果必须成功创建冲突 Task，证明前置与 reserve 均合法。
4. 闸门 5：合法 Planner 图配合法综合模板，仅在系统生成综合 Task 时注入
   pytest: 准则；commit_task_graph 必须在 synthesis insert 闸门拒绝，
   已在同一事务写入的普通 Task、预算账户、事件、receipt 一起回滚。
   恢复模板后同一个图提交成功且包含综合 Task，证明没有误中整图闸门。

四条都断言 domain + doc-research-v1 + pytest 的明确错误，不以任意异常当通过；
全部前置状态由公共 commit 入口产生，不直接写数据库，不单独调用共同 checker。
这里只验证 commit 边界；PASS 层是测试输入，不宣称真实 verifier / Provider 已运行。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_orchestrator.contracts import (
    Artifact,
    AttemptStatus,
    Budget,
    ClaimProposal,
    ClaimStatus,
    MissionStatus,
    ResultEnvelope,
    TaskStatus,
    ids,
)
from agent_orchestrator.governance.domains import DOC_DOMAIN, DOC_PROFILE
from agent_orchestrator.graph.changes import TaskGraphChange
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator import commit_service as commit_module
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    MissionSpec,
    Reservation,
    TaskProposal,
)
from agent_orchestrator.storage.store import Store

PYTEST_CRITERION = "pytest:tests/test_unrelated.py"
DOC_POLICY = ("format_check", "rule_check", "critic_review")
DOC_PASSES = tuple({"layer": layer, "status": "PASS", "detail": {}} for layer in DOC_POLICY)


@pytest.fixture
def service(tmp_path):
    # 固定时钟避免测试机暂停导致租约过期；文档部署包含人工裁决，不部署 code_test。
    store = Store.open(tmp_path / "orchestrator.db", clock=lambda: 1_000.0)
    try:
        yield CommitService(store, deployed_layers=frozenset(DOC_PROFILE.runs_layers))
    finally:
        store.close()


def _planning(service, **overrides):
    mission, _ = service.create_mission(
        MissionSpec(
            goal="比较资料并形成报告",
            success_criteria=("file:REPORT.md",),
            tenant_id="p33-remaining-gates",
            idempotency_key="doc-mission",
            domain=DOC_DOMAIN,
            budget=Budget(max_tokens=100_000, max_attempts=12),
            **overrides,
        )
    )
    planning = service.begin_planning(mission.id)
    assert planning.status is MissionStatus.PLANNING
    assert service.store.get_mission_domain(mission.id)["domain_id"] == DOC_DOMAIN
    return planning


def _node(key, dependencies=()):
    return {
        "key": key,
        "goal": f"核对资料 {key}",
        "rationale": "为资料比较报告提供依据",
        "dependencies": list(dependencies),
        "success_criteria": [f"file:notes/{key}.md"],
        "verification_policy": list(DOC_POLICY),
        "allowed_tools": [],
        "budget": {"max_tokens": 20_000, "max_attempts": 3},
        "outputs": [f"notes/{key}.md"],
    }


def _graph(service, planning, *nodes):
    return service.commit_task_graph(
        planning.id,
        TaskGraphProposal.from_json({"tasks": list(nodes)}),
        base_version=planning.version,
        source={"planner": "p33-09"},
    )


def _state(service, mission_id):
    """正式快照加预算和 receipt：只读，不靠直接造库绕开公共入口。"""
    snapshot = service.store.snapshot(mission_id)
    snapshot.pop("event_count")  # 拒绝审计事件单独断言，不能掩盖成功事件泄漏。
    with service.store.transaction() as connection:
        for table, key in (
            ("budget_accounts", "account_id"),
            ("budget_reservations", "reservation_id"),
            ("imported_usage", "usage_ref"),
            ("commit_receipts", "commit_id"),
        ):
            # 表名全为本测试常量；每个测试拥有独立 tmp_path 数据库。
            snapshot[table] = [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {key}")
            ]
    return snapshot


def _assert_domain_rejection(error, location):
    message = str(error.value)
    for expected in ("domain", DOC_DOMAIN, PYTEST_CRITERION, location):
        assert expected in message, message
    assert "verification_policy_undeployed" not in message


def test_p33_09_gate2_graph_change_rejects_pytest_without_committing_state(service):
    planning = _planning(service)
    tasks, _ = _graph(service, planning, _node("A"))
    mission = service.store.get_mission(planning.id)
    assert mission.status is MissionStatus.ACTIVE
    valid_node = {
        "op": "add_task",
        **_node("B", dependencies=(tasks[0].id,)),
        "parent_task_ids": [tasks[0].id],  # 明确细化已存在的 Task，避免 goal_drift。
    }
    valid = {
        "base_graph_version": mission.final_report["graph_version"],
        "basis": {"trigger": "manager_review"},
        "rationale": "补充对报告所需资料的核对",
        "operations": [valid_node],
    }
    poisoned = TaskGraphChange.from_json(
        {**valid, "operations": [{**valid_node, "success_criteria": [PYTEST_CRITERION]}]}
    )
    before = _state(service, mission.id)
    events_before = service.store.iter_events(mission.id)

    with pytest.raises(CommitRejected) as error:
        service.commit_graph_change(mission.id, poisoned, source={"manager": "p33-09"})

    _assert_domain_rejection(error, "graph change rejected (domain)")
    assert _state(service, mission.id) == before
    events_after = service.store.iter_events(mission.id)
    assert events_after[:-1] == events_before
    assert events_after[-1].type == "TaskGraphChangeRejected"
    assert events_after[-1].payload["reason"] == "domain"
    assert PYTEST_CRITERION in events_after[-1].payload["detail"]

    created, receipt = service.commit_graph_change(
        mission.id, TaskGraphChange.from_json(valid), source={"manager": "p33-09"}
    )
    assert len(created) == 1 and created[0].success_criteria == ("file:notes/B.md",)
    assert created[0].dependency_ids == (tasks[0].id,)
    assert receipt["from_version"] == 1 and receipt["to_version"] == 2
    assert service.store.get_receipt(receipt["change_id"]) == receipt


def test_p33_09_gate3_single_task_proposal_rejects_pytest_without_activating_mission(service):
    planning = _planning(service)
    valid = TaskProposal(
        goal="核对报告依据",
        rationale="为资料比较报告提供依据",
        success_criteria=("file:notes/A.md",),
        verification_policy=DOC_POLICY,
        allowed_tools=(),
        budget=Budget(max_tokens=20_000, max_attempts=3),
    )
    before = _state(service, planning.id)
    events_before = service.store.iter_events(planning.id)

    with pytest.raises(CommitRejected) as error:
        service.commit_task_proposal(
            planning.id,
            replace(valid, success_criteria=(PYTEST_CRITERION,)),
            base_version=planning.version,
            source={"planner": "p33-09"},
        )

    _assert_domain_rejection(error, "task proposal")
    assert _state(service, planning.id) == before
    assert service.store.iter_events(planning.id) == events_before
    assert service.store.list_tasks(planning.id) == []
    assert service.store.get_mission(planning.id).status is MissionStatus.PLANNING

    task, receipt = service.commit_task_proposal(
        planning.id, valid, base_version=planning.version, source={"planner": "p33-09"}
    )
    assert task.status is TaskStatus.READY and task.success_criteria == valid.success_criteria
    assert service.store.get_mission(planning.id).status is MissionStatus.ACTIVE
    assert service.store.get_receipt(receipt["commit_id"]) == receipt


def _document_result(service, task, *, stance):
    """真实状态迁移到 VERIFYING；验证层结果由调用方作为 commit 输入提供。"""
    attempt, intent = service.create_attempt(
        task.id,
        role="worker",
        model="fixture-model",
        prompt_version="worker-v2",
        context_version="p33-09",
        reservation=Reservation(tokens=4_000, cost_micros=0),
        intent_config={"agent_config": {}, "message": "核对资料"},
        input_hash="p33-09",
    )
    turn = f"turn:{attempt.id}"
    agent = f"agent:{attempt.id}"
    service.claim_intent(intent.intent_id, owner="p33-09", lease_seconds=60)
    service.record_agent_created(intent.intent_id, agent_id=agent, expected_turn_id=turn)
    service.record_submitted(intent.intent_id, receipt={"turn_id": turn, "seq": 1})
    path = task.outputs[0]
    content_hash = "a" * 64
    artifact = Artifact(
        id=ids.artifact_id(attempt.id, path, content_hash),
        mission_id=task.mission_id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=path,
        version=1,
        content_hash=content_hash,
        size_bytes=10,
        produced_by=agent,
    )
    envelope = ResultEnvelope(
        id=f"result:{attempt.id}",
        mission_id=task.mission_id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="资料核对候选结果",
        artifacts=(path,),
        claims=(
            ClaimProposal(
                content="资料支持方案 A" if stance == "affirms" else "资料不支持方案 A",
                confidence=0.8,
                key="document.option_a",
                stance=stance,
                evidence=(f"artifact:{path}",),
            ),
        ),
        evidence=(f"artifact:{path}",),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    service.record_result(
        attempt.id, envelope=envelope, turn_id=turn, artifacts=(artifact,), usage_refs=()
    )
    return service.start_verification(envelope.id)


def test_p33_09_gate4_conflict_insert_rejects_pytest_and_rolls_back_accept(service, monkeypatch):
    planning = _planning(service, conflict_reserve_tokens=20_000)
    (task_a, task_b), _ = _graph(service, planning, _node("A"), _node("B"))
    first = _document_result(service, task_a, stance="affirms")
    assert service.accept_result(first.envelope.id, verifier_results=DOC_PASSES).status is (
        TaskStatus.COMPLETED
    )
    first_claim = service.store.list_claims(first.envelope.id)[0]
    assert first_claim.status is ClaimStatus.SUPPORTED  # 合法 artifact 证据，无 pytest。
    second = _document_result(service, task_b, stance="refutes")
    assert second.verification_state == "RUNNING" and second.verdict is None
    assert service.store.list_conflicts(planning.id) == []
    before = _state(service, planning.id)
    events_before = service.store.iter_events(planning.id)
    original_factory = commit_module.conflict_task
    generated = []

    def poisoned_factory(*args, **kwargs):
        task = original_factory(*args, **kwargs)
        generated.append(task)
        return replace(task, success_criteria=(*task.success_criteria, PYTEST_CRITERION))

    with monkeypatch.context() as patch:
        patch.setattr(commit_module, "conflict_task", poisoned_factory)
        with pytest.raises(CommitRejected) as error:
            service.accept_result(second.envelope.id, verifier_results=DOC_PASSES)

    _assert_domain_rejection(error, "system template conflict")
    assert len(generated) == 1  # 真正走到系统模板，不能提前 DEFERRED 或在其他校验失败。
    assert generated[0].kind == "conflict"
    assert generated[0].verification_policy == DOC_PROFILE.conflict_template.policy
    assert _state(service, planning.id) == before
    assert service.store.iter_events(planning.id) == events_before
    assert service.store.get_claim(first_claim.id) == first_claim
    assert service.store.get_attempt(second.envelope.attempt_id).status is AttemptStatus.VERIFYING
    assert service.store.get_task(task_b.id).status is TaskStatus.VERIFYING
    assert service.store.get_result(second.envelope.id) == second
    assert service.store.list_conflicts(planning.id) == []

    completed = service.accept_result(second.envelope.id, verifier_results=DOC_PASSES)
    assert completed.status is TaskStatus.COMPLETED
    conflicts = service.store.list_conflicts(planning.id)
    assert len(conflicts) == 1 and conflicts[0]["state"] == "OPEN"
    conflict_task = service.store.get_task(conflicts[0]["task_id"])
    assert conflict_task.id == generated[0].id and conflict_task.kind == "conflict"
    assert conflict_task.status is TaskStatus.READY
    assert "human_review" in conflict_task.verification_policy
    assert not any(c.startswith("pytest:") for c in conflict_task.success_criteria)
    assert {c.status for c in service.store.list_mission_claims(planning.id)} == {
        ClaimStatus.DISPUTED
    }
    assert service.store.get_mission(planning.id).final_report["conflict_reserve_remaining"] == 0


def test_p33_09_gate5_synthesis_insert_rejects_pytest_and_rolls_back_entire_graph(
    service, monkeypatch
):
    planning = _planning(
        service,
        synthesis={
            "goal": "汇总资料比较报告",
            "success_criteria": ["file:REPORT.md"],
            "budget": {"max_tokens": 20_000, "max_attempts": 3},
            "outputs": ["REPORT.md"],
        },
    )
    before = _state(service, planning.id)
    events_before = service.store.iter_events(planning.id)
    original_factory = commit_module.synthesis_task
    generated = []
    planner_tasks_seen = []

    def poisoned_factory(*args, **kwargs):
        task = original_factory(*args, **kwargs)
        generated.append(task)
        # 此刻合法 Planner Task 已在事务内写入；回滚必须连它与账户/事件一起撤销。
        planner_tasks_seen.extend(service.store.list_tasks(planning.id))
        return replace(task, success_criteria=(*task.success_criteria, PYTEST_CRITERION))

    with monkeypatch.context() as patch:
        patch.setattr(commit_module, "synthesis_task", poisoned_factory)
        with pytest.raises(CommitRejected) as error:
            _graph(service, planning, _node("A"))

    _assert_domain_rejection(error, "system template synthesis")
    assert len(generated) == 1 and generated[0].kind == "synthesis"
    assert generated[0].verification_policy == DOC_PROFILE.synthesis_default_policy
    assert len(planner_tasks_seen) == 1 and planner_tasks_seen[0].kind == "work"
    assert generated[0].dependency_ids == (planner_tasks_seen[0].id,)
    assert _state(service, planning.id) == before
    assert service.store.iter_events(planning.id) == events_before
    assert service.store.list_tasks(planning.id) == []
    assert service.store.get_mission(planning.id).status is MissionStatus.PLANNING

    tasks, receipt = _graph(service, planning, _node("A"))
    assert [task.kind for task in tasks] == ["work", "synthesis"]
    assert tasks[-1].id == generated[0].id
    assert tasks[-1].success_criteria == ("file:REPORT.md",)
    assert tasks[-1].dependency_ids == (tasks[0].id,)
    assert receipt["terminal_task_id"] == tasks[-1].id
    assert service.store.get_mission(planning.id).status is MissionStatus.ACTIVE
    assert service.store.get_receipt(receipt["commit_id"]) == receipt

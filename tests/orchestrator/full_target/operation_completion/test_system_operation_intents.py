"""2026-09-29（plans/2026-09-28-system-operations）：申请单由系统按已批准效果准备。

真机第四、五局：模型写申请单反复出错，把任务 12 次尝试耗光。现在内容步骤只写文件；内容
全部通过后，系统用确认页批准的"发布什么、发到哪里"和审过的那份真实文件生成申请单，以确认人
的身份提交；之后照旧：审阅 → 物化 → 等人批准。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
for path in (_HERE, _HERE.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from operation_runtime_fixture import _passing_critic, _review_dispatch
from test_completion_plan_commit import _admitted_single_root
import test_completion_spec_approval as approval_fixture
from test_completion_spec_approval import _api, _approval_world, _command

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope
from agent_orchestrator.contracts.operation_intents import (
    OperationIntentSourceKind,
    OperationIntentSourceV2,
)
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.operation_materialization import OperationMaterializationRuntime
from agent_orchestrator.orchestrator.operation_proposal_review import ActionProposalReviewCoordinator
from agent_orchestrator.orchestrator.system_operations import (
    pending_system_operations,
    prepare_system_operations,
)
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from agent_orchestrator.runtime.operation_profiles import BuiltinOperationProfiles
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.operation_intent_store import OperationIntentStore

TARGET = "reports/final.json"
CONTENT = b'{"total": 42}\n'


def _world(tmp_path: Path, *, produce: str | None = "final.json") -> dict[str, Any]:
    published = tmp_path / "published"
    published.mkdir()
    publish = FilePublishConnector(published, tmp_path / "publish-ledger")
    connectors = {"file_publish": publish}
    deployment = DeploymentPolicy(enabled_connectors=("file_publish",))
    profiles = BuiltinOperationProfiles(connectors)
    # 桌面端的要求原文就是 "action:<连接器>.<操作>:<目标>"（夹具的原文是泛泛的描述）
    original_criterion = approval_fixture._criterion

    def criterion(identifier):
        value = original_criterion(identifier)
        if identifier == "criterion-delivered":
            value = dataclasses.replace(value, statement="action:file_publish.publish:" + TARGET)
        return value

    approval_fixture._criterion = criterion
    try:
        world, requirements = _approval_world(tmp_path)
    finally:
        approval_fixture._criterion = original_criterion
    original = world.mission
    world.mission = dataclasses.replace(
        original, success_criteria=(*original.success_criteria, "action:file_publish.publish:" + TARGET),
        version=original.version + 1)
    world.store.update_mission(world.mission, expected_version=original.version)
    world.service.begin_planning(world.mission.id)
    completion = _command(requirements, milestone="CONTENT_HASH_VERIFIED")
    completion["proposal"]["effects"][0]["milestone_policy_ref"] = profiles.milestone_policy_ref.to_json()
    completion["proposal"]["effects"][0]["evidence_policy_ref"] = profiles.evidence_policy_ref.to_json()
    approved = _api(world).approve(completion)
    plan, _ = _admitted_single_root(world, requirements, outputs=(("report", "report.schema"),))
    occurrence = str(plan.network.root_occurrence_ids[0])
    task_id = world.store.connection.execute(
        "SELECT task_id FROM plan_memberships WHERE mission_id=? AND revision=1 AND occurrence_id=?",
        (world.mission.id, occurrence)).fetchone()["task_id"]
    task = world.store.get_task(task_id)
    attempt, worker = world.service.create_attempt(
        task.id, role="worker", model="fixture-worker", prompt_version="fixture-worker-v1",
        context_version="operation-runtime-v1", reservation=Reservation(1_000, 0),
        intent_config={"message": "write the report"}, input_hash="a" * 64)
    world.service.claim_intent(worker.intent_id, owner="fixture", lease_seconds=60)
    world.service.record_agent_created(worker.intent_id, agent_id="fixture-worker", expected_turn_id="worker-turn")
    world.service.record_submitted(worker.intent_id, receipt={"turn_id": "worker-turn", "seq": 1})
    cas = world.service._source_artifact_store
    assert isinstance(cas, ArtifactStore)
    path = produce or "notes.txt"  # 没写出要发布的文件时，交的是别的文件
    digest = cas.put_bytes(CONTENT)
    artifact = Artifact(
        id="artifact-report", mission_id=world.mission.id, task_id=task.id, attempt_id=attempt.id,
        type="file", path=path, version=1, content_hash=hashlib.sha256(CONTENT).hexdigest(),
        size_bytes=len(CONTENT), produced_by="fixture-worker", storage_uri=str(cas.path_for(digest)))
    result = ResultEnvelope(
        id="result-report", mission_id=world.mission.id, task_id=task.id, attempt_id=attempt.id,
        outcome="candidate", summary="Wrote the report.",
        claims=(ClaimProposal(content="report written", confidence=0.9),), evidence=(),
        artifacts=(artifact.path,), proposed_tasks=(), used_knowledge=(), risks=(), cost={})
    stored = world.service.record_result(
        attempt.id, envelope=result, turn_id="worker-turn", artifacts=(artifact,), usage_refs=(),
        port_claims=(PortClaim(port_key="report", path=artifact.path),))
    world.service.start_verification(stored.envelope.id)
    for layer in ("schema_check", "rule_check", "critic_review"):
        world.service.record_verification_layer(
            stored.envelope.id, layer=layer, status="PASS", detail={"producer": "CommitService"})
    world.service.accept_result(stored.envelope.id, verifier_results=())

    captured: dict[str, Any] = {}

    def prepare_review(sources, payloads, package_id):
        coordinator = ActionProposalReviewCoordinator(
            world.store, connectors, deployment, profiles, profiles.policy_for(sources))
        captured["coordinator"] = coordinator
        captured["draft"] = coordinator.prepare_review(sources, payloads, package_id=package_id)
        return captured["draft"]

    runtime = OperationMaterializationRuntime(
        connectors, deployment, profiles, profiles.policy_for, prepare_review, object())
    world.service.bind_operation_materialization_runtime(runtime)
    notes: list[str] = []
    stops: list[dict[str, Any]] = []
    orch = SimpleNamespace(
        store=world.store, commit=world.service, _note=notes.append,
        _connectors=connectors, _config=SimpleNamespace(deployment_policy=deployment),
        _commit_fail_mission=lambda mission_id, **kw: stops.append(kw), _new_mode=lambda mission: None)
    return {"world": world, "orch": orch, "notes": notes, "stops": stops, "artifact": artifact,
            "approved": approved, "captured": captured, "runtime": runtime}


def test_the_system_prepares_the_approved_publish_from_the_reviewed_file(tmp_path):
    case = _world(tmp_path)
    world, orch, artifact = case["world"], case["orch"], case["artifact"]

    assert prepare_system_operations(orch, world.mission.id) is True, case["notes"]
    rows = OperationIntentStore(world.store).for_mission(world.mission.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["source_kind"] == "AUTHORIZED_SLOT"
    assert row["principal_id"] == "human-confirming"  # 以确认页批准人的身份提交
    assert row["candidate_artifact_id"] == artifact.id  # 候选引用是那份审过的真实文件
    # 再跑一轮不会重复提交
    assert prepare_system_operations(orch, world.mission.id) is False
    assert len(OperationIntentStore(world.store).for_mission(world.mission.id)) == 1
    assert case["notes"] == []  # 不是"提交被拒"，而是根本没再提交

    # 之后照旧：审阅 → 物化 → 等人批准；发布参数绑定到审过文件的真实哈希
    draft = case["captured"]["draft"]
    dispatch = _review_dispatch(world, subject="operation-review:" + row["intent_id"],
                                package_id=str(draft.package.package_id), intent_id=row["intent_id"])
    with world.store.transaction():
        review = case["captured"]["coordinator"].record_critic_verdict(
            draft, record_id="operation-review-record:" + row["intent_id"],
            dispatch_intent_id=dispatch.intent_id, reviewer_agent_id="operation-proposal-reviewer-agent",
            reviewer_turn_id="operation-proposal-reviewer-turn",
            raw_critic_text=_passing_critic(c.criterion_id for c in draft.package.criteria))
    materialized = world.service.materialize_reviewed_operation(
        intent_id=row["intent_id"], command_id="materialize:" + row["intent_id"],
        official_review_ref=TypedRef(TypedRefKind.REVIEW, str(review.record.record_id), 1,
                                     content_hash_of(review.record.to_json())),
        service_authority=case["runtime"].service_authority)
    action = world.store.get_action(materialized["action_key"])
    assert action["state"] == "AWAITING_APPROVAL"  # 人仍要逐个批准
    assert action["target"] == TARGET
    params = json.loads(action["params_json"]) if "params_json" in action else action["params"]
    assert params["artifact_path"] == "final.json"
    assert params["content_hash"] == artifact.content_hash
    # 理由是系统按已批准效果写的：审批卡片不再标"模型生成，未核实"
    assert action["reason_source"] == "system"
    approval = world.store.get_approval(action["approval_request_id"])
    assert approval["summary"]["reason_source"] == "system"


def test_a_missing_file_asks_the_planner_or_stops_visibly(tmp_path):
    case = _world(tmp_path, produce=None)
    plans = pending_system_operations(case["orch"], case["world"].mission.id)
    assert [(plan.get("ask"), plan.get("matches")) for plan in plans] == [(True, [])]
    # 本夹具没有分层装配，无法请规划器补步骤：停下并写明原因，不默默卡住
    assert prepare_system_operations(case["orch"], case["world"].mission.id) is True
    assert OperationIntentStore(case["world"].store).for_mission(case["world"].mission.id) == ()
    [stop] = case["stops"]
    assert stop["detail"]["reason"] == "system_operation_blocked"
    assert "没有任何步骤产出 " + TARGET in stop["detail"]["explanation"]


class _Status:
    """审阅结论由测试指定（真实审阅在上一个测试里走过）。"""

    def __init__(self, service, state):
        self._service, self.state = service, state

    def operation_intent_status(self, intent_id, **kw):
        return {**self._service.operation_intent_status(intent_id, **kw),
                "state": self.state, "materialization": None}

    def __getattr__(self, name):
        return getattr(self._service, name)


def test_a_review_that_could_not_decide_is_resubmitted_twice_then_stops(tmp_path):
    """审阅 2026-09-29：审阅没做成/判断不了后没人再提交，任务卡死。"""
    case = _world(tmp_path)
    world, orch = case["world"], case["orch"]
    assert prepare_system_operations(orch, world.mission.id) is True
    orch.commit = _Status(world.service, "INCONCLUSIVE")
    for count in (2, 3):
        assert prepare_system_operations(orch, world.mission.id) is True, case["notes"]
        rows = OperationIntentStore(world.store).for_mission(world.mission.id)
        assert len(rows) == count
        assert sum(1 for row in rows if row["supersedes_intent_id"]) == count - 1  # 替代重交
    assert prepare_system_operations(orch, world.mission.id) is True
    assert len(OperationIntentStore(world.store).for_mission(world.mission.id)) == 3
    assert "没能完成" in case["stops"][0]["detail"]["explanation"]


def test_a_reviewer_refusal_stops_for_a_person_to_judge(tmp_path):
    case = _world(tmp_path)
    world, orch = case["world"], case["orch"]
    assert prepare_system_operations(orch, world.mission.id) is True
    orch.commit = _Status(world.service, "REJECTED")
    assert prepare_system_operations(orch, world.mission.id) is True
    assert len(OperationIntentStore(world.store).for_mission(world.mission.id)) == 1
    assert case["stops"][0]["detail"]["verdict"] == "REJECTED"


def test_an_operation_that_needs_no_approval_is_never_prepared_by_the_system(tmp_path):
    case = _world(tmp_path)
    case["orch"]._connectors = {}  # 没有部署这个连接器：不需要（也无法）人批准，系统不代办
    plans = pending_system_operations(case["orch"], case["world"].mission.id)
    assert [plan.get("skip") for plan in plans] == ["operation_needs_no_approval"]
    assert prepare_system_operations(case["orch"], case["world"].mission.id) is False


def test_a_continued_file_is_taken_from_the_most_downstream_step():
    """审阅 2026-09-29：续写同一文件的两步都算"当前"，被判成多个匹配而卡住。上下游从计划
    网络的数据连线读——分层任务的 Task.dependency_ids 故意留空（第二轮审阅）。"""
    from agent_orchestrator.contracts.htn import TaskForm
    from agent_orchestrator.orchestrator.system_operations import _leaves, _sources

    def artifact(ident, path):
        return SimpleNamespace(id=ident, path=path, content_hash=ident * 2, verification_status="VERIFIED")

    first, second, other = artifact("a1", "README.md"), artifact("a2", "README.md"), artifact("a3", "notes.md")
    tasks = {"t1": SimpleNamespace(id="t1", dependency_ids=(), accepted_result_id="r1", outputs=()),
             "t2": SimpleNamespace(id="t2", dependency_ids=(), accepted_result_id="r2", outputs=())}
    results = {"r1": SimpleNamespace(artifacts=("a1", "a3")), "r2": SimpleNamespace(artifacts=("a2",))}
    artifacts = {a.id: a for a in (first, second, other)}
    store = SimpleNamespace(get_task=tasks.get, get_result=results.get, get_artifact=artifacts.get)
    acceptance = lambda *items: SimpleNamespace(  # noqa: E731
        validity="CURRENT", artifact_refs=tuple(SimpleNamespace(id=a.id, content_hash=a.content_hash) for a in items))
    members = (SimpleNamespace(occurrence_id="o1", task_id="t1", form=TaskForm.PRIMITIVE),
               SimpleNamespace(occurrence_id="o2", task_id="t2", form=TaskForm.PRIMITIVE))
    edges = [SimpleNamespace(producer_occurrence="o1", consumer_occurrence="o2")]  # o2 续写 o1 的交付

    def htn():
        return SimpleNamespace(
            active_plan_revision=lambda mission_id: SimpleNamespace(revision=1),
            list_plan_memberships=lambda mission_id, revision: members,
            list_order_constraints=lambda mission_id, revision: (),
            list_data_requirements=lambda mission_id, revision: tuple(edges),
            list_acceptances=lambda mission_id: (acceptance(first, other), acceptance(second)))

    graph = htn()
    [(chosen, _, leaf)] = _sources(store, graph, "m", _leaves(store, graph, "m"), "README.md")
    assert (chosen.id, leaf.task.id) == ("a2", "t2")
    # 两步互不相连：判断不了用哪一版，交给规划器
    edges.clear()
    graph = htn()
    assert len(_sources(store, graph, "m", _leaves(store, graph, "m"), "README.md")) == 2


def test_the_declared_producer_decides_which_file_is_published():
    """2026-09-29 第 5 批：计划里声明写出 README.md 的步骤（outputs 含它）是锚：在它和它下游
    里按路径完全相同取最下游一版；并行分支里同名文件、只是文件名相同的文件都不再干扰。"""
    from agent_orchestrator.contracts.htn import TaskForm
    from agent_orchestrator.orchestrator.system_operations import _leaves, _sources

    def artifact(ident, path):
        return SimpleNamespace(id=ident, path=path, content_hash=ident * 2, verification_status="VERIFIED")

    items = [artifact("a1", "README.md"), artifact("a2", "README.md"), artifact("a3", "README.md"),
             artifact("a4", "docs/README.md")]
    tasks = {
        "t1": SimpleNamespace(id="t1", accepted_result_id="r1", outputs=("README.md",)),  # 声明产出
        "t2": SimpleNamespace(id="t2", accepted_result_id="r2", outputs=()),  # t1 下游续写
        "t3": SimpleNamespace(id="t3", accepted_result_id="r3", outputs=()),  # 并行分支
    }
    results = {"r1": SimpleNamespace(artifacts=("a1",)), "r2": SimpleNamespace(artifacts=("a2", "a4")),
               "r3": SimpleNamespace(artifacts=("a3",))}
    by_id = {a.id: a for a in items}
    store = SimpleNamespace(get_task=tasks.get, get_result=results.get, get_artifact=by_id.get)
    members = tuple(SimpleNamespace(occurrence_id=f"o{n}", task_id=f"t{n}", form=TaskForm.PRIMITIVE)
                    for n in (1, 2, 3))
    graph = SimpleNamespace(
        active_plan_revision=lambda mission_id: SimpleNamespace(revision=1),
        list_plan_memberships=lambda mission_id, revision: members,
        list_order_constraints=lambda mission_id, revision: (),
        list_data_requirements=lambda mission_id, revision: (
            SimpleNamespace(producer_occurrence="o1", consumer_occurrence="o2"),),
        list_acceptances=lambda mission_id: (SimpleNamespace(validity="CURRENT", artifact_refs=tuple(
            SimpleNamespace(id=a.id, content_hash=a.content_hash) for a in items)),))
    [(chosen, _, leaf)] = _sources(store, graph, "m", _leaves(store, graph, "m"), "README.md")
    assert (chosen.id, leaf.task.id) == ("a2", "t2")
    # 没有声明时照旧：三个同名版本里 t1→t2 与 t3 互不相连，判断不了（多个匹配交给规划器）
    tasks["t1"].outputs = ()
    assert len(_sources(store, graph, "m", _leaves(store, graph, "m"), "README.md")) > 1


def test_only_the_confirming_person_can_carry_the_slot_authority(tmp_path):
    case = _world(tmp_path)
    world = case["world"]
    [plan] = pending_system_operations(case["orch"], world.mission.id)
    with pytest.raises(ValueError, match="AUTHORIZED_SLOT authority differs"):
        world.service.submit_operation_intent(
            plan["command"], tenant_id=plan["tenant_id"], principal=Principal("someone-else"))
    forged = dataclasses.replace(plan["command"], intent_source=OperationIntentSourceV2(
        OperationIntentSourceKind.AUTHORIZED_SLOT, origin_receipt_id=plan["command"].intent_source.origin_receipt_id,
        slot_key="another-effect"))
    with pytest.raises(ValueError, match="AUTHORIZED_SLOT authority differs"):
        world.service.submit_operation_intent(
            forged, tenant_id=plan["tenant_id"], principal=Principal(plan["principal_id"]))

"""Exercise H4 retry and runtime wakes through persisted production entry points."""
import asyncio
import json
from dataclasses import replace

import pytest

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.commit_service import CommitRejected, Reservation
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.planning_repair_requests import collect_triggers, pending_requests
from agent_orchestrator.orchestrator.planning_retry import pending_retry_permit
from agent_orchestrator.orchestrator.planning_runtime_block import (
    WOKEN, blocks_intent, last_wake, pending_block, resume_source_current, wake_blocks,
)
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from test_h1i_production_entry import _config, _seed_new_protocol, _refine_reply


def grant(loop, mission, intent):
    PlanningAuthorizationApi(loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)).issue(
        mission.id, command_id="grant-" + intent.intent_id, request_id=intent.intent_id)


async def refined(loop, tmp_path, key):
    mission, world, binding, dispatch = _seed_new_protocol(loop, tmp_path, key=key)
    intent = await loop._create_planner_intent(mission.id, ordinal=1)
    grant(loop, mission, intent)
    await loop._collect_plan_decision(intent, object(), mission, _refine_reply(intent.config["planning_package"]), dispatch)
    row = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(intent.intent_id, 0)
    assert row["status"] == "COMMITTED", row["detail_json"]
    target = next(b for b in dispatch.network(mission.id).task_bindings
                  if str(b.form) == "primitive" and not b.input_ports)
    return loop.store.get_mission(mission.id), dispatch, str(target.task_id)


def attempt(loop, task_id, retry_of=None):
    return loop.commit.create_attempt(task_id, role="worker", model="fixture-worker",
        prompt_version="fixture-worker-v1", context_version="h4-runtime-v1",
        reservation=Reservation(tokens=1000, cost_micros=0),
        intent_config={"message": "Exercise a failed dispatch and its admitted recovery."},
        input_hash="e" * 64, inputs=(), retry_of=retry_of)


async def repair(loop, mission, dispatch, task_id, payload, ordinal=2):
    intent = await loop._create_planner_intent(mission.id, ordinal=ordinal)
    package = intent.config["planning_package"]
    subject = next(s for s in package["planning_subjects"] if s["task_id"] == task_id)
    body = {"schema_version": 1, "decision_type": "REPAIR", "subject_key": subject["subject_key"],
        "rationale": "Recover the recorded failed execution.", "reason_refs": [], "assumptions": [],
        "uncertainties": [], "alternatives": [], "replan_triggers": [], "payload": payload(package)}
    grant(loop, mission, intent)
    await loop._collect_plan_decision(intent, object(), mission,
        "<planning_decision>" + json.dumps(body) + "</planning_decision>", dispatch)
    row = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(intent.intent_id, 0)
    return intent, row


def retry_payload(dispatch, mission, task_id, failed_id, package):
    network = dispatch.network(mission.id)
    occurrence = next(s.occurrence_id for s in network.occurrences if str(s.task_id) == task_id)
    instance = next(i for i in network.method_instances
                    if any((c.goal_occurrence_id or c.occurrence_id) == occurrence for c in i.child_bindings))
    ref = next(r for r in package["visible_refs"]
               if r["kind"] == "method_instance" and r["id"] == str(instance.instance_id))
    return {"repair_kind": "RETRY_SAME_METHOD", "failed_attempt_id": failed_id, "method_instance_ref": ref}


def test_timeout_opens_repair_and_committed_retry_is_single_use(tmp_path):
    async def case():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, dispatch, task_id = await refined(loop, tmp_path, "h4-retry-live")
            first, first_intent = attempt(loop, task_id)
            loop.commit.claim_intent(first_intent.intent_id, owner=loop._owner, lease_seconds=60)
            loop.commit.record_agent_created(first_intent.intent_id, agent_id="fixture", expected_turn_id="turn-1")
            loop.commit.record_submitted(first_intent.intent_id, receipt={"turn_id": "turn-1", "seq": 1})
            loop.commit.mark_attempt_timed_out(first.id, reason="stalled", detail={})
            assert collect_triggers(loop, mission)
            assert len(pending_requests(loop.store, mission.id)) == 1
            with pytest.raises(CommitRejected, match="current committed RETRY_SAME_METHOD"):
                attempt(loop, task_id, first.id)
            def payload(package):
                return retry_payload(dispatch, mission, task_id, first.id, package)
            _, row = await repair(loop, mission, dispatch, task_id, payload)
            assert row["status"] == "COMMITTED", row["detail_json"]
            assert pending_retry_permit(loop.store, mission.id, task_id) is not None
            assert not pending_requests(loop.store, mission.id)
            dispatch.issue_start_witnesses(mission.id)
            assert await loop._next_attempt(loop.store.get_mission(mission.id), loop.store.get_task(task_id),
                                            loop.store.list_attempts(task_id))
            attempts = loop.store.list_attempts(task_id)
            assert len(attempts) == 2
            second = attempts[-1]
            second_intent = loop.store.get_intent_for_subject(second.id)
            assert second.retry_of == first.id
            assert second_intent.config["planning_retry_decision_id"] == row["decision_id"]
            assert pending_retry_permit(loop.store, mission.id, task_id) is None
            with pytest.raises(CommitRejected):
                attempt(loop, task_id, first.id)
    asyncio.run(case())


def test_runtime_wakes_fence_stale_planner_and_repeated_source_transitions(tmp_path):
    async def case():
        async with Orchestrator(replace(_config(tmp_path), max_planning_attempts=6), RoleScriptedProvider({"planner": []})) as loop:
            mission, dispatch, task_id = await refined(loop, tmp_path, "h4-runtime-live")
            first, _ = attempt(loop, task_id)
            loop.commit.mark_attempt_lost(first.id, reason="runtime_unavailable")
            assert collect_triggers(loop, mission)
            def payload(package):
                request = next(r for r in package["repair_requests"]
                               if r["request"]["trigger_source"] == "RUNTIME_UNAVAILABLE")
                return {"repair_kind": "DECLARE_RUNTIME_BLOCKED", "repair_request_id": request["request_id"],
                        "blockers": [{"code": "OTHER", "detail": "Recorded executor is unavailable."}],
                        "resumable_if": ["plan_revision_changed"]}
            _, row = await repair(loop, mission, dispatch, task_id, payload)
            assert row["status"] == "NO_STATE_CHANGE", row["detail_json"]
            assert pending_block(loop.store, mission.id) is not None
            assert not wake_blocks(loop)
            with pytest.raises(CommitRejected, match="runtime block"):
                attempt(loop, task_id, first.id)
            def unavailable():
                loop.commit.record_profile_failure("default", error={"reason": "unavailable"},
                    threshold=1, cooldown_seconds=3600, mission_ids=(mission.id,))
            unavailable()
            assert wake_blocks(loop)
            loop.commit.record_profile_success("default")
            assert wake_blocks(loop)
            resumed = await loop._create_planner_intent(mission.id, ordinal=3)
            assert resume_source_current(loop, resumed, mission)
            assert not blocks_intent(loop.store, resumed)
            unavailable()
            assert not resume_source_current(loop, resumed, mission)
            assert wake_blocks(loop)
            assert blocks_intent(loop.store, resumed)
            assert loop.store.get_intent(resumed.intent_id).state == "FAILED"
            loop.commit.record_profile_success("default")
            assert wake_blocks(loop)
            assert not wake_blocks(loop)
            wakes = [e for e in loop.store.iter_events(mission.id) if e.type == WOKEN]
            assert len(wakes) == 4
            assert len({e.payload["decision_id"] for e in wakes}) == 4
            assert wakes[0].payload["source_hash"] == wakes[2].payload["source_hash"]
            assert wakes[1].payload["source_hash"] == wakes[3].payload["source_hash"]
            block = pending_block(loop.store, mission.id)
            assert last_wake(loop.store, mission.id, block).id == wakes[-1].id
            assert not resume_source_current(loop, resumed, mission)
            def retry(package):
                return retry_payload(dispatch, mission, task_id, first.id, package)
            _, retried = await repair(loop, mission, dispatch, task_id, retry, ordinal=4)
            assert retried["status"] == "COMMITTED", retried["detail_json"]
            assert pending_block(loop.store, mission.id) is None
            second, _ = attempt(loop, task_id, first.id)
            assert second.retry_of == first.id
    asyncio.run(case())


async def _owed_repair(loop, tmp_path, key):
    """A committed plan, a leaf whose attempt timed out, and the repair still owed."""
    mission, dispatch, task_id = await refined(loop, tmp_path, key)
    first, first_intent = attempt(loop, task_id)
    loop.commit.claim_intent(first_intent.intent_id, owner=loop._owner, lease_seconds=60)
    loop.commit.record_agent_created(first_intent.intent_id, agent_id="fixture", expected_turn_id="turn-1")
    loop.commit.record_submitted(first_intent.intent_id, receipt={"turn_id": "turn-1", "seq": 1})
    loop.commit.mark_attempt_timed_out(first.id, reason="stalled", detail={})
    assert collect_triggers(loop, mission) and pending_requests(loop.store, mission.id)
    return loop.store.get_mission(mission.id), task_id


def test_a_refused_repair_round_opens_the_next_round_instead_of_idling(tmp_path):
    """2026-09-25 desktop run: the repair round's ordinal (4, counting the committed
    rounds) was over the ladder although it was the first refusal, so the refusal was
    only noted and the Mission sat ACTIVE with no work and no human request."""

    async def case():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _task_id = await _owed_repair(loop, tmp_path, "h4-refused-repair")
            refused = await loop._create_planner_intent(mission.id, ordinal=4)
            before = {i for i in range(1, 10) if loop.store.get_intent_for_subject(f"{mission.id}:planner:{i}")}
            await loop._planning_rejected(refused, reason="proposal_not_grounded", detail={"why": "fixture"})
            assert loop.store.get_mission(mission.id).status.value == "ACTIVE"
            after = {i for i in range(1, 10) if loop.store.get_intent_for_subject(f"{mission.id}:planner:{i}")}
            assert len(after - before) == 1  # the next round, not silence
    asyncio.run(case())


def test_a_spent_repair_ladder_ends_the_mission_by_name(tmp_path):
    async def case():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _task_id = await _owed_repair(loop, tmp_path, "h4-spent-repair")
            for n in (2, 3):
                loop.commit.record_planning_rejected(mission.id, ordinal=n, reason="proposal_not_grounded", detail={})
            refused = await loop._create_planner_intent(mission.id, ordinal=4)
            await loop._planning_rejected(refused, reason="proposal_not_grounded", detail={})
            final = loop.store.get_mission(mission.id)
            assert final.status.value == "FAILED", final.status
    asyncio.run(case())


def test_a_failed_attempt_with_an_unknown_charge_can_still_be_retried(tmp_path):
    # 2026-09-25 UI 全量点击：工人一轮因工具参数 JSON 不合法失败，这次调用的用量
    # 记为未知；以前 RETRY_SAME_METHOD 一律被拒，文档任务停滞失败。按记账硬规则
    # （多算不少算、不冻结），未知费用留在原预留上，重试另开一次 Attempt。
    async def case():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, dispatch, task_id = await refined(loop, tmp_path, "h4-retry-unknown-charge")
            first, first_intent = attempt(loop, task_id)
            loop.commit.claim_intent(first_intent.intent_id, owner=loop._owner, lease_seconds=60)
            loop.commit.record_agent_created(first_intent.intent_id, agent_id="fixture", expected_turn_id="turn-1")
            loop.commit.record_submitted(first_intent.intent_id, receipt={"turn_id": "turn-1", "seq": 1})
            loop.commit.mark_attempt_timed_out(first.id, reason="stalled", detail={})
            with loop.store.transaction():
                loop.store.connection.execute(
                    "INSERT INTO imported_usage(usage_ref,subject_id,mission_id,input_tokens,output_tokens,"
                    "cost_micros,unpriced,unknown,imported_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("usage-unknown-1", first.id, mission.id, 0, 0, None, 1, 1, 1.0))
            assert loop.commit.ledger.has_unknown_usage(first.id)
            def payload(package):
                return retry_payload(dispatch, mission, task_id, first.id, package)
            _, row = await repair(loop, mission, dispatch, task_id, payload)
            assert row["status"] == "COMMITTED", row["detail_json"]
            assert pending_retry_permit(loop.store, mission.id, task_id) is not None
    asyncio.run(case())


def test_a_step_that_reports_blocked_goes_back_to_the_planner(tmp_path):
    """2026-09-29 真机第十、十一局：测试步如实报告"卡住"（工作区缺 wordfreq.py），尝试进了
    "等重试"，但这个结果不在"转规划修补"的来源里——没人问规划器，几秒后判"没有可派发
    的工作"，整局失败。卡住 / 失败 / 没进展的如实报告都应交给规划器，带上步骤的说明。

    **Mutation**: drop ``OutcomeRecorded`` from the sources → no request, red."""
    from agent_orchestrator.contracts.models import ResultEnvelope, ResultOutcome

    async def case():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, dispatch, task_id = await refined(loop, tmp_path, "h4-blocked-live")
            first, first_intent = attempt(loop, task_id)
            loop.commit.claim_intent(first_intent.intent_id, owner=loop._owner, lease_seconds=60)
            loop.commit.record_agent_created(first_intent.intent_id, agent_id="fixture", expected_turn_id="turn-1")
            loop.commit.record_submitted(first_intent.intent_id, receipt={"turn_id": "turn-1", "seq": 1})
            summary = "工作区里没有 wordfreq.py，pytest 收集阶段 ModuleNotFoundError，本步骤无法完成"
            loop.commit.record_outcome_result(first.id, envelope=ResultEnvelope(
                id="result-blocked-1", task_id=task_id, attempt_id=first.id, outcome=ResultOutcome.BLOCKED,
                summary=summary, claims=(), evidence=(), artifacts=(), proposed_tasks=(),
                used_knowledge=(), risks=(), cost={}, mission_id=mission.id), turn_id="turn-1", usage_refs=())
            assert collect_triggers(loop, mission)
            requests = pending_requests(loop.store, mission.id)
            assert len(requests) == 1
            request = requests[0]["request"]
            assert request["trigger_source"] == "WORKER_REJECT"
            assert summary in json.dumps(request, ensure_ascii=False)
            assert not collect_triggers(loop, mission), "the same report opens one request only"
    asyncio.run(case())

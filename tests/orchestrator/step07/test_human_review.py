# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 7 · slice D (D7-8' / D7-9', S7-07): the sixth verification layer, NEEDS_HUMAN,
Verifier conflicts sent to a person, takeover and comments — a person's word is kept as
HumanOverride with its basis, and it never widens what was authorised."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, envelope_step, graph_proposal_step
from graph_helpers7 import complete, drive_to_running
from helpers_step07 import ALICE, BOB, ledger_service

from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.action_commits import ActionCommitError
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import package_of, role_of

TOOLS = ["workspace_read_file", "workspace_write_file", "workspace_list", "run_tests"]
SEED = {
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "tests/test_bad.py": "def test_bad():\n    assert False\n",
}
FULL = ["format_check", "rule_check", "critic_review", "code_test"]


def _config(tmp_path):
    return OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )


def _spec(key, criteria=("file:REPORT.md",)):
    return MissionSpec(
        goal="写一份变更报告",
        success_criteria=tuple(criteria),
        tenant_id="tenant-7",
        idempotency_key=key,
        allowed_tools=tuple(TOOLS),
        budget=Budget(max_tokens=300_000, max_attempts=8),
        workspace_seed=SEED,
    )


def _task(key, policy, *, deps=(), criteria=("file:REPORT.md",), attempts=2):
    return {
        "key": key,
        "goal": f"写变更报告（{key}）",
        "rationale": "报告需要人看过才算数",
        "dependencies": list(deps),
        "success_criteria": list(criteria),
        "verification_policy": list(policy),
        "outputs": ["REPORT.md"],
        "allowed_tools": TOOLS,
        "budget": {"max_tokens": 30_000, "max_attempts": attempts},
        "priority": 1.0,
    }


def _worker(text="# 报告\n\n改了一个开关。\n", summary="写好了报告"):
    return [
        ("workspace_list", {}),
        ("workspace_write_file", {"path": "REPORT.md", "content": text}),
        envelope_step(summary=summary, artifacts=["REPORT.md"], claims=["报告已写好"]),
    ]


def critic_needs_human(request):
    """A Critic that finds no blocker but cannot reliably judge (original §22)."""

    criteria = package_of(request).get("mission_success_criteria", [])
    body = {
        "verdict": "PASS",
        "findings": [],
        "needs_human": True,
        "mission_criteria": [{"criterion": c, "met": True, "reason": "scripted"} for c in criteria],
    }
    return "<critic_verdict>" + json.dumps(body, ensure_ascii=False) + "</critic_verdict>"


def _layers(store, result_id):
    return {v["layer"]: v["status"] for v in store.list_verifications(result_id)}


# ------------------------------------------------------------------ the sixth layer
def test_s7_07_a_policy_review_suspends_survives_a_restart_and_resumes_on_a_pass(tmp_path):
    cfg = _config(tmp_path)
    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step([_task("A", ["format_check", "rule_check", "human_review"])])
            ],
            "worker": _worker(),
        }
    )

    async def first():
        async with Orchestrator(cfg, provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("r1"))
            await orchestrator.run()
            await orchestrator.run()  # idle: a suspended result is not picked up again
            store = orchestrator.store
            [task] = store.list_tasks(mission.id)
            assert task.status is TaskStatus.VERIFYING
            [request] = store.list_approvals(mission.id)
            assert (request["kind"], request["state"], request["reason"]) == (
                "review",
                "PENDING",
                "policy",
            )
            assert store.get_result(request["subject_key"]).verification_state == "SUSPENDED"
            assert [w["kind"] for w in store.waiting_on(mission.id)] == ["review"]
            assert store.count_events(mission.id, "VerificationSuspended") == 1
            with pytest.raises(ActionCommitError):  # a review is not an action approval
                orchestrator.commit.decide_approval(
                    request["request_id"],
                    principal=ALICE,
                    decision="grant",
                    nonce="x",
                    deployment=cfg.deployment_policy,
                )
            return mission.id, request

    mission_id, request = asyncio.run(first())

    async def second():
        async with Orchestrator(cfg, provider) as orchestrator:
            orchestrator.commit.review_result(
                request["request_id"],
                principal=ALICE,
                verdict="pass",
                note="看过了，可以",
                nonce="n-1",
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission_id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            layers = _layers(store, request["subject_key"])
            assert layers["human_review"] == "PASS" and layers["rule_check"] == "PASS"
            [granted] = [e for e in store.list_events(mission_id) if e.type == "ApprovalGranted"]
            assert (granted.actor_type, granted.actor_id, granted.payload["kind"]) == (
                "user",
                "alice",
                "review",
            )

    asyncio.run(second())
    assert provider.by_role.get("worker") == 3  # one Attempt; the review re-used what passed


def test_s7_07_needs_human_forces_the_person_after_the_tests_ran_and_the_critic_is_asked_once(
    tmp_path,
):
    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step(
                    [_task("A", FULL, criteria=("file:REPORT.md", "pytest:tests/test_ok.py"))]
                )
            ],
            "worker": _worker(),
            "critic": [critic_needs_human],
        }
    )

    async def case():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("r2"))
            await orchestrator.run()
            store = orchestrator.store
            [request] = store.list_approvals(mission.id)
            assert request["reason"] == "needs_human"
            layers = _layers(store, request["subject_key"])
            assert (layers["critic_review"], layers["code_test"], layers["human_review"]) == (
                "NEEDS_HUMAN",
                "PASS",  # the tests ran before anyone was asked
                "SUSPENDED",
            )
            orchestrator.commit.review_result(
                request["request_id"], principal=BOB, verdict="pass", note="", nonce="n-1"
            )
            await orchestrator.run()
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED

    asyncio.run(case())
    assert provider.by_role.get("critic") == 1  # its NEEDS_HUMAN was reused on resume


def test_s7_07_a_person_is_never_asked_to_cover_a_failing_test(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step(
                    [
                        _task(
                            "A",
                            FULL,
                            criteria=("file:REPORT.md", "pytest:tests/test_bad.py"),
                            attempts=1,
                        )
                    ]
                )
            ],
            "worker": _worker(),
            "critic": [critic_needs_human],
        }
    )

    async def case():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("r3"))
            await orchestrator.run()
            store = orchestrator.store
            assert store.list_approvals(mission.id) == []  # nobody was asked
            [failed] = [e for e in store.list_events(mission.id) if e.type == "VerificationFailed"]
            assert [f["layer"] for f in failed.payload["failures"]] == ["code_test"]
            assert store.get_mission(mission.id).status is MissionStatus.FAILED

    asyncio.run(case())


def test_a_person_s_fail_reaches_the_next_attempt_and_a_task_escalates_only_once(tmp_path):
    note = "报告缺少回滚步骤"
    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step([_task("A", ["format_check", "rule_check", "critic_review"])])
            ],
            "worker": _worker() + _worker(text="# 报告\n\n回滚：把开关关掉。\n"),
            "critic": [critic_needs_human, critic_needs_human],
        }
    )

    async def case():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("r4"))
            await orchestrator.run()
            [request] = orchestrator.store.list_approvals(mission.id)
            orchestrator.commit.review_result(
                request["request_id"], principal=ALICE, verdict="fail", note=note, nonce="n-1"
            )
            await orchestrator.run()
            store = orchestrator.store
            assert len(store.list_approvals(mission.id)) == 1  # the second needs_human is a FAIL
            failures = [
                e.payload["failures"]
                for e in store.list_events(mission.id)
                if e.type == "VerificationFailed"
            ]
            assert failures[0][0]["layer"] == "human_review" and note in failures[0][0]["summary"]
            assert (
                failures[1][0]["layer"] == "critic_review"
                and "one escalation" in failures[1][0]["summary"]
            )
            assert store.get_mission(mission.id).status is MissionStatus.FAILED

    asyncio.run(case())
    worker_packages = [
        json.dumps(package_of(r), ensure_ascii=False)
        for r in provider.requests
        if role_of(r) == "worker"
    ]
    assert (
        note not in worker_packages[0] and note in worker_packages[3]
    )  # the 2nd Attempt's first turn


def test_a_suspended_review_ends_with_its_mission(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step([_task("A", ["format_check", "rule_check", "human_review"])])
            ],
            "worker": _worker(),
        }
    )

    async def case():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("r5"))
            await orchestrator.run()
            [request] = orchestrator.store.list_approvals(mission.id)
            orchestrator.commit.cancel_mission(mission.id)
            store = orchestrator.store
            assert store.get_approval(request["request_id"])["state"] == "CANCELLED"
            assert store.get_result(request["subject_key"]).verification_state == "REJECTED"
            with pytest.raises(ActionCommitError):
                orchestrator.commit.review_result(
                    request["request_id"], principal=ALICE, verdict="pass", note="", nonce="n-1"
                )

    asyncio.run(case())


# ------------------------------------------------------------------ Verifier conflicts
@pytest.mark.parametrize("ruling", ["met", "unmet"])
def test_s7_07_a_judge_that_disagrees_with_the_task_critics_goes_to_a_person(tmp_path, ruling):
    criteria = ("file:REPORT.md", "报告写明了回滚方式")
    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step(
                    [
                        _task("A", ["format_check", "rule_check", "critic_review"]),
                        _task("B", ["format_check", "rule_check", "critic_review"], deps=["A"]),
                    ]
                )
            ],
            "worker": _worker() + _worker(text="# 报告\n\n第 3 节写了回滚。\n"),
            "critic": [
                critic_step(verdict="PASS", criteria_met=True),
                critic_step(verdict="PASS", criteria_met=True),
                critic_step(verdict="PASS", criteria_met=False),  # the independent judge disagrees
            ],
        }
    )

    async def case():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec(f"j-{ruling}", criteria=criteria))
            await orchestrator.run()
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.ACTIVE
            [request] = store.list_approvals(mission.id)
            assert (request["kind"], request["topic"]) == ("arbitration", "judgment")
            assert request["context"]["criteria"] == ["报告写明了回滚方式"]
            orchestrator.commit.arbitrate(
                request["request_id"],
                principal=BOB,
                ruling=ruling,
                basis="我读了报告第 3 节",
                nonce="n-1",
            )
            await orchestrator.run()
            final = store.get_mission(mission.id)
            expected = MissionStatus.COMPLETED if ruling == "met" else MissionStatus.FAILED
            assert final.status is expected, orchestrator.progress_log
            judged = {j["criterion"]: j for j in final.final_report["success_criteria"]}
            assert judged["报告写明了回滚方式"]["judge"] == "human_arbitration"
            [override] = store.list_overrides(mission.id)
            assert (
                override["basis"] == "我读了报告第 3 节"
                and override["principal"]["principal_id"] == "bob"
            )

    asyncio.run(case())
    assert provider.by_role.get("critic") == 3  # the judge ran once; waiting did not re-run it


def _conflict(service, mission, task):
    conflict_id = f"{mission.id}:conflict-1"
    service.store.upsert_conflict(
        {
            "conflict_id": conflict_id,
            "mission_id": mission.id,
            "key": "impl.empty_input",
            "state": "OPEN",
            "task_id": task.id,
            "claim_ids": ["claim-a", "claim-b"],
            "sides": [{"claim_id": "claim-a"}, {"claim_id": "claim-b"}],
            "opened_by": "result-x",
            "created_at": 1.0,
            "version": 1,
            "resolution_knowledge_id": None,
        }
    )
    request, created = service.request_arbitration(
        mission.id,
        subject=conflict_id,
        topic="conflict",
        options=["keep:claim-a", "keep:claim-b", "unresolved"],
        context={"key": "impl.empty_input"},
        task_id=task.id,
    )
    assert (
        created
        and service.request_arbitration(
            mission.id, subject=conflict_id, topic="conflict", options=[], context={}
        )[1]
        is False
    )
    return conflict_id, request


def test_a_person_settles_a_conflict_the_conflict_task_could_not(tmp_path):
    service, mission, t, _config, _connectors, _ = ledger_service(tmp_path)
    attempt = drive_to_running(service, t["A"])
    conflict_id, request = _conflict(service, mission, t["A"])
    with pytest.raises(ActionCommitError):
        service.arbitrate(
            request["request_id"], principal=ALICE, ruling="keep:claim-z", basis="x", nonce="n-0"
        )
    ruled = service.arbitrate(
        request["request_id"],
        principal=ALICE,
        ruling="keep:claim-a",
        basis="复现了 A 的探针",
        nonce="n-1",
    )
    assert (
        service.arbitrate(
            request["request_id"],
            principal=ALICE,
            ruling="keep:claim-a",
            basis="复现了 A 的探针",
            nonce="n-1",
        )
        == ruled
    )
    conflict = service.store.get_conflict(conflict_id)
    assert (
        conflict["state"] == "RESOLVED_BY_HUMAN" and conflict["resolution"]["claim_id"] == "claim-a"
    )
    assert service.store.get_attempt(attempt.id).status is AttemptStatus.CANCELLED
    assert service.store.get_task(t["A"].id).status is TaskStatus.CANCELLED
    assert service.store.get_mission(mission.id).status is MissionStatus.ACTIVE
    [override] = service.store.list_overrides(mission.id)
    assert override["ruling"] == "keep:claim-a" and override["scope"].startswith(
        "this conflict only"
    )


def test_a_person_can_confirm_a_conflict_is_unresolved_and_the_mission_stops(tmp_path):
    service, mission, t, _config, _connectors, _ = ledger_service(tmp_path)
    drive_to_running(service, t["A"])
    _conflict_id, request = _conflict(service, mission, t["A"])
    service.arbitrate(
        request["request_id"],
        principal=ALICE,
        ruling="unresolved",
        basis="两边证据都站得住",
        nonce="n-1",
    )
    final = service.store.get_mission(mission.id)
    assert final.status is MissionStatus.FAILED and final.stop_reason == "human_override"


# ------------------------------------------------------------------ takeover and comments
def test_s7_07_a_takeover_stop_ends_the_task_with_its_basis_on_the_books(tmp_path):
    service, mission, t, _config, _connectors, _ = ledger_service(tmp_path)
    attempt = drive_to_running(service, t["A"])
    record = service.takeover(
        t["A"].id, principal=ALICE, action="stop", basis="卡住 40 分钟没有进展"
    )
    assert record["action"] == "takeover_stop" and "no approval, tool, budget" in record["scope"]
    final = service.store.get_mission(mission.id)
    assert final.status is MissionStatus.FAILED and final.stop_reason == "human_override"
    assert final.final_report["detail"]["basis"] == "卡住 40 分钟没有进展"
    assert service.store.get_attempt(attempt.id).status is AttemptStatus.CANCELLED
    [event] = [e for e in service.store.list_events(mission.id) if e.type == "HumanOverride"]
    assert (event.actor_type, event.actor_id) == ("user", "alice")


def test_s7_07_retry_with_note_stays_within_the_task_and_carries_the_note(tmp_path):
    service, mission, t, _config, _connectors, _ = ledger_service(tmp_path)
    attempt = drive_to_running(service, t["A"])
    service.takeover(
        t["A"].id,
        principal=BOB,
        action="retry_with_note",
        basis="方向不对",
        note="先读 docs/SPEC.md",
    )
    closed = service.store.get_attempt(attempt.id)
    assert (
        closed.status is AttemptStatus.CANCELLED
        and closed.failure["reason"] == "human_retry_with_note"
    )
    [comment] = [e for e in service.store.list_events(mission.id) if e.type == "HumanCommentAdded"]
    assert (
        comment.payload["target_id"] == t["A"].id and comment.payload["text"] == "先读 docs/SPEC.md"
    )
    assert service.store.get_mission(mission.id).status is MissionStatus.ACTIVE
    task = service.store.get_task(t["A"].id)
    assert (
        task.status is TaskStatus.ACTIVE and task.budget.max_attempts == t["A"].budget.max_attempts
    )
    with pytest.raises(ActionCommitError):  # only stop / retry_with_note exist
        service.takeover(t["A"].id, principal=BOB, action="approve", basis="x")
    with pytest.raises(ActionCommitError):
        service.takeover(
            t["A"].id, principal=BOB, action="stop", basis="sk-" + "a" * 40
        )  # looks like a secret


def test_a_takeover_never_revives_an_ended_task_or_mission(tmp_path):
    service, mission, t, _config, _connectors, _ = ledger_service(tmp_path)
    complete(service, t["A"])
    with pytest.raises(ActionCommitError):
        service.takeover(t["A"].id, principal=ALICE, action="retry_with_note", basis="再来一次")
    service.cancel_mission(mission.id)
    with pytest.raises(ActionCommitError):
        service.takeover(t["B"].id, principal=ALICE, action="stop", basis="停")


def test_comments_are_kept_as_data_on_a_task_or_a_request(tmp_path):
    service, mission, t, _config, connectors, deployment = ledger_service(tmp_path)
    from helpers_step07 import candidate

    action = service.propose_action(
        candidate(),
        mission_id=mission.id,
        task_id=t["A"].id,
        result_id="result-1",
        attempt_id=f"{t['A'].id}:attempt-1",
        artifact_id="artifact-1",
        artifact_hash="a" * 64,
        connectors=connectors,
        deployment=deployment,
    )
    service.add_comment(action["approval_request_id"], principal=ALICE, text="上线窗口在周四")
    service.add_comment(t["A"].id, principal=BOB, text="注意兼容旧客户端")
    request = service.store.get_approval(action["approval_request_id"])
    assert [c["text"] for c in request["comments"]] == ["上线窗口在周四"]
    assert request["state"] == "PENDING"  # a comment decides nothing
    texts = [
        e.payload["text"]
        for e in service.store.list_events(mission.id)
        if e.type == "HumanCommentAdded"
    ]
    assert texts == ["上线窗口在周四", "注意兼容旧客户端"]
